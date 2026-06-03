#!/usr/bin/env python3
"""
Contract valuation tool: compute fair contract value for a player using
VPC (dollars per VOS point) calibrated from league MLB salaries, then
project per-year VOS with an age curve and hand off to contract_builder.py
for structuring.

Pipeline:
  1. Load latest evaluation_summary_<league>_*.csv
  2. Calibrate VPC via farm_value_old.compute_vpc_base
  3. Locate target player, grab VOS_Current, VOS_Potential, Age, Pos
  4. Project per-year VOS across contract years using age curve
  5. Apply per-year type multipliers (arb / extension_fa / market) + risk discount
  6. Sum to total_fair_value, call contract_builder.build_contract()
  7. Print valuation table + structured contract

Run:
  python contract.py --league sahl --id 12345 --years 5 --type extension --arb-years 2
  python contract.py --league sahl --id 12345 --years 4 --type market
"""

from __future__ import annotations
# --- repo-root + core/ path bootstrap ---
import os as _os, sys as _sys
_ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
for _p in (_ROOT, _os.path.join(_ROOT, "core")):
    if _p not in _sys.path:
        _sys.path.insert(0, _p)
# --- end bootstrap ---


import argparse
import json
import logging
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.error import URLError

# Reuse existing modules — do not modify.
import farm_value_old as fv
import contract_builder as cb

SCRIPT_DIR = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = SCRIPT_DIR / "config" / "contract_config.json"
DEFAULT_LEAGUE_URL_CFG = SCRIPT_DIR / "config" / "league_url.json"

logger = logging.getLogger("contract")


# ---------- Config loading ----------------------------------------------------

def load_config(path: Path) -> Dict:
    if not path.exists():
        raise FileNotFoundError(f"Config not found: {path}")
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


# ---------- Player lookup -----------------------------------------------------

@dataclass
class PlayerSnapshot:
    pid: str
    name: str
    age: int
    pos: str
    vos_current: float
    vos_potential: float
    is_pitcher: bool


def _is_pitcher_pos(pos: str) -> bool:
    p = (pos or "").strip().upper()
    return p in {"SP", "RP", "CL", "P", "MR", "SU"} or p.startswith("P")


def locate_player(rows: List[Dict[str, str]], pid: str) -> PlayerSnapshot:
    pid = str(pid).strip()
    for r in rows:
        if (r.get("ID") or "").strip() == pid:
            pos = (r.get("Pos") or "").strip()
            return PlayerSnapshot(
                pid=pid,
                name=(r.get("Name") or "").strip() or pid,
                age=int(fv.to_float(r.get("Age"), 0)),
                pos=pos,
                vos_current=fv.to_float(r.get("VOS_Score"), 0.0),
                vos_potential=fv.to_float(r.get("VOS_Potential"), 0.0),
                is_pitcher=_is_pitcher_pos(pos),
            )
    raise ValueError(f"Player ID {pid} not found in evaluation file.")


# ---------- Age curve ---------------------------------------------------------

def project_vos_year(
    age: int,
    vos_current: float,
    vos_potential: float,
    years_from_now: int,
    curve: Dict,
) -> float:
    """Project VOS at (age + years_from_now).

    Before peak: close ramp_per_year fraction of the gap toward Potential each year.
    In peak window: hold at max(projected_so_far, Potential).
    After peak: decline by decline_per_year_post_peak each year, accelerating after
    accelerating_decline_age.
    """
    peak_start = int(curve["peak_start_age"])
    peak_end = int(curve["peak_end_age"])
    ramp = float(curve["ramp_per_year"])
    decline = float(curve["decline_per_year_post_peak"])
    accel_age = int(curve.get("accelerating_decline_age", peak_end + 5))
    accel_mult = float(curve.get("decline_accel_multiplier", 1.0))

    # Simulate year by year so decline compounds and ramp is monotone.
    v = float(vos_current)
    for step in range(years_from_now + 1):
        cur_age = age + step
        if cur_age <= peak_end:
            # Ramp phase (and peak entry). Close fraction of remaining gap.
            if cur_age >= peak_start:
                # In peak window: allow full potential to be reached.
                v = max(v, float(vos_potential))
            else:
                gap = float(vos_potential) - v
                if gap > 0:
                    v += gap * ramp
        else:
            step_decline = decline
            if cur_age >= accel_age:
                step_decline *= accel_mult
            v -= step_decline
    # Floor at a reasonable 20 (sub-replacement), cap at 80.
    return max(20.0, min(80.0, v))


