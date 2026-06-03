#!/usr/bin/env python3
"""
league_provision.py — Stand up a brand-new league's config files straight from
the StatsPlus API (ticket 0003, Phase 4).

Given a league slug + base API URL + token, this:

  1. GET ``{url}/teams``    (CSV)  → ``teams-{slug}.json``
  2. GET ``{url}/ballparks`` (JSON) → ``{slug}-park-factors.json`` (raw factors
     + neutral tool_adjustments) **and** ``{slug}_orgs.json`` (the ML org list)
  3. writes the registry entries (url, token, scalar settings, an ``ML`` league-
     IDs stub) via :class:`league_registry.LeagueRegistry`.

``data/PlayerData-{slug}.csv`` is **not** pulled here — that's the existing
two-step /ratings fetch (``fetch_player_data`` / ``webapp/fetch``), kicked off
by the UI after provisioning writes the url + token this fetch needs.

Unlike /ratings, /teams and /ballparks are single-shot GETs (no polling). The
network layer is injected (``fetch_text`` / ``fetch_json``) so the orchestration
is fully unit-testable offline.

Park-factor decision (ticket 0003): all ``tool_adjustments`` are written neutral
(1.0). The file is structurally identical to the hand-authored ones — same keys,
same shape — so depth_chart's name→code map works immediately while scoring is
unaffected until the SAHL conversion formulas are ported (Phase 5).
"""

from __future__ import annotations
# --- repo-root + core/ path bootstrap ---
import os as _os, sys as _sys
_ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
for _p in (_ROOT, _os.path.join(_ROOT, "core")):
    if _p not in _sys.path:
        _sys.path.insert(0, _p)
# --- end bootstrap ---

import csv
import io
import json
from typing import Any, Callable, Dict, List, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from league_registry import LeagueRegistry, LeagueConfig, RegistryError

# statsplus serves a different (HTML) response to urllib's default UA in some
# cases; present a browser-ish UA like the other fetch tools do.
_USER_AGENT = "Mozilla/5.0 (compatible; vosball-provision/1.0; +ratings-tooling)"

# Tool ratings left at a neutral (1.0) park multiplier. The batting tools are
# computed from the raw factors (the deterministic "SAHL conversion formulas");
# defense / baserunning / pitcher were hand-tuned per park in the original files
# with no formula, so we leave them neutral (ticket 0003 Phase 5 decision).
# Replicating the key sets means a generated file is consumed byte-for-byte the
# same way a hand-authored one is.
_NEUTRAL_TOOLS: Dict[str, List[str]] = {
    "defense": ["OFR", "IFR", "OFE", "OFA", "IFE", "IFA", "TDP", "CArm", "CBlk", "CFrm"],
    "baserunning": ["Speed", "Run", "StealAbi", "StlRt"],
    "pitcher_ability": ["Stuff", "Movement", "Control", "HR_Avoid"],
}


def _r(x: float) -> float:
    return round(x, 3)


def compute_batting_adjustments(raw: Dict[str, float]) -> Dict[str, float]:
    """The deterministic SAHL raw→tool conversion for the batting tools.
    Verified to reproduce the hand-authored files exactly:
      Pow = 1 + (hr_overall - 1)·0.6
      Gap = 1 + (doubles - 1)·0.5 + (triples - 1)·0.3
      Eye = 1 + (avg_overall - 1)·0.3
      Ks  = 1 + (avg_overall - 1)·(-0.2)
    """
    avg, d, t = raw["avg_overall"], raw["doubles"], raw["triples"]
    hr = raw["hr_overall"]
    return {
        "Pow": _r(1.0 + (hr - 1.0) * 0.6),
        "Gap": _r(1.0 + (d - 1.0) * 0.5 + (t - 1.0) * 0.3),
        "Eye": _r(1.0 + (avg - 1.0) * 0.3),
        "Ks": _r(1.0 + (avg - 1.0) * -0.2),
    }


