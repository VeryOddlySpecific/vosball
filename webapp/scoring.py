"""VOSBall web UI — shared scoring / data-access layer.

The league-discovery and scoring helpers used by every page, plus the auto-run
that scores a league on first visit with offline-safe defaults. Extracted from
app.py so the page modules (depth.py, prospects.py) and the app share ONE cached
scoring path — no duplicate caches, and no importing the __main__ app module from
a page.

A pure consumer of vosball.services (the same seam the CLI uses) and the
per-league silo in state.py. It imports no sibling page module, so there is no
import cycle.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

# Make the repo root importable (the vosball package + lib live there). Mirrors
# app.py / depth.py so this module works whether it's imported first or not.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import streamlit as st  # noqa: E402

from vosball.services import evaluate_league  # noqa: E402
from vosball.reporting import write_output_csv  # noqa: E402
from vosball.data import DEFAULT_RATING_SCALE  # noqa: E402

from state import get_result, set_result  # noqa: E402

DATA_DIR = ROOT / "data"
CONFIG_DIR = ROOT / "config"

# Leagues whose PlayerData exports component ratings on a 1-100 scale. Everything
# else defaults to 20-80 (weights_v10 native). Always overridable in the sidebar.
LEAGUE_SCALE_DEFAULTS: Dict[str, str] = {"ndl": "1-100"}


def discover_leagues() -> List[str]:
    """League slugs for which PlayerData exists, sorted. No hard-coded list."""
    return sorted(p.name[len("PlayerData-"):-len(".csv")]
                  for p in DATA_DIR.glob("PlayerData-*.csv"))


def default_scale_for(league: str) -> str:
    """Smart default rating scale for a league (overridable in the UI)."""
    return LEAGUE_SCALE_DEFAULTS.get(league, DEFAULT_RATING_SCALE)


def park_factors_path_for(league: str) -> Optional[Path]:
    """Path to the league's park-factors file if one is shipped in config/."""
    p = CONFIG_DIR / f"{league}-park-factors.json"
    return p if p.exists() else None


def player_data_mtime(league: str) -> float:
    """Modification time of the league's PlayerData CSV (0.0 if absent).

    Folded into the score cache key so a fresh `fetch_*_player_data.py` pull
    auto-invalidates the cache — same file → instant hit, new file → re-scored.
    """
    p = DATA_DIR / f"PlayerData-{league}.csv"
    return p.stat().st_mtime if p.exists() else 0.0


def evaluate(
    league: str,
    rating_scale: str,
    draft: bool,
    contracts: bool,
    apply_park: bool,
) -> List[Dict[str, Any]]:
    """Score a league via the same services seam the CLI uses; return row-dicts.

    Pure glue — no Streamlit. Raises ValueError / FileNotFoundError on fatal
    input problems (missing weights, no players, missing contract base URL),
    exactly as evaluate_league does, for the caller to surface.
    """
    park_path = park_factors_path_for(league) if apply_park else None
    return evaluate_league(
        league,
        data_dir=DATA_DIR,
        config_dir=CONFIG_DIR,
        rating_scale=rating_scale,
        draft=draft,
        contracts=contracts,
        park_factors_path=str(park_path) if park_path else None,
    )


def to_csv_bytes(rows: List[Dict[str, Any]], draft: bool, contracts: bool) -> bytes:
    """Serialize rows through the canonical writer → byte-identical CLI CSV."""
    fd, name = tempfile.mkstemp(suffix=".csv")
    os.close(fd)  # close the handle so Windows lets us unlink it afterward
    tmp = Path(name)
    try:
        write_output_csv(rows, tmp, draft_mode=draft, include_contracts=contracts)
        return tmp.read_bytes()
    finally:
        tmp.unlink(missing_ok=True)


@st.cache_data(show_spinner=False)
def cached_eval(league, rating_scale, draft, contracts, apply_park, data_mtime):
    """Cache scoring so re-renders (sorting/filtering) don't re-score ~12k players.
    `data_mtime` is part of the cache key (not used in the body): when the
    PlayerData CSV changes on disk the key changes and the league is re-scored.
    One module-level cache shared by the Eval Browser's Run button and the
    auto-run below, so a manual run after an auto-run is an instant hit."""
    return evaluate(league, rating_scale, draft, contracts, apply_park)


def autorun_result(league: Optional[str]) -> Optional[Dict[str, Any]]:
    """Ensure `league` has a scored result in the silo, scoring it once on first
    visit with offline-safe defaults: the league's smart rating scale, park
    factors only if a file is shipped, and NO draft / contracts (both of which
    would otherwise reach the network). Returns the result dict, or None if the
    league has no PlayerData or scoring failed — the caller shows a message.

    Idempotent: a league already in the silo is returned untouched, so this never
    overrides a manual run's chosen options."""
    existing = get_result(league)
    if existing is not None:
        return existing
    if not league or league not in discover_leagues():
        return None
    rating_scale = default_scale_for(league)
    apply_park = park_factors_path_for(league) is not None
    try:
        with st.spinner(f"Scoring {league}…"):
            rows = cached_eval(league, rating_scale, False, False, apply_park,
                               player_data_mtime(league))
    except (ValueError, FileNotFoundError):
        return None
    set_result(league, {
        "rows": rows, "league": league, "draft": False, "contracts": False,
        "rating_scale": rating_scale, "apply_park": apply_park,
    })
    return get_result(league)
