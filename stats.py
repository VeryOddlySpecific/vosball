#!/usr/bin/env python3
"""
stats.py — Fetch and aggregate StatsPlus v2 player stat endpoints.

Pulls /playerbatstatsv2, /playerpitchstatsv2, /playerfieldstatsv2 for a window
of seasons (default current + 2 prior), year-weights the counting stats, and
computes derived stats (wOBA, FIP, K%, BB%, etc.) along with league constants
needed for those derivations (league wOBA, cFIP).

Splits: returns three views per hitter (overall, vs_l, vs_r). Pitcher splits
are also fetched but consumers typically only need overall. Fielding has no
splits.

Used by depth_chart.py.
"""

from __future__ import annotations

import csv
import logging
from datetime import date, datetime
from io import StringIO
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.error import URLError
from urllib.parse import parse_qs, urlparse
from urllib.request import urlopen

import json

logger = logging.getLogger(__name__)
SCRIPT_DIR = Path(__file__).resolve().parent

# Split codes per the StatsPlus wiki.
SPLIT_OVERALL = 1
SPLIT_VS_L = 2
SPLIT_VS_R = 3
SPLIT_PLAYOFF = 21


# -----------------------------------------------------------------------------
# League URL resolution (mirrors farm_value_old.resolve_base_url)
# -----------------------------------------------------------------------------

def load_league_urls(config_path: Optional[Path] = None) -> Dict[str, str]:
    path = config_path or (SCRIPT_DIR / "config" / "league_url.json")
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as f:
        raw = json.load(f)
    if not isinstance(raw, dict):
        return {}
    return {str(k).strip().lower(): str(v).strip().rstrip("/") for k, v in raw.items() if k and v}


def resolve_base_url(league: str, override: Optional[str] = None, config_path: Optional[Path] = None) -> Optional[str]:
    if override:
        return override.rstrip("/")
    return load_league_urls(config_path).get((league or "").strip().lower())


# -----------------------------------------------------------------------------
# Endpoint fetch + simple disk cache (calendar-day TTL)
# -----------------------------------------------------------------------------

# Optional tqdm progress bar — graceful no-op when not installed.
try:
    from tqdm import tqdm as _tqdm  # type: ignore
except ImportError:  # pragma: no cover
    def _tqdm(iterable=None, **_kwargs):  # type: ignore
        return iterable if iterable is not None else iter([])


def _cache_marker_path(cache_dir: Path) -> Path:
    """Sentinel file dropped after a fully-successful cache-warmed run.

    When this file's date matches today, the rest of the cache is assumed
    fresh — every per-URL freshness check + per-URL log line in
    ``_fetch_csv`` gets bypassed. Re-fetched on any cache miss so a partial
    cache never gets falsely promoted to "complete".
    """
    return cache_dir / "_cache_complete.marker"


def _cache_marker_fresh(cache_dir: Optional[Path]) -> bool:
    if not cache_dir:
        return False
    path = _cache_marker_path(cache_dir)
    if not path.exists():
        return False
    try:
        mtime_date = datetime.fromtimestamp(path.stat().st_mtime).date()
    except OSError:
        return False
    return mtime_date == date.today()


def _write_cache_marker(cache_dir: Optional[Path]) -> None:
    if not cache_dir:
        return
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        _cache_marker_path(cache_dir).write_text(date.today().isoformat(), encoding="utf-8")
    except OSError as e:
        logger.warning("Failed to write cache marker: %s", e)


def _cache_path_for_url(url: str, cache_dir: Path) -> Path:
    """Build a stable, human-readable cache filename from the URL.

    Uses the endpoint name + sorted query params so the same logical request
    always lands on the same file regardless of param ordering.
    """
    parsed = urlparse(url)
    path_parts = [p for p in parsed.path.strip("/").split("/") if p]
    endpoint = path_parts[-1] if path_parts else "endpoint"
    qs = parse_qs(parsed.query)
    qs_pairs = [f"{k}-{v[0]}" for k, v in sorted(qs.items()) if v]
    qs_str = "_".join(qs_pairs)
    fname = f"{endpoint}__{qs_str}.csv" if qs_str else f"{endpoint}.csv"
    return cache_dir / fname


def _is_cache_fresh(path: Path) -> bool:
    """Cache is fresh if its mtime falls on today's calendar date (1-day TTL)."""
    if not path.exists():
        return False
    try:
        mtime_date = datetime.fromtimestamp(path.stat().st_mtime).date()
    except OSError:
        return False
    return mtime_date == date.today()


def _parse_csv_payload(payload: str) -> List[Dict[str, str]]:
    """Parse a stat-endpoint CSV. Empty / no-header responses are valid "no
    data for this slice" signals (e.g., older years that don't store vs-L /
    vs-R splits) and resolve to an empty list. Caching an empty file is
    intentional — avoids re-hitting the API for a known-empty slice every
    run. Truly malformed CSVs with content but no header still resolve to
    [] here; downstream aggregators tolerate empties.
    """
    reader = csv.DictReader(StringIO(payload))
    if not reader.fieldnames:
        return []
    return [r for r in reader if isinstance(r, dict) and r.get("player_id")]


