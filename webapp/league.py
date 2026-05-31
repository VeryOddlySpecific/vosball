"""VOSBall web UI — League Hub.

The per-league landing page reached by clicking a status chip in the header band
(or via the sidebar nav). Shows a per-sim checklist (interactive, persisted per
league) transcribed from DAILY_CHECKLIST.md, plus a quick-link grid of modules —
links to the built pages and "coming soon" placeholders for the rest.

A pure consumer: reads config + st.session_state; nothing in vosball/ changes.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict, List

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import streamlit as st  # noqa: E402

from status import configured_leagues  # noqa: E402  (reuse the league list)

CONFIG_DIR = ROOT / "config"
SETTINGS_PATH = Path(__file__).resolve().parent / ".ui_settings.json"

# Per-sim checklist, rewritten for the app era. The app now scores, builds depth
# charts, ranks prospects, and renders cards IN-PROCESS from the fetched
# PlayerData, so the old per-sim CLI runs (vos_v2 / prospect_rankings /
# depth_chart / player_card just to view) are folded into "review in the app".
# What survives: fetching fresh data (the app can't), the roster moves + upload
# (the app can't), and the few tools not yet in the app.
# {lg} is filled with the league slug. Item ids are index-based.
CHECKLIST = [
    ("Get fresh data (required — the app reads it, can't fetch it)", [
        "Confirm the sim ran & export is up (the header band shows each league)",
        "py fetch_player_data.py --league {lg}   → data/PlayerData-{lg}.csv",
    ]),
    ("Review in the app (replaces the old vos / prospect / depth / card CLI runs)", [
        "Eval Browser — Run evaluation for {lg} (scored in-process; no vos_v2 needed)",
        "Depth Charts — lineup / rotation / bullpen by level",
        "Prospects — farm board",
        "Player Card — check any notables (slumping starter, hot prospect)",
    ]),
    ("Make roster moves + upload (the app can't do this part)", [
        "Set lineups / rotation / bullpen per the depth chart",
        "Process trade offers + waiver claims",
        "Save & upload changes back to StatsPlus",
    ]),
    ("Still command-line (not in the app yet)", [
        "py farm_value.py --league {lg}   — farm $ values",
        "py project_season.py --league {lg} …   — season projection vs standings",
        "py current_standings.py --league {lg}   (optional)",
        "py statsplus_paper_news.py --league {lg}   (optional)",
    ]),
]

# Module registry — the framework. `page` is a key into st.session_state["_pages"]
# for built modules, or None for planned ones (renders a disabled placeholder).
# Shipping a new module = flip its `page` from None to the page key.
MODULES = [
    {"label": "Evaluations", "icon": "📊", "page": "eval", "blurb": "Browse & score the league"},
    {"label": "Player Card", "icon": "🪪", "page": "card", "blurb": "Single-player detail"},
    {"label": "Depth Charts", "icon": "📋", "page": "depth", "blurb": "Lineups & staff by level"},
    {"label": "Prospects", "icon": "🌱", "page": "prospects", "blurb": "Prospect board"},
    {"label": "Farm Value", "icon": "💲", "page": None, "blurb": "Farm system $ values"},
    {"label": "Trade Targets", "icon": "🔄", "page": None, "blurb": "Shopping list vs trade blocks"},
    {"label": "Draft Room", "icon": "🎯", "page": None, "blurb": "Pool tiers · board · values"},
    {"label": "Free Agents", "icon": "🧢", "page": None, "blurb": "FA market & fair value"},
    {"label": "Finances", "icon": "🏦", "page": None, "blurb": "Payroll & budget audits"},
]


# --- settings (checklist persistence) ---------------------------------------

def _load_settings() -> Dict[str, Any]:
    try:
        if SETTINGS_PATH.exists():
            d = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
            return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        pass
    return {}


def load_checklist(league: str) -> Dict[str, bool]:
    cl = _load_settings().get("checklists")
    return (cl.get(league, {}) if isinstance(cl, dict) else {}) or {}


def save_checklist(league: str, state: Dict[str, bool]) -> None:
    """Merge this league's checklist state into the shared settings file (keeps
    other keys like `palette` and other leagues' checklists intact)."""
    s = _load_settings()
    cl = s.get("checklists")
    if not isinstance(cl, dict):
        cl = {}
    cl[league] = state
    s["checklists"] = cl
    try:
        SETTINGS_PATH.write_text(json.dumps(s, indent=2), encoding="utf-8")
    except OSError:
        pass


