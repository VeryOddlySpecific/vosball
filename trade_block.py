#!/usr/bin/env python3
"""
trade_block.py — Build an ideal trade block for one organization across every
level in the system. Surfaces players who could be moved without weakening the
org: those buried on the depth chart, those in overstaffed position groups,
and those off the chart entirely but with enough VOS / upside to fetch a
return (or at least be flipped before they're DFA'd).

Inputs
------
- Latest evaluation_summary_{league}_*.csv (same as depth_chart.py).
- StatsPlus stat endpoints (hitter/pitcher) for current + prior years.
- config/depth_config.json (per-level roster sizes, role counts, weights).
- /players API override + optional OOTP roster CSV patch (same as depth_chart.py).

What it surfaces (per the user-selected criteria)
-------------------------------------------------
1. Blocked at position/level — solid players sitting at Util/Bench tiers
   above somebody better at every viable position.
2. Surplus depth — position groups where the org has more quality bodies than
   the ML+AAA depth chart will ever absorb.
3. Cut-watch with value — off-the-chart players whose VOS or VOS_Pot is high
   enough that they shouldn't just be released for nothing.

Output
------
- {league}/trade_block/{org}_{ts}.md   (tiered report)
- {league}/trade_block/{org}_{ts}.csv  (flat candidates with scores/flags)

Usage
-----
    python trade_block.py --league sahl --org "Houston Astros" --year 2061
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import depth_chart as dc
import stats as sapi

logger = logging.getLogger(__name__)
SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG = SCRIPT_DIR / "config" / "depth_config.json"
DEFAULT_LEAGUE_URL = SCRIPT_DIR / "config" / "league_url.json"
DEFAULT_LEAGUE_IDS = SCRIPT_DIR / "config" / "league_ids.json"

HITTER_POSITIONS = dc.HITTER_POSITIONS

# Levels that count as "blocking" — a player who's buried below ML/AAA depth
# is genuinely tradeable; one buried at low-A typically isn't, because their
# value is still ratings-based and they have nowhere to go but up.
BLOCKING_LEVELS = {"ML", "AAA"}

# Thresholds for tagging — picked to align with the 20-80 scale used in
# depth_chart. Anything below a 45 composite at AAA+ is more of a cut than a
# trade chip; anything above a 50 with a roster jam is a genuine asset.
MIN_TRADE_COMPOSITE = 45.0      # below this, not really tradeable
PREMIUM_COMPOSITE = 55.0        # premium chip floor
LOTTERY_VOS_POT = 50.0          # young upside floor
LOTTERY_AGE_CEILING = 24        # "young" cutoff for lottery tickets
CUT_WATCH_VOS_FLOOR = 45.0      # off-chart, still has VOS value
CUT_WATCH_VOS_POT_FLOOR = 52.0  # off-chart, still has upside


# -----------------------------------------------------------------------------
# CLI
# -----------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Build an ideal trade block for one org across all levels.",
    )
    p.add_argument("--league", required=True, help="League slug (e.g. sahl).")
    p.add_argument("--org", required=True,
                   help="Organization display name as it appears in eval Org column.")
    p.add_argument("--org-code", type=str, default=None,
                   help="Subdirectory under {league}/eval/ to look in first for per-org evals.")
    p.add_argument("--year", type=int, default=None,
                   help="Latest year for stats window (default: current calendar year).")
    p.add_argument("--input", type=Path, default=None,
                   help="Override evaluation_summary CSV.")
    p.add_argument("--config", type=Path, default=DEFAULT_CONFIG,
                   help="depth_config.json path.")
    p.add_argument("--league-url-config", type=Path, default=DEFAULT_LEAGUE_URL)
    p.add_argument("--league-ids-config", type=Path, default=DEFAULT_LEAGUE_IDS)
    p.add_argument("--base-url", type=str, default=None,
                   help="Override league API base URL.")
    p.add_argument("--output-dir", type=Path, default=None,
                   help="Output directory (default: {league}/trade_block/).")
    p.add_argument("--no-archive", action="store_true",
                   help="Skip auto-archive of prior runs in the output directory.")
    p.add_argument("--no-stats", action="store_true",
                   help="Skip stat fetch; composite uses VOS only (debugging).")
    p.add_argument("--no-cache", action="store_true",
                   help="Skip disk cache; force fresh API fetches.")
    p.add_argument("--cache-dir", type=Path, default=None,
                   help="Override cache directory (default: {league}/cache/stats/).")
    p.add_argument("--no-players-override", action="store_true",
                   help="Skip the /players API override of League_Level/Org/Team.")
    p.add_argument("--players-override-csv", type=Path, default=None, action="append",
                   help="OOTP roster CSV export to patch on top of /players (repeatable).")
    p.add_argument("--include-inactive", action="store_true",
                   help="Keep retired/DFA/waivered/DL60 players in the analysis.")
    p.add_argument("--levels", type=str, default=None,
                   help="Comma-separated subset of levels to analyze (default: every level in depth_config).")
    p.add_argument("--min-composite", type=float, default=MIN_TRADE_COMPOSITE,
                   help=f"Composite floor for trade chip eligibility (default {MIN_TRADE_COMPOSITE}).")
    p.add_argument("--log-level", default="INFO",
                   choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    return p.parse_args()


# -----------------------------------------------------------------------------
# Per-level analysis — slot players into the same depth chart structure that
# depth_chart.py builds, then walk the placements to tag every player with
# their tier ("Starter", "Util1", "Util2", "Bench", "OffChart") and their
# slot label ("C-1", "C-2", "SP3", "MR-2", etc).
# -----------------------------------------------------------------------------

def analyze_level(
    level: str,
    args: argparse.Namespace,
    cfg: Dict[str, Any],
    eval_rows: List[Dict[str, str]],
    target_year: int,
    hitter_stats: Dict[str, Dict[str, Any]],
    pitcher_stats: Dict[str, Dict[str, Any]],
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Build tagged hitter/pitcher records for one level.

    Returns (hitter_records, pitcher_records). Each record is a depth_chart
    player dict plus two extra keys:
        _level     — the level this player is currently rostered at
        _tier      — Starter / Util1 / Util2 / Bench / OffChart (hitters)
                     SP1..SPn / CL / SU-N / MR-N / LR-N / OffChart (pitchers)
        _tier_rank — integer rank (1 = top of position; higher = deeper)
    """
    level_cfg = cfg["levels"].get(level)
    if not level_cfg:
        return [], []
    floors = cfg.get("stat_floors", {})

    # Z-score reference — computed across the whole league pool, same as
    # depth_chart so composites are comparable across runs.
    h_means, h_stds = (
        dc.compute_means_stds(hitter_stats, dc.HITTER_COMPONENTS, "overall")
        if hitter_stats else ({}, {})
    )
    h_means_l, h_stds_l = (
        dc.compute_means_stds(hitter_stats, dc.HITTER_COMPONENTS, "vs_l")
        if hitter_stats else ({}, {})
    )
    h_means_r, h_stds_r = (
        dc.compute_means_stds(hitter_stats, dc.HITTER_COMPONENTS, "vs_r")
        if hitter_stats else ({}, {})
    )
    p_means, p_stds = (
        dc.compute_means_stds(pitcher_stats, dc.PITCHER_COMPONENTS, "overall")
        if pitcher_stats else ({}, {})
    )

    # Filter eval to this org/level (we don't bother with affiliate splits at
    # rookie ball — for trade block purposes a R-ACL and R-DSL surplus look
    # the same).
    level_rows = dc.org_pool(eval_rows, args.org, level)
    records = [
        dc.build_player_record(
            r, pitcher_stats, hitter_stats, level_cfg, floors,
            p_means, p_stds, h_means, h_stds,
            h_means_l, h_stds_l, h_means_r, h_stds_r,
        )
        for r in level_rows
    ]

    hitters = [r for r in records if not r["is_pitcher"]]
    pitchers = [r for r in records if r["is_pitcher"]]

    # Slot into the depth chart structures depth_chart.py would build.
    placed = dc.assign_positions(hitters, level_cfg)
    pitcher_slots = dc.assign_pitchers(pitchers, level_cfg)

    # Tag every hitter with their tier + slot label. assign_positions creates
    # *copies* of the player dicts (via `{**player_dict, ...}`), so tagging
    # the slot entries doesn't tag the originals in ``hitters``. Walk the
    # placed slots, but write the tags onto the original hitter records via
    # a pid → record lookup. Otherwise the records we return from this
    # function are missing _level / _tier and downstream rendering shows
    # "?" for the level.
    hitter_by_pid = {h["pid"]: h for h in hitters}
    slotted_hitter_pids: set = set()
    for pos, slots in placed.items():
        for idx, p in enumerate(slots):
            tier_name = "Starter" if idx == 0 else f"Util{idx}" if idx <= 2 else "Bench"
            target = hitter_by_pid.get(p["pid"])
            if target is None:
                continue
            target["_level"] = level
            target["_tier"] = tier_name
            target["_tier_rank"] = idx + 1
            target["_slot_label"] = f"{pos}-{idx + 1}"
            slotted_hitter_pids.add(p["pid"])

    # Off-chart hitters (didn't make any tier). assign_positions doesn't copy
    # the originals for these, but tagging directly works fine either way.
    for p in hitters:
        if p["pid"] not in slotted_hitter_pids:
            p["_level"] = level
            p["_tier"] = "OffChart"
            p["_tier_rank"] = 99
            p["_slot_label"] = "—"

    # Same for pitchers — SP1..SPn / CL / SU / MR / LR with tier rank.
    slotted_pitcher_pids: set = set()
    for role, slots in pitcher_slots.items():
        for idx, p in enumerate(slots):
            if role == "SP":
                tier_name = f"SP{idx + 1}"
                slot_label = f"SP{idx + 1}"
            else:
                tier_name = f"{role}{idx + 1}" if len(slots) > 1 else role
                slot_label = f"{role}-{idx + 1}"
            p["_level"] = level
            p["_tier"] = tier_name
            p["_tier_rank"] = idx + 1
            p["_slot_label"] = slot_label
            slotted_pitcher_pids.add(p["pid"])

    for p in pitchers:
        if p["pid"] not in slotted_pitcher_pids:
            p["_level"] = level
            p["_tier"] = "OffChart"
            p["_tier_rank"] = 99
            p["_slot_label"] = "—"

    return hitters, pitchers


