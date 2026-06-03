#!/usr/bin/env python3
"""
run_waiver_wire_all.py — Run ``waiver_wire.py`` for every league in
``config/league_url.json``, generating per-league waiver claim reports for
the user's own org in each.

Per-league flags applied (all overridable via CLI):

- ``--league``               from league_url.json
- ``--org`` / ``--year``     auto-resolved by waiver_wire.py from
                             config/league_settings.json (this orchestrator
                             doesn't have to pass them through)

Sequential execution — waiver_wire fetches /players and the stat endpoints
per league, both calendar-day cached, so a second run on the same day is
cheap. Continues past per-league failures and prints a summary.

Usage::

    py run_waiver_wire_all.py                                  # all leagues
    py run_waiver_wire_all.py --leagues ndl,uba                # subset
    py run_waiver_wire_all.py --skip bwb                       # exclude
    py run_waiver_wire_all.py --include-prospects              # forward flag to waiver_wire
    py run_waiver_wire_all.py --include-no-need                # forward flag to waiver_wire
    py run_waiver_wire_all.py --no-preflight --dry-run         # debug iteration order

A league is skipped (with a warning) if it has no ``org`` entry in
``config/league_settings.json`` — waiver_wire needs the org name to grade
the wire against your roster.
"""

from __future__ import annotations
# --- tools/ -> repo-root bootstrap (added during tools/ move) ---
import os as _os, sys as _sys
_ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
if _ROOT not in _sys.path:
    _sys.path.insert(0, _ROOT)
# --- end bootstrap ---


import argparse
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

from preflight import filter_leagues as preflight_filter

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = REPO_ROOT / "config"
LEAGUE_URL_PATH = CONFIG_DIR / "league_url.json"
LEAGUE_SETTINGS_PATH = CONFIG_DIR / "league_settings.json"
WAIVER_SCRIPT = REPO_ROOT / "tools" / "waiver_wire.py"


def load_league_settings() -> dict:
    if not LEAGUE_SETTINGS_PATH.exists():
        raise SystemExit(
            f"Missing {LEAGUE_SETTINGS_PATH}. Need per-league 'org' to grade "
            f"waivers against your roster across leagues."
        )
    with LEAGUE_SETTINGS_PATH.open(encoding="utf-8") as f:
        return json.load(f)


def build_cmd(
    league: str,
    league_cfg: dict,
    *,
    include_prospects: bool,
    include_no_need: bool,
    include_retired: bool,
    min_composite: Optional[float],
) -> Optional[list[str]]:
    """Construct the waiver_wire.py command. Returns None if 'org' is missing
    (caller logs and skips).

    waiver_wire.py auto-resolves --org and --year from league_settings.json
    on its own, so we don't pass them through here — but we still verify the
    org is present up-front so the bulk runner can surface a clean skip
    message instead of letting waiver_wire fail late.
    """
    org = league_cfg.get("org")
    if not org:
        return None
    cmd = [sys.executable, str(WAIVER_SCRIPT), "--league", league]
    if include_prospects:
        cmd.append("--include-prospects")
    if include_no_need:
        cmd.append("--include-no-need")
    if include_retired:
        cmd.append("--include-retired")
    if min_composite is not None:
        cmd.extend(["--min-composite", str(min_composite)])
    return cmd


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(
        description="Batch-run waiver_wire.py for every configured league."
    )
    p.add_argument("--leagues",
                   help="Comma-separated subset (default: all leagues in league_url.json).")
    p.add_argument("--skip",
                   help="Comma-separated leagues to exclude.")
    p.add_argument("--include-prospects", action="store_true",
                   help="Forward --include-prospects to waiver_wire (include sub-AAA waivers).")
    p.add_argument("--include-no-need", action="store_true",
                   help="Forward --include-no-need to waiver_wire (premium pickups even at 'Set' positions).")
    p.add_argument("--include-retired", action="store_true",
                   help="Forward --include-retired to waiver_wire (keep retired-flagged waiver rows).")
    p.add_argument("--min-composite", type=float, default=None,
                   help="Composite floor passed through to every per-league waiver_wire run. "
                        "Per-league overrides are not currently read from league_settings.json — "
                        "if you want per-league floors, run waiver_wire.py directly with --min-composite.")
    p.add_argument("--no-preflight", action="store_true",
                   help="Skip the /exports pre-flight check (default: enabled). "
                        "Pre-flight skips any league whose org already has a "
                        "valid export for the current sim date — same logic the "
                        "VOS/depth-chart batch runners use. Waivers can change "
                        "intraday, so pass --no-preflight if you want to re-run "
                        "the wire without bumping the underlying export.")
    p.add_argument("--dry-run", action="store_true",
                   help="Print commands without running.")
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
        print("No leagues to run after filtering.", file=sys.stderr)
        return 1

    if not args.no_preflight:
        leagues = preflight_filter(leagues)
        if not leagues:
            print("\nNothing to run — every league's export is already current. "
                  "Pass --no-preflight to force a re-run anyway (waivers can change "
                  "without a fresh export).")
            return 0

    settings = load_league_settings()

    print(f"Running waiver_wire.py for {len(leagues)} leagues: {', '.join(leagues)}")
    if args.include_prospects:
        print(f"  --include-prospects: on")
    if args.include_no_need:
        print(f"  --include-no-need: on")
    if args.include_retired:
        print(f"  --include-retired: on")
    if args.min_composite is not None:
        print(f"  --min-composite (all leagues): {args.min_composite}")

    results: dict[str, int] = {}
    start = time.monotonic()

    for i, L in enumerate(leagues, 1):
        league_cfg = settings.get(L, {})
        cmd = build_cmd(
            L, league_cfg,
            include_prospects=args.include_prospects,
            include_no_need=args.include_no_need,
            include_retired=args.include_retired,
            min_composite=args.min_composite,
        )
        print(f"\n[{i}/{len(leagues)}] === {L} ===")
        if cmd is None:
            print(f"  SKIP: missing 'org' in league_settings.json")
            results[L] = 1
            continue
        org = league_cfg["org"]
        print(f"  org={org!r}")
        print(f"  $ {' '.join(cmd)}")
        if args.dry_run:
            results[L] = 0
            continue
        rc = subprocess.run(cmd).returncode
        results[L] = rc

    elapsed = int(time.monotonic() - start)
    ok = [L for L, rc in results.items() if rc == 0]
    failed = [L for L, rc in results.items() if rc != 0]
    print(f"\n=== Summary (elapsed {elapsed // 60}m {elapsed % 60}s) ===")
    print(f"OK ({len(ok)}): {', '.join(ok) or '(none)'}")
    if failed:
        print(f"FAILED ({len(failed)}): {', '.join(failed)}")
        print("Re-run individually with: py waiver_wire.py --league X")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
