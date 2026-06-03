#!/usr/bin/env python3
"""
trade_targets.py — The inverse of trade_block.py. Pull the league-wide list of
players on the trade block from the StatsPlus /tradeblock endpoint, evaluate
each one with the same composite/VOS pipeline depth_chart/trade_block use,
then match them against your org's actual needs to produce a ranked
"shopping list" of acquisition targets.

Why this lives next to trade_block.py instead of inside it
----------------------------------------------------------
- trade_block.py is org-scoped (your roster) and inward-facing (who YOU should
  shop). Its Acquisition Targets section already knows what archetypes you
  need; it just doesn't know who's actually available league-wide.
- /tradeblock provides the *availability* signal. This script joins that
  signal to trade_block's needs assessment so each candidate is graded
  against a real org hole, not just by raw rating.
- Keeping the two scripts separate lets the targets pipeline pull league-wide
  evals/stats (heavier I/O, different cache profile) without dragging that
  cost into the per-org trade_block run.

Inputs
------
- StatsPlus /tradeblock endpoint  -> {"player_ids": [...]}.
- Latest evaluation_summary_{league}_*.csv (league-wide; the same CSV
  trade_block.py loads, but consumed without org filtering).
- StatsPlus stat endpoints (hitter/pitcher) for current + prior years —
  shared cache with trade_block.py via stats.build_player_stats.
- config/depth_config.json (per-level roster sizes, role counts, weights).
- /players API override + optional OOTP roster CSV patch.

Outputs
-------
- {league}/trade_targets/{org}_trade_targets_{ts}.md  — tiered shopping list
- {league}/trade_targets/{org}_trade_targets_{ts}.csv — flat candidates

Usage
-----
    python trade_targets.py --league sahl                       # org/year auto-resolved
    python trade_targets.py --league sahl --org "Houston Astros" --year 2061

When --org and/or --year are omitted, the script reads them from
``config/league_settings.json`` keyed by --league (same source run_vos_all.py
and run_depth_chart_all.py use). Pass them explicitly to override the
configured defaults.
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.error import URLError
from urllib.request import Request, urlopen

# statsplus serves a different response to Python's default urllib UA
# (HTML error page, in some cases) than to browsers. Use a browser-ish UA
# so the API treats us as a normal client. Mirrors what curl/browser sends.
_USER_AGENT = (
    "Mozilla/5.0 (compatible; trade_targets.py/1.0; +ratings-tooling)"
)

import depth_chart as dc
import stats as sapi
import trade_block as tb

logger = logging.getLogger(__name__)
SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG = SCRIPT_DIR / "config" / "depth_config.json"
DEFAULT_LEAGUE_URL = SCRIPT_DIR / "config" / "league_url.json"
DEFAULT_LEAGUE_IDS = SCRIPT_DIR / "config" / "league_ids.json"
DEFAULT_LEAGUE_SETTINGS = SCRIPT_DIR / "config" / "league_settings.json"

HITTER_POSITIONS = dc.HITTER_POSITIONS
BLOCKING_LEVELS = tb.BLOCKING_LEVELS  # ML + AAA — the "available today" pool

# Need-tier weights for fit scoring. Critical needs reward fit much more
# heavily than Set positions (where we shouldn't be shopping at all).
NEED_TIER_WEIGHT: Dict[str, float] = {
    "Critical": 3.0,
    "Major":    2.0,
    "Depth":    1.0,
    "Set":      0.0,
}

# Composite floors mirroring trade_block.py's conventions, so candidates we
# tier as "Premium" here are interchangeable with chips trade_block tiers as
# "Premium" on the outbound side.
PREMIUM_COMPOSITE = tb.PREMIUM_COMPOSITE
MIN_TARGET_COMPOSITE = 42.0      # below this, not really a useful acquisition
LOTTERY_VOS_POT = tb.LOTTERY_VOS_POT
LOTTERY_AGE_CEILING = tb.LOTTERY_AGE_CEILING


# -----------------------------------------------------------------------------
# CLI
# -----------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=(
            "Build a ranked acquisition shopping list from the league-wide "
            "/tradeblock endpoint, matched against your org's needs."
        ),
    )
    p.add_argument("--league", required=True, help="League slug (e.g. sahl).")
    p.add_argument("--org", default=None,
                   help="Your organization display name (must match Org column in eval). "
                        "When omitted, resolved from config/league_settings.json keyed by --league.")
    p.add_argument("--org-code", type=str, default=None,
                   help="Subdirectory under {league}/eval/ to look in first for per-org evals.")
    p.add_argument("--year", type=int, default=None,
                   help="Latest year for stats window. When omitted, resolved from "
                        "config/league_settings.json keyed by --league; falls back to "
                        "the current calendar year if neither is set.")
    p.add_argument("--league-settings", type=Path, default=DEFAULT_LEAGUE_SETTINGS,
                   help="Path to league_settings.json (used to auto-resolve --org and --year).")
    p.add_argument("--input", type=Path, default=None,
                   help="Override evaluation_summary CSV.")
    p.add_argument("--config", type=Path, default=DEFAULT_CONFIG,
                   help="depth_config.json path.")
    p.add_argument("--league-url-config", type=Path, default=DEFAULT_LEAGUE_URL)
    p.add_argument("--league-ids-config", type=Path, default=DEFAULT_LEAGUE_IDS)
    p.add_argument("--base-url", type=str, default=None,
                   help="Override league API base URL.")
    p.add_argument("--output-dir", type=Path, default=None,
                   help="Output directory (default: {league}/trade_targets/).")
    p.add_argument("--tradeblock-input", type=Path, default=None,
                   help="Local JSON file with {'player_ids': [...]} — bypasses the API. "
                        "Useful when Dave's endpoint is flaky or for offline reruns.")
    p.add_argument("--cookie", type=str, default=None,
                   help="Session Cookie header value for the /tradeblock auth gate. "
                        "Copy from browser devtools (Network -> /tradeblock request -> "
                        "Headers -> Cookie). Format: 'key1=val1; key2=val2'. "
                        "Prefer --cookie-file for anything you want to keep around.")
    p.add_argument("--cookie-file", type=Path, default=None,
                   help="Path to a text file containing the Cookie header value. "
                        "Same format as --cookie; one line, no quoting. Use this so "
                        "the cookie isn't sitting in your shell history.")
    p.add_argument("--no-archive", action="store_true",
                   help="Skip auto-archive of prior runs in the output directory.")
    p.add_argument("--no-stats", action="store_true",
                   help="Skip stat fetch; composite uses VOS only (debugging).")
    p.add_argument("--no-cache", action="store_true",
                   help="Skip disk cache; force fresh API fetches.")
    p.add_argument("--cache-dir", type=Path, default=None,
                   help="Override cache directory (default: {league}/cache/stats/).")
    p.add_argument("--no-players-override", action="store_true",
                   help="Skip the /players API override of League_Level/Org/Team.")
    p.add_argument("--players-override-csv", type=Path, default=None, action="append",
                   help="OOTP roster CSV export to patch on top of /players (repeatable).")
    p.add_argument("--include-inactive", action="store_true",
                   help="Keep retired/DFA/waivered/DL60 players in the analysis.")
    p.add_argument("--levels", type=str, default=None,
                   help="Comma-separated subset of levels for the *own-org* needs analysis "
                        "(default: every level in depth_config). Candidate evaluation is "
                        "always ML+AAA-scoped since that's what's tradeable.")
    p.add_argument("--min-composite", type=float, default=MIN_TARGET_COMPOSITE,
                   help=f"Composite floor for candidates (default {MIN_TARGET_COMPOSITE}).")
    p.add_argument("--include-no-need", action="store_true",
                   help="Include candidates whose positions are 'Set' for the org. "
                        "Off by default — the shopping list focuses on real needs.")
    p.add_argument("--log-level", default="INFO",
                   choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    return p.parse_args()


# -----------------------------------------------------------------------------
# league_settings.json auto-resolve — keep CLI surface tight by sourcing
# org/year from the same file run_vos_all / run_depth_chart_all already use.
# -----------------------------------------------------------------------------

def load_league_settings(path: Path) -> Dict[str, Dict[str, Any]]:
    """Return the league_settings.json contents, or {} if the file is missing.

    Missing file is intentionally non-fatal — explicit --org / --year on the
    CLI is always a valid mode of operation, so failing here would be hostile
    to users who don't keep a settings file.
    """
    if not path.exists():
        return {}
    try:
        with path.open(encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("Could not read %s: %s — auto-resolve disabled.", path, e)
        return {}
    return data if isinstance(data, dict) else {}


def resolve_org_year(
    league: str,
    cli_org: Optional[str],
    cli_year: Optional[int],
    settings_path: Path,
) -> Tuple[Optional[str], Optional[int]]:
    """Fill in --org / --year from league_settings.json when not on the CLI.

    CLI values always win. Returns (org, year) — either may still be None if
    neither the CLI nor the settings file supplied a value (caller decides
    what to do; year falls back to the current calendar year downstream).
    """
    org = cli_org
    year = cli_year
    if org and year:
        return org, year

    settings = load_league_settings(settings_path)
    entry = settings.get(league) if isinstance(settings, dict) else None
    if not isinstance(entry, dict):
        return org, year

    if not org:
        candidate = entry.get("org")
        if isinstance(candidate, str) and candidate.strip():
            org = candidate.strip()
            logger.info("Resolved --org %r from %s", org, settings_path.name)

    if year is None:
        candidate_yr = entry.get("year")
        if isinstance(candidate_yr, int):
            year = candidate_yr
            logger.info("Resolved --year %d from %s", year, settings_path.name)
        elif isinstance(candidate_yr, str) and candidate_yr.strip().isdigit():
            year = int(candidate_yr.strip())
            logger.info("Resolved --year %d from %s", year, settings_path.name)

    return org, year


# -----------------------------------------------------------------------------
# /tradeblock fetch — JSON, not CSV. Lightweight: one URL, small payload.
# Reuses stats.py's calendar-day cache directory convention so the fetch
# slots into the existing cache hierarchy without a separate refresh story.
# -----------------------------------------------------------------------------

def resolve_cookie(literal: Optional[str], file_path: Optional[Path]) -> Optional[str]:
    """Pick a session cookie string from --cookie or --cookie-file.

    --cookie takes precedence (the explicit value the user typed); --cookie-
    file is the recommended persistent form. Trailing whitespace/newlines
    stripped — browsers occasionally append them when copying.
    """
    if literal:
        return literal.strip()
    if file_path:
        try:
            return file_path.read_text(encoding="utf-8").strip()
        except OSError as e:
            logger.warning("Could not read --cookie-file %s: %s", file_path, e)
    return None


def resolve_token(league: str) -> Optional[str]:
    """Best-effort StatsPlus API token for ``league`` from
    config/statsplus_tokens.json, reusing fetch_player_data.load_token_for (the
    same resolver the ratings/contracts/career-WAR features use). Returns None
    when no token is configured so the caller can fall back to cookie auth. The
    import is local so trade_targets stays usable even if fetch_player_data is
    absent in some stripped-down environment.
    """
    try:
        from fetch_player_data import load_token_for
        return load_token_for(league)
    except Exception as exc:  # noqa: BLE001 — token is optional; degrade quietly
        logger.debug("Token resolve failed for %s: %s", league, exc)
        return None


def fetch_tradeblock(
    base_url: str,
    cache_dir: Optional[Path] = None,
    cookie: Optional[str] = None,
    token: Optional[str] = None,
) -> List[str]:
    """Fetch the league-wide tradeblock list. Returns pids as strings to match
    the convention used throughout depth_chart/stats (eval CSV's ID column is
    string-typed even though the API returns ints).

    Empty list on network failure — caller decides whether that's fatal.

    The /tradeblock endpoint is auth-gated. Two ways to authenticate:

    - ``token`` — a StatsPlus API token, appended as ``?token=`` exactly like
      the stat/ratings endpoints. This is the preferred path: the CLI resolves
      one automatically from config/statsplus_tokens.json (see ``resolve_token``)
      so most runs need no flags at all.
    - ``cookie`` — a raw Cookie-header string (e.g.
      ``"sessionid=abc; csrftoken=xyz"``) from a logged-in browser session.
      Fallback for when no token is configured.

    A token, when present, wins over a cookie (cleaner auth) and the cookie is
    not sent. With neither, the server returns a plain-text "log in to a linked
    team" notice that won't parse as JSON, and this returns [].
    """
    url = f"{base_url.rstrip('/')}/tradeblock/"
    # The token is kept out of ``url`` so it never lands in a cache filename or
    # a log line; only the actual request (``req_url``) carries it.
    if token:
        req_url = f"{url}?token={token}"
        cookie = None  # token wins — don't also send the cookie header
    else:
        req_url = url

    # Calendar-day cache, same shape stats._fetch_csv uses. We piggyback on
    # _cache_path_for_url so cache layout is consistent across endpoints. Keyed
    # on the token-less ``url`` so the cache is stable regardless of auth method.
    cache_path: Optional[Path] = None
    if cache_dir:
        cache_path = sapi._cache_path_for_url(url, cache_dir)
        if sapi._is_cache_fresh(cache_path):
            try:
                payload = cache_path.read_text(encoding="utf-8")
                parsed = _parse_tradeblock_payload(payload)
                if parsed:
                    logger.info("Cache hit  %s", cache_path.name)
                    return parsed
                # Cached payload didn't parse — almost certainly a bad
                # response from a prior run (HTML error page, empty body).
                # Fall through to a fresh fetch and overwrite the cache.
                logger.warning(
                    "Cached tradeblock payload is unparseable; refetching %s",
                    cache_path.name,
                )
            except (OSError, ValueError) as e:
                logger.warning("Tradeblock cache read failed (%s); refetching", e)

    logger.info("Fetching %s (auth: %s)", url,
                "token" if token else "cookie" if cookie else "none")
    headers = {"User-Agent": _USER_AGENT, "Accept": "application/json"}
    if cookie:
        headers["Cookie"] = cookie
    try:
        req = Request(req_url, headers=headers)
        with urlopen(req, timeout=60) as resp:
            payload = resp.read().decode("utf-8-sig", errors="replace")
    except (URLError, TimeoutError, ValueError) as e:
        logger.warning("/tradeblock fetch failed (%s): %s", url, e)
        return []

    # Validate BEFORE caching. Caching a malformed payload would poison
    # every subsequent run for the rest of the calendar day until the
    # cache file ages out — exactly the bug that motivated this fix.
    parsed = _parse_tradeblock_payload(payload)
    if not parsed:
        prefix = payload[:200] if payload else "(empty)"
        logger.warning(
            "Tradeblock fetched but parsed to empty list. Payload prefix: %r",
            prefix,
        )
        # Targeted hint for the most common failure mode — easier than making
        # the user re-read the docs to figure out the cookie story.
        if "log in" in (payload or "").lower() or "logged in" in (payload or "").lower():
            logger.warning(
                "Server says you need to be logged in. Capture a Cookie header "
                "from your browser's devtools (Network tab -> /tradeblock request "
                "-> Headers) and pass it via --cookie or --cookie-file."
            )
        return []

    if cache_path:
        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(payload, encoding="utf-8")
        except OSError as e:
            logger.warning("Tradeblock cache write failed (%s): %s", url, e)

    return parsed


def _parse_tradeblock_payload(payload: str) -> List[str]:
    """Extract pids from the {'player_ids': [...]} envelope. Tolerant of int
    or string entries (the spec shows ints; we coerce to string for join
    consistency with eval row IDs)."""
    try:
        obj = json.loads(payload)
    except (json.JSONDecodeError, ValueError) as e:
        logger.warning("Tradeblock payload not JSON-decodable: %s", e)
        return []
    raw = obj.get("player_ids") if isinstance(obj, dict) else None
    if not isinstance(raw, list):
        logger.warning("Tradeblock payload missing 'player_ids' list; got %r", type(raw))
        return []
    out: List[str] = []
    for v in raw:
        s = str(v).strip()
        if s:
            out.append(s)
    return out


def load_local_tradeblock(path: Path) -> List[str]:
    """Read a local JSON file in the same shape Dave's endpoint returns. Used
    by --tradeblock-input for offline reruns or for testing scenarios."""
    payload = path.read_text(encoding="utf-8")
    return _parse_tradeblock_payload(payload)


# -----------------------------------------------------------------------------
# Candidate evaluation — for each tradeblock pid, find their eval row and
# build the same record trade_block builds for org players. Scoping the
# candidate pool to ML+AAA only is deliberate: tradeblock players below AAA
# are essentially prospect-package speculation, and pricing them off a single
# eval composite is not reliable. If/when Dave adds an "include_prospects"
# flag we can broaden this.
# -----------------------------------------------------------------------------

def build_candidate_records(
    tradeblock_pids: List[str],
    own_org: str,
    eval_rows: List[Dict[str, str]],
    cfg: Dict[str, Any],
    hitter_stats: Dict[str, Dict[str, Any]],
    pitcher_stats: Dict[str, Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Return one record per tradeable candidate (ML+AAA, not on own org).

    Each record is augmented with:
        _level         — League_Level from eval (post-/players override)
        _current_org   — Org from eval (post-override) — who currently holds them
        _current_team  — Team from eval
        _status_flags  — DL / DFA / Secondary annotations
    """
    pid_set = set(tradeblock_pids)
    if not pid_set:
        return []

    floors = cfg.get("stat_floors", {})
    # League-wide z-score reference. We deliberately compute means/stds across
    # ALL eval rows that have stats, not just the tradeblock pool, so the
    # composite of an available player is comparable to the composites of
    # the user's own ML/AAA roster (which is the relevant baseline for
    # "is this an upgrade").
    h_means, h_stds = (
        dc.compute_means_stds(hitter_stats, dc.HITTER_COMPONENTS, "overall")
        if hitter_stats else ({}, {})
    )
    h_means_l, h_stds_l = (
        dc.compute_means_stds(hitter_stats, dc.HITTER_COMPONENTS, "vs_l")
        if hitter_stats else ({}, {})
    )
    h_means_r, h_stds_r = (
        dc.compute_means_stds(hitter_stats, dc.HITTER_COMPONENTS, "vs_r")
        if hitter_stats else ({}, {})
    )
    p_means, p_stds = (
        dc.compute_means_stds(pitcher_stats, dc.PITCHER_COMPONENTS, "overall")
        if pitcher_stats else ({}, {})
    )

    own_l = own_org.strip().lower()
    candidates: List[Dict[str, Any]] = []
    seen_pids: set = set()
    missing_in_eval = 0

    for row in eval_rows:
        pid = (row.get("ID") or "").strip()
        if not pid or pid not in pid_set:
            continue
        if pid in seen_pids:
            # Eval can occasionally have duplicates from CSV-patch overrides;
            # the override layer keeps the last write, so do the same here.
            continue
        seen_pids.add(pid)

        # Skip players on the user's own roster — they're "available" league-
        # wide but you can't acquire your own player. The override layer has
        # already mapped Org to its current value, so this filter is post-truth.
        if (row.get("Org") or "").strip().lower() == own_l:
            continue

        # Scope to ML+AAA — see module docstring.
        level = (row.get("League_Level") or "").strip().upper()
        if level not in BLOCKING_LEVELS:
            continue

        # The level_cfg drives stat weight + position min logic inside
        # build_player_record. ML cfg is the right reference for trade-pool
        # players (we want their stats weighted as ML caliber for comparison).
        # If --levels narrowed the cfg subset, fall back to whichever blocking
        # level cfg exists.
        level_cfg = (
            cfg["levels"].get(level)
            or cfg["levels"].get("ML")
            or cfg["levels"].get("AAA")
            or next(iter(cfg["levels"].values()), {})
        )
        rec = dc.build_player_record(
            row, pitcher_stats, hitter_stats, level_cfg, floors,
            p_means, p_stds, h_means, h_stds,
            h_means_l, h_stds_l, h_means_r, h_stds_r,
        )
        rec["_level"] = level
        rec["_current_org"] = (row.get("Org") or "").strip()
        rec["_current_team"] = (row.get("Team") or "").strip()
        rec["_status_flags"] = (row.get("_Status_Flags") or "").strip()
        candidates.append(rec)

    # Surface the gap between what the API thinks is tradeable and what we
    # can actually evaluate — typically retired/inactive/very-new pids.
    missing_in_eval = len(pid_set - seen_pids)
    if missing_in_eval:
        logger.info(
            "%d tradeblock pid(s) had no usable ML/AAA eval row "
            "(retired/inactive/below AAA/missing); skipping.",
            missing_in_eval,
        )

    return candidates


