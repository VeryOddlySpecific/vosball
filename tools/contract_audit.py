#!/usr/bin/env python3
"""
contract_audit.py — League-wide audit of current player contracts vs. fair value.

For every player on a current contract (Contract_* fields populated by
`vos_v2 --contracts`), compute the fair value of the *remaining* contract years
using the same VPC + age-curve + type-multiplier + elite-premium + risk-discount
engine as contract.py. Compare to the actual remaining dollars committed.

Classify each contract as:
  - OVERPRICED (actual > fair by more than threshold)
  - FAIR
  - UNDERPRICED / SURPLUS (fair > actual by more than threshold)

Outputs (Markdown only, by user request):
  {league}/contract_audit/contract_audit_{league}_{ts}.md

  Sections:
    1. Methodology + VPC calibration
    2. League summary table (counts, total commitment vs fair)
    3. Per-org rollup (sortable by surplus value)
    4. Top N most overpriced contracts (league-wide)
    5. Top N biggest steals / underpriced (league-wide)

Type inference:
  - Defaults to a service-time heuristic using /players (mlb_service_days,
    pro_service_years):
      * >= 6 service years         -> all remaining years = market
      * arb-eligible (3-6 years)   -> step the arb ladder, then market
      * pre-arb  (< 3 years)       -> pre_arb -> arb ladder -> market
  - --force-market: treat every remaining year as open-market FA
  - --type-override player_id=market[,player_id=extension] : manual overrides

Filters:
  - --exclude-pre-arb       drop players with <3 service years (cost-controlled
                            by rule, so they always look like steals)
  - --exclude-one-year      drop 1-year contracts from BOTH VPC calibration and
                            the audit (multi-year deals only)
  - --min-actual-salary N   drop contracts with total remaining $ < N

Usage:
  py contract_audit.py --league sahl
  py contract_audit.py --league sahl --org "Houston Astros"
  py contract_audit.py --league sahl --exclude-pre-arb --min-actual-salary 1000000
  py contract_audit.py --league sahl --force-market --over-threshold 0.20
"""

from __future__ import annotations
# --- tools/ -> repo-root bootstrap (added during tools/ move) ---
import os as _os, sys as _sys
_ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
if _ROOT not in _sys.path:
    _sys.path.insert(0, _ROOT)
# --- end bootstrap ---


import argparse
import csv
import logging
import re
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.error import URLError

import contract as ct
import farm_value_old as fv

logger = logging.getLogger("contract_audit")
SCRIPT_DIR = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = SCRIPT_DIR / "config" / "contract_config.json"
DEFAULT_LEAGUE_URL_CFG = SCRIPT_DIR / "config" / "league_url.json"

# Service-time thresholds (years). Standard MLB rules.
ARB_THRESHOLD_SERVICE_YEARS = 3.0
FA_THRESHOLD_SERVICE_YEARS = 6.0
ARB_LADDER_LEN = 3


# -----------------------------------------------------------------------------
# Remaining contract extraction
# -----------------------------------------------------------------------------

@dataclass
class RemainingContract:
    pid: str
    name: str
    org: str
    pos: str
    age: int
    vos_current: float
    vos_potential: float
    is_pitcher: bool
    # Current contract (years remaining on the active deal)
    years_remaining: int
    salaries_remaining: List[float]
    total_remaining: float
    aav_remaining: float
    service_years: float
    contract_type_inferred: str
    pre_arb_years: int
    arb_years: int
    # Queued extension (kicks in after current contract ends; 0/empty if none)
    ext_years: int = 0
    ext_salaries: Optional[List[float]] = None
    ext_total: float = 0.0
    ext_aav: float = 0.0
    ext_contract_type: str = "market"
    ext_pre_arb_years: int = 0
    ext_arb_years: int = 0


def _row_float(row: Dict[str, str], key: str, default: float = 0.0) -> float:
    return fv.to_float(row.get(key), default)


def _extract_remaining_salaries(row: Dict[str, str]) -> Tuple[List[float], int]:
    """Return (remaining salaries, count). Current year first."""
    years = int(_row_float(row, "Contract_years", 0))
    current_year = int(_row_float(row, "Contract_current_year", 0))

    salaries: List[float] = []
    if years > 0 and current_year >= 1 and current_year <= years:
        start = current_year - 1
        for i in range(start, years):
            salaries.append(_row_float(row, f"Contract_salary{i}", 0.0))
    else:
        # Fallback: take contiguous non-zero block from salary0
        for i in range(15):
            s = _row_float(row, f"Contract_salary{i}", 0.0)
            if s <= 0 and salaries:
                break
            if s > 0:
                salaries.append(s)

    while salaries and salaries[-1] <= 0:
        salaries.pop()

    return salaries, len(salaries)