def _fetch_csv(
    url: str,
    cache_dir: Optional[Path] = None,
    fast_path: bool = False,
) -> List[Dict[str, str]]:
    """Fetch a v2 stats endpoint and parse as CSV. Header-keyed; tolerant of empty rows.

    If ``cache_dir`` is provided, responses cache to disk and are reused for the
    rest of the calendar day. Cache misses fetch from the API and write through.
    When ``fast_path`` is True (cache marker already validated), skips the
    per-file mtime check and per-URL log line — just reads the file if present.
    """
    if cache_dir:
        cache_path = _cache_path_for_url(url, cache_dir)
        cache_ok = cache_path.exists() if fast_path else _is_cache_fresh(cache_path)
        if cache_ok:
            try:
                payload = cache_path.read_text(encoding="utf-8")
                rows = _parse_csv_payload(payload)
                if not fast_path:
                    logger.info("Cache hit  %s", cache_path.name)
                return rows
            except (OSError, ValueError) as e:
                logger.warning("Cache read failed (%s); refetching", e)

    if not fast_path:
        logger.info("Fetching %s", url)
    with urlopen(url, timeout=60) as resp:
        payload = resp.read().decode("utf-8-sig", errors="replace")
    rows = _parse_csv_payload(payload)

    if cache_dir:
        # Negative-caching guard: a completely blank / headerless body is almost
        # always a transient hiccup (HTTP 200 with an empty body during a sim,
        # etc.), NOT a real "no data for this slice" signal — which still returns
        # a CSV header. Caching a blank body would serve [] for the rest of the
        # calendar day (silently dropping wOBA/FIP). Header-present responses are
        # still cached even with zero data rows, preserving the intentional
        # empty-slice optimization in _parse_csv_payload.
        if payload.strip():
            try:
                cache_path = _cache_path_for_url(url, cache_dir)
                cache_path.parent.mkdir(parents=True, exist_ok=True)
                cache_path.write_text(payload, encoding="utf-8")
            except OSError as e:
                logger.warning("Cache write failed for %s: %s", url, e)
        elif not fast_path:
            logger.warning("Blank response for %s — not caching (likely transient).", url)
    return rows


def _build_url(
    base_url: str,
    endpoint: str,
    year: Optional[int],
    split: Optional[int],
    lid: Optional[int] = None,
) -> str:
    base = base_url.rstrip("/")
    qs: List[str] = []
    if year is not None:
        qs.append(f"year={int(year)}")
    if split is not None:
        qs.append(f"split={int(split)}")
    if lid is not None:
        qs.append(f"lid={int(lid)}")
    suffix = ("?" + "&".join(qs)) if qs else ""
    return f"{base}/{endpoint}/{suffix}"


def fetch_year(
    base_url: str,
    endpoint: str,
    year: int,
    split: Optional[int] = None,
    lids: Optional[List[int]] = None,
    cache_dir: Optional[Path] = None,
    fast_path: bool = False,
    errors: Optional[List[str]] = None,
) -> List[Dict[str, str]]:
    """Fetch one year's worth of rows for one endpoint/split.

    If ``lids`` is provided, hits the endpoint once per lid and concatenates.
    Without ``lids``, calls without an lid query param (the API then defaults
    to top-level leagues — i.e. ML only). When ``cache_dir`` is set, each
    distinct URL is cached on disk for the rest of the calendar day. When
    ``fast_path`` is True, skips per-URL freshness checks and per-URL logs.

    If ``errors`` (a list) is passed, the URL of any failed fetch is appended to
    it. Callers use this to avoid promoting a partial cache to "complete" (see
    the cache-marker logic in ``_build_pool_aggregates``).
    """
    if not lids:
        url = _build_url(base_url, endpoint, year, split)
        try:
            return _fetch_csv(url, cache_dir=cache_dir, fast_path=fast_path)
        except (URLError, TimeoutError, ValueError) as e:
            logger.warning("Stat fetch failed (%s): %s", url, e)
            if errors is not None:
                errors.append(url)
            return []

    rows: List[Dict[str, str]] = []
    for lid in lids:
        url = _build_url(base_url, endpoint, year, split, lid)
        try:
            rows.extend(_fetch_csv(url, cache_dir=cache_dir, fast_path=fast_path))
        except (URLError, TimeoutError, ValueError) as e:
            logger.warning("Stat fetch failed (%s): %s", url, e)
            if errors is not None:
                errors.append(url)
    return rows


# -----------------------------------------------------------------------------
# /players endpoint — current per-player metadata (team, level, status flags)
# -----------------------------------------------------------------------------

def _normalize_players_key(key: str) -> str:
    """Mirror the convention used elsewhere (farm_value_old, contract).

    'First Name' -> 'first_name'; 'Organization ID' -> 'organization_id'.
    """
    return (key or "").strip().lower().replace(" ", "_")


def _parse_players_csv(payload: str) -> List[Dict[str, str]]:
    """Parse /players CSV. Unlike the stat endpoints, the player-id column is
    'ID' (capitalized) — we keep every row that has a non-empty ID after
    normalization.
    """
    reader = csv.DictReader(StringIO(payload))
    if not reader.fieldnames:
        return []
    out: List[Dict[str, str]] = []
    for row in reader:
        if not isinstance(row, dict):
            continue
        normalized = {_normalize_players_key(k): (v or "") for k, v in row.items()}
        if (normalized.get("id") or "").strip():
            out.append(normalized)
    return out


def fetch_players(
    base_url: str,
    cache_dir: Optional[Path] = None,
) -> List[Dict[str, str]]:
    """Fetch /players (one row per player) with the same calendar-day disk
    cache used for stat endpoints. Returned rows have lowercase, underscore-
    normalized keys (e.g., 'organization_id', 'is_on_dl60').

    Returns [] on network errors so depth_chart can fall back to the eval CSV.
    """
    base = base_url.rstrip("/")
    url = f"{base}/players/"

    if cache_dir:
        cache_path = _cache_path_for_url(url, cache_dir)
        if _is_cache_fresh(cache_path):
            try:
                payload = cache_path.read_text(encoding="utf-8")
                logger.info("Cache hit  %s", cache_path.name)
                return _parse_players_csv(payload)
            except (OSError, ValueError) as e:
                logger.warning("Cache read failed (%s); refetching", e)

    logger.info("Fetching %s", url)
    try:
        with urlopen(url, timeout=60) as resp:
            payload = resp.read().decode("utf-8-sig", errors="replace")
    except (URLError, TimeoutError, ValueError) as e:
        logger.warning("/players fetch failed (%s): %s", url, e)
        return []

    rows = _parse_players_csv(payload)

    if cache_dir and payload.strip():
        # Same negative-caching guard as _fetch_csv: never persist a blank body
        # (a stale empty /players cache would suppress the eval reconciliation
        # for the whole day).
        try:
            cache_path = _cache_path_for_url(url, cache_dir)
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(payload, encoding="utf-8")
        except OSError as e:
            logger.warning("/players cache write failed (%s): %s", url, e)
    elif cache_dir and not payload.strip():
        logger.warning("Blank /players response — not caching (likely transient).")
    return rows