def compute_handedness_pow(raw: Dict[str, float]) -> Dict[str, Dict[str, float]]:
    """Per-hand Power multipliers from the handed HR factors (same ·0.6)."""
    return {
        "RHB": {"Pow": _r(1.0 + (raw["hr_rhb"] - 1.0) * 0.6)},
        "LHB": {"Pow": _r(1.0 + (raw["hr_lhb"] - 1.0) * 0.6)},
    }


def build_park_profile(raw: Dict[str, float], bp: Dict[str, Any]) -> Dict[str, Any]:
    """A human-readable park summary derived from the raw factors + the stadium
    facts /ballparks provides. Heuristic classification — purely descriptive,
    not consumed by scoring."""
    avg, d, t, hr = raw["avg_overall"], raw["doubles"], raw["triples"], raw["hr_overall"]

    def pct(x: float) -> str:
        return f"{(x - 1.0) * 100:+.1f}%"

    chars: List[str] = []
    if abs(hr - 1.0) >= 0.02:
        chars.append(f"Home runs {pct(hr)}")
    if abs(d - 1.0) >= 0.02:
        chars.append(f"Doubles {pct(d)}")
    if abs(t - 1.0) >= 0.02:
        chars.append(f"Triples {pct(t)}")
    if abs(avg - 1.0) >= 0.01:
        chars.append(f"Batting average {pct(avg)}")

    if hr >= 1.04:
        ptype = "hr_park"
    elif hr <= 0.97 and (d >= 1.03 or t >= 1.03):
        ptype = "gap_park"
    elif avg <= 0.98 and hr <= 0.98:
        ptype = "pitchers_park"
    elif avg >= 1.02 or d >= 1.04 or t >= 1.04:
        ptype = "hitters_park"
    else:
        ptype = "neutral_park"

    return {
        "_comment": "Auto-generated from /ballparks raw factors.",
        "type": ptype,
        "description": f"Auto-classified as {ptype.replace('_', ' ')} from raw "
                       "park factors; review and refine as needed.",
        "key_characteristics": chars or ["Near league-average in all factors"],
        "capacity": bp.get("capacity"),
        "stadium_type": bp.get("stadium_type"),
        "surface": bp.get("surface"),
    }


class ProvisionError(RuntimeError):
    """Raised when a provisioning step fails (network, parse, or validation)."""


# --- pure parsers / builders (UI-free, unit-testable) -----------------------

def parse_teams_csv(text: str) -> Dict[str, Dict[str, Any]]:
    """``/teams`` CSV (``ID,Name,Nickname,Parent Team ID``) → the
    ``teams-{slug}.json`` shape ``{ "<id>": {Name, Nickname, Parent} }``.

    Reads by header name (per the API docs: never rely on column order)."""
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise ProvisionError("/teams returned no header row.")
    norm = {(h or "").strip().lower(): h for h in reader.fieldnames}

    def col(*candidates: str) -> Optional[str]:
        for c in candidates:
            if c in norm:
                return norm[c]
        return None

    id_c = col("id")
    name_c = col("name")
    nick_c = col("nickname")
    parent_c = col("parent team id", "parent team", "parent")
    if not id_c or not name_c:
        raise ProvisionError(
            f"/teams missing expected columns; saw {reader.fieldnames!r}.")

    out: Dict[str, Dict[str, Any]] = {}
    for row in reader:
        tid = (row.get(id_c) or "").strip()
        if not tid:
            continue
        try:
            parent = int((row.get(parent_c) or "0").strip() or 0) if parent_c else 0
        except ValueError:
            parent = 0
        out[tid] = {
            "Name": (row.get(name_c) or "").strip(),
            "Nickname": (row.get(nick_c) or "").strip() if nick_c else "",
            "Parent": parent,
        }
    if not out:
        raise ProvisionError("/teams returned no team rows.")
    return out