def _extract_extension_salaries(row: Dict[str, str]) -> Tuple[List[float], int]:
    """Pull the queued ContractExtension_* salary stream, if any. Extensions
    haven't started yet, so every year is 'remaining'."""
    years = int(_row_float(row, "ContractExtension_years", 0))
    if years <= 0:
        return [], 0
    salaries: List[float] = []
    for i in range(years):
        salaries.append(_row_float(row, f"ContractExtension_salary{i}", 0.0))
    while salaries and salaries[-1] <= 0:
        salaries.pop()
    return salaries, len(salaries)


def _infer_type(
    service_years: float,
    years_remaining: int,
    force_market: bool,
    override: Optional[str],
) -> Tuple[str, int, int]:
    """Return (contract_type, pre_arb_years, arb_years) for run_valuation."""
    if override:
        ovr = override.strip().lower()
        if ovr == "market":
            return ("market", 0, 0)
        if ovr == "extension":
            return ("extension", 0, 0)

    if force_market:
        return ("market", 0, 0)

    if service_years < 0:
        return ("market", 0, 0)

    if service_years >= FA_THRESHOLD_SERVICE_YEARS:
        return ("market", 0, 0)

    if service_years >= ARB_THRESHOLD_SERVICE_YEARS:
        arb_left = max(0, int(round(FA_THRESHOLD_SERVICE_YEARS - service_years)))
        arb_left = min(arb_left, ARB_LADDER_LEN, years_remaining)
        return ("extension", 0, arb_left)

    pre_arb_left = max(0, int(round(ARB_THRESHOLD_SERVICE_YEARS - service_years)))
    pre_arb_left = min(pre_arb_left, years_remaining)
    arb_left = min(ARB_LADDER_LEN, years_remaining - pre_arb_left)
    return ("extension", pre_arb_left, arb_left)


def _filter_rows_multi_year(rows: List[Dict[str, str]]) -> List[Dict[str, str]]:
    """Return only rows where the original contract length is >1 year."""
    out: List[Dict[str, str]] = []
    for r in rows:
        if int(_row_float(r, "Contract_years", 0)) > 1:
            out.append(r)
    return out