# -----------------------------------------------------------------------------
# Need matching — for each candidate, find the best org-need bucket they fit
# and score them by composite × need-tier weight × age curve.
# -----------------------------------------------------------------------------

def _build_need_lookup(
    targets: List[Dict[str, Any]],
) -> Dict[str, Dict[str, Any]]:
    """Index build_acquisition_targets output by position/role label so we
    can look up the need-tier for a candidate's positions quickly."""
    return {t["pos"]: t for t in targets}


def _pitcher_role_bucket(p: Dict[str, Any], all_org_pitchers: List[Dict[str, Any]]) -> str:
    """Which trade_block pitcher-need bucket does this candidate map to?

    Use the candidate's composite vs the org's existing ML+AAA staff to pick:
    a 60-composite SP slots into SP1/2 (top of rotation); a 48-composite SP
    is SP3-5 material. RP split between late-inning (high-leverage) and
    middle relief by composite quartile.
    """
    role = (p.get("proj_role") or "RP").upper()
    comp = float(p.get("composite") or 0)
    own_pool = [
        x for x in all_org_pitchers
        if (x.get("proj_role") or "RP").upper() == role
        and x.get("_level") in BLOCKING_LEVELS
    ]
    own_pool.sort(key=lambda x: -x.get("composite", 0))

    if role == "SP":
        # If candidate would be a top-2 SP in your org, they're SP1/2 fit;
        # else SP3-5. Empty staff -> default to SP1/2 (you need everything).
        bar = own_pool[1].get("composite", 0) if len(own_pool) >= 2 else 0
        return "SP1/2" if comp >= bar else "SP3-5"
    # RP — top 3 = late-inning, rest = middle.
    bar = own_pool[2].get("composite", 0) if len(own_pool) >= 3 else 0
    return "CL/SU" if comp >= bar else "MR/LR"


