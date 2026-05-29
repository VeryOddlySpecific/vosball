#!/usr/bin/env python3
"""
awards_rank.py — Season-end awards rankings for a single league/year.

Pulls full-league stats once from /playerbatstatsv2, /playerpitchstatsv2,
and /playerfieldstatsv2 (year-only queries), joins them to /players for
bios + rookie status, then ranks candidates for:

  - MVP            (league-wide; hitters + pitchers)
  - Cy Young       (league-wide pitchers, min IP)
  - Reliever of the Year (non-starters; blends WAR + WPA + SV/HLD)
  - Rookie of the Year (hitters + pitchers, mlb_service_years < 1)
  - Gold Glove     (per position, fielding-only)
  - Silver Slugger (per position, offense filtered to players whose primary
                    position is that slot)

The score for each award is a transparent blend of WAR + a couple of
context bonuses, so a tie-break is sensible but you can always see why
someone won.

Usage:

    py awards_rank.py --league sahl                       # current sim year
    py awards_rank.py --league sahl --year 2061
    py awards_rank.py --league sahl --year 2061 --refresh # ignore cached MD
    py awards_rank.py --league sahl --year 2061 --top 5   # change list length
    py awards_rank.py --league sahl --year 2061 --json    # machine-readable

Outputs:
  - Console: one ranked table per award.
  - reports/awards/{league}/{year}.md — all tables in one file. If it
    already exists and --refresh is not passed, the file is printed back
    instead of recomputed.

Thresholds (min IP for Cy, min PA per position for SS, etc.) live in the
THRESHOLDS dict near the top. Drop config/awards_thresholds-{league}.json
to override any of them per league.

Sub-league split: when the league has divisions-{league}.json AND
teams-{league}.json configured (sahl, ndl, uba, bwb, wwoba), every award
is printed once per sub-league (AL / NL style). Players traded mid-season
count toward their FINAL team's sub-league. Leagues without those configs
(sdmb, woba, tlg) get a single combined section, same as before.
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

logger = logging.getLogger("awards_rank")

REPO_ROOT = Path(__file__).resolve().parent
CACHE_DIR = REPO_ROOT / "cache" / "awards"
REPORTS_DIR = REPO_ROOT / "reports" / "awards"
CONFIG_DIR = REPO_ROOT / "config"

# OOTP fielding position codes
POS_CODE_TO_NAME = {
    1: "P", 2: "C", 3: "1B", 4: "2B", 5: "3B", 6: "SS",
    7: "LF", 8: "CF", 9: "RF", 10: "DH",
}
# Positions eligible for Gold/Silver — DH gets a Silver but no Gold.
GG_POSITIONS = ["C", "1B", "2B", "3B", "SS", "LF", "CF", "RF"]
SS_POSITIONS = ["C", "1B", "2B", "3B", "SS", "LF", "CF", "RF", "DH"]

DEFAULT_THRESHOLDS: Dict[str, Any] = {
    # Cy Young eligibility — combined SP/RP min. Real MLB Cy voters
    # essentially require ~100+ IP for SP / 60+ IP for elite RP.
    "cy_min_ip_sp": 120,
    "cy_min_ip_rp": 50,
    # Reliever of the Year — relievers throw fewer innings; closers ~60-70,
    # elite setup ~70-80. 40 IP keeps quality middle-relief in scope.
    "rely_min_ip": 40,
    "rely_min_g": 30,
    # Gold Glove minimum innings at the position (lower for C).
    "gg_min_inn_default": 600,
    "gg_min_inn_C": 450,
    # Silver Slugger minimum PA per position (lower for C).
    "ss_min_pa_default": 350,
    "ss_min_pa_C": 250,
    # Rookie of the Year activity floor — to avoid a 5-PA cup-of-coffee
    # rookie topping the list on noise.
    "roty_min_pa": 200,
    "roty_min_ip": 40,
    # MVP-style activity floors.
    "mvp_min_pa": 500,
    "mvp_min_ip": 100,
    # Top-N for each award in the report.
    "top_n": 10,
    "top_n_per_position": 5,
}


def load_thresholds(league: str) -> Dict[str, Any]:
    cfg = json.loads(json.dumps(DEFAULT_THRESHOLDS))
    override = CONFIG_DIR / f"awards_thresholds-{league}.json"
    if override.exists():
        with override.open(encoding="utf-8") as f:
            overrides = json.load(f)
        cfg.update(overrides)
        logger.info("Applied threshold overrides from %s", override)
    return cfg


# -----------------------------------------------------------------------------
# Sub-league split — every StatsPlus league API returns a single league_id,
# but most leagues are configured AL/NL-style internally. We reconstruct the
# split from config/divisions-{league}.json + config/teams-{league}.json:
#   divisions-{league}.json: {sub_league_name: {division_name: ["City Nickname", ...]}}
#   teams-{league}.json:     {team_id: {"Name": "City", "Nickname": "Nickname", ...}}
# We need team_id -> sub_league_name.
# -----------------------------------------------------------------------------

NO_SUBLEAGUE = "__combined__"  # sentinel for leagues without a divisions config


def load_team_subleagues(league: str) -> Dict[str, str]:
    """team_id (as str) -> sub-league display name. Empty dict if either
    config is missing — caller treats this as "single league, no split"."""
    divisions_path = CONFIG_DIR / f"divisions-{league}.json"
    teams_path     = CONFIG_DIR / f"teams-{league}.json"
    if not divisions_path.exists() or not teams_path.exists():
        logger.info("No sub-league split for %s (missing divisions/teams config)", league)
        return {}
    with divisions_path.open(encoding="utf-8") as f:
        divisions = json.load(f)
    with teams_path.open(encoding="utf-8") as f:
        teams = json.load(f)

    # Build "City Nickname" -> team_id map. Match strategy is exact first,
    # then case-insensitive substring on the divisions team name as a
    # safety net (some configs include accented characters etc.).
    name_to_id: Dict[str, str] = {}
    for tid, info in teams.items():
        full = f"{(info.get('Name') or '').strip()} {(info.get('Nickname') or '').strip()}".strip()
        if full:
            name_to_id[full] = str(tid)

    out: Dict[str, str] = {}
    unmatched: List[Tuple[str, str]] = []
    for sub_league, divs in divisions.items():
        if sub_league.startswith("_"):
            continue
        # divs is {div_name: [team1, team2, ...]}
        for div_name, team_list in divs.items():
            for team_name in team_list:
                tid = name_to_id.get(team_name)
                if not tid:
                    # case-insensitive fallback
                    lo = team_name.lower()
                    tid = next((v for k, v in name_to_id.items() if k.lower() == lo), None)
                if tid:
                    out[tid] = sub_league
                else:
                    unmatched.append((sub_league, team_name))
    if unmatched:
        logger.warning("%d teams in divisions-%s.json could not be matched to teams-%s.json: %s",
                       len(unmatched), league, league,
                       ", ".join(f"{s}/{n}" for s, n in unmatched[:5]) +
                       ("..." if len(unmatched) > 5 else ""))
    return out


# -----------------------------------------------------------------------------
# Fetch — league-wide, year-scoped, cached per (league, year, endpoint).
# -----------------------------------------------------------------------------

def _auth(league: str) -> Tuple[str, Optional[str], Optional[str]]:
    base = load_league_base(league)
    token = load_token_for(league)
    cookie = None if token else load_cookie_for(base)
    if not token and not cookie:
        raise SystemExit(
            f"No auth for league '{league}'. Add a token to "
            f"config/statsplus_tokens.json or cookies to "
            f"config/statsplus_session.json."
        )
    return base, token, cookie


def _fetch_year(league: str, endpoint: str, year: int,
                split: Optional[int], refresh: bool) -> str:
    """Year-scoped league-wide fetch. Cached per (league, year, endpoint, split)."""
    cache_dir = CACHE_DIR / league
    cache_dir.mkdir(parents=True, exist_ok=True)
    suffix = f"split{split}" if split is not None else "all"
    cache_path = cache_dir / f"{year}-{endpoint}-{suffix}.csv"
    if cache_path.exists() and not refresh:
        logger.debug("cache hit: %s", cache_path)
        return cache_path.read_text(encoding="utf-8")
    base, token, cookie = _auth(league)
    params = [f"year={year}"]
    if split is not None and endpoint != "playerfieldstatsv2":
        params.append(f"split={split}")
    if token:
        params.append(f"token={token}")
    url = f"{base}/{endpoint}/?{'&'.join(params)}"
    logger.info("fetching %s", url)
    status, body = _get(url, None if token else cookie, timeout=180)
    if status == 204:
        body = ""
    elif status >= 400:
        raise SystemExit(f"HTTP {status} from {endpoint} year={year}: {body[:200]}")
    cache_path.write_text(body, encoding="utf-8")
    return body


def _fetch_players(league: str, refresh: bool) -> List[Dict[str, str]]:
    """The /players endpoint — bio + rookie/HoF status for every player.
    Cached per league (it changes slowly relative to game state)."""
    cache_dir = CACHE_DIR / league
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_path = cache_dir / "players.csv"
    if cache_path.exists() and not refresh:
        return list(csv.DictReader(cache_path.open(encoding="utf-8")))
    base, token, cookie = _auth(league)
    url = f"{base}/players/" + (f"?token={token}" if token else "")
    status, body = _get(url, None if token else cookie, timeout=180)
    if status >= 400 or not body.strip():
        raise SystemExit(f"HTTP {status} from /players: {body[:200]}")
    cache_path.write_text(body, encoding="utf-8")
    return list(csv.DictReader(io.StringIO(body)))


def _fetch_current_date(league: str) -> str:
    """/date returns the current sim date as a single-line YYYY-MM-DD."""
    base, token, cookie = _auth(league)
    url = f"{base}/date/" + (f"?token={token}" if token else "")
    status, body = _get(url, None if token else cookie, timeout=30)
    if status >= 400:
        raise SystemExit(f"HTTP {status} from /date: {body[:200]}")
    return body.strip()


def _parse_csv(text: str) -> List[Dict[str, str]]:
    if not text.strip():
        return []
    return list(csv.DictReader(io.StringIO(text)))


# -----------------------------------------------------------------------------
# Helpers
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


def _is_ml_overall(row: Dict[str, str]) -> bool:
    if _i(row.get("level_id"), -1) != 1:
        return False
    # Fielding has split_id=0; bat/pitch use 1 for overall.
    sid = _i(row.get("split_id"), -1)
    return sid in (0, 1)


# -----------------------------------------------------------------------------
# Per-player season aggregation (sum stints within the year)
# -----------------------------------------------------------------------------

@dataclass
class HitLine:
    pid: str
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
    war: float = 0.0

    @property
    def avg(self) -> float:
        return self.h / self.ab if self.ab else 0.0
    @property
    def obp(self) -> float:
        d = self.ab + self.bb
        return (self.h + self.bb) / d if d else 0.0
    @property
    def slg(self) -> float:
        if not self.ab: return 0.0
        s = self.h - self.d - self.t - self.hr
        return (s + 2*self.d + 3*self.t + 4*self.hr) / self.ab
    @property
    def ops(self) -> float:
        return self.obp + self.slg


@dataclass
class PitLine:
    pid: str
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
    war: float = 0.0
    ra9war: float = 0.0
    # Reliever-specific columns from the pitch CSV.
    hld: int = 0    # holds
    svo: int = 0    # save opportunities
    bs: int = 0     # blown saves
    wpa: float = 0.0  # win probability added (high-leverage outs count more)

    @property
    def era(self) -> float:
        return (self.er * 9.0) / self.ip if self.ip else 0.0
    @property
    def whip(self) -> float:
        return (self.bb + self.h) / self.ip if self.ip else 0.0
    @property
    def k9(self) -> float:
        return (self.so * 9.0) / self.ip if self.ip else 0.0
    @property
    def is_starter(self) -> bool:
        # Heuristic: majority of appearances are starts AND non-trivial GS.
        return self.gs >= 5 and self.gs >= self.g * 0.5
    @property
    def sv_pct(self) -> float:
        """Save conversion rate — 0..1. Returns 0 if no opportunities."""
        return self.sv / self.svo if self.svo else 0.0


def aggregate_hitters(rows: List[Dict[str, str]]) -> Dict[str, HitLine]:
    out: Dict[str, HitLine] = {}
    for r in rows:
        if not _is_ml_overall(r):
            continue
        pid = (r.get("player_id") or "").strip()
        if not pid:
            continue
        h = out.setdefault(pid, HitLine(pid=pid))
        h.g   += _i(r.get("g"))
        h.pa  += _i(r.get("pa"))
        h.ab  += _i(r.get("ab"))
        h.h   += _i(r.get("h"))
        h.d   += _i(r.get("d"))
        h.t   += _i(r.get("t"))
        h.hr  += _i(r.get("hr"))
        h.r   += _i(r.get("r"))
        h.rbi += _i(r.get("rbi"))
        h.bb  += _i(r.get("bb"))
        h.k   += _i(r.get("k"))
        h.sb  += _i(r.get("sb"))
        h.war += _f(r.get("war"))
    return out


def final_team_per_player(
    bat_rows: List[Dict[str, str]],
    pitch_rows: List[Dict[str, str]],
) -> Dict[str, str]:
    """Returns {player_id: team_id_of_final_stint}. For traded players we
    pick the row with the highest `stint`; ties (rare) take the row with
    the most playing time. Rows from both bat and pitch are considered so
    a Shohei-style two-way player gets the right final team.
    """
    # Build per-player list of (stint, weight, team_id) tuples across both
    # endpoints, keeping ML-only.
    by_pid: Dict[str, List[Tuple[int, float, str]]] = {}
    for r in bat_rows:
        if not _is_ml_overall(r):
            continue
        pid = (r.get("player_id") or "").strip()
        tid = (r.get("team_id") or "").strip()
        if not pid or not tid:
            continue
        by_pid.setdefault(pid, []).append(
            (_i(r.get("stint")), _f(r.get("pa")) or _f(r.get("g")), tid)
        )
    for r in pitch_rows:
        if not _is_ml_overall(r):
            continue
        pid = (r.get("player_id") or "").strip()
        tid = (r.get("team_id") or "").strip()
        if not pid or not tid:
            continue
        by_pid.setdefault(pid, []).append(
            (_i(r.get("stint")), _f(r.get("ip")) or _f(r.get("g")), tid)
        )
    out: Dict[str, str] = {}
    for pid, entries in by_pid.items():
        # Pick max stint, tiebreak on weight desc.
        entries.sort(key=lambda e: (-e[0], -e[1]))
        out[pid] = entries[0][2]
    return out


def aggregate_pitchers(rows: List[Dict[str, str]]) -> Dict[str, PitLine]:
    out: Dict[str, PitLine] = {}
    for r in rows:
        if not _is_ml_overall(r):
            continue
        pid = (r.get("player_id") or "").strip()
        if not pid:
            continue
        p = out.setdefault(pid, PitLine(pid=pid))
        p.g  += _i(r.get("g"))
        p.gs += _i(r.get("gs"))
        p.ip += _f(r.get("ip"))
        p.w  += _i(r.get("w"))
        p.l  += _i(r.get("l"))
        p.sv += _i(r.get("s"))
        p.so += _i(r.get("k"))
        p.bb += _i(r.get("bb"))
        p.h  += _i(r.get("ha"))
        p.er += _i(r.get("er"))
        p.war += _f(r.get("war"))
        p.ra9war += _f(r.get("ra9war"))
        p.hld += _i(r.get("hld"))
        p.svo += _i(r.get("svo"))
        p.bs  += _i(r.get("bs"))
        p.wpa += _f(r.get("wpa"))
    return out


# -----------------------------------------------------------------------------
# Primary-position map (from this year's fielding stats)
# -----------------------------------------------------------------------------

@dataclass
class FieldLine:
    pid: str
    pos: int
    g: int = 0
    gs: int = 0
    ip: float = 0.0
    zr: float = 0.0
    framing: float = 0.0
    arm: float = 0.0


def aggregate_fielding(rows: List[Dict[str, str]]) -> Dict[Tuple[str, int], FieldLine]:
    """Key by (player_id, position). Sum across stints."""
    out: Dict[Tuple[str, int], FieldLine] = {}
    for r in rows:
        if _i(r.get("level_id"), -1) != 1:
            continue
        pid = (r.get("player_id") or "").strip()
        pos = _i(r.get("position"))
        if not pid or pos <= 0:
            continue
        key = (pid, pos)
        fl = out.setdefault(key, FieldLine(pid=pid, pos=pos))
        fl.g  += _i(r.get("g"))
        fl.gs += _i(r.get("gs"))
        fl.ip += _f(r.get("ip"))
        fl.zr += _f(r.get("zr"))
        fl.framing += _f(r.get("framing"))
        fl.arm += _f(r.get("arm"))
    return out


def primary_positions(field_lines: Dict[Tuple[str, int], FieldLine]
                     ) -> Dict[str, str]:
    """Map player_id -> primary position name based on innings this year.
    Excludes pitcher from primary unless that's the only position recorded
    (i.e. so a position player who occasionally pitches still ranks at
    their bat-position for Silver Slugger)."""
    by_player: Dict[str, Dict[int, float]] = {}
    for (pid, pos), fl in field_lines.items():
        by_player.setdefault(pid, {})[pos] = max(fl.ip, fl.g)
    out: Dict[str, str] = {}
    for pid, by_pos in by_player.items():
        non_p = {k: v for k, v in by_pos.items() if k != 1}
        src = non_p if non_p else by_pos
        best = max(src, key=src.get)
        out[pid] = POS_CODE_TO_NAME.get(best, "")
    return out


# -----------------------------------------------------------------------------
# Bio + rookie map from /players
# -----------------------------------------------------------------------------

@dataclass
class Bio:
    pid: str
    name: str = ""
    team_id: str = ""
    pos_code: str = ""
    is_rookie: bool = False
    inducted: bool = False


def build_bios(players: List[Dict[str, str]]) -> Dict[str, Bio]:
    out: Dict[str, Bio] = {}
    for r in players:
        pid = (r.get("ID") or "").strip()
        if not pid:
            continue
        # Rookie: mlb_service_years < 1 (per user's spec).
        svc = r.get("mlb_service_years", "")
        is_rookie = svc.isdigit() and int(svc) < 1
        out[pid] = Bio(
            pid=pid,
            name=f"{(r.get('First Name') or '').strip()} "
                 f"{(r.get('Last Name') or '').strip()}".strip(),
            team_id=(r.get("Team ID") or "").strip(),
            pos_code=(r.get("Pos") or "").strip(),
            is_rookie=is_rookie,
            inducted=(r.get("inducted") or "0") == "1",
        )
    return out


# -----------------------------------------------------------------------------
# Award scoring
# -----------------------------------------------------------------------------

def score_mvp(hitters: Dict[str, HitLine], pitchers: Dict[str, PitLine],
              thr: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Combined player score: WAR is primary, with a small offensive-context
    bonus so two players within ~0.1 WAR break on production. Includes
    pitchers — a dominant pitcher MVP is rare but should rank if WAR says so.
    """
    rows: List[Dict[str, Any]] = []
    all_pids = set(hitters) | set(pitchers)
    for pid in all_pids:
        h = hitters.get(pid)
        p = pitchers.get(pid)
        hit_war = h.war if h else 0.0
        pit_war = p.war if p else 0.0
        total_war = hit_war + pit_war

        # Activity floor: needs meaningful playing time on at least one side.
        if (h and h.pa >= thr["mvp_min_pa"]) or (p and p.ip >= thr["mvp_min_ip"]):
            pass
        else:
            continue

        # Context bonus (tie-break, capped). For hitters: OPS over .800.
        # For pitchers: sub-3.50 ERA over min IP. Each maxes at +0.5 score.
        bonus = 0.0
        if h and h.pa >= thr["mvp_min_pa"]:
            bonus += min(0.5, max(0.0, (h.ops - 0.800) * 1.5))
        if p and p.ip >= thr["mvp_min_ip"]:
            bonus += min(0.5, max(0.0, (3.80 - p.era) * 0.25))
        rows.append({
            "pid": pid,
            "score": total_war + bonus,
            "war": total_war,
            "hit_war": hit_war,
            "pit_war": pit_war,
            "hit_line": _hit_line_dict(h) if h else None,
            "pit_line": _pit_line_dict(p) if p else None,
        })
    rows.sort(key=lambda r: -r["score"])
    return rows