def build_remaining_contracts(
    rows: List[Dict[str, str]],
    players_lookup: Optional[Dict[str, Dict[str, str]]],
    force_market: bool,
    overrides: Dict[str, str],
    org_filter: Optional[str],
    exclude_pre_arb: bool = False,
    exclude_one_year: bool = False,
    min_actual_salary: float = 0.0,
    skipped_counters: Optional[Dict[str, int]] = None,
    skipped_log: Optional[List[Dict[str, str]]] = None,
) -> List[RemainingContract]:
    def _log(reason: str, detail: str, r: Dict[str, str], pid: str = "") -> None:
        if skipped_log is None:
            return
        skipped_log.append({
            "pid": pid or (r.get("ID") or "").strip(),
            "name": (r.get("Name") or "").strip(),
            "org": (r.get("Org") or "").strip(),
            "pos": (r.get("Pos") or "").strip(),
            "reason": reason,
            "detail": detail,
        })

    out: List[RemainingContract] = []
    for r in rows:
        pid = (r.get("ID") or "").strip()
        if not pid:
            _log("missing_id", "row has no ID", r)
            continue
        org = (r.get("Org") or "").strip()
        if not org:
            _log("missing_org", "row has no Org (likely free agent / unsigned)", r, pid)
            continue
        if org_filter and org != org_filter:
            continue
        contract_years = int(_row_float(r, "Contract_years", 0))
        ext_years_raw = int(_row_float(r, "ContractExtension_years", 0))
        if contract_years <= 0:
            _log("no_contract", "Contract_years <= 0 (no active contract)", r, pid)
            continue
        # Filter: a 1-year current contract still counts as "multi-year"
        # commitment if a queued extension pushes combined years > 1.
        if exclude_one_year and contract_years == 1 and ext_years_raw <= 0:
            if skipped_counters is not None:
                skipped_counters["one_year"] = skipped_counters.get("one_year", 0) + 1
            _log("one_year", "1-year contract excluded by --exclude-one-year", r, pid)
            continue
        salaries, yrs = _extract_remaining_salaries(r)
        ext_salaries, ext_yrs = _extract_extension_salaries(r)
        if yrs <= 0 or sum(salaries) <= 0:
            _log("no_remaining_salary",
                 f"Contract_years={contract_years} ext_years={ext_years_raw} "
                 f"but no positive remaining salary rows", r, pid)
            continue

        total_actual = float(sum(salaries))
        ext_total = float(sum(ext_salaries))
        # min-salary filter looks at the FULL commitment (current + extension)
        # so a small current year with a fat extension still passes.
        if min_actual_salary > 0 and (total_actual + ext_total) < min_actual_salary:
            if skipped_counters is not None:
                skipped_counters["min_salary"] = skipped_counters.get("min_salary", 0) + 1
            _log("min_salary",
                 f"total remaining ${total_actual + ext_total:,.0f} (cur+ext) "
                 f"< threshold ${min_actual_salary:,.0f}", r, pid)
            continue

        pos = (r.get("Pos") or "").strip()
        snap_age = int(_row_float(r, "Age", 0))
        vos_c = _row_float(r, "VOS_Score", 0.0)
        vos_p = _row_float(r, "VOS_Potential", vos_c)
        is_p = ct._is_pitcher_pos(pos)

        service_years = -1.0
        if players_lookup is not None:
            pmeta = players_lookup.get(pid)
            if pmeta is not None:
                service_years = fv.to_float(pmeta.get("mlb_service_years"), -1.0)

        # Drop pre-arb players if requested (skip override / force_market players).
        if exclude_pre_arb and overrides.get(pid) is None and not force_market:
            if service_years >= 0 and service_years < ARB_THRESHOLD_SERVICE_YEARS:
                if skipped_counters is not None:
                    skipped_counters["pre_arb"] = skipped_counters.get("pre_arb", 0) + 1
                _log("pre_arb",
                     f"service_years={service_years:.2f} < {ARB_THRESHOLD_SERVICE_YEARS:.0f}",
                     r, pid)
                continue

        ctype, pre_arb, arb = _infer_type(
            service_years=service_years,
            years_remaining=yrs,
            force_market=force_market,
            override=overrides.get(pid),
        )

        # Extension type inference: service time will have advanced by `yrs`
        # by the time the extension kicks in.
        ext_ctype, ext_pre_arb, ext_arb = "market", 0, 0
        if ext_yrs > 0:
            ext_service = (service_years + yrs) if service_years >= 0 else service_years
            ext_ctype, ext_pre_arb, ext_arb = _infer_type(
                service_years=ext_service,
                years_remaining=ext_yrs,
                force_market=force_market,
                override=overrides.get(pid),
            )

        out.append(RemainingContract(
            pid=pid,
            name=(r.get("Name") or pid).strip(),
            org=org,
            pos=pos,
            age=snap_age,
            vos_current=vos_c,
            vos_potential=vos_p,
            is_pitcher=is_p,
            years_remaining=yrs,
            salaries_remaining=salaries,
            total_remaining=total_actual,
            aav_remaining=total_actual / yrs,
            service_years=service_years,
            contract_type_inferred=ctype,
            pre_arb_years=pre_arb,
            arb_years=arb,
            ext_years=ext_yrs,
            ext_salaries=ext_salaries if ext_yrs > 0 else None,
            ext_total=ext_total,
            ext_aav=(ext_total / ext_yrs) if ext_yrs > 0 else 0.0,
            ext_contract_type=ext_ctype,
            ext_pre_arb_years=ext_pre_arb,
            ext_arb_years=ext_arb,
        ))
    return out


# -----------------------------------------------------------------------------
# Audit per contract
# -----------------------------------------------------------------------------

@dataclass
class AuditRow:
    contract: RemainingContract
    # Current contract
    fair_value: int
    delta_dollars: float
    delta_pct: float
    # Queued extension (zeros when no extension on file)
    fair_value_ext: int
    delta_dollars_ext: float
    delta_pct_ext: float
    # Combined view — used for sort/classification
    delta_total: float
    delta_pct_total: float
    classification: str


