#!/usr/bin/env python3
"""
check_exports.py — Run the bulk-runner pre-flight check and report which
leagues still need an export for the current sim date.

Uses the same logic as fetch_all_player_data / run_vos_all / run_depth_chart_all
(``preflight.check_leagues``), but does no fetching — just prints the verdict.

Usage::

    py check_exports.py                  # all leagues in league_url.json
    py check_exports.py --leagues ndl,uba
    py check_exports.py --skip bwb
    py check_exports.py --quiet          # only print the leagues that need export
"""

from __future__ import annotations
# --- repo-root + core/ path bootstrap ---
import os as _os, sys as _sys
_ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
for _p in (_ROOT, _os.path.join(_ROOT, "core")):
    if _p not in _sys.path:
        _sys.path.insert(0, _p)
# --- end bootstrap ---


import argparse
import datetime
import json
import sys
from pathlib import Path

from preflight import check_leagues

REPO_ROOT = Path(__file__).resolve().parent.parent
LEAGUE_URL_PATH = REPO_ROOT / "config" / "league_url.json"
LEAGUE_SETTINGS_PATH = REPO_ROOT / "config" / "league_settings.json"
DATA_DIR = REPO_ROOT / "data"


def _load_league_settings() -> dict:
    if not LEAGUE_SETTINGS_PATH.exists():
        return {}
    try:
        with LEAGUE_SETTINGS_PATH.open(encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def _playerdata_freshness(league: str) -> str:
    """Return a short tag indicating whether the PlayerData CSV was updated today."""
    path = DATA_DIR / f"PlayerData-{league}.csv"
    if not path.exists():
        return "no file"
    mtime = datetime.datetime.fromtimestamp(path.stat().st_mtime).date()
    today = datetime.date.today()
    if mtime == today:
        return "data updated today"
    delta = (today - mtime).days
    return f"data {delta}d old ({mtime})"


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    p.add_argument("--leagues",
                   help="Comma-separated subset (default: all leagues in league_url.json).")
    p.add_argument("--skip", help="Comma-separated leagues to exclude.")
    p.add_argument("--quiet", action="store_true",
                   help="Only print the leagues that still need an export.")
    args = p.parse_args(argv)

    if not LEAGUE_URL_PATH.exists():
        print(f"Missing {LEAGUE_URL_PATH}", file=sys.stderr)
        return 1
    with LEAGUE_URL_PATH.open(encoding="utf-8") as f:
        all_leagues = sorted(json.load(f).keys())

    if args.leagues:
        leagues = [x.strip() for x in args.leagues.split(",") if x.strip()]
        unknown = sorted(set(leagues) - set(all_leagues))
        if unknown:
            print(f"Unknown leagues: {unknown}. Known: {all_leagues}", file=sys.stderr)
            return 1
    else:
        leagues = list(all_leagues)
    if args.skip:
        skips = {x.strip() for x in args.skip.split(",")}
        leagues = [L for L in leagues if L not in skips]

    if not leagues:
        print("No leagues to check after filtering.", file=sys.stderr)
        return 1

    results = check_leagues(leagues)
    need, done = [], []
    for L in leagues:
        (done if results[L].skip else need).append(L)

    if args.quiet:
        for L in need:
            print(L)
        return 0

    settings = _load_league_settings()

    print(f"\n=== Export status ({len(leagues)} leagues) ===")
    for L in leagues:
        r = results[L]
        tag = "OK  " if r.skip else "NEED"
        fresh = _playerdata_freshness(L)
        cfg = settings.get(L) or {}
        version = cfg.get("game_version", "?")
        sim_time = cfg.get("sim_time", "?")
        print(f"  [{tag}] {L} ({version}) — {r.reason}  |  {fresh}  |  sim: {sim_time}")

    print()
    if need:
        print(f"Need export ({len(need)}): {', '.join(need)}")
    else:
        print("All leagues current — nothing to export.")
    if done:
        print(f"Already current ({len(done)}): {', '.join(done)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
