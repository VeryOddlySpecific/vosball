#!/usr/bin/env python3
"""
playoff_planner.py — Postseason re-optimization over the regular-season depth chart.

Regular-season strategy (depth_chart.py) optimizes a 26-man roster, a 5-man
rotation, and split-neutral lineups for a 162-game grind. October is different:

  * Rotation compresses — a best-of-5 leans on ~3 starters (the ace can come
    back on rest), a best-of-7 on ~4. Back-end starters move to the bullpen.
  * Bullpen leverage matters more — high-leverage arms throw a larger share of
    innings, so the pen is ranked/roled by the rp_leverage_v1 model (the same
    one bullpen_builder.py uses), blended with regular composite.
  * Lineups are matched to the *specific* opponent — each game uses our vs-LHP
    or vs-RHP card depending on the opponent's probable starter's handedness,
    with platoon-bench swap suggestions.
  * The roster is reconfigured — drop the surplus starter / long man, add a
    platoon bat or an extra high-leverage arm.

This script composes existing tooling rather than re-deriving it:
  * depth_chart.build_team_pool  — fetch + score both clubs on ONE league scale.
  * depth_chart.assign_positions / build_lineup — same starter + Tango logic.
  * bullpen_builder.*             — the leverage model + PlayerData join.
  * project_season.pythagenpat_wins — first-order series win probability.

The v10 weights (and the eval CSV they produce) carry no playoff/leverage/
matchup logic; all postseason tuning is applied here, downstream of scoring.

Usage:
  py playoff_planner.py --league ndl --org "Sugar Land Space Cowboys" \\
        --opponent "Philadelphia Cheesesteaks" --games 5
  py playoff_planner.py --league sahl --org "Houston Astros" \\
        --opponent "Atlanta Braves" --games 7 --home-team us
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
import logging
import math
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import depth_chart as dc
import bullpen_builder as bb
import project_season as ps
import stats as sapi

SCRIPT_DIR = Path(__file__).resolve().parent.parent
CONFIG_DIR = SCRIPT_DIR / "config"
DEFAULT_CONFIG = CONFIG_DIR / "depth_config.json"

logger = logging.getLogger("playoff_planner")

# Fallback rotation depth when the config doesn't list a series length.
_DEFAULT_SP_FOR_SERIES = {3: 2, 5: 3, 7: 4}


# -----------------------------------------------------------------------------
# CLI
# -----------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Optimize depth chart, rotation, bullpen, and lineups for a "
                    "specific best-of-N playoff opponent."
    )
    p.add_argument("--league", required=True, help="League slug (e.g. ndl, sahl).")
    p.add_argument("--org", required=True,
                   help="Our organization (name as it appears in the eval Org column, or a team code).")
    p.add_argument("--opponent", required=True,
                   help="Opponent organization (name or team code).")
    p.add_argument("--games", "--best-of", dest="games", type=int, required=True,
                   choices=[3, 5, 7], help="Series length (best-of-N).")
    p.add_argument("--level", default="ML",
                   help="Roster level to plan for (default: ML).")
    p.add_argument("--home-team", choices=["us", "them"], default="us",
                   help="Who holds home-field advantage (affects the win-prob nudge). Default: us.")
    p.add_argument("--leverage-weight", type=float, default=None,
                   help="Weight on leverage vs composite when ordering late-inning relievers: "
                        "oct_score = w*lev + (1-w)*composite. Default: playoff.leverage_weight in config.")
    p.add_argument("--alpha", type=float, default=None,
                   help="Leverage-model blend: lev = alpha*L1 + (1-alpha)*L2. Default: playoff.alpha in config.")
    p.add_argument("--no-win-prob", action="store_true",
                   help="Skip the series win-probability estimate.")
    # Forwarded to build_team_pool / the stats pipeline.
    p.add_argument("--year", type=int, default=None, help="Latest year for stats window (default: current year).")
    p.add_argument("--input", type=Path, default=None, help="Override evaluation_summary CSV (league-wide).")
    p.add_argument("--config", type=Path, default=DEFAULT_CONFIG, help="depth_config.json path.")
    p.add_argument("--league-url-config", type=Path, default=dc.DEFAULT_LEAGUE_URL)
    p.add_argument("--league-ids-config", type=Path, default=dc.DEFAULT_LEAGUE_IDS)
    p.add_argument("--base-url", type=str, default=None, help="Override league API base URL.")
    p.add_argument("--output-dir", type=Path, default=None, help="Output dir (default: {league}/playoff/).")
    p.add_argument("--no-archive", action="store_true", help="Keep prior playoff outputs alongside new ones.")
    p.add_argument("--no-stats", action="store_true", help="Skip stat fetch; composite uses VOS only (debug).")
    p.add_argument("--no-cache", action="store_true", help="Force fresh API fetches.")
    p.add_argument("--cache-dir", type=Path, default=None)
    p.add_argument("--all-levels", action="store_true", help="Fetch stats for every level (rarely needed).")
    p.add_argument("--lids", type=str, default=None, help="Comma-separated lid override.")
    p.add_argument("--no-players-override", action="store_true",
                   help="Skip the /players API reconciliation of Level/Org/Team.")
    p.add_argument("--players-override-csv", type=Path, default=None, action="append",
                   help="OOTP roster CSV patch on top of /players. Repeatable.")
    p.add_argument("--include-inactive", action="store_true",
                   help="Keep retired/DFA/waivered/DL60 players.")
    p.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    return p.parse_args()


# -----------------------------------------------------------------------------
# Handedness (PlayerData join — Bats/Throws don't live in the eval CSV)
# -----------------------------------------------------------------------------

def load_playerdata(league: str) -> Dict[str, Dict[str, str]]:
    """Return {ID: PlayerData row}. Empty dict (with a warning) if the file is
    missing — handedness and leverage scoring degrade gracefully rather than
    aborting the run."""
    path = bb.DATA_DIR / bb.PLAYER_DATA_TEMPLATE.format(league=league)
    if not path.is_file():
        logger.warning("PlayerData not found: %s — handedness & leverage scoring disabled.", path)
        return {}
    return bb.load_playerdata(league)


def handedness_map(playerdata: Dict[str, Dict[str, str]]) -> Dict[str, Dict[str, str]]:
    return {
        pid: {
            "bats": (r.get("Bats") or "").strip().upper(),
            "throws": (r.get("Throws") or "").strip().upper(),
        }
        for pid, r in playerdata.items()
    }


def attach_handedness(records: List[Dict[str, Any]], hand: Dict[str, Dict[str, str]]) -> None:
    """Stamp bats/throws onto each record (planner layer only — depth_chart's
    records intentionally never carry these)."""
    for r in records:
        h = hand.get(str(r.get("pid", "")), {})
        r["bats"] = h.get("bats", "")
        r["throws"] = h.get("throws", "")


# -----------------------------------------------------------------------------
# Org name resolution
# -----------------------------------------------------------------------------

def find_latest_eval(league: str, override: Optional[Path]) -> Path:
    """Newest league-wide eval CSV, searching BOTH the top-level eval dir AND the
    per-org subdirs.

    ``vos_v2 --per-org-evals`` (production mode) writes ONLY into per-org subdirs
    (``{league}/eval/{code}/``), leaving any top-level file stale. depth_chart's
    ``find_latest_eval(league, override, org_code=None)`` is non-recursive on the
    top level, so it silently returns the stale file. Per-org evals are
    league-wide (every org's players under one park's VOS view — VOS scores are
    park-invariant), so the newest by timestamp anywhere is the correct source
    for a two-team comparison. ``--input`` always wins.
    """
    if override is not None:
        if not override.exists():
            raise FileNotFoundError(f"--input not found: {override}")
        return override
    eval_root = dc.SCRIPT_DIR / league / "eval"
    pattern = f"evaluation_summary_{league}_*.csv"
    candidates = list(eval_root.glob(pattern)) + list(eval_root.glob(f"*/{pattern}"))
    if not candidates:
        raise FileNotFoundError(f"No evaluation_summary CSV under {eval_root}")
    # Newest by filename timestamp; on a tie prefer a nested (per-org) file,
    # since per-org-evals is the production write path.
    candidates.sort(key=lambda p: (p.name, p.parent != eval_root))
    return candidates[-1]


def resolve_org_name(raw: str, league: str) -> str:
    """Accept either a display name or a team code; return the canonical display
    name used in the eval Org column (reusing depth_chart's park-factors map)."""
    pf_path = dc._default_park_factors_path(league)
    name_to_code = dc._name_to_code_map(pf_path)
    if raw in name_to_code:
        return raw
    code_lookup = {c: n for n, c in name_to_code.items()}
    if raw.strip().lower() in code_lookup:
        resolved = code_lookup[raw.strip().lower()]
        logger.info("Resolved %r to %r.", raw, resolved)
        return resolved
    return raw


# -----------------------------------------------------------------------------
# Starters / bench (no min-comp gate — playoffs always field the best nine)
# -----------------------------------------------------------------------------

def starters_and_bench(
    hitter_pool: List[Dict[str, Any]], level_cfg: Dict[str, Any],
) -> Tuple[Dict[str, List[Dict[str, Any]]], Dict[str, Optional[Dict[str, Any]]],
           List[Dict[str, Any]], List[Dict[str, Any]], List[str]]:
    placed = dc.assign_positions(hitter_pool, level_cfg)
    starters_by_pos: Dict[str, Optional[Dict[str, Any]]] = {}
    for pos in dc.HITTER_POSITIONS:
        starters_by_pos[pos] = placed[pos][0] if placed.get(pos) else None
    starter_set = [v for v in starters_by_pos.values() if v]
    starter_pids = {p["pid"] for p in starter_set}
    bench: List[Dict[str, Any]] = []
    for slots in placed.values():
        for p in slots[1:]:
            if p["pid"] not in starter_pids:
                bench.append(p)
                starter_pids.add(p["pid"])
    missing = [pos for pos in dc.HITTER_POSITIONS if starters_by_pos.get(pos) is None]
    return placed, starters_by_pos, starter_set, bench, missing


# -----------------------------------------------------------------------------
# Rotation
# -----------------------------------------------------------------------------

def sp_for_series(games: int, cfg: Dict[str, Any]) -> int:
    pcfg = cfg.get("playoff", {})
    table = pcfg.get("sp_for_series", {})
    if str(games) in table:
        return int(table[str(games)])
    return _DEFAULT_SP_FOR_SERIES.get(games, max(2, games // 2 + 1))


def rotation_schedule(rotation: List[Dict[str, Any]], games: int) -> List[Dict[str, Any]]:
    """Assign a starter to each game by cycling the (compressed) rotation. Games
    that wrap past the rotation length are flagged ``projected`` — whether they're
    actually reached depends on how the series unfolds, and the back-end starter
    may be on short rest."""
    sched: List[Dict[str, Any]] = []
    n = len(rotation)
    for g in range(1, games + 1):
        sp = rotation[(g - 1) % n] if n else None
        sched.append({
            "game": g,
            "sp": sp,
            "throws": (sp.get("throws") if sp else "") or "",
            "projected": g > n,
        })
    return sched


def compress_rotation(
    pitcher_pool: List[Dict[str, Any]], games: int, cfg: Dict[str, Any],
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Return (rotation, bullpen_converts, schedule). Top ``sp_n`` SPs by composite
    form the rotation; the rest are tagged converted_sp and folded into the pen."""
    sp_n = sp_for_series(games, cfg)
    sp_pool = sorted((p for p in pitcher_pool if p.get("proj_role") == "SP"),
                     key=lambda p: -p["composite"])
    rotation = sp_pool[:sp_n]
    converts = sp_pool[sp_n:]
    for c in converts:
        c["converted_sp"] = True
    return rotation, converts, rotation_schedule(rotation, games)


# -----------------------------------------------------------------------------
# Bullpen leverage ladder
# -----------------------------------------------------------------------------

def leverage_models(weights: Dict[str, Any]) -> Optional[Tuple[Dict[str, Any], Dict[str, Any]]]:
    lev = weights.get("scoring_modes", {}).get("vos_leverage_rp_v1", {})
    l1 = lev.get("l1_leverage_role")
    l2 = lev.get("l2_career_avg_li")
    if not l1 or not l2:
        return None
    return l1, l2


def score_leverage(
    pen_pool: List[Dict[str, Any]],
    playerdata: Dict[str, Dict[str, str]],
    models: Optional[Tuple[Dict[str, Any], Dict[str, Any]]],
    alpha: float,
    w_lev: float,
) -> List[Dict[str, Any]]:
    """Annotate each reliever with lev_L1/lev_L2/lev_score/lev_role and an
    oct_score (October-weighted blend of leverage and composite). Pitchers absent
    from PlayerData (or when the model/PlayerData is unavailable) fall back to
    composite-only ordering."""
    l1m = l2m = None
    if models is not None:
        l1m, l2m = models
        l1_baseline = float(l1m.get("positive_rate", 0.187))
        l2_mean = float(l2m.get("target_mean", 0.92))
        l2_std = float(l2m.get("target_std", 0.28))

    n_missing = 0
    for p in pen_pool:
        raw = playerdata.get(str(p.get("pid", ""))) if playerdata else None
        if l1m is not None and raw is None:
            n_missing += 1
        if l1m is None or raw is None:
            p["lev_L1"] = None
            p["lev_L2"] = None
            p["lev_score"] = None
            p["lev_role"] = ""
            p["oct_score"] = round(float(p.get("composite", 50.0)), 1)
            continue
        feats = bb.extract_pitcher_features(raw)
        L1 = bb.prob_to_20_80(bb.apply_logistic(feats, l1m), l1_baseline)
        L2 = bb.li_to_20_80(bb.apply_ridge(feats, l2m), l2_mean, l2_std)
        lev = alpha * L1 + (1.0 - alpha) * L2
        p["lev_L1"] = round(L1, 1)
        p["lev_L2"] = round(L2, 1)
        p["lev_score"] = round(lev, 1)
        p["lev_role"] = bb.assign_role(lev)
        p["oct_score"] = round(w_lev * lev + (1.0 - w_lev) * float(p.get("composite", 50.0)), 1)

    if l1m is not None and playerdata and n_missing:
        logger.warning(
            "%d/%d relievers missing from PlayerData — leverage unavailable for them, "
            "ranked by composite only. Check PlayerData-{league}.csv is current (post-trade IDs).",
            n_missing, len(pen_pool),
        )
    pen_pool.sort(key=lambda p: -p["oct_score"])
    return pen_pool


def assign_pen_roles(pen_sorted: List[Dict[str, Any]], counts: Dict[str, int]) -> Dict[str, List[Dict[str, Any]]]:
    """Slot relievers into CL/SU/MR/LR in oct_score order. Converted starters tend
    to sort toward the bottom (their leverage score is lower), landing in LR."""
    roles: Dict[str, List[Dict[str, Any]]] = {"CL": [], "SU": [], "MR": [], "LR": []}
    i = 0
    for role in ("CL", "SU", "MR"):
        n = int(counts.get(role, {"CL": 1, "SU": 2, "MR": 4}[role]))
        roles[role] = pen_sorted[i:i + n]
        i += n
    roles["LR"] = pen_sorted[i:]
    return roles


# -----------------------------------------------------------------------------
# Opponent scouting
# -----------------------------------------------------------------------------

def opponent_lineup_tilt(
    opp_hitter_pool: List[Dict[str, Any]], level_cfg: Dict[str, Any],
) -> Dict[str, Any]:
    """Handedness profile of the opponent's projected starting nine. Drives our
    pitcher deployment and LOOGY/ROOGY decisions."""
    _, _, starter_set, _, _ = starters_and_bench(opp_hitter_pool, level_cfg)
    lhb = sum(1 for p in starter_set if p.get("bats") == "L")
    rhb = sum(1 for p in starter_set if p.get("bats") == "R")
    shb = sum(1 for p in starter_set if p.get("bats") == "S")
    # Switch hitters count toward the platoon advantage of whichever arm faces them,
    # so treat them as neutral here; lean is decided by the L/R imbalance.
    if lhb - rhb >= 2:
        lean = "L"
    elif rhb - lhb >= 2:
        lean = "R"
    else:
        lean = "balanced"
    return {"lhb": lhb, "rhb": rhb, "shb": shb, "lean": lean, "starters": starter_set}


def rotation_hand_majority(sched: List[Dict[str, Any]]) -> str:
    l = sum(1 for g in sched if (g.get("throws") or "").upper() == "L")
    r = sum(1 for g in sched if (g.get("throws") or "").upper() == "R")
    if l > r:
        return "L"
    if r > l:
        return "R"
    return "balanced"


# -----------------------------------------------------------------------------
# Per-game lineups + platoon swaps
# -----------------------------------------------------------------------------

def platoon_swaps(
    lineup: List[Tuple[int, Dict[str, Any]]],
    bench: List[Dict[str, Any]],
    split: str,
    margin: float,
) -> List[Dict[str, Any]]:
    """Flag bench bats whose same-side split beats the in-lineup starter's by
    ``margin`` (20-80 scale) at a position they can play."""
    swaps: List[Dict[str, Any]] = []
    used: set = set()
    for slot, starter in lineup:
        if starter.get("_lineup_gap"):
            continue
        pos = starter.get("_assigned_pos") or starter.get("primary_pos") or ""
        s_score = dc._split_score(starter, split)
        best: Optional[Dict[str, Any]] = None
        best_score = s_score + margin
        for b in bench:
            if b["pid"] in used:
                continue
            viable = ((b.get("pos_scores_blended") or {}).get(pos, 0.0) > 0.0
                      or b.get("primary_pos") == pos)
            if not viable:
                continue
            b_score = dc._split_score(b, split)
            if b_score >= best_score:
                best, best_score = b, b_score
        if best is not None:
            used.add(best["pid"])
            swaps.append({
                "slot": slot, "pos": pos, "out": starter, "in": best,
                "delta": round(dc._split_score(best, split) - s_score, 1),
            })
    return swaps


def per_game_plan(
    opp_sched: List[Dict[str, Any]],
    lineup_l: List[Tuple[int, Dict[str, Any]]],
    lineup_r: List[Tuple[int, Dict[str, Any]]],
    bench: List[Dict[str, Any]],
    margin: float,
) -> List[Dict[str, Any]]:
    plan: List[Dict[str, Any]] = []
    for g in opp_sched:
        throws = (g.get("throws") or "R").upper()
        use_l = throws == "L"
        split = "vs_l" if use_l else "vs_r"
        lineup = lineup_l if use_l else lineup_r
        plan.append({
            "game": g["game"],
            "opp_sp": g["sp"],
            "opp_throws": throws,
            "projected": g["projected"],
            "use_label": "vs LHP" if use_l else "vs RHP",
            "split": split,
            "lineup": lineup,
            "swaps": platoon_swaps(lineup, bench, split, margin),
        })
    return plan


# -----------------------------------------------------------------------------
# Roster reconfiguration
# -----------------------------------------------------------------------------

def playoff_roster_reconfig(
    pitcher_pool: List[Dict[str, Any]],
    rotation: List[Dict[str, Any]],
    pen_roles: Dict[str, List[Dict[str, Any]]],
    bench: List[Dict[str, Any]],
    level_cfg: Dict[str, Any],
    opp_rotation_hand: str,
    margin: float,
) -> Dict[str, Any]:
    """Compare the playoff staff to the regular-season staff and recommend the
    platoon bat to add. Returns added/dropped pitcher lists, the platoon-bat pick,
    and the assembled playoff staff."""
    reg = dc.assign_pitchers(pitcher_pool, level_cfg)
    reg_pids = {p["pid"] for slots in reg.values() for p in slots}

    pitcher_count = int(level_cfg.get("pitcher_count", 13))
    staff = list(rotation)
    for role in ("CL", "SU", "MR", "LR"):
        staff.extend(pen_roles.get(role, []))
    # De-dupe (a converted SP could appear once) and cap at the pitcher budget.
    seen: set = set()
    playoff_staff: List[Dict[str, Any]] = []
    for p in staff:
        if p["pid"] in seen:
            continue
        seen.add(p["pid"])
        playoff_staff.append(p)
    playoff_staff = playoff_staff[:pitcher_count]
    pl_pids = {p["pid"] for p in playoff_staff}

    added = [p for p in playoff_staff if p["pid"] not in reg_pids]
    dropped = [p for slots in reg.values() for p in slots if p["pid"] not in pl_pids]

    # Platoon bat: the bench bat with the biggest edge on the side we'll face most.
    split = "vs_l" if opp_rotation_hand == "L" else "vs_r"
    platoon_bat = None
    best_edge = margin
    for b in bench:
        same = dc._split_score(b, split)
        other = dc._split_score(b, "vs_r" if split == "vs_l" else "vs_l")
        edge = same - other
        if edge >= best_edge:
            platoon_bat, best_edge = b, edge
    return {
        "added": added,
        "dropped": dropped,
        "platoon_bat": platoon_bat,
        "platoon_split": split,
        "playoff_staff": playoff_staff,
    }


# -----------------------------------------------------------------------------
# Series win probability (first-order)
# -----------------------------------------------------------------------------

def _mean(vals: List[float]) -> float:
    vals = [v for v in vals if v is not None]
    return sum(vals) / len(vals) if vals else 0.0


def _league_rate_means(stats_bundle: Dict[str, Any]) -> Tuple[float, float]:
    """League-average wOBA and FIP from the full fetched pool. (0, 0) under
    --no-stats."""
    hitters = stats_bundle.get("hitters", {}) or {}
    pitchers = stats_bundle.get("pitchers", {}) or {}
    wobas = [float((v.get("overall") or {}).get("wOBA", 0.0) or 0.0)
             for v in hitters.values()
             if float((v.get("overall") or {}).get("PA", 0.0) or 0.0) >= 50]
    fips = [float((v.get("overall") or {}).get("FIP", 0.0) or 0.0)
            for v in pitchers.values()
            if float((v.get("overall") or {}).get("IP", 0.0) or 0.0) >= 20]
    wobas = [w for w in wobas if w > 0]
    fips = [f for f in fips if f > 0]
    return (_mean(wobas), _mean(fips))


def _team_woba(starter_set: List[Dict[str, Any]]) -> float:
    vals = [float((p.get("hitter_bundle") or {}).get("overall", {}).get("wOBA", 0.0) or 0.0)
            for p in starter_set]
    return _mean([v for v in vals if v > 0])


def _staff_fip(rotation: List[Dict[str, Any]], pen_roles: Dict[str, List[Dict[str, Any]]]) -> float:
    def fip(p: Dict[str, Any]) -> float:
        return float((p.get("pitcher_bundle") or {}).get("overall", {}).get("FIP", 0.0) or 0.0)
    rot = [fip(p) for p in rotation]
    pen = [fip(p) for r in ("CL", "SU", "MR") for p in pen_roles.get(r, [])]
    rot = [v for v in rot if v > 0]
    pen = [v for v in pen if v > 0]
    if rot and pen:
        return 0.6 * _mean(rot) + 0.4 * _mean(pen)
    return _mean(rot or pen)


def series_win_prob(
    us: Dict[str, Any], them: Dict[str, Any], stats_bundle: Dict[str, Any],
    games: int, home_us: bool,
) -> Dict[str, Any]:
    """Per-game win probability for us, then the best-of-N series probability and
    expected length. Uses a runs model (wOBA→RS, FIP→RA) when stats are present,
    else a composite-quality fallback. First-order — not a game simulation."""
    BASE_RPG = 4.5
    lg_woba, lg_fip = _league_rate_means(stats_bundle)

    method = ""
    our_w, our_f = _team_woba(us["starter_set"]), _staff_fip(us["rotation"], us["pen_roles"])
    opp_w, opp_f = _team_woba(them["starter_set"]), _staff_fip(them["rotation"], them["pen_roles"])

    if lg_woba > 0 and lg_fip > 0 and our_w > 0 and opp_w > 0 and our_f > 0 and opp_f > 0:
        method = "runs (wOBA→RS, FIP→RA)"
        our_rs = BASE_RPG * (our_w / lg_woba) * (opp_f / lg_fip)
        opp_rs = BASE_RPG * (opp_w / lg_woba) * (our_f / lg_fip)
        p_game, _x = ps.pythagenpat_wins(our_rs, opp_rs, 1)
    else:
        method = "ratings-only (no stats — rough estimate)"
        our_q = 0.5 * _mean([dc._split_score(p, "vs_r") for p in us["starter_set"]]) \
            + 0.5 * _mean([float(p["composite"]) for p in us["rotation"]])
        opp_q = 0.5 * _mean([dc._split_score(p, "vs_r") for p in them["starter_set"]]) \
            + 0.5 * _mean([float(p["composite"]) for p in them["rotation"]])
        p_game = 1.0 / (1.0 + math.exp(-(our_q - opp_q) / 10.0))

    # Home-field nudge: ~4% per-game edge to the home club, neutralized over a
    # symmetric series but it shifts the per-game baseline we report.
    p_game = max(0.05, min(0.95, p_game + (0.02 if home_us else -0.02)))

    need = games // 2 + 1
    # P(win series) = P(reach `need` wins before opponent does), games independent.
    p_series = sum(
        math.comb(need - 1 + k, k) * (p_game ** need) * ((1 - p_game) ** k)
        for k in range(need)
    )
    # Expected series length: sum over the clinching game of P(series ends there).
    exp_len = 0.0
    length_dist: Dict[int, float] = {}
    for total in range(need, 2 * need):
        losses = total - need
        # We clinch in `total` games.
        p_we = math.comb(total - 1, need - 1) * (p_game ** need) * ((1 - p_game) ** losses)
        # They clinch in `total` games.
        p_they = math.comb(total - 1, need - 1) * ((1 - p_game) ** need) * (p_game ** losses)
        length_dist[total] = p_we + p_they
        exp_len += total * (p_we + p_they)
    return {
        "method": method,
        "p_game": p_game,
        "p_series": p_series,
        "exp_length": exp_len,
        "length_dist": length_dist,
        "our_rates": (our_w, our_f),
        "opp_rates": (opp_w, opp_f),
    }


# -----------------------------------------------------------------------------
# Rendering
# -----------------------------------------------------------------------------

def _hand(p: Optional[Dict[str, Any]], key: str) -> str:
    return (p.get(key) if p else "") or "?"


def render_md(
    league: str, level: str, year: int, games: int,
    our_org: str, opp_org: str, home_us: bool,
    us: Dict[str, Any], them: Dict[str, Any],
    game_plan: List[Dict[str, Any]],
    reconfig: Dict[str, Any],
    win_prob: Optional[Dict[str, Any]],
) -> str:
    out: List[str] = []
    best_of = f"best-of-{games}"
    out.append(f"# Playoff Plan — {our_org} vs {opp_org}")
    out.append("")
    out.append(f"_{league.upper()} · {level} · {year} · {best_of} · "
               f"home: {'us' if home_us else 'them'} · generated {datetime.now():%Y-%m-%d %H:%M}_")
    out.append("")

    # 1. Series snapshot
    out.append("## Series Snapshot")
    out.append("")
    our_top_h = max(us["starter_set"], key=lambda p: p["composite"], default=None)
    opp_top_h = max(them["starter_set"], key=lambda p: p["composite"], default=None)
    our_ace = us["rotation"][0] if us["rotation"] else None
    opp_ace = them["rotation"][0] if them["rotation"] else None
    out.append("| | Top Hitter | Ace | Rotation depth |")
    out.append("| --- | --- | --- | --- |")

    def _f(p: Optional[Dict[str, Any]]) -> str:
        return f"{p['name']} ({p['composite']:.1f})" if p else "—"

    out.append(f"| **{our_org}** | {_f(our_top_h)} | {_f(our_ace)} | {len(us['rotation'])} SP |")
    out.append(f"| **{opp_org}** | {_f(opp_top_h)} | {_f(opp_ace)} | {len(them['rotation'])} SP |")
    out.append("")
    if win_prob:
        out.append(f"**Series win probability (us): {win_prob['p_series']*100:.0f}%** "
                   f"(per-game {win_prob['p_game']*100:.0f}%, expected length "
                   f"{win_prob['exp_length']:.1f} games)")
        out.append("")
        out.append(f"_Estimate method: {win_prob['method']}. First-order, not a simulation._")
        out.append("")

    # 2. Compressed rotation
    out.append("## Our Rotation (compressed)")
    out.append("")
    out.append("| Game | SP | Hand | Composite | Notes |")
    out.append("| --- | --- | --- | --- | --- |")
    starts_by_pid: Dict[str, int] = {}
    for g in us["schedule"]:
        sp = g["sp"]
        if sp:
            starts_by_pid[sp["pid"]] = starts_by_pid.get(sp["pid"], 0) + 1
    for g in us["schedule"]:
        sp = g["sp"]
        if not sp:
            out.append(f"| G{g['game']} | — | — | — | no starter available |")
            continue
        note = []
        if g["projected"]:
            note.append("if series reaches this game")
        if starts_by_pid.get(sp["pid"], 0) > 1 and not g["projected"]:
            note.append("starts again later")
        out.append(f"| G{g['game']} | {sp['name']} | {_hand(sp,'throws')} | "
                   f"{sp['composite']:.1f} | {'; '.join(note)} |")
    out.append("")
    if us["converts"]:
        names = ", ".join(f"{p['name']} ({p['composite']:.1f})" for p in us["converts"])
        out.append(f"_Starters shifted to the bullpen for this series: {names}._")
        out.append("")

    # 3. Leverage bullpen ladder
    out.append("## Our Bullpen — Leverage Ladder")
    out.append("")
    out.append("_Ordered by oct_score (October blend of leverage skill and composite). "
               "L1 = P(earns a leverage role), L2 = predicted avg leverage index._")
    out.append("")
    out.append("| Role | Pitcher | Hand | oct | lev | L1 | L2 | comp | prof | conv? |")
    out.append("| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |")
    for role in ("CL", "SU", "MR", "LR"):
        for p in us["pen_roles"].get(role, []):
            lev = "—" if p.get("lev_score") is None else f"{p['lev_score']:.1f}"
            l1 = "—" if p.get("lev_L1") is None else f"{p['lev_L1']:.1f}"
            l2 = "—" if p.get("lev_L2") is None else f"{p['lev_L2']:.1f}"
            out.append(f"| {role} | {p['name']} | {_hand(p,'throws')} | {p['oct_score']:.1f} | "
                       f"{lev} | {l1} | {l2} | {p['composite']:.1f} | {p.get('lev_role','')} | "
                       f"{'Y' if p.get('converted_sp') else ''} |")
    out.append("")
    # LOOGY/ROOGY note off opponent lineup tilt.
    tilt = them["tilt"]
    if tilt["lean"] in ("L", "R"):
        want = "L" if tilt["lean"] == "L" else "R"
        same_hand = [p for r in ("SU", "MR", "LR") for p in us["pen_roles"].get(r, [])
                     if (p.get("throws") or "").upper() == want and p.get("lev_score") is not None]
        same_hand.sort(key=lambda p: -(p.get("lev_score") or 0))
        side = "left-handed" if want == "L" else "right-handed"
        out.append(f"**Matchup note:** {opp_org}'s projected lineup leans "
                   f"{tilt['lhb']}L / {tilt['rhb']}R — a {side} specialist is valuable late.")
        if same_hand:
            out.append("  Top same-hand arms: "
                       + ", ".join(f"{p['name']} (lev {p['lev_score']:.0f})" for p in same_hand[:3]) + ".")
        out.append("")

    # 4. Per-game lineups
    out.append("## Per-Game Lineups")
    out.append("")
    for gp in game_plan:
        opp = gp["opp_sp"]
        opp_label = (f"{opp['name']} ({_hand(opp,'throws')}HP)" if opp else "TBD")
        proj = " _(projected)_" if gp["projected"] else ""
        out.append(f"### Game {gp['game']} — vs {opp_label}{proj} · our {gp['use_label']} lineup")
        out.append("")
        out.append("| # | Batter | Pos | B | wOBA |")
        out.append("| --- | --- | --- | --- | --- |")
        split_key = "vs_l" if gp["split"] == "vs_l" else "vs_r"
        for slot, p in gp["lineup"]:
            if p.get("_lineup_gap"):
                out.append(f"| {slot} | — | {p.get('_assigned_pos','')} | — | — |")
                continue
            wb = (p.get("hitter_bundle") or {}).get(split_key, {})
            out.append(f"| {slot} | {p['name']} | "
                       f"{p.get('_assigned_pos', p.get('primary_pos',''))} | "
                       f"{_hand(p,'bats')} | {wb.get('wOBA', 0):.3f} |")
        if gp["swaps"]:
            out.append("")
            out.append("_Platoon swaps to consider:_")
            for s in gp["swaps"]:
                out.append(f"  - {s['pos']}: **{s['in']['name']}** ({_hand(s['in'],'bats')}) "
                           f"for {s['out']['name']} (+{s['delta']:.1f} {gp['use_label']})")
        out.append("")

    # 5. Bench / platoon plan
    out.append("## Bench / Platoon Plan")
    out.append("")
    if us["bench"]:
        out.append("| Name | Pos | B | vs L | vs R | Composite |")
        out.append("| --- | --- | --- | --- | --- | --- |")
        for p in us["bench"]:
            out.append(f"| {p['name']} | {p.get('primary_pos','')} | {_hand(p,'bats')} | "
                       f"{dc._split_score(p,'vs_l'):.1f} | {dc._split_score(p,'vs_r'):.1f} | "
                       f"{p['composite']:.1f} |")
    else:
        out.append("_No bench bats available in the pool._")
    out.append("")

    # 6. Roster reconfig
    out.append("## Roster Reconfiguration vs Regular Season")
    out.append("")
    if reconfig["added"]:
        out.append("**Added to staff:** " + ", ".join(
            f"{p['name']} ({p.get('lev_role') or p.get('proj_role','')})" for p in reconfig["added"]))
        out.append("")
    if reconfig["dropped"]:
        out.append("**Dropped from staff:** " + ", ".join(
            f"{p['name']} ({p.get('proj_role','')})" for p in reconfig["dropped"]))
        out.append("")
    pb = reconfig.get("platoon_bat")
    if pb is not None:
        side = "LHP" if reconfig["platoon_split"] == "vs_l" else "RHP"
        out.append(f"**Suggested platoon bat:** {pb['name']} ({_hand(pb,'bats')}) — strong vs {side} "
                   f"({dc._split_score(pb, reconfig['platoon_split']):.1f} on that side).")
        out.append("")

    # 7. Opponent scouting
    out.append("## Opponent Scouting — " + opp_org)
    out.append("")
    out.append("**Probable rotation:**")
    out.append("")
    out.append("| Game | SP | Hand | Composite |")
    out.append("| --- | --- | --- | --- |")
    for g in them["schedule"]:
        sp = g["sp"]
        proj = " *(proj.)*" if g["projected"] else ""
        if not sp:
            out.append(f"| G{g['game']}{proj} | — | — | — |")
            continue
        out.append(f"| G{g['game']}{proj} | {sp['name']} | {_hand(sp,'throws')} | "
                   f"{sp['composite']:.1f} |")
    out.append("")
    out.append(f"**Lineup tilt:** {tilt['lhb']} LHB / {tilt['rhb']} RHB / {tilt['shb']} SHB "
               f"(lean: {tilt['lean']}).")
    out.append("")
    opp_pen_top = sorted(
        (p for r in ("CL", "SU", "MR") for p in them["pen_roles"].get(r, [])),
        key=lambda p: -(p.get("lev_score") or p.get("composite", 0.0)),
    )[:4]
    if opp_pen_top:
        out.append("**Top leverage arms:** " + ", ".join(
            f"{p['name']} ({_hand(p,'throws')}, "
            f"lev {p['lev_score']:.0f})" if p.get("lev_score") is not None
            else f"{p['name']} ({_hand(p,'throws')}, comp {p['composite']:.0f})"
            for p in opp_pen_top))
        out.append("")
    return "\n".join(out)


def write_csv(path: Path, us: Dict[str, Any], game_plan: List[Dict[str, Any]], games: int) -> None:
    fields = ["pid", "name", "playoff_role", "bats", "throws", "composite",
              "oct_score", "lev_score", "lev_L1", "lev_L2", "lev_role", "converted_sp",
              "split_vs_l", "split_vs_r"]
    start_cols = [f"start_g{g}" for g in range(1, games + 1)]
    fields += start_cols

    # Which game(s) each lineup hitter starts (by chosen per-game lineup).
    starts: Dict[str, set] = {}
    for gp in game_plan:
        for _slot, p in gp["lineup"]:
            if not p.get("_lineup_gap"):
                starts.setdefault(p["pid"], set()).add(gp["game"])

    rows: List[Dict[str, Any]] = []

    def _row(p: Dict[str, Any], role: str) -> Dict[str, Any]:
        r = {
            "pid": p.get("pid", ""), "name": p.get("name", ""), "playoff_role": role,
            "bats": p.get("bats", ""), "throws": p.get("throws", ""),
            "composite": round(float(p.get("composite", 0.0)), 2),
            "oct_score": p.get("oct_score", ""),
            "lev_score": p.get("lev_score", "") if p.get("lev_score") is not None else "",
            "lev_L1": p.get("lev_L1", "") if p.get("lev_L1") is not None else "",
            "lev_L2": p.get("lev_L2", "") if p.get("lev_L2") is not None else "",
            "lev_role": p.get("lev_role", ""),
            "converted_sp": "Y" if p.get("converted_sp") else "",
            "split_vs_l": round(dc._split_score(p, "vs_l"), 2) if not p.get("is_pitcher") else "",
            "split_vs_r": round(dc._split_score(p, "vs_r"), 2) if not p.get("is_pitcher") else "",
        }
        for g in range(1, games + 1):
            r[f"start_g{g}"] = "1" if g in starts.get(p["pid"], set()) else ""
        return r

    for i, p in enumerate(us["rotation"], start=1):
        rows.append(_row(p, f"SP{i}"))
    for role in ("CL", "SU", "MR", "LR"):
        for p in us["pen_roles"].get(role, []):
            rows.append(_row(p, role))
    seen = {r["pid"] for r in rows}
    for p in us["starter_set"]:
        if p["pid"] not in seen:
            rows.append(_row(p, "LINEUP"))
            seen.add(p["pid"])
    for p in us["bench"]:
        if p["pid"] not in seen:
            rows.append(_row(p, "BENCH"))
            seen.add(p["pid"])

    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


# -----------------------------------------------------------------------------
# Per-team assembly
# -----------------------------------------------------------------------------

def build_team_plan(
    pool: Dict[str, Any], games: int, cfg: Dict[str, Any],
    playerdata: Dict[str, Dict[str, str]],
    lev_models: Optional[Tuple[Dict[str, Any], Dict[str, Any]]],
    alpha: float, w_lev: float,
) -> Dict[str, Any]:
    """Assemble the postseason picture for one club from its scored pool."""
    level_cfg = pool["level_cfg"]
    hitter_pool = pool["hitter_pool"]
    pitcher_pool = pool["pitcher_pool"]

    placed, starters_by_pos, starter_set, bench, missing = starters_and_bench(hitter_pool, level_cfg)
    lineup_l = dc.build_lineup(starter_set, "vs_l", missing_positions=missing)
    lineup_r = dc.build_lineup(starter_set, "vs_r", missing_positions=missing)

    rotation, converts, schedule = compress_rotation(pitcher_pool, games, cfg)

    # Bullpen = existing RPs + converted starters.
    pen_pool = [p for p in pitcher_pool if p.get("proj_role") == "RP"] + list(converts)
    score_leverage(pen_pool, playerdata, lev_models, alpha, w_lev)
    counts = cfg.get("playoff", {}).get("leverage_role_counts", {"CL": 1, "SU": 2, "MR": 4, "LR": 1})
    pen_roles = assign_pen_roles(pen_pool, counts)

    tilt = opponent_lineup_tilt(hitter_pool, level_cfg)

    return {
        "level_cfg": level_cfg,
        "placed": placed,
        "starters_by_pos": starters_by_pos,
        "starter_set": starter_set,
        "bench": bench,
        "missing": missing,
        "lineup_l": lineup_l,
        "lineup_r": lineup_r,
        "rotation": rotation,
        "converts": converts,
        "schedule": schedule,
        "pen_pool": pen_pool,
        "pen_roles": pen_roles,
        "tilt": tilt,
    }


# -----------------------------------------------------------------------------
# main
# -----------------------------------------------------------------------------

def main() -> int:
    args = parse_args()
    logging.basicConfig(level=getattr(logging, args.log_level), format="%(levelname)s: %(message)s")

    cfg = dc.load_config(args.config)
    pcfg = cfg.get("playoff", {})
    alpha = args.alpha if args.alpha is not None else float(pcfg.get("alpha", bb.DEFAULT_ALPHA))
    w_lev = args.leverage_weight if args.leverage_weight is not None else float(pcfg.get("leverage_weight", 0.5))
    level = args.level.strip().upper()
    if level not in cfg["levels"]:
        logger.error("Level '%s' not in %s. Available: %s", level, args.config, ", ".join(cfg["levels"].keys()))
        return 2

    # Default to the league's in-game season (league_settings.json) — see
    # depth_chart.league_default_year. Without it, the stats window misses the
    # live season and wOBA/FIP-driven features fall back to ratings-only.
    settings_year = dc.league_default_year(args.league)
    target_year = args.year or settings_year or datetime.now().year
    if not args.year and settings_year:
        logger.info("Using in-game season %d from league_settings.json (pass --year to override).", settings_year)
    our_org = resolve_org_name(args.org, args.league)
    opp_org = resolve_org_name(args.opponent, args.league)
    if our_org.strip().lower() == opp_org.strip().lower():
        logger.error("--org and --opponent are the same team (%s).", our_org)
        return 2

    # Leverage model + PlayerData handedness join (shared by both clubs).
    weights = bb.load_leverage_weights()
    lev_models = leverage_models(weights)
    if lev_models is None:
        logger.warning("Leverage weights missing l1/l2 models — bullpen falls back to composite ordering.")
    playerdata = load_playerdata(args.league)
    hand = handedness_map(playerdata)

    # League-wide eval (contains every org). Searches per-org subdirs too, so a
    # fresh --per-org-evals run isn't missed in favor of a stale top-level file.
    try:
        eval_path = find_latest_eval(args.league, args.input)
    except FileNotFoundError as e:
        logger.error("%s", e)
        return 2
    logger.info("Using eval file: %s", eval_path)
    eval_rows = dc.read_eval(eval_path)

    players_lookup: Dict[str, Dict[str, str]] = {}
    level_id_to_label: Dict[int, str] = {}
    team_id_to_name: Dict[int, str] = {}
    if not args.no_players_override and not args.no_stats:
        base_url = sapi.resolve_base_url(args.league, args.base_url, args.league_url_config)
        if base_url:
            cache_dir = None if args.no_cache else (args.cache_dir or (dc.SCRIPT_DIR / args.league / "cache" / "stats"))
            players_lookup = sapi.build_players_lookup(base_url, cache_dir=cache_dir)
            if players_lookup:
                level_id_to_label = dc.load_level_id_to_label()
                team_id_to_name = dc.load_team_id_to_name(args.league)
                dc.apply_players_override(
                    eval_rows, players_lookup, level_id_to_label, team_id_to_name,
                    include_inactive=args.include_inactive,
                )
                logger.info("Reconciled eval against /players (%d players).", len(players_lookup))

    # CSV roster patch on top of /players — reflects in-app moves between sims.
    if args.players_override_csv:
        if not level_id_to_label:
            level_id_to_label = dc.load_level_id_to_label()
        if not team_id_to_name:
            team_id_to_name = dc.load_team_id_to_name(args.league)
        csv_patch = dc.build_players_lookup_from_csv(
            args.players_override_csv, dc.invert_team_id_to_name(team_id_to_name))
        if csv_patch:
            players_lookup.update(csv_patch)
            dc.apply_players_override(
                eval_rows, players_lookup, level_id_to_label, team_id_to_name,
                include_inactive=args.include_inactive,
            )
            logger.info("Applied CSV roster patch (%d players).", len(csv_patch))

    # Build both clubs off ONE shared stats fetch so they share a z-score scale.
    args.org = our_org
    us_pool = dc.build_team_pool(level, args, cfg, eval_rows, target_year)
    if us_pool is None:
        logger.error("Could not build pool for %s.", our_org)
        return 2
    shared_stats = us_pool["stats"]

    args.org = opp_org
    opp_pool = dc.build_team_pool(level, args, cfg, eval_rows, target_year, shared_stats=shared_stats)
    if opp_pool is None:
        logger.error("Could not build pool for %s.", opp_org)
        return 2

    if not us_pool["hitter_pool"] and not us_pool["pitcher_pool"]:
        logger.error("No players found for %s at %s — check the org name / eval.", our_org, level)
        return 2
    if not opp_pool["hitter_pool"] and not opp_pool["pitcher_pool"]:
        logger.error("No players found for %s at %s — check the opponent name / eval.", opp_org, level)
        return 2

    # Stamp handedness onto every record up front.
    for pool in (us_pool, opp_pool):
        attach_handedness(pool["hitter_pool"], hand)
        attach_handedness(pool["pitcher_pool"], hand)

    us = build_team_plan(us_pool, args.games, cfg, playerdata, lev_models, alpha, w_lev)
    them = build_team_plan(opp_pool, args.games, cfg, playerdata, lev_models, alpha, w_lev)

    # Per-game lineups vs the opponent's probable starters.
    margin = float(pcfg.get("platoon_swap_margin", 3.0))
    game_plan = per_game_plan(them["schedule"], us["lineup_l"], us["lineup_r"], us["bench"], margin)

    # Roster reconfiguration.
    opp_hand = rotation_hand_majority(them["schedule"])
    reconfig = playoff_roster_reconfig(
        us_pool["pitcher_pool"], us["rotation"], us["pen_roles"], us["bench"],
        us["level_cfg"], opp_hand, margin,
    )

    # Series win probability.
    win_prob = None
    if not args.no_win_prob:
        win_prob = series_win_prob(us, them, shared_stats, args.games, home_us=(args.home_team == "us"))

    # Output.
    out_dir = args.output_dir or (dc.SCRIPT_DIR / args.league / "playoff")
    out_dir.mkdir(parents=True, exist_ok=True)
    if not args.no_archive:
        moved, archive_dir = dc.archive_previous_runs(out_dir)
        if moved:
            logger.info("Archived %d prior playoff file(s) to %s", moved, archive_dir)

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    slug = f"{our_org.lower().replace(' ', '_')}_vs_{opp_org.lower().replace(' ', '_')}_bo{args.games}_{ts}"
    md_path = out_dir / f"{slug}.md"
    csv_path = out_dir / f"{slug}.csv"

    md = render_md(args.league, level, target_year, args.games, our_org, opp_org,
                   args.home_team == "us", us, them, game_plan, reconfig, win_prob)
    md_path.write_text(md, encoding="utf-8")
    logger.info("Wrote %s", md_path)

    write_csv(csv_path, us, game_plan, args.games)
    logger.info("Wrote %s", csv_path)

    # Console summary.
    if win_prob:
        print(f"\n  {our_org} vs {opp_org} (best-of-{args.games}): "
              f"series win {win_prob['p_series']*100:.0f}% "
              f"(per-game {win_prob['p_game']*100:.0f}%)", file=sys.stderr)
    print(f"  Rotation: " + ", ".join(
        f"G{g['game']}={g['sp']['name']}({_hand(g['sp'],'throws')})" for g in us["schedule"] if g["sp"]),
        file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
