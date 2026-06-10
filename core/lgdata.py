#!/usr/bin/env python3
"""
lgdata.py — /lgdata fetch + game-world structure / standings helpers.

The ``/lgdata`` endpoint returns ONE JSON document describing a league file's
whole game world: ``leagues`` (ML + affiliated minors + foreign leagues),
``subleagues`` (AL/NL etc.), ``divisions``, ``teams`` (with all three structure
ids), and ``standings`` (a current W/L record row per team). One token-authed
GET replaces what previously took several config files to approximate.

Conventions match the other endpoint fetchers (stats.py / trade_targets.py):

- base URL from config/league_url.json, token from config/statsplus_tokens.json
- calendar-day disk cache via stats._cache_path_for_url / _is_cache_fresh,
  keyed on the token-less URL so auth never lands in a filename
- payload validated BEFORE caching (a cached error page would poison the
  rest of the day)
- **fail-open**: any error returns None; callers decide how to surface it

On a successful fetch, callers can persist the structural sections (everything
except standings) to ``config/structure-{league}.json`` via
:func:`write_structure_snapshot`, so the game-world tree survives offline.

Public API:
    fetch_lgdata(base_url, cache_dir=..., token=...) -> Optional[dict]
    resolve_token(league) -> Optional[str]
    write_structure_snapshot(league, data) -> Optional[Path]
    league_options(data) -> [(league_id, label)]
    division_standings(data, league_id) -> [{label, rows}]
    max_games_played(data, league_id) -> int
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
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.error import URLError
from urllib.request import Request, urlopen

import stats as sapi

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = REPO_ROOT / "config"
DEFAULT_LEAGUE_URL = CONFIG_DIR / "league_url.json"

_USER_AGENT = "Mozilla/5.0 (compatible; vosball-lgdata/1.0; +ratings-tooling)"

logger = logging.getLogger("lgdata")

# A response is only a valid /lgdata document when all five sections exist.
REQUIRED_KEYS = ("leagues", "subleagues", "divisions", "teams", "standings")


# -----------------------------------------------------------------------------
# Fetch
# -----------------------------------------------------------------------------

def resolve_token(league: str) -> Optional[str]:
    """Best-effort StatsPlus API token for ``league`` from
    config/statsplus_tokens.json (same resolver every other endpoint uses).
    Local import so lgdata stays usable if fetch_player_data is absent."""
    try:
        from fetch_player_data import load_token_for
        return load_token_for(league)
    except Exception as exc:  # noqa: BLE001 — token is optional; degrade quietly
        logger.debug("Token resolve failed for %s: %s", league, exc)
        return None


def _parse_lgdata_payload(payload: str) -> Optional[Dict[str, Any]]:
    """Parse + validate an /lgdata response. None when it isn't the expected
    five-section document (auth notice, HTML error page, truncated body…)."""
    try:
        data = json.loads(payload)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    if not all(isinstance(data.get(k), list) for k in REQUIRED_KEYS):
        return None
    return data


def fetch_lgdata(
    base_url: str,
    cache_dir: Optional[Path] = None,
    token: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Fetch the /lgdata document. None on any failure (fail-open).

    Mirrors trade_targets.fetch_tradeblock: the token rides only on the actual
    request URL (never the cache key), and the payload is validated before it
    is cached so a bad response can't poison the rest of the calendar day.
    """
    url = f"{base_url.rstrip('/')}/lgdata/"
    req_url = f"{url}?token={token}" if token else url

    cache_path: Optional[Path] = None
    if cache_dir:
        cache_path = sapi._cache_path_for_url(url, cache_dir)
        if sapi._is_cache_fresh(cache_path):
            try:
                parsed = _parse_lgdata_payload(
                    cache_path.read_text(encoding="utf-8"))
                if parsed:
                    logger.info("Cache hit  %s", cache_path.name)
                    return parsed
                logger.warning(
                    "Cached lgdata payload is unparseable; refetching %s",
                    cache_path.name)
            except (OSError, ValueError) as e:
                logger.warning("lgdata cache read failed (%s); refetching", e)

    logger.info("Fetching %s (auth: %s)", url, "token" if token else "none")
    try:
        req = Request(req_url, headers={"User-Agent": _USER_AGENT,
                                        "Accept": "application/json"})
        with urlopen(req, timeout=60) as resp:
            payload = resp.read().decode("utf-8-sig", errors="replace")
    except (URLError, TimeoutError, ValueError) as e:
        logger.warning("/lgdata fetch failed (%s): %s", url, e)
        return None

    parsed = _parse_lgdata_payload(payload)
    if not parsed:
        logger.warning("/lgdata fetched but didn't validate. Payload prefix: %r",
                       (payload or "(empty)")[:200])
        return None

    if cache_path:
        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(payload, encoding="utf-8")
        except OSError as e:
            logger.warning("Could not write lgdata cache %s: %s", cache_path, e)

    return parsed


def evict_cache(base_url: str, cache_dir: Path) -> None:
    """Delete the cached /lgdata payload so the next fetch_lgdata call hits the
    network even within the calendar-day TTL — a sim can land mid-day and the
    user wants the post-sim standings now."""
    try:
        sapi._cache_path_for_url(f"{base_url.rstrip('/')}/lgdata/",
                                 cache_dir).unlink(missing_ok=True)
    except OSError as e:
        logger.warning("Could not evict lgdata cache: %s", e)


# -----------------------------------------------------------------------------
# Structure snapshot — config/structure-{league}.json
# -----------------------------------------------------------------------------

def structure_path_for(league: str) -> Path:
    return CONFIG_DIR / f"structure-{league}.json"