def audit_contract(rc: RemainingContract, vpc: float, vpc_n: int, cfg: Dict,
                   over_thr: float, under_thr: float) -> AuditRow:
    snap = ct.PlayerSnapshot(
        pid=rc.pid, name=rc.name, age=rc.age, pos=rc.pos,
        vos_current=rc.vos_current, vos_potential=rc.vos_potential,
        is_pitcher=rc.is_pitcher,
    )
    rounding = int(cfg["contract_defaults"].get("rounding", 100000))

    # Current contract valuation
    val = ct.run_valuation(
        snap=snap, vpc=vpc, vpc_sample=vpc_n,
        years=rc.years_remaining,
        contract_type=rc.contract_type_inferred,
        arb_years=rc.arb_years,
        pre_arb_years=rc.pre_arb_years,
        cfg=cfg,
        rounding=rounding,
    )
    actual = rc.total_remaining
    fair = float(val.total_fair_value)
    delta = actual - fair
    pct = (delta / fair) if fair > 0 else float("inf")

    # Extension valuation (if any). Age the snapshot forward so VOS projection
    # for the extension period starts from when those years actually begin.
    fair_ext = 0.0
    delta_ext = 0.0
    pct_ext = 0.0
    if rc.ext_years > 0:
        ext_snap = ct.PlayerSnapshot(
            pid=rc.pid, name=rc.name,
            age=rc.age + rc.years_remaining,
            pos=rc.pos,
            vos_current=rc.vos_current, vos_potential=rc.vos_potential,
            is_pitcher=rc.is_pitcher,
        )
        val_ext = ct.run_valuation(
            snap=ext_snap, vpc=vpc, vpc_sample=vpc_n,
            years=rc.ext_years,
            contract_type=rc.ext_contract_type,
            arb_years=rc.ext_arb_years,
            pre_arb_years=rc.ext_pre_arb_years,
            cfg=cfg,
            rounding=rounding,
        )
        fair_ext = float(val_ext.total_fair_value)
        delta_ext = rc.ext_total - fair_ext
        pct_ext = (delta_ext / fair_ext) if fair_ext > 0 else float("inf")

    # Combined view for sort/classification
    total_actual = actual + rc.ext_total
    total_fair = fair + fair_ext
    delta_total = total_actual - total_fair
    pct_total = (delta_total / total_fair) if total_fair > 0 else float("inf")

    if pct_total > over_thr:
        cls = "OVERPRICED"
    elif pct_total < -under_thr:
        cls = "UNDERPRICED"
    else:
        cls = "FAIR"

    return AuditRow(
        contract=rc,
        fair_value=int(fair),
        delta_dollars=delta, delta_pct=pct,
        fair_value_ext=int(fair_ext),
        delta_dollars_ext=delta_ext, delta_pct_ext=pct_ext,
        delta_total=delta_total, delta_pct_total=pct_total,
        classification=cls,
    )


# -----------------------------------------------------------------------------
# Reporting
# -----------------------------------------------------------------------------

def _fmt_dollars(x: float) -> str:
    if x is None:
        return ""
    sign = "-" if x < 0 else ""
    x = abs(x)
    if x >= 1_000_000:
        return f"{sign}${x/1_000_000:.2f}M"
    if x >= 1_000:
        return f"{sign}${x/1_000:.0f}K"
    return f"{sign}${x:,.0f}"


def _fmt_pct(p: float) -> str:
    if p == float("inf"):
        return "n/a"
    return f"{p*100:+.0f}%"


