#!/usr/bin/env python3
"""
Farm system valuation from VOS evaluation summaries (stdlib-only version).
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
import csv
import json
import logging
import math
from datetime import datetime
from pathlib import Path
from io import StringIO
from typing import Dict, Iterable, List, Optional, Tuple
from urllib.error import URLError
from urllib.request import urlopen

logger = logging.getLogger(__name__)
SCRIPT_DIR = Path(__file__).resolve().parent.parent

LEVEL_MULT = {"AAA": 0.955, "AA": 0.92, "A+": 0.89, "A": 0.87, "R": 0.8375, "Rookie": 0.8375}
PROX_MULT = {"AAA": 1.00, "AA": 0.93, "A+": 0.86, "A": 0.80, "R": 0.72, "Rookie": 0.72}
# VOS-gap m_risk penalty is scaled by level: full weight at AAA, gentler deeper in the minors.
RISK_GAP_LEVEL_MULT = {"AAA": 1.0, "AA": 0.78, "A+": 0.62, "A": 0.50, "R": 0.42, "Rookie": 0.42}
BASELINE_AGE = {"AAA": 25.5, "AA": 24.0, "A+": 22.5, "A": 21.5, "R": 19.5, "Rookie": 19.5}
# League scarcity: fixed buckets so equal-share baseline is stable
POSITION_BUCKETS = ("SP", "RP", "C", "SS", "CF", "LF", "RF", "1B", "2B", "3B", "DH", "OTHER")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Estimate farm value using VOS point-cost calibration.")
    parser.add_argument("--input", type=Path, default=None, help="Path to evaluation_summary CSV.")
    parser.add_argument("--league", type=str, default=None, help="League slug (auto-picks latest summary file).")
    parser.add_argument("--output-org", type=Path, default=None, help="Output CSV for org farm values.")
    parser.add_argument("--output-players", type=Path, default=None, help="Optional output CSV for player values.")
    parser.add_argument(
        "--base-url",
        type=str,
        default=None,
        help="Override league API base URL (e.g. https://host/league/api).",
    )
    parser.add_argument(
        "--league-url-config",
        type=Path,
        default=SCRIPT_DIR / "config" / "league_url.json",
        help="JSON file with league->base_url mappings.",
    )
    parser.add_argument("--salary-col", type=str, default="Contract_salary0", help="Salary column for VPC.")
    parser.add_argument("--vos-col", type=str, default="VOS_Score", help="VOS score column.")
    parser.add_argument("--pot-col", type=str, default="VOS_Potential", help="VOS potential column.")
    parser.add_argument("--non40-only", action="store_true", help="Org totals use only non-40-man players.")
    parser.add_argument("--vos-floor", type=float, default=25.0, help="Minimum MLB VOS for calibration.")
    parser.add_argument("--winsor-lower", type=float, default=0.025, help="Lower winsorization quantile.")
    parser.add_argument("--winsor-upper", type=float, default=0.975, help="Upper winsorization quantile.")
    parser.add_argument(
        "--prospect-max-mlb-days",
        type=float,
        default=90.0,
        help="Use gentler m_risk when /players mlb_service_days <= this (prospect-ish).",
    )
    parser.add_argument(
        "--prospect-max-pro-years",
        type=float,
        default=6.0,
        help="Use gentler m_risk when /players pro_service_years < this.",
    )
    parser.add_argument(
        "--risk-gap-per-point-vet",
        type=float,
        default=0.0065,
        help="Vet/non-prospect: m_risk -= this * level_mult * max(0, potential-current).",
    )
    parser.add_argument(
        "--risk-gap-min-vet",
        type=float,
        default=0.62,
        help="Floor for vet/non-prospect m_risk.",
    )
    parser.add_argument(
        "--risk-gap-per-point-prospect",
        type=float,
        default=0.0032,
        help="Prospect: penalty per VOS point of gap (scaled by level; see RISK_GAP_LEVEL_MULT).",
    )
    parser.add_argument(
        "--risk-gap-min-prospect",
        type=float,
        default=0.78,
        help="Floor for prospect m_risk (gentler than vet).",
    )
    parser.add_argument(
        "--level-prox-toward-one",
        type=float,
        default=0.0,
        help="Blend m_level and m_prox from built-in tables toward 1.0: m_eff=1+t*(m_table-1). 0=full table discount, 1=flat (no level/prox effect).",
    )
    parser.add_argument(
        "--org-include-non-prospects",
        action="store_true",
        help="Include non-prospect farm players (per service thresholds) in org totals. Default: prospects only.",
    )
    parser.add_argument(
        "--age-plateau-half-width",
        type=float,
        default=2.0,
        help="Years around typical age for level where m_age stays at 1.0 (each side).",
    )
    parser.add_argument(
        "--age-young-bonus-per-year",
        type=float,
        default=0.04,
        help="m_age bonus per year younger beyond the young threshold (capped by --age-m-max).",
    )
    parser.add_argument(
        "--age-old-penalty-per-year",
        type=float,
        default=0.06,
        help="m_age penalty per year older beyond the old threshold (floored by --age-m-min).",
    )
    parser.add_argument(
        "--age-m-min",
        type=float,
        default=0.70,
        help="Minimum m_age multiplier from age-for-level curve.",
    )
    parser.add_argument(
        "--age-m-max",
        type=float,
        default=1.15,
        help="Maximum m_age multiplier from age-for-level curve.",
    )
    parser.add_argument(
        "--disable-position-adjust",
        action="store_true",
        help="Skip RP/premium role multipliers (and league scarcity if --league-scarcity is on).",
    )
    parser.add_argument(
        "--league-scarcity",
        action="store_true",
        help="Apply league-wide position scarcity multiplier (off by default). Implies computing farm-pool shares.",
    )
    parser.add_argument(
        "--rp-debuff",
        type=float,
        default=0.93,
        help="Multiplier for projected RP/CL (and similar relief roles).",
    )
    parser.add_argument(
        "--premium-pos-boost",
        type=float,
        default=1.04,
        help="Multiplier for projected C, SS, CF.",
    )
    parser.add_argument(
        "--scarcity-strength",
        type=float,
        default=0.05,
        help="With --league-scarcity: m *= 1 + strength * ((equal_share - share_k) / equal_share), clamped.",
    )
    parser.add_argument(
        "--scarcity-min",
        type=float,
        default=0.94,
        help="With --league-scarcity: floor for scarcity multiplier.",
    )
    parser.add_argument(
        "--scarcity-max",
        type=float,
        default=1.06,
        help="With --league-scarcity: ceiling for scarcity multiplier.",
    )
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    return parser.parse_args()


def clamp(value: float, lower: float, upper: float) -> float:
    return max(lower, min(upper, value))


def blend_multiplier_toward_one(table_m: float, toward_one: float) -> float:
    """Linear blend from table_m toward 1.0. toward_one clamped to [0, 1]."""
    t = clamp(toward_one, 0.0, 1.0)
    return 1.0 + t * (table_m - 1.0)


def age_for_level_multiplier(
    age: float,
    baseline_age: float,
    plateau_half_width: float,
    young_bonus_per_year: float,
    old_penalty_per_year: float,
    m_min: float,
    m_max: float,
) -> Tuple[float, float]:
    """
    Age-for-level multiplier with a neutral plateau near typical age.
    Returns (m_age, age_dev) where age_dev = age - baseline_age.
    """
    dev = age - baseline_age
    thr = plateau_half_width
    if dev <= -thr:
        # Younger than typical: bonus increases as player gets younger
        raw = 1.0 + young_bonus_per_year * (-thr - dev)
        return clamp(raw, m_min, m_max), dev
    if dev >= thr:
        raw = 1.0 - old_penalty_per_year * (dev - thr)
        return clamp(raw, m_min, m_max), dev
    return 1.0, dev


def projected_role_field(row: Dict[str, str]) -> str:
    """Prefer Projected_Position from evaluation summary; fallback to Pos."""
    p = (row.get("Projected_Position") or "").strip()
    if p:
        return p
    return (row.get("Pos") or "").strip()


def canonical_position_bucket(row: Dict[str, str]) -> str:
    """
    Map projected role to a fixed bucket for role multipliers and league scarcity counts.
    """
    raw = projected_role_field(row).upper().strip()
    if not raw:
        return "OTHER"
    # Relief / closer
    if raw in ("RP", "CL", "MR", "SU", "LR"):
        return "RP"
    # Starters / generic P treated as SP unless explicitly relief
    if raw in ("SP", "P"):
        return "SP"
    hitters = {"C", "SS", "CF", "LF", "RF", "1B", "2B", "3B", "DH"}
    if raw in hitters:
        return raw
    return "OTHER"


def role_static_multiplier(
    bucket: str,
    rp_debuff: float,
    premium_boost: float,
) -> float:
    """RP debuff; premium positions (C, SS, CF) boost; SP and others neutral."""
    if bucket == "RP":
        return rp_debuff
    if bucket in ("C", "SS", "CF"):
        return premium_boost
    return 1.0


def compute_league_position_shares(all_rows: List[Dict[str, str]]) -> Dict[str, float]:
    """Share of each position bucket among farm-eligible rows (non-ML, has Org)."""
    counts: Dict[str, int] = {b: 0 for b in POSITION_BUCKETS}
    total = 0
    for r in all_rows:
        league_level = (r.get("League_Level") or "").strip()
        org = (r.get("Org") or "").strip()
        if league_level == "ML" or not org:
            continue
        b = canonical_position_bucket(r)
        counts[b] += 1
        total += 1
    if total <= 0:
        return {b: 1.0 / len(POSITION_BUCKETS) for b in POSITION_BUCKETS}
    return {b: counts[b] / total for b in POSITION_BUCKETS}


def scarcity_multiplier(
    bucket: str,
    shares: Dict[str, float],
    strength: float,
    lo: float,
    hi: float,
) -> float:
    """
    Rare buckets (share below equal split) get a boost; overloaded buckets get a debuff.
    """
    if strength <= 0:
        return 1.0
    k = len(POSITION_BUCKETS)
    equal_share = 1.0 / k
    share_k = shares.get(bucket, equal_share)
    # Relative deviation from equal distribution
    raw = 1.0 + strength * ((equal_share - share_k) / max(equal_share, 1e-9))
    return clamp(raw, lo, hi)


def to_float(value: object, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        text = str(value).strip()
        if text == "":
            return default
        return float(text)
    except (TypeError, ValueError):
        return default


def percentile(values: List[float], q: float) -> float:
    if not values:
        return 0.0
    if q <= 0:
        return min(values)
    if q >= 1:
        return max(values)
    s = sorted(values)
    pos = (len(s) - 1) * q
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return s[lo]
    frac = pos - lo
    return s[lo] * (1.0 - frac) + s[hi] * frac


def validate_columns(fieldnames: Optional[List[str]], required: Iterable[str]) -> None:
    if not fieldnames:
        raise ValueError("Input CSV has no header row.")
    missing = [c for c in required if c not in fieldnames]
    if missing:
        raise ValueError(
            "Missing required columns: "
            + ", ".join(missing)
            + ". Re-run vos_v2.py with --contracts if needed."
        )


def read_csv_rows(path: Path) -> Tuple[List[Dict[str, str]], List[str]]:
    with path.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        return rows, list(reader.fieldnames or [])


def load_json(path: Path) -> Dict[str, object]:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def load_league_api_base_urls(config_path: Path) -> Dict[str, str]:
    """Load league API base URLs from config/league_url.json."""
    raw = load_json(config_path)
    if not isinstance(raw, dict):
        logger.warning("league_url.json missing or invalid at %s", config_path)
        return {}
    return {str(k).strip().lower(): str(v).strip().rstrip("/") for k, v in raw.items() if k and v}


def resolve_base_url(league: Optional[str], base_url_override: Optional[str], config_path: Path) -> Optional[str]:
    if base_url_override:
        return base_url_override.rstrip("/")
    league_slug = (league or "").strip().lower()
    if not league_slug:
        return None
    mapping = load_league_api_base_urls(config_path)
    return mapping.get(league_slug)


def _fetch_csv_endpoint(url: str) -> List[Dict[str, str]]:
    with urlopen(url, timeout=30) as resp:
        payload = resp.read().decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(StringIO(payload))
    return [r for r in reader if isinstance(r, dict)]


def normalize_key(key: str) -> str:
    return key.strip().lower().replace(" ", "_")


def bool_from_value(value: object, default: bool = False) -> bool:
    text = str(value).strip().lower()
    if text in ("1", "true", "yes", "y"):
        return True
    if text in ("0", "false", "no", "n", ""):
        return False
    return default


def build_players_lookup(base_url: str) -> Dict[str, Dict[str, str]]:
    rows = _fetch_csv_endpoint(f"{base_url.rstrip('/')}/players")
    out: Dict[str, Dict[str, str]] = {}
    for row in rows:
        normalized = {normalize_key(k): (v or "") for k, v in row.items()}
        pid = (normalized.get("id") or "").strip()
        if pid:
            out[pid] = normalized
    return out


def has_multi_year_guarantee(contract_row: Dict[str, str]) -> bool:
    years = to_float(contract_row.get("Contract_years"), 0.0)
    if years > 1:
        return True
    for i in range(1, 15):
        if to_float(contract_row.get(f"Contract_salary{i}"), 0.0) > 0:
            return True
    return False


def is_market_comp_player(contract_row: Dict[str, str], player_meta: Optional[Dict[str, str]]) -> bool:
    if player_meta is None:
        # Keep row when /players metadata is missing instead of silently dropping.
        return True

    if player_meta.get("retired") and bool_from_value(player_meta.get("retired"), default=False):
        return False
    if player_meta.get("is_active") and not bool_from_value(player_meta.get("is_active"), default=True):
        return False

    service_years = to_float(player_meta.get("mlb_service_years"), 0.0)
    has_arb = bool_from_value(player_meta.get("has_received_arbitration"), default=False)
    has_multi = has_multi_year_guarantee(contract_row)
    return (service_years >= 6.0) or has_arb or has_multi


def is_prospect_for_risk(
    player_meta: Optional[Dict[str, str]],
    max_mlb_days: float,
    max_pro_years: float,
) -> Tuple[bool, float, float]:
    """
    True if player qualifies for gentler gap risk: low MLB exposure and limited pro tenure.
    Returns (is_prospect, mlb_days, pro_years). If meta missing, returns (True, -1, -1) — gentler risk.
    """
    if player_meta is None:
        return True, -1.0, -1.0
    mlb_days = to_float(player_meta.get("mlb_service_days"), -1.0)
    pro_years = to_float(player_meta.get("pro_service_years"), -1.0)
    if mlb_days < 0 or pro_years < 0:
        return True, mlb_days, pro_years
    if mlb_days > max_mlb_days:
        return False, mlb_days, pro_years
    if pro_years >= max_pro_years:
        return False, mlb_days, pro_years
    return True, mlb_days, pro_years


def compute_vpc_base(
    rows: List[Dict[str, str]],
    salary_col: str,
    calib_col: str,
    vos_floor: float,
    winsor_lower: float,
    winsor_upper: float,
    players_lookup: Optional[Dict[str, Dict[str, str]]] = None,
) -> Tuple[float, int]:
    mlb_rows: List[Tuple[float, float]] = []
    market_skipped = 0
    for r in rows:
        if (r.get("League_Level") or "").strip() != "ML":
            continue
        if to_float(r.get("Contract_is_major"), 0.0) != 1.0:
            continue
        salary = to_float(r.get(salary_col), 0.0)
        vos = to_float(r.get(calib_col), 0.0)
        if salary <= 0 or vos < vos_floor:
            continue
        if players_lookup is not None:
            pid = (r.get("ID") or "").strip()
            pmeta = players_lookup.get(pid)
            if not is_market_comp_player(r, pmeta):
                market_skipped += 1
                continue
        mlb_rows.append((salary, vos))

    if not mlb_rows:
        raise ValueError("No MLB rows available for VPC calibration after filters.")
    if players_lookup is not None:
        logger.info("Skipped %d MLB rows by market-comp /players filter", market_skipped)

    salaries = [s for s, _ in mlb_rows]
    vos_values = [v for _, v in mlb_rows]
    s_lo = percentile(salaries, winsor_lower)
    s_hi = percentile(salaries, winsor_upper)
    v_lo = percentile(vos_values, winsor_lower)
    v_hi = percentile(vos_values, winsor_upper)

    salary_sum = 0.0
    vos_sum = 0.0
    for salary, vos in mlb_rows:
        salary_sum += clamp(salary, s_lo, s_hi)
        vos_sum += clamp(vos, v_lo, v_hi)

    if vos_sum <= 0:
        raise ValueError("VPC denominator is zero after winsorization.")
    return (salary_sum / vos_sum), len(mlb_rows)


def apply_player_valuation(
    rows: List[Dict[str, str]],
    vpc_base: float,
    vos_col: str,
    pot_col: str,
    players_lookup: Optional[Dict[str, Dict[str, str]]] = None,
    prospect_max_mlb_days: float = 90.0,
    prospect_max_pro_years: float = 6.0,
    risk_gap_per_point_vet: float = 0.0065,
    risk_gap_min_vet: float = 0.62,
    risk_gap_per_point_prospect: float = 0.0032,
    risk_gap_min_prospect: float = 0.78,
    age_plateau_half_width: float = 2.0,
    age_young_bonus_per_year: float = 0.04,
    age_old_penalty_per_year: float = 0.06,
    age_m_min: float = 0.70,
    age_m_max: float = 1.15,
    position_shares: Optional[Dict[str, float]] = None,
    position_adjust: bool = True,
    rp_debuff: float = 0.93,
    premium_pos_boost: float = 1.04,
    scarcity_strength: float = 0.05,
    scarcity_min: float = 0.94,
    scarcity_max: float = 1.06,
    league_scarcity: bool = False,
    level_prox_toward_one: float = 0.0,
) -> List[Dict[str, object]]:
    out: List[Dict[str, object]] = []
    n_prospect_risk = 0
    n_vet_risk = 0
    n_no_api = 0
    for r in rows:
        league_level = (r.get("League_Level") or "").strip()
        org = (r.get("Org") or "").strip()
        if league_level == "ML" or org == "":
            continue

        vos = to_float(r.get(vos_col), 0.0)
        vos_pot_raw = to_float(r.get(pot_col), float("nan"))
        vos_pot = vos if math.isnan(vos_pot_raw) else vos_pot_raw
        age = to_float(r.get("Age"), float("nan"))
        is_major = int(to_float(r.get("Contract_is_major"), 0.0))

        m_level = blend_multiplier_toward_one(
            LEVEL_MULT.get(league_level, 0.8625), level_prox_toward_one
        )
        m_prox = blend_multiplier_toward_one(
            PROX_MULT.get(league_level, 0.78), level_prox_toward_one
        )
        risk_level_mult = RISK_GAP_LEVEL_MULT.get(league_level, 0.50)
        gap = max(0.0, vos_pot - vos)

        pid = (r.get("ID") or "").strip()
        pmeta = players_lookup.get(pid) if players_lookup is not None else None
        use_prospect_risk, mlb_days_meta, pro_years_meta = is_prospect_for_risk(
            pmeta, prospect_max_mlb_days, prospect_max_pro_years
        )
        if players_lookup is None:
            use_prospect_risk = True
            n_no_api += 1
        elif pmeta is None:
            n_no_api += 1

        if use_prospect_risk:
            m_risk = clamp(
                1.05 - (risk_gap_per_point_prospect * risk_level_mult * gap),
                risk_gap_min_prospect,
                1.05,
            )
            risk_mode = "prospect"
            n_prospect_risk += 1
        else:
            m_risk = clamp(
                1.05 - (risk_gap_per_point_vet * risk_level_mult * gap),
                risk_gap_min_vet,
                1.05,
            )
            risk_mode = "vet"
            n_vet_risk += 1

        base_age = BASELINE_AGE.get(league_level)
        age_dev = float("nan")
        if base_age is None or math.isnan(age):
            m_age = 1.0
        else:
            m_age, age_dev = age_for_level_multiplier(
                age,
                float(base_age),
                age_plateau_half_width,
                age_young_bonus_per_year,
                age_old_penalty_per_year,
                age_m_min,
                age_m_max,
            )

        m_control = 1.03 if is_major == 1 else 1.00
        is_non40 = 1 if is_major == 0 else 0

        pos_bucket = canonical_position_bucket(r)
        proj_role = projected_role_field(r)
        if position_adjust:
            m_pos_role = role_static_multiplier(pos_bucket, rp_debuff, premium_pos_boost)
            if league_scarcity and position_shares is not None:
                m_pos_scarcity = scarcity_multiplier(
                    pos_bucket, position_shares, scarcity_strength, scarcity_min, scarcity_max
                )
            else:
                m_pos_scarcity = 1.0
            m_pos = m_pos_role * m_pos_scarcity
        else:
            m_pos_role = 1.0
            m_pos_scarcity = 1.0
            m_pos = 1.0

        # Farm valuation is projection-first: use potential VOS as the base value term.
        proj_vos = vos_pot
        farm_value = (
            proj_vos * vpc_base * m_level * m_prox * m_risk * m_age * m_control * m_pos
        )

        row_out: Dict[str, object] = {
            "ID": r.get("ID", ""),
            "Name": r.get("Name", ""),
            "Org": org,
            "Team": r.get("Team", ""),
            "League_Level": league_level,
            "Age": r.get("Age", ""),
            "Pos": r.get("Pos", ""),
            "projected_role": proj_role,
            "pos_bucket": pos_bucket,
            "m_pos_role": round(m_pos_role, 4),
            "m_pos_scarcity": round(m_pos_scarcity, 4),
            "m_pos": round(m_pos, 4),
            "vos": round(vos, 4),
            "vos_pot": round(vos_pot, 4),
            "proj_vos": round(proj_vos, 4),
            "vos_gap": round(gap, 4),
            "m_risk_mode": risk_mode,
            "mlb_service_days_api": int(mlb_days_meta) if mlb_days_meta >= 0 else "",
            "pro_service_years_api": round(pro_years_meta, 2) if pro_years_meta >= 0 else "",
            "is_prospect_org": 1 if use_prospect_risk else 0,
            "is_major": is_major,
            "is_non40": is_non40,
            "age_baseline_level": round(base_age, 2) if base_age is not None else "",
            "age_dev_vs_level": round(age_dev, 2) if not math.isnan(age_dev) else "",
            "m_level": round(m_level, 4),
            "m_prox": round(m_prox, 4),
            "m_risk": round(m_risk, 4),
            "m_age": round(m_age, 4),
            "m_control": round(m_control, 4),
            "farm_value": round(farm_value, 2),
        }
        out.append(row_out)
    logger.info(
        "m_risk: prospect=%d vet=%d no_players_api_or_missing_id=%d (farm rows)",
        n_prospect_risk,
        n_vet_risk,
        n_no_api,
    )
    return out


def summarize_org_values(
    farm_rows: List[Dict[str, object]],
    non40_only: bool,
    top_n: int = 12,
    tail_weight: float = 0.25,
    prospect_only_org: bool = True,
) -> List[Dict[str, object]]:
    totals: Dict[str, Dict[str, object]] = {}
    non40_totals: Dict[str, Dict[str, float]] = {}

    def include_for_org(r: Dict[str, object]) -> bool:
        if prospect_only_org and int(r.get("is_prospect_org", 1)) != 1:
            return False
        if non40_only and int(r["is_non40"]) != 1:
            return False
        return True

    scope = [r for r in farm_rows if include_for_org(r)]

    for r in scope:
        org = str(r["Org"])
        totals.setdefault(org, {"farm_values": [], "num_farm_players": 0.0})
        values = totals[org]["farm_values"]
        assert isinstance(values, list)
        values.append(float(r["farm_value"]))
        totals[org]["num_farm_players"] = float(totals[org]["num_farm_players"]) + 1.0

    for r in farm_rows:
        if int(r["is_non40"]) != 1:
            continue
        if prospect_only_org and int(r.get("is_prospect_org", 1)) != 1:
            continue
        org = str(r["Org"])
        non40_totals.setdefault(org, {"farm_value_non40": 0.0, "num_non40": 0.0})
        non40_totals[org]["farm_value_non40"] += float(r["farm_value"])
        non40_totals[org]["num_non40"] += 1.0

    out: List[Dict[str, object]] = []
    for org, info in totals.items():
        cnt = int(info["num_farm_players"])
        org_values = sorted([float(v) for v in info["farm_values"]], reverse=True)
        top_component = sum(org_values[:top_n])
        tail_component = tail_weight * sum(org_values[top_n:])
        total = top_component + tail_component
        non40 = non40_totals.get(org, {"farm_value_non40": 0.0, "num_non40": 0.0})
        out.append(
            {
                "Org": org,
                "farm_value_total": round(total, 2),
                "farm_value_top12": round(top_component, 2),
                "farm_value_tail_weighted": round(tail_component, 2),
                "num_farm_players": cnt,
                "avg_value_per_player": round(total / cnt, 2) if cnt > 0 else 0.0,
                "farm_value_non40": round(float(non40["farm_value_non40"]), 2),
                "num_non40": int(non40["num_non40"]),
            }
        )
    out.sort(key=lambda x: float(x["farm_value_total"]), reverse=True)
    return out


def assign_prospect_rankings(farm_rows: List[Dict[str, object]]) -> None:
    """
    Set prospect_rank_overall and prospect_rank_org by farm_value (desc) among rows
    with m_risk_mode == prospect. Non-prospect rows get empty strings.
    """
    for r in farm_rows:
        r["prospect_rank_overall"] = ""
        r["prospect_rank_org"] = ""

    prospects = [r for r in farm_rows if str(r.get("m_risk_mode", "")) == "prospect"]
    sort_key = lambda r: (-float(r["farm_value"]), str(r.get("Name", "")), str(r.get("ID", "")))

    for i, r in enumerate(sorted(prospects, key=sort_key), start=1):
        r["prospect_rank_overall"] = i

    by_org: Dict[str, List[Dict[str, object]]] = {}
    for r in prospects:
        org = str(r.get("Org", ""))
        by_org.setdefault(org, []).append(r)
    for rows in by_org.values():
        for i, r in enumerate(sorted(rows, key=sort_key), start=1):
            r["prospect_rank_org"] = i


def write_csv(path: Path, rows: List[Dict[str, object]], fieldnames: List[str]) -> None:
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def write_md_table(
    path: Path,
    rows: List[Dict[str, object]],
    fieldnames: List[str],
    title: str = "",
    max_rows: Optional[int] = None,
    dollar_cols: Optional[List[str]] = None,
) -> None:
    """Write a Markdown table alongside a CSV output for Obsidian quick-reference.

    Args:
        path: Output .md file path.
        rows: Data rows (dicts).
        fieldnames: Column order.
        title: Optional H2 heading above the table.
        max_rows: If set, only emit the first N rows (add a note if truncated).
        dollar_cols: Column names whose values should be formatted as $X,XXX,XXX.
    """
    dollar_cols_set = set(dollar_cols or [])

    def fmt(col: str, val: object) -> str:
        if col in dollar_cols_set:
            try:
                return f"${float(val):,.0f}"
            except (TypeError, ValueError):
                pass
        return str(val) if val is not None else ""

    display_rows = rows[:max_rows] if max_rows is not None else rows
    truncated = max_rows is not None and len(rows) > max_rows

    lines: List[str] = []
    if title:
        lines.append(f"## {title}")
        lines.append("")

    if not display_rows:
        lines.append("_No data._")
    else:
        header = "| " + " | ".join(fieldnames) + " |"
        sep = "| " + " | ".join("---" for _ in fieldnames) + " |"
        lines.append(header)
        lines.append(sep)
        for row in display_rows:
            cells = [fmt(f, row.get(f, "")) for f in fieldnames]
            lines.append("| " + " | ".join(cells) + " |")

    if truncated:
        lines.append("")
        lines.append(f"_Showing {max_rows} of {len(rows)} rows. See CSV for full data._")

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def infer_league_slug(input_path: Path, league_arg: Optional[str]) -> Optional[str]:
    """League slug from --league or evaluation_summary_<league>_*.csv filename."""
    if league_arg and str(league_arg).strip():
        return str(league_arg).strip().lower()
    stem = input_path.stem
    if stem.startswith("evaluation_summary_"):
        rest = stem[len("evaluation_summary_") :]
        parts = rest.split("_", 1)
        if parts and parts[0]:
            return parts[0].lower()
    return None


def default_org_output_path(input_path: Path, league_slug: Optional[str], run_ts: str) -> Path:
    """Unique per run: farm_values_<league>_<run_ts>.csv (or farm_values_<run_ts>.csv)."""
    if league_slug:
        name = f"farm_values_{league_slug}_{run_ts}.csv"
        out = SCRIPT_DIR / league_slug / "farm" / name
    else:
        name = f"farm_values_{run_ts}.csv"
        out = input_path.with_name(name)
    out.parent.mkdir(parents=True, exist_ok=True)
    return out


def resolve_input_path(input_path: Optional[Path], league: Optional[str], search_dir: Path) -> Path:
    if input_path is not None:
        return input_path
    league_slug = (league or "").strip()
    if not league_slug:
        raise ValueError("Provide either --input or --league.")
    pattern = f"evaluation_summary_{league_slug}_*.csv"
    matches = list(search_dir.glob(pattern))
    if not matches:
        raise FileNotFoundError(f"No files found matching {pattern} in {search_dir}")
    return sorted(matches, key=lambda p: p.name)[-1]


def main() -> int:
    args = parse_args()
    logging.basicConfig(level=getattr(logging, args.log_level), format="%(levelname)s: %(message)s")
    run_ts = datetime.now().strftime("%Y%m%d_%H%M%S")

    _eval_search_dir = SCRIPT_DIR / args.league / "eval" if args.league else Path.cwd()
    input_path = resolve_input_path(args.input, args.league, _eval_search_dir)
    if not input_path.exists():
        raise FileNotFoundError(f"Input not found: {input_path}")
    logger.info("Using input file: %s", input_path)

    rows, fieldnames = read_csv_rows(input_path)
    validate_columns(
        fieldnames,
        ["ID", "Org", "League_Level", "Age", "Contract_is_major", args.salary_col, args.vos_col, args.pot_col],
    )

    players_lookup: Optional[Dict[str, Dict[str, str]]] = None
    base_url = resolve_base_url(args.league, args.base_url, args.league_url_config)
    if base_url:
        try:
            players_lookup = build_players_lookup(base_url)
            logger.info("Loaded %d /players rows from %s", len(players_lookup), base_url)
        except (URLError, TimeoutError, ValueError) as e:
            logger.warning("Failed to load /players data from %s: %s", base_url, e)
    else:
        logger.warning("No league/base-url provided for /players filter; using legacy MLB calibration filter only.")

    vpc_base, mlb_count = compute_vpc_base(
        rows=rows,
        salary_col=args.salary_col,
        calib_col=args.pot_col,
        vos_floor=args.vos_floor,
        winsor_lower=args.winsor_lower,
        winsor_upper=args.winsor_upper,
        players_lookup=players_lookup,
    )
    logger.info("Calibrated VPC (dollars per projected VOS): %.2f", vpc_base)
    logger.info("MLB calibration sample size: %d", mlb_count)

    position_shares: Optional[Dict[str, float]] = None
    if args.league_scarcity:
        position_shares = compute_league_position_shares(rows)
        logger.info(
            "League farm pool position shares (--league-scarcity): %s",
            ", ".join(f"{k}={position_shares[k]:.3f}" for k in POSITION_BUCKETS),
        )

    if args.level_prox_toward_one != 0.0:
        logger.info(
            "Blending m_level/m_prox toward 1.0 (--level-prox-toward-one=%s)",
            args.level_prox_toward_one,
        )

    farm_rows = apply_player_valuation(
        rows,
        vpc_base,
        args.vos_col,
        args.pot_col,
        players_lookup=players_lookup,
        prospect_max_mlb_days=args.prospect_max_mlb_days,
        prospect_max_pro_years=args.prospect_max_pro_years,
        risk_gap_per_point_vet=args.risk_gap_per_point_vet,
        risk_gap_min_vet=args.risk_gap_min_vet,
        risk_gap_per_point_prospect=args.risk_gap_per_point_prospect,
        risk_gap_min_prospect=args.risk_gap_min_prospect,
        age_plateau_half_width=args.age_plateau_half_width,
        age_young_bonus_per_year=args.age_young_bonus_per_year,
        age_old_penalty_per_year=args.age_old_penalty_per_year,
        age_m_min=args.age_m_min,
        age_m_max=args.age_m_max,
        position_shares=position_shares,
        position_adjust=not args.disable_position_adjust,
        rp_debuff=args.rp_debuff,
        premium_pos_boost=args.premium_pos_boost,
        scarcity_strength=args.scarcity_strength,
        scarcity_min=args.scarcity_min,
        scarcity_max=args.scarcity_max,
        league_scarcity=args.league_scarcity,
        level_prox_toward_one=args.level_prox_toward_one,
    )
    logger.info("Farm player rows valued: %d", len(farm_rows))
    assign_prospect_rankings(farm_rows)

    org_rows = summarize_org_values(
        farm_rows,
        non40_only=args.non40_only,
        top_n=12,
        tail_weight=0.25,
        prospect_only_org=not args.org_include_non_prospects,
    )
    league_slug = infer_league_slug(input_path, args.league)
    output_org = args.output_org or default_org_output_path(input_path, league_slug, run_ts)
    org_fields = [
        "Org",
        "farm_value_total",
        "farm_value_top12",
        "farm_value_tail_weighted",
        "num_farm_players",
        "avg_value_per_player",
        "farm_value_non40",
        "num_non40",
    ]
    write_csv(output_org, org_rows, org_fields)
    logger.info("Wrote organization farm values: %s", output_org)
    org_md = output_org.with_suffix(".md")
    write_md_table(
        org_md, org_rows, org_fields,
        title="Farm System Values by Org",
        dollar_cols=["farm_value_total", "farm_value_top12", "farm_value_tail_weighted", "farm_value_non40", "avg_value_per_player"],
    )
    logger.info("Wrote organization farm values MD: %s", org_md)

    if args.output_players:
        player_fields = [
            "ID",
            "prospect_rank_overall",
            "prospect_rank_org",
            "Name",
            "Org",
            "Team",
            "League_Level",
            "Age",
            "Pos",
            "projected_role",
            "pos_bucket",
            "m_pos_role",
            "m_pos_scarcity",
            "m_pos",
            "vos",
            "vos_pot",
            "proj_vos",
            "vos_gap",
            "m_risk_mode",
            "mlb_service_days_api",
            "pro_service_years_api",
            "is_prospect_org",
            "is_major",
            "is_non40",
            "age_baseline_level",
            "age_dev_vs_level",
            "m_level",
            "m_prox",
            "m_risk",
            "m_age",
            "m_control",
            "farm_value",
        ]
        sorted_players = sorted(farm_rows, key=lambda r: float(r["farm_value"]), reverse=True)
        write_csv(args.output_players, sorted_players, player_fields)
        logger.info("Wrote player farm value details: %s", args.output_players)
        players_md = Path(args.output_players).with_suffix(".md")
        md_player_fields = ["prospect_rank_overall", "prospect_rank_org", "Name", "Org", "Pos", "League_Level", "Age", "vos", "vos_pot", "farm_value"]
        write_md_table(
            players_md, sorted_players, md_player_fields,
            title="Farm Player Values",
            max_rows=100,
            dollar_cols=["farm_value"],
        )
        logger.info("Wrote player farm value MD: %s", players_md)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
