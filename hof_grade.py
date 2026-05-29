#!/usr/bin/env python3
"""
hof_grade.py — Hall of Fame candidacy grader for a single player.

Pulls career stats from /playerbatstatsv2, /playerpitchstatsv2, and
/playerfieldstatsv2 (filtered to ML, level_id=1, split=1 overall), then
runs a battery of HoF heuristics:

  - Career ML WAR (sum across batting+pitching stints)
  - JAWS — average of (career WAR, sum of top 7 single-season WARs)
  - 7-year peak WAR
  - Bill James HoF Monitor (~100 = likely, 130 = lock)
  - Bill James HoF Standards (~50 = average HoFer)
  - Career counting-stat milestones (3000 H, 500 HR, 300 W, 3000 K, ...)
  - Postseason boost (uses split=21 from the same endpoints)
  - Position adjustment for JAWS thresholds (C/SS get lower bars than 1B/LF)

A "Resume Strength" score rolls these signals into one number (how strong
the HoF resume is, NOT a vote-share prediction), but the scorecard is the
real product — the components explain the verdict.

Usage:

    py hof_grade.py --league sahl --id 67384
    py hof_grade.py --league sahl --id 67384 --refresh   # ignore cached report
    py hof_grade.py --league sahl --id 67384 --json      # machine-readable

    # Batch: grade many players, print a sorted table. Per-player MD reports
    # are still saved (and cached) just like single-id runs.
    py hof_grade.py --league sahl --ids 67384,15416,32523
    py hof_grade.py --league sahl --ids-file hof_candidates.txt

Output:
  - Console scorecard (always)
  - reports/hof_review/{league}/{id}-{Last_First}.md (cached; if it exists,
    we print it back instead of recomputing — pass --refresh to override)

Thresholds live in code (MLB-historical defaults). To override per-league,
drop a file at config/hof_thresholds-{league}.json with any of the same keys.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import logging
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from fetch_player_data import (
    _get,
    load_cookie_for,
    load_league_base,
    load_token_for,
)

logger = logging.getLogger("hof_grade")

REPO_ROOT = Path(__file__).resolve().parent
CACHE_DIR = REPO_ROOT / "cache" / "hof"
REPORTS_DIR = REPO_ROOT / "reports" / "hof_review"
PLAYER_DATA_DIR = REPO_ROOT / "data"
CONFIG_DIR = REPO_ROOT / "config"


# -----------------------------------------------------------------------------
# Position codes — OOTP fielding 'position' column is numeric
# -----------------------------------------------------------------------------
# 1=P 2=C 3=1B 4=2B 5=3B 6=SS 7=LF 8=CF 9=RF 10=DH
POS_CODE_TO_NAME = {
    1: "P", 2: "C", 3: "1B", 4: "2B", 5: "3B", 6: "SS",
    7: "LF", 8: "CF", 9: "RF", 10: "DH",
}


# -----------------------------------------------------------------------------
# Default thresholds — calibrated to MLB-historical HoFers (Jaffe / James).
# Overridable via config/hof_thresholds-{league}.json.
# -----------------------------------------------------------------------------

DEFAULT_THRESHOLDS: Dict[str, Any] = {
    # JAWS targets per position (Jaffe averages of inducted HoFers).
    # Pitchers handled separately (SP vs RP).
    "jaws_target_by_pos": {
        "C":  44.0,
        "1B": 54.0,
        "2B": 57.0,
        "3B": 56.0,
        "SS": 55.0,
        "LF": 53.0,
        "CF": 58.0,
        "RF": 58.0,
        "DH": 54.0,
        "SP": 62.0,
        "RP": 32.0,
    },
    # Career WAR rough cutoffs (Jaffe-style)
    "war_borderline": 50.0,
    "war_likely":     65.0,
    "war_lock":       80.0,
    # Bill James HoF Monitor cutoffs
    "monitor_likely": 100.0,
    "monitor_lock":   130.0,
    # Bill James HoF Standards (50 ~ avg HoFer, 75+ strong)
    "standards_avg":  50.0,
    # Composite probability bands (% chance of induction, after weighting)
    "prob_no":      25.0,
    "prob_maybe":   45.0,
    "prob_likely":  65.0,
    "prob_lock":    80.0,
}


def load_thresholds(league: str) -> Dict[str, Any]:
    """Defaults, then merge any league-specific overrides on top."""
    cfg = json.loads(json.dumps(DEFAULT_THRESHOLDS))  # deep copy
    override_path = CONFIG_DIR / f"hof_thresholds-{league}.json"
    if override_path.exists():
        with override_path.open(encoding="utf-8") as f:
            overrides = json.load(f)
        # Shallow-merge top-level, deep-merge nested dicts (e.g. jaws targets).
        for k, v in overrides.items():
            if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                cfg[k].update(v)
            else:
                cfg[k] = v
        logger.info("Applied threshold overrides from %s", override_path)
    return cfg


# -----------------------------------------------------------------------------
# Fetch — these endpoints are synchronous (no polling like /ratings).
# -----------------------------------------------------------------------------

def _fetch_endpoint(
    league: str,
    endpoint: str,
    player_id: str,
    split: Optional[int] = 1,
    refresh: bool = False,
) -> str:
    """Hit one of the three player stat endpoints. Cache the raw CSV under
    cache/hof/{league}/{player_id}-{endpoint}-split{split}.csv.
    """
    cache_dir = CACHE_DIR / league
    cache_dir.mkdir(parents=True, exist_ok=True)
    suffix = f"split{split}" if split is not None else "all"
    cache_path = cache_dir / f"{player_id}-{endpoint}-{suffix}.csv"
    if cache_path.exists() and not refresh:
        logger.debug("cache hit: %s", cache_path)
        return cache_path.read_text(encoding="utf-8")

    base = load_league_base(league)
    token = load_token_for(league)
    cookie = None if token else load_cookie_for(base)
    if not token and not cookie:
        raise SystemExit(
            f"No auth for league '{league}'. Add a token to "
            f"config/statsplus_tokens.json or cookies to "
            f"config/statsplus_session.json."
        )
    params = [f"pid={player_id}"]
    if split is not None and endpoint != "playerfieldstatsv2":
        params.append(f"split={split}")
    if token:
        params.append(f"token={token}")
    url = f"{base}/{endpoint}/?{'&'.join(params)}"
    logger.debug("fetching %s", url)
    status, body = _get(url, None if token else cookie, timeout=60)
    if status == 204 or (status == 200 and not body.strip()):
        # Player has no rows for this endpoint (e.g. pitcher with no batting).
        body = ""
    elif status >= 400:
        raise SystemExit(
            f"HTTP {status} from {endpoint} for pid={player_id}: {body[:200]}"
        )
    cache_path.write_text(body, encoding="utf-8")
    return body


def _parse_csv(text: str) -> List[Dict[str, str]]:
    if not text.strip():
        return []
    return list(csv.DictReader(io.StringIO(text)))


# -----------------------------------------------------------------------------
# Player bio — pulled from data/PlayerData-{league}.csv if cached locally,
# falling back to the /players endpoint.
# -----------------------------------------------------------------------------

@dataclass
class PlayerBio:
    player_id: str
    name: str = ""
    pos: str = ""               # declared position (e.g. "CF", "SP")
    age: Optional[int] = None
    bats: str = ""
    throws: str = ""
    retired: bool = False
    inducted: bool = False
    hall_of_fame: bool = False


def load_bio(league: str, player_id: str) -> PlayerBio:
    pid = str(player_id).strip()
    # First try /players API for the most authoritative bio + HoF status.
    try:
        base = load_league_base(league)
        token = load_token_for(league)
        cookie = None if token else load_cookie_for(base)
        url = f"{base}/players/"
        if token:
            url += f"?token={token}"
        status, body = _get(url, None if token else cookie, timeout=60)
        if status == 200 and body:
            for row in csv.DictReader(io.StringIO(body)):
                if (row.get("ID") or "").strip() == pid:
                    age = row.get("Age")
                    return PlayerBio(
                        player_id=pid,
                        name=f"{(row.get('First Name') or '').strip()} "
                             f"{(row.get('Last Name') or '').strip()}".strip(),
                        pos=(row.get("Pos") or "").strip(),
                        age=int(age) if age and age.isdigit() else None,
                        bats=(row.get("bats") or "").strip(),
                        throws=(row.get("throws") or "").strip(),
                        retired=(row.get("Retired") or "0") == "1",
                        inducted=(row.get("inducted") or "0") == "1",
                        hall_of_fame=(row.get("hall_of_fame") or "0") == "1",
                    )
    except Exception as e:
        logger.debug("players API lookup failed: %s", e)

    # Fallback: PlayerData CSV (no HoF flags but has name/pos).
    path = PLAYER_DATA_DIR / f"PlayerData-{league}.csv"
    if path.exists():
        with path.open(encoding="utf-8") as f:
            for row in csv.DictReader(f):
                if (row.get("ID") or "").strip() == pid:
                    return PlayerBio(
                        player_id=pid,
                        name=(row.get("Name") or "").strip(),
                        pos=(row.get("Pos") or "").strip(),
                        age=(int(row["Age"]) if row.get("Age", "").isdigit() else None),
                        bats=(row.get("Bats") or "").strip(),
                        throws=(row.get("Throws") or "").strip(),
                    )
    return PlayerBio(player_id=pid)


# -----------------------------------------------------------------------------
# Aggregation
# -----------------------------------------------------------------------------

def _f(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _i(v: Any, default: int = 0) -> int:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return default


def _filter_ml(rows: List[Dict[str, str]]) -> List[Dict[str, str]]:
    """Keep only ML rows (level_id == 1) and overall splits (split_id == 1)."""
    out = []
    for r in rows:
        if _i(r.get("level_id"), -1) != 1:
            continue
        # Fielding has split_id=0; bat/pitch use 1 for overall.
        sid = _i(r.get("split_id"), -1)
        if sid not in (0, 1):
            continue
        out.append(r)
    return out


def _aggregate_by_year(
    rows: List[Dict[str, str]],
    sum_cols: List[str],
    extra_cols: Optional[List[str]] = None,
) -> Dict[int, Dict[str, float]]:
    """Sum numeric columns across stints within a season. Returns
    {year: {col: total, ...}}. extra_cols are summed too but kept separate
    in case the caller wants to recompute rates after the season-totals roll
    up (e.g. AVG = H/AB after summing).
    """
    out: Dict[int, Dict[str, float]] = {}
    cols = list(sum_cols) + list(extra_cols or [])
    for r in rows:
        yr = _i(r.get("year"))
        if yr <= 0:
            continue
        slot = out.setdefault(yr, {c: 0.0 for c in cols})
        for c in cols:
            slot[c] += _f(r.get(c))
    return out


# -----------------------------------------------------------------------------
# HoF heuristic computations
# -----------------------------------------------------------------------------

@dataclass
class HittingTotals:
    seasons: int = 0
    g: int = 0
    pa: int = 0
    ab: int = 0
    h: int = 0
    d: int = 0
    t: int = 0
    hr: int = 0
    r: int = 0
    rbi: int = 0
    bb: int = 0
    k: int = 0
    sb: int = 0
    war_seasons: List[Tuple[int, float]] = field(default_factory=list)  # (year, war)

    @property
    def war(self) -> float:
        return sum(w for _, w in self.war_seasons)

    @property
    def avg(self) -> float:
        return self.h / self.ab if self.ab else 0.0

    @property
    def obp(self) -> float:
        denom = self.ab + self.bb  # ignoring HBP/SF for brevity
        return (self.h + self.bb) / denom if denom else 0.0

    @property
    def slg(self) -> float:
        if not self.ab:
            return 0.0
        singles = self.h - self.d - self.t - self.hr
        tb = singles + 2 * self.d + 3 * self.t + 4 * self.hr
        return tb / self.ab

    @property
    def ops(self) -> float:
        return self.obp + self.slg


@dataclass
class PitchingTotals:
    seasons: int = 0
    g: int = 0
    gs: int = 0
    ip: float = 0.0
    w: int = 0
    l: int = 0
    sv: int = 0
    so: int = 0
    bb: int = 0
    h: int = 0
    er: int = 0
    cg: int = 0
    sho: int = 0
    war_seasons: List[Tuple[int, float]] = field(default_factory=list)

    @property
    def war(self) -> float:
        return sum(w for _, w in self.war_seasons)

    @property
    def era(self) -> float:
        return (self.er * 9.0) / self.ip if self.ip else 0.0

    @property
    def whip(self) -> float:
        return (self.bb + self.h) / self.ip if self.ip else 0.0


def aggregate_hitting(rows: List[Dict[str, str]]) -> HittingTotals:
    by_year = _aggregate_by_year(
        rows,
        sum_cols=["pa", "ab", "h", "d", "t", "hr", "r", "rbi", "bb", "k", "sb", "g", "war"],
    )
    t = HittingTotals()
    for yr, agg in by_year.items():
        if agg["pa"] <= 0 and agg["ab"] <= 0:
            continue
        t.seasons += 1
        t.g += int(agg["g"])
        t.pa += int(agg["pa"])
        t.ab += int(agg["ab"])
        t.h += int(agg["h"])
        t.d += int(agg["d"])
        t.t += int(agg["t"])
        t.hr += int(agg["hr"])
        t.r += int(agg["r"])
        t.rbi += int(agg["rbi"])
        t.bb += int(agg["bb"])
        t.k += int(agg["k"])
        t.sb += int(agg["sb"])
        t.war_seasons.append((yr, agg["war"]))
    t.war_seasons.sort()
    return t


def aggregate_pitching(rows: List[Dict[str, str]]) -> PitchingTotals:
    by_year = _aggregate_by_year(
        rows,
        sum_cols=["g", "gs", "ip", "w", "l", "s", "k", "bb", "ha", "er", "cg", "sho", "war"],
    )
    t = PitchingTotals()
    for yr, agg in by_year.items():
        if agg["ip"] <= 0:
            continue
        t.seasons += 1
        t.g += int(agg["g"])
        t.gs += int(agg["gs"])
        t.ip += agg["ip"]
        t.w += int(agg["w"])
        t.l += int(agg["l"])
        t.sv += int(agg["s"])
        t.so += int(agg["k"])
        t.bb += int(agg["bb"])
        t.h += int(agg["ha"])
        t.er += int(agg["er"])
        t.cg += int(agg["cg"])
        t.sho += int(agg["sho"])
        t.war_seasons.append((yr, agg["war"]))
    t.war_seasons.sort()
    return t


def jaws(war_seasons: List[Tuple[int, float]]) -> Tuple[float, float, float]:
    """Return (career_war, peak7_war, jaws). peak7 = sum of top 7 single-season
    WARs (using full season WARs, not partials)."""
    career = sum(w for _, w in war_seasons)
    top7 = sorted((w for _, w in war_seasons), reverse=True)[:7]
    peak7 = sum(top7)
    return career, peak7, (career + peak7) / 2.0


def primary_position(field_rows: List[Dict[str, str]], bio_pos: str) -> str:
    """Determine primary position by ML innings. Falls back to bio.pos."""
    by_pos: Dict[int, float] = {}
    for r in field_rows:
        code = _i(r.get("position"))
        ip = _f(r.get("ipf")) / 100.0 + _f(r.get("ip"))  # ipf is partial-innings
        # OOTP stores ip+ipf; we just want a relative measure so adding is fine
        by_pos[code] = by_pos.get(code, 0.0) + max(_f(r.get("ip")), ip, _f(r.get("g")))
    if not by_pos:
        return bio_pos or ""
    # Exclude pitcher unless that's the only position
    non_p = {k: v for k, v in by_pos.items() if k != 1}
    src = non_p if non_p else by_pos
    best_code = max(src, key=src.get)
    return POS_CODE_TO_NAME.get(best_code, bio_pos or "")


# -----------------------------------------------------------------------------
# Bill James HoF Monitor & Standards — simplified, but the structure mirrors
# the real Bill James scoring system. Calibrated for sim leagues, so awards
# data isn't required (we don't have it from these endpoints).
# -----------------------------------------------------------------------------

def hof_monitor_hitter(h: HittingTotals) -> Dict[str, float]:
    """Return {category: points}. Categories sum to the Monitor score."""
    pts: Dict[str, float] = {}
    # Single-season milestones (from per-season aggregations)
    seasons = [(_i(r["ab"]) if isinstance(r, dict) else 0, r) for r in []]
    # We need per-season totals; recompute here from war_seasons years.
    # (Skip — we hit single-season tests via separate parameter below if needed.)
    # Career milestones
    pts["3000 H"]   = 50 if h.h >= 3000 else (15 if h.h >= 2500 else (5 if h.h >= 2000 else 0))
    pts["500 HR"]   = 50 if h.hr >= 500 else (30 if h.hr >= 400 else (10 if h.hr >= 300 else 0))
    pts["1500 RBI"] = 25 if h.rbi >= 1500 else (10 if h.rbi >= 1200 else 0)
    pts["1500 R"]   = 25 if h.r >= 1500 else (10 if h.r >= 1200 else 0)
    pts["500 SB"]   = 15 if h.sb >= 500 else (5 if h.sb >= 300 else 0)
    pts[".300 AVG"] = 20 if h.avg >= .300 else (8 if h.avg >= .285 else 0)
    pts[".400 OBP"] = 12 if h.obp >= .400 else 0
    pts[".500 SLG"] = 12 if h.slg >= .500 else 0
    pts[".900 OPS"] = 15 if h.ops >= .900 else (5 if h.ops >= .830 else 0)
    return pts


def hof_monitor_pitcher(p: PitchingTotals) -> Dict[str, float]:
    pts: Dict[str, float] = {}
    pts["300 W"]    = 60 if p.w >= 300 else (30 if p.w >= 250 else (10 if p.w >= 200 else 0))
    pts["3000 K"]   = 40 if p.so >= 3000 else (15 if p.so >= 2500 else (5 if p.so >= 2000 else 0))
    pts["3.00 ERA"] = 15 if p.era and p.era < 3.00 else (5 if p.era and p.era < 3.50 else 0)
    pts["WHIP<1.20"] = 10 if 0 < p.whip < 1.20 else (3 if 0 < p.whip < 1.30 else 0)
    pts["IP/Workload"] = 20 if p.ip >= 4000 else (10 if p.ip >= 3000 else 0)
    pts["400 SV"]   = 50 if p.sv >= 400 else (25 if p.sv >= 300 else (10 if p.sv >= 200 else 0))
    pts["50 SHO"]   = 25 if p.sho >= 50 else (10 if p.sho >= 30 else 0)
    pts["100 CG"]   = 15 if p.cg >= 100 else (5 if p.cg >= 50 else 0)
    return pts


def hof_standards_hitter(h: HittingTotals) -> Dict[str, float]:
    """50 = avg HoFer. Captures career rate + counting stats."""
    pts: Dict[str, float] = {}
    pts["AVG"]  = min(8, (h.avg - .275) / .005) if h.avg > .275 else 0
    pts["OBP"]  = min(8, (h.obp - .355) / .005) if h.obp > .355 else 0
    pts["SLG"]  = min(8, (h.slg - .450) / .010) if h.slg > .450 else 0
    pts["Hits"] = min(15, h.h / 200)
    pts["HR"]   = min(15, h.hr / 35)
    pts["RBI"]  = min(12, h.rbi / 125)
    pts["Runs"] = min(12, h.r / 125)
    return {k: round(v, 1) for k, v in pts.items()}


def hof_standards_pitcher(p: PitchingTotals) -> Dict[str, float]:
    pts: Dict[str, float] = {}
    pts["Wins"]   = min(20, p.w / 15)
    pts["K"]      = min(15, p.so / 200)
    pts["ERA"]    = min(8, (3.80 - p.era) / 0.10) if 0 < p.era < 3.80 else 0
    pts["WHIP"]   = min(5, (1.30 - p.whip) / 0.04) if 0 < p.whip < 1.30 else 0
    pts["IP"]     = min(15, p.ip / 300)
    pts["SV"]     = min(10, p.sv / 50)
    return {k: round(v, 1) for k, v in pts.items()}


# -----------------------------------------------------------------------------
# Composite probability
# -----------------------------------------------------------------------------

def composite_probability(
    is_pitcher: bool,
    career_war: float,
    peak7_war: float,
    jaws_score: float,
    monitor_total: float,
    standards_total: float,
    jaws_target: float,
    thr: Dict[str, Any],
    postseason_bonus: float = 0.0,
) -> float:
    """Weighted blend of the four primary signals, returning 0-100. The
    individual signals are still on the scorecard — this is just a roll-up.
    """
    # Normalize each to a 0..1 ratio against its "likely HoFer" target.
    war_ratio       = career_war      / thr["war_likely"]
    peak_ratio      = peak7_war       / (jaws_target * 0.7)   # peak7 ≈ 70% of JAWS target for typical HoFer
    jaws_ratio      = jaws_score      / jaws_target
    monitor_ratio   = monitor_total   / thr["monitor_likely"]
    standards_ratio = standards_total / thr["standards_avg"]

    # Cap each ratio at 1.5 so a single outlier doesn't dominate.
    cap = lambda r: max(0.0, min(1.5, r))
    weighted = (
        0.30 * cap(war_ratio) +
        0.20 * cap(peak_ratio) +
        0.20 * cap(jaws_ratio) +
        0.20 * cap(monitor_ratio) +
        0.10 * cap(standards_ratio)
    )
    # weighted is now 0..1.5; map [0..1.0] to [0..80%] then any excess (the
    # "above HoFer" zone) into the remaining 20% so 100% is reserved for
    # true inner-circle resumes.
    base = min(weighted, 1.0) * 80.0
    over = max(0.0, weighted - 1.0) * 40.0  # excess (max 0.5) -> up to +20
    prob = base + min(over, 20.0)
    # Postseason adds a small finishing nudge (-2 to +5)
    return max(0.0, min(100.0, prob + postseason_bonus))


def verdict(prob: float, thr: Dict[str, Any]) -> str:
    if prob >= thr["prob_lock"]:
        return "LOCK"
    if prob >= thr["prob_likely"]:
        return "LIKELY"
    if prob >= thr["prob_maybe"]:
        return "BORDERLINE"
    if prob >= thr["prob_no"]:
        return "LONGSHOT"
    return "NOT HOF-WORTHY"


# -----------------------------------------------------------------------------
# Render
# -----------------------------------------------------------------------------

def render_report(payload: Dict[str, Any]) -> str:
    """Markdown scorecard. Same content used for both console and saved file."""
    b = payload["bio"]
    is_p = payload["is_pitcher"]
    out: List[str] = []
    out.append(f"# HoF Review — {b['name'] or '(unknown)'} (ID {b['player_id']})")
    out.append("")
    bio_line = []
    # /players returns Pos as a numeric code (1=P, 2=C, ...); translate when possible.
    raw_pos = (b.get("pos") or "").strip()
    pos_disp = POS_CODE_TO_NAME.get(int(raw_pos), raw_pos) if raw_pos.isdigit() else raw_pos
    if pos_disp:        bio_line.append(f"**Pos:** {pos_disp}")
    if payload.get("primary_pos"): bio_line.append(f"**Primary:** {payload['primary_pos']}")
    if b.get("age") is not None: bio_line.append(f"**Age:** {b['age']}")
    if b.get("bats") or b.get("throws"):
        bio_line.append(f"**B/T:** {b.get('bats','?')}/{b.get('throws','?')}")
    if b.get("retired"):    bio_line.append("**Retired**")
    if b.get("inducted"):   bio_line.append("**Inducted ✓**")
    out.append(" · ".join(bio_line))
    out.append("")

    out.append(f"## Verdict: **{payload['verdict']}**  ·  Resume Strength {payload['prob']:.1f}%")
    out.append("")
    out.append(f"Career ML WAR: **{payload['career_war']:.1f}**  ·  "
               f"7yr Peak: **{payload['peak7_war']:.1f}**  ·  "
               f"JAWS: **{payload['jaws']:.1f}**  "
               f"(target for {payload['primary_pos']}: {payload['jaws_target']:.1f})")
    out.append("")

    # Hitting
    h = payload.get("hitting")
    if h and h.get("seasons"):
        out.append("## Hitting")
        out.append(f"- Seasons: {h['seasons']}  ·  G: {h['g']}  ·  PA: {h['pa']:,}")
        out.append(f"- Slash: .{int(round(h['avg']*1000)):03d}/"
                   f".{int(round(h['obp']*1000)):03d}/"
                   f".{int(round(h['slg']*1000)):03d}  "
                   f"OPS {h['ops']:.3f}")
        out.append(f"- H: {h['h']:,}  ·  2B: {h['d']:,}  ·  3B: {h['t']}  "
                   f"·  HR: {h['hr']:,}  ·  R: {h['r']:,}  ·  RBI: {h['rbi']:,}  "
                   f"·  BB: {h['bb']:,}  ·  SB: {h['sb']}")
        out.append(f"- Batting WAR: {h['war']:.1f}")
        out.append("")

    # Pitching
    p = payload.get("pitching")
    if p and p.get("seasons"):
        out.append("## Pitching")
        out.append(f"- Seasons: {p['seasons']}  ·  G: {p['g']}  ·  GS: {p['gs']}  ·  IP: {p['ip']:.1f}")
        out.append(f"- W-L: {p['w']}-{p['l']}  ·  SV: {p['sv']}  ·  CG: {p['cg']}  ·  SHO: {p['sho']}")
        out.append(f"- K: {p['so']:,}  ·  BB: {p['bb']:,}  ·  ERA: {p['era']:.2f}  ·  WHIP: {p['whip']:.2f}")
        out.append(f"- Pitching WAR: {p['war']:.1f}")
        out.append("")

    # Monitor
    mon = payload["monitor"]
    out.append(f"## Bill James HoF Monitor — **{payload['monitor_total']:.0f}**  "
               f"(100 ~ likely HoFer, 130 ~ lock)")
    for cat, pts in sorted(mon.items(), key=lambda kv: -kv[1]):
        if pts > 0:
            out.append(f"- {cat}: {pts:g}")
    out.append("")

    # Standards
    std = payload["standards"]
    out.append(f"## Bill James HoF Standards — **{payload['standards_total']:.1f}**  "
               f"(50 ~ avg HoFer)")
    for cat, pts in sorted(std.items(), key=lambda kv: -kv[1]):
        if pts > 0:
            out.append(f"- {cat}: {pts:g}")
    out.append("")

    # Postseason
    ps = payload.get("postseason")
    if ps:
        out.append(f"## Postseason (boost: {ps['bonus']:+.1f})")
        if ps.get("bat_pa"):
            out.append(f"- Hitting: {ps['bat_pa']} PA, {ps['bat_hr']} HR, "
                       f"OPS {ps['bat_ops']:.3f}")
        if ps.get("pitch_ip"):
            out.append(f"- Pitching: {ps['pitch_ip']:.1f} IP, "
                       f"{ps['pitch_w']}-{ps['pitch_l']}, "
                       f"ERA {ps['pitch_era']:.2f}")
        out.append("")

    # Best seasons
    out.append("## Best Single Seasons by WAR")
    for yr, war in payload["top_seasons"][:7]:
        out.append(f"- {yr}: {war:+.1f} WAR")
    out.append("")

    out.append("---")
    out.append("_Auto-generated by hof_grade.py_")
    return "\n".join(out)


# -----------------------------------------------------------------------------
# Postseason — uses split=21 on the same endpoints
# -----------------------------------------------------------------------------

def compute_postseason(league: str, player_id: str, refresh: bool) -> Optional[Dict[str, Any]]:
    bat_csv   = _fetch_endpoint(league, "playerbatstatsv2",   player_id, split=21, refresh=refresh)
    pitch_csv = _fetch_endpoint(league, "playerpitchstatsv2", player_id, split=21, refresh=refresh)
    bat_rows = [r for r in _parse_csv(bat_csv) if _i(r.get("level_id"), -1) == 1]
    pit_rows = [r for r in _parse_csv(pitch_csv) if _i(r.get("level_id"), -1) == 1]
    if not bat_rows and not pit_rows:
        return None

    bat_pa = sum(_i(r.get("pa")) for r in bat_rows)
    bat_ab = sum(_i(r.get("ab")) for r in bat_rows)
    bat_h  = sum(_i(r.get("h"))  for r in bat_rows)
    bat_hr = sum(_i(r.get("hr")) for r in bat_rows)
    bat_bb = sum(_i(r.get("bb")) for r in bat_rows)
    bat_d  = sum(_i(r.get("d"))  for r in bat_rows)
    bat_t  = sum(_i(r.get("t"))  for r in bat_rows)
    singles = bat_h - bat_d - bat_t - bat_hr
    tb = singles + 2 * bat_d + 3 * bat_t + 4 * bat_hr
    bat_obp = (bat_h + bat_bb) / (bat_ab + bat_bb) if (bat_ab + bat_bb) else 0.0
    bat_slg = tb / bat_ab if bat_ab else 0.0
    bat_ops = bat_obp + bat_slg

    pit_ip = sum(_f(r.get("ip")) for r in pit_rows)
    pit_er = sum(_i(r.get("er")) for r in pit_rows)
    pit_w  = sum(_i(r.get("w"))  for r in pit_rows)
    pit_l  = sum(_i(r.get("l"))  for r in pit_rows)
    pit_era = (pit_er * 9.0) / pit_ip if pit_ip else 0.0

    # Postseason bonus: small nudge. Heroic-tier (.900+ OPS or sub-2.50 ERA
    # over a meaningful sample) adds up to +5; merely-having-played adds +1.
    bonus = 0.0
    if bat_pa >= 50:
        if bat_ops >= 1.000: bonus += 5
        elif bat_ops >= .900: bonus += 3
        elif bat_ops >= .800: bonus += 1
        elif bat_ops < .600: bonus -= 2
    if pit_ip >= 50:
        if pit_era <= 2.50: bonus += 5
        elif pit_era <= 3.25: bonus += 3
        elif pit_era <= 4.00: bonus += 1
        elif pit_era >= 5.50: bonus -= 2
    return {
        "bat_pa": bat_pa, "bat_hr": bat_hr, "bat_ops": bat_ops,
        "pitch_ip": pit_ip, "pitch_w": pit_w, "pitch_l": pit_l,
        "pitch_era": pit_era,
        "bonus": bonus,
    }


# -----------------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------------

def safe_filename(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", s).strip("_") or "unknown"


def grade(league: str, player_id: str, refresh: bool) -> Dict[str, Any]:
    bio = load_bio(league, player_id)

    bat_text   = _fetch_endpoint(league, "playerbatstatsv2",   player_id, split=1, refresh=refresh)
    pitch_text = _fetch_endpoint(league, "playerpitchstatsv2", player_id, split=1, refresh=refresh)
    field_text = _fetch_endpoint(league, "playerfieldstatsv2", player_id, split=None, refresh=refresh)

    bat_rows   = _filter_ml(_parse_csv(bat_text))
    pitch_rows = _filter_ml(_parse_csv(pitch_text))
    field_rows = [r for r in _parse_csv(field_text) if _i(r.get("level_id"), -1) == 1]

    hitting  = aggregate_hitting(bat_rows)
    pitching = aggregate_pitching(pitch_rows)

    # Decide whether this is primarily a pitcher.
    is_pitcher = pitching.ip >= 300 and pitching.war >= hitting.war
    primary_pos = primary_position(field_rows, bio.pos)
    if is_pitcher:
        # SP vs RP: starter if majority of appearances were starts
        primary_pos = "SP" if pitching.gs >= max(1, pitching.g * 0.5) else "RP"

    # Combined WAR seasons (a two-way player gets credit for both)
    combined: Dict[int, float] = {}
    for yr, w in hitting.war_seasons:  combined[yr] = combined.get(yr, 0.0) + w
    for yr, w in pitching.war_seasons: combined[yr] = combined.get(yr, 0.0) + w
    combined_seasons = sorted(combined.items())
    career_war, peak7_war, jaws_score = jaws(combined_seasons)

    monitor = (hof_monitor_pitcher(pitching) if is_pitcher
               else hof_monitor_hitter(hitting))
    standards = (hof_standards_pitcher(pitching) if is_pitcher
                 else hof_standards_hitter(hitting))
    monitor_total   = sum(monitor.values())
    standards_total = sum(standards.values())

    thr = load_thresholds(league)
    jaws_target = thr["jaws_target_by_pos"].get(primary_pos, 55.0)

    postseason = compute_postseason(league, player_id, refresh)
    ps_bonus = postseason["bonus"] if postseason else 0.0

    prob = composite_probability(
        is_pitcher, career_war, peak7_war, jaws_score,
        monitor_total, standards_total, jaws_target, thr, ps_bonus,
    )
    v = verdict(prob, thr)

    top_seasons = sorted(combined_seasons, key=lambda kv: -kv[1])

    return {
        "bio": bio.__dict__,
        "is_pitcher": is_pitcher,
        "primary_pos": primary_pos,
        "career_war": career_war,
        "peak7_war":  peak7_war,
        "jaws":       jaws_score,
        "jaws_target": jaws_target,
        "hitting":   _hit_to_dict(hitting),
        "pitching":  _pit_to_dict(pitching),
        "monitor":   monitor,
        "monitor_total":   monitor_total,
        "standards": standards,
        "standards_total": standards_total,
        "postseason": postseason,
        "prob":    prob,
        "verdict": v,
        "top_seasons": top_seasons,
    }


def _hit_to_dict(h: HittingTotals) -> Dict[str, Any]:
    return {
        "seasons": h.seasons, "g": h.g, "pa": h.pa, "ab": h.ab,
        "h": h.h, "d": h.d, "t": h.t, "hr": h.hr,
        "r": h.r, "rbi": h.rbi, "bb": h.bb, "k": h.k, "sb": h.sb,
        "avg": h.avg, "obp": h.obp, "slg": h.slg, "ops": h.ops,
        "war": h.war,
    }


def _pit_to_dict(p: PitchingTotals) -> Dict[str, Any]:
    return {
        "seasons": p.seasons, "g": p.g, "gs": p.gs, "ip": p.ip,
        "w": p.w, "l": p.l, "sv": p.sv, "so": p.so, "bb": p.bb,
        "h": p.h, "er": p.er, "cg": p.cg, "sho": p.sho,
        "era": p.era, "whip": p.whip, "war": p.war,
    }


# -----------------------------------------------------------------------------
# Batch mode
# -----------------------------------------------------------------------------

def _parse_ids(ids_arg: Optional[str], ids_file: Optional[Path]) -> List[str]:
    """Combine --ids (comma list) and --ids-file (one per line, # comments
    allowed) into a de-duplicated, order-preserving list."""
    out: List[str] = []
    seen: set[str] = set()

    def _add(token: str) -> None:
        token = token.strip()
        # Allow "12345  # Frank Velez" style annotations in the file
        token = token.split("#", 1)[0].strip()
        if token and token not in seen:
            seen.add(token)
            out.append(token)

    if ids_arg:
        for t in ids_arg.split(","):
            _add(t)
    if ids_file:
        if not ids_file.exists():
            raise SystemExit(f"--ids-file not found: {ids_file}")
        for line in ids_file.read_text(encoding="utf-8").splitlines():
            _add(line)
    return out


def _table_row(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Flatten a per-player payload to the columns shown in the batch table."""
    b = payload["bio"]
    return {
        "id":       b.get("player_id", ""),
        "name":     b.get("name") or "(unknown)",
        "pos":      payload.get("primary_pos") or "",
        "war":      payload["career_war"],
        "peak7":    payload["peak7_war"],
        "jaws":     payload["jaws"],
        "jaws_tgt": payload["jaws_target"],
        "monitor":  payload["monitor_total"],
        "stds":     payload["standards_total"],
        "resume":   payload["prob"],
        "verdict":  payload["verdict"],
    }