def match_candidate_to_need(
    cand: Dict[str, Any],
    need_lookup: Dict[str, Dict[str, Any]],
    all_org_pitchers: List[Dict[str, Any]],
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Return (best_need_entry, position_label) for this candidate.

    For hitters: walk every viable position (pos_scores > 0) and pick the one
    where the org's need is most severe (Critical > Major > Depth > Set).
    Ties broken by the candidate's position score there — we'd rather slot
    them in their strongest defensive position when the need tiers match.

    For pitchers: derive role bucket from composite vs own staff (see
    _pitcher_role_bucket) and look that up directly.

    Returns (None, None) when no viable position has any kind of need entry
    (shouldn't happen — build_acquisition_targets returns one entry per pos).
    """
    if cand.get("is_pitcher"):
        bucket = _pitcher_role_bucket(cand, all_org_pitchers)
        entry = need_lookup.get(bucket)
        return (entry, bucket) if entry else (None, None)

    pos_scores = cand.get("pos_scores") or {}
    viable = [p for p in HITTER_POSITIONS if pos_scores.get(p, 0) > 0]
    if not viable:
        primary = (cand.get("primary_pos") or "").upper()
        viable = [primary] if primary in HITTER_POSITIONS else []
    if not viable:
        return None, None

    tier_rank = {"Critical": 0, "Major": 1, "Depth": 2, "Set": 3}
    best_entry: Optional[Dict[str, Any]] = None
    best_pos: Optional[str] = None
    best_key: Tuple[int, float] = (99, 0.0)
    for pos in viable:
        entry = need_lookup.get(pos)
        if not entry:
            continue
        rank = tier_rank.get(entry["tier"], 9)
        # Negate pos_score so higher scores break ties toward "better fit"
        # when need-tier is equal across positions.
        key = (rank, -pos_scores.get(pos, 0))
        if key < best_key:
            best_key = key
            best_entry = entry
            best_pos = pos
    return best_entry, best_pos


def compute_fit_score(
    cand: Dict[str, Any],
    need_entry: Optional[Dict[str, Any]],
) -> float:
    """Single-number rank used for sorting the shopping list.

    Formula:
        composite_score × need_weight + age_bonus

    where need_weight follows NEED_TIER_WEIGHT. A 55-composite player who
    fills a Critical hole scores 165 + age; a 55-composite player who fills
    a Set position scores just the age adjustment (i.e. ~zero — flagged as
    interesting only if --include-no-need is set).
    """
    comp = float(cand.get("composite") or 0)
    vos_pot = float(cand.get("vos_potential") or 0)
    age = tb.parse_age(cand) or 28.0
    headline = max(comp, 0.5 * (comp + vos_pot))  # reward upside

    need_w = NEED_TIER_WEIGHT.get(need_entry["tier"] if need_entry else "Set", 0.0)

    # Mirror trade_block's age curve so an outbound chip and an inbound
    # target of the same age sort consistently across the two reports.
    if age <= 22:
        age_adj = 3.0
    elif age <= 25:
        age_adj = 1.5
    elif age <= 29:
        age_adj = 0.0
    elif age <= 32:
        age_adj = -2.0
    else:
        age_adj = -4.0

    return headline * need_w + age_adj


def categorize_target(
    cand: Dict[str, Any],
    need_entry: Optional[Dict[str, Any]],
) -> str:
    """Bucket each candidate for the report sections."""
    tier = need_entry["tier"] if need_entry else "Set"
    comp = float(cand.get("composite") or 0)
    vos_pot = float(cand.get("vos_potential") or 0)
    age = tb.parse_age(cand) or 28.0

    if tier in ("Critical", "Major") and comp >= PREMIUM_COMPOSITE:
        return "Priority Target"
    if tier in ("Critical", "Major"):
        return "Need Fit"
    if tier == "Depth":
        return "Depth Add"
    # No real org need — only interesting if they're elite or a lottery flier.
    if comp >= PREMIUM_COMPOSITE:
        return "Premium (no need)"
    if vos_pot >= LOTTERY_VOS_POT and age <= LOTTERY_AGE_CEILING:
        return "Lottery"
    return "Pass"


# -----------------------------------------------------------------------------
# Core orchestration — build org needs, evaluate the tradeblock pool, and
# match / score / categorize / filter each candidate. Pure (no I/O): the caller
# supplies the eval rows (already /players-overridden), the tradeblock pids, and
# any stats. Shared by main() (CLI) and the web UI page so the two stay in
# lockstep — mirrors free_agent_market.compute_fa_fit's role for Free Agents.
# -----------------------------------------------------------------------------

def build_trade_targets(
    args: argparse.Namespace,
    cfg: Dict[str, Any],
    eval_rows: List[Dict[str, str]],
    tradeblock_pids: List[str],
    levels_to_run: List[str],
    target_year: int,
    hitter_stats: Dict[str, Dict[str, Any]],
    pitcher_stats: Dict[str, Dict[str, Any]],
) -> Dict[str, Any]:
    """Grade org needs, evaluate the tradeblock pool, and return scored targets.

    Reads ``args.org`` (whose roster to grade needs for), ``args.min_composite``
    (candidate floor), and ``args.include_no_need`` (keep 'Premium (no need)' /
    'Pass' candidates). ``hitter_stats`` / ``pitcher_stats`` may be empty for a
    ratings-only run (composite then comes from VOS alone).

    Returns::

        {
            "targets": [...],           # one acquisition-need entry per pos/role
            "scored": [...],            # filtered, scored candidate records,
                                        #   each carrying _need_entry / _fit_pos /
                                        #   _fit_score / _category
            "all_org_hitters": [...],   # full org pools (callers may want them
            "all_org_pitchers": [...],  #   for context / an empty-roster check)
        }

    Empty ``scored`` (not an error) when the tradeblock or the org pool is empty;
    callers decide how to surface that. No files are written and no network is
    touched here — all fetching happens in the caller.
    """
    all_org_hitters: List[Dict[str, Any]] = []
    all_org_pitchers: List[Dict[str, Any]] = []
    for level in levels_to_run:
        h, p = tb.analyze_level(
            level, args, cfg, eval_rows, target_year, hitter_stats, pitcher_stats,
        )
        all_org_hitters.extend(h)
        all_org_pitchers.extend(p)

    targets = tb.build_acquisition_targets(all_org_hitters, all_org_pitchers)
    need_lookup = _build_need_lookup(targets)

    candidates = build_candidate_records(
        tradeblock_pids, args.org, eval_rows, cfg, hitter_stats, pitcher_stats,
    )

    scored: List[Dict[str, Any]] = []
    for cand in candidates:
        need_entry, fit_pos = match_candidate_to_need(cand, need_lookup, all_org_pitchers)
        cand["_need_entry"] = need_entry
        cand["_fit_pos"] = fit_pos
        cand["_fit_score"] = compute_fit_score(cand, need_entry)
        cand["_category"] = categorize_target(cand, need_entry)

        # Floors. Drop anything below the composite floor unless it's a lottery
        # ticket or the user asked for the full list via --include-no-need.
        comp = float(cand.get("composite") or 0)
        vos_pot = float(cand.get("vos_potential") or 0)
        if comp < args.min_composite and vos_pot < LOTTERY_VOS_POT:
            continue
        # Filter out "no need" by default — the shopping list is more useful
        # when scoped to actual gaps.
        if cand["_category"] in ("Premium (no need)", "Pass") and not args.include_no_need:
            continue
        scored.append(cand)

    return {
        "targets": targets,
        "scored": scored,
        "all_org_hitters": all_org_hitters,
        "all_org_pitchers": all_org_pitchers,
    }


# -----------------------------------------------------------------------------
# Report rendering — mirrors trade_block.py's md/csv pair so the two outputs
# read consistently when laid side-by-side.
# -----------------------------------------------------------------------------

def fmt(x: Any, digits: int = 1) -> str:
    try:
        return f"{float(x):.{digits}f}"
    except (TypeError, ValueError):
        return str(x) if x not in (None, "") else "—"


def render_md(
    league: str,
    org: str,
    year: int,
    candidates: List[Dict[str, Any]],
    targets: List[Dict[str, Any]],
    tradeblock_size: int,
) -> str:
    out: List[str] = []
    out.append(f"# Trade Targets — {org}  ·  {league.upper()}  ·  {year}")
    out.append("")
    out.append(
        "_Players from the league-wide /tradeblock, scored against your org's "
        "Acquisition Targets. Fit Score blends candidate composite, your need-"
        "tier at their position, and an age curve — higher is a better fit._"
    )
    out.append("")
    out.append(
        f"_Generated {datetime.now().strftime('%Y-%m-%d %H:%M')}. "
        f"{tradeblock_size} pids on the block · {len(candidates)} evaluated as ML/AAA-tradeable._"
    )
    out.append("")

    by_cat: Dict[str, List[Dict[str, Any]]] = {}
    for c in candidates:
        by_cat.setdefault(c["_category"], []).append(c)

    out.append("## Summary")
    out.append("")
    out.append("| Bucket | Count | Top Candidate |")
    out.append("| --- | --- | --- |")
    section_order = ("Priority Target", "Need Fit", "Depth Add", "Premium (no need)", "Lottery", "Pass")
    for label in section_order:
        rows = by_cat.get(label) or []
        if not rows:
            out.append(f"| {label} | 0 | — |")
            continue
        rows.sort(key=lambda p: -p["_fit_score"])
        top = rows[0]
        out.append(
            f"| {label} | {len(rows)} | "
            f"{top['name']} ({top.get('_current_org','?')}, fit {top['_fit_score']:.1f}) |"
        )
    out.append("")

    # Org needs snapshot — same shape trade_block.py renders, so the two
    # reports can be compared without flipping between files.
    tier_order = {"Critical": 0, "Major": 1, "Depth": 2, "Set": 3}
    ordered_targets = sorted(targets, key=lambda t: (tier_order.get(t["tier"], 9), t["pos"]))
    out.append("## Your Needs (for cross-reference)")
    out.append("")
    out.append("| Tier | Pos/Role | Current State | Archetype | Reasoning |")
    out.append("| --- | --- | --- | --- | --- |")
    for t in ordered_targets:
        out.append(
            f"| **{t['tier']}** | {t['pos']} | {t['summary']} | "
            f"{t['archetype']} | {t['reasoning']} |"
        )
    out.append("")

    def _table(rows: List[Dict[str, Any]], title: str, blurb: str) -> None:
        if not rows:
            return
        out.append(f"## {title}")
        out.append("")
        out.append(f"_{blurb}_")
        out.append("")
        out.append(
            "| Name | Current Org | Lvl | Age | Pos/Role | Fit Need | "
            "Need Tier | Career | Reach | Comp | Fit | Flags |"
        )
        out.append(
            "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"
        )
        rows = sorted(rows, key=lambda p: -p["_fit_score"])
        for p in rows:
            pos_label = p.get("primary_pos", "") or p.get("proj_role", "")
            fit_pos = p.get("_fit_pos") or "—"
            need_tier = (p.get("_need_entry") or {}).get("tier", "—")
            flags = p.get("_status_flags") or "—"
            out.append(
                f"| {p['name']} | {p.get('_current_org','')} | "
                f"{p.get('_level','')} | {fmt(p.get('age'), 0)} | "
                f"{pos_label} | {fit_pos} | {need_tier} | "
                f"{fmt(p.get('vos'))} | {fmt(p.get('vos_potential'))} | "
                f"{fmt(p.get('composite'))} | **{p['_fit_score']:.1f}** | {flags} |"
            )
        out.append("")

    _table(
        by_cat.get("Priority Target") or [],
        "Priority Targets",
        "Premium-composite players who fill Critical or Major holes. "
        "These are who you call about first.",
    )
    _table(
        by_cat.get("Need Fit") or [],
        "Need Fits",
        "Solid pieces that address a Critical or Major hole. May not be "
        "stars, but they move the needle.",
    )
    _table(
        by_cat.get("Depth Add") or [],
        "Depth Adds",
        "Players who fit a Depth-tier need — useful as cost-controlled "
        "insurance behind a current starter.",
    )
    _table(
        by_cat.get("Premium (no need)") or [],
        "Premium (no current need)",
        "High-composite players available, but your org grades the position "
        "as Set. Worth tracking in case the market value lets you flip them.",
    )
    _table(
        by_cat.get("Lottery") or [],
        "Lottery Tickets",
        "Young, available, high VOS_Pot. Cheap fliers; pair with cash or "
        "secondary pieces.",
    )
    _table(
        by_cat.get("Pass") or [],
        "Pass",
        "Below the composite floor and not filling a need — listed for "
        "completeness only.",
    )

    return "\n".join(out)


def write_csv(path: Path, candidates: List[Dict[str, Any]]) -> None:
    fields = [
        "pid", "name", "age", "current_org", "current_team", "level",
        "primary_pos", "proj_role",
        "career", "reach", "composite",
        "fit_pos", "need_tier", "need_archetype",
        "fit_score", "category", "status_flags",
    ]
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for c in sorted(candidates, key=lambda p: -p["_fit_score"]):
            entry = c.get("_need_entry") or {}
            writer.writerow({
                "pid": c.get("pid", ""),
                "name": c.get("name", ""),
                "age": c.get("age", ""),
                "current_org": c.get("_current_org", ""),
                "current_team": c.get("_current_team", ""),
                "level": c.get("_level", ""),
                "primary_pos": c.get("primary_pos", ""),
                "proj_role": c.get("proj_role", ""),
                "career": f"{float(c.get('vos') or 0):.2f}",
                "reach":  f"{float(c.get('vos_potential') or 0):.2f}",
                "composite": f"{float(c.get('composite') or 0):.2f}",
                "fit_pos": c.get("_fit_pos", ""),
                "need_tier": entry.get("tier", ""),
                "need_archetype": entry.get("archetype", ""),
                "fit_score": f"{c['_fit_score']:.2f}",
                "category": c.get("_category", ""),
                "status_flags": c.get("_status_flags", ""),
            })


# -----------------------------------------------------------------------------
# main()
# -----------------------------------------------------------------------------

def main() -> int:
    args = parse_args()
    logging.basicConfig(level=getattr(logging, args.log_level), format="%(levelname)s: %(message)s")

    cfg = dc.load_config(args.config)

    # Auto-resolve --org / --year from league_settings.json when not on CLI.
    # Keeps the bulk-runner ergonomics consistent with run_vos_all /
    # run_depth_chart_all, which read the same file.
    resolved_org, resolved_year = resolve_org_year(
        args.league, args.org, args.year, args.league_settings,
    )
    args.org = resolved_org
    args.year = resolved_year
    if not args.org:
        logger.error(
            "No --org provided and no 'org' entry for league %r in %s. "
            "Either pass --org explicitly or add the league to league_settings.json.",
            args.league, args.league_settings,
        )
        return 2
    target_year = args.year or datetime.now().year

    if args.levels:
        levels_to_run = [lvl.strip().upper() for lvl in args.levels.split(",") if lvl.strip()]
        for lvl in levels_to_run:
            if lvl not in cfg["levels"]:
                logger.error("Level '%s' not in depth_config.json", lvl)
                return 2
    else:
        levels_to_run = list(cfg["levels"].keys())

    out_dir = args.output_dir or (SCRIPT_DIR / args.league / "trade_targets")
    out_dir.mkdir(parents=True, exist_ok=True)
    if not args.no_archive:
        moved, archive_dir = dc.archive_previous_runs(out_dir)
        if moved:
            logger.info("Archived %d prior trade_targets file(s) to %s", moved, archive_dir)

    # --- Load eval + apply /players override (mirrors trade_block.main) -----
    try:
        eval_path = dc.find_latest_eval(args.league, args.input, args.org_code)
    except FileNotFoundError as e:
        logger.error("%s", e)
        return 2
    logger.info("Using eval file: %s", eval_path)
    eval_rows = dc.read_eval(eval_path)

    base_url = sapi.resolve_base_url(args.league, args.base_url, args.league_url_config)
    cache_dir = None
    if not args.no_cache:
        cache_dir = args.cache_dir or (SCRIPT_DIR / args.league / "cache" / "stats")

    players_lookup: Dict[str, Dict[str, str]] = {}
    level_id_to_label: Dict[int, str] = {}
    team_id_to_name: Dict[int, str] = {}
    if not args.no_players_override and not args.no_stats and base_url:
        players_lookup = sapi.build_players_lookup(base_url, cache_dir=cache_dir)
        if players_lookup:
            level_id_to_label = dc.load_level_id_to_label()
            team_id_to_name = dc.load_team_id_to_name(args.league)
            logger.info("Loaded /players (%d) — overriding eval Level/Org/Team.",
                        len(players_lookup))

    if args.players_override_csv:
        if not level_id_to_label:
            level_id_to_label = dc.load_level_id_to_label()
        if not team_id_to_name:
            team_id_to_name = dc.load_team_id_to_name(args.league)
        team_name_to_id = dc.invert_team_id_to_name(team_id_to_name)
        csv_patch = dc.build_players_lookup_from_csv(args.players_override_csv, team_name_to_id)
        if csv_patch:
            collisions = sum(1 for pid in csv_patch if pid in players_lookup)
            players_lookup.update(csv_patch)
            logger.info("Applied roster CSV patch: %d rows | %d overrode /players entries.",
                        len(csv_patch), collisions)

    if players_lookup:
        counts = dc.apply_players_override(
            eval_rows, players_lookup, level_id_to_label, team_id_to_name,
            include_inactive=args.include_inactive,
        )
        logger.info(
            "Players override: %d eval rows | %d level overrides | %d org overrides | "
            "filtered: %d retired, %d DFA, %d waivers, %d DL60",
            counts["total"], counts["level_overrides"], counts["org_overrides"],
            counts["filtered_retired"], counts["filtered_dfa"],
            counts["filtered_waivers"], counts["filtered_dl60"],
        )

    # --- Fetch /tradeblock --------------------------------------------------
    if args.tradeblock_input:
        tradeblock_pids = load_local_tradeblock(args.tradeblock_input)
        logger.info("Loaded %d pid(s) from local --tradeblock-input.", len(tradeblock_pids))
    elif base_url:
        cookie = resolve_cookie(args.cookie, args.cookie_file)
        # Prefer the configured API token when no explicit cookie was given —
        # most runs then need no auth flags at all. An explicit --cookie still
        # wins (the user asked for it).
        token = None if cookie else resolve_token(args.league)
        if not cookie and not token:
            logger.info(
                "No --cookie / --cookie-file and no token in "
                "config/statsplus_tokens.json for league %r. /tradeblock requires "
                "an authenticated session; the request will likely fail unless "
                "Dave whitelisted your league.",
                args.league,
            )
        tradeblock_pids = fetch_tradeblock(
            base_url, cache_dir=cache_dir, cookie=cookie, token=token,
        )
    else:
        logger.error("No base URL for league '%s' and no --tradeblock-input — nothing to evaluate.",
                     args.league)
        return 2

    if not tradeblock_pids:
        logger.error("Tradeblock returned no pids; aborting. "
                     "Try --tradeblock-input with a local snapshot, or check the API.")
        return 2
    logger.info("Tradeblock: %d pid(s) available league-wide.", len(tradeblock_pids))

    # --- Stats pipeline -----------------------------------------------------
    hitter_stats: Dict[str, Dict[str, Any]] = {}
    pitcher_stats: Dict[str, Dict[str, Any]] = {}
    if not args.no_stats:
        if not base_url:
            logger.error("No base URL for league '%s'", args.league)
            return 2
        league_ids_map = dc.load_league_ids(args.league_ids_config)
        all_lids: List[int] = []
        seen: set = set()
        for level_ids in league_ids_map.get(args.league.lower(), {}).values():
            for lid in level_ids:
                if lid not in seen:
                    seen.add(lid)
                    all_lids.append(lid)
        logger.info("Fetching stats for %d lids", len(all_lids))
        hitter_stats, pitcher_stats, _, _ = sapi.build_player_stats(
            base_url, target_year,
            cfg.get("year_weights", [0.55, 0.35, 0.10]),
            cfg.get("woba_weights", {}),
            lids=all_lids or None,
            target_lids=None,
            cache_dir=cache_dir,
        )

    # --- Build needs + evaluate tradeblock candidates -----------------------
    # All the analysis lives in build_trade_targets so the web UI page can run
    # the exact same pipeline. analyze_level (via the core) is the same code
    # trade_block.main uses, so the needs output matches a fresh trade_block run.
    result = build_trade_targets(
        args, cfg, eval_rows, tradeblock_pids, levels_to_run,
        target_year, hitter_stats, pitcher_stats,
    )
    if not result["all_org_hitters"] and not result["all_org_pitchers"]:
        logger.error("No players found for org '%s' at levels %s — cannot grade needs.",
                     args.org, levels_to_run)
        return 2
    targets = result["targets"]
    scored = result["scored"]

    logger.info(
        "Targets: %d candidates after filtering (%d hitters, %d pitchers).",
        len(scored),
        sum(1 for c in scored if not c.get("is_pitcher")),
        sum(1 for c in scored if c.get("is_pitcher")),
    )

    # --- Write outputs ------------------------------------------------------
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    org_slug = args.org.lower().replace(" ", "_")
    md_path = out_dir / f"{org_slug}_trade_targets_{ts}.md"
    csv_path = out_dir / f"{org_slug}_trade_targets_{ts}.csv"

    md = render_md(args.league, args.org, target_year, scored, targets, len(tradeblock_pids))
    md_path.write_text(md, encoding="utf-8")
    logger.info("Wrote %s", md_path)

    write_csv(csv_path, scored)
    logger.info("Wrote %s", csv_path)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
