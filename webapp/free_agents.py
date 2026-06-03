"""VOSBall web UI — Free Agents page (biggest-holes-first).

Pick a team and a level, set a weak-spot threshold, and see your depth chart's
biggest holes ranked by gap — each paired with the best-fit free agents from the
league pool. A pure consumer of the in-process FA-fit core
(free_agent_market.compute_fa_fit) and the eval rows the Eval Browser already
produced (per-league silo). VOS-ratings-only — fully offline, always fresh with
the active eval (no depth-chart files read; no staleness possible).
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

import stats as sapi  # noqa: E402  (/players lookup for the pro-service-time gate)
from depth_chart import (  # noqa: E402
    build_team_pool, assign_positions, assign_pitchers, load_config,
    league_default_year, HITTER_POSITIONS, DEFAULT_CONFIG, DEFAULT_LEAGUE_URL,
    DEFAULT_LEAGUE_IDS,
)
from free_agent_market import score_fa_records, compute_fa_fit, PITCHER_ROLES  # noqa: E402
from state import active_result, active_league  # noqa: E402  (per-league silo)
from scoring import autorun_result  # noqa: E402  (auto-score on first visit)
# Reuse the depth page's org/level helpers rather than duplicating them.
from depth import (  # noqa: E402
    orgs_for_league, user_org_for_league, depth_levels, org_level_counts,
)

CONFIG_DIR = ROOT / "config"


# --- data layer -------------------------------------------------------------

def min_comp_for_league(league: str, fallback: float = 50.0) -> float:
    """The league's starter min_comp from league_settings.json (the same key the
    CLI's --min-comp / run_depth_chart_all read). Defaults to ``fallback`` when
    absent, so the weak-spot threshold matches CLI semantics out of the box."""
    import json
    p = CONFIG_DIR / "league_settings.json"
    if not p.exists():
        return fallback
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        entry = data.get(league) if isinstance(data, dict) else None
        if isinstance(entry, dict) and entry.get("min_comp") is not None:
            return float(entry["min_comp"])
    except (OSError, ValueError, TypeError):
        pass
    return fallback


def _load_players_lookup(league: str) -> Dict[str, Dict[str, Any]]:
    """The /players payload for a league, from the daily disk cache when present
    (so this stays offline after any prior CLI/UI run) else a fresh fetch.
    Returns {} when there's no base URL or the fetch fails — the caller then
    skips the service gate and warns."""
    try:
        base_url = sapi.resolve_base_url(league, None, DEFAULT_LEAGUE_URL)
        if not base_url:
            return {}
        cache_dir = ROOT / league / "cache" / "stats"
        return sapi.build_players_lookup(base_url, cache_dir=cache_dir) or {}
    except Exception:  # noqa: BLE001 — offline / no creds: degrade, don't crash
        return {}


@st.cache_data(show_spinner=False)
def build_fa_context(_eval_rows, eval_sig, league, org, level, target_year):
    """Build the org's starters + scored FA pool at a level (ratings-only, no
    file writes). Cached on ``eval_sig`` so moving the threshold/service controls
    re-runs only the cheap filter + compute_fa_fit pass, not this.

    Retired players are dropped and ``pro_service_days`` is attached here (via the
    cached /players lookup); the variable min-service gate is applied by the
    caller so the control stays responsive. Returns artifacts or {'error': msg}.
    """
    try:
        cfg = load_config(DEFAULT_CONFIG)
        args = argparse.Namespace(
            league=league, org=org, base_url=None,
            league_url_config=DEFAULT_LEAGUE_URL, league_ids_config=DEFAULT_LEAGUE_IDS,
            all_levels=False, lids=None, no_cache=False, no_stats=True, cache_dir=None,
        )
        pool = build_team_pool(level, args, cfg, _eval_rows, target_year)
        if pool is None:
            return {"error": f"Couldn't build a pool for {org} at {level}."}
        level_cfg = pool["level_cfg"]
        placed = assign_positions(pool["hitter_pool"], level_cfg)
        pslots = assign_pitchers(pool["pitcher_pool"], level_cfg)
        starters = {pos: (placed[pos][0] if placed.get(pos) else None) for pos in HITTER_POSITIONS}

        players_lookup = _load_players_lookup(league)
        # Attach pro_service_days + drop retired here; defer the min-service gate
        # to the caller (min_pro_service_days=0) so the sidebar control is snappy.
        fa_records = score_fa_records(
            _eval_rows, level_cfg, cfg.get("stat_floors", {}),
            players_lookup=players_lookup, min_pro_service_days=0,
            exclude_retired=True,
        )
        return {
            "starters_by_pos": starters,
            "pitcher_slots": pslots,
            "fa_hitters": [r for r in fa_records if not r["is_pitcher"]],
            "fa_pitchers": [r for r in fa_records if r["is_pitcher"]],
            "n_fa": len(fa_records),
            "players_available": bool(players_lookup),
        }
    except Exception as e:  # noqa: BLE001 — surface, don't crash the page
        return {"error": f"{type(e).__name__}: {e}"}


# --- render helpers ---------------------------------------------------------

def _aav(v) -> str:
    try:
        return f"${float(v) / 1e6:.1f}M"
    except (TypeError, ValueError):
        return "—"


def _priority_rows(holes: List[Dict[str, Any]], kind: str) -> List[Dict[str, Any]]:
    """Flatten holes into Priority-Needs table rows (one per hole, best FA)."""
    rows = []
    for h in holes:
        best = h["fas"][0] if h["fas"] else None
        slot_label = h.get("pos") or h.get("role")
        rows.append({
            "Need": f"{slot_label} ({kind})",
            "Your Player": h["starter_name"],
            "Score": round(h["slot_score"], 1),
            "Gap": round(h["gap"], 1),
            "Best FA": best["name"] if best else "—",
            "FA Fit": round(best["fit_score"], 1) if best else None,
            "Edge": round(best["edge"], 1) if best else None,
            "Fair AAV": _aav(best["fair_aav"]) if best else "—",
        })
    return rows


def _fa_pool_df(records: List[Dict[str, Any]], n: int) -> pd.DataFrame:
    top = sorted(records, key=lambda r: -float(r.get("composite", 0.0) or 0.0))[:n]
    return pd.DataFrame([{
        "Name": r.get("name", ""),
        "Age": r.get("age", ""),
        "Pos": r.get("primary_pos", "") or r.get("proj_role", ""),
        "Last Lvl": r.get("last_level", "") or "—",
        "VOS": round(float(r.get("vos", 0.0) or 0.0), 1),
        "Tier": r.get("vos_tier", "") or "—",
        "Comp": round(float(r.get("composite", 0.0) or 0.0), 1),
    } for r in top])


# --- page -------------------------------------------------------------------

def page() -> None:
    result = active_result() or autorun_result(active_league())
    if not result or not result.get("rows"):
        st.info("Run an evaluation on the **Eval Browser** page first — free-agent "
                "targeting uses that league's scored players.")
        return
    league, rows = result["league"], result["rows"]
    orgs = orgs_for_league(league, rows)
    if not orgs:
        st.warning(f"No organizations found for {league.upper()}.")
        return

    # Default the org selector to the team the user plays as. Re-seed only when
    # the loaded league changes (mirrors the Depth page).
    if st.session_state.get("_fa_org_league") != league:
        st.session_state["_fa_org_league"] = league
        default_org = user_org_for_league(league)
        if default_org in orgs:
            st.session_state["fa_org"] = default_org
        elif st.session_state.get("fa_org") not in orgs:
            st.session_state.pop("fa_org", None)

    levels = depth_levels()
    with st.sidebar:
        st.header("Free Agents")
        org = st.selectbox("Organization", orgs, key="fa_org")
        level = st.selectbox("Level", levels, key="fa_level")
        threshold = st.slider(
            "Weak-spot threshold", min_value=20.0, max_value=80.0,
            value=min_comp_for_league(league), step=1.0,
            help="A slot is a 'need' when its score falls below this. Defaults to "
                 "the league's min_comp from league_settings.json.")
        top_n = st.number_input("FAs per need", min_value=1, max_value=10, value=3, step=1)
        min_svc = st.number_input(
            "Min pro service (days)", min_value=0, value=1, step=1,
            help="Exclude free agents below this many days of professional service "
                 "(via /players) — keeps amateur draft-eligible players out of the "
                 "recommendations. Default 1; set 0 to include amateurs.")

    counts = org_level_counts(rows, org, levels)
    if counts.get(level, 0) == 0:
        st.info(f"{org} has no players at {level}. Pick another level.")
        return

    st.subheader(f"{org} — {level} free-agent targets")
    st.caption("VOS-ratings-only — scored live from the active evaluation, so this "
               "always reflects your latest eval (no depth-chart files involved).")

    target_year = league_default_year(league) or datetime.now().year
    eval_sig = "|".join(str(result.get(k)) for k in
                        ("league", "rating_scale", "draft", "apply_park", "contracts"))
    eval_sig += f"|{org}|{level}"
    with st.spinner(f"Scoring {org} {level} needs & the FA pool…"):
        ctx = build_fa_context(rows, eval_sig, league, org, level, target_year)
    if "error" in ctx:
        st.error(ctx["error"])
        return

    # Pro-service-time gate (keeps amateur draft-eligible players out). Applied
    # here (not in the cached build) so the control stays responsive. Conservative
    # like the CLI: unknown service (player absent from /players) ⇒ excluded.
    fa_hitters, fa_pitchers = ctx["fa_hitters"], ctx["fa_pitchers"]
    if not ctx["players_available"]:
        st.warning(
            f"Couldn't load /players for {league.upper()} — can't verify pro "
            "service time, so amateur draft-eligible players may appear below. "
            "Cache /players via a CLI run (e.g. depth_chart.py) or the League "
            "Hub's **Pull fresh ratings**, then revisit.")
    elif min_svc > 0:
        def _svc_ok(r: Dict[str, Any]) -> bool:
            psd = r.get("pro_service_days")
            return psd is not None and psd >= int(min_svc)
        kept_h = [r for r in fa_hitters if _svc_ok(r)]
        kept_p = [r for r in fa_pitchers if _svc_ok(r)]
        dropped = (len(fa_hitters) + len(fa_pitchers)) - (len(kept_h) + len(kept_p))
        fa_hitters, fa_pitchers = kept_h, kept_p
        st.caption(f"Pro FAs only — excluded {dropped} player(s) below "
                   f"{int(min_svc)}d pro service (amateur/draft-eligible), via /players.")

    thresholds: Dict[str, float] = {p: threshold for p in HITTER_POSITIONS}
    thresholds.update({role: threshold for role in PITCHER_ROLES})
    fit = compute_fa_fit(ctx["starters_by_pos"], ctx["pitcher_slots"],
                         fa_hitters, fa_pitchers, thresholds, top_n=int(top_n))

    # --- Priority Needs (biggest holes first) -------------------------------
    pri_rows = (_priority_rows(fit["hitter_holes"], "bat")
                + _priority_rows(fit["pitcher_holes"], "arm"))
    pri_rows.sort(key=lambda r: -r["Gap"])
    st.markdown("### 🎯 Priority Needs — biggest holes first")
    if not pri_rows:
        st.success(f"No slots fall below {threshold:.0f} at {level} — your depth "
                   "chart has no holes by this threshold. Lower it to scout upgrades.")
    else:
        st.dataframe(pd.DataFrame(pri_rows), hide_index=True, use_container_width=True)

        # Per-need detail: all top-N candidates for each hole.
        st.markdown("**Candidates per need**")
        for h in sorted(fit["hitter_holes"] + fit["pitcher_holes"],
                        key=lambda x: -x["gap"]):
            slot = h.get("pos") or h.get("role")
            with st.expander(f"{slot} — {h['starter_name']} "
                             f"(score {h['slot_score']:.1f}, gap {h['gap']:+.1f})"):
                if not h["fas"]:
                    st.caption("No eligible free agents for this slot.")
                    continue
                st.dataframe(pd.DataFrame([{
                    "FA": f["name"], "Age": f["age"], "Last Lvl": f["last_level"] or "—",
                    "Fit": round(f["fit_score"], 1), "VOS": round(f["vos"], 1),
                    "Tier": f["vos_tier"] or "—", "Edge": round(f["edge"], 1),
                    "Fair AAV": _aav(f["fair_aav"]),
                } for f in h["fas"]]), hide_index=True, use_container_width=True)

    # --- Full FA pool (post service-gate) -----------------------------------
    st.markdown(f"### Free-agent pool · {len(fa_hitters) + len(fa_pitchers)} eligible")
    a, b = st.columns(2)
    a.caption("Top FA hitters")
    a.dataframe(_fa_pool_df(fa_hitters, 25), hide_index=True, use_container_width=True)
    b.caption("Top FA pitchers")
    b.dataframe(_fa_pool_df(fa_pitchers, 25), hide_index=True, use_container_width=True)