def build_players_lookup(
    base_url: str,
    cache_dir: Optional[Path] = None,
) -> Dict[str, Dict[str, str]]:
    """Convenience wrapper: returns {pid: {normalized field: value}}.

    Empty dict on fetch failure.
    """
    rows = fetch_players(base_url, cache_dir=cache_dir)
    out: Dict[str, Dict[str, str]] = {}
    for row in rows:
        pid = (row.get("id") or "").strip()
        if pid:
            out[pid] = row
    return out


# -----------------------------------------------------------------------------
# Year-weighted counting stat aggregation
# -----------------------------------------------------------------------------

def _to_float(v: Any) -> float:
    try:
        if v is None:
            return 0.0
        s = str(v).strip()
        return float(s) if s else 0.0
    except (TypeError, ValueError):
        return 0.0


def aggregate_counting(
    rows_by_year: Dict[int, List[Dict[str, str]]],
    year_weights: List[float],
    counting_cols: Iterable[str],
) -> Dict[str, Dict[str, float]]:
    """
    Sum (year-weighted) counting stats per player_id across years.

    rows_by_year keys are years; year_weights[0] applies to the most recent year.
    Returns: {player_id: {col: weighted_sum, ...}}.

    Use this for additive stats (PA, AB, H, IP, etc). For per-year ratings or
    averages (framing, arm, zr), use aggregate_ratings instead — it normalizes
    by present-year weights so a single-year sample isn't artificially
    diminished by missing prior years.
    """
    if not rows_by_year:
        return {}
    years_desc = sorted(rows_by_year.keys(), reverse=True)
    # Pad weights if the user gave fewer than years.
    weights = list(year_weights) + [0.0] * max(0, len(years_desc) - len(year_weights))

    out: Dict[str, Dict[str, float]] = {}
    cols = list(counting_cols)
    for w, year in zip(weights, years_desc):
        if w <= 0:
            continue
        for r in rows_by_year[year]:
            pid = (r.get("player_id") or "").strip()
            if not pid:
                continue
            slot = out.setdefault(pid, {c: 0.0 for c in cols})
            for c in cols:
                slot[c] += w * _to_float(r.get(c))
    return out


def aggregate_ratings(
    rows_by_year: Dict[int, List[Dict[str, str]]],
    year_weights: List[float],
    rating_cols: Iterable[str],
) -> Dict[str, Dict[str, float]]:
    """
    Year-weighted average per player_id for non-additive columns.

    Unlike aggregate_counting, this divides by the sum of weights for years
    where the player actually has a row, so a single-year sample resolves to
    that year's value rather than year_weight[0] * value.
    """
    if not rows_by_year:
        return {}
    years_desc = sorted(rows_by_year.keys(), reverse=True)
    weights = list(year_weights) + [0.0] * max(0, len(years_desc) - len(year_weights))
    cols = list(rating_cols)

    sums: Dict[str, Dict[str, float]] = {}
    weight_totals: Dict[str, float] = {}
    for w, year in zip(weights, years_desc):
        if w <= 0:
            continue
        for r in rows_by_year[year]:
            pid = (r.get("player_id") or "").strip()
            if not pid:
                continue
            slot = sums.setdefault(pid, {c: 0.0 for c in cols})
            weight_totals[pid] = weight_totals.get(pid, 0.0) + w
            for c in cols:
                slot[c] += w * _to_float(r.get(c))

    out: Dict[str, Dict[str, float]] = {}
    for pid, slot in sums.items():
        wt = weight_totals.get(pid, 0.0)
        if wt <= 0:
            out[pid] = {c: 0.0 for c in cols}
        else:
            out[pid] = {c: slot[c] / wt for c in cols}
    return out


# -----------------------------------------------------------------------------
# Hitter derived stats
# -----------------------------------------------------------------------------

HITTER_COUNTING = (
    "pa", "ab", "h", "d", "t", "hr", "k", "bb", "ibb", "hp",
    "sf", "sh", "sb", "cs", "gdp", "r", "rbi", "ubr", "wpa", "war",
)


def hitter_derived(c: Dict[str, float], woba_w: Dict[str, float]) -> Dict[str, float]:
    """Compute derived hitter stats from year-weighted counting totals."""
    pa = c.get("pa", 0.0)
    ab = c.get("ab", 0.0)
    h = c.get("h", 0.0)
    d = c.get("d", 0.0)
    t = c.get("t", 0.0)
    hr = c.get("hr", 0.0)
    bb = c.get("bb", 0.0)
    ibb = c.get("ibb", 0.0)
    hp = c.get("hp", 0.0)
    sf = c.get("sf", 0.0)
    k = c.get("k", 0.0)
    sb = c.get("sb", 0.0)
    cs = c.get("cs", 0.0)

    singles = max(0.0, h - d - t - hr)
    tb = singles + 2 * d + 3 * t + 4 * hr
    ubb = max(0.0, bb - ibb)

    avg = h / ab if ab > 0 else 0.0
    obp_den = ab + bb + hp + sf
    obp = (h + bb + hp) / obp_den if obp_den > 0 else 0.0
    slg = tb / ab if ab > 0 else 0.0
    iso = slg - avg
    babip_den = ab - k - hr + sf
    babip = (h - hr) / babip_den if babip_den > 0 else 0.0

    woba_num = (
        woba_w.get("uBB", 0.69) * ubb
        + woba_w.get("HBP", 0.72) * hp
        + woba_w.get("1B", 0.89) * singles
        + woba_w.get("2B", 1.27) * d
        + woba_w.get("3B", 1.62) * t
        + woba_w.get("HR", 2.10) * hr
    )
    woba_den = ab + ubb + sf + hp
    woba = woba_num / woba_den if woba_den > 0 else 0.0

    bb_pct = bb / pa if pa > 0 else 0.0
    k_pct = k / pa if pa > 0 else 0.0
    sb_attempts = sb + cs
    sb_pct = sb / sb_attempts if sb_attempts > 0 else 0.0

    return {
        "PA": pa, "AB": ab, "H": h, "HR": hr,
        "R": c.get("r", 0.0), "RBI": c.get("rbi", 0.0),
        "AVG": avg, "OBP": obp, "SLG": slg, "OPS": obp + slg,
        "ISO": iso, "BABIP": babip, "wOBA": woba,
        "BB%": bb_pct, "K%": k_pct,
        "SB": sb, "CS": cs, "SB%": sb_pct,
        "WAR": c.get("war", 0.0),
    }


