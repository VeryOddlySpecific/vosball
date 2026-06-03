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
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import streamlit as st  # noqa: E402

from status import configured_leagues  # noqa: E402  (reuse the league list)

CONFIG_DIR = ROOT / "config"
SETTINGS_PATH = Path(__file__).resolve().parent / ".ui_settings.json"

# A short, general per-sim loop — workflow steps, not tool invocations. The app
# does the scoring/depth/prospect/card work in-process, so this is just the GM
# rhythm each time the league sims. Tool-specific tasks live on the module cards
# (below); the seasonal data pull is noted in the caption, not a daily step.
# {lg} is filled with the league slug. Item ids are index-based.
# (section, [items]) — section header is rendered only when non-empty, so more
# categories can be added later without forcing a header on this single group.
CHECKLIST = [
    ("", [
        "Run the eval",
        "Check standings",
        "Review depth charts & lineups",
        "Review waiver wire / trades / free agents",
        "Set roster moves & export the save to StatsPlus",
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
    {"label": "Farm Value", "icon": "💲", "page": "farm_value", "blurb": "Farm system $ values"},
    {"label": "Trade Targets", "icon": "🔄", "page": "trade_targets", "blurb": "Shopping list vs trade blocks"},
    {"label": "Draft Room", "icon": "🎯", "page": None, "blurb": "Pool tiers · board · values"},
    {"label": "Free Agents", "icon": "🧢", "page": "free_agents", "blurb": "Biggest holes & best-fit FAs"},
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
    st.caption("The per-sim GM loop. (Pulling fresh ratings data is a ~1–2×/season "
               "task — the header band flags staleness — not a daily step.)")
    for s, (section, items) in enumerate(CHECKLIST):
        if section:
            st.markdown(f"**{section}**")
        for i, item in enumerate(items):
            st.checkbox(item.format(lg=lg), key=f"chk_{lg}_{s}_{i}", on_change=_persist)
    if st.button("Reset checklist", key=f"reset_{lg}"):
        for iid in ids:
            st.session_state[f"chk_{lg}_{iid}"] = False
        save_checklist(lg, {})
        st.rerun()


def _run_fetch(lg: str) -> None:
    """Pull fresh ratings for `lg`, streaming progress into an st.status panel.
    On success, evict the league's cached evaluation so it re-scores off the new
    file the next time it's opened."""
    from fetch import fetch_league_ratings  # lazy: pulls in fetch_player_data
    from state import drop_result

    outcome = "error"
    with st.status(f"Pulling fresh {lg.upper()} ratings…", expanded=True) as box:
        for ev in fetch_league_ratings(lg):
            kind = ev.get("type")
            if kind == "progress":
                box.write(ev["msg"])
            elif kind == "done":
                box.write("✅ " + ev["msg"])
                box.update(label=f"{lg.upper()} ratings updated ✓", state="complete")
                outcome = "done"
            else:  # error
                box.write("❌ " + ev["msg"])
                box.update(label="Fetch failed", state="error")
                outcome = "error"
    if outcome == "done":
        drop_result(lg)
        st.success("Fresh ratings saved. Evaluations, depth charts and prospects "
                   "will re-score with the new data the next time you open them.")


def _render_data_controls(lg: str) -> None:
    """Show this league's ratings-data freshness and a button to pull a new pull."""
    from scoring import player_data_mtime  # lazy: avoids import cost on cold pages

    mtime = player_data_mtime(lg)
    left, right = st.columns([2, 1])
    with left:
        if mtime:
            st.caption("Ratings data: "
                       f"{datetime.fromtimestamp(mtime):%Y-%m-%d %H:%M}")
        else:
            st.caption("No ratings data pulled for this league yet.")
    clicked = right.button(
        "⟳ Pull fresh ratings", key=f"fetch_btn_{lg}", use_container_width=True,
        help="Fetch the latest player ratings from StatsPlus for this league. "
             "The export can take a few minutes to build; progress shows below.")
    if clicked:
        _run_fetch(lg)


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

    _render_data_controls(lg)
    st.divider()
    _render_checklist(lg)
    st.divider()
    _render_modules(lg)
