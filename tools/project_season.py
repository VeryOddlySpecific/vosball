#!/usr/bin/env python3
"""
project_season.py — Pythagorean season projection from a depth_chart roster.

Reads {league}/depth/{org}_{level}_*.csv plus the matching *_constants.json
sidecar (both produced by depth_chart.py) and produces a win projection.

Method
------
RS:
  team_wOBA = lineup-slot-weighted average of starters' wOBA
              blended 70% vs RHP / 30% vs LHP
  RS = ((team_wOBA - lg_wOBA) / wOBA_scale + lg_R_per_PA) * team_PA

RA:
  team_FIP = IP-share-weighted FIP across SPs and bullpen roles
  RA       = (team_FIP * team_IP / 9) * 1.07   # convert ER → R

Defense shade (optional):
  RA *= 1 - 0.005 * (avg_starter_defense_score - 50)   # capped to ±10%

Wins:
  Pythagenpat exponent x = ((RS + RA) / games_played) ^ 0.287
  W = games_played * RS^x / (RS^x + RA^x)

Output
------
Writes {org}_{level}_projection_{ts}.md alongside the depth chart files.

Usage
-----
    python project_season.py --league sahl --org "Houston Astros" --level ML
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
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)
SCRIPT_DIR = Path(__file__).resolve().parent.parent

# Lineup PA shares per slot (1-9) — based on real-life MLB averages, normalized.
LINEUP_PA_SHARE = {
    1: 0.122, 2: 0.119, 3: 0.117, 4: 0.114, 5: 0.111,
    6: 0.108, 7: 0.106, 8: 0.103, 9: 0.100,
}

# Default platoon split: ~70% PAs come against RHP, ~30% against LHP.
DEFAULT_VS_R_SHARE = 0.70

# Pitcher IP allocations per role (fractions of total team IP). Tuned for a
# 13-pitcher staff; if the staff has more (minor leagues), the leftover is
# distributed proportionally to MR.
ROLE_IP_SHARE = {
    "SP1": 0.121, "SP2": 0.121, "SP3": 0.121, "SP4": 0.121, "SP5": 0.121,
    "CL":  0.041,
    "SU":  0.048,   # per SU pitcher
    "MR":  0.041,   # per MR pitcher
    "LR":  0.055,
}

# Total games per season at each level (rough OOTP defaults). Override with --games.
DEFAULT_GAMES_BY_LEVEL = {"ML": 162, "AAA": 150, "AA": 138, "A+": 132, "A": 132, "A-": 76, "R": 60}

# ER → R bump (unearned runs typically ~7% on top of ER).
ER_TO_R_MULT = 1.07


# -----------------------------------------------------------------------------
# CLI / IO
# -----------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Project a season win total from a depth_chart roster.")
    p.add_argument("--league", required=True)
    p.add_argument("--org", default=None, help="Org name (eval Org column). Required unless --all-orgs is passed.")
    p.add_argument("--level", default=None, help="Single level to project. Required unless --all-levels is passed.")
    p.add_argument("--all-orgs", action="store_true",
                   help="Project every ML org from teams-{league}.json. Pairs naturally with --all-levels.")
    p.add_argument("--all-levels", action="store_true",
                   help="Project every level present in depth_config.json (per the chosen org/orgs).")
    p.add_argument("--park-factors", type=Path, default=None,
                   help="Path to combined teams[] park-factors file (used only for org_code mapping in --all-orgs mode).")
    p.add_argument("--input", type=Path, default=None,
                   help="Specific depth chart CSV (default: latest matching {org}_{level}_*.csv). Single-org/level only.")
    p.add_argument("--constants", type=Path, default=None,
                   help="Specific constants sidecar JSON (default: paired with --input or latest).")
    p.add_argument("--year", type=int, default=None,
                   help="Season year (default: read from the constants sidecar). Used for current-standings filtering and the league summary header.")
    p.add_argument("--games", type=int, default=None,
                   help="Schedule length (default: 162 for ML, fewer in minors).")
    p.add_argument("--vs-r-share", type=float, default=DEFAULT_VS_R_SHARE,
                   help="Fraction of team PAs vs RHP (default 0.70).")
    p.add_argument("--blend-current-fip", type=float, default=0.5,
                   help="Weight (0-1) on current-year-only FIP vs the 3-yr-weighted FIP. Default 0.5.")
    p.add_argument("--blend-current-woba", type=float, default=0.5,
                   help="Weight (0-1) on current-year-only wOBA vs the 3-yr-weighted wOBA. Default 0.5. "
                        "Helps catch hitters whose 2061 form differs significantly from their 3-yr trailing average.")
    p.add_argument("--qual-pa-per-game", type=float, default=3.1,
                   help="Hitter qualifying threshold: PA per team game played. Default 3.1 (MLB standard). "
                        "Threshold scales automatically with where you are in the season.")
    p.add_argument("--qual-ip-per-game", type=float, default=1.0,
                   help="Starter qualifying threshold: IP per team game played. Default 1.0 (MLB standard for SPs).")
    p.add_argument("--qual-reliever-ip-per-game", type=float, default=0.3,
                   help="Reliever qualifying threshold: IP per team game played. Default 0.3 — about 50 IP over 162 games, "
                        "the real-MLB convention for relievers.")
    p.add_argument("--qual-counting-fraction", type=float, default=0.25,
                   help="Counting-stat leaderboards (HR/RBI/W/K/etc) require this fraction of the rate-stat "
                        "qualifier so September call-ups can still appear. Default 0.25 (i.e. 25%% of full qualified).")
    p.add_argument("--leader-mode", choices=["comprehensive", "individual", "both"], default="comprehensive",
                   help="'comprehensive' = single z-score-sum table per side (hitters/pitchers). "
                        "'individual' = one table per stat (legacy behavior). 'both' = comprehensive + individual. "
                        "Default 'comprehensive'.")
    p.add_argument("--defense-shade-strength", type=float, default=0.005,
                   help="RA shift per Defense_Score point above/below 50, capped at ±10%%. Default 0.005.")
    p.add_argument("--no-defense-shade", action="store_true",
                   help="Skip defense-based RA adjustment (equivalent to --defense-shade-strength 0).")
    p.add_argument("--no-summary", action="store_true",
                   help="Skip the league projection rollup written when projecting multiple orgs.")
    p.add_argument("--no-rebalance", action="store_true",
                   help="Skip the roster-realistic recalibration pass (multi-org runs only).")
    p.add_argument("--use-current-standings", action="store_true",
                   help="Treat the projection as rest-of-season only. Pulls actual W/L/RS/RA "
                        "from /gamehistory, applies projected per-game rates to the remaining "
                        "schedule, and reports actual + projected = final.")
    p.add_argument("--depth-config", type=Path, default=Path("config") / "depth_config.json",
                   help="Used when --all-levels is set, to enumerate which levels to project.")
    p.add_argument("--output-dir", type=Path, default=None)
    p.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    return p.parse_args()


def find_latest_pair(
    league: str,
    org: str,
    level: str,
    org_code: Optional[str] = None,
) -> Tuple[Path, Path]:
    """Locate the newest depth CSV and its constants sidecar.

    depth_chart.py names outputs using the team_code (e.g. ``wlg_A-_*.csv``)
    when a code is available; only falls back to the slugified org name when
    no code resolves. So we look up by code first, then by slug.
    """
    depth_dir = SCRIPT_DIR / league / "depth"
    if not depth_dir.exists():
        raise FileNotFoundError(f"No depth output dir at {depth_dir}")

    # Candidate slugs in priority order: explicit code, then legacy slug.
    candidates: List[str] = []
    if org_code:
        candidates.append(org_code.strip().lower())
    legacy_slug = org.lower().replace(" ", "_")
    if legacy_slug and legacy_slug not in candidates:
        candidates.append(legacy_slug)

    tried: List[str] = []
    for slug in candidates:
        csv_pattern = f"{slug}_{level}_*.csv"
        tried.append(csv_pattern)
        csv_matches = [
            p for p in depth_dir.glob(csv_pattern)
            if "_constants" not in p.name
            and "_player_stats" not in p.name
        ]
        if not csv_matches:
            continue
        csv_path = sorted(csv_matches, key=lambda p: p.name)[-1]
        const_path = csv_path.with_name(csv_path.stem + "_constants.json")
        if not const_path.exists():
            raise FileNotFoundError(f"Constants sidecar missing: {const_path}")
        return csv_path, const_path

    raise FileNotFoundError(
        f"No depth CSV found matching any of: {', '.join(tried)}"
    )


def load_csv(path: Path) -> List[Dict[str, str]]:
    with path.open("r", encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def to_float(v: Any, default: float = 0.0) -> float:
    try:
        s = str(v).strip()
        return float(s) if s else default
    except (TypeError, ValueError):
        return default


# -----------------------------------------------------------------------------
# Runs scored
# -----------------------------------------------------------------------------

def starters_for_lineup(rows: List[Dict[str, str]], split: str) -> List[Dict[str, str]]:
    """Pick the 9 hitters in slot order for vs_l ('L') or vs_r ('R')."""
    slot_col = f"lineup_slot_{split}"
    starters_by_slot: Dict[int, Dict[str, str]] = {}
    for r in rows:
        slot_val = (r.get(slot_col) or "").strip()
        if not slot_val:
            continue
        try:
            slot = int(float(slot_val))
        except ValueError:
            continue
        if 1 <= slot <= 9:
            starters_by_slot[slot] = r
    return [starters_by_slot[s] for s in sorted(starters_by_slot)]


def _blended_woba(
    row: Dict[str, str],
    full_col: str,
    current_col: str,
    fallback_full_col: str,
    fallback_current_col: str,
    blend: float,
) -> float:
    """Per-player wOBA blended between current-year-only and 3-yr-weighted.
    Mirrors the _blended_fip helper. Falls back gracefully if either half is
    missing (e.g., hitter only played this year, or has no 2061 stats yet)."""
    full = to_float(row.get(full_col), 0.0)
    if full <= 0:
        full = to_float(row.get(fallback_full_col), 0.0)
    current = to_float(row.get(current_col), 0.0)
    if current <= 0:
        current = to_float(row.get(fallback_current_col), 0.0)

    if full <= 0 and current <= 0:
        return 0.320  # league-avg-ish fallback
    if current <= 0:
        return full
    if full <= 0:
        return current
    b = max(0.0, min(1.0, blend))
    return (1.0 - b) * full + b * current


def split_team_woba(
    rows: List[Dict[str, str]],
    split: str,
    blend_current: float = 0.0,
) -> Tuple[float, int]:
    """Lineup-PA-share-weighted team wOBA for split=='L' or 'R'.

    Returns (team_wOBA, slots_filled). When fewer than 9 slots are filled,
    weights are renormalized so the remaining slots still sum to 1. Per-player
    wOBA is a blend of 3-yr-weighted and current-year-only values controlled
    by ``blend_current`` (0 = pure 3-yr, 1 = pure current).
    """
    starters = starters_for_lineup(rows, split)
    if not starters:
        return 0.0, 0
    woba_col = f"wOBA_vs_{split}"
    woba_cur_col = f"wOBA_vs_{split}_current"
    weighted_sum = 0.0
    total_weight = 0.0
    for r in starters:
        slot_val = int(float(r[f"lineup_slot_{split}"]))
        share = LINEUP_PA_SHARE.get(slot_val, 0.0)
        woba = _blended_woba(r, woba_col, woba_cur_col, "wOBA", "wOBA_current", blend_current)
        weighted_sum += woba * share
        total_weight += share
    return (weighted_sum / total_weight if total_weight > 0 else 0.0), len(starters)


def _resolve_lg_block(constants: Dict[str, Any], view: str = "full") -> Dict[str, float]:
    """Pull league averages from the new {full, current} sidecar schema, or
    fall back to the legacy flat schema when older sidecars are encountered."""
    block = constants.get(view) or constants.get("full") or {}
    if not block:
        # Legacy flat schema: lg_wOBA / lg_R_per_PA / lg_FIP / lg_ERA at root.
        return {
            "lg_wOBA": float(constants.get("lg_wOBA", 0.320)),
            "lg_R_per_PA": float(constants.get("lg_R_per_PA", 0.115)),
            "lg_FIP": float(constants.get("lg_FIP", 4.00)),
            "lg_ERA": float(constants.get("lg_ERA", 4.00)),
        }
    return {
        "lg_wOBA": float(block.get("lg_wOBA", 0.320)),
        "lg_R_per_PA": float(block.get("lg_R_per_PA", 0.115)),
        "lg_FIP": float(block.get("lg_FIP", 4.00)),
        "lg_ERA": float(block.get("lg_ERA", 4.00)),
    }


def project_runs_scored(
    rows: List[Dict[str, str]],
    constants: Dict[str, Any],
    games: int,
    vs_r_share: float,
    blend_current_woba: float = 0.0,
) -> Dict[str, Any]:
    lg_full = _resolve_lg_block(constants, "full")
    lg_current = _resolve_lg_block(constants, "current")
    b = max(0.0, min(1.0, blend_current_woba))
    lg_wOBA = (1 - b) * lg_full["lg_wOBA"] + b * lg_current["lg_wOBA"]
    lg_R_per_PA = (1 - b) * lg_full["lg_R_per_PA"] + b * lg_current["lg_R_per_PA"]
    woba_scale = float(constants.get("wOBA_scale", 1.20))

    team_wOBA_R, n_r = split_team_woba(rows, "R", blend_current=b)
    team_wOBA_L, n_l = split_team_woba(rows, "L", blend_current=b)
    if n_r == 0 and n_l == 0:
        # Fall back to overall wOBA across hitters with positive PA.
        starters = [r for r in rows if to_float(r.get("PA"), 0.0) > 0 and not _truthy(r.get("is_pitcher"))]
        team_wOBA = sum(to_float(r["wOBA"], 0) for r in starters[:9]) / max(1, min(9, len(starters)))
        team_wOBA_R = team_wOBA_L = team_wOBA

    team_wOBA = vs_r_share * team_wOBA_R + (1.0 - vs_r_share) * team_wOBA_L
    rs_per_pa = (team_wOBA - lg_wOBA) / woba_scale + lg_R_per_PA

    # Approximate team PA: ~38.4 PA/game (real MLB ~38, OOTP scales similarly)
    team_pa = 38.4 * games
    rs = max(0.0, rs_per_pa * team_pa)

    return {
        "team_wOBA_vs_R": team_wOBA_R,
        "team_wOBA_vs_L": team_wOBA_L,
        "team_wOBA": team_wOBA,
        "lg_wOBA": lg_wOBA,
        "lg_wOBA_full": lg_full["lg_wOBA"],
        "lg_wOBA_current": lg_current["lg_wOBA"],
        "lg_wOBA_blend": lg_wOBA,
        "blend_woba": b,
        "rs_per_pa": rs_per_pa,
        "team_pa": team_pa,
        "rs": rs,
    }


# -----------------------------------------------------------------------------
# Runs allowed
# -----------------------------------------------------------------------------

def _truthy(s: Any) -> bool:
    return str(s).strip().lower() in {"true", "1", "yes"}


def _role_share_lookup(tier: str, role_counts: Dict[str, int]) -> float:
    """Map the depth-chart tier label to its IP share."""
    t = tier.strip().upper()
    if t.startswith("SP"):
        # SP1..SP5 each own a single share key; tighter staffs adjust proportionally.
        return ROLE_IP_SHARE.get(t, ROLE_IP_SHARE["SP1"])
    if t.startswith("CL"):
        return ROLE_IP_SHARE["CL"]
    if t.startswith("SU"):
        return ROLE_IP_SHARE["SU"]
    if t.startswith("MR"):
        return ROLE_IP_SHARE["MR"]
    if t.startswith("LR"):
        return ROLE_IP_SHARE["LR"]
    return 0.0


def _blended_fip(row: Dict[str, str], blend: float, fallback: float) -> float:
    """Return per-pitcher FIP blended between current-year and 3-yr-weighted.

    blend=0 → use overall (3-yr) FIP only. blend=1 → use current-year FIP only.
    Missing FIP falls back to lg_FIP. Missing FIP_current degrades the blend
    to overall (no current-year data → can't blend it in).
    """
    fip_full = to_float(row.get("FIP"), 0.0)
    fip_current = to_float(row.get("FIP_current"), 0.0)
    if fip_full <= 0 and fip_current <= 0:
        return fallback
    if fip_current <= 0:
        return fip_full
    if fip_full <= 0:
        return fip_current
    b = max(0.0, min(1.0, blend))
    return (1.0 - b) * fip_full + b * fip_current


def project_runs_allowed(
    rows: List[Dict[str, str]],
    constants: Dict[str, Any],
    games: int,
    use_defense: bool,
    blend_current_fip: float,
    defense_shade_strength: float,
) -> Dict[str, Any]:
    pitchers = [r for r in rows if _truthy(r.get("is_pitcher"))]
    if not pitchers:
        return {"team_FIP": 0.0, "ra": 0.0, "team_ip": 0.0, "defense_mult": 1.0,
                "blend": blend_current_fip}

    lg_full = _resolve_lg_block(constants, "full")
    lg_current = _resolve_lg_block(constants, "current")
    lg_FIP_blend = (1.0 - blend_current_fip) * lg_full["lg_FIP"] + blend_current_fip * lg_current["lg_FIP"]

    # Aggregate IP share by role-tier.
    role_counts: Dict[str, int] = {}
    for r in pitchers:
        t = (r.get("tier") or "").upper()
        if t.startswith("MR"):
            role_counts["MR"] = role_counts.get("MR", 0) + 1
        elif t.startswith("SU"):
            role_counts["SU"] = role_counts.get("SU", 0) + 1

    weighted_fip = 0.0
    total_share = 0.0
    for r in pitchers:
        share = _role_share_lookup(r.get("tier", ""), role_counts)
        if share <= 0:
            continue
        fip = _blended_fip(r, blend_current_fip, lg_FIP_blend)
        weighted_fip += fip * share
        total_share += share

    team_FIP = weighted_fip / total_share if total_share > 0 else lg_FIP_blend
    team_ip = games * 8.95
    ra = (team_FIP * team_ip / 9.0) * ER_TO_R_MULT

    defense_mult = 1.0
    if use_defense and defense_shade_strength > 0:
        starter_def_scores = [
            to_float(r.get("defense_score"), 50.0)
            for r in rows
            if not _truthy(r.get("is_pitcher")) and (r.get("lineup_slot_R") or r.get("lineup_slot_L"))
        ]
        if starter_def_scores:
            avg_def = sum(starter_def_scores) / len(starter_def_scores)
            shift = max(-0.10, min(0.10, defense_shade_strength * (avg_def - 50.0)))
            defense_mult = 1.0 - shift
            ra *= defense_mult

    return {
        "team_FIP": team_FIP,
        "lg_FIP_full": lg_full["lg_FIP"],
        "lg_FIP_current": lg_current["lg_FIP"],
        "lg_FIP_blend": lg_FIP_blend,
        "team_ip": team_ip,
        "ra": ra,
        "defense_mult": defense_mult,
        "blend": blend_current_fip,
    }


# -----------------------------------------------------------------------------
# Wins (Pythagenpat)
# -----------------------------------------------------------------------------

def pythagenpat_wins(rs: float, ra: float, games: int) -> Tuple[float, float]:
    if rs <= 0 or ra <= 0 or games <= 0:
        return 0.0, 1.83
    rpg = (rs + ra) / games
    x = max(1.0, min(2.5, rpg ** 0.287))
    win_pct = (rs ** x) / ((rs ** x) + (ra ** x))
    return win_pct * games, x


# -----------------------------------------------------------------------------
# Per-player stat-line projections
# -----------------------------------------------------------------------------

def _player_rates(player_stats: Optional[Dict[str, Any]], group: str, pid: str) -> Dict[str, Any]:
    """Pull a player's rate dict from the sidecar; returns {} if missing."""
    if not player_stats:
        return {}
    return (player_stats.get(group) or {}).get(str(pid).strip(), {}) or {}


def _blend_hitter_line(
    line: Dict[str, Any],
    rates: Dict[str, Any],
    remaining_pa: float,
) -> Dict[str, Any]:
    """Replace a full-season hitter projection with actual current-year stats
    plus career-rate projections of the rest of the schedule.

    line: full-season projection dict from project_hitter_lines.
    rates: sidecar entry containing both career rates and current-year totals.
    remaining_pa: PA the player is expected to accumulate over the rest of
        the season, computed from games_remaining × per-game lineup share.
        This decouples the remainder from the player's actual to-date PA so
        an injured / part-time player doesn't get inflated remainders.

    Math:
        actual_PA      = current-year PA from the sidecar
        Counting stats: actual + (career_rate × remaining_PA)
        Rate stats: (actual_AB × actual_AVG + remaining_AB × career_AVG) / combined_AB
                    same pattern for OBP/SLG. wOBA blended by PA, not AB.

    No-op if the player has zero current-year PA (treats them as preseason).
    """
    actual_pa = float(rates.get("PA_current", 0.0) or 0.0)
    remaining_pa = max(0.0, float(remaining_pa))

    if actual_pa <= 0 and remaining_pa <= 0:
        # No actuals AND no remaining schedule (e.g. season is over). Nothing
        # to project — keep the upstream full-season line as-is.
        return line

    # Current-year actuals.
    actual_ab = float(rates.get("AB_current", 0.0) or 0.0)
    actual_h = float(rates.get("H_current", 0.0) or 0.0)
    actual_hr = float(rates.get("HR_current", 0.0) or 0.0)
    actual_r = float(rates.get("R_current", 0.0) or 0.0)
    actual_rbi = float(rates.get("RBI_current", 0.0) or 0.0)
    actual_sb = float(rates.get("SB_current", 0.0) or 0.0)
    actual_avg = float(rates.get("AVG_current", 0.0) or 0.0)
    actual_obp = float(rates.get("OBP_current", 0.0) or 0.0)
    actual_slg = float(rates.get("SLG_current", 0.0) or 0.0)
    actual_woba = float(rates.get("wOBA_current", 0.0) or 0.0)

    # Career rates for projecting the remainder.
    ab_per_pa = float(rates.get("AB_per_PA", 0.0) or 0.0)
    hr_per_pa = float(rates.get("HR_per_PA", 0.0) or 0.0)
    r_per_pa = float(rates.get("R_per_PA", 0.0) or 0.0)
    rbi_per_pa = float(rates.get("RBI_per_PA", 0.0) or 0.0)
    sb_per_pa = float(rates.get("SB_per_PA", 0.0) or 0.0)
    h_per_pa = float(rates.get("H_per_PA", 0.0) or 0.0)
    career_avg = float(rates.get("AVG", 0.0) or 0.0)
    career_obp = float(rates.get("OBP", 0.0) or 0.0)
    career_slg = float(rates.get("SLG", 0.0) or 0.0)
    career_woba = float(rates.get("wOBA", 0.0) or 0.0)

    rem_ab = ab_per_pa * remaining_pa
    rem_h = h_per_pa * remaining_pa

    combined_pa = actual_pa + remaining_pa
    combined_ab = actual_ab + rem_ab

    # Slash-line blend: weight actual rates by their actual AB / PA, weight
    # career rates by the projected remaining AB / PA. Falls back to the
    # available side when the other is zero.
    def _weighted(actual_rate: float, career_rate: float, actual_w: float, rem_w: float) -> float:
        denom = actual_w + rem_w
        if denom <= 0:
            return 0.0
        return (actual_rate * actual_w + career_rate * rem_w) / denom

    blended = dict(line)
    blended["PA"] = combined_pa
    blended["AB"] = combined_ab
    blended["AVG"] = _weighted(actual_avg, career_avg, actual_ab, rem_ab)
    blended["OBP"] = _weighted(actual_obp, career_obp, actual_pa, remaining_pa)
    blended["SLG"] = _weighted(actual_slg, career_slg, actual_ab, rem_ab)
    blended["OPS"] = blended["OBP"] + blended["SLG"]
    blended["wOBA"] = _weighted(actual_woba, career_woba, actual_pa, remaining_pa)
    blended["HR"] = actual_hr + hr_per_pa * remaining_pa
    blended["R"] = actual_r + r_per_pa * remaining_pa
    blended["RBI"] = actual_rbi + rbi_per_pa * remaining_pa
    blended["SB"] = actual_sb + sb_per_pa * remaining_pa
    # BB% / K% are rates we don't project counting totals for; carry through.
    return blended


def project_hitter_lines(
    rows: List[Dict[str, str]],
    player_stats: Optional[Dict[str, Any]],
    team_pa: float,
    vs_r_share: float,
) -> List[Dict[str, Any]]:
    """Project per-hitter counting lines for the 9 starters.

    PA at each lineup slot = LINEUP_PA_SHARE × team_PA. Counting stats are
    rate × PA off the 3-yr-weighted player_stats sidecar. Slash line uses the
    sidecar's AVG / OBP / SLG directly (no platoon split blending).

    Each line stashes its sidecar rates dict on ``_rates`` and the per-game
    lineup share on ``_pa_per_game`` so apply_player_overlay() (called from
    apply_current_standings) can blend the line with current-year actuals
    once games_played is known.
    """
    if not player_stats:
        return []
    # Prefer the vs-R lineup since vs_r_share is typically ~0.7. Fall back to L.
    starters = starters_for_lineup(rows, "R") or starters_for_lineup(rows, "L")
    if not starters:
        return []

    out: List[Dict[str, Any]] = []
    for r in starters:
        slot_r = r.get("lineup_slot_R") or r.get("lineup_slot_L") or ""
        try:
            slot = int(float(str(slot_r).strip()))
        except (TypeError, ValueError):
            continue
        if not 1 <= slot <= 9:
            continue
        share = LINEUP_PA_SHARE.get(slot, 0.0)
        proj_pa = share * team_pa
        rates = _player_rates(player_stats, "hitters", r.get("pid", ""))
        if not rates:
            continue
        line = {
            "slot": slot,
            "name": rates.get("name") or r.get("name", ""),
            "pos": (r.get("primary_pos") or r.get("tier", "").split("-")[0]),
            "PA": proj_pa,
            "AB": rates.get("AB_per_PA", 0.0) * proj_pa,
            "AVG": rates.get("AVG", 0.0),
            "OBP": rates.get("OBP", 0.0),
            "SLG": rates.get("SLG", 0.0),
            "OPS": rates.get("OPS") or (rates.get("OBP", 0.0) + rates.get("SLG", 0.0)),
            "wOBA": rates.get("wOBA", 0.0),
            "HR": rates.get("HR_per_PA", 0.0) * proj_pa,
            "R": rates.get("R_per_PA", 0.0) * proj_pa,
            "RBI": rates.get("RBI_per_PA", 0.0) * proj_pa,
            "SB": rates.get("SB_per_PA", 0.0) * proj_pa,
            "BB%": rates.get("BB%", 0.0),
            "K%": rates.get("K%", 0.0),
            # Stashed for the post-fact current-standings overlay.
            "_rates": rates,
            "_pa_per_game": share * 38.4,
        }
        out.append(line)
    out.sort(key=lambda r: r["slot"])
    return out


def _blend_pitcher_line(
    line: Dict[str, Any],
    rates: Dict[str, Any],
    remaining_ip: float,
) -> Dict[str, Any]:
    """Replace a full-season pitcher projection with actual current-year IP +
    career-rate projection of the remainder. Symmetric with _blend_hitter_line.

    remaining_ip is computed from games_remaining × per-game role share so
    pitchers who've been on the IL don't get a giant remainder.
    """
    actual_ip = float(rates.get("IP_current", 0.0) or 0.0)
    if actual_ip <= 0:
        return line

    remaining_ip = max(0.0, float(remaining_ip))

    actual_g = float(rates.get("G_current", 0.0) or 0.0)
    actual_gs = float(rates.get("GS_current", 0.0) or 0.0)
    actual_w = float(rates.get("W_current", 0.0) or 0.0)
    actual_l = float(rates.get("L_current", 0.0) or 0.0)
    actual_sv = float(rates.get("SV_current", 0.0) or 0.0)
    actual_hld = float(rates.get("HLD_current", 0.0) or 0.0)
    actual_qs = float(rates.get("QS_current", 0.0) or 0.0)
    actual_era = float(rates.get("ERA_current", 0.0) or 0.0)
    actual_whip = float(rates.get("WHIP_current", 0.0) or 0.0)
    actual_fip = float(rates.get("FIP_current", 0.0) or 0.0)
    actual_k9 = float(rates.get("K/9_current", 0.0) or 0.0)
    actual_bb9 = float(rates.get("BB/9_current", 0.0) or 0.0)
    actual_hr9 = float(rates.get("HR/9_current", 0.0) or 0.0)

    career_era = float(rates.get("ERA", 0.0) or 0.0)
    career_fip = float(rates.get("FIP", 0.0) or 0.0)
    career_whip = float(rates.get("WHIP", 0.0) or 0.0)
    career_k9 = float(rates.get("K/9", 0.0) or 0.0)
    career_bb9 = float(rates.get("BB/9", 0.0) or 0.0)
    career_hr9 = float(rates.get("HR/9", 0.0) or 0.0)
    ip_per_gs = float(rates.get("IP_per_GS", 0.0) or 0.0)
    ip_per_g = float(rates.get("IP_per_G", 0.0) or 0.0)
    w_per_gs = float(rates.get("W_per_GS", 0.0) or 0.0)
    l_per_gs = float(rates.get("L_per_GS", 0.0) or 0.0)
    qs_per_gs = float(rates.get("QS_per_GS", 0.0) or 0.0)
    sv_per_g = float(rates.get("SV_per_G", 0.0) or 0.0)
    hld_per_g = float(rates.get("HLD_per_G", 0.0) or 0.0)

    rem_gs = (remaining_ip / ip_per_gs) if ip_per_gs > 0 else 0.0
    rem_g = (remaining_ip / ip_per_g) if ip_per_g > 0 else rem_gs

    def _weighted(actual_rate: float, career_rate: float, actual_w: float, rem_w: float) -> float:
        denom = actual_w + rem_w
        if denom <= 0:
            return 0.0
        return (actual_rate * actual_w + career_rate * rem_w) / denom

    blended = dict(line)
    blended["IP"] = actual_ip + remaining_ip
    blended["G"] = actual_g + rem_g
    blended["GS"] = actual_gs + rem_gs
    blended["ERA"] = _weighted(actual_era, career_era, actual_ip, remaining_ip)
    blended["FIP"] = _weighted(actual_fip, career_fip, actual_ip, remaining_ip)
    blended["WHIP"] = _weighted(actual_whip, career_whip, actual_ip, remaining_ip)
    blended["K/9"] = _weighted(actual_k9, career_k9, actual_ip, remaining_ip)
    blended["BB/9"] = _weighted(actual_bb9, career_bb9, actual_ip, remaining_ip)
    blended["HR/9"] = _weighted(actual_hr9, career_hr9, actual_ip, remaining_ip)
    # Counting stats: actual + (career rate × remaining IP or remaining GS/G).
    blended["K"] = (actual_k9 * actual_ip / 9.0) + career_k9 * remaining_ip / 9.0
    blended["BB"] = (actual_bb9 * actual_ip / 9.0) + career_bb9 * remaining_ip / 9.0
    blended["HR"] = (actual_hr9 * actual_ip / 9.0) + career_hr9 * remaining_ip / 9.0
    blended["W"] = actual_w + w_per_gs * rem_gs
    blended["L"] = actual_l + l_per_gs * rem_gs
    blended["QS"] = actual_qs + qs_per_gs * rem_gs
    blended["SV"] = actual_sv + sv_per_g * rem_g
    blended["HLD"] = actual_hld + hld_per_g * rem_g
    return blended


def project_pitcher_lines(
    rows: List[Dict[str, str]],
    player_stats: Optional[Dict[str, Any]],
    team_ip: float,
) -> List[Dict[str, Any]]:
    """Project per-pitcher counting lines for staff arms (SP1-5, CL, SU/MR/LR).

    IP for each pitcher = role_share × team_IP (matches the team-FIP weighting).
    GS / G come from the per-pitcher IP_per_GS / IP_per_G rates so pitchers
    who start get realistic start counts and bullpen arms get appearance counts.

    Each line stashes its sidecar rates dict on ``_rates`` and per-game IP
    share on ``_ip_per_game`` so the post-fact current-standings overlay
    can compute remaining_IP from games_remaining (not full_IP - actual_IP).
    """
    if not player_stats:
        return []
    pitchers = [r for r in rows if _truthy(r.get("is_pitcher"))]
    if not pitchers:
        return []

    role_counts: Dict[str, int] = {}
    for r in pitchers:
        t = (r.get("tier") or "").upper()
        if t.startswith("MR"):
            role_counts["MR"] = role_counts.get("MR", 0) + 1
        elif t.startswith("SU"):
            role_counts["SU"] = role_counts.get("SU", 0) + 1

    out: List[Dict[str, Any]] = []
    for r in pitchers:
        tier = (r.get("tier") or "").strip().upper()
        share = _role_share_lookup(tier, role_counts)
        if share <= 0:
            continue
        rates = _player_rates(player_stats, "pitchers", r.get("pid", ""))
        if not rates:
            continue
        proj_ip = share * team_ip
        ip_per_gs = rates.get("IP_per_GS", 0.0)
        ip_per_g = rates.get("IP_per_G", 0.0)
        is_starter = tier.startswith("SP")
        proj_gs = (proj_ip / ip_per_gs) if (is_starter and ip_per_gs > 0) else 0.0
        proj_g = (proj_ip / ip_per_g) if ip_per_g > 0 else proj_gs
        line = {
            "tier": tier,
            "name": rates.get("name") or r.get("name", ""),
            "IP": proj_ip,
            "G": proj_g,
            "GS": proj_gs,
            "ERA": rates.get("ERA", 0.0),
            "FIP": rates.get("FIP", 0.0),
            "K/9": rates.get("K/9", 0.0),
            "BB/9": rates.get("BB/9", 0.0),
            "HR/9": rates.get("HR/9", 0.0),
            "WHIP": rates.get("WHIP", 0.0),
            "K": rates.get("K/9", 0.0) * proj_ip / 9.0,
            "BB": rates.get("BB/9", 0.0) * proj_ip / 9.0,
            "HR": rates.get("HR/9", 0.0) * proj_ip / 9.0,
            "W": rates.get("W_per_GS", 0.0) * proj_gs,
            "L": rates.get("L_per_GS", 0.0) * proj_gs,
            "QS": rates.get("QS_per_GS", 0.0) * proj_gs,
            "SV": rates.get("SV_per_G", 0.0) * proj_g,
            "HLD": rates.get("HLD_per_G", 0.0) * proj_g,
            # Stashed for the post-fact current-standings overlay.
            "_rates": rates,
            "_ip_per_game": share * 8.95,
        }
        out.append(line)
    # Stable role ordering for the rendered table.
    role_order = {"SP": 0, "CL": 1, "SU": 2, "MR": 3, "LR": 4}
    def _key(p: Dict[str, Any]) -> Tuple[int, str]:
        prefix = next((k for k in role_order if p["tier"].startswith(k)), "ZZ")
        return (role_order.get(prefix, 99), p["tier"])
    out.sort(key=_key)
    return out


# -----------------------------------------------------------------------------
# Render
# -----------------------------------------------------------------------------

def render_md(
    league: str, org: str, level: str,
    constants: Dict[str, Any],
    rs_block: Dict[str, Any],
    ra_block: Dict[str, Any],
    wins: float, losses: float, exponent: float, games: int,
    hitter_lines: Optional[List[Dict[str, Any]]] = None,
    pitcher_lines: Optional[List[Dict[str, Any]]] = None,
) -> str:
    lines: List[str] = []
    lines.append(f"# Season Projection — {org} ({level})  ·  {league.upper()}")
    lines.append("")
    lines.append(f"_Pythagenpat from depth chart roster.  Stats year: {constants.get('year', '?')}._")
    lines.append("")
    lines.append("## Bottom Line")
    lines.append("")
    lines.append(f"**Projected record:** {wins:.0f}-{losses:.0f}  ({wins/games:.3f} win%) over {games} games")
    lines.append("")
    lines.append("## Offense")
    lines.append("")
    lines.append(f"- Team wOBA vs RHP: {rs_block['team_wOBA_vs_R']:.3f}")
    lines.append(f"- Team wOBA vs LHP: {rs_block['team_wOBA_vs_L']:.3f}")
    lines.append(
        f"- Blended team wOBA: {rs_block['team_wOBA']:.3f}  "
        f"(lg avg blended {rs_block.get('lg_wOBA_blend', rs_block['lg_wOBA']):.3f}; "
        f"3-yr {rs_block.get('lg_wOBA_full', 0):.3f}, current {rs_block.get('lg_wOBA_current', 0):.3f})"
    )
    lines.append(
        f"- Current-year wOBA blend weight: {rs_block.get('blend_woba', 0):.2f}  "
        f"(0 = use 3-yr; 1 = use current only)"
    )
    lines.append(f"- Runs scored: **{rs_block['rs']:.0f}**  ({rs_block['rs']/games:.2f}/G)")
    lines.append("")
    lines.append("## Pitching / Defense")
    lines.append("")
    blend = ra_block.get("blend", 0.0)
    lines.append(
        f"- Team FIP: {ra_block['team_FIP']:.2f}  "
        f"(lg avg blended {ra_block.get('lg_FIP_blend', 0):.2f}; "
        f"3-yr {ra_block.get('lg_FIP_full', 0):.2f}, current {ra_block.get('lg_FIP_current', 0):.2f})"
    )
    lines.append(f"- Current-year FIP blend weight: {blend:.2f}  (0 = use 3-yr; 1 = use current only)")
    if ra_block["defense_mult"] != 1.0:
        lines.append(f"- Defense adjustment: ×{ra_block['defense_mult']:.3f}")
    lines.append(f"- Runs allowed: **{ra_block['ra']:.0f}**  ({ra_block['ra']/games:.2f}/G)")
    lines.append("")
    lines.append("## Pythagorean")
    lines.append("")
    lines.append(f"- Run differential: {rs_block['rs'] - ra_block['ra']:+.0f}")
    lines.append(f"- Pythagenpat exponent: {exponent:.2f}")
    lines.append(f"- Expected wins: **{wins:.1f}**")
    lines.append("")

    # Per-player projection tables (only when sidecar data is available).
    if hitter_lines:
        lines.append("## Top Hitters (Projected)")
        lines.append("")
        lines.append("| Slot | Name | Pos | PA | AB | AVG | OBP | SLG | OPS | wOBA | HR | R | RBI | SB | BB% | K% |")
        lines.append("| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |")
        for h in hitter_lines:
            lines.append(
                f"| {h['slot']} | {h['name']} | {h['pos']} | {h['PA']:.0f} | "
                f"{h.get('AB', 0):.0f} | {h['AVG']:.3f} | {h['OBP']:.3f} | "
                f"{h['SLG']:.3f} | {h['OPS']:.3f} | {h['wOBA']:.3f} | "
                f"{h['HR']:.0f} | {h['R']:.0f} | {h['RBI']:.0f} | "
                f"{h['SB']:.0f} | {h['BB%']*100:.1f}% | {h['K%']*100:.1f}% |"
            )
        lines.append("")

    if pitcher_lines:
        lines.append("## Top Pitchers (Projected)")
        lines.append("")
        lines.append("| Role | Name | IP | GS | G | W | L | SV | HLD | QS | ERA | FIP | K/9 | BB/9 | HR/9 | WHIP | K | BB |")
        lines.append("| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |")
        for p in pitcher_lines:
            lines.append(
                f"| {p['tier']} | {p['name']} | {p['IP']:.0f} | "
                f"{p['GS']:.0f} | {p['G']:.0f} | {p['W']:.0f} | {p['L']:.0f} | "
                f"{p['SV']:.0f} | {p['HLD']:.0f} | {p['QS']:.0f} | "
                f"{p['ERA']:.2f} | {p['FIP']:.2f} | {p['K/9']:.1f} | {p['BB/9']:.1f} | "
                f"{p['HR/9']:.2f} | {p['WHIP']:.2f} | {p['K']:.0f} | {p['BB']:.0f} |"
            )
        lines.append("")

    lines.append("## Caveats")
    lines.append("")
    lines.append("- Bullpen FIP weighted only by IP share, not leverage. Closers don't get an LI bonus.")
    lines.append("- No park adjustment in this projection (eval CSV has them but they're not propagated here).")
    lines.append("- No injury attrition, schedule strength, or platoon optimization.")
    lines.append("- League averages computed across the fetched lid pool, which may include a")
    lines.append("  level below — slightly drags lg averages toward the weaker level.")
    lines.append("- Real OOTP outcomes typically vary ±6-10 wins from a static projection like this.")
    return "\n".join(lines)


# -----------------------------------------------------------------------------
# League rollup writer
# -----------------------------------------------------------------------------

def load_divisions(league: str) -> Optional[Dict[str, Dict[str, List[str]]]]:
    """Load config/divisions-{league}.json. Returns None if missing/invalid."""
    path = SCRIPT_DIR / "config" / f"divisions-{league}.json"
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    if not isinstance(raw, dict):
        return None
    cleaned: Dict[str, Dict[str, List[str]]] = {}
    for lg_name, divs in raw.items():
        if lg_name.startswith("_") or not isinstance(divs, dict):
            continue
        per_div: Dict[str, List[str]] = {}
        for div_name, teams in divs.items():
            if div_name.startswith("_") or not isinstance(teams, list):
                continue
            per_div[div_name] = [str(t).strip() for t in teams if str(t).strip()]
        if per_div:
            cleaned[lg_name] = per_div
    return cleaned or None


def _standings_table(rows: List[Dict[str, Any]], use_gb: bool = False) -> List[str]:
    """Render a single standings block. Adds GB column when use_gb is True."""
    if not rows:
        return ["| _no projections_ |  |  |  |  |  |  |"]
    sorted_rows = sorted(rows, key=lambda r: -r["wins"])
    leader_w = sorted_rows[0]["wins"]
    leader_l = sorted_rows[0]["losses"]
    out: List[str] = []
    if use_gb:
        out.append("| Rank | Org | W-L | Win% | GB | RS | RA | RD | Pythag exp |")
        out.append("| --- | --- | --- | --- | --- | --- | --- | --- | --- |")
    else:
        out.append("| Rank | Org | W-L | Win% | RS | RA | RD | Pythag exp |")
        out.append("| --- | --- | --- | --- | --- | --- | --- | --- |")
    for i, r in enumerate(sorted_rows, start=1):
        gb = ((leader_w - r["wins"]) + (r["losses"] - leader_l)) / 2.0
        # Round GB to the nearest half-game (standings convention).
        gb = round(gb * 2) / 2
        gb_str = "—" if i == 1 else f"{gb:.1f}"
        if use_gb:
            out.append(
                f"| {i} | {r['org']} | {r['wins']:.0f}-{r['losses']:.0f} | "
                f"{r['wins']/r['games']:.3f} | {gb_str} | {r['rs']:.0f} | {r['ra']:.0f} | "
                f"{r['rs']-r['ra']:+.0f} | {r['exponent']:.2f} |"
            )
        else:
            out.append(
                f"| {i} | {r['org']} | {r['wins']:.0f}-{r['losses']:.0f} | "
                f"{r['wins']/r['games']:.3f} | {r['rs']:.0f} | {r['ra']:.0f} | "
                f"{r['rs']-r['ra']:+.0f} | {r['exponent']:.2f} |"
            )
    return out


def _calibration_line(rows: List[Dict[str, Any]]) -> str:
    total_w = sum(r["wins"] for r in rows)
    total_games = sum(r["games"] for r in rows)
    expected_w = total_games / 2.0
    delta = total_w - expected_w
    return (
        f"_Calibration: total projected wins = {total_w:.1f}, "
        f"expected = {expected_w:.1f} (Δ {delta:+.1f})._"
    )


# -----------------------------------------------------------------------------
# League leaders (top-N by stat across all teams at a level)
# -----------------------------------------------------------------------------

def _flatten_player_lines(
    level_rows: List[Dict[str, Any]],
    key: str,
) -> List[Dict[str, Any]]:
    """Flatten per-team _hitter_lines / _pitcher_lines into one list with the
    team name attached so league leaders can be sorted across the level."""
    out: List[Dict[str, Any]] = []
    for rec in level_rows:
        team = rec.get("org", "")
        for p in (rec.get(key) or []):
            row = dict(p)
            row["_team"] = team
            out.append(row)
    return out


def _stamp_qualifying_pa(rows: List[Dict[str, Any]]) -> bool:
    """Stamp ``_qual_pa`` on each hitter row.

    Mid-season (any player has actual current-year PA at the target level):
        ``_qual_pa = PA_current``. Players with 0 PA_current — including any
        prospect whose career rates are pulled in from a lower level — fall
        out of leader-board consideration since 0 < any positive threshold.
    Preseason (no actuals anywhere): ``_qual_pa = projected PA`` so leader
        boards still work for offseason / spring training projections.

    Returns True if mid-season mode is in effect.
    """
    has_actuals = any(
        float((r.get("_rates") or {}).get("PA_current", 0.0) or 0.0) > 0
        for r in rows
    )
    for r in rows:
        if has_actuals:
            r["_qual_pa"] = float((r.get("_rates") or {}).get("PA_current", 0.0) or 0.0)
        else:
            r["_qual_pa"] = float(r.get("PA", 0.0) or 0.0)
    return has_actuals


def _stamp_qualifying_ip(rows: List[Dict[str, Any]]) -> bool:
    """Symmetric helper for pitchers, keyed on IP_current."""
    has_actuals = any(
        float((r.get("_rates") or {}).get("IP_current", 0.0) or 0.0) > 0
        for r in rows
    )
    for r in rows:
        if has_actuals:
            r["_qual_ip"] = float((r.get("_rates") or {}).get("IP_current", 0.0) or 0.0)
        else:
            r["_qual_ip"] = float(r.get("IP", 0.0) or 0.0)
    return has_actuals


def _leader_table(
    rows: List[Dict[str, Any]],
    title: str,
    stat_key: str,
    stat_fmt: str,
    qual_attr: str,
    qual_label: str,
    qual_min: float = 0.0,
    descending: bool = True,
    top_n: int = 10,
    qual_note: Optional[str] = None,
) -> List[str]:
    """Render one top-N leaderboard table.

    ``qual_attr`` is the line key to read for qualification (e.g. "_qual_pa").
    ``qual_label`` is the column header text.
    ``qual_note`` overrides the auto-generated parenthetical (defaults to
    "(min N PA-to-date)"). Pass a per-game rate string here to show
    "(min 3.1 PA per game)" instead.
    Rows below ``qual_min`` are filtered out.
    descending=True sorts highest-first; False (lower-is-better) sorts ascending.
    """
    eligible = [r for r in rows if r.get(qual_attr, 0) >= qual_min] if qual_min > 0 else rows
    if not eligible:
        return []
    sorted_rows = sorted(
        eligible,
        key=lambda r: r.get(stat_key, 0.0),
        reverse=descending,
    )[:top_n]
    if not sorted_rows:
        return []
    if qual_note is None:
        qual_note = f"  (min {qual_min:.0f} {qual_label})" if qual_min > 0 else ""
    out: List[str] = []
    out.append(f"**{title}**{qual_note}")
    out.append("")
    out.append(f"| Rank | Player | Team | {stat_key} | {qual_label} |")
    out.append("| --- | --- | --- | --- | --- |")
    for i, r in enumerate(sorted_rows, start=1):
        out.append(
            f"| {i} | {r.get('name', '')} | {r.get('_team', '')} | "
            f"{stat_fmt.format(r.get(stat_key, 0.0))} | {r.get(qual_attr, 0):.0f} |"
        )
    out.append("")
    return out


def _hitter_leader_table(
    rows: List[Dict[str, Any]],
    title: str,
    stat_key: str,
    qual_min: float = 0.0,
    qual_label: str = "PA",
    descending: bool = True,
    top_n: int = 10,
    qual_note: Optional[str] = None,
) -> List[str]:
    """Render a hitter leader board with a full slash-line stat row per player.

    Filters by ``_qual_pa`` (stamped by _stamp_qualifying_pa) so mid-season
    runs require actual current-year PA at the target level.
    """
    eligible = [r for r in rows if r.get("_qual_pa", 0.0) >= qual_min] if qual_min > 0 else rows
    if not eligible:
        return []
    sorted_rows = sorted(eligible, key=lambda r: r.get(stat_key, 0.0), reverse=descending)[:top_n]
    if not sorted_rows:
        return []
    if qual_note is None:
        qual_note = f"  (min {qual_min:.0f} {qual_label})" if qual_min > 0 else ""
    out: List[str] = []
    out.append(f"**{title}**{qual_note}")
    out.append("")
    out.append("| Rank | Player | Team | AB | AVG | OBP | SLG | HR | RBI | SB | PA |")
    out.append("| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |")
    for i, r in enumerate(sorted_rows, start=1):
        out.append(
            f"| {i} | {r.get('name', '')} | {r.get('_team', '')} | "
            f"{r.get('AB', 0.0):.0f} | {r.get('AVG', 0.0):.3f} | "
            f"{r.get('OBP', 0.0):.3f} | {r.get('SLG', 0.0):.3f} | "
            f"{r.get('HR', 0.0):.0f} | {r.get('RBI', 0.0):.0f} | "
            f"{r.get('SB', 0.0):.0f} | {r.get('PA', 0.0):.0f} |"
        )
    out.append("")
    return out


# Comprehensive scoring categories. The sign indicates whether higher is
# better (+1) or lower is better (-1). z-scores get sign-flipped before
# summing so all categories pull in the same direction.
HITTER_SCORE_CATEGORIES: List[Tuple[str, int]] = [
    ("HR", +1), ("RBI", +1), ("R", +1), ("SB", +1),
    ("AVG", +1), ("OBP", +1), ("SLG", +1), ("OPS", +1), ("wOBA", +1),
]
STARTER_SCORE_CATEGORIES: List[Tuple[str, int]] = [
    ("W", +1), ("K", +1), ("QS", +1),
    ("ERA", -1), ("FIP", -1), ("WHIP", -1), ("K/9", +1),
]
RELIEVER_SCORE_CATEGORIES: List[Tuple[str, int]] = [
    ("SV", +1), ("HLD", +1), ("K", +1),
    ("ERA", -1), ("FIP", -1), ("WHIP", -1), ("K/9", +1),
]
# Kept for backwards compatibility / generic use (e.g. preseason composite).
PITCHER_SCORE_CATEGORIES: List[Tuple[str, int]] = [
    ("W", +1), ("K", +1), ("SV", +1), ("QS", +1),
    ("ERA", -1), ("FIP", -1), ("WHIP", -1), ("K/9", +1),
]


def _is_starter_tier(tier: str) -> bool:
    return (tier or "").upper().startswith("SP")


def _stamp_z_scores(
    rows: List[Dict[str, Any]],
    categories: List[Tuple[str, int]],
) -> None:
    """For each category, compute z-score across `rows` and stamp `_z_<key>`.
    Also stamps `_score` = average signed z-score across categories that
    actually had data (categories with zero variance are skipped from both
    numerator and denominator).

    Average rather than sum makes the number interpretable on its own:
    +1.9 means "roughly 1.9 standard deviations above league average across
    these categories." Sort order is identical to the sum-of-z version
    (constant divisor when all categories contribute), so rankings don't
    change relative to the prior implementation.
    """
    if not rows:
        return
    means_stds: Dict[str, Tuple[float, float]] = {}
    for key, _sign in categories:
        vals = [float(r.get(key, 0.0) or 0.0) for r in rows]
        n = len(vals)
        if n == 0:
            means_stds[key] = (0.0, 0.0)
            continue
        mean = sum(vals) / n
        var = sum((v - mean) ** 2 for v in vals) / max(1, n - 1)
        means_stds[key] = (mean, var ** 0.5)

    for r in rows:
        total = 0.0
        present = 0
        for key, sign in categories:
            mean, std = means_stds[key]
            if std <= 0:
                r[f"_z_{key}"] = 0.0
                continue
            z = (float(r.get(key, 0.0) or 0.0) - mean) / std
            r[f"_z_{key}"] = z
            total += sign * z
            present += 1
        r["_score"] = (total / present) if present > 0 else 0.0


def _comprehensive_hitter_table(
    rows: List[Dict[str, Any]],
    qual_min: float,
    qual_label: str,
    top_n: int = 10,
    qual_note: Optional[str] = None,
) -> List[str]:
    """Top-N hitters by composite z-score-sum across all categories."""
    eligible = [r for r in rows if r.get("_qual_pa", 0.0) >= qual_min] if qual_min > 0 else list(rows)
    if not eligible:
        return []
    _stamp_z_scores(eligible, HITTER_SCORE_CATEGORIES)
    sorted_rows = sorted(eligible, key=lambda r: -r.get("_score", 0.0))[:top_n]
    if not sorted_rows:
        return []
    if qual_note is None:
        qual_note = f"  (min {qual_min:.0f} {qual_label})" if qual_min > 0 else ""
    out: List[str] = [
        f"**Top {top_n} Hitters — Comprehensive Score**{qual_note}",
        "",
        "_Score = average z-score across HR, RBI, R, SB, AVG, OBP, SLG, OPS, wOBA. "
        "+1.00 = roughly one full standard deviation above league average across the board. "
        "+2.0 territory is elite; 0 is league average._",
        "",
        "| Rank | Player | Team | AB | AVG | OBP | SLG | HR | RBI | R | SB | wOBA | Score |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for i, r in enumerate(sorted_rows, start=1):
        out.append(
            f"| {i} | {r.get('name', '')} | {r.get('_team', '')} | "
            f"{r.get('AB', 0.0):.0f} | "
            f"{r.get('AVG', 0.0):.3f} | {r.get('OBP', 0.0):.3f} | {r.get('SLG', 0.0):.3f} | "
            f"{r.get('HR', 0.0):.0f} | {r.get('RBI', 0.0):.0f} | "
            f"{r.get('R', 0.0):.0f} | {r.get('SB', 0.0):.0f} | "
            f"{r.get('wOBA', 0.0):.3f} | {r.get('_score', 0.0):+.2f} |"
        )
    out.append("")
    return out


def _comprehensive_starter_table(
    rows: List[Dict[str, Any]],
    qual_min: float,
    qual_label: str,
    top_n: int = 10,
    qual_note: Optional[str] = None,
) -> List[str]:
    """Top-N starting pitchers by composite z-score-sum.
    No SV/HLD column (rotation roles don't earn those)."""
    eligible = [r for r in rows if r.get("_qual_ip", 0.0) >= qual_min] if qual_min > 0 else list(rows)
    if not eligible:
        return []
    _stamp_z_scores(eligible, STARTER_SCORE_CATEGORIES)
    sorted_rows = sorted(eligible, key=lambda r: -r.get("_score", 0.0))[:top_n]
    if not sorted_rows:
        return []
    if qual_note is None:
        qual_note = f"  (min {qual_min:.0f} {qual_label})" if qual_min > 0 else ""
    out: List[str] = [
        f"**Top {top_n} Starters — Comprehensive Score**{qual_note}",
        "",
        "_Score = average z-score across W, K, QS, ERA (inverted), FIP (inverted), "
        "WHIP (inverted), K/9. +1.00 ≈ one std dev above league average across the board; "
        "+2.0 is elite._",
        "",
        "| Rank | Player | Team | IP | GS | W | K | QS | ERA | FIP | WHIP | K/9 | Score |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for i, r in enumerate(sorted_rows, start=1):
        out.append(
            f"| {i} | {r.get('name', '')} | {r.get('_team', '')} | "
            f"{r.get('IP', 0.0):.0f} | {r.get('GS', 0.0):.0f} | "
            f"{r.get('W', 0.0):.0f} | {r.get('K', 0.0):.0f} | "
            f"{r.get('QS', 0.0):.0f} | "
            f"{r.get('ERA', 0.0):.2f} | {r.get('FIP', 0.0):.2f} | "
            f"{r.get('WHIP', 0.0):.2f} | {r.get('K/9', 0.0):.1f} | "
            f"{r.get('_score', 0.0):+.2f} |"
        )
    out.append("")
    return out


def _comprehensive_reliever_table(
    rows: List[Dict[str, Any]],
    qual_min: float,
    qual_label: str,
    top_n: int = 10,
    qual_note: Optional[str] = None,
) -> List[str]:
    """Top-N relievers by composite z-score-sum.
    Uses bullpen-specific stats (SV/HLD) and excludes W/QS (rotation-flavored)."""
    eligible = [r for r in rows if r.get("_qual_ip", 0.0) >= qual_min] if qual_min > 0 else list(rows)
    if not eligible:
        return []
    _stamp_z_scores(eligible, RELIEVER_SCORE_CATEGORIES)
    sorted_rows = sorted(eligible, key=lambda r: -r.get("_score", 0.0))[:top_n]
    if not sorted_rows:
        return []
    if qual_note is None:
        qual_note = f"  (min {qual_min:.0f} {qual_label})" if qual_min > 0 else ""
    out: List[str] = [
        f"**Top {top_n} Relievers — Comprehensive Score**{qual_note}",
        "",
        "_Score = average z-score across SV, HLD, K, ERA (inverted), FIP (inverted), "
        "WHIP (inverted), K/9. +1.00 ≈ one std dev above league average across the board; "
        "+2.0 is elite._",
        "",
        "| Rank | Player | Team | IP | G | SV | HLD | K | ERA | FIP | WHIP | K/9 | Score |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for i, r in enumerate(sorted_rows, start=1):
        out.append(
            f"| {i} | {r.get('name', '')} | {r.get('_team', '')} | "
            f"{r.get('IP', 0.0):.0f} | {r.get('G', 0.0):.0f} | "
            f"{r.get('SV', 0.0):.0f} | {r.get('HLD', 0.0):.0f} | "
            f"{r.get('K', 0.0):.0f} | "
            f"{r.get('ERA', 0.0):.2f} | {r.get('FIP', 0.0):.2f} | "
            f"{r.get('WHIP', 0.0):.2f} | {r.get('K/9', 0.0):.1f} | "
            f"{r.get('_score', 0.0):+.2f} |"
        )
    out.append("")
    return out


def _level_games_played(level_rows: List[Dict[str, Any]]) -> float:
    """Return the typical games-played figure across teams at this level.

    Mid-season runs (with --use-current-standings) have ``games_played`` on
    each rec. Resolution priority:

      1. **Teams with actual standings folded in** — use the median of their
         games_played values. Median is more robust than max because a single
         team that *failed* to match standings (and stayed at full-season
         games) would otherwise blow the threshold up to 162 for everyone.
         OOTP schedules tend to have all teams within ±1 game of each other
         on any given sim day, so median ≈ typical.
      2. **Player PA_current data** — if no team has actual standings but the
         player_stats sidecar carries PA_current values, derive games_played
         from the maximum PA_current divided by ~4.7 PA per game (typical
         high-leverage starter rate).
      3. **Full season fallback** — preseason / no actuals anywhere. Use the
         schedule length so the threshold equals the conventional 502 PA
         qualified cutoff.
    """
    # Path 1: teams with current standings overlay
    gp_with_standings = [
        float(r.get("games_played", 0) or 0)
        for r in level_rows
        if r.get("uses_current_standings") and (r.get("games_played", 0) or 0) > 0
    ]
    if gp_with_standings:
        gp_with_standings.sort()
        return gp_with_standings[len(gp_with_standings) // 2]

    # Path 2: derive from player PA_current data
    max_pa_current = 0.0
    for rec in level_rows:
        for line in (rec.get("_hitter_lines") or []):
            pa_cur = float((line.get("_rates") or {}).get("PA_current", 0.0) or 0.0)
            if pa_cur > max_pa_current:
                max_pa_current = pa_cur
    if max_pa_current > 0:
        # ~4.7 PA per game for a leadoff/full-time starter.
        return max_pa_current / 4.7

    # Path 3: preseason fallback — schedule length
    full_season = max((float(r.get("games", 0) or 0) for r in level_rows), default=0.0)
    return full_season


def render_league_leaders(
    level_rows: List[Dict[str, Any]],
    top_n: int = 10,
    qual_pa_per_game: float = 3.1,
    qual_ip_per_game: float = 1.0,
    qual_reliever_ip_per_game: float = 0.3,
    qual_counting_fraction: float = 0.25,
    mode: str = "comprehensive",
) -> List[str]:
    """Render hitter + pitcher league leaders for one level. Returns a list
    of MD lines. Empty when no per-player projection data is available.

    Qualifying thresholds use the standard MLB convention (3.1 PA per team
    game played for hitters, 1.0 IP/G for pitchers). Mid-season the
    threshold scales with games played; preseason it equals the full-season
    qualified cutoff.

    ``mode``:
      - "comprehensive": single z-score-sum table per side (default).
      - "individual": one table per category (legacy verbose output).
      - "both": comprehensive table first, then individual tables.
    """
    hitters = _flatten_player_lines(level_rows, "_hitter_lines")
    pitchers = _flatten_player_lines(level_rows, "_pitcher_lines")
    if not hitters and not pitchers:
        return []

    games_played = _level_games_played(level_rows)
    out: List[str] = []

    if hitters:
        mid_season = _stamp_qualifying_pa(hitters)
        pa_qual = round(qual_pa_per_game * games_played) if games_played > 0 else 0
        pa_qual_counting = round(qual_counting_fraction * pa_qual) if mid_season else 0
        qual_label = "PA-to-date" if mid_season else "PA"
        # Rate-based parenthetical notes (replace the absolute-threshold display).
        h_rate_note = f"  (min {qual_pa_per_game:.1f} PA per game)" if pa_qual > 0 else ""
        h_rate_note_counting = (
            f"  (min {qual_pa_per_game * qual_counting_fraction:.2f} PA per game)"
            if pa_qual_counting > 0 else ""
        )

        out.append("### Hitter Leaders")
        out.append("")

        if mode in ("comprehensive", "both"):
            out.extend(_comprehensive_hitter_table(hitters, pa_qual, qual_label, top_n, qual_note=h_rate_note))

        if mode in ("individual", "both"):
            out.extend(_hitter_leader_table(hitters, f"Top {top_n} HR", "HR", pa_qual_counting, qual_label, qual_note=h_rate_note_counting))
            out.extend(_hitter_leader_table(hitters, f"Top {top_n} RBI", "RBI", pa_qual_counting, qual_label, qual_note=h_rate_note_counting))
            out.extend(_hitter_leader_table(hitters, f"Top {top_n} R", "R", pa_qual_counting, qual_label, qual_note=h_rate_note_counting))
            out.extend(_hitter_leader_table(hitters, f"Top {top_n} SB", "SB", pa_qual_counting, qual_label, qual_note=h_rate_note_counting))
            out.extend(_hitter_leader_table(hitters, f"Top {top_n} AVG", "AVG", pa_qual, qual_label, qual_note=h_rate_note))
            out.extend(_hitter_leader_table(hitters, f"Top {top_n} OBP", "OBP", pa_qual, qual_label, qual_note=h_rate_note))
            out.extend(_hitter_leader_table(hitters, f"Top {top_n} SLG", "SLG", pa_qual, qual_label, qual_note=h_rate_note))
            out.extend(_hitter_leader_table(hitters, f"Top {top_n} OPS", "OPS", pa_qual, qual_label, qual_note=h_rate_note))
            out.extend(_hitter_leader_table(hitters, f"Top {top_n} wOBA", "wOBA", pa_qual, qual_label, qual_note=h_rate_note))

    if pitchers:
        mid_season_p = _stamp_qualifying_ip(pitchers)
        # Split by tier: SP1-SPN vs everyone else (CL, SU, MR, LR).
        starters = [p for p in pitchers if _is_starter_tier(p.get("tier", ""))]
        relievers = [p for p in pitchers if not _is_starter_tier(p.get("tier", ""))]

        sp_qual = round(qual_ip_per_game * games_played) if games_played > 0 else 0
        rp_qual = round(qual_reliever_ip_per_game * games_played) if games_played > 0 else 0
        sp_qual_counting = round(qual_counting_fraction * sp_qual) if mid_season_p else 0
        rp_qual_counting = round(qual_counting_fraction * rp_qual) if mid_season_p else 0
        ip_label = "IP-to-date" if mid_season_p else "IP"
        # Rate-based parenthetical notes.
        sp_rate_note = f"  (min {qual_ip_per_game:.1f} IP per game)" if sp_qual > 0 else ""
        sp_rate_note_counting = (
            f"  (min {qual_ip_per_game * qual_counting_fraction:.2f} IP per game)"
            if sp_qual_counting > 0 else ""
        )
        rp_rate_note = f"  (min {qual_reliever_ip_per_game:.1f} IP per game)" if rp_qual > 0 else ""
        rp_rate_note_counting = (
            f"  (min {qual_reliever_ip_per_game * qual_counting_fraction:.2f} IP per game)"
            if rp_qual_counting > 0 else ""
        )

        if starters:
            out.append("### Starting Pitcher Leaders")
            out.append("")

            if mode in ("comprehensive", "both"):
                out.extend(_comprehensive_starter_table(starters, sp_qual, ip_label, top_n, qual_note=sp_rate_note))

            if mode in ("individual", "both"):
                out.extend(_leader_table(starters, f"Top {top_n} W", "W", "{:.0f}", "_qual_ip", ip_label, sp_qual_counting, qual_note=sp_rate_note_counting))
                out.extend(_leader_table(starters, f"Top {top_n} K", "K", "{:.0f}", "_qual_ip", ip_label, sp_qual_counting, qual_note=sp_rate_note_counting))
                out.extend(_leader_table(starters, f"Top {top_n} QS", "QS", "{:.0f}", "_qual_ip", ip_label, sp_qual_counting, qual_note=sp_rate_note_counting))
                out.extend(_leader_table(starters, f"Top {top_n} ERA", "ERA", "{:.2f}", "_qual_ip", ip_label, sp_qual, descending=False, qual_note=sp_rate_note))
                out.extend(_leader_table(starters, f"Top {top_n} FIP", "FIP", "{:.2f}", "_qual_ip", ip_label, sp_qual, descending=False, qual_note=sp_rate_note))
                out.extend(_leader_table(starters, f"Top {top_n} WHIP", "WHIP", "{:.2f}", "_qual_ip", ip_label, sp_qual, descending=False, qual_note=sp_rate_note))
                out.extend(_leader_table(starters, f"Top {top_n} K/9", "K/9", "{:.1f}", "_qual_ip", ip_label, sp_qual, qual_note=sp_rate_note))

        if relievers:
            out.append("### Reliever Leaders")
            out.append("")

            if mode in ("comprehensive", "both"):
                out.extend(_comprehensive_reliever_table(relievers, rp_qual, ip_label, top_n, qual_note=rp_rate_note))

            if mode in ("individual", "both"):
                out.extend(_leader_table(relievers, f"Top {top_n} SV", "SV", "{:.0f}", "_qual_ip", ip_label, rp_qual_counting, qual_note=rp_rate_note_counting))
                out.extend(_leader_table(relievers, f"Top {top_n} HLD", "HLD", "{:.0f}", "_qual_ip", ip_label, rp_qual_counting, qual_note=rp_rate_note_counting))
                out.extend(_leader_table(relievers, f"Top {top_n} K", "K", "{:.0f}", "_qual_ip", ip_label, rp_qual_counting, qual_note=rp_rate_note_counting))
                out.extend(_leader_table(relievers, f"Top {top_n} ERA", "ERA", "{:.2f}", "_qual_ip", ip_label, rp_qual, descending=False, qual_note=rp_rate_note))
                out.extend(_leader_table(relievers, f"Top {top_n} FIP", "FIP", "{:.2f}", "_qual_ip", ip_label, rp_qual, descending=False, qual_note=rp_rate_note))
                out.extend(_leader_table(relievers, f"Top {top_n} WHIP", "WHIP", "{:.2f}", "_qual_ip", ip_label, rp_qual, descending=False, qual_note=rp_rate_note))
                out.extend(_leader_table(relievers, f"Top {top_n} K/9", "K/9", "{:.1f}", "_qual_ip", ip_label, rp_qual, qual_note=rp_rate_note))

    return out


def render_league_summary(
    league: str,
    year: int,
    rows: List[Dict[str, Any]],
    qual_pa_per_game: float = 3.1,
    qual_ip_per_game: float = 1.0,
    qual_reliever_ip_per_game: float = 0.3,
    qual_counting_fraction: float = 0.25,
    leader_mode: str = "comprehensive",
) -> str:
    """Render the rollup MD. If config/divisions-{league}.json exists, group
    each level's standings by league → division before falling back to a
    flat overall standings block."""
    out: List[str] = []
    out.append(f"# League Projection — {league.upper()}  ·  {year}")
    out.append("")
    out.append(f"_Generated {datetime.now().strftime('%Y-%m-%d %H:%M')}._")
    out.append("")

    divisions = load_divisions(league)

    # Group rows by level.
    by_level: Dict[str, List[Dict[str, Any]]] = {}
    for r in rows:
        by_level.setdefault(r["level"], []).append(r)

    level_order = ("ML", "AAA", "AA", "A+", "A", "A-", "R")
    sorted_levels = sorted(
        by_level.keys(),
        key=lambda s: level_order.index(s) if s in level_order else 99,
    )

    for level in sorted_levels:
        level_rows = by_level[level]
        out.append(f"## {level} Projected Standings")
        out.append("")

        if divisions:
            # Build org → row lookup for fast division grouping.
            row_by_org = {r["org"]: r for r in level_rows}
            unmatched_orgs = set(row_by_org.keys())

            for lg_name, divs in divisions.items():
                lg_rows: List[Dict[str, Any]] = []
                out.append(f"### {lg_name}")
                out.append("")
                for div_name, teams in divs.items():
                    div_rows = [row_by_org[t] for t in teams if t in row_by_org]
                    if not div_rows:
                        continue
                    out.append(f"**{lg_name} — {div_name}**")
                    out.append("")
                    out.extend(_standings_table(div_rows, use_gb=True))
                    out.append("")
                    lg_rows.extend(div_rows)
                    for t in teams:
                        unmatched_orgs.discard(t)

                # Per-league overall standings (sorted across that league's divisions).
                if lg_rows:
                    out.append(f"**{lg_name} — Overall**")
                    out.append("")
                    out.extend(_standings_table(lg_rows, use_gb=False))
                    out.append("")
                    out.append(_calibration_line(lg_rows))
                    out.append("")

            if unmatched_orgs:
                # Orgs the divisions file didn't cover — render flat at the bottom.
                stragglers = [row_by_org[o] for o in unmatched_orgs]
                out.append("### Unassigned (not in divisions file)")
                out.append("")
                out.extend(_standings_table(stragglers, use_gb=False))
                out.append("")
        else:
            # No divisions file — flat standings (legacy rendering).
            out.extend(_standings_table(level_rows, use_gb=False))
            out.append("")
            out.append(_calibration_line(level_rows))
            out.append("")

        # League leaders (top-N hitters + pitchers across all teams at this
        # level). No-op when player_stats sidecars weren't produced upstream.
        leader_lines = render_league_leaders(
            level_rows,
            qual_pa_per_game=qual_pa_per_game,
            qual_ip_per_game=qual_ip_per_game,
            qual_reliever_ip_per_game=qual_reliever_ip_per_game,
            qual_counting_fraction=qual_counting_fraction,
            mode=leader_mode,
        )
        if leader_lines:
            out.append(f"## {level} League Leaders")
            out.append("")
            out.extend(leader_lines)

    return "\n".join(out)


# -----------------------------------------------------------------------------
# Per-projection helper
# -----------------------------------------------------------------------------

def compute_one_projection(
    args: argparse.Namespace,
    league: str,
    org: str,
    level: str,
    csv_override: Optional[Path] = None,
    const_override: Optional[Path] = None,
    org_code: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Compute one (org, level) projection. Returns a record with everything
    main() needs to (a) optionally rebalance and (b) render the MD later.
    Does NOT write any files. Returns None on missing inputs."""
    if csv_override and const_override:
        csv_path, const_path = csv_override, const_override
    elif csv_override:
        csv_path = csv_override
        const_path = csv_path.with_name(csv_path.stem + "_constants.json")
    else:
        try:
            csv_path, const_path = find_latest_pair(league, org, level, org_code=org_code)
        except FileNotFoundError as e:
            logger.warning("Skipping %s @ %s: %s", org, level, e)
            return None

    logger.info("Roster CSV: %s", csv_path)
    logger.info("Constants:  %s", const_path)

    rows = load_csv(csv_path)
    constants = json.loads(const_path.read_text(encoding="utf-8"))

    # Optional per-player rate sidecar produced by depth_chart.py. When present,
    # it powers the Top Hitters / Top Pitchers projection tables.
    player_stats: Optional[Dict[str, Any]] = None
    player_stats_path = csv_path.with_name(csv_path.stem + "_player_stats.json")
    if player_stats_path.exists():
        try:
            player_stats = json.loads(player_stats_path.read_text(encoding="utf-8"))
            logger.info("Player stats: %s", player_stats_path)
        except json.JSONDecodeError as e:
            logger.warning("Could not parse %s: %s", player_stats_path, e)
            player_stats = None

    games = args.games or DEFAULT_GAMES_BY_LEVEL.get(level.upper(), 162)
    use_def = not args.no_defense_shade
    def_strength = 0.0 if args.no_defense_shade else float(args.defense_shade_strength)

    rs_block = project_runs_scored(
        rows, constants, games, args.vs_r_share,
        blend_current_woba=float(args.blend_current_woba),
    )
    ra_block = project_runs_allowed(
        rows, constants, games,
        use_defense=use_def,
        blend_current_fip=float(args.blend_current_fip),
        defense_shade_strength=def_strength,
    )
    wins, x = pythagenpat_wins(rs_block["rs"], ra_block["ra"], games)
    losses = games - wins

    # Lines are pure full-season projections here. apply_current_standings
    # (when --use-current-standings is set) layers in actual current-year
    # stats once games_played is known, so remaining_PA / remaining_IP can
    # be tied to remaining games instead of "full_proj_PA - actual_PA".
    hitter_lines = project_hitter_lines(
        rows, player_stats, rs_block.get("team_pa", 38.4 * games), args.vs_r_share,
    )
    pitcher_lines = project_pitcher_lines(
        rows, player_stats, ra_block.get("team_ip", 8.95 * games),
    )

    return {
        "org": org, "level": level,
        "org_code": (org_code or "").strip().lower() or None,
        "wins": wins, "losses": losses,
        "rs": rs_block["rs"], "ra": ra_block["ra"],
        "games": games, "exponent": x,
        "year": constants.get("year", ""),
        # Stash everything needed to re-render after a rebalance pass.
        "_rs_block": rs_block,
        "_ra_block": ra_block,
        "_constants": constants,
        "_hitter_lines": hitter_lines,
        "_pitcher_lines": pitcher_lines,
    }


def rebalance_to_roster_baseline(
    rows: List[Dict[str, Any]],
    note_lines: Optional[Dict[str, List[str]]] = None,
) -> List[Dict[str, Any]]:
    """Roster-realistic recalibration.

    The depth chart picks each team's *best* 9 hitters and *best* 13 pitchers,
    but the original lg_wOBA / lg_FIP constants come from the entire fetched
    pool (including bench, AAA fill-ins, etc.). So every team's wOBA beats
    the league average and every team's FIP undercuts it — leading to nearly
    every team projecting positive RD, which is mathematically impossible.

    This pass re-anchors each level's league baseline to the actual
    distribution of optimized-roster team_wOBAs and team_FIPs. Equivalently:
    shift each team's RS and RA so league-wide sum(RS) = sum(RA), preserving
    each team's offensive/defensive deltas relative to the league.

    Returns new summary rows with adjusted RS/RA/wins/losses. The original
    pre-rebalance values are stashed under ``rs_original`` / ``ra_original``
    / ``wins_original`` for reference. Per-level shift amounts (what was
    added to RS and RA across all teams in that level) are written to
    ``note_lines[level]`` so the renderers can surface the correction.
    """
    by_level: Dict[str, List[Dict[str, Any]]] = {}
    for r in rows:
        by_level.setdefault(r["level"], []).append(r)

    out: List[Dict[str, Any]] = []
    for level, level_rows in by_level.items():
        if len(level_rows) < 2:
            # Need at least 2 teams to define a "league" baseline.
            out.extend(level_rows)
            continue

        n = len(level_rows)
        sum_rs = sum(r["rs"] for r in level_rows)
        sum_ra = sum(r["ra"] for r in level_rows)
        target = (sum_rs + sum_ra) / 2.0
        shift_rs = (target - sum_rs) / n
        shift_ra = (target - sum_ra) / n

        if note_lines is not None:
            note_lines.setdefault(level, []).append(
                f"Roster-realistic recalibration applied: each team's RS shifted "
                f"{shift_rs:+.1f}, RA shifted {shift_ra:+.1f}. League baseline "
                f"now anchored to optimized-roster averages so sum(RS) = sum(RA)."
            )

        for r in level_rows:
            new_rs = max(0.0, r["rs"] + shift_rs)
            new_ra = max(0.0, r["ra"] + shift_ra)
            new_w, new_x = pythagenpat_wins(new_rs, new_ra, r["games"])
            adjusted = dict(r)
            adjusted["rs_original"] = r["rs"]
            adjusted["ra_original"] = r["ra"]
            adjusted["wins_original"] = r["wins"]
            adjusted["rs"] = new_rs
            adjusted["ra"] = new_ra
            adjusted["wins"] = new_w
            adjusted["losses"] = r["games"] - new_w
            adjusted["exponent"] = new_x
            adjusted["rebalanced"] = True
            adjusted["shift_rs"] = shift_rs
            adjusted["shift_ra"] = shift_ra
            # Mutate the stashed blocks so re-rendering the MD picks up the new totals.
            if "_rs_block" in adjusted:
                adjusted["_rs_block"] = dict(adjusted["_rs_block"])
                adjusted["_rs_block"]["rs"] = new_rs
            if "_ra_block" in adjusted:
                adjusted["_ra_block"] = dict(adjusted["_ra_block"])
                adjusted["_ra_block"]["ra"] = new_ra
            out.append(adjusted)

    return out


def _overlay_player_lines(rec: Dict[str, Any], games_remaining: float) -> None:
    """Blend each per-player line in ``rec`` with current-year actuals.

    Called from apply_current_standings once games_remaining is known. Uses
    the per-game lineup share / role share stashed on the line at projection
    time, so remaining_PA and remaining_IP scale with the games left rather
    than being inflated by the player's actual-vs-projected PA gap.
    """
    games_remaining = max(0.0, float(games_remaining))
    for line in rec.get("_hitter_lines") or []:
        rates = line.get("_rates") or {}
        if not rates:
            continue
        remaining_pa = float(line.get("_pa_per_game", 0.0) or 0.0) * games_remaining
        blended = _blend_hitter_line(line, rates, remaining_pa)
        line.update(blended)
    for line in rec.get("_pitcher_lines") or []:
        rates = line.get("_rates") or {}
        if not rates:
            continue
        remaining_ip = float(line.get("_ip_per_game", 0.0) or 0.0) * games_remaining
        blended = _blend_pitcher_line(line, rates, remaining_ip)
        line.update(blended)


def apply_current_standings(
    rows: List[Dict[str, Any]],
    standings: Dict[str, Dict[str, float]],
) -> List[Dict[str, Any]]:
    """Split each team's full-season projection into actual + remaining.

    For each team:
      - Pull actual W/L/RS/RA/GP from `standings`.
      - Convert the rebalanced full-season projection to per-game rates.
      - Apply rates to (season_length - GP) to get projected remaining values.
      - Re-Pythagenpat over the remaining segment for projected remaining wins.
      - Combined "final" = actual + projected_remaining.

    Skipped teams (no standings entry) keep their full-season projection
    unchanged. Mutates ``rows`` in place and returns the same list.
    """
    skipped: List[str] = []
    for rec in rows:
        team = rec["org"]
        s = standings.get(team)
        if not s:
            skipped.append(team)
            continue

        gp = float(s.get("GP", 0.0))
        actual_w = float(s.get("W", 0.0))
        actual_l = float(s.get("L", 0.0))
        actual_rs = float(s.get("RS", 0.0))
        actual_ra = float(s.get("RA", 0.0))
        season_length = float(rec["games"])
        remaining_games = max(0.0, season_length - gp)

        if remaining_games <= 0 or season_length <= 0:
            # Season already over (or no schedule info) — actuals replace the projection.
            rec.update({
                "uses_current_standings": True,
                "games_played": gp,
                "games_remaining": 0.0,
                "actual_w": actual_w,
                "actual_l": actual_l,
                "actual_rs": actual_rs,
                "actual_ra": actual_ra,
                "remaining_w": 0.0,
                "remaining_rs": 0.0,
                "remaining_ra": 0.0,
                "wins": actual_w,
                "losses": actual_l,
                "rs": actual_rs,
                "ra": actual_ra,
            })
            # Sync the stashed projection blocks so the per-team MD picks up
            # actual numbers in the Offense / Pitching / Pythagorean sections.
            if "_rs_block" in rec:
                rec["_rs_block"] = dict(rec["_rs_block"])
                rec["_rs_block"]["rs"] = actual_rs
            if "_ra_block" in rec:
                rec["_ra_block"] = dict(rec["_ra_block"])
                rec["_ra_block"]["ra"] = actual_ra
            # Player lines: zero remaining → blend math collapses to actuals.
            _overlay_player_lines(rec, 0.0)
            continue

        proj_full_rs = float(rec["rs"])
        proj_full_ra = float(rec["ra"])
        rs_per_game = proj_full_rs / season_length
        ra_per_game = proj_full_ra / season_length

        rem_rs = rs_per_game * remaining_games
        rem_ra = ra_per_game * remaining_games
        rem_w, _exp = pythagenpat_wins(rem_rs, rem_ra, int(round(remaining_games)))
        rem_l = remaining_games - rem_w

        combined_rs = actual_rs + rem_rs
        combined_ra = actual_ra + rem_ra
        rec.update({
            "uses_current_standings": True,
            "games_played": gp,
            "games_remaining": remaining_games,
            "actual_w": actual_w,
            "actual_l": actual_l,
            "actual_rs": actual_rs,
            "actual_ra": actual_ra,
            "remaining_w": rem_w,
            "remaining_l": rem_l,
            "remaining_rs": rem_rs,
            "remaining_ra": rem_ra,
            # The "final" wins/losses reported reflect actual-to-date plus projected remainder.
            "wins_projection_only": rec.get("wins_original", rec["wins"]),  # preserve pure-projection number
            "wins": actual_w + rem_w,
            "losses": actual_l + rem_l,
            "rs": combined_rs,
            "ra": combined_ra,
        })
        # Sync the stashed projection blocks so render_md's Offense / Pitching /
        # Pythagorean sections reflect actual-to-date + projected remainder
        # rather than the pre-overlay full-season projection.
        if "_rs_block" in rec:
            rec["_rs_block"] = dict(rec["_rs_block"])
            rec["_rs_block"]["rs"] = combined_rs
        if "_ra_block" in rec:
            rec["_ra_block"] = dict(rec["_ra_block"])
            rec["_ra_block"]["ra"] = combined_ra
        # Per-player overlay: actual current-year stats + career-rate
        # projection over remaining_games. Tied to schedule, not to
        # full_proj_PA, so part-time / injured players don't get inflated.
        _overlay_player_lines(rec, remaining_games)

    if skipped:
        logger.warning(
            "No current standings for %d team(s) (left as full-season projection): %s",
            len(skipped), ", ".join(sorted(skipped)),
        )
    return rows


def write_projection_md(
    args: argparse.Namespace,
    league: str,
    rec: Dict[str, Any],
    ts: str,
) -> Path:
    """Render and write a single per-org per-level projection MD using the
    (possibly rebalanced) numbers stored on ``rec``."""
    out_dir = args.output_dir or (SCRIPT_DIR / league / "projections")
    out_dir.mkdir(parents=True, exist_ok=True)
    org_slug = (rec.get("org_code") or rec["org"].lower().replace(" ", "_"))
    out_md = out_dir / f"{org_slug}_{rec['level']}_projection_{ts}.md"

    md = render_md(
        league, rec["org"], rec["level"],
        rec["_constants"], rec["_rs_block"], rec["_ra_block"],
        rec["wins"], rec["losses"], rec["exponent"], rec["games"],
        hitter_lines=rec.get("_hitter_lines"),
        pitcher_lines=rec.get("_pitcher_lines"),
    )
    if rec.get("rebalanced"):
        md += (
            "\n\n## Roster-Realistic Recalibration\n\n"
            f"_Original Pythagorean: {rec.get('wins_original', 0):.1f}W "
            f"({rec.get('rs_original', 0):.0f} RS / {rec.get('ra_original', 0):.0f} RA). "
            f"League-wide rebalance shifted RS by {rec.get('shift_rs', 0):+.1f} and "
            f"RA by {rec.get('shift_ra', 0):+.1f} to anchor the league baseline to the "
            f"optimized-roster distribution._\n"
        )
    if rec.get("uses_current_standings"):
        md += (
            "\n## Current Standings + Rest-of-Season\n\n"
            "_Final projection above is actual-to-date plus projected remainder._\n\n"
            "| Segment | W-L | Win% | RS | RA | RD | Games |\n"
            "| --- | --- | --- | --- | --- | --- | --- |\n"
            f"| Actual to date | "
            f"{rec.get('actual_w', 0):.0f}-{rec.get('actual_l', 0):.0f} | "
            f"{(rec.get('actual_w', 0) / max(1, rec.get('games_played', 1))):.3f} | "
            f"{rec.get('actual_rs', 0):.0f} | {rec.get('actual_ra', 0):.0f} | "
            f"{rec.get('actual_rs', 0) - rec.get('actual_ra', 0):+.0f} | "
            f"{rec.get('games_played', 0):.0f} |\n"
            f"| Projected rest-of-season | "
            f"{rec.get('remaining_w', 0):.0f}-{rec.get('remaining_l', 0):.0f} | "
            f"{(rec.get('remaining_w', 0) / max(1, rec.get('games_remaining', 1))):.3f} | "
            f"{rec.get('remaining_rs', 0):.0f} | {rec.get('remaining_ra', 0):.0f} | "
            f"{rec.get('remaining_rs', 0) - rec.get('remaining_ra', 0):+.0f} | "
            f"{rec.get('games_remaining', 0):.0f} |\n"
            f"| **Final** | "
            f"**{rec['wins']:.0f}-{rec['losses']:.0f}** | "
            f"**{rec['wins'] / max(1, rec['games']):.3f}** | "
            f"{rec['rs']:.0f} | {rec['ra']:.0f} | "
            f"{rec['rs'] - rec['ra']:+.0f} | {rec['games']:.0f} |\n"
        )
    out_md.write_text(md, encoding="utf-8")
    logger.info("Wrote %s", out_md)
    return out_md


# -----------------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------------

def main() -> int:
    args = parse_args()
    logging.basicConfig(level=getattr(logging, args.log_level), format="%(levelname)s: %(message)s")

    # Resolve org list as (display_name, team_code) pairs. team_code mirrors
    # what depth_chart.py used in its output filenames (e.g. "wlg"); when it's
    # None we fall back to the slugified display name.
    orgs_to_run: List[Tuple[str, Optional[str]]]
    try:
        import depth_chart  # for resolve_all_orgs / _name_to_code_map / _default_park_factors_path
    except Exception as e:
        logger.error("Could not import depth_chart helpers: %s", e)
        return 2

    # Default park-factors to the league's canonical file so team codes resolve
    # even when --park-factors isn't passed explicitly. Mirrors depth_chart.
    pf_path = args.park_factors or depth_chart._default_park_factors_path(args.league)
    if not pf_path.exists():
        logger.warning(
            "No park-factors file at %s — team codes can't be resolved, "
            "depth-file lookup will fall back to slugified org names.",
            pf_path,
        )
        pf_path = None

    if args.all_orgs:
        try:
            pairs = depth_chart.resolve_all_orgs(args.league, Path("config"), pf_path)
        except Exception as e:
            logger.error("Failed to resolve --all-orgs: %s", e)
            return 2
        if not pairs:
            logger.error("No ML orgs found in teams-%s.json", args.league)
            return 2
        # If resolve_all_orgs came back without codes (e.g. it walked the
        # ndl_orgs.json path with no park-factors), patch the codes in from
        # the park-factors mapping we just loaded.
        if pf_path is not None and any(code is None for _, code in pairs):
            mapping = depth_chart._name_to_code_map(pf_path)
            pairs = [(name, code or mapping.get(name)) for name, code in pairs]
        orgs_to_run = [(name, code) for name, code in pairs]
    else:
        if not args.org:
            logger.error("Either --org or --all-orgs is required.")
            return 2
        single_code: Optional[str] = None
        if pf_path is not None:
            mapping = depth_chart._name_to_code_map(pf_path)
            single_code = mapping.get(args.org)
        orgs_to_run = [(args.org, single_code)]

    # Resolve level list.
    levels_to_run: List[str]
    if args.all_levels:
        try:
            cfg = json.loads(args.depth_config.read_text(encoding="utf-8"))
            levels_to_run = list(cfg.get("levels", {}).keys())
            if not levels_to_run:
                raise ValueError("No 'levels' block in depth_config.json")
        except Exception as e:
            logger.error("Failed to load depth_config for --all-levels: %s", e)
            return 2
    else:
        if not args.level:
            logger.error("Either --level or --all-levels is required.")
            return 2
        levels_to_run = [args.level.strip().upper()]

    # Single-org/single-level can still use --input/--constants explicit overrides.
    if (args.input or args.constants) and (args.all_orgs or args.all_levels):
        logger.error("--input / --constants are only valid in single-org single-level mode.")
        return 2

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    summary_rows: List[Dict[str, Any]] = []
    multi_orgs = len(orgs_to_run) > 1
    multi = multi_orgs or (len(levels_to_run) > 1)

    # Pass 1: compute projections. Don't write MDs yet.
    for org, org_code in orgs_to_run:
        for level in levels_to_run:
            if multi:
                logger.info("=" * 60)
                logger.info("Projecting: %s @ %s (code=%s)", org, level, org_code or "—")
            rec = compute_one_projection(
                args, args.league, org, level,
                csv_override=args.input, const_override=args.constants,
                org_code=org_code,
            )
            if rec is not None:
                summary_rows.append(rec)

    # Pass 2: roster-realistic recalibration when running multiple orgs.
    rebalance_notes: Dict[str, List[str]] = {}
    if multi_orgs and summary_rows and not args.no_rebalance:
        summary_rows = rebalance_to_roster_baseline(summary_rows, note_lines=rebalance_notes)
        for level, notes in rebalance_notes.items():
            for n in notes:
                logger.info("[%s] %s", level, n)

    # Resolve target_year — explicit flag wins; else fall back to the constants sidecar's year.
    target_year = args.year
    if target_year is None and summary_rows:
        for r in summary_rows:
            y = r.get("year")
            if y not in (None, "", "?"):
                try:
                    target_year = int(y)
                    break
                except (TypeError, ValueError):
                    continue
    if target_year is None:
        # In-game season from league_settings.json before the calendar-year fallback.
        try:
            import depth_chart as dc
            target_year = dc.league_default_year(args.league)
        except Exception:
            target_year = None
    if target_year is None:
        target_year = datetime.now().year

    # Pass 3: split into actual + remaining if --use-current-standings.
    standings_notes: Dict[str, str] = {}
    if args.use_current_standings and summary_rows:
        try:
            import current_standings as cs
            import depth_chart as dc
        except ImportError as e:
            logger.error("Could not import current_standings module: %s", e)
            return 2

        league_ids_map = dc.load_league_ids(args.league_ids_config) if hasattr(args, "league_ids_config") else {}
        if not league_ids_map:
            try:
                league_ids_map = json.loads(Path("config/league_ids.json").read_text(encoding="utf-8"))
            except Exception:
                league_ids_map = {}

        # Resolve base URL for the API.
        try:
            import stats as sapi
            base_url = sapi.resolve_base_url(args.league, None, Path("config") / "league_url.json")
        except Exception as e:
            logger.error("Could not resolve API base URL: %s", e)
            base_url = None

        if base_url:
            # Per-level: fetch standings using that level's lid (use first lid in the list).
            for level in levels_to_run:
                lids = (league_ids_map.get(args.league.lower(), {}) or {}).get(level.upper(), [])
                if not lids:
                    logger.warning("No lid for %s/%s; skipping current-standings overlay at this level.", args.league, level)
                    continue
                try:
                    standings = cs.fetch_standings_by_team_name(
                        base_url, target_year, int(lids[0]), args.league, Path("config"),
                    )
                except (URLError, TimeoutError, ValueError) as e:
                    logger.warning("Failed to fetch standings for %s: %s", level, e)
                    continue

                level_rows = [r for r in summary_rows if r["level"] == level]
                apply_current_standings(level_rows, standings)
                # Quick summary of the segment
                gp_total = sum(r.get("games_played", 0) for r in level_rows if r.get("uses_current_standings"))
                rem_total = sum(r.get("games_remaining", 0) for r in level_rows if r.get("uses_current_standings"))
                if gp_total > 0:
                    standings_notes[level] = (
                        f"Current-standings overlay: {gp_total:.0f} games played, "
                        f"{rem_total:.0f} games remaining (per-team avg "
                        f"{rem_total / max(1, len(level_rows)):.0f})."
                    )
        else:
            logger.warning("Skipping --use-current-standings: no API base URL.")

    # Pass 4: write per-org MDs (using rebalanced + standings-adjusted values).
    for rec in summary_rows:
        write_projection_md(args, args.league, rec, ts)

    # Single-projection stdout dump (legacy behavior).
    if not multi and summary_rows:
        only = summary_rows[0]
        out_dir = args.output_dir or (SCRIPT_DIR / args.league / "projections")
        org_slug = (only.get("org_code") or only["org"].lower().replace(" ", "_"))
        single_md = out_dir / f"{org_slug}_{only['level']}_projection_{ts}.md"
        if single_md.exists():
            print(single_md.read_text(encoding="utf-8"))

    # League rollup.
    if multi and summary_rows and not args.no_summary:
        out_dir = args.output_dir or (SCRIPT_DIR / args.league / "projections")
        out_dir.mkdir(parents=True, exist_ok=True)
        league_md = render_league_summary(
            args.league, target_year, summary_rows,
            qual_pa_per_game=float(args.qual_pa_per_game),
            qual_ip_per_game=float(args.qual_ip_per_game),
            qual_reliever_ip_per_game=float(args.qual_reliever_ip_per_game),
            qual_counting_fraction=float(args.qual_counting_fraction),
            leader_mode=args.leader_mode,
        )
        # Append rebalance + standings-overlay notes at the top so context is visible.
        preamble_lines: List[str] = []
        if rebalance_notes:
            preamble_lines.append("")
            preamble_lines.append("_Note: roster-realistic recalibration applied (per-level shifts):_")
            preamble_lines.append("")
            for level, notes in rebalance_notes.items():
                preamble_lines.append(f"- **{level}**: {notes[0]}")
            preamble_lines.append("")
        if standings_notes:
            preamble_lines.append("_Note: actual standings folded in (rest-of-season projection):_")
            preamble_lines.append("")
            for level, note in standings_notes.items():
                preamble_lines.append(f"- **{level}**: {note}")
            preamble_lines.append("")
        if preamble_lines:
            league_md = league_md.replace("_Generated", "\n".join(preamble_lines) + "\n_Generated", 1)
        league_path = out_dir / f"_league_projection_{ts}.md"
        league_path.write_text(league_md, encoding="utf-8")
        logger.info("Wrote league projection summary: %s", league_path)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