# -----------------------------------------------------------------------------
# Pitcher derived stats
# -----------------------------------------------------------------------------

PITCHER_COUNTING = (
    "outs", "ip", "ipf", "bf", "ab", "ha", "k", "bb", "iw", "hp", "hra",
    "r", "er", "gb", "fb", "g", "gs", "qs", "cg", "sho", "w", "l",
    "s", "svo", "bs", "hld", "ir", "irs", "wpa", "war", "ra9war", "pi",
)


def _ip_from_outs_or_field(c: Dict[str, float]) -> float:
    """Prefer the 'outs' column; fall back to OOTP-style decimal IP if needed."""
    outs = c.get("outs", 0.0)
    if outs > 0:
        return outs / 3.0
    raw = c.get("ip", 0.0)
    if raw <= 0:
        return 0.0
    whole = int(raw)
    frac = raw - whole
    # OOTP encodes thirds as .1 / .2 — convert to true decimal.
    if 0.0 <= frac < 0.35:
        thirds = round(frac * 10)
        return whole + thirds / 3.0
    return raw


def league_constants(
    pit_counting: Dict[str, Dict[str, float]],
    woba_weights: Dict[str, float],
) -> Dict[str, float]:
    """Compute league cFIP so league-average FIP equals league-average ERA.

    cFIP = lgERA - lgFIP_raw, where lgFIP_raw uses (13*HR + 3*(BB+HBP) - 2*K)/IP.
    """
    total_outs = 0.0
    total_er = 0.0
    sum_hr = 0.0
    sum_bb = 0.0
    sum_hp = 0.0
    sum_k = 0.0
    for c in pit_counting.values():
        ip = _ip_from_outs_or_field(c)
        if ip <= 0:
            continue
        total_outs += ip
        total_er += c.get("er", 0.0)
        sum_hr += c.get("hra", 0.0)
        sum_bb += c.get("bb", 0.0)
        sum_hp += c.get("hp", 0.0)
        sum_k += c.get("k", 0.0)

    if total_outs <= 0:
        return {"cFIP": 0.0, "lgERA": 0.0}

    lg_era = total_er * 9.0 / total_outs
    lg_fip_raw = (13.0 * sum_hr + 3.0 * (sum_bb + sum_hp) - 2.0 * sum_k) / total_outs
    return {"cFIP": lg_era - lg_fip_raw, "lgERA": lg_era}


def pitcher_derived(c: Dict[str, float], constants: Dict[str, float]) -> Dict[str, float]:
    """Compute derived pitcher stats from year-weighted counting totals."""
    ip = _ip_from_outs_or_field(c)
    bf = c.get("bf", 0.0)
    er = c.get("er", 0.0)
    ha = c.get("ha", 0.0)
    bb = c.get("bb", 0.0)
    hp = c.get("hp", 0.0)
    hra = c.get("hra", 0.0)
    k = c.get("k", 0.0)
    gb = c.get("gb", 0.0)
    fb = c.get("fb", 0.0)
    gs = c.get("gs", 0.0)
    g = c.get("g", 0.0)

    if ip <= 0:
        # No useful rate stats but preserve identity totals.
        return {
            "IP": 0.0, "BF": bf, "G": g, "GS": gs, "ERA": 0.0, "FIP": 0.0,
            "K/9": 0.0, "BB/9": 0.0, "HR/9": 0.0, "K%": 0.0, "BB%": 0.0,
            "K-BB%": 0.0, "WHIP": 0.0, "GB%": 0.0,
            "W": c.get("w", 0.0), "L": c.get("l", 0.0),
            "SV": c.get("s", 0.0), "HLD": c.get("hld", 0.0),
            "QS": c.get("qs", 0.0), "CG": c.get("cg", 0.0), "SHO": c.get("sho", 0.0),
            "WAR": c.get("war", 0.0), "RA9WAR": c.get("ra9war", 0.0),
        }

    era = er * 9.0 / ip
    fip_raw = (13.0 * hra + 3.0 * (bb + hp) - 2.0 * k) / ip
    fip = fip_raw + constants.get("cFIP", 0.0)
    k_per_9 = k * 9.0 / ip
    bb_per_9 = bb * 9.0 / ip
    hr_per_9 = hra * 9.0 / ip
    whip = (ha + bb) / ip
    gb_pct = gb / (gb + fb) if (gb + fb) > 0 else 0.0
    k_pct = k / bf if bf > 0 else 0.0
    bb_pct = bb / bf if bf > 0 else 0.0

    return {
        "IP": ip, "BF": bf, "G": g, "GS": gs,
        "ERA": era, "FIP": fip,
        "K/9": k_per_9, "BB/9": bb_per_9, "HR/9": hr_per_9,
        "K%": k_pct, "BB%": bb_pct, "K-BB%": k_pct - bb_pct,
        "WHIP": whip, "GB%": gb_pct,
        # Counting totals (3-yr-weighted) for projection rate lookups.
        "W": c.get("w", 0.0), "L": c.get("l", 0.0),
        "SV": c.get("s", 0.0), "HLD": c.get("hld", 0.0),
        "QS": c.get("qs", 0.0), "CG": c.get("cg", 0.0), "SHO": c.get("sho", 0.0),
        "WAR": c.get("war", 0.0), "RA9WAR": c.get("ra9war", 0.0),
    }


# -----------------------------------------------------------------------------
# Fielder derived stats
# -----------------------------------------------------------------------------

# Counting columns that aggregate cleanly across years.
FIELDER_COUNTING = (
    "g", "gs", "ipf", "tc", "po", "a", "e", "dp", "tp",
    "pb", "sba", "rto", "plays", "plays_base", "roe",
)