def score_cy_young(pitchers: Dict[str, PitLine], thr: Dict[str, Any]
                  ) -> List[Dict[str, Any]]:
    """Pitchers only. Min IP gate, then rank by WAR + tiny bonuses for
    ERA & K/9 to break ties cleanly."""
    rows: List[Dict[str, Any]] = []
    for pid, p in pitchers.items():
        min_ip = thr["cy_min_ip_sp"] if p.is_starter else thr["cy_min_ip_rp"]
        if p.ip < min_ip:
            continue
        era_bonus = min(0.5, max(0.0, (3.80 - p.era) * 0.25))
        k_bonus   = min(0.3, max(0.0, (p.k9 - 8.0) * 0.05))
        rows.append({
            "pid": pid,
            "score": p.war + era_bonus + k_bonus,
            "war":   p.war,
            "ra9war": p.ra9war,
            "pit_line": _pit_line_dict(p),
            "role": "SP" if p.is_starter else "RP",
        })
    rows.sort(key=lambda r: -r["score"])
    return rows


def score_reliever_of_year(pitchers: Dict[str, PitLine], thr: Dict[str, Any]
                          ) -> List[Dict[str, Any]]:
    """Relievers only. Blend WAR + WPA (high-leverage outs counted more) plus
    a small closer/setup-role bonus. WPA matters because a reliever's value
    is concentrated in high-leverage innings, which WAR alone undervalues.
    """
    rows: List[Dict[str, Any]] = []
    for pid, p in pitchers.items():
        if p.is_starter:
            continue
        if p.ip < thr["rely_min_ip"] or p.g < thr["rely_min_g"]:
            continue
        # Closers and elite setup men: small score nudge based on SV+HLD,
        # capped so it can't overtake a more dominant non-closer.
        leverage_bonus = min(0.5, (p.sv + p.hld) / 80.0)
        # ERA gate adds a small tiebreaker (sub-3.00 elite, sub-2.50 dominant).
        era_bonus = min(0.5, max(0.0, (3.20 - p.era) * 0.25))
        # Penalize blown saves only mildly (already reflected in ERA/WPA).
        bs_penalty = min(0.3, p.bs * 0.05)
        score = p.war + p.wpa + leverage_bonus + era_bonus - bs_penalty
        rows.append({
            "pid": pid,
            "score": score,
            "war": p.war,
            "wpa": p.wpa,
            "pit_line": _pit_line_dict(p),
        })
    rows.sort(key=lambda r: -r["score"])
    return rows


