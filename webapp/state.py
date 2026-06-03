"""VOSBall web UI — shared session-state helpers (per-league evaluation silo).

The whole app keys off one idea: a *per-league* store of scored evaluations plus
a single *active league* pointer. Scored results live in
st.session_state["results"], keyed by league slug, so every league keeps its own
evaluation instead of one global slot being overwritten on each run (the source
of the cross-league mix-ups). The active league — set by the League Hub, a header
status chip, or the Eval Browser's own picker — is st.session_state["selected_league"];
every page resolves its data through active_result().

st.session_state["result"] is retained as a back-compat pointer to the
most-recently-set league's data for any code path still reading it directly.

This module imports only streamlit, so app.py and the page modules (depth.py,
prospects.py) can all share it with no import cycle.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

import streamlit as st


def results_silo() -> Dict[str, Dict[str, Any]]:
    """The per-league result store (created on first use)."""
    return st.session_state.setdefault("results", {})


def set_result(league: str, data: Dict[str, Any]) -> None:
    """Store a league's scored result in the silo and update the legacy pointer."""
    results_silo()[league] = data
    st.session_state["result"] = data  # back-compat for any direct readers


def get_result(league: Optional[str]) -> Optional[Dict[str, Any]]:
    """A league's scored result from the silo, or None if it hasn't been run."""
    if not league:
        return None
    return results_silo().get(league)


def clear_results() -> None:
    """Drop all siloed results and the legacy pointer (Clear cache & re-score)."""
    st.session_state.pop("results", None)
    st.session_state.pop("result", None)


def drop_result(league: str) -> None:
    """Forget one league's scored result — e.g. after fetching fresh ratings —
    so its next visit re-scores off the new data. Also clears the legacy pointer
    if it referenced this league. (The scoring cache itself keys on the file's
    mtime, so a new file already re-scores; this just evicts the stored rows.)"""
    if not league:
        return
    results_silo().pop(league, None)
    legacy = st.session_state.get("result")
    if isinstance(legacy, dict) and legacy.get("league") == league:
        st.session_state.pop("result", None)


def active_league() -> Optional[str]:
    """The league currently in focus across the app (hub / chip / eval picker)."""
    return st.session_state.get("selected_league")


def active_result() -> Optional[Dict[str, Any]]:
    """The active league's scored result, or None if none is selected/run yet."""
    return get_result(active_league())