def fielder_derived(c: Dict[str, float]) -> Dict[str, float]:
    """Compute derived fielding stats. Note: framing/arm/zr are ratings rather than
    counting stats so they're carried through as-is, weighted-averaged across years
    by the same year_weights.
    """
    tc = c.get("tc", 0.0)
    po = c.get("po", 0.0)
    a = c.get("a", 0.0)
    e = c.get("e", 0.0)
    ipf = c.get("ipf", 0.0)
    g = c.get("g", 0.0)

    fpct = (po + a) / tc if tc > 0 else 0.0
    rf9 = ((po + a) * 9.0) / ipf if ipf > 0 else 0.0
    rf_per_g = (po + a) / g if g > 0 else 0.0
    cs_pct_against = c.get("rto", 0.0) / c.get("sba", 0.0) if c.get("sba", 0.0) > 0 else 0.0

    return {
        "G": g, "GS": c.get("gs", 0.0), "IPF": ipf,
        "TC": tc, "PO": po, "A": a, "E": e, "DP": c.get("dp", 0.0),
        "FPCT": fpct, "RF/9": rf9, "RF/G": rf_per_g,
        "PB": c.get("pb", 0.0), "SBA": c.get("sba", 0.0),
        "CS%_against": cs_pct_against,
        "ZR": c.get("zr", 0.0),
        "framing": c.get("framing", 0.0),
        "arm": c.get("arm", 0.0),
    }


# -----------------------------------------------------------------------------
# Convenience: pull a multi-year, multi-split bundle for one league
# -----------------------------------------------------------------------------

def fetch_window(
    base_url: str,
    endpoint: str,
    year: int,
    window: int = 3,
    splits: Optional[List[int]] = None,
    lids: Optional[List[int]] = None,
    cache_dir: Optional[Path] = None,
    fast_path: bool = False,
    progress_desc: Optional[str] = None,
    errors: Optional[List[str]] = None,
) -> Dict[int, Dict[int, List[Dict[str, str]]]]:
    """Fetch [year, year-1, ..., year-(window-1)] across the given splits.

    If ``lids`` is provided, fetches each lid and concatenates rows for the
    year/split. Without it, the StatsPlus API defaults to top-level leagues
    only (i.e. ML). With ``cache_dir`` set, each distinct URL is disk-cached
    until the next calendar day. ``fast_path`` skips per-URL freshness checks
    and per-URL log spam when the caller has already validated the cache.

    ``errors`` (a list) is forwarded to ``fetch_year`` so the caller can detect
    partial failures before writing the "cache complete" marker.
    """
    splits = splits or [SPLIT_OVERALL]
    out: Dict[int, Dict[int, List[Dict[str, str]]]] = {s: {} for s in splits}
    pairs = [(s, y) for s in splits for y in range(year, year - window, -1)]
    desc = progress_desc or endpoint
    for s, y in _tqdm(pairs, desc=desc, leave=False, unit="req"):
        out[s][y] = fetch_year(
            base_url, endpoint, y, split=s, lids=lids,
            cache_dir=cache_dir, fast_path=fast_path, errors=errors,
        )
    return out


def aggregate_split(
    by_year: Dict[int, List[Dict[str, str]]],
    year_weights: List[float],
    counting_cols: Iterable[str],
) -> Dict[str, Dict[str, float]]:
    """Wrapper exposing aggregate_counting for one split."""
    return aggregate_counting(by_year, year_weights, counting_cols)


def compute_lg_constants_from_raw(
    bat_by_year: Dict[int, List[Dict[str, str]]],
    pit_by_year: Dict[int, List[Dict[str, str]]],
    year_weights: List[float],
    woba_weights: Dict[str, float],
) -> Dict[str, float]:
    """Compute league-average constants directly from year-segmented raw rows.

    Caller is responsible for pre-filtering rows by lid before passing them in.
    Returns lg_wOBA / lg_R_per_PA / lg_FIP / lg_ERA / cFIP plus sample sizes.
    """
    # Hitters: aggregate counting (year-weighted), then derive league rates.
    h_counting = aggregate_counting(bat_by_year, year_weights, HITTER_COUNTING)
    total_pa, woba_pa_sum, total_runs = 0.0, 0.0, 0.0
    for pid, c in h_counting.items():
        derived = hitter_derived(c, woba_weights)
        pa = derived.get("PA", 0.0)
        if pa <= 0:
            continue
        total_pa += pa
        woba_pa_sum += derived.get("wOBA", 0.0) * pa
        total_runs += derived.get("R", 0.0)

    # Pitchers: same shape.
    p_counting = aggregate_counting(pit_by_year, year_weights, PITCHER_COUNTING)
    pit_constants = league_constants(p_counting, woba_weights)
    total_ip, fip_ip_sum, total_er = 0.0, 0.0, 0.0
    for pid, c in p_counting.items():
        derived = pitcher_derived(c, pit_constants)
        ip = derived.get("IP", 0.0)
        if ip <= 0:
            continue
        total_ip += ip
        fip_ip_sum += derived.get("FIP", 0.0) * ip
        total_er += derived.get("ERA", 0.0) * ip / 9.0

    return {
        "lg_wOBA": (woba_pa_sum / total_pa) if total_pa > 0 else 0.320,
        "lg_R_per_PA": (total_runs / total_pa) if total_pa > 0 else 0.115,
        "lg_FIP": (fip_ip_sum / total_ip) if total_ip > 0 else 4.00,
        "lg_ERA": (total_er * 9.0 / total_ip) if total_ip > 0 else 4.00,
        "cFIP": pit_constants.get("cFIP", 0.0),
        "sample": {"total_pa": total_pa, "total_ip": total_ip},
    }


def _filter_rows_by_lid(
    by_year: Dict[int, List[Dict[str, str]]],
    target_lids: List[int],
) -> Dict[int, List[Dict[str, str]]]:
    """Return per-year rows whose ``league_id`` is in target_lids."""
    target_set = {int(x) for x in target_lids}
    out: Dict[int, List[Dict[str, str]]] = {}
    for year, rows in by_year.items():
        kept = []
        for r in rows:
            try:
                lid = int(float((r.get("league_id") or "0").strip()))
            except (TypeError, ValueError):
                continue
            if lid in target_set:
                kept.append(r)
        out[year] = kept
    return out