def write_structure_snapshot(league: str,
                             data: Dict[str, Any]) -> Optional[Path]:
    """Persist the structural sections (everything except standings, which is
    point-in-time) so the game-world tree is retained offline. Overwrites the
    previous snapshot — structure changes are rare but real (expansion,
    realignment), and /lgdata is authoritative. None on write failure."""
    snap = {k: data.get(k) for k in ("leagues", "subleagues",
                                     "divisions", "teams")}
    path = structure_path_for(league)
    try:
        path.write_text(json.dumps(snap, indent=1), encoding="utf-8")
        return path
    except OSError as e:
        logger.warning("Could not write %s: %s", path, e)
        return None


def load_structure_snapshot(league: str) -> Optional[Dict[str, Any]]:
    """The last persisted structure snapshot, or None. Offline consumers'
    entry point — no standings in here, just the tree."""
    path = structure_path_for(league)
    try:
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        pass
    return None


# -----------------------------------------------------------------------------
# Pure structure / standings helpers
# -----------------------------------------------------------------------------

def league_options(data: Dict[str, Any]) -> List[Tuple[int, str]]:
    """(league_id, display label) for every game-world league, top level
    first. Label: 'MLB — Major League Baseball'."""
    leagues = sorted(data.get("leagues", []),
                     key=lambda l: (l.get("level", 99), l.get("league_id", 0)))
    return [(l["league_id"], f"{l.get('abbr', '?')} — {l.get('name', '?')}")
            for l in leagues if "league_id" in l]


def load_level_labels(path: Path = CONFIG_DIR / "id_maps.json") -> Dict[int, str]:
    """{1: 'ML', 2: 'AAA', ...} — id_maps.json's league_level dict, inverted.
    Same source as depth_chart.load_level_id_to_label, inlined here so lgdata
    stays dependency-light. Empty dict when the config is missing/bad."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    out: Dict[int, str] = {}
    for label, value in ((raw or {}).get("league_level") or {}).items():
        if not isinstance(label, str) or label.startswith("_"):
            continue
        try:
            out[int(value)] = label
        except (TypeError, ValueError):
            continue
    return out


def leagues_by_level(data: Dict[str, Any]) -> List[Tuple[int, List[Dict[str, Any]]]]:
    """``[(level, [league, ...]), ...]`` — levels ascending (1 = ML first),
    leagues within a level ordered by league_id (so IL before PCL at AAA).
    Lets renderers group same-level leagues (IL + PCL = the AAA view)."""
    by: Dict[int, List[Dict[str, Any]]] = {}
    for l in data.get("leagues", []):
        by.setdefault(int(l.get("level", 99)), []).append(l)
    return [(lvl, sorted(by[lvl], key=lambda l: l.get("league_id", 0)))
            for lvl in sorted(by)]


def max_games_played(data: Dict[str, Any], league_id: int) -> int:
    """Most games played by any team in one game-world league — 0 means the
    season hasn't started (standings are seed order, not results)."""
    lg_team_ids = {t["team_id"] for t in data.get("teams", [])
                   if t.get("league_id") == league_id}
    return max((int(s.get("g") or 0) for s in data.get("standings", [])
                if s.get("team_id") in lg_team_ids), default=0)


def division_standings(data: Dict[str, Any],
                       league_id: int) -> List[Dict[str, Any]]:
    """Per-division standings tables for one game-world league.

    Returns ``[{"label", "sub_league_id", "subleague", "division", "rows"},
    ...]`` ordered by subleague then division — ``sub_league_id`` lets
    renderers keep each subleague's divisions together (e.g. one column per
    subleague). Each row joins team identity onto its record:
    ``{team, abbr, w, l, t, pct, gb, streak, magic_number, pos}``, sorted by
    the server's ``pos`` (pct desc as tiebreak). Teams with no standings row
    still appear (zeros) so a division is never silently short."""
    subs = {(s.get("league_id"), s.get("sub_league_id")): s
            for s in data.get("subleagues", [])}
    divs = {(d.get("league_id"), d.get("sub_league_id"), d.get("division_id")): d
            for d in data.get("divisions", [])}
    recs = {s.get("team_id"): s for s in data.get("standings", [])}

    grouped: Dict[Tuple[int, int], List[Dict[str, Any]]] = {}
    for t in data.get("teams", []):
        if t.get("league_id") != league_id:
            continue
        key = (t.get("sub_league_id", 0), t.get("division_id", 0))
        rec = recs.get(t.get("team_id")) or {}
        grouped.setdefault(key, []).append({
            "team": f"{t.get('name', '')} {t.get('nickname', '')}".strip(),
            "abbr": t.get("abbr", ""),
            "w": int(rec.get("w") or 0),
            "l": int(rec.get("l") or 0),
            "t": int(rec.get("t") or 0),
            "pct": float(rec.get("pct") or 0.0),
            "gb": float(rec.get("gb") or 0.0),
            "streak": int(rec.get("streak") or 0),
            "magic_number": int(rec.get("magic_number") or 0),
            "pos": int(rec.get("pos") or 99),
        })

    out: List[Dict[str, Any]] = []
    for (sub_id, div_id) in sorted(grouped):
        sub_name = (subs.get((league_id, sub_id)) or {}).get("name", "")
        div_name = (divs.get((league_id, sub_id, div_id)) or {}).get(
            "name", f"Division {div_id}")
        label = f"{sub_name} — {div_name}" if sub_name else div_name
        rows = sorted(grouped[(sub_id, div_id)],
                      key=lambda r: (r["pos"], -r["pct"]))
        out.append({"label": label, "sub_league_id": sub_id,
                    "subleague": sub_name, "division": div_name, "rows": rows})
    return out
