"""VOSBall web UI — Home / league-select landing.

The cold-boot landing screen: an expanded version of the header status chips,
one tile per league the user is in (config/league_url.json), color-coded by
export status. Picking a league sets the active league and opens its hub — from
there the module cards (Evaluations, Depth, …) auto-load that league's data.

A pure consumer: reuses status.py's cached export check (no extra network) and
league.py's settings lookup. Imports no app/page module, so there's no cycle.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import streamlit as st  # noqa: E402

from status import configured_leagues, export_status  # noqa: E402
from league import league_entry  # noqa: E402

COLS = 4


def _tile_color_css(leagues, results) -> str:
    """Color each tile's Open button green/amber by export status (matches the
    header chips), keyed via Streamlit's `st-key-<key>` class."""
    rules = ["<style>"]
    for lg in leagues:
        ok = bool((results.get(lg) or {}).get("skip"))
        rules.append(
            f'.st-key-home_open_{lg} button {{ background:{"#46B36B" if ok else "#E8A33D"}'
            f' !important; color:#000 !important; border:none !important;'
            f' font-weight:700; letter-spacing:1px; }}')
    rules.append("</style>")
    return "".join(rules)


def page() -> None:
    leagues = configured_leagues()
    if not leagues:
        st.warning("No leagues configured (expected `config/league_url.json`).")
        return

    st.header("Select a league")
    st.caption("Open a league to reach its hub — evaluations, depth charts, "
               "prospects and more load automatically for that league.")

    # Same cached status the header band computed this session (no extra network).
    nonce = st.session_state.get("exports_nonce", 0)
    results = export_status(tuple(leagues), nonce).get("results", {})
    st.markdown(_tile_color_css(leagues, results), unsafe_allow_html=True)

    pages = st.session_state.get("_pages", {})
    cols = st.columns(COLS)
    for i, lg in enumerate(leagues):
        entry = league_entry(lg)
        r = results.get(lg) or {}
        ok = bool(r.get("skip"))
        with cols[i % COLS].container(border=True):
            st.markdown(f"### {'🟢' if ok else '🟠'} {lg.upper()}")
            bits = [str(entry.get(k)) for k in ("org", "year") if entry.get(k)]
            if bits:
                st.caption(" · ".join(bits))
            st.caption(("Current" if ok else "Needs export")
                       + (f": {r.get('reason', '')}" if r.get("reason") else ""))
            if st.button("Open hub →", key=f"home_open_{lg}",
                         use_container_width=True):
                st.session_state["selected_league"] = lg
                if "league" in pages:
                    st.switch_page(pages["league"])
