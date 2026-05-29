#!/usr/bin/env python3
"""
fetch_all_player_data.py — Pull /ratings CSVs for every league in
``config/league_url.json`` using a staged parallel flow.

**Default mode (parallel staged):**

1. **Kickoff phase** — fire ``GET /ratings/?token=…`` at each league back-to-back
   (each one returns quickly, just queues a job on the server). Collect the
   polling URL for each.
2. **Polling phase** — every ``--poll-interval`` seconds, hit every still-pending
   league's polling URL once. Save the CSV for any league whose export is ready
   and drop it from the pending set. Repeat until pending is empty or
   ``--timeout`` minutes elapse.

Total wall time ≈ the slowest single league's export, not the sum of all. The
``/ratings`` rate limit is per-league, so firing 8 kickoffs in quick succession
is fine.

**Sequential fallback (``--sequential``)** — runs one league at a time via
subprocess. Slower but more isolated; useful if the parallel mode misbehaves.

Usage::

    py fetch_all_player_data.py                       # parallel, all leagues
    py fetch_all_player_data.py --osa
    py fetch_all_player_data.py --leagues ndl,uba
    py fetch_all_player_data.py --skip bwb
    py fetch_all_player_data.py --sequential          # one at a time
"""

from __future__ import annotations

import argparse
import json
import logging
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

# Reuse the building blocks from the single-league script.
from fetch_player_data import (
    DEFAULT_OUT_DIR,
    IN_PROGRESS_RE,
    _get,
    kick_off,
    load_cookie_for,
    load_league_base,
    load_token_for,
    save_csv,
)
from preflight import filter_leagues as preflight_filter

logger = logging.getLogger("fetch_all_player_data")

REPO_ROOT = Path(__file__).resolve().parent
LEAGUE_URL_PATH = REPO_ROOT / "config" / "league_url.json"
FETCH_SCRIPT = REPO_ROOT / "fetch_player_data.py"


# -----------------------------------------------------------------------------
# Parallel staged mode
# -----------------------------------------------------------------------------

def _kickoff_one(league: str, osa: bool) -> dict:
    """Kick off /ratings for a single league. Returns a state dict with the
    polling URL (token already appended), or marks the league failed.
    """
    state = {"league": league, "status": "pending", "poll_url": None,
             "cookie_for_poll": None, "error": None}
    try:
        base = load_league_base(league)
        token = load_token_for(league)
        cookie = None if token else load_cookie_for(base)
        if not token and not cookie:
            raise RuntimeError(
                "no auth configured (add token to statsplus_tokens.json "
                "or cookies to statsplus_session.json)"
            )
        poll_url = kick_off(base, token, cookie, osa)
        # Same fix as the single-league flow: the server doesn't always
        # append the token to the polling URL it hands back.
        if token and "token=" not in poll_url:
            sep = "&" if "?" in poll_url else "?"
            poll_url = f"{poll_url}{sep}token={token}"
        state["poll_url"] = poll_url
        state["cookie_for_poll"] = None if token else cookie
    except SystemExit as e:
        state["status"] = "failed"
        state["error"] = str(e)
    except Exception as e:
        state["status"] = "failed"
        state["error"] = f"{type(e).__name__}: {e}"
    return state


def _check_one(state: dict) -> tuple[str, Optional[str]]:
    """Poll one league's URL once. Returns (new_status, csv_body_or_None).

    ``new_status`` is one of: 'pending', 'done', 'failed'.
    """
    status, body = _get(state["poll_url"], state["cookie_for_poll"])
    if status >= 400:
        return "failed", None
    if IN_PROGRESS_RE.search(body):
        return "pending", None
    # Light CSV sanity check — guard against unexpected error pages that
    # don't match the in-progress pattern.
    head = body[:200]
    if "," not in head or body.count("\n") < 5:
        return "failed", None
    return "done", body


def run_parallel(leagues: list[str], osa: bool, poll_interval: int,
                 timeout_minutes: int) -> dict[str, int]:
    """Staged parallel run. Returns {league: exit_code_like}: 0 = ok,
    1 = failed.
    """
    results: dict[str, int] = {}
    states: dict[str, dict] = {}

    # --- Kickoff phase -------------------------------------------------------
    print(f"\n=== Kickoff phase ({len(leagues)} leagues) ===")
    for L in leagues:
        st = _kickoff_one(L, osa)
        states[L] = st
        if st["status"] == "failed":
            print(f"  [{L}] KICKOFF FAILED: {st['error']}")
            results[L] = 1
        else:
            print(f"  [{L}] queued: {st['poll_url']}")

    pending = {L: s for L, s in states.items() if s["status"] == "pending"}
    if not pending:
        return results

    # --- Polling phase -------------------------------------------------------
    print(f"\n=== Polling phase ({len(pending)} pending, "
          f"interval {poll_interval}s, timeout {timeout_minutes}m) ===")

    deadline = time.monotonic() + timeout_minutes * 60
    initial_wait = min(15, poll_interval)
    print(f"Waiting {initial_wait}s before first cycle...")
    time.sleep(initial_wait)

    cycle = 0
    while pending and time.monotonic() < deadline:
        cycle += 1
        elapsed = int(time.monotonic() - (deadline - timeout_minutes * 60))
        print(f"\n--- Cycle {cycle} (~{elapsed}s elapsed, "
              f"{len(pending)} pending) ---")
        finished_now = []
        for L, st in pending.items():
            try:
                new_status, body = _check_one(st)
            except Exception as e:
                print(f"  [{L}] poll error: {e}")
                results[L] = 1
                finished_now.append(L)
                continue
            if new_status == "pending":
                print(f"  [{L}] still in progress")
            elif new_status == "done":
                out_path = DEFAULT_OUT_DIR / f"PlayerData-{L}.csv"
                try:
                    save_csv(body, out_path)
                    print(f"  [{L}] DONE -> {out_path}")
                    results[L] = 0
                except Exception as e:
                    print(f"  [{L}] save failed: {e}")
                    results[L] = 1
                finished_now.append(L)
            else:  # failed
                print(f"  [{L}] FAILED (HTTP error or non-CSV body)")
                results[L] = 1
                finished_now.append(L)
        for L in finished_now:
            del pending[L]
        if pending:
            time.sleep(poll_interval)

    # Anything still pending hit the timeout
    for L in pending:
        print(f"  [{L}] TIMED OUT after {timeout_minutes}m")
        results[L] = 1

    return results


