#!/usr/bin/env python3
"""
run_trade_targets_all.py — Run ``trade_targets.py`` for every league in
``config/league_url.json``, generating per-league acquisition shopping lists
for the user's own org.

Per-league flags applied (all overridable via CLI):

- ``--league``        from league_url.json
- ``--org`` / ``--year``  auto-resolved by trade_targets.py from
                          config/league_settings.json (this orchestrator
                          doesn't have to pass them through)
- ``--cookie``        auto-resolved from config/statsplus_session.json
                      keyed by the league's base-URL hostname (same map
                      fetch_player_data.py uses)

Sequential execution — trade_targets fetches /tradeblock + /players + stat
endpoints per league, all calendar-day cached. Continues past per-league
failures and prints a summary.

Usage::

    py run_trade_targets_all.py                                  # all leagues
    py run_trade_targets_all.py --leagues sahl,uba               # subset
    py run_trade_targets_all.py --skip bwb                       # exclude
    py run_trade_targets_all.py --include-no-need                # forward to all runs
    py run_trade_targets_all.py --no-cookies --dry-run           # debug
    py run_trade_targets_all.py --no-preflight                   # bypass /exports current-date gate

Auth notes
----------
/tradeblock is gated behind a statsplus session login. This orchestrator
auto-fills --cookie for each league from ``config/statsplus_session.json``
(the same file fetch_player_data.py uses). Per-league cookies are looked up
by the hostname of the league's base URL — if every league lives on the
same statsplus instance, one cookie covers all of them.

A league is skipped (with a warning) if it has no ``org`` entry in
``config/league_settings.json``. A league with ``org`` but no resolvable
cookie still RUNS — trade_targets will log the missing-auth hint itself
and fail gracefully (the underlying /tradeblock call returns empty when
unauthenticated).
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
from fetch_player_data import load_cookie_for, load_league_base

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = REPO_ROOT / "config"
LEAGUE_URL_PATH = CONFIG_DIR / "league_url.json"
LEAGUE_SETTINGS_PATH = CONFIG_DIR / "league_settings.json"
TARGETS_SCRIPT = REPO_ROOT / "trade_targets.py"


def load_league_settings() -> dict:
    if not LEAGUE_SETTINGS_PATH.exists():
        raise SystemExit(
            f"Missing {LEAGUE_SETTINGS_PATH}. Need per-league 'org' to grade "
            f"trade targets against your roster across leagues."
        )
    with LEAGUE_SETTINGS_PATH.open(encoding="utf-8") as f:
        return json.load(f)


def resolve_cookie_for_league(league: str) -> Optional[str]:
    """Pull the statsplus_session.json cookie for the league's base host.
    Returns None when the league has no entry or no host can be resolved
    (caller decides whether to warn or continue).
    """
    try:
        base = load_league_base(league)
    except SystemExit:
        # load_league_base raises SystemExit when the league is missing.
        # Catch it so the bulk runner can surface a clean per-league skip
        # message instead of dying mid-loop.
        return None
    except Exception:
        return None
    return load_cookie_for(base)


def build_cmd(
    league: str,
    league_cfg: dict,
    cookie: Optional[str],
    *,
    include_no_need: bool,
    min_composite: Optional[float],
) -> Optional[list[str]]:
    """Construct the trade_targets.py command. Returns None if 'org' is missing.

    trade_targets.py auto-resolves --org and --year from league_settings.json,
    so this orchestrator only has to pass --league. Cookie is forwarded when
    resolved; missing cookie is not a hard skip (trade_targets logs its own
    warning and runs to a soft failure).
    """
    if not league_cfg.get("org"):
        return None
    cmd = [sys.executable, str(TARGETS_SCRIPT), "--league", league]
    if cookie:
        cmd.extend(["--cookie", cookie])
    if include_no_need:
        cmd.append("--include-no-need")
    if min_composite is not None:
        cmd.extend(["--min-composite", str(min_composite)])
    return cmd


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(
        description="Batch-run trade_targets.py for every configured league."
    )
    p.add_argument("--leagues",
                   help="Comma-separated subset (default: all leagues in league_url.json).")
    p.add_argument("--skip",
                   help="Comma-separated leagues to exclude.")
    p.add_argument("--include-no-need", action="store_true",
                   help="Forward --include-no-need to trade_targets (premium-only candidates at 'Set' positions).")
    p.add_argument("--min-composite", type=float, default=None,
                   help="Composite floor applied to every per-league trade_targets run. "
                        "Per-league overrides aren't currently read from league_settings.json — "
                        "run trade_targets.py directly if you need per-league floors.")
    p.add_argument("--no-cookies", action="store_true",
                   help="Don't auto-resolve cookies from statsplus_session.json. /tradeblock "
                        "will then likely return an empty list for any league behind a login. "
                        "Useful only when debugging cache behavior.")
    p.add_argument("--no-preflight", action="store_true",
                   help="Skip the /exports pre-flight check (default: enabled). "
                        "Tradeblock pids can change intraday, so pass --no-preflight "
                        "if you want to re-run without bumping the underlying export.")
    p.add_argument("--dry-run", action="store_true",
                   help="Print commands without running. Cookies are masked in the printed "
                        "command so the shell scrollback doesn't leak session state.")
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
                  "Pass --no-preflight to force a re-run anyway (tradeblock can "
                  "change without a fresh export).")
            return 0

    settings = load_league_settings()

    print(f"Running trade_targets.py for {len(leagues)} leagues: {', '.join(leagues)}")
    if args.include_no_need:
        print(f"  --include-no-need: on")
    if args.min_composite is not None:
        print(f"  --min-composite (all leagues): {args.min_composite}")
    if args.no_cookies:
        print(f"  --no-cookies: on (auth-gated /tradeblock will likely return empty)")

    results: dict[str, int] = {}
    start = time.monotonic()

    for i, L in enumerate(leagues, 1):
        league_cfg = settings.get(L, {})
        cookie = None if args.no_cookies else resolve_cookie_for_league(L)
        cmd = build_cmd(
            L, league_cfg, cookie,
            include_no_need=args.include_no_need,
            min_composite=args.min_composite,
        )
        print(f"\n[{i}/{len(leagues)}] === {L} ===")
        if cmd is None:
            print(f"  SKIP: missing 'org' in league_settings.json")
            results[L] = 1
            continue
        org = league_cfg["org"]
        cookie_status = "cookie ✓" if cookie else "no cookie (will likely fail auth)"
        print(f"  org={org!r}  {cookie_status}")
        # Mask the cookie in the printed command so terminal scrollback /
        # CI logs don't leak session state.
        printable = [arg if not (cookie and arg == cookie) else "<cookie>" for arg in cmd]
        print(f"  $ {' '.join(printable)}")
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
        print("Re-run individually with: py trade_targets.py --league X --cookie-file ...")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
