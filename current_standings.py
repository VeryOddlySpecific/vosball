#!/usr/bin/env python3
"""
current_standings.py — Pull current-season standings from the StatsPlus
public standings HTML report, used by project_season.py's
--use-current-standings mode to split projections into actual-to-date plus
rest-of-season.

Two paths are exposed:

1. ``fetch_standings_via_html`` (default) — scrapes the standings report at
   ``{web_base}/reports/news/html/leagues/league_{lid}_standings.html``. This
   is the path currently in use because the StatsPlus /gamehistory API
   endpoint is returning a download prompt rather than raw CSV at the time
   of writing. Falls back gracefully when columns are missing.
2. ``fetch_standings_via_gamehistory`` — kept around for when the API is
   serving CSV again. Filters /gamehistory rows to the target year +
   league_id + regular-season + played, then sums W/L/RS/RA per team.

Both paths return ``{team_display_name: {"W", "L", "RS", "RA", "GP"}}``.
"""

from __future__ import annotations

import csv
import json
import logging
import re
from io import StringIO
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.error import URLError
from urllib.request import Request, urlopen

logger = logging.getLogger(__name__)


# -----------------------------------------------------------------------------
# Fetching
# -----------------------------------------------------------------------------

def _to_int(v: Any, default: int = 0) -> int:
    try:
        s = str(v).strip()
        return int(s) if s else default
    except (TypeError, ValueError):
        return default


def fetch_gamehistory(base_url: str) -> List[Dict[str, str]]:
    """One HTTP fetch of /gamehistory. Returns the parsed CSV rows."""
    url = f"{base_url.rstrip('/')}/gamehistory"
    logger.info("Fetching %s", url)
    with urlopen(url, timeout=120) as resp:
        payload = resp.read().decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(StringIO(payload))
    if not reader.fieldnames:
        raise ValueError(f"No CSV header at {url}")
    return list(reader)


# -----------------------------------------------------------------------------
# Aggregation
# -----------------------------------------------------------------------------

def standings_for_year(
    rows: List[Dict[str, str]],
    year: int,
    league_id: int,
) -> Dict[int, Dict[str, float]]:
    """Sum W/L/RS/RA per team_id from regular-season games played in ``year``
    within league ``league_id``.

    Returns: {team_id: {"W", "L", "RS", "RA", "GP"}}.
    """
    out: Dict[int, Dict[str, float]] = {}
    year_str = str(year)

    for r in rows:
        if (r.get("played") or "").strip() != "1":
            continue
        # game_type == 0 = regular season per OOTP convention. Skip playoffs/exhibitions.
        if (r.get("game_type") or "").strip() != "0":
            continue
        if _to_int(r.get("league_id")) != league_id:
            continue
        date = (r.get("date") or "").strip()
        if not date.startswith(year_str):
            continue

        home = _to_int(r.get("home_team"))
        away = _to_int(r.get("away_team"))
        runs_home = _to_int(r.get("runs0"))
        runs_away = _to_int(r.get("runs1"))
        if home <= 0 or away <= 0:
            continue

        for tid in (home, away):
            out.setdefault(tid, {"W": 0.0, "L": 0.0, "RS": 0.0, "RA": 0.0, "GP": 0.0})

        out[home]["RS"] += runs_home
        out[home]["RA"] += runs_away
        out[home]["GP"] += 1
        out[away]["RS"] += runs_away
        out[away]["RA"] += runs_home
        out[away]["GP"] += 1

        if runs_home > runs_away:
            out[home]["W"] += 1
            out[away]["L"] += 1
        elif runs_away > runs_home:
            out[away]["W"] += 1
            out[home]["L"] += 1
        # Ties (rare in baseball) are recorded as games played but not W or L.

    return out


def rekey_by_team_name(
    standings: Dict[int, Dict[str, float]],
    teams_map: Dict[int, str],
) -> Dict[str, Dict[str, float]]:
    """Re-key standings by team display name using the teams map."""
    out: Dict[str, Dict[str, float]] = {}
    for tid, rec in standings.items():
        name = teams_map.get(tid)
        if name:
            out[name] = rec
    return out