# ---------- Type multipliers --------------------------------------------------

def build_type_mult_schedule(
    years: int,
    contract_type: str,
    arb_years: int,
    pre_arb_years: int,
    cfg: Dict,
) -> List[Tuple[str, float]]:
    """Return per-year list of (label, multiplier).

    contract_type:
      - market: all years at 'market' multiplier (FA open market)
      - extension: first pre_arb_years at 'pre_arb', next arb_years use
        'arb_per_year' ladder, remainder at 'extension_fa' (discount for early
        commit to FA years).
    """
    tm = cfg["type_multipliers"]
    arb_ladder = list(tm["arb_per_year"])

    out: List[Tuple[str, float]] = []
    if contract_type == "market":
        return [("FA", float(tm["market"]))] * years

    if contract_type != "extension":
        raise ValueError(f"Unknown --type {contract_type}")

    remaining = years
    # Pre-arb
    n_pre = min(pre_arb_years, remaining)
    for i in range(n_pre):
        out.append((f"Pre-arb Y{i+1}", float(tm["pre_arb"])))
    remaining -= n_pre
    # Arb
    n_arb = min(arb_years, remaining)
    for i in range(n_arb):
        # If arb_years > ladder length, extend with last rung.
        mult = arb_ladder[i] if i < len(arb_ladder) else arb_ladder[-1]
        out.append((f"Arb Y{i+1}", float(mult)))
    remaining -= n_arb
    # Extension FA buyout
    for i in range(remaining):
        out.append((f"FA-ext Y{i+1}", float(tm["extension_fa"])))
    return out


# ---------- Risk discount -----------------------------------------------------

def elite_premium_for_vos(vos: float, cfg: Dict) -> Tuple[float, str]:
    """Return (multiplier, label) for the highest tier whose min_vos <= vos."""
    block = cfg.get("elite_premium") or {}
    if not block.get("enabled", False):
        return 1.0, ""
    tiers = block.get("tiers") or []
    # Tiers may be in any order; sort descending by min_vos and pick first match.
    for t in sorted(tiers, key=lambda x: float(x.get("min_vos", 0)), reverse=True):
        if vos >= float(t.get("min_vos", 0)):
            return float(t.get("multiplier", 1.0)), str(t.get("label", ""))
    return 1.0, ""


def compute_risk_discount(snap: PlayerSnapshot, cfg: Dict) -> float:
    rd = cfg["risk_discount"]
    if not rd.get("enabled", True):
        return 0.0
    # Established MLB performer: no projection-risk discount.
    if snap.vos_current >= float(rd.get("min_mlb_vos_floor", 45)):
        base = 0.0
    else:
        gap = max(0.0, snap.vos_potential - snap.vos_current)
        base = gap * float(rd.get("per_gap_point", 0.005))
    if snap.is_pitcher:
        base += float(rd.get("pitcher_extra_discount", 0.0))
    return min(float(rd.get("max_discount", 0.25)), base)


# ---------- Valuation ---------------------------------------------------------

@dataclass
class YearValuation:
    year: int
    age: int
    label: str
    projected_vos: float
    type_mult: float
    tier_label: str
    tier_mult: float
    raw_value: float       # VPC * projected_vos
    fair_value: float      # raw_value * type_mult * tier_mult


@dataclass
class ValuationResult:
    snap: PlayerSnapshot
    vpc: float
    vpc_sample: int
    rows: List[YearValuation]
    subtotal: float           # sum of fair_value across years (pre-risk)
    risk_discount: float      # fraction, e.g. 0.07 for 7%
    total_fair_value: int     # subtotal * (1 - risk_discount), rounded


