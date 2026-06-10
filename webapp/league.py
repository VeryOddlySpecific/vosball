"""VOSBall web UI — League Hub.

The per-league landing page reached by clicking a status chip in the header band
(or via the sidebar nav). Shows a per-sim checklist (interactive, persisted per
league) transcribed from DAILY_CHECKLIST.md, current standings for any league in
the game world (via the /lgdata endpoint — core/lgdata.py), plus a quick-link
grid of modules — links to the built pages and "coming soon" placeholders.

A pure consumer: reads config + st.session_state; nothing in vosball/ changes.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import streamlit as st  # noqa: E402
import pandas as pd  # noqa: E402

import lgdata  # noqa: E402  (core: /lgdata fetch + structure/standings helpers)
import stats as sapi  # noqa: E402  (core: resolve_base_url)
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


# --- standings (/lgdata) ------------------------------------------------------

@st.cache_data(show_spinner=False)
def _lgdata_for(league: str, nonce: int) -> Optional[Dict[str, Any]]:
    """The /lgdata document for `league`. Session-cached; `nonce` busts it on a
    manual ⟳ (which also evicts the calendar-day disk cache — see the button).
    Persists the structure snapshot (config/structure-{league}.json) on every
    successful fetch. None = unavailable (no base URL, offline, no endpoint)."""
    base = sapi.resolve_base_url(league, None, CONFIG_DIR / "league_url.json")
    if not base:
        return None
    data = lgdata.fetch_lgdata(base, cache_dir=ROOT / league / "cache" / "stats",
                               token=lgdata.resolve_token(league))
    if data:
        lgdata.write_structure_snapshot(league, data)
    return data


def _fmt_streak(n: int) -> str:
    return f"W{n}" if n > 0 else f"L{-n}" if n < 0 else "—"


def _fmt_magic(n: int) -> str:
    # Server conventions: -1 = clinched, >=1000 = not applicable (non-leaders).
    return "✓" if n < 0 else "—" if n >= 1000 else str(n)


def _standings_df(rows: List[Dict[str, Any]]) -> pd.DataFrame:
    show_ties = any(r["t"] for r in rows)
    out = []
    for r in rows:
        row = {"Pos": r["pos"], "Team": r["team"], "W": r["w"], "L": r["l"]}
        if show_ties:
            row["T"] = r["t"]
        row.update({"Pct": f"{r['pct']:.3f}",
                    "GB": "—" if r["gb"] == 0 else f"{r['gb']:g}",
                    "Strk": _fmt_streak(r["streak"]),
                    "Magic#": _fmt_magic(r["magic_number"])})
        out.append(row)
    return pd.DataFrame(out)


def _render_standings(lg: str) -> None:
    st.subheader("Standings")
    st.session_state.setdefault("standings_nonce", 0)
    with st.spinner("Loading standings…"):
        data = _lgdata_for(lg, st.session_state["standings_nonce"])
    if not data:
        st.info(f"Standings unavailable for {lg.upper()} — the /lgdata endpoint "
                "didn't answer (no base URL/token configured, offline, or the "
                "site doesn't serve it yet).")
        if st.button("⟳ Retry", key=f"standings_retry_{lg}"):
            st.session_state["standings_nonce"] += 1
            st.rerun()
        return

    # Level picker — ML / AAA / AA / … (level ids translated via id_maps.json).
    # One level can span several game-world leagues (AAA = IL + PCL).
    level_labels = lgdata.load_level_labels()
    by_level = lgdata.leagues_by_level(data)

    def _lvl_label(opt) -> str:
        lvl, lgs = opt
        name = level_labels.get(lvl, f"Level {lvl}")
        return f"{name} — " + " · ".join(l.get("abbr", "?") for l in lgs)

    left, right = st.columns([3, 1])
    _, level_leagues = left.selectbox(
        "Level", by_level, key=f"standings_lvl_{lg}",
        format_func=_lvl_label, label_visibility="collapsed")
    if right.button("⟳", key=f"standings_refresh_{lg}", use_container_width=True,
                    help="Re-pull /lgdata now (otherwise cached for the "
                         "session / calendar day)."):
        base = sapi.resolve_base_url(lg, None, CONFIG_DIR / "league_url.json")
        if base:
            lgdata.evict_cache(base, ROOT / lg / "cache" / "stats")
        st.session_state["standings_nonce"] += 1
        st.rerun()

    # Build display blocks. A block is one column-worth of grouping that must
    # stay contiguous: a game-world league (IL | PCL at AAA) — or, when the
    # level is a single league, its subleagues (AL | NL). Each block holds
    # (header, [(table label, rows)], games_played).
    blocks: List[Any] = []
    if len(level_leagues) == 1:
        only = level_leagues[0]
        tables = lgdata.division_standings(data, only["league_id"])
        gp = lgdata.max_games_played(data, only["league_id"])
        sub_ids = list(dict.fromkeys(t["sub_league_id"] for t in tables))
        if len(sub_ids) > 1:
            for sid in sub_ids:
                subs = [t for t in tables if t["sub_league_id"] == sid]
                blocks.append((subs[0]["subleague"] or None,
                               [(t["division"], t["rows"]) for t in subs], gp))
        else:
            # Single league, single subleague: no grouping to honor — round-
            # robin its divisions across two columns like before.
            blocks.append((None, [(t["label"], t["rows"]) for t in tables], gp))
    else:
        for l in level_leagues:
            tables = lgdata.division_standings(data, l["league_id"])
            blocks.append((f"{l.get('abbr', '?')} — {l.get('name', '?')}",
                           [(t["label"], t["rows"]) for t in tables],
                           lgdata.max_games_played(data, l["league_id"])))

    if not any(tbls for _, tbls, _ in blocks):
        st.caption("No teams found at this level in /lgdata.")
        return
    if all(gp == 0 for _, _, gp in blocks):
        st.caption("0 games played — this level's season hasn't started; "
                   "order shown is the seeded/default one.")

    def _table(lab, rows) -> None:
        st.markdown(f"**{lab}**")
        st.dataframe(_standings_df(rows), hide_index=True,
                     use_container_width=True)

    if len(blocks) == 1:
        # No grouping to honor — round-robin the divisions across two columns.
        _, tbls, _ = blocks[0]
        cols = st.columns(2)
        for i, (lab, rows) in enumerate(tbls):
            with cols[i % 2]:
                _table(lab, rows)
    else:
        # One column per block when it fits; beyond 3 blocks (e.g. six A-ball
        # leagues) round-robin whole blocks across 2 columns — a league still
        # never splits across columns.
        ncols = len(blocks) if len(blocks) <= 3 else 2
        cols = st.columns(ncols)
        for i, (header, tbls, gp) in enumerate(blocks):
            with cols[i % ncols]:
                if header:
                    st.markdown(f"#### {header}")
                if gp == 0 and any(g > 0 for _, _, g in blocks):
                    st.caption("Season not started (0 games).")
                for lab, rows in tbls:
                    _table(lab, rows)


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
    _render_standings(lg)
    st.divider()
    _render_modules(lg)