def score_roty(hitters: Dict[str, HitLine], pitchers: Dict[str, PitLine],
               bios: Dict[str, Bio], thr: Dict[str, Any]) -> List[Dict[str, Any]]:
    """MVP-style score, but restricted to rookies (mlb_service_years < 1)."""
    rows: List[Dict[str, Any]] = []
    for pid, bio in bios.items():
        if not bio.is_rookie:
            continue
        h = hitters.get(pid)
        p = pitchers.get(pid)
        if h and h.pa >= thr["roty_min_pa"]:
            ok = True
        elif p and p.ip >= thr["roty_min_ip"]:
            ok = True
        else:
            continue
        hit_war = h.war if h else 0.0
        pit_war = p.war if p else 0.0
        rows.append({
            "pid": pid,
            "score": hit_war + pit_war,
            "war": hit_war + pit_war,
            "hit_war": hit_war,
            "pit_war": pit_war,
            "hit_line": _hit_line_dict(h) if h else None,
            "pit_line": _pit_line_dict(p) if p else None,
        })
    rows.sort(key=lambda r: -r["score"])
    return rows


def score_gold_glove(field_lines: Dict[Tuple[str, int], FieldLine],
                     thr: Dict[str, Any]) -> Dict[str, List[Dict[str, Any]]]:
    """Per position. Score = ZR (zone runs above avg) + framing for C, + arm
    for C/CF/RF. Min innings gate per position. Returns {pos_name: rows}."""
    out: Dict[str, List[Dict[str, Any]]] = {p: [] for p in GG_POSITIONS}
    for (pid, pos), fl in field_lines.items():
        name = POS_CODE_TO_NAME.get(pos)
        if name not in out:
            continue
        min_inn = thr.get(f"gg_min_inn_{name}", thr["gg_min_inn_default"])
        if fl.ip < min_inn:
            continue
        score = fl.zr
        # Catcher: framing is the dominant skill; arm matters too.
        if name == "C":
            score = fl.framing + fl.arm + fl.zr
        # Corner OFs and CF: arm carries some signal
        elif name in ("CF", "RF", "LF"):
            score += fl.arm * 0.5
        out[name].append({
            "pid": pid,
            "score": score,
            "ip": fl.ip,
            "g": fl.g,
            "zr": fl.zr,
            "framing": fl.framing,
            "arm": fl.arm,
        })
    for rows in out.values():
        rows.sort(key=lambda r: -r["score"])
    return out