def load_teams_map(league: str, config_dir: Path) -> Dict[int, str]:
    """Load {team_id: 'Name Nickname'} from teams-{league}.json."""
    import json
    path = config_dir / f"teams-{league}.json"
    if not path.exists():
        return {}
    raw = json.loads(path.read_text(encoding="utf-8"))
    out: Dict[int, str] = {}
    for tid_str, info in raw.items():
        if tid_str.startswith("_") or not isinstance(info, dict):
            continue
        try:
            tid = int(tid_str)
        except (TypeError, ValueError):
            continue
        name = (info.get("Name") or "").strip()
        nick = (info.get("Nickname") or "").strip()
        display = f"{name} {nick}".strip()
        if display:
            out[tid] = display
    return out


# -----------------------------------------------------------------------------
# HTML standings scrape (current default — API /gamehistory returning download)
# -----------------------------------------------------------------------------

def _api_base_to_web_base(api_base_url: str) -> str:
    """Strip a trailing ``/api`` from the league API URL to get the web base.

    e.g. ``https://statsplus.net/sahl/api`` → ``https://statsplus.net/sahl``.
    """
    base = api_base_url.rstrip("/")
    if base.endswith("/api"):
        return base[:-4]
    return base


def fetch_standings_html(api_base_url: str, league_id: int) -> str:
    """Pull the standings report HTML for a given lid."""
    web_base = _api_base_to_web_base(api_base_url)
    url = f"{web_base}/reports/news/html/leagues/league_{league_id}_standings.html"
    logger.info("Fetching %s", url)
    req = Request(url, headers={"User-Agent": "Mozilla/5.0 (vos-toolkit)"})
    with urlopen(req, timeout=60) as resp:
        return resp.read().decode("utf-8-sig", errors="replace")


def _strip_tags(html_fragment: str) -> str:
    """Strip HTML tags and collapse whitespace from a small chunk."""
    text = re.sub(r"<[^>]+>", " ", html_fragment)
    return re.sub(r"\s+", " ", text).strip()


def parse_standings_html(html: str) -> Dict[str, Dict[str, float]]:
    """Parse OOTP standings HTML into ``{team_name: {W, L, RS, RA, GP}}``.

    OOTP standings reports group teams into multiple tables by division/league.
    Each division table is preceded by a heading row containing 'Division',
    then a header row with column labels (Team, W, L, PCT, GB, RS or R,
    RA or OR, ...), then one row per team.

    The parser walks every ``<table>`` in the page; for each table, it locates
    the header row, builds a column-index map, and reads team rows. Defensive
    enough to skip malformed rows. Returns short team names as they appear in
    the standings (often city-only, e.g. 'Toronto' rather than 'Toronto Blue
    Jays') — caller is responsible for mapping to canonical display names.
    """
    out: Dict[str, Dict[str, float]] = {}

    # Crude but effective: find every <tr>...</tr> in the document.
    table_pattern = re.compile(r"<table[^>]*>(.*?)</table>", re.IGNORECASE | re.DOTALL)
    row_pattern = re.compile(r"<tr[^>]*>(.*?)</tr>", re.IGNORECASE | re.DOTALL)
    cell_pattern = re.compile(r"<t[dh][^>]*>(.*?)</t[dh]>", re.IGNORECASE | re.DOTALL)

    def _is_int(s: str) -> bool:
        try:
            int(s.replace(",", ""))
            return True
        except ValueError:
            return False

    for table_match in table_pattern.finditer(html):
        table_html = table_match.group(1)
        rows = row_pattern.findall(table_html)
        if not rows:
            continue

        col_index: Dict[str, int] = {}
        for row_html in rows:
            cells = [_strip_tags(c) for c in cell_pattern.findall(row_html)]
            if not cells:
                continue

            # Header row detection: looks like W, L, PCT, RS, RA labels.
            cells_upper = [c.upper() for c in cells]
            if "W" in cells_upper and "L" in cells_upper and not col_index:
                for i, c in enumerate(cells_upper):
                    if c in ("W", "WINS"):
                        col_index["W"] = i
                    elif c in ("L", "LOSSES"):
                        col_index["L"] = i
                    elif c in ("RS", "R", "RUNS"):
                        col_index["RS"] = i
                    elif c in ("RA", "OR", "RAA"):
                        col_index["RA"] = i
                continue

            # If we have a column map and this looks like a data row, parse it.
            if col_index and len(cells) > max(col_index.values()):
                team = cells[0]
                # Skip section headers / totals / blank lines.
                if not team or team.upper() in {"TEAM", "DIVISION", "TOTAL"}:
                    continue
                if not _is_int(cells[col_index["W"]]) or not _is_int(cells[col_index["L"]]):
                    continue
                try:
                    w = int(cells[col_index["W"]].replace(",", ""))
                    l = int(cells[col_index["L"]].replace(",", ""))
                    rs = int(cells[col_index["RS"]].replace(",", "")) if "RS" in col_index else 0
                    ra = int(cells[col_index["RA"]].replace(",", "")) if "RA" in col_index else 0
                except (ValueError, KeyError):
                    continue
                out[team] = {"W": float(w), "L": float(l), "RS": float(rs), "RA": float(ra), "GP": float(w + l)}

    return out


