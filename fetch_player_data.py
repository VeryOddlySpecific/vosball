#!/usr/bin/env python3
"""
fetch_player_data.py — Pull the /ratings endpoint CSV for a given league
and save it to ``data/PlayerData-{league}.csv`` (the canonical input path
for ``vos_v2.py``).

The endpoint is a two-step asynchronous flow:

1. GET ``{base}/ratings/`` — kicks off the export job. Response is plain
   text containing the polling URL ``{base}/mycsv/?request=GUID``.
2. GET the polling URL on an interval. While the job is still queued, the
   response body matches "Request ID ... still in progress". When done,
   the body is the CSV payload.

Auth, in order of preference:

1. **API token** (``?token=XXXX`` query param) — preferred. Set in
   ``config/statsplus_tokens.json``, keyed by league slug. Use ``_default``
   for a single token that applies to every league. Tokens are valid 90
   days; the polling URL automatically inherits the token when the kickoff
   request used one, so no extra plumbing needed.
2. **Session cookie** — fallback. Set in ``config/statsplus_session.json``,
   keyed by hostname. Same values your logged-in browser holds. Refresh
   from DevTools when auth starts failing.

Usage::

    py fetch_player_data.py --league ndl
    py fetch_player_data.py --league uba --osa
    py fetch_player_data.py --league sahl --out data/PlayerData-sahl.csv

Exit codes: 0 ok, 1 config error, 2 auth likely expired, 3 timed out,
4 unexpected HTTP error.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import time
from pathlib import Path
from typing import Optional
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

logger = logging.getLogger("fetch_player_data")

REPO_ROOT = Path(__file__).resolve().parent
LEAGUE_URL_PATH = REPO_ROOT / "config" / "league_url.json"
TOKENS_PATH = REPO_ROOT / "config" / "statsplus_tokens.json"
SESSION_PATH = REPO_ROOT / "config" / "statsplus_session.json"
DEFAULT_OUT_DIR = REPO_ROOT / "data"

# Polling URL embedded in the initial response — accept either http(s) and
# any host so this keeps working across statsplus.net / atl-NN.statsplus.net.
MYCSV_URL_RE = re.compile(r"https?://[^\s\"'<>]+/api/mycsv/\?request=[A-Za-z0-9\-]+")

# Body returned while the job is still queued. Match leniently — exact
# wording per docs is "Request ID X still in progress, check back soon."
IN_PROGRESS_RE = re.compile(r"still in progress", re.IGNORECASE)


# -----------------------------------------------------------------------------
# Config loading
# -----------------------------------------------------------------------------

def load_league_base(league: str) -> str:
    if not LEAGUE_URL_PATH.exists():
        raise SystemExit(f"Missing {LEAGUE_URL_PATH}")
    with LEAGUE_URL_PATH.open(encoding="utf-8") as f:
        urls = json.load(f)
    base = urls.get(league)
    if not base:
        raise SystemExit(
            f"League '{league}' not in league_url.json. "
            f"Known: {', '.join(sorted(urls))}"
        )
    return base.rstrip("/")


def load_token_for(league: str) -> Optional[str]:
    """Look up an API token for ``league``. Returns None if no token is
    configured (caller should then try cookie auth).

    Resolution order: explicit ``league`` key → ``_default`` key → None.
    """
    if not TOKENS_PATH.exists():
        return None
    with TOKENS_PATH.open(encoding="utf-8") as f:
        tokens = json.load(f)
    tok = tokens.get(league) or tokens.get("_default")
    if tok and not tok.startswith("PASTE"):
        return tok
    return None


def load_cookie_for(base_url: str) -> Optional[str]:
    """Look up the session cookie string for the host serving ``base_url``.

    Returns None if not configured. Caller decides whether the absence is
    fatal (it only is when there's also no token).
    """
    if not SESSION_PATH.exists():
        return None
    with SESSION_PATH.open(encoding="utf-8") as f:
        sessions = json.load(f)
    host = urlparse(base_url).hostname or ""
    cookie = sessions.get(host)
    if cookie and not cookie.startswith("csrftoken=PASTE"):
        return cookie
    return None


# -----------------------------------------------------------------------------
# HTTP
# -----------------------------------------------------------------------------

def _get(url: str, cookie: Optional[str], timeout: int = 60) -> tuple[int, str]:
    """GET ``url`` optionally with a session cookie (None when using token
    auth via query param). Returns (status, body_text).
    """
    headers = {
        # Mimic a real browser; some StatsPlus paths sniff UA and 403 on
        # the bare urllib default.
        "User-Agent": "Mozilla/5.0 (fetch_player_data.py)",
        "Accept": "text/csv,text/plain,*/*",
    }
    if cookie:
        headers["Cookie"] = cookie
    req = Request(url, headers=headers)
    try:
        with urlopen(req, timeout=timeout) as resp:
            # Endpoint returns text/plain for the polling-URL message and
            # CSV when done; either way we want decoded text.
            return resp.status, resp.read().decode("utf-8", errors="replace")
    except HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", errors="replace")
        except Exception:
            pass
        return e.code, body


# -----------------------------------------------------------------------------
# Workflow
# -----------------------------------------------------------------------------

def kick_off(base_url: str, token: Optional[str], cookie: Optional[str], osa: bool) -> str:
    """Hit /ratings and return the polling URL extracted from the response.

    ``token`` and ``cookie`` are mutually optional but at least one must be
    set (enforced earlier). If both are passed, token wins (cleaner auth)
    and cookie is ignored.
    """
    params = []
    if token:
        params.append(f"token={token}")
    if osa:
        params.append("osa=1")
    qs = ("?" + "&".join(params)) if params else ""
    url = f"{base_url}/ratings/{qs}"
    cookie_arg = None if token else cookie
    logger.info("Requesting %s (auth: %s)", url, "token" if token else "cookie")
    status, body = _get(url, cookie_arg)
    if status in (401, 403):
        which = "token in config/statsplus_tokens.json" if token else \
                "cookies in config/statsplus_session.json"
        raise SystemExit(
            f"Got HTTP {status} from {url}. Auth likely expired — refresh {which}."
        )
    if status >= 400:
        logger.error("HTTP %s body: %s", status, body[:500])
        raise SystemExit(f"Unexpected HTTP {status} from {url}")
    # Heuristic: if we got CSV back directly (no async wait), the response
    # will be many lines with commas. Bail with a clearer message because
    # nothing in the public docs suggests this path exists today.
    if "still in progress" in body.lower():
        # Shouldn't happen on the kickoff response, but tolerate.
        logger.warning("Kickoff response already looks like a poll body; continuing.")
    m = MYCSV_URL_RE.search(body)
    if not m:
        # If the endpoint redirected us to login the body will be HTML.
        snippet = body[:300].replace("\n", " ")
        raise SystemExit(
            "Could not find polling URL in /ratings response. "
            f"Likely an auth/redirect issue. Body starts: {snippet!r}"
        )
    poll_url = m.group(0)
    logger.info("Polling URL: %s", poll_url)
    return poll_url


def poll(poll_url: str, cookie: Optional[str], poll_interval: int, timeout_minutes: int) -> str:
    """Poll ``poll_url`` until the response is no longer an in-progress
    message. Returns the CSV body text.

    Caller is responsible for appending a ``token=`` query param to
    ``poll_url`` if token auth is in use. (The docs claim the server
    embeds it automatically; in practice — at least for some hosts — it
    doesn't.)
    """
    deadline = time.monotonic() + timeout_minutes * 60
    attempt = 0
    # First poll gets a short delay — server almost never finishes in <30s
    # per docs, but no point hammering immediately either.
    initial_wait = min(15, poll_interval)
    logger.info("Waiting %ds before first poll...", initial_wait)
    time.sleep(initial_wait)

    while True:
        attempt += 1
        if time.monotonic() > deadline:
            raise SystemExit(
                f"Timed out after {timeout_minutes}m waiting for export "
                f"(polled {attempt - 1} times). Try increasing --timeout "
                f"or check the league's StatsPlus status."
            )
        status, body = _get(poll_url, cookie)
        if status >= 400:
            logger.error("HTTP %s on poll attempt %d. Body: %s", status, attempt, body[:300])
            raise SystemExit(f"Polling failed with HTTP {status}")
        if IN_PROGRESS_RE.search(body):
            elapsed = int(time.monotonic() - (deadline - timeout_minutes * 60))
            logger.info("Attempt %d (~%ds elapsed): still in progress.", attempt, elapsed)
            time.sleep(poll_interval)
            continue
        # Sanity-check: the payload should look like CSV (have a comma in
        # the first ~200 chars and at least a couple newlines).
        head = body[:200]
        if "," not in head or body.count("\n") < 5:
            raise SystemExit(
                "Polling URL returned a non-progress response that doesn't "
                f"look like CSV. First 200 chars: {head!r}"
            )
        logger.info("Export ready (attempt %d, %d bytes).", attempt, len(body))
        return body


def save_csv(body: str, out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(body, encoding="utf-8")
    logger.info("Wrote %s (%d bytes)", out_path, out_path.stat().st_size)


# -----------------------------------------------------------------------------
# CLI
# -----------------------------------------------------------------------------

def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[1] if __doc__ else "")
    parser.add_argument("--league", required=True, help="League slug (e.g. ndl, uba, sahl).")
    parser.add_argument("--osa", action="store_true", help="Pull OSA ratings (default: scouted).")
    parser.add_argument("--out", type=Path, default=None,
                        help="Output path. Default: data/PlayerData-{league}.csv")
    parser.add_argument("--poll-interval", type=int, default=30,
                        help="Seconds between polls (default: 30).")
    parser.add_argument("--timeout", type=int, default=10,
                        help="Total minutes before giving up (default: 10).")
    # Resume options — let you re-poll an export already in flight without
    # hitting /ratings again (rate-limited, ~3min cooldown).
    resume = parser.add_mutually_exclusive_group()
    resume.add_argument("--request-id",
                        help="Skip kickoff; poll an existing request GUID. "
                             "Built URL: https://statsplus.net/{slug}/api/mycsv/?request={id}")
    resume.add_argument("--poll-url",
                        help="Skip kickoff; poll this exact URL. Use when "
                             "--request-id's built URL is wrong for the league.")
    parser.add_argument("-v", "--verbose", action="store_true", help="Debug logging.")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    base = load_league_base(args.league)
    token = load_token_for(args.league)
    cookie = None if token else load_cookie_for(base)
    if not token and not cookie:
        raise SystemExit(
            f"No auth configured for league '{args.league}'. "
            f"Add a token to {TOKENS_PATH} (preferred) or session cookies "
            f"to {SESSION_PATH}."
        )
    out_path = args.out or (DEFAULT_OUT_DIR / f"PlayerData-{args.league}.csv")

    if args.poll_url:
        poll_url = args.poll_url
        logger.info("Resuming with explicit poll URL.")
    elif args.request_id:
        # Polling lives on statsplus.net (no subdomain) per observed
        # behavior — even when the kickoff host is atl-01/atl-02. Build
        # the path from the league's base so the slug is right.
        parsed = urlparse(base)
        poll_url = f"https://statsplus.net{parsed.path}/mycsv/?request={args.request_id}"
        logger.info("Resuming request %s at %s", args.request_id, poll_url)
    else:
        poll_url = kick_off(base, token, cookie, args.osa)
    # Docs claim the polling URL inherits the token from the kickoff
    # request. In practice (observed on atl-01.statsplus.net) the server
    # returns the polling URL on a different host without the token, so
    # we append it ourselves when needed.
    if token and "token=" not in poll_url:
        sep = "&" if "?" in poll_url else "?"
        poll_url = f"{poll_url}{sep}token={token}"
        logger.info("Appended token to polling URL.")
    csv_body = poll(poll_url, None if token else cookie, args.poll_interval, args.timeout)
    save_csv(csv_body, out_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