def score_silver_slugger(hitters: Dict[str, HitLine],
                         primary_pos: Dict[str, str],
                         thr: Dict[str, Any]) -> Dict[str, List[Dict[str, Any]]]:
    """Per position. Score = a wOBA-ish OPS-based metric. Filter to hitters
    whose primary position this year was that slot. Min PA gate per pos.
    """
    out: Dict[str, List[Dict[str, Any]]] = {p: [] for p in SS_POSITIONS}
    for pid, h in hitters.items():
        pos = primary_pos.get(pid, "")
        # DH catchall: if player has no fielding rows but DH'd, primary will
        # be empty — give them the DH bucket.
        if not pos and h.pa >= 200 and h.g and h.pa // max(1, h.g) >= 3:
            pos = "DH"
        if pos not in out:
            continue
        min_pa = thr.get(f"ss_min_pa_{pos}", thr["ss_min_pa_default"])
        if h.pa < min_pa:
            continue
        # Simple offensive score: weighted OBP*1.7 + SLG (rough wOBA proxy
        # scale, comparable across positions). Volume modestly rewarded.
        score = (h.obp * 1.7 + h.slg) + (h.pa / 700.0) * 0.05
        out[pos].append({
            "pid": pid,
            "score": score,
            "hit_line": _hit_line_dict(h),
        })
    for rows in out.values():
        rows.sort(key=lambda r: -r["score"])
    return out