def _build_team_entry(bp: Dict[str, Any]) -> Dict[str, Any]:
    """Build one park-factors entry from a /ballparks team object: real batting
    + handedness multipliers from the SAHL formulas; defense/baserunning/pitcher
    left neutral (no formula exists for them)."""
    def f(key: str, default: float = 1.0) -> float:
        v = bp.get(key)
        try:
            return float(v) if v is not None else default
        except (TypeError, ValueError):
            return default

    raw = {
        "avg_overall": f("avg"), "avg_rhb": f("avg_r"), "avg_lhb": f("avg_l"),
        "doubles": f("d"), "triples": f("t"),
        "hr_overall": f("hr"), "hr_rhb": f("hr_r"), "hr_lhb": f("hr_l"),
    }

    tool_adjustments: Dict[str, Any] = {
        "_comment": "Batting computed from raw factors (SAHL formulas); "
                    "defense/baserunning/pitcher left neutral (no formula).",
        "batting": compute_batting_adjustments(raw),
    }
    for group, tools in _NEUTRAL_TOOLS.items():
        tool_adjustments[group] = {tool: 1.0 for tool in tools}

    handed = compute_handedness_pow(raw)
    return {
        "team_info": {
            "team_name": bp.get("display_name") or bp.get("name") or "",
            "team_code": (bp.get("abbr") or "").strip(),
            "park_name": "",  # /ballparks does not expose the stadium name
        },
        "raw_park_factors": {
            "_comment": "Raw park factors from /ballparks - 1.0 = league average",
            **raw,  # preserve API fidelity; only computed adjustments are rounded
        },
        "park_profile": build_park_profile(raw, bp),
        "tool_adjustments": tool_adjustments,
        "handedness_splits": {
            "_comment": "Per-hand Power from handed HR factors; disabled by default.",
            "enabled": False,
            "RHB": handed["RHB"],
            "LHB": handed["LHB"],
        },
        "application_rules": {
            "apply_to_prospects": True,
            "apply_to_major_leaguers": True,
            "use_handedness_splits": False,
            "adjustment_strength": 0.75,
        },
    }


def build_park_factors(ballparks: Dict[str, Any]) -> Dict[str, Any]:
    """``/ballparks`` JSON → the ``{slug}-park-factors.json`` shape, keyed by
    team ``display_name``, with neutral tool adjustments."""
    parks = ballparks.get("ballparks") if isinstance(ballparks, dict) else None
    if not isinstance(parks, list) or not parks:
        raise ProvisionError("/ballparks returned no 'ballparks' list.")
    teams: Dict[str, Any] = {}
    for bp in parks:
        if not isinstance(bp, dict):
            continue
        name = bp.get("display_name") or bp.get("name")
        if not name:
            continue
        teams[name] = _build_team_entry(bp)
    if not teams:
        raise ProvisionError("/ballparks produced no usable team entries.")
    return {
        "_comment": "Park factors by team for VOS Evaluation",
        "_comment_2": "Auto-generated from /ballparks. Batting tool_adjustments "
                      "computed from raw factors; defense/baserunning/pitcher neutral.",
        "teams": teams,
    }


def build_orgs(ballparks: Dict[str, Any]) -> List[str]:
    """``/ballparks`` JSON → the ``{slug}_orgs.json`` list of ML org display
    names (sorted, matching the hand-authored files)."""
    parks = ballparks.get("ballparks") if isinstance(ballparks, dict) else None
    if not isinstance(parks, list):
        return []
    names = {bp.get("display_name") or bp.get("name")
             for bp in parks if isinstance(bp, dict)}
    return sorted(n for n in names if n)


def ml_lid_from_ballparks(ballparks: Dict[str, Any]) -> Optional[int]:
    """The ML StatsPlus league ID shared by /ballparks rows (their ``league_id``
    field), used to seed the ``ML`` entry of league_ids.json."""
    parks = ballparks.get("ballparks") if isinstance(ballparks, dict) else None
    if not isinstance(parks, list):
        return None
    for bp in parks:
        if isinstance(bp, dict):
            lid = bp.get("league_id")
            if isinstance(lid, int) and lid > 0:
                return lid
    return None