def run_valuation(
    snap: PlayerSnapshot,
    vpc: float,
    vpc_sample: int,
    years: int,
    contract_type: str,
    arb_years: int,
    pre_arb_years: int,
    cfg: Dict,
    rounding: int,
) -> ValuationResult:
    curve_block = cfg["age_curve"]["pitcher" if snap.is_pitcher else "hitter"]
    schedule = build_type_mult_schedule(years, contract_type, arb_years, pre_arb_years, cfg)

    rows: List[YearValuation] = []
    for i in range(years):
        label, tmult = schedule[i]
        projected = project_vos_year(snap.age, snap.vos_current, snap.vos_potential, i, curve_block)
        tier_mult, tier_label = elite_premium_for_vos(projected, cfg)
        raw = vpc * projected
        fair = raw * tmult * tier_mult
        rows.append(YearValuation(
            year=i + 1,
            age=snap.age + i,
            label=label,
            projected_vos=projected,
            type_mult=tmult,
            tier_label=tier_label,
            tier_mult=tier_mult,
            raw_value=raw,
            fair_value=fair,
        ))

    subtotal = sum(r.fair_value for r in rows)
    risk = compute_risk_discount(snap, cfg)
    total_raw = subtotal * (1.0 - risk)
    # Round to contract rounding
    if rounding and rounding > 0:
        total = int(round(total_raw / rounding) * rounding)
    else:
        total = int(round(total_raw))

    return ValuationResult(
        snap=snap,
        vpc=vpc,
        vpc_sample=vpc_sample,
        rows=rows,
        subtotal=subtotal,
        risk_discount=risk,
        total_fair_value=total,
    )


# ---------- Output ------------------------------------------------------------

def _fmt_dollars(x: float) -> str:
    return f"${x:,.0f}"


def format_valuation(v: ValuationResult, contract_type: str, years: int, calib_mode: str = "") -> str:
    lines: List[str] = []
    s = v.snap
    lines.append("=" * 80)
    lines.append(f"VALUATION — {s.name} (ID {s.pid}) | {s.pos} | Age {s.age}")
    lines.append("=" * 80)
    lines.append(f"VOS Current: {s.vos_current:.2f}   VOS Potential: {s.vos_potential:.2f}")
    mode_suffix = f"  [{calib_mode}]" if calib_mode else ""
    lines.append(f"VPC (calibrated): ${v.vpc:,.0f} per VOS point   (sample n={v.vpc_sample}){mode_suffix}")
    lines.append(f"Contract type: {contract_type}   Years: {years}")
    lines.append("")
    lines.append(f"{'Yr':<4}{'Age':<5}{'Phase':<12}{'VOS':<8}{'Tier':<12}{'xType':<7}{'xTier':<7}{'Raw $':<14}{'Fair $':<14}")
    lines.append("-" * 90)
    for r in v.rows:
        lines.append(
            f"{r.year:<4}{r.age:<5}{r.label:<12}{r.projected_vos:<8.2f}{r.tier_label:<12}"
            f"{r.type_mult:<7.2f}{r.tier_mult:<7.2f}"
            f"{_fmt_dollars(r.raw_value):<14}{_fmt_dollars(r.fair_value):<14}"
        )
    lines.append("-" * 90)
    lines.append(f"Subtotal (pre-risk):  {_fmt_dollars(v.subtotal)}")
    if v.risk_discount > 0:
        lines.append(f"Risk discount:        {v.risk_discount*100:.1f}%  "
                     f"({_fmt_dollars(v.subtotal * v.risk_discount)} off)")
    lines.append(f"Total fair value:     {_fmt_dollars(v.total_fair_value)}")
    lines.append(f"Implied AAV:          {_fmt_dollars(v.total_fair_value / years)}")
    lines.append("")
    return "\n".join(lines)


# ---------- CLI ---------------------------------------------------------------