# -----------------------------------------------------------------------------
# Render helpers
# -----------------------------------------------------------------------------

def _hit_line_dict(h: Optional[HitLine]) -> Optional[Dict[str, Any]]:
    if not h:
        return None
    return {
        "g": h.g, "pa": h.pa, "ab": h.ab, "h": h.h, "hr": h.hr,
        "r": h.r, "rbi": h.rbi, "bb": h.bb, "sb": h.sb,
        "avg": h.avg, "obp": h.obp, "slg": h.slg, "ops": h.ops,
        "war": h.war,
    }


def _pit_line_dict(p: Optional[PitLine]) -> Optional[Dict[str, Any]]:
    if not p:
        return None
    return {
        "g": p.g, "gs": p.gs, "ip": p.ip, "w": p.w, "l": p.l,
        "sv": p.sv, "so": p.so, "bb": p.bb, "era": p.era,
        "whip": p.whip, "war": p.war, "ra9war": p.ra9war,
        "hld": p.hld, "svo": p.svo, "bs": p.bs, "wpa": p.wpa,
        "sv_pct": p.sv_pct,
    }


def _fmt_slash(h: Optional[Dict[str, Any]]) -> str:
    if not h or not h.get("ab"):
        return "—"
    return (f".{int(round(h['avg']*1000)):03d}/"
            f".{int(round(h['obp']*1000)):03d}/"
            f".{int(round(h['slg']*1000)):03d}")