# -----------------------------------------------------------------------------
# Cross-level scoring
# -----------------------------------------------------------------------------

def parse_age(p: Dict[str, Any]) -> Optional[float]:
    age_raw = p.get("age")
    try:
        return float(age_raw) if age_raw not in (None, "") else None
    except (TypeError, ValueError):
        return None


def compute_block_status(
    hitter: Dict[str, Any],
    all_org_hitters: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """For each viable position the hitter plays, find their rank by composite
    among ML+AAA org players at that position. We scope to ML+AAA because
    that's where the immediate roster competition actually lives — a R-ball
    kid with a high projected composite doesn't "block" a ML starter, and
    including him in the ranking pollutes the signal.

    If the hitter himself is below AA, his block calc uses ML+AAA *plus* his
    own level so the ranking still makes sense (otherwise he wouldn't appear
    in the pool at all and the function returns rank 999).

    Returns:
        {
            "best_rank": int,           # min rank across all viable positions
            "best_pos": str,            # the position where they rank best
            "by_pos": {pos: rank, ...}  # full rank map for narrative
        }
    """
    pos_scores = hitter.get("pos_scores") or {}
    viable = [pos for pos in HITTER_POSITIONS if pos_scores.get(pos, 0) > 0]
    if not viable:
        primary = (hitter.get("primary_pos") or "").upper()
        viable = [primary] if primary in HITTER_POSITIONS else []

    # Scope the ranking pool. ML/AAA always; the hitter's own level too if
    # they're rostered below AAA — keeps them in the comparison.
    own_level = hitter.get("_level")
    pool_levels = set(BLOCKING_LEVELS)
    if own_level:
        pool_levels.add(own_level)

    by_pos: Dict[str, int] = {}
    best_rank = 999
    best_pos = ""
    for pos in viable:
        pool = [
            h for h in all_org_hitters
            if h.get("_level") in pool_levels
            and (h.get("pos_scores") or {}).get(pos, 0) > 0
        ]
        pool.sort(key=lambda h: -h.get("composite", 0))
        try:
            rank = next(i + 1 for i, h in enumerate(pool) if h["pid"] == hitter["pid"])
        except StopIteration:
            rank = 999
        by_pos[pos] = rank
        if rank < best_rank:
            best_rank = rank
            best_pos = pos
    return {"best_rank": best_rank, "best_pos": best_pos, "by_pos": by_pos}


def compute_pitcher_block(
    pitcher: Dict[str, Any],
    all_org_pitchers: List[Dict[str, Any]],
) -> int:
    """Rank among same-role ML+AAA org pitchers (plus the pitcher's own level
    if they're rostered below AAA, same logic as compute_block_status)."""
    role = pitcher.get("proj_role") or "RP"
    own_level = pitcher.get("_level")
    pool_levels = set(BLOCKING_LEVELS)
    if own_level:
        pool_levels.add(own_level)
    pool = [
        p for p in all_org_pitchers
        if (p.get("proj_role") or "RP") == role
        and p.get("_level") in pool_levels
    ]
    pool.sort(key=lambda p: -p.get("composite", 0))
    try:
        return next(i + 1 for i, p in enumerate(pool) if p["pid"] == pitcher["pid"])
    except StopIteration:
        return 999


def is_blocked_hitter(player: Dict[str, Any]) -> bool:
    """A hitter is 'blocked' when they're at ML/AAA in a non-starting tier
    AND the org has 3+ better options at every position they can play.
    """
    if player.get("is_pitcher"):
        return False
    if player.get("_level") not in BLOCKING_LEVELS:
        return False
    tier = player.get("_tier", "")
    # Starters aren't blocked. Util1 + Util2 + Bench + OffChart at ML/AAA are
    # the candidate pool.
    if tier == "Starter":
        return False
    block = player.get("_block") or {}
    best_rank = block.get("best_rank", 999)
    # Buried 4th-deep org-wide at every viable position = blocked.
    return best_rank >= 4


def is_blocked_pitcher(player: Dict[str, Any]) -> bool:
    """A pitcher is 'blocked' when they're at ML/AAA but outside the realistic
    promotion window:
        SP: 7th or deeper org-wide (5 ML + 5 AAA rotation only carries ~8)
        RP: 13th or deeper org-wide (8-9 ML pen + ~6 AAA pen)
    """
    if not player.get("is_pitcher"):
        return False
    if player.get("_level") not in BLOCKING_LEVELS:
        return False
    tier = player.get("_tier", "")
    # Top-of-staff guys aren't blocked.
    if tier in {"SP1", "SP2", "CL", "SU1", "SU2"}:
        return False
    role = player.get("proj_role") or "RP"
    rank = player.get("_pitcher_rank", 999)
    cutoff = 7 if role == "SP" else 13
    return rank >= cutoff


def is_cut_watch_with_value(player: Dict[str, Any]) -> bool:
    """Off-chart at any level, but still carries usable VOS or upside.

    Important: a 16-year-old with 50 VOS_Pot at rookie ball isn't "tradeable"
    in any meaningful sense — they're just normal farm depth. The thresholds
    below are deliberately tuned for players who'd return *something*.
    """
    if player.get("_tier") != "OffChart":
        return False
    vos = float(player.get("vos") or 0)
    vos_pot = float(player.get("vos_potential") or 0)
    # At ML/AAA, off-chart with VOS >= 45 is genuinely tradeable. At lower
    # levels, we want a higher VOS_Pot bar because raw VOS deflates with age.
    if player.get("_level") in BLOCKING_LEVELS:
        return vos >= CUT_WATCH_VOS_FLOOR or vos_pot >= CUT_WATCH_VOS_POT_FLOOR
    # Lower levels: only flag if VOS_Pot is meaningful AND the player is old
    # enough that this is "now or never" rather than developmental.
    age = parse_age(player) or 0
    return vos_pot >= CUT_WATCH_VOS_POT_FLOOR + 3 and age >= 21


def compute_trade_value(player: Dict[str, Any]) -> float:
    """Single-number rating used for sorting & tiering.

    Blends composite (current value) with VOS_Pot (ceiling) and an age
    adjustment. Younger players get a boost because they retain trade value
    longer; older blocked players still have some value if their composite
    is high but it decays.
    """
    comp = float(player.get("composite") or 0)
    vos = float(player.get("vos") or 0)
    vos_pot = float(player.get("vos_potential") or 0)
    age = parse_age(player) or 28.0  # default to neutral when missing

    # Take the better of current composite and (vos + vos_pot midpoint) — a
    # young guy with low composite but high VOS_Pot still scores well.
    headline = max(comp, 0.5 * (vos + vos_pot))

    # Age curve: +3 if 22 or under, +1.5 if 23-25, 0 if 26-29, -2 if 30-32, -4 if 33+.
    if age <= 22:
        age_adj = 3.0
    elif age <= 25:
        age_adj = 1.5
    elif age <= 29:
        age_adj = 0.0
    elif age <= 32:
        age_adj = -2.0
    else:
        age_adj = -4.0

    # Penalize sub-floor composites — they're depth pieces, not chips.
    if comp < 40 and vos_pot < 45:
        headline -= 3.0

    return headline + age_adj


def categorize(player: Dict[str, Any]) -> str:
    """Bucket each player into a report section.

    Premium requires the player to actually be at ML/AAA — a 20-year-old
    A-ball kid with a high projected composite is a Lottery ticket, not a
    plug-and-play piece for a contender.
    """
    comp = float(player.get("composite") or 0)
    vos = float(player.get("vos") or 0)
    vos_pot = float(player.get("vos_potential") or 0)
    age = parse_age(player) or 28.0
    level = player.get("_level", "")

    if comp >= PREMIUM_COMPOSITE and age < 32 and level in BLOCKING_LEVELS:
        return "Premium"
    if comp >= 48 or vos >= 52:
        return "Mid-Tier"
    if vos_pot >= LOTTERY_VOS_POT and age <= LOTTERY_AGE_CEILING:
        return "Lottery"
    return "Filler"


def build_reason_tags(player: Dict[str, Any]) -> List[str]:
    """Short human-readable reason strings for why this player is on the block."""
    reasons: List[str] = []
    if is_blocked_hitter(player):
        block = player.get("_block") or {}
        best_pos = block.get("best_pos") or player.get("primary_pos", "")
        best_rank = block.get("best_rank", 0)
        reasons.append(f"Blocked at {best_pos} (#{best_rank} in org)")
    if is_blocked_pitcher(player):
        role = player.get("proj_role", "RP")
        rank = player.get("_pitcher_rank", 0)
        reasons.append(f"Blocked in {role} pool (#{rank} in org)")
    if is_cut_watch_with_value(player):
        reasons.append(f"Off-chart with value (VOS {float(player.get('vos') or 0):.0f})")
    if player.get("_surplus_flag"):
        reasons.append(player["_surplus_flag"])
    return reasons


def compute_replacement_names(
    player: Dict[str, Any],
    all_hitters: List[Dict[str, Any]],
    all_pitchers: List[Dict[str, Any]],
    max_names: int = 2,
) -> List[str]:
    """Return the top ML+AAA org alternatives at this player's best position
    (or role, for pitchers), excluding the player themselves. Used to answer
    the "who makes this chip tradeable?" question in the chip tables.

    The returned strings include the alternative's level + composite so the
    user can see whether the replacement is an ML starter, an AAA backup,
    etc., at a glance.
    """
    if player.get("is_pitcher"):
        role = player.get("proj_role") or "RP"
        pool = [
            p for p in all_pitchers
            if (p.get("proj_role") or "RP") == role
            and p.get("_level") in BLOCKING_LEVELS
            and p["pid"] != player["pid"]
        ]
    else:
        block = player.get("_block") or {}
        # Best defensive position from the block calc; primary_pos is the
        # fallback when block_status hasn't been computed yet.
        best_pos = (block.get("best_pos") or player.get("primary_pos") or "").upper()
        if not best_pos:
            return []
        pool = [
            h for h in all_hitters
            if (h.get("pos_scores") or {}).get(best_pos, 0) > 0
            and h.get("_level") in BLOCKING_LEVELS
            and h["pid"] != player["pid"]
        ]
    pool.sort(key=lambda p: -p.get("composite", 0))
    out: List[str] = []
    for p in pool[:max_names]:
        comp = float(p.get("composite") or 0)
        out.append(f"{p['name']} ({p.get('_level','?')}, {comp:.0f})")
    return out


# -----------------------------------------------------------------------------
# Surplus matrix — for each position, list every org hitter sorted by
# composite. Beyond the realistic ML+AAA carry count, mark "surplus".
# -----------------------------------------------------------------------------

# Approximate ML + AAA carry per position. Skill positions carry 2-3 deep
# across both levels; corner / DH spots carry 2.
POSITION_CARRY = {
    "C":  4,   # 2 ML + 2 AAA
    "1B": 3,
    "2B": 3,
    "SS": 3,
    "3B": 3,
    "LF": 3,
    "CF": 4,   # CFs play other OF too; depth matters
    "RF": 3,
    "DH": 2,
}


def build_position_surplus(
    all_hitters: List[Dict[str, Any]],
) -> Dict[str, List[Dict[str, Any]]]:
    """For each position, return an org ranking by composite (ML+AAA only,
    since that's where surplus actually matters — a 4th-deep CF at R-ball is
    farm depth, not a trade chip). The published matrix still shows the full
    org for context.

    A player is flagged ``_surplus_flag`` when:
      - they're at ML or AAA, AND
      - they are NOT a current Starter at their level (Starters are filling
        a real role; they can't simultaneously be "surplus"), AND
      - their best viable defensive position still has them past the carry
        line in the ML/AAA pool.

    DH is excluded from the best-position calc because every hitter scores
    at DH — including it would let a kid with 60 DH composite "block" the
    ML DH starter and incorrectly flag the starter as surplus.
    """
    surplus_by_pos: Dict[str, List[Dict[str, Any]]] = {}
    # ML+AAA-only rank lookup, used for surplus flagging.
    rank_by_pos: Dict[str, Dict[str, int]] = {}

    for pos in HITTER_POSITIONS:
        # Full org pool (for the rendered matrix).
        full_pool = [
            h for h in all_hitters
            if (h.get("pos_scores") or {}).get(pos, 0) > 0
        ]
        full_pool.sort(key=lambda h: -h.get("composite", 0))
        surplus_by_pos[pos] = full_pool

        # ML/AAA-only pool (for the surplus decision).
        tier_pool = [
            h for h in full_pool
            if h.get("_level") in BLOCKING_LEVELS
        ]
        rank_by_pos[pos] = {h["pid"]: i + 1 for i, h in enumerate(tier_pool)}

    for h in all_hitters:
        # Starters at their level are NOT surplus by definition.
        if h.get("_tier") == "Starter":
            continue
        # Only ML/AAA non-starters can be "surplus" — for everyone below,
        # this isn't the right framing (they're prospects, not chips yet).
        if h.get("_level") not in BLOCKING_LEVELS:
            continue
        defensive_positions = [
            p for p in HITTER_POSITIONS
            if p != "DH" and (h.get("pos_scores") or {}).get(p, 0) > 0
        ]
        if not defensive_positions:
            defensive_positions = ["DH"] if (h.get("pos_scores") or {}).get("DH", 0) > 0 else []
        if not defensive_positions:
            continue
        best_pos = min(
            defensive_positions,
            key=lambda p: rank_by_pos.get(p, {}).get(h["pid"], 999),
        )
        best_rank = rank_by_pos.get(best_pos, {}).get(h["pid"], 999)
        carry = POSITION_CARRY.get(best_pos, 3)
        if best_rank > carry:
            h["_surplus_flag"] = f"Surplus at {best_pos} (#{best_rank} ML+AAA)"

    return surplus_by_pos


# -----------------------------------------------------------------------------
# Acquisition target analysis — the inverse of the trade block. Walk every
# hitter position + pitcher role and grade the org's current depth, then
# emit a tier list of what to target back in trades.
# -----------------------------------------------------------------------------

# Need-tier thresholds. The numbers are calibrated for the same 20-80
# composite scale used everywhere else in this file.
STARTER_FLOOR_OK = 55.0       # above this, the starter is a clear plus piece
STARTER_FLOOR_MID = 50.0      # 50-54: average-ish; depth becomes important
DEPTH_FLOOR_OK = 48.0         # AAA backup is usable if comp >= this
AGING_AGE = 32                # starter age at which "aging out" kicks in


def _best_at_position(
    pos: str,
    all_hitters: List[Dict[str, Any]],
) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """Return (best, second_best) ML+AAA org players for ``pos`` by composite.

    Only the ML+AAA pool counts because that's who's available to start /
    backup tomorrow. Lower-level prospects may be in the pipeline but
    they're not depth today.
    """
    pool = [
        h for h in all_hitters
        if (h.get("pos_scores") or {}).get(pos, 0) > 0
        and h.get("_level") in BLOCKING_LEVELS
    ]
    pool.sort(key=lambda h: -h.get("composite", 0))
    best = pool[0] if pool else None
    second = pool[1] if len(pool) > 1 else None
    return best, second


def _farm_successor(
    pos: str,
    all_hitters: List[Dict[str, Any]],
    max_age: int = 26,
) -> Optional[Dict[str, Any]]:
    """Highest-ceiling young prospect below AAA at ``pos``. Used to decide
    whether an aging ML starter has a real internal replacement coming."""
    pool = [
        h for h in all_hitters
        if (h.get("pos_scores") or {}).get(pos, 0) > 0
        and h.get("_level") not in BLOCKING_LEVELS
        and (parse_age(h) or 99) <= max_age
    ]
    pool.sort(key=lambda h: -(float(h.get("vos_potential") or 0)))
    return pool[0] if pool else None


def assess_position_need(
    pos: str,
    all_hitters: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Grade the org's depth at one hitter position. Returns:
        {
            "pos": str,
            "tier": "Critical" | "Major" | "Depth" | "Set",
            "starter": player_dict | None,
            "depth": player_dict | None,
            "farm_successor": player_dict | None,
            "summary": str,        # one-line state of the position
            "archetype": str,      # what to go acquire
            "reasoning": str,      # why this tier
        }
    """
    starter, depth = _best_at_position(pos, all_hitters)
    starter_comp = float(starter.get("composite") or 0) if starter else 0.0
    starter_age = parse_age(starter) if starter else None
    depth_comp = float(depth.get("composite") or 0) if depth else 0.0

    # No ML/AAA option at all = critical hole.
    if starter is None:
        successor = _farm_successor(pos, all_hitters, max_age=27)
        return {
            "pos": pos,
            "tier": "Critical",
            "starter": None,
            "depth": None,
            "farm_successor": successor,
            "summary": "No ML/AAA option on the roster.",
            "archetype": _archetype_for_position(pos, age_target="25-30", upgrade_level="starter"),
            "reasoning": "Position is empty at the immediate-roster level.",
        }

    aging = starter_age is not None and starter_age >= AGING_AGE
    successor = _farm_successor(pos, all_hitters, max_age=27) if aging else None
    successor_pot = float(successor.get("vos_potential") or 0) if successor else 0.0

    # Tier 1 — Critical: weak starter + no usable depth, OR aging + no successor.
    if starter_comp < STARTER_FLOOR_MID and depth_comp < DEPTH_FLOOR_OK:
        tier = "Critical"
        reasoning = (
            f"Starter composite {starter_comp:.1f} below average, "
            f"depth composite {depth_comp:.1f} below the {DEPTH_FLOOR_OK:.0f} floor."
        )
        archetype = _archetype_for_position(pos, age_target="25-30", upgrade_level="starter")
    elif aging and depth_comp < DEPTH_FLOOR_OK and successor_pot < 50:
        tier = "Critical"
        reasoning = (
            f"Starter is {starter_age:.0f} with no AAA backup (comp {depth_comp:.1f}) "
            f"and no farm successor (best VOS_Pot {successor_pot:.1f})."
        )
        archetype = _archetype_for_position(pos, age_target="24-29", upgrade_level="starter")
    # Tier 2 — Major: mediocre starter or no depth.
    elif starter_comp < STARTER_FLOOR_OK or (aging and successor_pot < 50):
        tier = "Major"
        if aging:
            reasoning = (
                f"Starter is {starter_age:.0f}; successor pipeline is thin "
                f"(best VOS_Pot {successor_pot:.1f})."
            )
        else:
            reasoning = (
                f"Starter composite {starter_comp:.1f} is mediocre; "
                f"upgrade or insurance worth pursuing."
            )
        archetype = _archetype_for_position(pos, age_target="25-30", upgrade_level="upgrade")
    # Tier 3 — Depth: strong starter but thin AAA insurance.
    elif depth_comp < DEPTH_FLOOR_OK:
        tier = "Depth"
        reasoning = (
            f"Starter is strong (comp {starter_comp:.1f}) but AAA depth is "
            f"weak (comp {depth_comp:.1f})."
        )
        archetype = _archetype_for_position(pos, age_target="any", upgrade_level="depth")
    else:
        tier = "Set"
        reasoning = (
            f"Starter (comp {starter_comp:.1f}) and depth (comp {depth_comp:.1f}) "
            "both above their thresholds."
        )
        archetype = "—"

    summary_parts = [f"{starter['name']} (comp {starter_comp:.1f}"]
    if starter_age is not None:
        summary_parts[0] += f", age {starter_age:.0f}"
    summary_parts[0] += ")"
    if depth:
        summary_parts.append(
            f"depth: {depth['name']} (comp {depth_comp:.1f})"
        )
    else:
        summary_parts.append("no AAA depth")
    summary = " · ".join(summary_parts)

    return {
        "pos": pos,
        "tier": tier,
        "starter": starter,
        "depth": depth,
        "farm_successor": successor,
        "summary": summary,
        "archetype": archetype,
        "reasoning": reasoning,
    }


def _archetype_for_position(
    pos: str,
    age_target: str = "25-30",
    upgrade_level: str = "starter",
) -> str:
    """Translate (position, upgrade target) into a short archetype string. The
    age range is a rough guide — "starter" upgrades favor primes, "depth"
    upgrades can be older role players or pre-arb upside swings."""
    base = {
        "C": "Two-way catcher (framing + adequate bat)",
        "1B": "Power bat — RH or LH; ISO .200+",
        "2B": "Contact-and-glove middle infielder",
        "SS": "Plus defender with average bat",
        "3B": "Power-hitting corner infielder",
        "LF": "Bat-first corner OF; ISO .180+",
        "CF": "Plus defender with on-base skills",
        "RF": "Power bat with arm strength",
        "DH": "Pure middle-of-order bat",
    }.get(pos, "Quality regular at the position")
    if upgrade_level == "depth":
        return f"AAA-ready {base.lower()}; pre-arb preferred"
    if upgrade_level == "upgrade":
        return f"{base}; age {age_target}, controllable contract"
    return f"{base}; age {age_target}, multi-year control"


def assess_pitcher_need(
    role: str,
    all_pitchers: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Same shape as assess_position_need but for pitcher role buckets.

    Roles assessed:
      - SP-top   (top of rotation: best 2 SPs)
      - SP-back  (back of rotation: SPs 3-5)
      - RP-late  (CL + SU: high-leverage)
      - RP-mid   (MR + LR: middle relief)
    """
    sps = sorted(
        [p for p in all_pitchers
         if (p.get("proj_role") or "RP") == "SP"
         and p.get("_level") in BLOCKING_LEVELS],
        key=lambda p: -p.get("composite", 0),
    )
    rps = sorted(
        [p for p in all_pitchers
         if (p.get("proj_role") or "RP") == "RP"
         and p.get("_level") in BLOCKING_LEVELS],
        key=lambda p: -p.get("composite", 0),
    )

    if role == "SP-top":
        top = sps[:2]
        avg = sum(p["composite"] for p in top) / len(top) if top else 0.0
        if avg >= 58:
            tier, archetype, reasoning = (
                "Set", "—",
                f"Top-2 SP avg composite {avg:.1f} is plus."
            )
        elif avg >= 53:
            tier, archetype, reasoning = (
                "Major", "Front-line SP — sub-3.50 FIP, multi-year control",
                f"Top-2 SP avg {avg:.1f} is average; ace-level upgrade worth pursuing."
            )
        else:
            tier, archetype, reasoning = (
                "Critical", "Front-line SP — sub-3.50 FIP, multi-year control",
                f"Top-2 SP avg {avg:.1f} is below average; need a real #1/#2."
            )
        leader_name = top[0]["name"] if top else "—"
        return {
            "pos": "SP1/2",
            "tier": tier,
            "starter": top[0] if top else None,
            "depth": top[1] if len(top) > 1 else None,
            "farm_successor": None,
            "summary": f"Top SPs: {', '.join(p['name'] for p in top) or '—'}",
            "archetype": archetype,
            "reasoning": reasoning,
        }

    if role == "SP-back":
        back = sps[2:5]
        avg = sum(p["composite"] for p in back) / len(back) if back else 0.0
        if avg >= 50:
            tier, archetype, reasoning = (
                "Set", "—",
                f"Back-end SP avg composite {avg:.1f} is acceptable."
            )
        elif avg >= 45:
            tier, archetype, reasoning = (
                "Depth", "Mid-rotation SP — 4.20 FIP, contact-suppression types",
                f"Back-end SP avg {avg:.1f} is fringe; one more rotation arm would help."
            )
        else:
            tier, archetype, reasoning = (
                "Major", "Mid-rotation SP — 4.20 FIP, ground-ball lean preferred",
                f"Back-end SP avg {avg:.1f} is poor; multiple rotation upgrades needed."
            )
        return {
            "pos": "SP3-5",
            "tier": tier,
            "starter": back[0] if back else None,
            "depth": back[1] if len(back) > 1 else None,
            "farm_successor": None,
            "summary": f"Back-end SPs: {', '.join(p['name'] for p in back) or '—'}",
            "archetype": archetype,
            "reasoning": reasoning,
        }

    if role == "RP-late":
        late = rps[:3]  # CL + 2 SU is the high-leverage group
        avg = sum(p["composite"] for p in late) / len(late) if late else 0.0
        if avg >= 58:
            tier, archetype, reasoning = (
                "Set", "—",
                f"Late-inning trio avg composite {avg:.1f} is plus."
            )
        elif avg >= 53:
            tier, archetype, reasoning = (
                "Depth", "Setup arm — K-BB% 15%+ ",
                f"Late-inning trio avg {avg:.1f} is average; adding a high-leverage arm tightens the back end."
            )
        else:
            tier, archetype, reasoning = (
                "Major", "Closer-quality reliever — K-BB% 18%+, swing-and-miss FB",
                f"Late-inning trio avg {avg:.1f} is below average; bullpen needs a real closer."
            )
        return {
            "pos": "CL/SU",
            "tier": tier,
            "starter": late[0] if late else None,
            "depth": late[1] if len(late) > 1 else None,
            "farm_successor": None,
            "summary": f"Late: {', '.join(p['name'] for p in late) or '—'}",
            "archetype": archetype,
            "reasoning": reasoning,
        }

    if role == "RP-mid":
        mid = rps[3:9]
        avg = sum(p["composite"] for p in mid) / len(mid) if mid else 0.0
        if avg >= 50:
            tier, archetype, reasoning = (
                "Set", "—",
                f"Middle-relief avg composite {avg:.1f} is fine."
            )
        elif avg >= 45:
            tier, archetype, reasoning = (
                "Depth", "Bulk reliever — multi-inning capable, pre-arb preferred",
                f"Middle-relief avg {avg:.1f} is thin; volume arms would help."
            )
        else:
            tier, archetype, reasoning = (
                "Major", "Bulk reliever — ground-ball type, cheap control",
                f"Middle-relief avg {avg:.1f} is poor; the bridge from starter to late innings is weak."
            )
        return {
            "pos": "MR/LR",
            "tier": tier,
            "starter": mid[0] if mid else None,
            "depth": mid[1] if len(mid) > 1 else None,
            "farm_successor": None,
            "summary": f"Middle: {', '.join(p['name'] for p in mid[:3]) or '—'}",
            "archetype": archetype,
            "reasoning": reasoning,
        }

    raise ValueError(f"Unknown pitcher role: {role}")


def build_acquisition_targets(
    all_hitters: List[Dict[str, Any]],
    all_pitchers: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Run the full needs assessment and return one entry per position/role."""
    out: List[Dict[str, Any]] = []
    for pos in HITTER_POSITIONS:
        out.append(assess_position_need(pos, all_hitters))
    for role in ("SP-top", "SP-back", "RP-late", "RP-mid"):
        out.append(assess_pitcher_need(role, all_pitchers))
    return out


# -----------------------------------------------------------------------------
# Report rendering
# -----------------------------------------------------------------------------

def fmt(x: Any, digits: int = 1) -> str:
    try:
        return f"{float(x):.{digits}f}"
    except (TypeError, ValueError):
        return str(x) if x not in (None, "") else "—"


def render_md(
    league: str,
    org: str,
    year: int,
    candidates: List[Dict[str, Any]],
    surplus_by_pos: Dict[str, List[Dict[str, Any]]],
    all_org_hitters: List[Dict[str, Any]],
    all_org_pitchers: List[Dict[str, Any]],
) -> str:
    out: List[str] = []
    out.append(f"# Trade Block — {org}  ·  {league.upper()}  ·  {year}")
    out.append("")
    out.append(
        "_Players blocked on the depth chart, in overstaffed position groups, "
        "or off the chart entirely with leftover value. Trade Value blends "
        "composite, VOS_Pot, and an age curve — higher is a bigger chip._"
    )
    out.append("")
    out.append(f"_Generated {datetime.now().strftime('%Y-%m-%d %H:%M')}._")
    out.append("")

    # Headline numbers.
    by_cat: Dict[str, List[Dict[str, Any]]] = {
        "Premium": [], "Mid-Tier": [], "Lottery": [], "Filler": [],
    }
    for c in candidates:
        by_cat.setdefault(c["_category"], []).append(c)

    out.append("## Summary")
    out.append("")
    out.append("| Tier | Count | Best Chip |")
    out.append("| --- | --- | --- |")
    for tier in ("Premium", "Mid-Tier", "Lottery", "Filler"):
        chips = by_cat.get(tier) or []
        if not chips:
            out.append(f"| {tier} | 0 | — |")
            continue
        chips.sort(key=lambda p: -p["_trade_value"])
        best = chips[0]
        out.append(
            f"| {tier} | {len(chips)} | "
            f"{best['name']} ({best.get('_level','?')}, TV {best['_trade_value']:.1f}) |"
        )
    out.append("")

    # Acquisition targets — the inverse of the trade block. For each position
    # and pitcher role, grade the org's depth and suggest what archetype to
    # go acquire. Tier ordering: Critical → Major → Depth → Set.
    targets = build_acquisition_targets(all_org_hitters, all_org_pitchers)
    tier_order = {"Critical": 0, "Major": 1, "Depth": 2, "Set": 3}
    targets.sort(key=lambda t: (tier_order.get(t["tier"], 9), t["pos"]))

    out.append("## Acquisition Targets")
    out.append("")
    out.append(
        "_Positions and pitcher roles where the org needs help. Tiers run "
        "Critical (must address) -> Major (clear upgrade) -> Depth (insurance) "
        "-> Set (don't trade for this). Archetype is what to ask for back._"
    )
    out.append("")
    out.append("| Tier | Pos/Role | Current State | Archetype to Target | Why |")
    out.append("| --- | --- | --- | --- | --- |")
    for t in targets:
        out.append(
            f"| **{t['tier']}** | {t['pos']} | {t['summary']} | "
            f"{t['archetype']} | {t['reasoning']} |"
        )
    out.append("")

    by_tier = {"Critical": 0, "Major": 0, "Depth": 0, "Set": 0}
    for t in targets:
        by_tier[t["tier"]] = by_tier.get(t["tier"], 0) + 1
    out.append(
        f"_Needs by tier: **{by_tier['Critical']} Critical** | "
        f"**{by_tier['Major']} Major** | "
        f"{by_tier['Depth']} Depth | {by_tier['Set']} Set._"
    )
    out.append("")

    def _player_table(
        rows: List[Dict[str, Any]],
        title: str,
        blurb: str,
    ) -> None:
        if not rows:
            return
        out.append(f"## {title}")
        out.append("")
        out.append(f"_{blurb}_")
        out.append("")
        out.append(
            "| Name | Lvl | Age | Pos/Role | Tier | Career | Reach | Comp "
            "| Trade Value | Replaced By | Why |"
        )
        out.append(
            "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"
        )
        rows = sorted(rows, key=lambda p: -p["_trade_value"])
        for p in rows:
            pos_label = p.get("primary_pos", "") or p.get("proj_role", "")
            reasons = ", ".join(p.get("_reasons") or []) or "-"
            replaced = "; ".join(p.get("_replacement") or []) or "-"
            out.append(
                f"| {p['name']} | {p.get('_level','')} | {fmt(p.get('age'), 0)} | "
                f"{pos_label} | {p.get('_tier','')} | "
                f"{fmt(p.get('vos'))} | {fmt(p.get('vos_potential'))} | "
                f"{fmt(p.get('composite'))} | "
                f"**{p['_trade_value']:.1f}** | {replaced} | {reasons} |"
            )
        out.append("")

    _player_table(
        by_cat.get("Premium") or [],
        "Premium Chips",
        "High-composite blocked players. These return real value in a trade.",
    )
    _player_table(
        by_cat.get("Mid-Tier") or [],
        "Mid-Tier Pieces",
        "Useful depth a contender could plug in tomorrow. Good filler for bigger packages.",
    )
    _player_table(
        by_cat.get("Lottery") or [],
        "Lottery Tickets",
        "Young, blocked or off-chart, but with VOS_Pot worth a flier. Pair with cash or another piece.",
    )
    _player_table(
        by_cat.get("Filler") or [],
        "Cut Watch / Filler",
        "Marginal value -- likely DFA targets if not moved. Better to flip for any return.",
    )

    out.append("## Position Surplus Matrix")
    out.append("")
    out.append(
        "_For every position, the org's top players by composite. The "
        "carry line shows the realistic ML + AAA absorption -- anyone below "
        "it is logistical depth at best._"
    )
    out.append("")
    out.append("| Pos | Carry | Players (rank . name . lvl . comp) |")
    out.append("| --- | --- | --- |")
    for pos in HITTER_POSITIONS:
        ranked = surplus_by_pos.get(pos) or []
        carry = POSITION_CARRY.get(pos, 3)
        cells = []
        for i, h in enumerate(ranked[:8], start=1):
            marker = "" if i <= carry else " *"
            cells.append(
                f"{i}. {h['name']} ({h.get('_level','?')}, {fmt(h.get('composite'))}){marker}"
            )
        line = "; ".join(cells) if cells else "-"
        out.append(f"| {pos} | {carry} | {line} |")
    out.append("")
    out.append("_\* = below the carry line; surplus depth._")
    out.append("")

    out.append("## Pitcher Surplus")
    out.append("")
    for role, cutoff in (("SP", 7), ("RP", 13)):
        pool = sorted(
            [p for p in all_org_pitchers if (p.get("proj_role") or "RP") == role],
            key=lambda p: -p.get("composite", 0),
        )
        if not pool:
            continue
        out.append(f"### {role} (carry ~ {cutoff})")
        out.append("")
        out.append("| Rank | Name | Lvl | Age | Career | Comp | Surplus? |")
        out.append("| --- | --- | --- | --- | --- | --- | --- |")
        for i, p in enumerate(pool[:20], start=1):
            tag = "yes" if i >= cutoff else ""
            out.append(
                f"| {i} | {p['name']} | {p.get('_level','')} | "
                f"{fmt(p.get('age'), 0)} | {fmt(p.get('vos'))} | "
                f"{fmt(p.get('composite'))} | {tag} |"
            )
        out.append("")

    return "\n".join(out)


def write_csv(path: Path, candidates: List[Dict[str, Any]]) -> None:
    fields = [
        "pid", "name", "age", "level", "tier", "slot_label",
        "primary_pos", "proj_role",
        "career", "reach", "composite", "trade_value", "category",
        "best_pos_rank", "best_pos", "pitcher_rank",
        "is_blocked", "is_cut_watch", "is_surplus",
        "replaced_by", "reasons",
    ]
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for c in sorted(candidates, key=lambda p: -p["_trade_value"]):
            block = c.get("_block") or {}
            writer.writerow({
                "pid": c.get("pid", ""),
                "name": c.get("name", ""),
                "age": c.get("age", ""),
                "level": c.get("_level", ""),
                "tier": c.get("_tier", ""),
                "slot_label": c.get("_slot_label", ""),
                "primary_pos": c.get("primary_pos", ""),
                "proj_role": c.get("proj_role", ""),
                "career": f"{float(c.get('vos') or 0):.2f}",
                "reach":  f"{float(c.get('vos_potential') or 0):.2f}",
                "composite": f"{float(c.get('composite') or 0):.2f}",
                "trade_value": f"{c['_trade_value']:.2f}",
                "category": c.get("_category", ""),
                "best_pos_rank": block.get("best_rank", ""),
                "best_pos": block.get("best_pos", ""),
                "pitcher_rank": c.get("_pitcher_rank", ""),
                "is_blocked": "1" if (
                    is_blocked_hitter(c) or is_blocked_pitcher(c)
                ) else "0",
                "is_cut_watch": "1" if is_cut_watch_with_value(c) else "0",
                "is_surplus": "1" if c.get("_surplus_flag") else "0",
                "replaced_by": "; ".join(c.get("_replacement") or []),
                "reasons": "; ".join(c.get("_reasons") or []),
            })


def main() -> int:
    args = parse_args()
    logging.basicConfig(level=getattr(logging, args.log_level), format="%(levelname)s: %(message)s")

    cfg = dc.load_config(args.config)
    # In-game season (league_settings.json) before calendar year — OOTP seasons
    # rarely match real-world dates, and a wrong year empties the stats window.
    target_year = args.year or dc.league_default_year(args.league) or datetime.now().year

    if args.levels:
        levels_to_run = [lvl.strip().upper() for lvl in args.levels.split(",") if lvl.strip()]
        for lvl in levels_to_run:
            if lvl not in cfg["levels"]:
                logger.error("Level '%s' not in depth_config.json", lvl)
                return 2
    else:
        levels_to_run = list(cfg["levels"].keys())

    out_dir = args.output_dir or (SCRIPT_DIR / args.league / "trade_block")
    out_dir.mkdir(parents=True, exist_ok=True)
    if not args.no_archive:
        moved, archive_dir = dc.archive_previous_runs(out_dir)
        if moved:
            logger.info("Archived %d prior trade_block file(s) to %s", moved, archive_dir)

    try:
        eval_path = dc.find_latest_eval(args.league, args.input, args.org_code)
    except FileNotFoundError as e:
        logger.error("%s", e)
        return 2
    logger.info("Using eval file: %s", eval_path)
    eval_rows = dc.read_eval(eval_path)

    players_lookup = {}
    level_id_to_label = {}
    team_id_to_name = {}
    if not args.no_players_override and not args.no_stats:
        base_url = sapi.resolve_base_url(args.league, args.base_url, args.league_url_config)
        if base_url:
            cache_dir = None
            if not args.no_cache:
                cache_dir = args.cache_dir or (SCRIPT_DIR / args.league / "cache" / "stats")
            players_lookup = sapi.build_players_lookup(base_url, cache_dir=cache_dir)
            if players_lookup:
                level_id_to_label = dc.load_level_id_to_label()
                team_id_to_name = dc.load_team_id_to_name(args.league)
                logger.info(
                    "Loaded /players (%d) -- overriding eval Level/Org/Team and "
                    "filtering retired/DFA/waivers/DL60.",
                    len(players_lookup),
                )

    if args.players_override_csv:
        if not level_id_to_label:
            level_id_to_label = dc.load_level_id_to_label()
        if not team_id_to_name:
            team_id_to_name = dc.load_team_id_to_name(args.league)
        team_name_to_id = dc.invert_team_id_to_name(team_id_to_name)
        csv_patch = dc.build_players_lookup_from_csv(
            args.players_override_csv, team_name_to_id
        )
        if csv_patch:
            collisions = sum(1 for pid in csv_patch if pid in players_lookup)
            players_lookup.update(csv_patch)
            logger.info(
                "Applied roster CSV patch: %d rows | %d overrode /players entries.",
                len(csv_patch), collisions,
            )

    if players_lookup:
        counts = dc.apply_players_override(
            eval_rows, players_lookup, level_id_to_label, team_id_to_name,
            include_inactive=args.include_inactive,
        )
        logger.info(
            "Players override: %d eval rows | %d level overrides | %d org overrides | "
            "filtered: %d retired, %d DFA, %d waivers, %d DL60",
            counts["total"], counts["level_overrides"], counts["org_overrides"],
            counts["filtered_retired"], counts["filtered_dfa"],
            counts["filtered_waivers"], counts["filtered_dl60"],
        )

    hitter_stats = {}
    pitcher_stats = {}
    if not args.no_stats:
        base_url = sapi.resolve_base_url(args.league, args.base_url, args.league_url_config)
        if not base_url:
            logger.error("No base URL for league '%s'", args.league)
            return 2
        league_ids_map = dc.load_league_ids(args.league_ids_config)
        all_lids = []
        seen = set()
        for level_ids in league_ids_map.get(args.league.lower(), {}).values():
            for lid in level_ids:
                if lid not in seen:
                    seen.add(lid)
                    all_lids.append(lid)
        cache_dir = None
        if not args.no_cache:
            cache_dir = args.cache_dir or (SCRIPT_DIR / args.league / "cache" / "stats")
        logger.info("Fetching stats for %d lids", len(all_lids))
        hitter_stats, pitcher_stats, _, _ = sapi.build_player_stats(
            base_url, target_year,
            cfg.get("year_weights", [0.55, 0.35, 0.10]),
            cfg.get("woba_weights", {}),
            lids=all_lids or None,
            target_lids=None,
            cache_dir=cache_dir,
        )

    all_hitters = []
    all_pitchers = []
    for level in levels_to_run:
        h, p = analyze_level(
            level, args, cfg, eval_rows, target_year, hitter_stats, pitcher_stats,
        )
        logger.info("Level %s: %d hitters, %d pitchers", level, len(h), len(p))
        all_hitters.extend(h)
        all_pitchers.extend(p)

    if not all_hitters and not all_pitchers:
        logger.error("No players found for org '%s' at levels %s", args.org, levels_to_run)
        return 2

    for h in all_hitters:
        h["_block"] = compute_block_status(h, all_hitters)
    for p in all_pitchers:
        p["_pitcher_rank"] = compute_pitcher_block(p, all_pitchers)

    surplus_by_pos = build_position_surplus(all_hitters)

    candidates = []
    for player in all_hitters + all_pitchers:
        is_block = is_blocked_hitter(player) or is_blocked_pitcher(player)
        is_cut = is_cut_watch_with_value(player)
        is_surplus = bool(player.get("_surplus_flag"))
        if not (is_block or is_cut or is_surplus):
            continue
        comp = float(player.get("composite") or 0)
        vos_pot = float(player.get("vos_potential") or 0)
        if comp < args.min_composite and vos_pot < LOTTERY_VOS_POT:
            continue
        player["_trade_value"] = compute_trade_value(player)
        player["_category"] = categorize(player)
        player["_reasons"] = build_reason_tags(player)
        player["_replacement"] = compute_replacement_names(
            player, all_hitters, all_pitchers, max_names=2,
        )
        candidates.append(player)

    logger.info(
        "Trade block: %d candidates (%d hitters, %d pitchers)",
        len(candidates),
        sum(1 for c in candidates if not c.get("is_pitcher")),
        sum(1 for c in candidates if c.get("is_pitcher")),
    )

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    org_slug = args.org.lower().replace(" ", "_")
    md_path = out_dir / f"{org_slug}_trade_block_{ts}.md"
    csv_path = out_dir / f"{org_slug}_trade_block_{ts}.csv"

    md = render_md(
        args.league, args.org, target_year,
        candidates, surplus_by_pos, all_hitters, all_pitchers,
    )
    md_path.write_text(md, encoding="utf-8")
    logger.info("Wrote %s", md_path)

    write_csv(csv_path, candidates)
    logger.info("Wrote %s", csv_path)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
