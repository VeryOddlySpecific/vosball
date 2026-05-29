#!/usr/bin/env python3
"""
run_vos_all.py — Run ``run_vos.py`` (v10 engine) for every league in
``config/league_url.json``.

This orchestrator drives the v10 engine (v7 BABIP-feature Reach logistic for
hitters/SP + v9 personality-feature Reach logistic for RP + recalibrated v5
Career heuristic + Blended). Output CSVs carry the three v5/v6+ score columns
(VOS_Reach, VOS_Career, VOS_Blended) alongside the legacy aliases (VOS_Score,
VOS_Potential) — see scripts that consume evaluation_summary CSVs for which
columns they read.

Per-league defaults (all on, all overridable):

- ``--contracts``        always on (pulls /contract and /contractextension)
- ``--per-org-evals``    always on (requires combined teams[] format park-factors)
- ``--park-factors``     auto-resolved to ``config/{league}-park-factors.json`` if present
- ``--rating-scale``     defaults to run_vos's default (20-80); override per league via
                         ``config/league_settings.json``

Sequential execution — each league's VOS run is local CPU work (no
server polling), typically 10-30s. Continues past per-league failures
and prints a summary at the end.

Usage::

    py run_vos_all.py                              # all leagues, defaults
    py run_vos_all.py --leagues ndl,uba,sahl       # subset
    py run_vos_all.py --skip bwb                   # exclude
    py run_vos_all.py --no-per-org-evals           # disable per-org evals
    py run_vos_all.py --no-contracts               # disable contract pull

Override rating scale per league by editing ``config/league_settings.json``::

    {
      "ndl":  { "rating_scale": "20-80" },
      "sahl": { "rating_scale": "1-100" }
    }

Leagues not listed get run_vos.py's default (20-80).
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

from preflight import filter_leagues as preflight_filter

REPO_ROOT = Path(__file__).resolve().parent
CONFIG_DIR = REPO_ROOT / "config"
LEAGUE_URL_PATH = CONFIG_DIR / "league_url.json"
LEAGUE_SETTINGS_PATH = CONFIG_DIR / "league_settings.json"
VOS_SCRIPT = REPO_ROOT / "run_vos.py"


def load_league_settings() -> dict:
    """Return per-league overrides, or {} if the file doesn't exist."""
    if not LEAGUE_SETTINGS_PATH.exists():
        return {}
    with LEAGUE_SETTINGS_PATH.open(encoding="utf-8") as f:
        return json.load(f)


def resolve_park_factors(league: str) -> Optional[Path]:
    """Find the combined teams[] format park-factors file for a league.
    Returns None if missing — VOS will run without ballpark adjustments.

    Canonical name: ``config/{league}-park-factors.json``. The legacy
    ``config/park-factors-{league}.json`` files were archived to
    ``config/archive/`` on 2026-05-18 (they were the older single-park
    format, incompatible with --per-org-evals).
    """
    candidate = CONFIG_DIR / f"{league}-park-factors.json"
    return candidate if candidate.exists() else None


def build_cmd(league: str, settings: dict, *,
              contracts: bool, per_org_evals: bool) -> list[str]:
    """Construct the ``run_vos.py`` command for a single league."""
    cmd = [sys.executable, str(VOS_SCRIPT), "--league", league]
    if contracts:
        cmd.append("--contracts")
    if per_org_evals:
        cmd.append("--per-org-evals")
    pf = resolve_park_factors(league)
    if pf:
        cmd.extend(["--park-factors", str(pf)])
    rating_scale = settings.get(league, {}).get("rating_scale")
    if rating_scale:
        cmd.extend(["--rating-scale", rating_scale])
    return cmd


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(
        description="Batch-run run_vos.py (v10 engine) for every configured league."
    )
    p.add_argument("--leagues",
                   help="Comma-separated subset (default: all leagues in league_url.json).")
    p.add_argument("--skip",
                   help="Comma-separated leagues to exclude.")
    p.add_argument("--no-contracts", action="store_true",
                   help="Disable --contracts (skip /contract API pull).")
    p.add_argument("--no-per-org-evals", action="store_true",
                   help="Disable --per-org-evals (skip per-team eval folders).")
    p.add_argument("--no-preflight", action="store_true",
                   help="Skip the /exports pre-flight check (default: enabled). "
                        "Pre-flight skips any league whose org already has a "
                        "valid export for the current sim date.")
    p.add_argument("--force",
                   help="Comma-separated leagues to force-run even if preflight "
                        "would skip them (e.g. --force sdmb or --force sdmb,uba).")
    p.add_argument("--dry-run", action="store_true",
                   help="Print the command for each league without running.")
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

    forced: set[str] = set()
    if args.force:
        forced = {x.strip() for x in args.force.split(",") if x.strip()}
        unknown_forced = sorted(forced - set(leagues))
        if unknown_forced:
            print(f"--force references unknown/excluded leagues: {unknown_forced}", file=sys.stderr)
            return 1

    if not args.no_preflight:
        preflight_result = preflight_filter(leagues)
        if forced:
            forced_back = [L for L in leagues if L in forced and L not in preflight_result]
            if forced_back:
                print(f"Force-adding (preflight overridden): {', '.join(forced_back)}")
            leagues = preflight_result + forced_back
        else:
            leagues = preflight_result
        if not leagues:
            print("\nNothing to run — every league's export is already current.")
            return 0

    settings = load_league_settings()
    contracts = not args.no_contracts
    per_org_evals = not args.no_per_org_evals

    print(f"Running run_vos.py (v10) for {len(leagues)} leagues: {', '.join(leagues)}")
    print(f"  --contracts: {contracts}")
    print(f"  --per-org-evals: {per_org_evals}")
    print(f"  per-league settings: {sorted(settings.keys()) or '(none)'}")

    results: dict[str, int] = {}
    start = time.monotonic()

    for i, L in enumerate(leagues, 1):
        cmd = build_cmd(L, settings, contracts=contracts, per_org_evals=per_org_evals)
        print(f"\n[{i}/{len(leagues)}] === {L} ===")
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
        print("Re-run a single league with: py run_vos.py --league X --contracts ...")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