def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="VPC/VOS contract valuation + structuring.")
    p.add_argument("--league", required=True, help="League slug (sahl, wwoba, ...).")
    p.add_argument("--id", required=True, help="Player ID.")
    p.add_argument("--years", type=int, required=True, help="Contract length in years.")
    p.add_argument(
        "--type", dest="contract_type", choices=["market", "extension"], default="market",
        help="Contract type. 'market' = open-market FA, 'extension' = pre-FA extension.",
    )
    p.add_argument("--arb-years", type=int, default=0,
                   help="Arb years remaining (extension only). Default 0.")
    p.add_argument("--pre-arb-years", type=int, default=0,
                   help="Pre-arb years covered (early extensions). Default 0.")
    p.add_argument("--config", type=Path, default=DEFAULT_CONFIG,
                   help="Path to contract_config.json.")
    p.add_argument("--input", type=Path, default=None,
                   help="Evaluation CSV override (otherwise uses latest for --league).")
    p.add_argument("--structure/--no-structure", dest="structure", default=True,
                   action=argparse.BooleanOptionalAction,
                   help="Also call contract_builder to produce a structured contract. Default on.")

    # Overrides passed to contract_builder
    p.add_argument("--incentives", type=int, default=None)
    p.add_argument("--incentive-cap-pct", type=float, default=None)
    p.add_argument("--use-option", action="store_true", default=None)
    p.add_argument("--option-year-value", type=int, default=None)
    p.add_argument("--buyout-pct", type=float, default=None)
    p.add_argument("--rounding", type=int, default=None,
                   help="Dollar rounding for salaries (e.g. 100000).")
    p.add_argument("--target-aav", type=int, default=None,
                   help="Build contract around this AAV (in dollars). Overrides the "
                        "VPC/VOS-derived total: total_max_value = target_aav * years. "
                        "Valuation table is still shown for reference.")
    p.add_argument("--min-annual-value", type=int, default=None)
    p.add_argument("--threshold-ip", type=float, default=None)
    p.add_argument("--incentives-per-year", action="store_true", default=None)
    p.add_argument("--no-apply-2x", action="store_true", default=False)

    # Calibration knobs
    p.add_argument("--vos-floor", type=float, default=None)
    p.add_argument("--winsor-lower", type=float, default=None)
    p.add_argument("--winsor-upper", type=float, default=None)
    p.add_argument("--salary-col", type=str, default=None)
    p.add_argument("--vos-col", type=str, default=None)
    p.add_argument("--pot-col", type=str, default=None)

    # VPC filter / calibration source
    p.add_argument("--market-only", action="store_true", default=False,
                   help="Strict VPC: calibrate only on 6+ service-year players (true FA market). "
                        "Default keeps arb + vet + multi-year guarantees (matches farm_value baseline).")
    p.add_argument("--no-players-filter", action="store_true", default=False,
                   help="Skip /players filter entirely (include pre-arb minimums). "
                        "Will inflate VPC calibration — not recommended.")
    p.add_argument("--base-url", type=str, default=None,
                   help="Override /players base URL.")
    p.add_argument("--league-url-config", type=Path, default=DEFAULT_LEAGUE_URL_CFG,
                   help="Path to league_url.json.")

    p.add_argument("--log-level", default="INFO")
    return p.parse_args(argv)


def _filter_rows_fa_only(
    rows: List[Dict[str, str]],
    players_lookup: Dict[str, Dict[str, str]],
    min_service_years: float = 6.0,
) -> List[Dict[str, str]]:
    """Keep only ML rows whose player has min_service_years+ MLB service.
    Used by --market-only to exclude arb deals from VPC calibration."""
    kept: List[Dict[str, str]] = []
    for r in rows:
        if (r.get("League_Level") or "").strip() != "ML":
            kept.append(r)  # let compute_vpc_base's own ML filter drop it
            continue
        pid = (r.get("ID") or "").strip()
        pmeta = players_lookup.get(pid)
        if pmeta is None:
            # Unknown — drop to be conservative for --market-only.
            continue
        svc = fv.to_float(pmeta.get("mlb_service_years"), 0.0)
        if svc >= min_service_years:
            kept.append(r)
    return kept