def _build_short_to_full_map(teams_map: Dict[int, str]) -> Dict[str, str]:
    """Build an index from {short identifier: full display name} so HTML
    standings team labels (often city-only) can be mapped to the full names
    used everywhere else.

    Indexed under multiple shapes: full name, just-city, just-nickname, and
    lowercased forms — first match wins.
    """
    idx: Dict[str, str] = {}
    for full in teams_map.values():
        idx[full] = full
        idx[full.lower()] = full
        # Best-effort split into city + nickname.
        parts = full.rsplit(" ", 1)
        if len(parts) == 2:
            city, nick = parts[0], parts[1]
            idx.setdefault(city, full)
            idx.setdefault(city.lower(), full)
            idx.setdefault(nick, full)
            idx.setdefault(nick.lower(), full)
    return idx


# -----------------------------------------------------------------------------
# Public entry point
# -----------------------------------------------------------------------------

def fetch_standings_via_html(
    base_url: str,
    league_id: int,
    league: str,
    config_dir: Path,
) -> Dict[str, Dict[str, float]]:
    """Default path: scrape the standings HTML page and rekey to full team names."""
    html = fetch_standings_html(base_url, league_id)
    raw_standings = parse_standings_html(html)

    teams_map = load_teams_map(league, config_dir)
    short_to_full = _build_short_to_full_map(teams_map)

    out: Dict[str, Dict[str, float]] = {}
    unmatched: List[str] = []
    for short_name, rec in raw_standings.items():
        full = short_to_full.get(short_name) or short_to_full.get(short_name.lower())
        if not full:
            unmatched.append(short_name)
            continue
        out[full] = rec
    if unmatched:
        logger.warning(
            "Could not map %d standings team name(s) to teams config: %s",
            len(unmatched), ", ".join(unmatched),
        )
    return out


def fetch_standings_via_gamehistory(
    base_url: str,
    year: int,
    league_id: int,
    league: str,
    config_dir: Path,
) -> Dict[str, Dict[str, float]]:
    """Alternate path via the /gamehistory CSV endpoint. Use when the API
    is serving CSV (not a download prompt)."""
    rows = fetch_gamehistory(base_url)
    by_id = standings_for_year(rows, year, league_id)
    teams_map = load_teams_map(league, config_dir)
    return rekey_by_team_name(by_id, teams_map)


def fetch_standings_by_team_name(
    base_url: str,
    year: int,
    league_id: int,
    league: str,
    config_dir: Path,
) -> Dict[str, Dict[str, float]]:
    """Default convenience entry point — currently delegates to the HTML scrape.

    Year is accepted for API symmetry with the gamehistory path; the HTML
    standings page already represents the live current season so it's
    ignored here.
    """
    return fetch_standings_via_html(base_url, league_id, league, config_dir)