def render_table(rows: List[Dict[str, Any]], league: str) -> str:
    """Sorted (by resume desc) fixed-width table for the console."""
    rows = sorted(rows, key=lambda r: -r["resume"])
    headers = ["ID", "Name", "Pos", "WAR", "Peak7", "JAWS", "Tgt",
               "Mon", "Stds", "Resume%", "Verdict"]
    name_w = max(20, min(28, max((len(r["name"]) for r in rows), default=20)))
    fmt = (
        f"{{id:<7}}  {{name:<{name_w}}}  {{pos:<4}}  "
        f"{{war:>6}}  {{peak7:>6}}  {{jaws:>6}}  {{jaws_tgt:>5}}  "
        f"{{monitor:>5}}  {{stds:>5}}  {{resume:>7}}  {{verdict}}"
    )
    header_fmt = (
        f"{{0:<7}}  {{1:<{name_w}}}  {{2:<4}}  "
        f"{{3:>6}}  {{4:>6}}  {{5:>6}}  {{6:>5}}  "
        f"{{7:>5}}  {{8:>5}}  {{9:>7}}  {{10}}"
    )
    out: List[str] = []
    out.append(f"HoF Candidates — league={league} (n={len(rows)}, sorted by Resume Strength)")
    out.append(header_fmt.format(*headers))
    out.append("-" * (name_w + 75))
    for r in rows:
        out.append(fmt.format(
            id=str(r["id"]),
            name=r["name"][:name_w],
            pos=r["pos"][:4],
            war=f"{r['war']:.1f}",
            peak7=f"{r['peak7']:.1f}",
            jaws=f"{r['jaws']:.1f}",
            jaws_tgt=f"{r['jaws_tgt']:.0f}",
            monitor=f"{r['monitor']:.0f}",
            stds=f"{r['stds']:.0f}",
            resume=f"{r['resume']:.1f}%",
            verdict=r["verdict"],
        ))
    return "\n".join(out)


