"""VOSBall web UI — Depth Charts page.

Pick a team (org), choose a level (ML / AAA / … / R) from preview cards, and see
the suggested position depth, lineup(s), and pitching staff for that level.

A pure consumer of depth_chart.py's in-process slotting (no files written) and of
the eval rows the Eval Browser already produced (st.session_state['result']).
VOS-ratings-only by default (fully offline); an opt-in toggle blends in-season
stats from the StatsPlus API and unlocks true vs-LHP / vs-RHP lineup splits.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

# Make the repo root importable (depth_chart.py + config live there). Mirrors
# app.py's path setup so this page works under `streamlit run webapp/app.py`.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import streamlit as st  # noqa: E402
import pandas as pd  # noqa: E402

from depth_chart import (  # noqa: E402
    build_team_pool, org_pool, assign_positions, assign_pitchers, build_lineup,
    build_position_depth_table, load_config, league_default_year,
    HITTER_POSITIONS, DEFAULT_CONFIG, DEFAULT_LEAGUE_URL, DEFAULT_LEAGUE_IDS,
)
from state import active_result, active_league  # noqa: E402  (per-league silo)
from scoring import autorun_result  # noqa: E402  (auto-score on first visit)

CONFIG_DIR = ROOT / "config"
BULLPEN_ROLES = ["CL", "SU", "MR", "LR"]


# --- data layer -------------------------------------------------------------

def orgs_for_league(league: str, rows: List[Dict[str, Any]]) -> List[str]:
    """Org names from config/{league}_orgs.json, else distinct eval-row Orgs."""
    p = CONFIG_DIR / f"{league}_orgs.json"
    if p.exists():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, list) and data:
                return [str(x) for x in data]
        except (OSError, ValueError):
            pass
    return sorted({str(r.get("Org", "")).strip()
                   for r in rows if str(r.get("Org", "")).strip()})


def user_org_for_league(league: str) -> Optional[str]:
    """The org the user plays as for this league, from config/league_settings.json
    (the same `org` key run_depth_chart_all.py / player_card.py read). None if
    the file/entry/org is absent."""
    p = CONFIG_DIR / "league_settings.json"
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    entry = data.get(league) if isinstance(data, dict) else None
    if isinstance(entry, dict) and isinstance(entry.get("org"), str) and entry["org"].strip():
        return entry["org"].strip()
    return None


def depth_levels() -> List[str]:
    try:
        return list(load_config(DEFAULT_CONFIG).get("levels", {})) or \
            ["ML", "AAA", "AA", "A+", "A", "A-", "R"]
    except Exception:  # noqa: BLE001
        return ["ML", "AAA", "AA", "A+", "A", "A-", "R"]


def org_level_counts(rows: List[Dict[str, Any]], org: str,
                     levels: List[str]) -> Dict[str, int]:
    """Player count per level for one org (same filter build_team_pool uses)."""
    return {lvl: len(org_pool(rows, org, lvl)) for lvl in levels}


@st.cache_data(show_spinner=False)
def build_depth(_eval_rows, eval_sig, league, org, level, use_stats, target_year):
    """Build a level's depth chart as plain data (no file writes).

    `_eval_rows` is un-hashed (leading underscore — Streamlit convention);
    `eval_sig` is the real cache key. Returns a dict of artifacts, or
    {'error': msg} so the caller can surface a friendly message.
    """
    try:
        cfg = load_config(DEFAULT_CONFIG)
        # Minimal Namespace mirroring CompositeContext's (depth_chart.py:2316).
        args = argparse.Namespace(
            league=league, org=org, base_url=None,
            league_url_config=DEFAULT_LEAGUE_URL, league_ids_config=DEFAULT_LEAGUE_IDS,
            all_levels=False, lids=None, no_cache=False,
            no_stats=(not use_stats), cache_dir=None,
        )
        pool = build_team_pool(level, args, cfg, _eval_rows, target_year)
        if pool is None:
            return {"error": f"Couldn't build a pool for {org} at {level} "
                             "(missing level config, or no API base URL when stats are on)."}
        level_cfg = pool["level_cfg"]
        placed = assign_positions(pool["hitter_pool"], level_cfg)
        pslots = assign_pitchers(pool["pitcher_pool"], level_cfg)
        starters_by_pos = {pos: (placed[pos][0] if placed.get(pos) else None)
                           for pos in HITTER_POSITIONS}
        starters = [starters_by_pos[pos] for pos in HITTER_POSITIONS if starters_by_pos[pos]]
        util_count = int(level_cfg.get("util_count_per_pos", 2))
        return {
            "depth_table": build_position_depth_table(
                pool["hitter_pool"], starters_by_pos, util_count=util_count),
            "lineup_r": build_lineup(starters, "vs_r"),
            "lineup_l": build_lineup(starters, "vs_l"),
            # Map a starter's id to the position he's slotted at, so the lineup
            # shows the fielding slot (not his raw primary_pos, which can repeat).
            "pos_by_pid": {sp["pid"]: pos for pos, sp in starters_by_pos.items() if sp},
            "pitcher_slots": pslots,
            "util_count": util_count,
            "n_hitters": len(pool["hitter_pool"]),
            "n_pitchers": len(pool["pitcher_pool"]),
            "used_stats": use_stats,
        }
    except Exception as e:  # noqa: BLE001 — surface, don't crash the page
        return {"error": f"{type(e).__name__}: {e}"}


# --- render helpers ---------------------------------------------------------

def _name(p) -> str:
    return (p or {}).get("name", "—") or "—"


def _comp(p) -> str:
    try:
        return f"{float((p or {}).get('composite')):.1f}"
    except (TypeError, ValueError):
        return "—"


def _lineup_df(lineup, pos_by_pid=None) -> pd.DataFrame:
    pos_by_pid = pos_by_pid or {}
    return pd.DataFrame([
        {"#": slot, "Player": _name(p),
         "Pos": pos_by_pid.get((p or {}).get("pid"), (p or {}).get("primary_pos", "")),
         "Comp": _comp(p)}
        for slot, p in lineup
    ])


def _depth_df(depth_table, util_count) -> pd.DataFrame:
    out = []
    for pos in HITTER_POSITIONS:
        slot = depth_table.get(pos, {})
        row = {"Pos": pos, "Starter": _name(slot.get("starter"))}
        for i in range(util_count):
            row[f"Util{i + 1}"] = _name(slot.get(f"util{i + 1}"))
        row["Def Sub"] = _name(slot.get("def_sub"))
        out.append(row)
    return pd.DataFrame(out)


def _rotation_df(pslots) -> pd.DataFrame:
    return pd.DataFrame([
        {"Slot": f"SP{i + 1}", "Pitcher": _name(p), "Comp": _comp(p)}
        for i, p in enumerate(pslots.get("SP", []))
    ])


def _bullpen_df(pslots) -> pd.DataFrame:
    out = []
    for role in BULLPEN_ROLES:
        bucket = pslots.get(role, [])
        for i, p in enumerate(bucket):
            label = f"{role}{i + 1}" if len(bucket) > 1 else role
            out.append({"Role": label, "Pitcher": _name(p), "Comp": _comp(p)})
    return pd.DataFrame(out)


# --- page -------------------------------------------------------------------

def page() -> None:
    result = active_result() or autorun_result(active_league())
    if not result or not result.get("rows"):
        st.info("Run an evaluation on the **Eval Browser** page first — depth "
                "charts use that league's scored players.")
        return
    league, rows = result["league"], result["rows"]
    orgs = orgs_for_league(league, rows)
    if not orgs:
        st.warning(f"No organizations found for {league.upper()}.")
        return

    # Default the org selector to the team the user plays as (from
    # league_settings.json). Re-seed only when the loaded league changes, so a
    # manual pick sticks within a league and a stale org from another league
    # never lands in the selectbox's options (which would error).
    if st.session_state.get("_depth_org_league") != league:
        st.session_state["_depth_org_league"] = league
        default_org = user_org_for_league(league)
        if default_org in orgs:
            st.session_state["depth_org"] = default_org
        elif st.session_state.get("depth_org") not in orgs:
            st.session_state.pop("depth_org", None)

    with st.sidebar:
        st.header("Depth Charts")
        org = st.selectbox("Organization", orgs, key="depth_org")
        use_stats = st.checkbox(
            "Blend in-season stats (network)", value=False,
            help="Fetch StatsPlus in-season stats to blend with VOS and enable "
                 "true vs-LHP/vs-RHP lineup splits. Needs network access and "
                 "the league in config/league_ids.json.")

    st.subheader(f"{org} — depth chart")

    levels = depth_levels()
    counts = org_level_counts(rows, org, levels)
    selected = st.session_state.get("depth_level")

    # Level preview cards.
    cols = st.columns(len(levels))
    for c, lvl in zip(cols, levels):
        n = counts.get(lvl, 0)
        with c.container(border=True):
            st.markdown(f"**{lvl}**" + (" ✓" if lvl == selected else ""))
            st.caption(f"{n} player{'' if n == 1 else 's'}")
            if st.button("View", key=f"lvlbtn_{lvl}", use_container_width=True,
                         disabled=(n == 0)):
                st.session_state["depth_level"] = lvl
                selected = lvl

    level = st.session_state.get("depth_level")
    if not level or counts.get(level, 0) == 0:
        st.info("Pick a level above to see its depth chart.")
        return

    target_year = league_default_year(league) or datetime.now().year
    eval_sig = "|".join(str(result.get(k)) for k in
                        ("league", "rating_scale", "draft", "apply_park", "contracts"))
    eval_sig += f"|{org}|{level}|{use_stats}"
    with st.spinner(f"Building {org} {level} depth chart…"):
        depth = build_depth(rows, eval_sig, league, org, level, use_stats, target_year)
    if "error" in depth:
        st.error(depth["error"])
        return

    st.markdown(f"### {level} · {depth['n_hitters']} hitters · {depth['n_pitchers']} pitchers")
    if depth["used_stats"]:
        st.caption("Composite blends VOS ratings with in-season stats; lineups "
                   "are split by handedness.")
    else:
        st.caption("VOS-ratings-only (no in-season stats) — vs-LHP / vs-RHP "
                   "lineups are identical here; flip **Blend in-season stats** "
                   "for real splits. Roster reflects the eval snapshot.")

    # Lineup
    st.markdown("**Lineup**")
    pbp = depth["pos_by_pid"]
    if depth["used_stats"]:
        a, b = st.columns(2)
        a.caption("vs RHP")
        a.dataframe(_lineup_df(depth["lineup_r"], pbp), hide_index=True, use_container_width=True)
        b.caption("vs LHP")
        b.dataframe(_lineup_df(depth["lineup_l"], pbp), hide_index=True, use_container_width=True)
    else:
        st.dataframe(_lineup_df(depth["lineup_r"], pbp), hide_index=True, use_container_width=True)

    # Position depth
    st.markdown("**Position depth**")
    st.dataframe(_depth_df(depth["depth_table"], depth["util_count"]),
                 hide_index=True, use_container_width=True)

    # Pitching staff
    st.markdown("**Pitching staff**")
    a, b = st.columns(2)
    a.caption("Rotation")
    a.dataframe(_rotation_df(depth["pitcher_slots"]), hide_index=True, use_container_width=True)
    b.caption("Bullpen")
    b.dataframe(_bullpen_df(depth["pitcher_slots"]), hide_index=True, use_container_width=True)
