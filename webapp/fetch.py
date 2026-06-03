"""VOSBall web UI — fresh-ratings pull for a single league.

Drives the same two-step StatsPlus /ratings export flow as fetch_player_data.py
(kick off the job, then poll until the CSV is ready — up to a few minutes),
reusing that script's building blocks so behavior matches the CLI exactly. The
difference: this is a *generator* that yields progress events between polls, so
the League Hub can show live progress in an st.status panel instead of blocking
silently.

Events yielded (dicts):
    {"type": "progress", "msg": str}   ongoing status line
    {"type": "done",     "msg": str, "bytes": int}   CSV saved (terminal)
    {"type": "error",    "msg": str}   gave up (terminal)

A pure consumer of fetch_player_data.py + the filesystem; nothing in vosball/ or
the CLI changes. Building blocks are referenced as module globals so tests can
monkeypatch them for fully-offline coverage.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Iterator, Optional

# Make the repo root importable (fetch_player_data.py lives there).
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Reuse the single-league script's proven pieces. Imported as module globals so
# tests can patch fetch.<name> to exercise the poll loop without a network.
from fetch_player_data import (  # noqa: E402
    DEFAULT_OUT_DIR, IN_PROGRESS_RE, _get, kick_off,
    load_cookie_for, load_league_base, load_token_for, save_csv,
)

DEFAULT_POLL_INTERVAL = 30      # seconds between polls (matches the CLI default)
DEFAULT_TIMEOUT_MINUTES = 10    # give up after this long


def fetch_league_ratings(
    league: str,
    *,
    osa: bool = False,
    poll_interval: int = DEFAULT_POLL_INTERVAL,
    timeout_minutes: int = DEFAULT_TIMEOUT_MINUTES,
) -> Iterator[dict]:
    """Pull fresh /ratings data for one league, yielding progress events and
    saving data/PlayerData-{league}.csv on success. Never raises for expected
    failures (config / auth / timeout / non-CSV) — those surface as an 'error'
    event so the caller can show them instead of crashing the page."""
    # --- Setup / auth (mirrors fetch_player_data.main) ----------------------
    try:
        base = load_league_base(league)
        token = load_token_for(league)
        cookie = None if token else load_cookie_for(base)
    except SystemExit as e:
        yield {"type": "error", "msg": str(e)}
        return
    except Exception as e:  # noqa: BLE001 — bad config JSON etc.
        yield {"type": "error", "msg": f"{type(e).__name__}: {e}"}
        return
    if not token and not cookie:
        yield {"type": "error", "msg": (
            "No auth configured for this league — add a token to "
            "config/statsplus_tokens.json (preferred) or cookies to "
            "config/statsplus_session.json.")}
        return

    # --- Kickoff ------------------------------------------------------------
    yield {"type": "progress", "msg": "Requesting a fresh export from StatsPlus…"}
    try:
        poll_url = kick_off(base, token, cookie, osa)
    except SystemExit as e:
        yield {"type": "error", "msg": str(e)}
        return
    except Exception as e:  # noqa: BLE001 — network/parse
        yield {"type": "error", "msg": f"Kickoff failed: {type(e).__name__}: {e}"}
        return
    # The server doesn't always echo the token on the polling URL (see the CLI).
    if token and "token=" not in poll_url:
        sep = "&" if "?" in poll_url else "?"
        poll_url = f"{poll_url}{sep}token={token}"
    poll_cookie = None if token else cookie

    # --- Poll until ready ---------------------------------------------------
    yield {"type": "progress",
           "msg": "Export queued — waiting for StatsPlus to build the file "
                  "(this can take a few minutes)…"}
    start = time.monotonic()
    deadline = start + timeout_minutes * 60
    time.sleep(min(15, poll_interval))  # the server rarely finishes in <30s

    attempt = 0
    while True:
        attempt += 1
        if time.monotonic() > deadline:
            yield {"type": "error",
                   "msg": f"Timed out after {timeout_minutes}m waiting for the "
                          f"export (polled {attempt - 1}×). Try again, or run "
                          f"`fetch_player_data.py --league {league}` in a terminal."}
            return
        try:
            status, body = _get(poll_url, poll_cookie)
        except Exception as e:  # noqa: BLE001 — transient network
            yield {"type": "error", "msg": f"Polling failed: {type(e).__name__}: {e}"}
            return
        if status >= 400:
            yield {"type": "error", "msg": f"Polling failed with HTTP {status}."}
            return
        if IN_PROGRESS_RE.search(body):
            elapsed = int(time.monotonic() - start)
            yield {"type": "progress",
                   "msg": f"Still building… (~{elapsed}s elapsed, poll {attempt})"}
            time.sleep(poll_interval)
            continue
        # Not in-progress: it should be the CSV. Sanity-check like the CLI does.
        head = body[:200]
        if "," not in head or body.count("\n") < 5:
            yield {"type": "error",
                   "msg": "StatsPlus returned a non-CSV response — auth may have "
                          "expired. Refresh the token/cookie and try again."}
            return
        out_path = DEFAULT_OUT_DIR / f"PlayerData-{league}.csv"
        try:
            save_csv(body, out_path)
        except Exception as e:  # noqa: BLE001 — disk/permission
            yield {"type": "error", "msg": f"Couldn't save the file: {e}"}
            return
        elapsed = int(time.monotonic() - start)
        yield {"type": "done",
               "msg": f"Saved {out_path.name} ({len(body):,} bytes) in ~{elapsed}s.",
               "bytes": len(body)}
        return
