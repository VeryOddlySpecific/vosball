#!/usr/bin/env python3
"""
preflight.py — Shared pre-flight check for the bulk runners.

Per league, hits the ``/exports`` endpoint and decides whether to skip
based on whether the user's org has a valid export for the league's
``current_date``. If it does, the league is skipped: my local data
from the last fetch is still current.

Resolution chain:

1. ``league_settings.json`` -> ``{league: {"org": "<Name Nickname>"}}``
2. ``teams-{league}.json`` -> match accent/case-insensitive against
   ``"Name Nickname"`` for ML clubs (``Parent: 0``) to get ``team_id``.
3. GET ``{base}/exports/?token=...`` -> JSON ``{"current_date": ...,
   "<date>": [team_ids]}``.
4. Skip if ``team_id in data[data["current_date"]]``.

**Fail-open:** any error (missing config, network, malformed response,
auth failure) yields ``skip=False`` with the reason. The downstream
fetch/vos/depth_chart steps will surface real auth/network errors
themselves; preflight should never silently drop a league.

Public API:
    check_league(league) -> PreflightResult
    check_leagues(leagues) -> dict[str, PreflightResult]
    filter_leagues(leagues, *, verbose=True) -> list[str]
"""

from __future__ import annotations
# --- repo-root + core/ path bootstrap ---
import os as _os, sys as _sys
_ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
for _p in (_ROOT, _os.path.join(_ROOT, "core")):
    if _p not in _sys.path:
        _sys.path.insert(0, _p)
# --- end bootstrap ---


import json
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from fetch_player_data import (
    _get,
    load_cookie_for,
    load_league_base,
    load_token_for,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = REPO_ROOT / "config"
LEAGUE_SETTINGS_PATH = CONFIG_DIR / "league_settings.json"


@dataclass
class PreflightResult:
    league: str
    skip: bool
    reason: str  # human-readable; printed in the per-league line


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------

def _normalize(s: str) -> str:
    """Case-fold + strip combining marks for accent-insensitive equality.

    Used for accent-insensitive org name matching (league_settings vs teams files).
    """
    nfkd = unicodedata.normalize("NFKD", s)
    stripped = "".join(c for c in nfkd if not unicodedata.combining(c))
    return stripped.casefold().strip()


def _load_json(path: Path) -> dict:
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def _resolve_team_id(league: str, org_name: str) -> Optional[int]:
    """Map an org name to a team_id via ``teams-{league}.json``.

    Only ML clubs (``Parent: 0``) are considered. Returns ``None`` if no
    match or the teams file is missing.
    """
    teams_path = CONFIG_DIR / f"teams-{league}.json"
    if not teams_path.exists():
        return None
    try:
        teams = _load_json(teams_path)
    except Exception:
        return None
    want = _normalize(org_name)
    for tid_str, info in teams.items():
        if info.get("Parent", 0) != 0:
            continue
        full = f"{info.get('Name', '')} {info.get('Nickname', '')}".strip()
        if _normalize(full) == want:
            try:
                return int(tid_str)
            except (TypeError, ValueError):
                return None
    return None


# -----------------------------------------------------------------------------
# Per-league check
# -----------------------------------------------------------------------------

def check_league(league: str) -> PreflightResult:
    """Return a PreflightResult for one league. Fail-open on any error."""
    if not LEAGUE_SETTINGS_PATH.exists():
        return PreflightResult(league, False, "no league_settings.json (will run)")

    try:
        settings = _load_json(LEAGUE_SETTINGS_PATH)
    except Exception as e:
        return PreflightResult(league, False, f"unreadable league_settings.json: {e} (will run)")

    org = settings.get(league, {}).get("org")
    if not org:
        return PreflightResult(league, False, f"no 'org' in league_settings (will run)")

    team_id = _resolve_team_id(league, org)
    if team_id is None:
        return PreflightResult(
            league, False,
            f"could not resolve team_id for {org!r} in teams-{league}.json (will run)"
        )

    # Hit /exports
    try:
        base = load_league_base(league)
        token = load_token_for(league)
        cookie = None if token else load_cookie_for(base)
        url = f"{base.rstrip('/')}/exports/"
        if token:
            sep = "&" if "?" in url else "?"
            url = f"{url}{sep}token={token}"
        status, body = _get(url, None if token else cookie, timeout=30)
    except Exception as e:
        return PreflightResult(league, False, f"/exports request error: {e} (will run)")

    if status >= 400:
        return PreflightResult(league, False, f"/exports HTTP {status} (will run)")

    try:
        data = json.loads(body)
    except Exception as e:
        return PreflightResult(league, False, f"/exports non-JSON response: {e} (will run)")

    current_date = data.get("current_date")
    if not current_date:
        return PreflightResult(league, False, "/exports missing 'current_date' (will run)")

    todays = data.get(current_date) or []
    if team_id in todays:
        return PreflightResult(
            league, True,
            f"export current as of {current_date} (team_id {team_id} present)"
        )
    return PreflightResult(
        league, False,
        f"no export for {org!r} (team_id {team_id}) on {current_date} (will run)"
    )


# -----------------------------------------------------------------------------
# Batch + filter
# -----------------------------------------------------------------------------

def check_leagues(leagues: list[str]) -> dict[str, PreflightResult]:
    return {L: check_league(L) for L in leagues}


def filter_leagues(leagues: list[str], *, verbose: bool = True) -> list[str]:
    """Run preflight on ``leagues``, print decisions, return the leagues
    to actually process (those with ``skip=False``)."""
    print(f"\n=== Pre-flight: /exports check ({len(leagues)} leagues) ===")
    results = check_leagues(leagues)
    keep, skipped = [], []
    for L in leagues:
        r = results[L]
        if r.skip:
            skipped.append(L)
            print(f"  [{L}] SKIP — {r.reason}")
        else:
            keep.append(L)
            if verbose:
                print(f"  [{L}] run — {r.reason}")
    print(f"Pre-flight result: {len(keep)} to run, {len(skipped)} skipped")
    return keep