def render_markdown(
    audit: List[AuditRow],
    vpc: float, vpc_n: int, calib_mode: str,
    league: str, top_n: int,
    over_thr: float, under_thr: float,
    org_filter: Optional[str],
    exclude_pre_arb: bool = False,
    exclude_one_year: bool = False,
    min_actual_salary: float = 0.0,
    skipped: Optional[Dict[str, int]] = None,
) -> str:
    L: List[str] = []
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    title_scope = org_filter or f"{league.upper()} (league-wide)"
    L.append(f"# Contract Audit - {title_scope}")
    L.append("")
    L.append(f"_Generated: {ts}_")
    L.append("")
    L.append("## Methodology")
    L.append("")
    L.append(f"- VPC calibration: **${vpc:,.0f}** per VOS point (n={vpc_n} MLB rows, mode: {calib_mode})")
    L.append("- Fair value = projected VOS x VPC x type multiplier x elite premium - risk discount")
    L.append(f"- Classification bands: >+{over_thr*100:.0f}% = OVERPRICED, within +-{over_thr*100:.0f}/{under_thr*100:.0f}% = FAIR, <-{under_thr*100:.0f}% = UNDERPRICED")
    L.append("- Type inference: service-time heuristic from /players (overridable per-player)")
    filters: List[str] = []
    if exclude_pre_arb:
        n_pa = (skipped or {}).get("pre_arb", 0)
        filters.append(f"excluded pre-arb players (<{ARB_THRESHOLD_SERVICE_YEARS:.0f} service yrs) - {n_pa} dropped")
    if exclude_one_year:
        n_oy = (skipped or {}).get("one_year", 0)
        filters.append(f"excluded 1-year contracts (VPC + audit on multi-year only) - {n_oy} dropped")
    if min_actual_salary > 0:
        n_ms = (skipped or {}).get("min_salary", 0)
        filters.append(f"excluded contracts under {_fmt_dollars(min_actual_salary)} total - {n_ms} dropped")
    if filters:
        L.append(f"- Filters: {'; '.join(filters)}")
    L.append("")

    n = len(audit)
    n_ext = sum(1 for a in audit if a.contract.ext_years > 0)
    n_over = sum(1 for a in audit if a.classification == "OVERPRICED")
    n_fair = sum(1 for a in audit if a.classification == "FAIR")
    n_under = sum(1 for a in audit if a.classification == "UNDERPRICED")
    # League/org totals are on the full commitment (current + extension).
    total_actual = sum(a.contract.total_remaining + a.contract.ext_total for a in audit)
    total_fair = sum(a.fair_value + a.fair_value_ext for a in audit)
    total_delta = total_actual - total_fair
    L.append("## League summary")
    L.append("")
    L.append(f"_{n_ext} of {n} audited players have a queued extension. Total $ figures combine current contract + extension._")
    L.append("")
    L.append("| Contracts | Overpriced | Fair | Underpriced | Total committed | Total fair | Net delta |")
    L.append("|---|---|---|---|---|---|---|")
    L.append(
        f"| {n} | {n_over} ({n_over/max(n,1)*100:.0f}%) | {n_fair} ({n_fair/max(n,1)*100:.0f}%) | "
        f"{n_under} ({n_under/max(n,1)*100:.0f}%) | {_fmt_dollars(total_actual)} | "
        f"{_fmt_dollars(total_fair)} | {_fmt_dollars(total_delta)} |"
    )
    L.append("")

    by_org: Dict[str, List[AuditRow]] = {}
    for a in audit:
        by_org.setdefault(a.contract.org, []).append(a)

    L.append("## Per-org rollup")
    L.append("")
    L.append("Sorted by surplus value (most underpaid roster first). $ totals include extensions.")
    L.append("")
    L.append("| Org | # K | Actual $ | Fair $ | Net delta | Over | Fair | Under |")
    L.append("|---|---|---|---|---|---|---|---|")
    org_summaries = []
    for org, rows in by_org.items():
        actual = sum(r.contract.total_remaining + r.contract.ext_total for r in rows)
        fair = sum(r.fair_value + r.fair_value_ext for r in rows)
        delta = actual - fair
        no = sum(1 for r in rows if r.classification == "OVERPRICED")
        nf = sum(1 for r in rows if r.classification == "FAIR")
        nu = sum(1 for r in rows if r.classification == "UNDERPRICED")
        org_summaries.append((org, len(rows), actual, fair, delta, no, nf, nu))
    org_summaries.sort(key=lambda x: x[4])
    for org, c, actual, fair, delta, no, nf, nu in org_summaries:
        L.append(
            f"| {org} | {c} | {_fmt_dollars(actual)} | {_fmt_dollars(fair)} | "
            f"{_fmt_dollars(delta)} | {no} | {nf} | {nu} |"
        )
    L.append("")

    def _ext_cell(value: str, has_ext: bool) -> str:
        return value if has_ext else "-"

    overs = sorted([a for a in audit if a.classification == "OVERPRICED"],
                   key=lambda a: a.delta_total, reverse=True)[:top_n]
    L.append(f"## Top {top_n} most overpriced contracts")
    L.append("")
    L.append("Sorted by total $ overpaid (current + extension). Ext columns are blank when no extension is queued.")
    L.append("")
    L.append("| Player | Pos | Age | Org | Yrs | VOS C/P | Actual | Fair | d $ | d % | Yrs_Ext | Actual_Ext | Fair_Ext | d $_Ext | d %_Ext | Type |")
    L.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for a in overs:
        c = a.contract
        has_ext = c.ext_years > 0
        L.append(
            f"| {c.name} | {c.pos} | {c.age} | {c.org} | {c.years_remaining} | "
            f"{c.vos_current:.0f}/{c.vos_potential:.0f} | "
            f"{_fmt_dollars(c.total_remaining)} | {_fmt_dollars(a.fair_value)} | "
            f"{_fmt_dollars(a.delta_dollars)} | {_fmt_pct(a.delta_pct)} | "
            f"{_ext_cell(str(c.ext_years), has_ext)} | "
            f"{_ext_cell(_fmt_dollars(c.ext_total), has_ext)} | "
            f"{_ext_cell(_fmt_dollars(a.fair_value_ext), has_ext)} | "
            f"{_ext_cell(_fmt_dollars(a.delta_dollars_ext), has_ext)} | "
            f"{_ext_cell(_fmt_pct(a.delta_pct_ext), has_ext)} | "
            f"{c.contract_type_inferred}{('/' + c.ext_contract_type) if has_ext else ''} |"
        )
    L.append("")

    unders = sorted([a for a in audit if a.classification == "UNDERPRICED"],
                    key=lambda a: a.delta_total)[:top_n]
    L.append(f"## Top {top_n} biggest steals (most underpriced)")
    L.append("")
    L.append("Sorted by total surplus value (current + extension). Surplus shown as positive $.")
    L.append("")
    L.append("| Player | Pos | Age | Org | Yrs | VOS C/P | Actual | Fair | Surplus | d % | Yrs_Ext | Actual_Ext | Fair_Ext | Surplus_Ext | d %_Ext | Type |")
    L.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for a in unders:
        c = a.contract
        has_ext = c.ext_years > 0
        L.append(
            f"| {c.name} | {c.pos} | {c.age} | {c.org} | {c.years_remaining} | "
            f"{c.vos_current:.0f}/{c.vos_potential:.0f} | "
            f"{_fmt_dollars(c.total_remaining)} | {_fmt_dollars(a.fair_value)} | "
            f"{_fmt_dollars(-a.delta_dollars)} | {_fmt_pct(a.delta_pct)} | "
            f"{_ext_cell(str(c.ext_years), has_ext)} | "
            f"{_ext_cell(_fmt_dollars(c.ext_total), has_ext)} | "
            f"{_ext_cell(_fmt_dollars(a.fair_value_ext), has_ext)} | "
            f"{_ext_cell(_fmt_dollars(-a.delta_dollars_ext), has_ext)} | "
            f"{_ext_cell(_fmt_pct(a.delta_pct_ext), has_ext)} | "
            f"{c.contract_type_inferred}{('/' + c.ext_contract_type) if has_ext else ''} |"
        )
    L.append("")

    return "\n".join(L)