def _name(bios: Dict[str, Bio], pid: str) -> str:
    b = bios.get(pid)
    return (b.name if b and b.name else f"#{pid}")[:24]


def render_mvp_like(title: str, rows: List[Dict[str, Any]],
                    bios: Dict[str, Bio], top_n: int) -> str:
    out = [f"## {title}"]
    out.append("")
    out.append(f"{'Rk':>3}  {'ID':<6}  {'Name':<24}  {'WAR':>5}  "
               f"{'Slash':<17}  {'HR':>3}  {'RBI':>4}  {'OPS':>5}  "
               f"{'P:IP':>5}  {'ERA':>5}  {'Score':>6}")
    out.append("-" * 99)
    for i, r in enumerate(rows[:top_n], 1):
        h = r.get("hit_line") or {}
        p = r.get("pit_line") or {}
        out.append(
            f"{i:>3}  {r['pid']:<6}  {_name(bios, r['pid']):<24}  "
            f"{r['war']:>5.1f}  "
            f"{_fmt_slash(h):<17}  "
            f"{h.get('hr',0):>3}  {h.get('rbi',0):>4}  "
            f"{h.get('ops',0):>5.3f}  "
            f"{p.get('ip',0):>5.1f}  {p.get('era',0) if p.get('ip',0) else 0:>5.2f}  "
            f"{r['score']:>6.2f}"
        )
    out.append("")
    return "\n".join(out)


def render_cy(rows: List[Dict[str, Any]], bios: Dict[str, Bio], top_n: int) -> str:
    out = ["## Cy Young"]
    out.append("")
    out.append(f"{'Rk':>3}  {'ID':<6}  {'Name':<24}  {'Role':<4}  "
               f"{'WAR':>5}  {'W-L':>5}  {'SV':>3}  {'IP':>6}  "
               f"{'ERA':>5}  {'WHIP':>5}  {'K':>4}  {'Score':>6}")
    out.append("-" * 99)
    for i, r in enumerate(rows[:top_n], 1):
        p = r["pit_line"]
        out.append(
            f"{i:>3}  {r['pid']:<6}  {_name(bios, r['pid']):<24}  "
            f"{r['role']:<4}  "
            f"{r['war']:>5.1f}  "
            f"{p['w']:>2}-{p['l']:<2}  "
            f"{p['sv']:>3}  {p['ip']:>6.1f}  "
            f"{p['era']:>5.2f}  {p['whip']:>5.2f}  {p['so']:>4}  "
            f"{r['score']:>6.2f}"
        )
    out.append("")
    return "\n".join(out)


def render_reliever(rows: List[Dict[str, Any]], bios: Dict[str, Bio],
                    top_n: int) -> str:
    out = ["## Reliever of the Year"]
    out.append("")
    out.append(f"{'Rk':>3}  {'ID':<6}  {'Name':<24}  "
               f"{'WAR':>4}  {'WPA':>5}  {'G':>3}  {'IP':>5}  "
               f"{'W-L':>5}  {'SV':>3}  {'HLD':>4}  "
               f"{'ERA':>5}  {'WHIP':>5}  {'K':>4}  {'Score':>6}")
    out.append("-" * 105)
    for i, r in enumerate(rows[:top_n], 1):
        p = r["pit_line"]
        out.append(
            f"{i:>3}  {r['pid']:<6}  {_name(bios, r['pid']):<24}  "
            f"{r['war']:>4.1f}  {r['wpa']:>5.2f}  "
            f"{p['g']:>3}  {p['ip']:>5.1f}  "
            f"{p['w']:>2}-{p['l']:<2}  "
            f"{p['sv']:>3}  {p['hld']:>4}  "
            f"{p['era']:>5.2f}  {p['whip']:>5.2f}  {p['so']:>4}  "
            f"{r['score']:>6.2f}"
        )
    out.append("")
    return "\n".join(out)