# Two-tier in-process memoization for build_player_stats.
#
# Tier 1: _POOL_AGGREGATE_CACHE — keyed WITHOUT target_lids. Caches the heavy
#   fetch + aggregate work (hitters/pitchers/fielders dicts + the raw
#   overall-split data needed to compute lg_constants). Hits across every
#   level in a multi-level run because lids/cache_dir don't change per level.
#
# Tier 2: _PLAYER_STATS_CACHE — keyed WITH target_lids. Caches the final
#   (hitters, pitchers, fielders, lg_constants) tuple per level. Hits across
#   every org at the same level.
_POOL_AGGREGATE_CACHE: Dict[Tuple[Any, ...], Tuple[
    Dict[str, Dict[str, Any]],
    Dict[str, Dict[str, Any]],
    Dict[str, Dict[str, Any]],
    Dict[str, Dict[int, List[Dict[str, str]]]],  # bat raw, keyed by split (year -> rows)
    Dict[str, Dict[int, List[Dict[str, str]]]],  # pit raw, keyed by split (year -> rows)
]] = {}

_PLAYER_STATS_CACHE: Dict[Tuple[Any, ...], Tuple[
    Dict[str, Dict[str, Any]],
    Dict[str, Dict[str, Any]],
    Dict[str, Dict[str, Any]],
    Dict[str, Dict[str, float]],
]] = {}


def clear_player_stats_cache() -> None:
    """Drop both in-process build_player_stats memos (e.g. between test runs)."""
    _POOL_AGGREGATE_CACHE.clear()
    _PLAYER_STATS_CACHE.clear()


def _hashable_woba_weights(woba_weights: Dict[str, float]) -> Tuple[Tuple[str, float], ...]:
    """Skip non-numeric entries (e.g. JSON comment fields) so the key stays hashable."""
    pairs: List[Tuple[str, float]] = []
    for k, v in (woba_weights or {}).items():
        try:
            pairs.append((str(k), round(float(v), 6)))
        except (TypeError, ValueError):
            continue
    return tuple(sorted(pairs))


def _hashable_year_weights(year_weights: List[float]) -> Tuple[float, ...]:
    out: List[float] = []
    for w in (year_weights or ()):
        try:
            out.append(round(float(w), 6))
        except (TypeError, ValueError):
            continue
    return tuple(out)


def _pool_cache_key(
    base_url: str,
    year: int,
    year_weights: List[float],
    woba_weights: Dict[str, float],
    lids: Optional[List[int]],
    cache_dir: Optional[Path],
) -> Tuple[Any, ...]:
    """Cache key for the pool-wide aggregate (no target_lids dependency)."""
    return (
        base_url,
        int(year),
        _hashable_year_weights(year_weights),
        _hashable_woba_weights(woba_weights),
        tuple(int(x) for x in (lids or ())),
        str(cache_dir) if cache_dir is not None else "",
    )


def _player_stats_cache_key(
    base_url: str,
    year: int,
    year_weights: List[float],
    woba_weights: Dict[str, float],
    lids: Optional[List[int]],
    target_lids: Optional[List[int]],
    cache_dir: Optional[Path],
) -> Tuple[Any, ...]:
    """Cache key for the full (hitters, pitchers, fielders, lg_constants) tuple."""
    return (
        base_url,
        int(year),
        _hashable_year_weights(year_weights),
        _hashable_woba_weights(woba_weights),
        tuple(int(x) for x in (lids or ())),
        tuple(int(x) for x in (target_lids or ())),
        str(cache_dir) if cache_dir is not None else "",
    )