def league_entry(league: str) -> Dict[str, Any]:
    try:
        d = json.loads((CONFIG_DIR / "league_settings.json").read_text(encoding="utf-8"))
        e = d.get(league)
        return e if isinstance(e, dict) else {}
    except (OSError, ValueError):
        return {}


def _item_ids() -> List[str]:
    return [f"{s}_{i}" for s, (_, items) in enumerate(CHECKLIST) for i in range(len(items))]


# --- render -----------------------------------------------------------------

def _render_checklist(lg: str) -> None:
    ids = _item_ids()
    saved = load_checklist(lg)
    for iid in ids:  # seed session state from saved, once per league
        k = f"chk_{lg}_{iid}"
        if k not in st.session_state:
            st.session_state[k] = bool(saved.get(iid, False))

    def _persist() -> None:
        save_checklist(lg, {iid: bool(st.session_state.get(f"chk_{lg}_{iid}", False))
                            for iid in ids})

    done = sum(1 for iid in ids if st.session_state.get(f"chk_{lg}_{iid}"))
    st.subheader(f"Per-sim checklist — {done}/{len(ids)} done")
    st.caption("Scoring, depth charts, prospects, and cards now run in-process in "
               "the app — the old per-sim CLI runs for those are gone. The "
               "command-line tools below read the eval CSV, so run `run_vos_all.py` "
               "first if you use them (the app doesn't write that file).")
    for s, (section, items) in enumerate(CHECKLIST):
        st.markdown(f"**{section}**")
        for i, item in enumerate(items):
            st.checkbox(item.format(lg=lg), key=f"chk_{lg}_{s}_{i}", on_change=_persist)
    if st.button("Reset checklist", key=f"reset_{lg}"):
        for iid in ids:
            st.session_state[f"chk_{lg}_{iid}"] = False
        save_checklist(lg, {})
        st.rerun()


def _render_modules(lg: str) -> None:
    st.subheader("Modules")
    st.caption("Quick-links to manage this team. Greyed tiles are planned.")
    pages = st.session_state.get("_pages", {})
    cols = st.columns(3)
    for idx, m in enumerate(MODULES):
        with cols[idx % 3].container(border=True):
            st.markdown(f"**{m['icon']} {m['label']}**")
            st.caption(m["blurb"])
            page_key = m.get("page")
            if page_key and page_key in pages:
                st.page_link(pages[page_key], label="Open →")
            else:
                st.button("Coming soon", key=f"mod_{m['label']}", disabled=True,
                          use_container_width=True)


def page() -> None:
    leagues = configured_leagues()
    if not leagues:
        st.warning("No leagues configured (expected `config/league_url.json`).")
        return

    pre = st.session_state.get("selected_league")
    if pre not in leagues:
        pre = leagues[0]
    chosen = st.selectbox("League", leagues, index=leagues.index(pre),
                          key="hub_league_select", format_func=str.upper)
    st.session_state["selected_league"] = chosen
    lg = chosen

    entry = league_entry(lg)
    st.header(f"🏟️ {entry.get('org', '—')} — {lg.upper()} hub")
    bits = [f"{label}: {entry.get(key)}" for label, key in (
        ("Year", "year"), ("Scale", "rating_scale"),
        ("Engine", "game_version"), ("Sim", "sim_time")) if entry.get(key)]
    if bits:
        st.caption(" · ".join(bits))

    _render_checklist(lg)
    st.divider()
    _render_modules(lg)