def render_gold_glove(by_pos: Dict[str, List[Dict[str, Any]]],
                      bios: Dict[str, Bio], top_n: int) -> str:
    out = ["## Gold Glove (by position)"]
    out.append("")
    for pos in GG_POSITIONS:
        rows = by_pos.get(pos, [])
        if not rows:
            out.append(f"### {pos}\n_(no qualifiers)_\n")
            continue
        out.append(f"### {pos}")
        out.append(f"{'Rk':>3}  {'ID':<6}  {'Name':<24}  {'G':>4}  "
                   f"{'IP':>6}  {'ZR':>6}  {'Frm':>6}  {'Arm':>6}  {'Score':>6}")
        out.append("-" * 80)
        for i, r in enumerate(rows[:top_n], 1):
            out.append(
                f"{i:>3}  {r['pid']:<6}  {_name(bios, r['pid']):<24}  "
                f"{r['g']:>4}  {r['ip']:>6.1f}  "
                f"{r['zr']:>6.2f}  {r['framing']:>6.2f}  {r['arm']:>6.2f}  "
                f"{r['score']:>6.2f}"
            )
        out.append("")
    return "\n".join(out)


def render_silver_slugger(by_pos: Dict[str, List[Dict[str, Any]]],
                          bios: Dict[str, Bio], top_n: int) -> str:
    out = ["## Silver Slugger (by position)"]
    out.append("")
    for pos in SS_POSITIONS:
        rows = by_pos.get(pos, [])
        if not rows:
            out.append(f"### {pos}\n_(no qualifiers)_\n")
            continue
        out.append(f"### {pos}")
        out.append(f"{'Rk':>3}  {'ID':<6}  {'Name':<24}  {'PA':>4}  "
                   f"{'Slash':<17}  {'HR':>3}  {'RBI':>4}  {'OPS':>5}  {'Score':>6}")
        out.append("-" * 88)
        for i, r in enumerate(rows[:top_n], 1):
            h = r["hit_line"]
            out.append(
                f"{i:>3}  {r['pid']:<6}  {_name(bios, r['pid']):<24}  "
                f"{h['pa']:>4}  {_fmt_slash(h):<17}  "
                f"{h['hr']:>3}  {h['rbi']:>4}  {h['ops']:>5.3f}  "
                f"{r['score']:>6.3f}"
            )
        out.append("")
    return "\n".join(out)


# -----------------------------------------------------------------------------
# Top-level orchestration
# -----------------------------------------------------------------------------

def _tag_subleague(rows: List[Dict[str, Any]],
                   final_team: Dict[str, str],
                   team_to_sub: Dict[str, str]) -> None:
    """Mutates each row in-place, adding a 'sub_league' key.
    Players with no team mapping (or leagues with no split) get NO_SUBLEAGUE."""
    for r in rows:
        tid = final_team.get(r["pid"], "")
        r["sub_league"] = team_to_sub.get(tid, NO_SUBLEAGUE) if team_to_sub else NO_SUBLEAGUE


def compute(league: str, year: int, refresh: bool, thr: Dict[str, Any]
           ) -> Dict[str, Any]:
    logger.info("fetching season %s for %s", year, league)
    bat_csv   = _fetch_year(league, "playerbatstatsv2",   year, split=1, refresh=refresh)
    pitch_csv = _fetch_year(league, "playerpitchstatsv2", year, split=1, refresh=refresh)
    field_csv = _fetch_year(league, "playerfieldstatsv2", year, split=None, refresh=refresh)
    players   = _fetch_players(league, refresh)

    bat_rows   = _parse_csv(bat_csv)
    pitch_rows = _parse_csv(pitch_csv)
    field_rows = _parse_csv(field_csv)

    hitters  = aggregate_hitters(bat_rows)
    pitchers = aggregate_pitchers(pitch_rows)
    field_lines = aggregate_fielding(field_rows)
    bios = build_bios(players)
    primary = primary_positions(field_lines)

    # Sub-league plumbing: final-team-of-season per player, joined to
    # divisions/teams config. Empty team_to_sub means "no split" (single
    # combined section, current behavior).
    team_to_sub = load_team_subleagues(league)
    final_team  = final_team_per_player(bat_rows, pitch_rows)

    mvp  = score_mvp(hitters, pitchers, thr)
    cy   = score_cy_young(pitchers, thr)
    rely = score_reliever_of_year(pitchers, thr)
    roty = score_roty(hitters, pitchers, bios, thr)
    gg   = score_gold_glove(field_lines, thr)
    ss   = score_silver_slugger(hitters, primary, thr)

    _tag_subleague(mvp,  final_team, team_to_sub)
    _tag_subleague(cy,   final_team, team_to_sub)
    _tag_subleague(rely, final_team, team_to_sub)
    _tag_subleague(roty, final_team, team_to_sub)
    for rows in gg.values(): _tag_subleague(rows, final_team, team_to_sub)
    for rows in ss.values(): _tag_subleague(rows, final_team, team_to_sub)

    # Preserve the canonical sub-league order from the divisions file
    # (AL before NL, etc.). Falls back to alphabetical if no config.
    sub_leagues: List[str] = []
    divisions_path = CONFIG_DIR / f"divisions-{league}.json"
    if divisions_path.exists():
        with divisions_path.open(encoding="utf-8") as f:
            div_data = json.load(f)
        sub_leagues = [k for k in div_data.keys() if not k.startswith("_")]
    if not sub_leagues:
        sub_leagues = [NO_SUBLEAGUE]

    return {
        "league": league, "year": year,
        "mvp": mvp, "cy": cy, "rely": rely, "roty": roty,
        "gg": gg, "ss": ss,
        "bios": bios,
        "sub_leagues": sub_leagues,
    }