def _resolve_input(league: str, override: Optional[Path]) -> Path:
    if override is not None:
        if not override.exists():
            raise FileNotFoundError(f"--input not found: {override}")
        return override
    eval_dir = SCRIPT_DIR / league / "eval"
    return fv.resolve_input_path(None, league, eval_dir)


def _mk_contract_params(
    total_max_value: int,
    years: int,
    args: argparse.Namespace,
    defaults: Dict,
) -> cb.ContractParams:
    def pick(val, key, cast=lambda x: x):
        return cast(val) if val is not None else cast(defaults[key])
    return cb.ContractParams(
        years=years,
        total_max_value=total_max_value,
        incentives=pick(args.incentives, "incentives", int),
        incentive_cap_pct=pick(args.incentive_cap_pct, "incentive_cap_pct", float),
        use_option=pick(args.use_option, "use_option", bool),
        option_year_value=args.option_year_value,
        buyout_pct=pick(args.buyout_pct, "buyout_pct", float),
        rounding=pick(args.rounding, "rounding", int),
        incentives_per_year=pick(args.incentives_per_year, "incentives_per_year", bool),
        min_annual_value=pick(args.min_annual_value, "min_annual_value", int),
        threshold_ip=args.threshold_ip,
        apply_2x=(not args.no_apply_2x) and bool(defaults["apply_2x"]),
    )


# ---------- Fallback structurer ----------------------------------------------
# Activates when contract_builder fails to hit target. Produces a
# minimum-guaranteed, 2x-compliant structure: (N-1) years at L, one year at H=2L,
# with per-year incentive cap at p*H. Rounds cleanly and absorbs any leftover
# into the high year. Does not support options (falls through to builder's error
# if --use-option is set and the builder fails — rare; revisit if needed).

@dataclass
class FallbackYear:
    year: int
    base_salary: int
    max_incentives: List[int]


@dataclass
class FallbackContract:
    years: List[FallbackYear]
    L: int
    H: int
    total_guaranteed: int
    total_max_incentives: int
    total_max_value: int
    warnings: List[str]