def _grade_one_for_batch(
    league: str, player_id: str, refresh: bool, save_md: bool,
) -> Optional[Dict[str, Any]]:
    """Run grade() for one player, save the per-player MD report (same path
    rules as single-id mode), and return the table-row dict. Returns None on
    error so the batch can continue.
    """
    try:
        bio = load_bio(league, player_id)
        name_slug = safe_filename(bio.name) if bio.name else player_id
        report_dir = REPORTS_DIR / league
        report_dir.mkdir(parents=True, exist_ok=True)
        report_path = report_dir / f"{player_id}-{name_slug}.md"

        # Batch honors the same cache as single-id: if a saved MD exists and
        # we're not refreshing, recompute the *payload* from cached CSVs (we
        # need it for the table) but don't overwrite the saved MD.
        payload = grade(league, player_id, refresh)
        row = _table_row(payload)

        if save_md and (refresh or not report_path.exists()):
            report_path.write_text(render_report(payload), encoding="utf-8")
        return row
    except SystemExit as e:
        print(f"  [{player_id}] FAILED: {e}", file=sys.stderr)
        return None
    except Exception as e:
        print(f"  [{player_id}] ERROR: {type(e).__name__}: {e}", file=sys.stderr)
        return None


def main(argv: Optional[List[str]] = None) -> int:
    # Windows consoles default to cp1252; the report contains em-dashes etc.
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1] if __doc__ else "")
    ap.add_argument("--league", required=True, help="League slug (ndl, uba, sahl, ...).")
    # One of --id / --ids / --ids-file is required; argparse can't easily
    # enforce "exactly one of these three" so we validate post-parse.
    ap.add_argument("--id", dest="player_id", help="Single player ID.")
    ap.add_argument("--ids",
                    help="Batch mode: comma-separated player IDs (e.g. 67384,15416,32523).")
    ap.add_argument("--ids-file", type=Path,
                    help="Batch mode: file with one ID per line. # starts a comment.")
    ap.add_argument("--refresh", action="store_true",
                    help="Ignore cached endpoint CSVs and saved report; recompute.")
    ap.add_argument("--json", action="store_true",
                    help="Print the result payload as JSON instead of the scorecard. "
                         "In batch mode, emits a JSON array of table rows.")
    ap.add_argument("--no-save", action="store_true",
                    help="Skip writing the per-player markdown report(s).")
    ap.add_argument("--table-out", type=Path,
                    help="Batch only: also write the sorted table to this path "
                         "(e.g. reports/hof_review/sahl_batch.md).")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    is_batch = bool(args.ids or args.ids_file)
    if not args.player_id and not is_batch:
        ap.error("Provide --id (single player) or --ids / --ids-file (batch).")
    if args.player_id and is_batch:
        ap.error("Use --id for a single player OR --ids/--ids-file for batch, not both.")

    # -------- Batch mode --------
    if is_batch:
        ids = _parse_ids(args.ids, args.ids_file)
        if not ids:
            ap.error("No player IDs found after parsing --ids/--ids-file.")
        print(f"Grading {len(ids)} player(s) in league={args.league}...", file=sys.stderr)
        rows: List[Dict[str, Any]] = []
        for i, pid in enumerate(ids, 1):
            print(f"  [{i}/{len(ids)}] {pid}", file=sys.stderr)
            row = _grade_one_for_batch(args.league, pid, args.refresh, not args.no_save)
            if row is not None:
                rows.append(row)
        if not rows:
            print("No successful grades.", file=sys.stderr)
            return 1
        if args.json:
            rows_sorted = sorted(rows, key=lambda r: -r["resume"])
            print(json.dumps(rows_sorted, default=str, indent=2))
            return 0
        table = render_table(rows, args.league)
        print(table)
        if args.table_out:
            args.table_out.parent.mkdir(parents=True, exist_ok=True)
            # Wrap in fenced code block so the column alignment survives MD rendering.
            args.table_out.write_text(
                f"# HoF Candidates — {args.league}\n\n```\n{table}\n```\n",
                encoding="utf-8",
            )
            print(f"\n(table saved to {args.table_out})", file=sys.stderr)
        return 0

    # -------- Single-player mode --------
    bio = load_bio(args.league, args.player_id)
    name_slug = safe_filename(bio.name) if bio.name else args.player_id
    report_dir = REPORTS_DIR / args.league
    report_dir.mkdir(parents=True, exist_ok=True)
    report_path = report_dir / f"{args.player_id}-{name_slug}.md"

    # Cached report: print it and exit (unless --refresh).
    if report_path.exists() and not args.refresh and not args.json:
        sys.stdout.write(f"(loading cached report: {report_path})\n\n")
        sys.stdout.write(report_path.read_text(encoding="utf-8"))
        sys.stdout.write("\n")
        return 0

    payload = grade(args.league, args.player_id, args.refresh)

    if args.json:
        print(json.dumps(payload, default=str, indent=2))
        return 0

    md = render_report(payload)
    print(md)
    if not args.no_save:
        report_path.write_text(md, encoding="utf-8")
        print(f"\n(saved to {report_path})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