def _filter_sub(rows: List[Dict[str, Any]], sub: str) -> List[Dict[str, Any]]:
    """Keep only rows tagged with this sub-league. The sentinel NO_SUBLEAGUE
    means "no split configured" — return everything in that case."""
    if sub == NO_SUBLEAGUE:
        return rows
    return [r for r in rows if r.get("sub_league") == sub]


def _filter_sub_by_pos(by_pos: Dict[str, List[Dict[str, Any]]], sub: str
                       ) -> Dict[str, List[Dict[str, Any]]]:
    return {pos: _filter_sub(rows, sub) for pos, rows in by_pos.items()}


def render_report(payload: Dict[str, Any], thr: Dict[str, Any]) -> str:
    bios = payload["bios"]
    n  = thr["top_n"]
    np_ = thr["top_n_per_position"]
    sub_leagues = payload["sub_leagues"]
    has_split = sub_leagues != [NO_SUBLEAGUE]

    parts = [f"# {payload['league'].upper()} Awards — {payload['year']}", ""]
    if has_split:
        parts.append(f"_Split by sub-league: {' · '.join(sub_leagues)}_")
        parts.append("")

    for sub in sub_leagues:
        if has_split:
            parts.append(f"# {sub}\n")
        parts.append(render_mvp_like("MVP", _filter_sub(payload["mvp"], sub), bios, n))
        parts.append(render_cy(_filter_sub(payload["cy"], sub), bios, n))
        parts.append(render_reliever(_filter_sub(payload["rely"], sub), bios, n))
        parts.append(render_mvp_like("Rookie of the Year",
                                     _filter_sub(payload["roty"], sub), bios, n))
        parts.append(render_gold_glove(_filter_sub_by_pos(payload["gg"], sub), bios, np_))
        parts.append(render_silver_slugger(_filter_sub_by_pos(payload["ss"], sub), bios, np_))
        if has_split:
            parts.append("")
    parts.append("---\n_Auto-generated by awards_rank.py_")
    return "\n".join(parts)


# -----------------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------------

def main(argv: Optional[List[str]] = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1] if __doc__ else "")
    ap.add_argument("--league", required=True, help="League slug (ndl, uba, sahl, ...).")
    ap.add_argument("--year", type=int,
                    help="Season year (default: current sim year via /date).")
    ap.add_argument("--top", type=int,
                    help="Top-N per award (overrides default in thresholds).")
    ap.add_argument("--refresh", action="store_true",
                    help="Ignore cached endpoint CSVs and saved report; recompute.")
    ap.add_argument("--json", action="store_true",
                    help="Emit JSON payload instead of the formatted report.")
    ap.add_argument("--no-save", action="store_true",
                    help="Skip writing the markdown report.")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    thr = load_thresholds(args.league)
    if args.top:
        thr["top_n"] = args.top
        thr["top_n_per_position"] = max(3, args.top // 2)

    # Resolve year: explicit or current sim date.
    if args.year:
        year = args.year
    else:
        date_str = _fetch_current_date(args.league)
        m = re.match(r"(\d{4})", date_str)
        if not m:
            raise SystemExit(f"Could not parse current date: {date_str!r}")
        year = int(m.group(1))
        logger.info("Defaulting to current sim year: %d", year)

    report_dir = REPORTS_DIR / args.league
    report_dir.mkdir(parents=True, exist_ok=True)
    report_path = report_dir / f"{year}.md"

    # Cached report path (only if not JSON and not refresh).
    if report_path.exists() and not args.refresh and not args.json:
        sys.stdout.write(f"(loading cached report: {report_path})\n\n")
        sys.stdout.write(report_path.read_text(encoding="utf-8"))
        sys.stdout.write("\n")
        return 0

    payload = compute(args.league, year, args.refresh, thr)

    if args.json:
        # Bios -> dicts for JSON; trim heavy nested objects optional.
        out = {k: v for k, v in payload.items() if k != "bios"}
        out["bios"] = {pid: b.__dict__ for pid, b in payload["bios"].items()}
        print(json.dumps(out, default=str, indent=2))
        return 0

    md = render_report(payload, thr)
    print(md)
    if not args.no_save:
        report_path.write_text(md, encoding="utf-8")
        print(f"\n(saved to {report_path})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