# -----------------------------------------------------------------------------
# Sequential fallback
# -----------------------------------------------------------------------------

def run_sequential(leagues: list[str], osa: bool, poll_interval: int,
                   timeout_minutes: int, delay: int, verbose: bool) -> dict[str, int]:
    """Run one league at a time via subprocess. Slow but maximally isolated."""
    results: dict[str, int] = {}
    for i, L in enumerate(leagues, 1):
        print(f"\n[{i}/{len(leagues)}] === {L} ===", flush=True)
        cmd = [
            sys.executable, str(FETCH_SCRIPT),
            "--league", L,
            "--poll-interval", str(poll_interval),
            "--timeout", str(timeout_minutes),
        ]
        if osa:
            cmd.append("--osa")
        if verbose:
            cmd.append("-v")
        rc = subprocess.run(cmd).returncode
        results[L] = rc
        if i < len(leagues) and delay > 0:
            print(f"Waiting {delay}s before next league...", flush=True)
            time.sleep(delay)
    return results


# -----------------------------------------------------------------------------
# CLI
# -----------------------------------------------------------------------------

def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(
        description="Batch-pull /ratings CSVs for every configured league."
    )
    p.add_argument("--leagues",
                   help="Comma-separated subset (default: all leagues in league_url.json).")
    p.add_argument("--skip",
                   help="Comma-separated leagues to exclude.")
    p.add_argument("--osa", action="store_true",
                   help="Pull OSA ratings for every league.")
    p.add_argument("--poll-interval", type=int, default=30,
                   help="Seconds between polling cycles (default: 30).")
    p.add_argument("--timeout", type=int, default=10,
                   help="Total minutes to wait for the slowest league (default: 10).")
    p.add_argument("--sequential", action="store_true",
                   help="One league at a time via subprocess. Default is parallel staged.")
    p.add_argument("--delay", type=int, default=0,
                   help="Sequential mode only: seconds between leagues.")
    p.add_argument("--no-preflight", action="store_true",
                   help="Skip the /exports pre-flight check (default: enabled). "
                        "Pre-flight skips any league whose org already has a "
                        "valid export for the current sim date.")
    p.add_argument("--force",
                   help="Comma-separated leagues to force-fetch even if preflight "
                        "would skip them (e.g. --force sdmb or --force sdmb,uba).")
    p.add_argument("-v", "--verbose", action="store_true",
                   help="Verbose logging.")
    args = p.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

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
        print("No leagues to fetch after filtering.", file=sys.stderr)
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
            # Add back any forced leagues that preflight dropped, in original order.
            forced_back = [L for L in leagues if L in forced and L not in preflight_result]
            if forced_back:
                print(f"Force-adding (preflight overridden): {', '.join(forced_back)}")
            leagues = preflight_result + forced_back
        else:
            leagues = preflight_result
        if not leagues:
            print("\nNothing to fetch — every league's export is already current.")
            return 0

    mode = "sequential" if args.sequential else "parallel (staged)"
    print(f"Mode: {mode}")
    print(f"Leagues ({len(leagues)}): {', '.join(leagues)}")
    if args.osa:
        print("(OSA ratings mode)")

    start = time.monotonic()
    if args.sequential:
        results = run_sequential(leagues, args.osa, args.poll_interval,
                                 args.timeout, args.delay, args.verbose)
    else:
        results = run_parallel(leagues, args.osa, args.poll_interval, args.timeout)
    elapsed = int(time.monotonic() - start)

    ok = [L for L, rc in results.items() if rc == 0]
    failed = [L for L, rc in results.items() if rc != 0]
    print(f"\n=== Summary (elapsed {elapsed // 60}m {elapsed % 60}s) ===")
    print(f"OK ({len(ok)}): {', '.join(ok) or '(none)'}")
    if failed:
        print(f"FAILED ({len(failed)}): {', '.join(failed)}")
        print("Re-run individual leagues with: py fetch_player_data.py --league X -v")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
