"""VOSBall web UI — Trade Targets page (the league trade block, sorted by need).

Pulls the league-wide /tradeblock list and scores every available player against
*your* org's needs, so the block is ranked as a shopping list: biggest holes and
best-fit acquisitions first. A consumer of the trade_targets core
(trade_targets.build_trade_targets) — the same pipeline the CLI report uses, so
the two stay in lockstep.

Full CLI fidelity (the user's choice): this applies the /players override and
fetches stats, so composites/fits match a real `trade_targets.py` run. The first
visit per league pays the stat fetch; it's cached after (and stats disk-cache
under {league}/cache/stats, so even a cache miss is disk-speed, not network).

Auth: /tradeblock is reached with the configured API token (config/
statsplus_tokens.json) via ?token= — no cookie needed. See trade_targets.fetch_
tradeblock / resolve_token.
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import streamlit as st  # noqa: E402
import pandas as pd  # noqa: E402

import stats as sapi  # noqa: E402  (/players override + stat endpoints)
import depth_chart as dc  # noqa: E402
import trade_targets as tt  # noqa: E402  (the scoring core + tradeblock fetch)
from state import active_result, active_league  # noqa: E402  (per-league silo)
from scoring import autorun_result  # noqa: E402  (auto-score on first visit)
# Reuse the depth page's org helpers rather than duplicating them.
from depth import orgs_for_league, user_org_for_league  # noqa: E402

# Display order for the shopping-list sections (mirrors trade_targets.render_md).
SECTION_ORDER = [
    ("Priority Target", "Premium-composite players who fill a Critical or Major "
                        "hole — call about these first."),
    ("Need Fit", "Solid pieces that address a Critical or Major hole."),
    ("Depth Add", "Fit a Depth-tier need — cost-controlled insurance."),
    ("Lottery", "Young, available, high VOS_Pot. Cheap fliers."),
    ("Premium (no need)", "High-composite but you grade the position as Set — "
                          "track in case the market lets you flip them."),
    ("Pass", "Below the floor and not a need — shown for completeness."),
]
TIER_ORDER = {"Critical": 0, "Major": 1, "Depth": 2, "Set": 3}


# --- data layer -------------------------------------------------------------

@st.cache_data(show_spinner=False)
def fetch_block_pids(league: str) -> Dict[str, Any]:
    """The league-wide /tradeblock pid list, token-authed and disk-cached per day.

    Returns {'pids': [...], 'had_token': bool, 'has_base_url': bool}. Empty pids
    can mean an empty block *or* an auth failure — the caller distinguishes via
    had_token. Cached for the session; the sidebar's refresh button clears it.
    """
    base_url = sapi.resolve_base_url(league, None, tt.DEFAULT_LEAGUE_URL)
    if not base_url:
        return {"pids": [], "had_token": False, "has_base_url": False}
    token = tt.resolve_token(league)
    cache_dir = ROOT / league / "cache" / "stats"
    pids = tt.fetch_tradeblock(base_url, cache_dir=cache_dir, token=token)
    return {"pids": pids, "had_token": bool(token), "has_base_url": True}


@st.cache_data(show_spinner=False)
def build_targets_context(_eval_rows, eval_sig, league, org, year, block_sig, _pids):
    """Score the whole tradeblock pool against ``org``'s needs (no UI filtering).

    Replicates the CLI pipeline: copy the silo rows (never mutate the shared
    eval), apply the /players override, fetch stats, then run the core with the
    floors wide open (min_composite=0, include_no_need=True) so the page can
    apply its own min-composite / no-need controls cheaply on top. Cached on
    eval_sig + org + block_sig — the heavy fetch only re-runs when one changes.

    Returns artifacts or {'error': msg}.
    """
    try:
        cfg = dc.load_config(tt.DEFAULT_CONFIG)
        levels = list(cfg["levels"].keys())

        # Copy so the /players override never touches the shared silo rows.
        rows = [dict(r) for r in _eval_rows]

        base_url = sapi.resolve_base_url(league, None, tt.DEFAULT_LEAGUE_URL)
        cache_dir = ROOT / league / "cache" / "stats"

        # /players override — current Level/Org/Team + retired/DFA filtering, so
        # ML/AAA scoping and "who holds them" match a real trade_targets run.
        players_lookup: Dict[str, Dict[str, Any]] = {}
        if base_url:
            players_lookup = sapi.build_players_lookup(base_url, cache_dir=cache_dir) or {}
        if players_lookup:
            lvl_map = dc.load_level_id_to_label()
            team_map = dc.load_team_id_to_name(league)
            dc.apply_players_override(
                rows, players_lookup, lvl_map, team_map, include_inactive=False,
            )

        # Stats — full fidelity (the user's choice). Disk-cached per day.
        hitter_stats: Dict[str, Dict[str, Any]] = {}
        pitcher_stats: Dict[str, Dict[str, Any]] = {}
        if base_url:
            league_ids_map = dc.load_league_ids(tt.DEFAULT_LEAGUE_IDS)
            all_lids: List[int] = []
            seen: set = set()
            for level_ids in league_ids_map.get(league.lower(), {}).values():
                for lid in level_ids:
                    if lid not in seen:
                        seen.add(lid)
                        all_lids.append(lid)
            hitter_stats, pitcher_stats, _, _ = sapi.build_player_stats(
                base_url, year,
                cfg.get("year_weights", [0.55, 0.35, 0.10]),
                cfg.get("woba_weights", {}),
                lids=all_lids or None, target_lids=None, cache_dir=cache_dir,
            )

        args = argparse.Namespace(
            league=league, org=org, org_code=None,
            min_composite=0.0, include_no_need=True,
        )
        res = tt.build_trade_targets(
            args, cfg, rows, list(_pids), levels, year, hitter_stats, pitcher_stats,
        )
        return {
            "targets": res["targets"],
            "scored_all": res["scored"],
            "org_pool": bool(res["all_org_hitters"] or res["all_org_pitchers"]),
            "players_available": bool(players_lookup),
            "stats_available": bool(hitter_stats or pitcher_stats),
        }
    except Exception as e:  # noqa: BLE001 — surface, don't crash the page
        return {"error": f"{type(e).__name__}: {e}"}


def _apply_filters(scored_all: List[Dict[str, Any]], min_comp: float,
                   include_no_need: bool) -> List[Dict[str, Any]]:
    """The page-level floors — mirrors build_trade_targets' own filter so the
    sidebar controls stay instant (no re-score). Drop sub-floor non-lottery
    players, and 'Premium (no need)' / 'Pass' unless the user opts in."""
    out: List[Dict[str, Any]] = []
    for c in scored_all:
        comp = float(c.get("composite") or 0)
        vos_pot = float(c.get("vos_potential") or 0)
        if comp < min_comp and vos_pot < tt.LOTTERY_VOS_POT:
            continue
        if c.get("_category") in ("Premium (no need)", "Pass") and not include_no_need:
            continue
        out.append(c)
    return out


# --- render helpers ---------------------------------------------------------

def _fmt(x: Any, digits: int = 1) -> str:
    try:
        return f"{float(x):.{digits}f}"
    except (TypeError, ValueError):
        return "—"


def _cand_df(cands: List[Dict[str, Any]]) -> pd.DataFrame:
    """Candidate table sorted by Fit (the need-weighted rank)."""
    rows = []
    for c in sorted(cands, key=lambda p: -float(p.get("_fit_score") or 0)):
        need_tier = (c.get("_need_entry") or {}).get("tier", "—")
        rows.append({
            "Name": c.get("name", ""),
            "Current Org": c.get("_current_org", "") or "—",
            "Lvl": c.get("_level", "") or "—",
            "Age": _fmt(c.get("age"), 0),
            "Pos/Role": c.get("primary_pos", "") or c.get("proj_role", ""),
            "Fit Need": c.get("_fit_pos") or "—",
            "Need Tier": need_tier,
            "Career": round(float(c.get("vos") or 0), 1),
            "Reach": round(float(c.get("vos_potential") or 0), 1),
            "Comp": round(float(c.get("composite") or 0), 1),
            "Fit": round(float(c.get("_fit_score") or 0), 1),
            "Flags": c.get("_status_flags") or "—",
        })
    return pd.DataFrame(rows)


def _needs_df(targets: List[Dict[str, Any]]) -> pd.DataFrame:
    ordered = sorted(targets, key=lambda t: (TIER_ORDER.get(t["tier"], 9), t["pos"]))
    return pd.DataFrame([{
        "Tier": t["tier"],
        "Pos/Role": t["pos"],
        "Current State": t.get("summary", ""),
        "Archetype to Target": t.get("archetype", ""),
        "Why": t.get("reasoning", ""),
    } for t in ordered])


# --- page -------------------------------------------------------------------

def page() -> None:
    result = active_result() or autorun_result(active_league())
    if not result or not result.get("rows"):
        st.info("Run an evaluation on the **Eval Browser** page first — trade "
                "targeting scores that league's players against your needs.")
        return
    league, rows = result["league"], result["rows"]
    orgs = orgs_for_league(league, rows)
    if not orgs:
        st.warning(f"No organizations found for {league.upper()}.")
        return

    # Default the org selector to the team the user plays as; re-seed only when
    # the loaded league changes (mirrors the Depth / Free Agents pages).
    if st.session_state.get("_tt_org_league") != league:
        st.session_state["_tt_org_league"] = league
        default_org = user_org_for_league(league)
        if default_org in orgs:
            st.session_state["tt_org"] = default_org
        elif st.session_state.get("tt_org") not in orgs:
            st.session_state.pop("tt_org", None)

    with st.sidebar:
        st.header("Trade Targets")
        org = st.selectbox("Organization", orgs, key="tt_org")
        min_comp = st.slider(
            "Min composite", min_value=20.0, max_value=80.0,
            value=float(tt.MIN_TARGET_COMPOSITE), step=1.0,
            help="Drop candidates below this composite (unless they clear the "
                 "lottery-upside bar). Defaults to the CLI floor.")
        include_no_need = st.toggle(
            "Include 'no need' & pass", value=False,
            help="Also show high-composite players at positions you grade as Set, "
                 "plus below-floor 'Pass' players. Off by default — the list "
                 "focuses on real gaps.")
        if st.button("⟳ Refresh trade block", use_container_width=True,
                     help="Re-pull /tradeblock (otherwise cached for the session "
                          "/ calendar day)."):
            fetch_block_pids.clear()
            st.rerun()

    st.subheader(f"{org} — {league.upper()} trade targets")
    st.caption("The league trade block, scored against your org's needs. **Fit** "
               "blends candidate composite, your need-tier at their position, and "
               "an age curve — higher is a better fit. Full fidelity (stats + "
               "/players), so it matches a CLI trade_targets run.")

    # --- Fetch the block list (token-authed, cached) ------------------------
    block = fetch_block_pids(league)
    pids = block["pids"]
    if not block["has_base_url"]:
        st.error(f"No base URL configured for {league.upper()} "
                 "(config/league_url.json) — can't reach /tradeblock.")
        return
    if not pids:
        if not block["had_token"]:
            st.error(
                f"No /tradeblock players returned and no API token is configured "
                f"for {league.upper()}. Add one to config/statsplus_tokens.json "
                "(keyed by league slug), then **Refresh trade block**.")
        else:
            st.info("The /tradeblock returned no players right now — nobody is on "
                    "the block, or the endpoint is briefly unavailable. Try "
                    "**Refresh trade block**.")
        return

    target_year = dc.league_default_year(league) or datetime.now().year
    eval_sig = "|".join(str(result.get(k)) for k in
                        ("league", "rating_scale", "draft", "apply_park", "contracts"))
    eval_sig += f"|{org}|{target_year}"
    block_sig = f"{len(pids)}:{hash(tuple(sorted(pids)))}"

    with st.spinner(f"Scoring {len(pids)} block players vs {org} needs "
                    "(first load fetches stats)…"):
        ctx = build_targets_context(rows, eval_sig, league, org, target_year,
                                    block_sig, pids)
    if "error" in ctx:
        st.error(ctx["error"])
        return
    if not ctx["org_pool"]:
        st.warning(f"No {org} players found in the evaluation — can't grade needs. "
                   "Pick another organization.")
        return
    if not ctx["stats_available"]:
        st.caption("⚠️ Stats unavailable (offline?) — composites are ratings-only "
                   "for this run, so fits are rougher than a full CLI report.")

    scored = _apply_filters(ctx["scored_all"], min_comp, include_no_need)

    # --- Summary ------------------------------------------------------------
    by_cat: Dict[str, List[Dict[str, Any]]] = {}
    for c in scored:
        by_cat.setdefault(c["_category"], []).append(c)
    m = st.columns(4)
    m[0].metric("On the block", len(pids))
    m[1].metric("Evaluated (ML/AAA)", len(ctx["scored_all"]))
    m[2].metric("Match your needs", len(scored))
    m[3].metric("Priority targets", len(by_cat.get("Priority Target", [])))

    # --- Your Needs (context) ----------------------------------------------
    needs = ctx["targets"]
    n_crit = sum(1 for t in needs if t["tier"] == "Critical")
    n_major = sum(1 for t in needs if t["tier"] == "Major")
    with st.expander(f"Your needs — {n_crit} Critical · {n_major} Major "
                     "(what to shop for)", expanded=False):
        st.dataframe(_needs_df(needs), hide_index=True, use_container_width=True)

    # --- Shopping list, sorted by fit --------------------------------------
    st.markdown("### 🎯 Shopping list — best fits first")
    if not scored:
        st.success("No block players clear your filters. Lower **Min composite** "
                   "or enable **Include 'no need' & pass** to widen the net.")
        return

    st.dataframe(_cand_df(scored), hide_index=True, use_container_width=True)

    # --- Per-category breakdown --------------------------------------------
    st.markdown("**By category**")
    for label, blurb in SECTION_ORDER:
        cands = by_cat.get(label) or []
        if not cands:
            continue
        with st.expander(f"{label} · {len(cands)}", expanded=(label in
                         ("Priority Target", "Need Fit"))):
            st.caption(blurb)
            st.dataframe(_cand_df(cands), hide_index=True, use_container_width=True)