_REASON_DESCRIPTIONS = {
    "missing_id":          "Row had no player ID (data hygiene; not auditable).",
    "missing_org":          "No Org set — typically free agents or unsigned players.",
    "no_contract":          "Contract_years <= 0 — player is not under contract.",
    "no_remaining_salary":  "Contract_years > 0 but no positive remaining salary rows (data quirk).",
    "one_year":             "1-year contract dropped by --exclude-one-year.",
    "min_salary":           "Total remaining $ below --min-actual-salary threshold.",
    "pre_arb":              "Pre-arb (service years < 3) dropped by --exclude-pre-arb.",
    "valuation_error":      "Exception raised by run_valuation — see detail.",
}


def _write_skipped_log(path: Path, entries: List[Dict[str, str]], args: argparse.Namespace) -> None:
    """Plain-text log of every contract that was filtered out, grouped by reason."""
    by_reason: Dict[str, List[Dict[str, str]]] = {}
    for e in entries:
        by_reason.setdefault(e["reason"], []).append(e)

    L: List[str] = []
    L.append(f"Contract audit — filtered-out contracts log")
    L.append(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    L.append(f"League: {args.league}  Org filter: {args.org or '(none)'}")
    flags = []
    if args.exclude_pre_arb:   flags.append("--exclude-pre-arb")
    if args.exclude_one_year:  flags.append("--exclude-one-year")
    if args.min_actual_salary: flags.append(f"--min-actual-salary {args.min_actual_salary:.0f}")
    if args.force_market:      flags.append("--force-market")
    L.append(f"Filter flags: {' '.join(flags) if flags else '(none)'}")
    L.append(f"Total filtered: {len(entries)}")
    L.append("")

    for reason in sorted(by_reason.keys()):
        rows = by_reason[reason]
        desc = _REASON_DESCRIPTIONS.get(reason, "")
        L.append(f"=== {reason} ({len(rows)})  {desc}")
        rows.sort(key=lambda e: (e.get("org", ""), e.get("name", "")))
        for e in rows:
            ident = f"{e['name'] or '(no name)'} [{e['pid'] or '-'}]"
            org = e["org"] or "-"
            pos = e["pos"] or "-"
            L.append(f"  - {ident}  org={org}  pos={pos}  | {e['detail']}")
        L.append("")

    path.write_text("\n".join(L), encoding="utf-8")


def write_appendix_csv(audit: List[AuditRow], out_path: Path) -> None:
    """Dump every audited contract to CSV. Current contract and queued
    extension are in separate column groups so VPC/audit math stays untouched
    while extension info is recorded alongside. Values are raw numbers
    (no $/% formatting) for spreadsheet use. Sorted by combined delta $."""
    headers = [
        "Player", "Pos", "Age", "Org",
        "Yrs", "VOS C", "VOS P",
        "Actual", "Fair", "d $", "d %", "Type",
        "Yrs_Ext", "Actual_Ext", "Fair_Ext", "d $_Ext", "d %_Ext", "Type_Ext",
        "Total_Actual", "Total_Fair", "Total_d $", "Total_d %",
        "Classification",
    ]

    def _pct(v: float) -> str:
        return "" if v == float("inf") else f"{v:.4f}"

    rows_sorted = sorted(audit, key=lambda a: a.delta_total, reverse=True)
    with out_path.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(headers)
        for a in rows_sorted:
            c = a.contract
            has_ext = c.ext_years > 0
            w.writerow([
                c.name, c.pos, c.age, c.org,
                c.years_remaining, f"{c.vos_current:.1f}", f"{c.vos_potential:.1f}",
                int(round(c.total_remaining)), int(a.fair_value),
                int(round(a.delta_dollars)), _pct(a.delta_pct),
                c.contract_type_inferred,
                c.ext_years if has_ext else "",
                int(round(c.ext_total)) if has_ext else "",
                int(a.fair_value_ext) if has_ext else "",
                int(round(a.delta_dollars_ext)) if has_ext else "",
                _pct(a.delta_pct_ext) if has_ext else "",
                c.ext_contract_type if has_ext else "",
                int(round(c.total_remaining + c.ext_total)),
                int(a.fair_value + a.fair_value_ext),
                int(round(a.delta_total)), _pct(a.delta_pct_total),
                a.classification,
            ])


# -----------------------------------------------------------------------------
# CLI
# -----------------------------------------------------------------------------

def parse_overrides(s: Optional[str]) -> Dict[str, str]:
    if not s:
        return {}
    out: Dict[str, str] = {}
    for chunk in s.split(","):
        chunk = chunk.strip()
        if not chunk or "=" not in chunk:
            continue
        pid, kind = chunk.split("=", 1)
        out[pid.strip()] = kind.strip().lower()
    return out


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Audit current contracts vs. VPC-based fair value (over/fair/underpriced)."
    )
    p.add_argument("--league", required=True, help="League slug (e.g. sahl).")
    p.add_argument("--org", default=None,
                   help="Limit to one org (matched against Org column). Default: league-wide.")
    p.add_argument("--input", type=Path, default=None,
                   help="Evaluation CSV override (must include Contract_* columns).")
    p.add_argument("--output-dir", type=Path, default=None,
                   help="Output dir (default: {league}/contract_audit/).")
    p.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    p.add_argument("--league-url-config", type=Path, default=DEFAULT_LEAGUE_URL_CFG)
    p.add_argument("--base-url", type=str, default=None,
                   help="Override /players base URL.")

    p.add_argument("--force-market", action="store_true",
                   help="Treat every remaining year of every contract as open-market FA.")
    p.add_argument("--type-override", type=str, default=None,
                   help="Comma-separated PID=type pairs, e.g. '12345=market,67890=extension'.")
    p.add_argument("--exclude-pre-arb", action="store_true",
                   help="Drop players with <3 MLB service years (cost-controlled by rule).")
    p.add_argument("--exclude-one-year", action="store_true",
                   help="Drop 1-year contracts from BOTH VPC calibration and the audit "
                        "(filters on original Contract_years == 1; multi-year deals only).")
    p.add_argument("--min-actual-salary", type=float, default=0.0,
                   help="Drop contracts with total remaining $ below this (e.g. 1000000).")

    p.add_argument("--over-threshold", type=float, default=0.15,
                   help="%% delta above which a contract is OVERPRICED. Default 0.15.")
    p.add_argument("--under-threshold", type=float, default=0.15,
                   help="%% delta below which a contract is UNDERPRICED. Default 0.15.")
    p.add_argument("--top-n", type=int, default=25,
                   help="How many to show in over/under leaderboards. Default 25.")

    p.add_argument("--market-only", action="store_true",
                   help="VPC calibration: only 6+ service-year players (true FA market).")
    p.add_argument("--no-players-filter", action="store_true",
                   help="Skip /players filter entirely (inflates VPC; not recommended).")

    p.add_argument("--log-level", default="INFO",
                   choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    return p.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=getattr(logging, args.log_level),
                        format="%(levelname)s: %(message)s")

    cfg = ct.load_config(args.config)
    vpc_cfg = cfg["vpc"]

    input_path = ct._resolve_input(args.league, args.input)
    logger.info("Input: %s", input_path)
    rows, fieldnames = fv.read_csv_rows(input_path)

    required = ["ID", "Name", "Pos", "Age", "Org",
                vpc_cfg["vos_col"], vpc_cfg["pot_col"],
                "Contract_years", "Contract_current_year", "Contract_salary0"]
    fv.validate_columns(fieldnames, required)

    players_lookup: Optional[Dict[str, Dict[str, str]]] = None
    if not args.no_players_filter:
        base_url = fv.resolve_base_url(args.league, args.base_url, args.league_url_config)
        if base_url:
            try:
                players_lookup = fv.build_players_lookup(base_url)
                logger.info("Loaded %d /players rows", len(players_lookup))
            except (URLError, TimeoutError, ValueError) as e:
                logger.warning("Failed to load /players (%s).", e)
        else:
            logger.warning("No base URL for league '%s'; service-time heuristic will fall back to market.", args.league)

    calib_rows = rows
    calib_lookup = players_lookup
    mode = "default (arb + vet + multi-year)"
    if args.market_only:
        if players_lookup is None:
            print("ERROR: --market-only requires /players access.", file=sys.stderr)
            return 2
        calib_rows = ct._filter_rows_fa_only(rows, players_lookup, min_service_years=6.0)
        calib_lookup = None
        mode = "market-only (6+ service years)"
    elif args.no_players_filter:
        calib_lookup = None
        mode = "no filter (includes pre-arb)"

    if args.exclude_one_year:
        before = len(calib_rows)
        calib_rows = _filter_rows_multi_year(calib_rows)
        logger.info("VPC calibration: dropped %d one-year contracts (%d -> %d)",
                    before - len(calib_rows), before, len(calib_rows))
        mode = f"{mode} + multi-year only"

    vpc, vpc_n = fv.compute_vpc_base(
        rows=calib_rows,
        salary_col=vpc_cfg["salary_col"],
        calib_col=vpc_cfg["pot_col"],
        vos_floor=float(vpc_cfg["vos_floor"]),
        winsor_lower=float(vpc_cfg["winsor_lower"]),
        winsor_upper=float(vpc_cfg["winsor_upper"]),
        players_lookup=calib_lookup,
    )
    logger.info("VPC=%.0f (n=%d, mode=%s)", vpc, vpc_n, mode)

    overrides = parse_overrides(args.type_override)
    skipped: Dict[str, int] = {}
    skipped_log: List[Dict[str, str]] = []
    contracts = build_remaining_contracts(
        rows=rows,
        players_lookup=players_lookup,
        force_market=args.force_market,
        overrides=overrides,
        org_filter=args.org,
        exclude_pre_arb=args.exclude_pre_arb,
        exclude_one_year=args.exclude_one_year,
        min_actual_salary=args.min_actual_salary,
        skipped_counters=skipped,
        skipped_log=skipped_log,
    )
    if skipped:
        logger.info(
            "Filtered out: %s",
            ", ".join(f"{v} {k.replace('_', '-')}" for k, v in skipped.items()),
        )
    logger.info("Auditing %d active contracts", len(contracts))
    if not contracts:
        print("No active contracts found. Did you run vos_v2 with --contracts?", file=sys.stderr)
        return 1

    audit: List[AuditRow] = []
    for rc in contracts:
        try:
            audit.append(audit_contract(rc, vpc, vpc_n, cfg,
                                        args.over_threshold, args.under_threshold))
        except Exception as e:
            logger.warning("Skipping %s (%s): %s", rc.name, rc.pid, e)
            skipped_log.append({
                "pid": rc.pid,
                "name": rc.name,
                "org": rc.org,
                "pos": rc.pos,
                "reason": "valuation_error",
                "detail": f"audit_contract raised: {e}",
            })

    md = render_markdown(
        audit, vpc, vpc_n, mode, args.league, args.top_n,
        args.over_threshold, args.under_threshold, args.org,
        exclude_pre_arb=args.exclude_pre_arb,
        exclude_one_year=args.exclude_one_year,
        min_actual_salary=args.min_actual_salary,
        skipped=skipped,
    )

    out_dir = args.output_dir or (SCRIPT_DIR / args.league / "contract_audit")
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    suffix = ""
    if args.org:
        suffix = "_" + re.sub(r"[^a-z0-9]+", "_", args.org.lower()).strip("_")
    out_path = out_dir / f"contract_audit_{args.league}{suffix}_{ts}.md"
    out_path.write_text(md, encoding="utf-8")
    print(f"Wrote {out_path}")

    csv_path = out_dir / f"contract_audit_{args.league}{suffix}_{ts}_appendix.csv"
    write_appendix_csv(audit, csv_path)
    print(f"Wrote {csv_path}")

    if skipped_log:
        log_path = out_dir / f"contract_audit_{args.league}{suffix}_{ts}_skipped.log"
        _write_skipped_log(log_path, skipped_log, args)
        print(f"Wrote {log_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
