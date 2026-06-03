#!/usr/bin/env python3
"""
run_depth_chart_all.py — Run ``depth_chart.py`` for every league in
``config/league_url.json``, generating all-level depth charts for the
user's own org in each.

Per-league flags applied (all overridable via CLI):

- ``--league``               from league_url.json
- ``--org``                  from config/league_settings.json (required)
- ``--year``                 from config/league_settings.json (required)
- ``--park-factors``         auto-resolved to config/{league}-park-factors.json
- ``--all-level-charts``     always on (covers ML/AAA/AA/etc. in one run)
- ``--no-pdf``               always on (skip PDF output for speed)
- ``--min-comp``             optional; from CLI ``--min-comp`` (all leagues)
                             or per-league ``min_comp`` in league_settings.json

Sequential execution — depth_chart is local CPU work, no server polling.
Continues past per-league failures and prints a summary.

Usage::

    py run_depth_chart_all.py                            # all leagues
    py run_depth_chart_all.py --leagues ndl,uba          # subset
    py run_depth_chart_all.py --skip bwb                 # exclude
    py run_depth_chart_all.py --with-pdf                 # include PDF output
    py run_depth_chart_all.py --dry-run                  # print commands only

A league is skipped (with a warning) if its entry in
``config/league_settings.json`` is missing ``org`` or ``year``.
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
DEPTH_SCRIPT = REPO_ROOT / "depth_chart.py"


def load_league_settings() -> dict:
    if not LEAGUE_SETTINGS_PATH.exists():
        raise SystemExit(
            f"Missing {LEAGUE_SETTINGS_PATH}. Need per-league 'org' and "
            f"'year' to run depth_chart across leagues."
        )
    with LEAGUE_SETTINGS_PATH.open(encoding="utf-8") as f:
        return json.load(f)


def resolve_park_factors(league: str) -> Optional[Path]:
    """Combined teams[] format park-factors. Returns None if missing —
    depth_chart will run without ballpark adjustments.
    """
    candidate = CONFIG_DIR / f"{league}-park-factors.json"
    return candidate if candidate.exists() else None


def build_cmd(league: str, league_cfg: dict, *,
              all_level_charts: bool, no_pdf: bool,
              min_comp_cli: Optional[float]) -> Optional[list[str]]:
    """Construct the depth_chart.py command. Returns None if required
    settings are missing (caller logs and skips).

    ``min_comp_cli`` overrides the per-league ``min_comp`` setting; if
    neither is set, the flag is omitted (depth_chart applies no floor).
    """
    org = league_cfg.get("org")
    year = league_cfg.get("year")
    if not org or not year:
        return None
    cmd = [
        sys.executable, str(DEPTH_SCRIPT),
        "--league", league,
        "--org", str(org),
        "--year", str(year),
    ]
    pf = resolve_park_factors(league)
    if pf:
        cmd.extend(["--park-factors", str(pf)])
    if all_level_charts:
        cmd.append("--all-level-charts")
    if no_pdf:
        cmd.append("--no-pdf")
    min_comp = min_comp_cli if min_comp_cli is not None else league_cfg.get("min_comp")
    if min_comp is not None:
        cmd.extend(["--min-comp", str(min_comp)])
    return cmd


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(
        description="Batch-run depth_chart.py for every configured league."
    )
    p.add_argument("--leagues",
                   help="Comma-separated subset (default: all leagues in league_url.json).")
    p.add_argument("--skip",
                   help="Comma-separated leagues to exclude.")
    p.add_argument("--with-pdf", action="store_true",
                   help="Include PDF output (default: --no-pdf for speed).")
    p.add_argument("--min-comp", type=float, default=None,
                   help="Minimum composite score (20-80) for a player to occupy a Starter slot. "
                        "Applied to every league in this run. Per-league overrides via 'min_comp' "
                        "in league_settings.json (this CLI flag wins if both set).")
    p.add_argument("--single-level", action="store_true",
                   help="Disable --all-level-charts (depth_chart will need a --level, "
                        "which this batch doesn't pass — use only with --dry-run).")
    p.add_argument("--no-preflight", action="store_true",
                   help="Skip the /exports pre-flight check (default: enabled). "
                        "Pre-flight skips any league whose org already has a "
                        "valid export for the current sim date.")
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
            print("\nNothing to run — every league's export is already current.")
            return 0

    settings = load_league_settings()
    all_level_charts = not args.single_level
    no_pdf = not args.with_pdf

    print(f"Running depth_chart.py for {len(leagues)} leagues: {', '.join(leagues)}")
    print(f"  --all-level-charts: {all_level_charts}")
    print(f"  --no-pdf: {no_pdf}")
    if args.min_comp is not None:
        print(f"  --min-comp (all leagues): {args.min_comp}")

    results: dict[str, int] = {}
    start = time.monotonic()

    for i, L in enumerate(leagues, 1):
        league_cfg = settings.get(L, {})
        cmd = build_cmd(L, league_cfg,
                        all_level_charts=all_level_charts, no_pdf=no_pdf,
                        min_comp_cli=args.min_comp)
        print(f"\n[{i}/{len(leagues)}] === {L} ===")
        if cmd is None:
            print(f"  SKIP: missing 'org' or 'year' in league_settings.json")
            results[L] = 1
            continue
        org = league_cfg["org"]
        year = league_cfg["year"]
        print(f"  org={org!r}  year={year}")
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
        print("Re-run individually with: py depth_chart.py --league X --org \"...\" --year YYYY --all-level-charts")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