def build_player_stats(
    base_url: str,
    year: int,
    year_weights: List[float],
    woba_weights: Dict[str, float],
    lids: Optional[List[int]] = None,
    target_lids: Optional[List[int]] = None,
    cache_dir: Optional[Path] = None,
) -> Tuple[
    Dict[str, Dict[str, Any]],
    Dict[str, Dict[str, Any]],
    Dict[str, Dict[str, Any]],
    Dict[str, Dict[str, float]],
]:
    """One-call convenience: returns (hitters, pitchers, fielders, lg_constants).

    hitters[pid] = {"overall": {...derived}, "vs_l": {...}, "vs_r": {...}}
    pitchers[pid] = {"overall": {...derived 3-yr-weighted...}, "current": {...current year only...}}
    fielders[pid] = {position: {...derived}}
    lg_constants = {"full": {...}, "current": {...}} — both restricted to ``target_lids``
        when provided so projection averages aren't dragged by promotion-pool levels.

    Pass ``lids`` to fetch from non-ML leagues (the API defaults to ML only when
    no lid is supplied). Pass ``target_lids`` (subset of ``lids``) to scope league
    averages to a single target level — typically the same level the depth chart
    is being built for. If omitted, lg_constants are computed across all ``lids``.

    Memoized in-process: identical (base_url, year, year_weights, woba_weights,
    lids, target_lids, cache_dir) returns the previously-computed result without
    re-reading the cache or re-aggregating. Multi-org batch runs share one
    aggregation instead of paying for it per org.
    """
    # Tier-2 cache hit: same level + same target_lids as a previous call.
    full_key = _player_stats_cache_key(
        base_url, year, year_weights, woba_weights, lids, target_lids, cache_dir,
    )
    if full_key in _PLAYER_STATS_CACHE:
        logger.info("build_player_stats: full cache hit (per-target-lids).")
        return _PLAYER_STATS_CACHE[full_key]

    # Tier-1 cache: pool aggregates (hitters/pitchers/fielders + raw split
    # data). Independent of target_lids, so multi-level runs share one
    # aggregation across every level.
    hitters_pool, pitchers_pool, fielders, bat_raw, pit_raw = _build_pool_aggregates(
        base_url, year, year_weights, woba_weights, lids, cache_dir,
    )

    bat_overall_raw = bat_raw.get(SPLIT_OVERALL, {})
    pit_overall_raw = pit_raw.get(SPLIT_OVERALL, {})

    # Cheap per-target-lids step: filter the raw overall data down to the
    # target level and compute league constants. Runs in milliseconds.
    if target_lids:
        bat_target = _filter_rows_by_lid(bat_overall_raw, target_lids)
        pit_target = _filter_rows_by_lid(pit_overall_raw, target_lids)
    else:
        bat_target = bat_overall_raw
        pit_target = pit_overall_raw

    lg_full = compute_lg_constants_from_raw(bat_target, pit_target, year_weights, woba_weights)
    cur_year_key = max(bat_target.keys(), default=year) if bat_target else year
    lg_current = compute_lg_constants_from_raw(
        {cur_year_key: bat_target.get(cur_year_key, [])},
        {cur_year_key: pit_target.get(cur_year_key, [])},
        [1.0], woba_weights,
    )
    lg_constants = {"full": lg_full, "current": lg_current}

    # Shallow-copy per-player views so the per-target-lid `_target` views we
    # add below don't leak into other build_player_stats() calls (different
    # target_lids) that share the same pool aggregate cache.
    hitters: Dict[str, Dict[str, Any]] = {pid: dict(view) for pid, view in hitters_pool.items()}
    pitchers: Dict[str, Dict[str, Any]] = {pid: dict(view) for pid, view in pitchers_pool.items()}

    # Per-target-lid per-player aggregates. These power the depth-chart CSV
    # and the player_stats sidecar so that --level ML projections use only
    # ML-level stats (e.g., a recently-promoted hitter's PA_current reflects
    # ML PA, not his combined ML+AAA total). The pool-level cross-level views
    # stay on the bundle under the unsuffixed keys for promotion ranking.
    if target_lids:
        for split, label in (
            (SPLIT_OVERALL, "overall"),
            (SPLIT_VS_L, "vs_l"),
            (SPLIT_VS_R, "vs_r"),
        ):
            split_target = _filter_rows_by_lid(bat_raw.get(split, {}), target_lids)
            # 3-yr-weighted, target-lid-only.
            counting = aggregate_counting(split_target, year_weights, HITTER_COUNTING)
            for pid, c in counting.items():
                hitters.setdefault(pid, {})[f"{label}_target"] = hitter_derived(c, woba_weights)
            # Current-year-only, target-lid-only. Always stamped (zero-PA target
            # is the truth for a player who hasn't appeared at this level yet).
            cur_yk = max(split_target.keys(), default=year) if split_target else year
            cur_by_year = {cur_yk: split_target.get(cur_yk, [])}
            cur_counting = aggregate_counting(cur_by_year, [1.0], HITTER_COUNTING)
            for pid, c in cur_counting.items():
                hitters.setdefault(pid, {})[f"{label}_current_target"] = hitter_derived(c, woba_weights)

        # Pitchers (overall split only — no L/R splits in the existing fetch).
        pit_target_full = _filter_rows_by_lid(pit_raw.get(SPLIT_OVERALL, {}), target_lids)
        pit_counting_t = aggregate_counting(pit_target_full, year_weights, PITCHER_COUNTING)
        pit_constants_t = league_constants(pit_counting_t, woba_weights)
        for pid, c in pit_counting_t.items():
            pitchers.setdefault(pid, {})["overall_target"] = pitcher_derived(c, pit_constants_t)
        cur_yk_p = max(pit_target_full.keys(), default=year) if pit_target_full else year
        pit_cur_t = {cur_yk_p: pit_target_full.get(cur_yk_p, [])}
        pit_cur_counting_t = aggregate_counting(pit_cur_t, [1.0], PITCHER_COUNTING)
        pit_cur_constants_t = league_constants(pit_cur_counting_t, woba_weights)
        for pid, c in pit_cur_counting_t.items():
            pitchers.setdefault(pid, {})["current_target"] = pitcher_derived(c, pit_cur_constants_t)

        # Stamp zero-PA / zero-IP placeholders for `_current_target` keys on
        # any pid that didn't pick up real target-current data above. The
        # downstream "always prefer target current view" logic in depth_chart
        # uses key presence to detect target-lids mode — without this pass, a
        # newly-promoted hitter with zero ML PA would silently fall back to
        # his cross-level current-year totals.
        zero_h_view = hitter_derived({}, woba_weights)
        zero_pit_constants = league_constants({}, woba_weights)
        zero_p_view = pitcher_derived({}, zero_pit_constants)
        for view in hitters.values():
            for label in ("overall", "vs_l", "vs_r"):
                view.setdefault(f"{label}_current_target", dict(zero_h_view))
        for view in pitchers.values():
            view.setdefault("current_target", dict(zero_p_view))

    result = (hitters, pitchers, fielders, lg_constants)
    _PLAYER_STATS_CACHE[full_key] = result
    return result


