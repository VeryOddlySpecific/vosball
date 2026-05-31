"""VOSBall web UI — accumulated career WAR fetch (for the player card).

Pulls a player's actual accumulated MLB WAR from the StatsPlus career endpoints
by reusing hof_grade.py's aggregators. Used by the player card to show
"Projected career WAR = actual accumulated + projected remaining" instead of the
archetype-only estimate (which is wonky for veterans).

Per-player + network (token auth, same as the contracts toggle); cached per
session, and hof_grade itself disk-caches the raw CSVs under cache/hof/.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import streamlit as st  # noqa: E402

import hof_grade as hg  # noqa: E402  (reuse its career-stat fetch + aggregators)


@st.cache_data(show_spinner=False)
def accumulated_war(league: str, player_id: str) -> Dict[str, Any]:
    """Actual accumulated ML career WAR for one player (batting + pitching).

    Returns {"ok": True, "hit", "pit", "total", "seasons"} or
    {"ok": False, "error": ...}. _fetch_endpoint raises SystemExit on missing
    auth / HTTP errors (not a normal Exception), so both are caught.
    """
    pid = str(player_id).strip()
    if not pid:
        return {"ok": False, "error": "no player id"}
    try:
        bat = hg._fetch_endpoint(league, "playerbatstatsv2", pid, split=1, refresh=False)
        pit = hg._fetch_endpoint(league, "playerpitchstatsv2", pid, split=1, refresh=False)
        h = hg.aggregate_hitting(hg._filter_ml(hg._parse_csv(bat)))
        p = hg.aggregate_pitching(hg._filter_ml(hg._parse_csv(pit)))
        return {
            "ok": True,
            "hit": round(h.war, 1),
            "pit": round(p.war, 1),
            "total": round(h.war + p.war, 1),
            "seasons": max(h.seasons, p.seasons),
        }
    except SystemExit as e:  # auth missing / HTTP >= 400 from _fetch_endpoint
        return {"ok": False, "error": str(e)}
    except Exception as e:  # noqa: BLE001 — surface, don't crash the card
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