def _round_down(x: int, r: int) -> int:
    if r <= 1:
        return int(x)
    return (int(x) // r) * r


def _round_nearest(x: int, r: int) -> int:
    if r <= 1:
        return int(x)
    return int(round(x / r)) * r


def fallback_structure(params: cb.ContractParams) -> FallbackContract:
    N = params.years
    p = params.incentive_cap_pct
    ninc = params.incentives
    T = params.total_max_value
    r = max(int(params.rounding), 1)
    warnings: List[str] = []

    if params.use_option:
        warnings.append("fallback ignores --use-option; producing straight guaranteed contract")

    # Min-guaranteed 2x pattern: (N-1) years at L, one at H=2L
    # Total = L*((N+1) + 2*p*ninc*N)   if incentives_per_year else L*((N+1) + 2*p*ninc)
    if params.apply_2x:
        denom = (N + 1) + (2 * p * ninc * N if params.incentives_per_year else 2 * p * ninc)
    else:
        # No 2x rule → still use same pattern for simplicity (H=2L); user can
        # override by not using fallback.
        denom = (N + 1) + (2 * p * ninc * N if params.incentives_per_year else 2 * p * ninc)

    L = _round_down(int(T / denom), r)
    L = max(L, params.min_annual_value or r)
    H = _round_down(2 * L, r)

    # Per-year incentive cap (dollars per incentive slot)
    inc_per = _round_down(int(H * p), r)
    if inc_per == 0 and ninc > 0:
        warnings.append(f"Incentive cap rounds to 0 at H=${H:,} and cap={p}; incentives will be 0")

    # Incentive total
    if params.incentives_per_year:
        inc_total = N * ninc * inc_per
    else:
        inc_total = ninc * inc_per

    # Base salary budget to hit target
    base_budget = T - inc_total
    if base_budget < 0:
        warnings.append("Incentives alone exceed target; dropping incentives to 0 and recomputing.")
        inc_per = 0
        inc_total = 0
        base_budget = T

    # If even N*L exceeds base_budget, L is too high — round it down until it fits.
    while N * L > base_budget and L > r:
        L -= r
        H = _round_down(2 * L, r)
        # Recompute incentive cap since H changed
        inc_per = _round_down(int(H * p), r)
        inc_total = (N * ninc * inc_per) if params.incentives_per_year else (ninc * inc_per)
        base_budget = T - inc_total

    # Allocate base_budget across N years, all in [L, H], minimizing guaranteed.
    # Strategy: start all at L, then fill year 0 toward H first (front-load),
    # then year 1, etc., until budget is spent.
    salaries = [L] * N
    remaining = base_budget - N * L  # extra to distribute
    for i in range(N):
        if remaining <= 0:
            break
        can_add = H - salaries[i]
        add = min(can_add, remaining)
        add = (add // r) * r  # floor to rounding (can't exceed H by construction)
        salaries[i] += add
        remaining -= add

    # Residual < r may remain due to rounding. Absorb into year 0 if within 1 rounding unit.
    if 0 < remaining <= r:
        # Year 0 may already be at H; only add if it fits.
        if salaries[0] + remaining <= H + r:  # tolerate one rounding unit of slack
            salaries[0] += remaining
            remaining = 0

    if remaining > 0:
        warnings.append(f"fallback short of target by ${remaining:,} after allocation")

    # Sort descending for front-loading
    salaries.sort(reverse=True)

    total_guaranteed = sum(salaries)
    total_max = total_guaranteed + inc_total

    years_data = [
        FallbackYear(year=i + 1, base_salary=salaries[i], max_incentives=[inc_per] * ninc)
        for i in range(N)
    ]
    return FallbackContract(
        years=years_data,
        L=L, H=max(salaries),
        total_guaranteed=total_guaranteed,
        total_max_incentives=inc_total,
        total_max_value=total_max,
        warnings=warnings,
    )


def format_fallback(fb: FallbackContract, params: cb.ContractParams) -> str:
    lines: List[str] = []
    lines.append("=" * 80)
    lines.append("CONTRACT STRUCTURE (fallback: min-guaranteed 2x pattern)")
    lines.append("=" * 80)
    lines.append("")
    lines.append(f"{'Year':<6}{'Base Salary':<18}{'Guaranteed':<18}{'Max Incentives':<20}")
    lines.append("-" * 80)
    for y in fb.years:
        inc_str = _fmt_dollars(sum(y.max_incentives))
        lines.append(f"{y.year:<6}{_fmt_dollars(y.base_salary):<18}{_fmt_dollars(y.base_salary):<18}{inc_str:<20}")
    lines.append("-" * 80)
    lines.append("")
    ratio = (fb.H / fb.L) if fb.L > 0 else 0.0
    lines.append("SUMMARY:")
    lines.append(f"  Lowest Annual Value (L):  {_fmt_dollars(fb.L)}")
    lines.append(f"  Highest Annual Value (H): {_fmt_dollars(fb.H)}")
    lines.append(f"  H/L Ratio: {ratio:.2f}x" + ("  [OK] 2x" if ratio <= 2.0 else "  [X] 2x"))
    lines.append("")
    lines.append(f"  Total Guaranteed Money: {_fmt_dollars(fb.total_guaranteed)}")
    lines.append(f"  Total Max Incentives:   {_fmt_dollars(fb.total_max_incentives)}")
    lines.append(f"  Total Max Value:        {_fmt_dollars(fb.total_max_value)}")
    miss = params.total_max_value - fb.total_max_value
    lines.append(f"  Target:                 {_fmt_dollars(params.total_max_value)}   (miss: {_fmt_dollars(miss)})")
    lines.append("")
    for w in fb.warnings:
        lines.append(f"  [WARNING] {w}")
    if fb.warnings:
        lines.append("")
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=getattr(logging, args.log_level), format="%(levelname)s: %(message)s")

    cfg = load_config(args.config)
    vpc_cfg = cfg["vpc"]
    cd = cfg["contract_defaults"]

    input_path = _resolve_input(args.league, args.input)
    logger.info("Input: %s", input_path)
    rows, fieldnames = fv.read_csv_rows(input_path)

    required = ["ID", "Name", "Pos", "Age",
                args.vos_col or vpc_cfg["vos_col"],
                args.pot_col or vpc_cfg["pot_col"]]
    fv.validate_columns(fieldnames, required)

    # Load /players lookup for VPC filtering (unless caller disables).
    players_lookup: Optional[Dict[str, Dict[str, str]]] = None
    if not args.no_players_filter:
        base_url = fv.resolve_base_url(args.league, args.base_url, args.league_url_config)
        if base_url:
            try:
                players_lookup = fv.build_players_lookup(base_url)
                logger.info("Loaded %d /players rows from %s", len(players_lookup), base_url)
            except (URLError, TimeoutError, ValueError) as e:
                logger.warning("Failed to load /players (%s). VPC will include pre-arb minimums.", e)
        else:
            logger.warning("No base URL for league '%s'; VPC will include pre-arb minimums.", args.league)

    # Choose calibration rows / filter.
    calib_rows = rows
    calib_lookup = players_lookup
    mode = "default (arb + vet + multi-year)"
    if args.market_only:
        if players_lookup is None:
            print("ERROR: --market-only requires /players access. No lookup available.", file=sys.stderr)
            return 2
        calib_rows = _filter_rows_fa_only(rows, players_lookup, min_service_years=6.0)
        calib_lookup = None  # already pre-filtered; don't double-filter
        mode = "market-only (6+ service years)"
    elif args.no_players_filter:
        calib_lookup = None
        mode = "no filter (includes pre-arb)"

    vpc, n = fv.compute_vpc_base(
        rows=calib_rows,
        salary_col=args.salary_col or vpc_cfg["salary_col"],
        calib_col=args.pot_col or vpc_cfg["pot_col"],
        vos_floor=args.vos_floor if args.vos_floor is not None else float(vpc_cfg["vos_floor"]),
        winsor_lower=args.winsor_lower if args.winsor_lower is not None else float(vpc_cfg["winsor_lower"]),
        winsor_upper=args.winsor_upper if args.winsor_upper is not None else float(vpc_cfg["winsor_upper"]),
        players_lookup=calib_lookup,
    )
    logger.info("VPC=%.0f  (n=%d MLB rows, mode=%s)", vpc, n, mode)

    snap = locate_player(rows, args.id)

    rounding_val = int(args.rounding if args.rounding is not None else cd["rounding"])

    val = run_valuation(
        snap=snap,
        vpc=vpc,
        vpc_sample=n,
        years=args.years,
        contract_type=args.contract_type,
        arb_years=args.arb_years,
        pre_arb_years=args.pre_arb_years,
        cfg=cfg,
        rounding=rounding_val,
    )

    print(format_valuation(val, args.contract_type, args.years, calib_mode=mode))

    if not args.structure:
        return 0

    # Hand off to contract_builder. If --target-aav set, build around that
    # instead of the VPC/VOS-derived fair value.
    if args.target_aav is not None:
        target_total = int(args.target_aav) * int(args.years)
        print(f"[target-aav override] AAV=${args.target_aav:,} x {args.years}y "
              f"= {_fmt_dollars(target_total)} "
              f"(fair value was {_fmt_dollars(val.total_fair_value)}, "
              f"delta {_fmt_dollars(target_total - val.total_fair_value)})\n")
        build_total = target_total
    else:
        build_total = val.total_fair_value
    params = _mk_contract_params(build_total, args.years, args, cd)
    result = None
    builder_err: Optional[str] = None
    try:
        result = cb.build_contract(params)
    except Exception as e:
        builder_err = str(e)

    target = params.total_max_value
    miss = None if result is None else abs(result.total_max_value - target)
    if result is None or miss is None or miss > params.rounding:
        if builder_err:
            print(f"[contract_builder failed: {builder_err}]")
        else:
            print(f"[contract_builder missed target by ${miss:,} — using fallback structurer]")
        fb = fallback_structure(params)
        print(format_fallback(fb, params))
        return 0

    errors = cb.validate_contract(result, params)
    print(cb.format_output(result))
    for e in errors:
        print(f"[validation] {e}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