def _build_pool_aggregates(
    base_url: str,
    year: int,
    year_weights: List[float],
    woba_weights: Dict[str, float],
    lids: Optional[List[int]],
    cache_dir: Optional[Path],
) -> Tuple[
    Dict[str, Dict[str, Any]],
    Dict[str, Dict[str, Any]],
    Dict[str, Dict[str, Any]],
    Dict[str, Dict[int, List[Dict[str, str]]]],
    Dict[str, Dict[int, List[Dict[str, str]]]],
]:
    """Heavy fetch + aggregate work that doesn't depend on target_lids.

    Returns (hitters, pitchers, fielders, bat_raw, pit_raw). bat_raw / pit_raw
    are keyed by split (SPLIT_OVERALL / SPLIT_VS_L / SPLIT_VS_R for batting;
    SPLIT_OVERALL only for pitching) so the build_player_stats outer layer can
    derive both per-target-lid lg_constants AND per-target-lid per-player views
    without re-fetching or re-aggregating the cross-level pool.
    """
    pool_key = _pool_cache_key(base_url, year, year_weights, woba_weights, lids, cache_dir)
    if pool_key in _POOL_AGGREGATE_CACHE:
        logger.info("build_player_stats: pool aggregate cache hit (skipping fetch+aggregate).")
        return _POOL_AGGREGATE_CACHE[pool_key]

    window = max(1, len(year_weights))

    # If the cache marker says "all fresh today", skip per-URL freshness checks
    # and per-URL log spam in the fetch chain. On any cache miss, _fetch_csv
    # silently re-fetches; we always rewrite the marker after a successful run.
    fast_path = _cache_marker_fresh(cache_dir)
    if fast_path:
        logger.info("Cache marker fresh — using fast path (skipping per-URL checks).")

    fetch_errors: List[str] = []
    bat = fetch_window(base_url, "playerbatstatsv2", year, window, splits=[SPLIT_OVERALL, SPLIT_VS_L, SPLIT_VS_R], lids=lids, cache_dir=cache_dir, fast_path=fast_path, progress_desc="bat", errors=fetch_errors)
    pit = fetch_window(base_url, "playerpitchstatsv2", year, window, splits=[SPLIT_OVERALL], lids=lids, cache_dir=cache_dir, fast_path=fast_path, progress_desc="pitch", errors=fetch_errors)
    fld = fetch_window(base_url, "playerfieldstatsv2", year, window, splits=[SPLIT_OVERALL], lids=lids, cache_dir=cache_dir, fast_path=fast_path, progress_desc="field", errors=fetch_errors)

    # Drop the marker only after a FULLY-successful warm. If any per-lid fetch
    # failed, leave the marker stale so the next run re-validates each file by
    # date (and re-fetches the failures) instead of trusting a partial cache as
    # "complete" — which would otherwise serve the gaps as empty all day.
    if fetch_errors:
        logger.warning(
            "%d stat fetch(es) failed this run — NOT writing the cache-complete marker "
            "so the next run re-checks and re-fetches the gaps.", len(fetch_errors),
        )
    else:
        _write_cache_marker(cache_dir)

    # Hitters: one bundle per split, both 3-yr-weighted ("overall"/"vs_l"/"vs_r")
    # AND current-year-only views ("overall_current"/"vs_l_current"/"vs_r_current").
    # The current-year views power project_season.py's --blend-current-woba.
    hitters: Dict[str, Dict[str, Any]] = {}
    for split, label in ((SPLIT_OVERALL, "overall"), (SPLIT_VS_L, "vs_l"), (SPLIT_VS_R, "vs_r")):
        counting = aggregate_counting(bat[split], year_weights, HITTER_COUNTING)
        for pid, c in counting.items():
            hitters.setdefault(pid, {})[label] = hitter_derived(c, woba_weights)

        # Current-year-only hitter view per split.
        cur_year_key = max(bat[split].keys(), default=year)
        cur_by_year = {cur_year_key: bat[split].get(cur_year_key, [])}
        cur_counting = aggregate_counting(cur_by_year, [1.0], HITTER_COUNTING)
        for pid, c in cur_counting.items():
            hitters.setdefault(pid, {})[f"{label}_current"] = hitter_derived(c, woba_weights)

    # Pitchers: full window AND current-year-only views (current-year used by
    # project_season.py to blend against the 3-yr weighted view).
    pit_counting = aggregate_counting(pit[SPLIT_OVERALL], year_weights, PITCHER_COUNTING)
    constants = league_constants(pit_counting, woba_weights)
    pitchers: Dict[str, Dict[str, Any]] = {}
    for pid, c in pit_counting.items():
        pitchers[pid] = {"overall": pitcher_derived(c, constants), "_constants": constants}

    current_year_key = max(pit[SPLIT_OVERALL].keys(), default=year)
    pit_current_by_year = {current_year_key: pit[SPLIT_OVERALL].get(current_year_key, [])}
    pit_current_counting = aggregate_counting(pit_current_by_year, [1.0], PITCHER_COUNTING)
    current_constants = league_constants(pit_current_counting, woba_weights)
    for pid, c in pit_current_counting.items():
        derived = pitcher_derived(c, current_constants)
        if pid in pitchers:
            pitchers[pid]["current"] = derived
        else:
            pitchers[pid] = {"overall": derived, "current": derived, "_constants": current_constants}

    # Fielders: bucket rows by (player, position) since one player can play
    # several positions, and stats need to roll up per-position.
    fielders: Dict[str, Dict[str, Any]] = {}
    by_pos: Dict[Tuple[str, str], Dict[int, List[Dict[str, str]]]] = {}
    pos_id_to_label = {
        "1": "P", "2": "C", "3": "1B", "4": "2B", "5": "3B",
        "6": "SS", "7": "LF", "8": "CF", "9": "RF", "10": "DH",
    }
    for y, rows in fld[SPLIT_OVERALL].items():
        for r in rows:
            pid = (r.get("player_id") or "").strip()
            pos_raw = (r.get("position") or "").strip()
            pos = pos_id_to_label.get(pos_raw, pos_raw)
            if not pid or not pos:
                continue
            slot = by_pos.setdefault((pid, pos), {})
            slot.setdefault(y, []).append(r)

    rating_cols = ("framing", "arm", "zr")
    for (pid, pos), per_year in by_pos.items():
        counting = aggregate_counting(per_year, year_weights, FIELDER_COUNTING)
        ratings = aggregate_ratings(per_year, year_weights, rating_cols)
        c = counting.get(pid, {col: 0.0 for col in FIELDER_COUNTING})
        c.update(ratings.get(pid, {col: 0.0 for col in rating_cols}))
        fielders.setdefault(pid, {})[pos] = fielder_derived(c)

    pool_result = (hitters, pitchers, fielders, bat, pit)
    _POOL_AGGREGATE_CACHE[pool_key] = pool_result
    return pool_result


__all__ = [
    "SPLIT_OVERALL", "SPLIT_VS_L", "SPLIT_VS_R", "SPLIT_PLAYOFF",
    "resolve_base_url", "fetch_year", "fetch_window",
    "aggregate_counting", "aggregate_ratings", "aggregate_split",
    "hitter_derived", "pitcher_derived", "fielder_derived",
    "league_constants", "compute_lg_constants_from_raw",
    "HITTER_COUNTING", "PITCHER_COUNTING", "FIELDER_COUNTING",
    "build_player_stats", "clear_player_stats_cache",
]