# --- network layer (injectable) ---------------------------------------------

def _build_url(base_url: str, endpoint: str, token: Optional[str]) -> str:
    url = base_url.rstrip("/") + "/" + endpoint.lstrip("/")
    if token:
        url += ("&" if "?" in url else "?") + urlencode({"token": token})
    return url


def _http_get(url: str, *, timeout: int = 60) -> str:
    req = Request(url, headers={"User-Agent": _USER_AGENT})
    try:
        with urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except HTTPError as e:
        raise ProvisionError(f"HTTP {e.code} fetching {url}") from e
    except URLError as e:
        raise ProvisionError(f"Network error fetching {url}: {e.reason}") from e


def fetch_text(base_url: str, endpoint: str, token: Optional[str], *, timeout: int = 60) -> str:
    return _http_get(_build_url(base_url, endpoint, token), timeout=timeout)


def fetch_json(base_url: str, endpoint: str, token: Optional[str], *, timeout: int = 60) -> Any:
    raw = _http_get(_build_url(base_url, endpoint, token), timeout=timeout)
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        raise ProvisionError(f"{endpoint} did not return JSON (auth/token issue?).") from e


# --- orchestration -----------------------------------------------------------

def provision_league(
    reg: LeagueRegistry,
    slug: str,
    url: str,
    token: Optional[str],
    *,
    settings: Optional[Dict[str, Any]] = None,
    overwrite: bool = False,
    text_fetcher: Callable[..., str] = fetch_text,
    json_fetcher: Callable[..., Any] = fetch_json,
) -> Dict[str, Any]:
    """Provision a new league from the API. Returns a manifest dict
    (counts + files written + warnings). Raises :class:`ProvisionError` /
    :class:`RegistryError` on failure.

    ``settings`` may carry the optional scalar metadata (org, rating_scale,
    year, …) to seed ``league_settings.json``.
    """
    LeagueRegistry.validate_slug(slug)
    LeagueRegistry.validate_url(url)
    if token:
        LeagueRegistry.validate_token(token)
    if not overwrite and reg.exists(slug):
        raise ProvisionError(
            f"League {slug!r} already exists. Use overwrite=True to re-provision.")

    warnings: List[str] = []
    files: List[str] = []

    # 1. /teams -> teams-{slug}.json
    teams = parse_teams_csv(text_fetcher(url, "teams", token))
    files.append(str(reg.write_teams(slug, teams)))

    # 2. /ballparks -> park-factors + orgs
    ballparks = json_fetcher(url, "ballparks", token)
    park_factors = build_park_factors(ballparks)
    files.append(str(reg.write_park_factors(slug, park_factors)))
    orgs = build_orgs(ballparks)
    ml_lid = ml_lid_from_ballparks(ballparks)
    if ml_lid is None:
        warnings.append("Could not determine the ML league ID from /ballparks; "
                        "league_ids ML stub left empty.")

    # 3. registry entries (url, token, scalar settings, orgs, ML league-ids stub)
    cfg = LeagueConfig(
        slug=slug, url=url, token=(token or None),
        orgs=orgs, league_ids={"ML": [ml_lid]} if ml_lid else {},
    )
    for key, val in (settings or {}).items():
        if hasattr(cfg, key) and val is not None:
            setattr(cfg, key, val)
    reg.save(cfg)

    return {
        "slug": slug,
        "teams_count": len(teams),
        "parks_count": len(park_factors["teams"]),
        "orgs_count": len(orgs),
        "ml_lid": ml_lid,
        "files_written": files,
        "warnings": warnings,
    }


__all__ = [
    "ProvisionError",
    "parse_teams_csv",
    "build_park_factors",
    "build_orgs",
    "ml_lid_from_ballparks",
    "compute_batting_adjustments",
    "compute_handedness_pow",
    "build_park_profile",
    "fetch_text",
    "fetch_json",
    "provision_league",
]
