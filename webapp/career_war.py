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


def tier_percentile_projection(actual: float, arch: float, arch_hi: float,
                               rem: float, rem_hi: float) -> Dict[str, float]:
    """Where is the player tracking within his tier, and the percentile-adjusted
    remaining + projected career WAR. Pure (no I/O) so it's unit-testable.

    Inputs (from the eval row + fetched accumulated WAR):
      actual            actual accumulated career WAR
      arch / arch_hi    tier career WAR at median (p50) / upside (~p90)
      rem / rem_hi      tier remaining WAR at median / ~p90

    The aging frac cancels, so "expected so far" at each percentile is just the
    tier total minus its remaining: exp = arch - rem (resp. arch_hi - rem_hi).
    t = where actual sits between them (0 = median pace, 1 = p90 pace). The
    remaining used blends median->p90 by clamp(t,0,1) — capped at p90, floored at
    median (no p10 anchor to blend below). Returns t, pct (~percentile, p50<->p90
    linear), remaining_adj, projected, exp_med, exp_hi.
    """
    exp_med = arch - rem
    exp_hi = arch_hi - rem_hi
    spread = exp_hi - exp_med
    t = (actual - exp_med) / spread if spread > 1e-9 else 0.0
    t_clamped = max(0.0, min(1.0, t))
    remaining_adj = rem + t_clamped * (rem_hi - rem)
    return {
        "t": t,
        "pct": 50.0 + 40.0 * t,  # p50 <-> p90 over t in [0,1]
        "remaining_adj": remaining_adj,
        "projected": actual + remaining_adj,
        "exp_med": exp_med,
        "exp_hi": exp_hi,
    }
