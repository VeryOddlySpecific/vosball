#!/usr/bin/env python3
"""
VOS v2 (VOS Optimized Score) — Baseball player evaluation using a weighted scoring system.

Calculates normalized 20–80 scores for hitters and pitchers from PlayerData CSV and
config (weights_v2.json, id_maps, teams). Outputs evaluation_summary_{league}_{timestamp}.csv (or draft_evaluation_* with --draft).
"""

from __future__ import annotations
# --- repo-root + core/ path bootstrap ---
import os as _os, sys as _sys
_ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
for _p in (_ROOT, _os.path.join(_ROOT, "core")):
    if _p not in _sys.path:
        _sys.path.insert(0, _p)
# --- end bootstrap ---


import argparse
import csv
import json
import logging
import sys
from io import StringIO
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple
from urllib.error import URLError
from urllib.request import urlopen

# -----------------------------------------------------------------------------
# Paths and constants
# -----------------------------------------------------------------------------

SCRIPT_DIR = Path(__file__).resolve().parent.parent
DEFAULT_DATA_DIR = SCRIPT_DIR / "data"
DEFAULT_CONFIG_DIR = SCRIPT_DIR / "config"
WEIGHTS_FILENAME = "weights_v2.json"
ID_MAPS_FILENAME = "id_maps.json"
LEAGUE_URLS_FILENAME = "league_url.json"
TEAMS_FILENAME_TEMPLATE = "teams-{league}.json"
PLAYER_DATA_FILENAME_TEMPLATE = "PlayerData-{league}.csv"

# CSV column alternatives (first present wins)
BASERUNNING_STEAL_COLS = ["StealAbi", "Steal"]
PITCHER_ABILITY_CSV_TO_CONFIG = {
    "Stf": "Stuff",
    "Mov": "Movement",
    "Ctrl": "Control",   # CSV may have Ctrl_R, Ctrl_L only
    "HRA": "HR_Avoid",
}
# Control: CSV often has Ctrl_R/Ctrl_L only, no "Ctrl"
PITCHER_ABILITY_COL_ALTERNATIVES: Dict[str, List[str]] = {
    "Control": ["Ctrl", "Ctrl_R", "Ctrl_L"],
}
# Current → potential column names for potential VOS (batting and pitcher ability only; defense/baserunning have no Pot* in CSV)
HITTER_BATTING_CURRENT_TO_POTENTIAL = {"Gap": "PotGap", "Pow": "PotPow", "Eye": "PotEye", "Ks": "PotKs"}
PITCHER_ABILITY_CURRENT_TO_POTENTIAL = {"Stf": "PotStf", "Mov": "PotMov", "HRA": "PotHRA", "Ctrl": "PotCtrl"}
POT_PITCH_COLUMN_TO_TYPE = {
    "PotFst": "Fastball",
    "PotSnk": "Sinker",
    "PotCutt": "Cutter",
    "PotCrv": "Curve",
    "PotSld": "Slider",
    "PotChg": "Changeup",
    "PotSplt": "Splitter",
    "PotFrk": "Forkball",
    "PotCirChg": "Circle_Change",
    "PotScr": "Screwball",
    "PotKncrv": "Knuckle_Curve",
    "PotKnbl": "Knuckleball",
}
PITCH_SPEED_TIERS = {
    "Fastball": "hard", "Sinker": "hard", "Cutter": "hard",
    "Slider": "breaker", "Curve": "breaker", "Knuckle_Curve": "breaker", "Knuckleball": "breaker",
    "Changeup": "offspeed", "Circle_Change": "offspeed", "Splitter": "offspeed",
    "Forkball": "offspeed", "Screwball": "offspeed",
}
PITCH_BREAK_PLANES = {
    "Fastball": "vertical", "Sinker": "vertical", "Cutter": "horizontal",
    "Slider": "horizontal", "Curve": "vertical", "Knuckle_Curve": "vertical",
    "Knuckleball": "horizontal", "Changeup": "vertical", "Circle_Change": "vertical",
    "Splitter": "vertical", "Forkball": "vertical", "Screwball": "horizontal",
}
PERSONALITY_CSV_TO_CONFIG = {
    "Int": "Intelligence",
    "WrkEthic": "Work_Ethic",
    "Greed": "Greed",
    "Loy": "Loyalty",
    "Lead": "Leadership",
}

HITTER_POSITIONS = ["C", "1B", "2B", "3B", "SS", "LF", "CF", "RF", "DH"]
LEVEL_LABEL_TO_CONFIG = {"R": "Rookie"}  # id_maps uses "R", config uses "Rookie"

# Rating-scale conversion ----------------------------------------------------
# weights_v2.json (cutoffs, hard floors/ceilings, hardcoded thresholds in this
# file) all assume the 20-80 scale. Some leagues export component ratings on a
# 1-100 scale instead. When --rating-scale 1-100 is set we convert each rating
# cell down to 20-80 at load time so the rest of the pipeline (and the output)
# stays on the familiar 20-80 scale. OVR/POT, ages, IDs and personality cells
# are NOT converted.
RATING_SCALES = ("20-80", "1-100")
DEFAULT_RATING_SCALE = "20-80"

# Component-rating column names (CSV). These are the only cells that get
# rescaled when --rating-scale 1-100 is active.
RATING_COLUMNS: Set[str] = {
    # Hitter batting (current + potential)
    "Cntct", "Gap", "Pow", "Eye", "Ks",
    "PotCntct", "PotGap", "PotPow", "PotEye", "PotKs",
    # Hitter defense
    "CArm", "CFrm", "CBlk",
    "IFR", "IFA", "IFE", "TDP",
    "OFR", "OFA", "OFE",
    # Hitter baserunning
    "Speed", "Run", "StealAbi", "Steal", "StlRt",
    # Pitcher ability (current + potential)
    "Stf", "Mov", "Ctrl", "Ctrl_R", "Ctrl_L", "HRA",
    "PotStf", "PotMov", "PotCtrl", "PotHRA",
    # Pitcher pitch ratings (current)
    "Fst", "Snk", "Cutt", "Crv", "Sld", "Chg",
    "Splt", "Frk", "CirChg", "Scr", "Kncrv", "Knbl",
    # Pitcher pitch ratings (potential)
    "PotFst", "PotSnk", "PotCutt", "PotCrv", "PotSld", "PotChg",
    "PotSplt", "PotFrk", "PotCirChg", "PotScr", "PotKncrv", "PotKnbl",
    # Stamina (compared against 20-80 cutoffs in weights_v2.json)
    "Stm",
}


def convert_1_100_to_20_80(value: float) -> float:
    """Linear remap: 1->20, 50->50.30, 100->80. Inverse of standard scout-grade scaling."""
    return (value - 1.0) * (60.0 / 99.0) + 20.0


def apply_rating_scale_to_row(row: Dict[str, str], scale: str) -> None:
    """In-place: convert component-rating cells from 1-100 to 20-80. No-op if scale='20-80'."""
    if scale == DEFAULT_RATING_SCALE:
        return
    if scale != "1-100":
        raise ValueError(f"Unknown rating scale: {scale!r} (expected one of {RATING_SCALES})")
    for col in RATING_COLUMNS:
        raw = row.get(col)
        if raw is None:
            continue
        s = raw.strip()
        if s == "" or s.upper() in ("NA", "N/A", "."):
            continue
        try:
            v = float(s)
        except (TypeError, ValueError):
            continue
        row[col] = f"{convert_1_100_to_20_80(v):.4f}"


CONTRACT_FIELDS = [
    "player_id", "team_id", "league_id", "is_major", "no_trade",
    "last_year_team_option", "last_year_player_option", "last_year_vesting_option",
    "next_last_year_team_option", "next_last_year_player_option", "next_last_year_vesting_option",
    "contract_team_id", "contract_league_id", "season_year",
    "salary0", "salary1", "salary2", "salary3", "salary4", "salary5", "salary6", "salary7",
    "salary8", "salary9", "salary10", "salary11", "salary12", "salary13", "salary14",
    "years", "current_year", "minimum_pa", "minimum_pa_bonus", "minimum_ip", "minimum_ip_bonus",
    "mvp_bonus", "cyyoung_bonus", "allstar_bonus", "next_last_year_option_buyout", "last_year_option_buyout",
]

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)


# -----------------------------------------------------------------------------
# Data loading
# -----------------------------------------------------------------------------

def load_json(path: Path) -> Dict[str, Any]:
    """Load a JSON file; return empty dict on missing/invalid."""
    if not path.exists():
        logger.warning("Config not found: %s", path)
        return {}
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def load_weights(config_dir: Path) -> Dict[str, Any]:
    """Load weights_v2.json."""
    return load_json(config_dir / WEIGHTS_FILENAME)


def load_id_maps(config_dir: Path) -> Dict[int, str]:
    """Build league level id -> label (e.g. 1 -> 'ML')."""
    raw = load_json(config_dir / ID_MAPS_FILENAME)
    level_map = raw.get("league_level") or raw.get("league_levels")
    if not isinstance(level_map, dict):
        return {}
    lookup: Dict[int, str] = {}
    for label, value in level_map.items():
        if label.startswith("_"):
            continue
        try:
            key = int(value)
            lookup[key] = str(label)
        except (TypeError, ValueError):
            continue
    return lookup


def load_teams(config_dir: Path, league: str) -> Dict[int, str]:
    """Build team id -> display name (e.g. 'Arizona Diamondbacks')."""
    path = config_dir / TEAMS_FILENAME_TEMPLATE.format(league=league)
    raw = load_json(path)
    if not isinstance(raw, dict):
        return {}
    result: Dict[int, str] = {}
    for tid_str, info in raw.items():
        if tid_str.startswith("_") or not isinstance(info, dict):
            continue
        try:
            tid = int(tid_str)
        except (TypeError, ValueError):
            continue
        name = info.get("Name") or ""
        nick = info.get("Nickname") or ""
        result[tid] = f"{name} {nick}".strip() or f"Team {tid}"
    return result


def load_league_api_base_urls(config_dir: Path) -> Dict[str, str]:
    """Load league API base URLs from config/league_url.json."""
    path = config_dir / LEAGUE_URLS_FILENAME
    raw = load_json(path)
    if not isinstance(raw, dict):
        logger.warning("league_url.json missing or invalid at %s", path)
        return {}
    return {str(k).strip().lower(): str(v).strip().rstrip("/") for k, v in raw.items() if k and v}


def resolve_float(row: Dict[str, str], *col_candidates: str) -> Optional[float]:
    """First non-empty numeric value from row for given column names."""
    for col in col_candidates:
        if col not in row:
            continue
        val = row.get(col, "").strip()
        if val == "" or val.upper() in ("NA", "N/A", "."):
            continue
        try:
            return float(val)
        except (TypeError, ValueError):
            continue
    return None


def resolve_int(row: Dict[str, str], col: str) -> Optional[int]:
    """Integer value for column; None if missing or invalid."""
    v = resolve_float(row, col)
    return int(v) if v is not None else None


def load_player_data(
    data_dir: Path,
    league: str,
    id_filter: Optional[Set[str]] = None,
    rating_scale: str = DEFAULT_RATING_SCALE,
) -> List[Dict[str, str]]:
    """Load PlayerData-{league}.csv; optionally filter by ID set. Skip rows that fail basic validation.

    When rating_scale='1-100', component-rating cells are converted down to the
    20-80 scale at load time so all downstream cutoffs/weights apply unchanged.
    """
    if rating_scale not in RATING_SCALES:
        raise ValueError(f"rating_scale must be one of {RATING_SCALES}, got {rating_scale!r}")
    path = data_dir / PLAYER_DATA_FILENAME_TEMPLATE.format(league=league)
    if not path.exists():
        logger.error("Player data not found: %s", path)
        return []
    rows: List[Dict[str, str]] = []
    with path.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames or "ID" not in reader.fieldnames:
            logger.error("CSV missing ID column")
            return []
        for row in reader:
            pid = (row.get("ID") or "").strip()
            if not pid:
                continue
            if id_filter is not None and pid not in id_filter:
                continue
            apply_rating_scale_to_row(row, rating_scale)
            rows.append(row)
    if rating_scale != DEFAULT_RATING_SCALE:
        logger.info("Converted component ratings from %s -> 20-80 for %d players", rating_scale, len(rows))
    logger.info("Loaded %d players from %s", len(rows), path.name)
    return rows


def load_id_filter(file_path: Optional[Path]) -> Optional[Set[str]]:
    """Load set of player IDs from file (one per line or comma/semicolon/tab separated).

    Returns None only when file_path itself is None (no --ids-file given). If the path
    is provided but unreadable/empty, raises FileNotFoundError / ValueError so the
    caller fails loudly instead of silently evaluating every player.
    """
    if file_path is None:
        return None
    if not file_path.exists():
        raise FileNotFoundError(f"--ids-file not found: {file_path}")
    ids: Set[str] = set()
    with file_path.open("r", encoding="utf-8") as f:
        for line in f:
            for sep in (",", ";", "\t", " "):
                line = line.replace(sep, " ")
            for token in line.split():
                t = token.strip()
                if t:
                    ids.add(t)
    if not ids:
        raise ValueError(f"--ids-file contained no IDs: {file_path}")
    return ids


def get_league_base_url(
    league: str,
    base_url_override: Optional[str] = None,
    league_api_base_urls: Optional[Dict[str, str]] = None,
) -> Optional[str]:
    """Resolve league API base URL from override or built-in dictionary."""
    if base_url_override:
        return base_url_override.rstrip("/")
    lookup = league_api_base_urls or DEFAULT_LEAGUE_API_BASE_URLS
    return lookup.get((league or "").strip().lower())


def _fetch_csv_endpoint(url: str) -> List[Dict[str, str]]:
    """Fetch CSV endpoint and parse into row dictionaries."""
    with urlopen(url, timeout=30) as resp:
        content_type = (resp.headers.get("Content-Type") or "").lower()
        payload = resp.read().decode("utf-8-sig", errors="replace")

    # StatsPlus endpoints are typically CSV. Keep the parser strict and predictable.
    reader = csv.DictReader(StringIO(payload))
    if not reader.fieldnames:
        if "json" in content_type:
            logger.warning("Endpoint returned JSON instead of CSV: %s", url)
        raise ValueError(f"No CSV header found at endpoint: {url}")
    return [r for r in reader if isinstance(r, dict)]


def _season_year_value(row: Dict[str, str]) -> int:
    """Parse season_year for selecting newest contract row."""
    try:
        return int(float((row.get("season_year") or "").strip()))
    except (TypeError, ValueError):
        return -1


def _build_contract_lookup(rows: List[Dict[str, str]], id_filter: Optional[Set[str]] = None) -> Dict[str, Dict[str, str]]:
    """
    Build player_id -> row lookup, keeping the latest season_year row when duplicates exist.
    """
    out: Dict[str, Dict[str, str]] = {}
    for r in rows:
        pid = (r.get("player_id") or "").strip()
        if not pid:
            continue
        if id_filter is not None and pid not in id_filter:
            continue
        prev = out.get(pid)
        if prev is None or _season_year_value(r) >= _season_year_value(prev):
            out[pid] = r
    return out


def load_contract_data(
    base_url: str,
    id_filter: Optional[Set[str]] = None,
) -> Tuple[Dict[str, Dict[str, str]], Dict[str, Dict[str, str]]]:
    """Load /contract and /contractextension endpoints; return lookups keyed by player_id."""
    contract_url = f"{base_url.rstrip('/')}/contract"
    extension_url = f"{base_url.rstrip('/')}/contractextension"

    contract_rows = _fetch_csv_endpoint(contract_url)
    extension_rows = _fetch_csv_endpoint(extension_url)
    contract_lookup = _build_contract_lookup(contract_rows, id_filter)
    extension_lookup = _build_contract_lookup(extension_rows, id_filter)

    logger.info("Loaded %d /contract rows, %d /contractextension rows", len(contract_rows), len(extension_rows))
    logger.info("Built %d contract player entries, %d extension player entries", len(contract_lookup), len(extension_lookup))
    return contract_lookup, extension_lookup


def attach_contract_fields(
    out_row: Dict[str, Any],
    contract_row: Optional[Dict[str, str]],
    extension_row: Optional[Dict[str, str]],
) -> None:
    """Attach prefixed contract columns to output row."""
    for field in CONTRACT_FIELDS:
        out_row[f"Contract_{field}"] = (contract_row or {}).get(field, "")
        out_row[f"ContractExtension_{field}"] = (extension_row or {}).get(field, "")


# -----------------------------------------------------------------------------
# Park factors (optional)
# -----------------------------------------------------------------------------

def load_park_factors(path: Optional[str]) -> Optional[Dict[str, Any]]:
    """
    Load park factor adjustments from JSON file.

    Args:
        path: Path to park-factors.json file (can be None).

    Returns:
        Dictionary with parks, team_to_park_mapping, application_rules; or None if not provided/not found/invalid.
    """
    if not path:
        return None
    path_obj = Path(path)
    if not path_obj.exists():
        logger.warning("Park factors file not found: %s", path)
        return None
    try:
        with path_obj.open("r", encoding="utf-8") as f:
            data = json.load(f)
        logger.info("Loaded park factors from %s", path)
        return data
    except json.JSONDecodeError as e:
        logger.error("Invalid JSON in park factors file: %s", e)
        return None


def _is_single_park_format(park_factors: Dict[str, Any]) -> bool:
    """True if file is single-park (LVK) format: tool_adjustments at root, no team lookup."""
    return isinstance(park_factors.get("tool_adjustments"), dict)


def _build_single_park_config(park_factors: Dict[str, Any]) -> Dict[str, Any]:
    """Build park config from single-park file (e.g. park-factors-lvk.json)."""
    tool_adjustments = park_factors.get("tool_adjustments") or {}
    team_info = park_factors.get("team_info") or {}
    name = team_info.get("park_name") if isinstance(team_info, dict) else None
    if not name or not isinstance(name, str):
        name = "Park"
    handedness_raw = park_factors.get("handedness_splits") or {}
    handedness_splits = {}
    if isinstance(handedness_raw, dict):
        for k in ("RHB", "LHB"):
            if k in handedness_raw and isinstance(handedness_raw[k], dict):
                handedness_splits[k] = handedness_raw[k]
    return {
        "name": name,
        "tool_adjustments": tool_adjustments,
        "handedness_splits": handedness_splits,
    }


def _build_team_block_park_config(team_block: Dict[str, Any]) -> Dict[str, Any]:
    """Adapt a per-team block from the combined teams[] format into the dict shape
    apply_park_adjustments expects (tool_adjustments, handedness_splits, name)."""
    info = team_block.get("team_info") or {}
    name = info.get("park_name") or info.get("team_name") or "Park"
    handedness_raw = team_block.get("handedness_splits") or {}
    handedness_splits: Dict[str, Any] = {}
    if isinstance(handedness_raw, dict):
        for k in ("RHB", "LHB"):
            if k in handedness_raw and isinstance(handedness_raw[k], dict):
                handedness_splits[k] = handedness_raw[k]
    return {
        "name": name,
        "tool_adjustments": team_block.get("tool_adjustments") or {},
        "handedness_splits": handedness_splits,
    }


def get_player_park_config(
    row: Dict[str, str],
    park_factors: Optional[Dict[str, Any]],
    teams: Dict[int, str],
    league_lookup: Dict[int, str],
) -> Optional[Dict[str, Any]]:
    """
    Determine which park configuration applies to this player.

    Three formats supported:
    - Single-park (e.g. park-factors-lvk.json): tool_adjustments at root; same park applied to
      all players (subject to application_rules). No team lookup.
    - Combined teams[] (e.g. sahl-park-factors.json): {"teams": {team_name: {team_info, tool_adjustments, ...}}}.
      Looks up the player's team display name in the teams block.
    - Legacy multi-park: parks[key] and team_to_park_mapping; park chosen by player's team.

    Args:
        row: Player row (CSV dict).
        park_factors: Loaded park factors data (can be None).
        teams: Team ID (int) -> display name.
        league_lookup: League level ID -> label (e.g. 1 -> 'ML').

    Returns:
        Park configuration dict (tool_adjustments, handedness_splits, name, etc.) or None.
    """
    if not park_factors:
        return None
    rules = park_factors.get("application_rules", {})
    if not isinstance(rules, dict):
        rules = {}
    lg_lvl = resolve_int(row, "LgLvl")
    league_label = get_league_label(lg_lvl, league_lookup) if lg_lvl is not None else ""
    # Don't apply to prospects if rule says so (non-ML = prospect)
    if not rules.get("apply_to_prospects", False) and league_label != "ML":
        return None
    # Don't apply to major leaguers if rule says so
    if not rules.get("apply_to_major_leaguers", True) and league_label == "ML":
        return None

    if _is_single_park_format(park_factors):
        return _build_single_park_config(park_factors)

    # Combined teams[] format — keyed by team display name.
    teams_block = park_factors.get("teams")
    if isinstance(teams_block, dict) and teams_block:
        team_id_int = resolve_int(row, "Team")
        team_name = get_team_display(team_id_int, teams) if team_id_int is not None else ""
        if not team_name:
            return None
        # Try exact match first, then case-insensitive fallback.
        block = teams_block.get(team_name)
        if not isinstance(block, dict):
            for k, v in teams_block.items():
                if k.startswith("_"):
                    continue
                if k.strip().lower() == team_name.strip().lower() and isinstance(v, dict):
                    block = v
                    break
        if isinstance(block, dict):
            return _build_team_block_park_config(block)
        return None

    # Legacy multi-park format
    team_id_raw = row.get("Team", "").strip()
    team_id_int = resolve_int(row, "Team")
    team_name = get_team_display(team_id_int, teams) if team_id_int is not None else ""
    team_to_park = park_factors.get("team_to_park_mapping", {})
    if not isinstance(team_to_park, dict):
        team_to_park = {}
    park_key = team_to_park.get(team_id_raw) or team_to_park.get(team_name)
    if not park_key:
        return None
    parks = park_factors.get("parks", {})
    if not isinstance(parks, dict):
        return None
    return parks.get(park_key)


def apply_park_adjustments(
    tool_scores: Dict[str, float],
    tool_category: str,
    park_config: Optional[Dict[str, Any]],
    adjustment_strength: float,
    player_handedness: Optional[str] = None,
    use_handedness_splits: bool = False,
) -> Dict[str, float]:
    """
    Apply park factor multipliers to tool scores (multiplicative, before weighting).

    Only tools with explicit multipliers in the park config are adjusted; others unchanged.
    Formula: effective_multiplier = 1.0 + ((base_multiplier - 1.0) * adjustment_strength).

    Args:
        tool_scores: Dictionary of {tool_name: score}.
        tool_category: One of 'batting', 'defense', 'baserunning', 'pitcher_ability'.
        park_config: Park configuration from park_factors.json (tool_adjustments, handedness_splits).
        adjustment_strength: Strength multiplier (0.0–1.0) from application_rules.
        player_handedness: 'L' or 'R' for batting handedness (optional).
        use_handedness_splits: Whether to use handedness-specific adjustments (batting only).

    Returns:
        Adjusted tool scores dictionary (same keys; values multiplied where config has multiplier).
    """
    if not park_config:
        return tool_scores.copy()
    tool_adjustments = (park_config.get("tool_adjustments") or {}).get(tool_category, {})
    if not isinstance(tool_adjustments, dict):
        return tool_scores.copy()
    if (
        tool_category == "batting"
        and use_handedness_splits
        and player_handedness in ("L", "R")
    ):
        handedness_key = "LHB" if player_handedness == "L" else "RHB"
        handedness_adj = (park_config.get("handedness_splits") or {}).get(handedness_key, {})
        if isinstance(handedness_adj, dict):
            tool_adjustments = {**tool_adjustments, **handedness_adj}
    adjusted = tool_scores.copy()
    for tool_name, score in adjusted.items():
        if tool_name not in tool_adjustments:
            continue
        base_mult = tool_adjustments[tool_name]
        try:
            base_mult = float(base_mult)
        except (TypeError, ValueError):
            continue
        effective = 1.0 + ((base_mult - 1.0) * adjustment_strength)
        adjusted[tool_name] = score * effective
    return adjusted


# -----------------------------------------------------------------------------
# Normalization (20–80 sigmoid)
# -----------------------------------------------------------------------------

def normalize_to_20_80(
    raw_score: float,
    center: float = 50.0,
    scale: float = 15.0,
    floor: float = 20.0,
    ceiling: float = 80.0,
) -> float:
    """
    Sigmoid-based normalization to 20–80 scale.

    Formula: center + (shifted / (scale * (1 + abs(shifted / scale)))) * 30
    so scores near center stay close; extremes compress smoothly.
    """
    shifted = raw_score - center
    denom = scale * (1.0 + abs(shifted / scale))
    normalized = (shifted / denom) * 30.0
    out = center + normalized
    return max(floor, min(ceiling, out))


# -----------------------------------------------------------------------------
# VOS tier classification
# -----------------------------------------------------------------------------

# Fallback bands used if weights config is missing a 'tiers' block.
# Calibrated from TLG ML distribution (Feb 2026).
_DEFAULT_HITTER_TIERS: List[Dict[str, Any]] = [
    {"min": 65.0, "label": "Star"},
    {"min": 58.0, "label": "Above-Avg Regular"},
    {"min": 52.0, "label": "Reliable Starter"},
    {"min": 47.0, "label": "Fringe Regular"},
    {"min": 42.0, "label": "Bench"},
    {"min": 37.0, "label": "Replacement"},
    {"min": 0.0,  "label": "Org Filler"},
]
_DEFAULT_PITCHER_TIERS: List[Dict[str, Any]] = [
    {"min": 58.0, "label": "Ace"},
    {"min": 51.0, "label": "#2/#3 Starter"},
    {"min": 46.0, "label": "Mid-Rotation"},
    {"min": 41.0, "label": "Back-End / Setup"},
    {"min": 36.0, "label": "Long Relief / Swing"},
    {"min": 31.0, "label": "Replacement"},
    {"min": 0.0,  "label": "Org Filler"},
]


def _resolve_tier_bands(cfg: Optional[Dict[str, Any]], role: str) -> List[Dict[str, Any]]:
    """Return tier bands for 'hitter' or 'pitcher' from cfg, falling back to defaults."""
    role_key = "pitcher" if role == "pitcher" else "hitter"
    tiers_cfg = (cfg or {}).get("tiers") or {}
    bands = tiers_cfg.get(role_key)
    if not isinstance(bands, list) or not bands:
        return _DEFAULT_PITCHER_TIERS if role_key == "pitcher" else _DEFAULT_HITTER_TIERS
    return bands


def classify_vos_tier(
    score: Any,
    role: str = "hitter",
    cfg: Optional[Dict[str, Any]] = None,
) -> str:
    """
    Map a numeric VOS-style score to a role-aware tier label.

    role: 'hitter' for batting/positional/hitter-overall scores, 'pitcher' for SP/RP
    or pitcher-overall scores. Bands are read from cfg['tiers'][role] (top-down,
    first band whose 'min' is <= score wins). Returns empty string for missing /
    non-numeric scores so downstream display can stay clean.
    """
    try:
        if score is None or score == "":
            return ""
        val = float(score)
    except (TypeError, ValueError):
        return ""
    bands = _resolve_tier_bands(cfg, role)
    # Bands are expected to be ordered high->low. Tolerate unordered configs.
    sorted_bands = sorted(bands, key=lambda b: float(b.get("min", 0.0)), reverse=True)
    for band in sorted_bands:
        try:
            if val >= float(band.get("min", 0.0)):
                return str(band.get("label", ""))
        except (TypeError, ValueError):
            continue
    return ""


def tier_for_player_role(row_or_pos: Any) -> str:
    """Return 'pitcher' or 'hitter' given either a Pos string or a dict-like row."""
    if isinstance(row_or_pos, dict):
        pos = (row_or_pos.get("Pos") or "").strip().upper()
    else:
        pos = (str(row_or_pos or "")).strip().upper()
    return "pitcher" if pos in ("SP", "RP", "CL", "P") else "hitter"


# -----------------------------------------------------------------------------
# League / team labels
# -----------------------------------------------------------------------------

def get_league_label(lg_lvl: Optional[int], league_lookup: Dict[int, str]) -> str:
    """League level label for display and config lookup (R -> Rookie for config)."""
    if lg_lvl is None:
        return ""
    label = league_lookup.get(lg_lvl, "")
    return label


def get_league_key_for_config(display_label: str) -> str:
    """Key to use in config level_targets (e.g. R -> Rookie)."""
    return LEVEL_LABEL_TO_CONFIG.get(display_label, display_label)


def get_team_display(team_id: Optional[int], teams: Dict[int, str]) -> str:
    """Team display name."""
    if team_id is None:
        return ""
    return teams.get(team_id, str(team_id) if team_id else "")


# -----------------------------------------------------------------------------
# Hitter evaluation
# -----------------------------------------------------------------------------

def _weighted_sum_from_dict(tool_dict: Dict[str, float], weights: Dict[str, float]) -> Optional[float]:
    """Weighted average from tool->value dict and config weights. Returns None if no overlap."""
    total = 0.0
    weight_sum = 0.0
    for tool, w in weights.items():
        if tool.startswith("_") or w <= 0:
            continue
        if tool not in tool_dict:
            continue
        total += tool_dict[tool] * w
        weight_sum += w
    if weight_sum <= 0:
        return None
    return total / weight_sum


def hitter_batting_score(
    row: Dict[str, str],
    weights: Dict[str, float],
    park_config: Optional[Dict[str, Any]] = None,
    park_rules: Optional[Dict[str, Any]] = None,
    use_potential: bool = False,
) -> Optional[float]:
    """Weighted average of Gap, Pow, Eye, Ks (or Pot* when use_potential); optionally park-adjusted."""
    tool_dict: Dict[str, float] = {}
    for tool, w in weights.items():
        if tool.startswith("_"):
            continue
        col = (HITTER_BATTING_CURRENT_TO_POTENTIAL.get(tool) or tool) if use_potential else tool
        v = resolve_float(row, col)
        if v is not None:
            tool_dict[tool] = v
    if park_config and park_rules:
        strength = float(park_rules.get("adjustment_strength", 1.0))
        use_splits = bool(park_rules.get("use_handedness_splits", False))
        bats = (row.get("Bats") or "").strip().upper()
        handedness = bats[:1] if bats and bats[0] in ("L", "R") else None
        tool_dict = apply_park_adjustments(
            tool_dict, "batting", park_config, strength, handedness, use_splits
        )
    return _weighted_sum_from_dict(tool_dict, weights)


def hitter_defense_score(
    row: Dict[str, str],
    pos: str,
    pos_weights: Dict[str, float],
    standards: Dict[str, int],
    park_config: Optional[Dict[str, Any]] = None,
    park_rules: Optional[Dict[str, Any]] = None,
) -> Optional[float]:
    """Defense score for one position; None if standards not met. Optionally park-adjusted."""
    # 3B: no left-handed throwers (throw angle to first makes L unsuitable)
    if pos == "3B":
        throws = (row.get("Throws") or "").strip().upper()
        if throws and throws[:1] == "L":
            return None
    for attr, minimum in (standards or {}).items():
        if attr.startswith("_"):
            continue
        v = resolve_float(row, attr)
        if v is not None and v < minimum:
            return None
    tool_dict: Dict[str, float] = {}
    for attr, w in (pos_weights or {}).items():
        if attr.startswith("_") or w <= 0:
            continue
        v = resolve_float(row, attr)
        if v is not None:
            tool_dict[attr] = v
    if park_config and park_rules:
        strength = float(park_rules.get("adjustment_strength", 1.0))
        tool_dict = apply_park_adjustments(
            tool_dict, "defense", park_config, strength, None, False
        )
    return _weighted_sum_from_dict(tool_dict, pos_weights or {})


def hitter_baserunning_score(
    row: Dict[str, str],
    weights: Dict[str, float],
    park_config: Optional[Dict[str, Any]] = None,
    park_rules: Optional[Dict[str, Any]] = None,
) -> Optional[float]:
    """Weighted sum of Speed, Run, StealAbi/Steal, StlRt; optionally park-adjusted."""
    tool_dict: Dict[str, float] = {}
    for tool, w in weights.items():
        if tool.startswith("_"):
            continue
        if tool == "StealAbi":
            v = resolve_float(row, *BASERUNNING_STEAL_COLS)
        else:
            v = resolve_float(row, tool)
        if v is not None:
            tool_dict[tool] = v
    if park_config and park_rules:
        strength = float(park_rules.get("adjustment_strength", 1.0))
        tool_dict = apply_park_adjustments(
            tool_dict, "baserunning", park_config, strength, None, False
        )
    return _weighted_sum_from_dict(tool_dict, weights)


def hitter_position_scores(
    row: Dict[str, str],
    cfg: Dict[str, Any],
    park_config: Optional[Dict[str, Any]] = None,
    park_rules: Optional[Dict[str, Any]] = None,
    use_potential: bool = False,
) -> Tuple[float, float, float, Dict[str, Optional[float]], str, float]:
    """
    Batting, defense, baserunning; per-position scores (composite for viable, bat-only for DH); ideal position; ideal value.
    use_potential=True uses PotGap/PotPow/PotEye/PotKs for batting only (defense/baserunning have no Pot* in CSV).
    """
    h = cfg.get("hitters", {})
    tool_cats = h.get("tool_categories", {})
    bat_weights = tool_cats.get("batting", {})
    base_weights = tool_cats.get("baserunning", {})
    def_weights_by_pos = tool_cats.get("defense", {})
    pos_cat_weights = h.get("position_category_weights", {})
    standards = h.get("positional_standards", {})

    bat = hitter_batting_score(row, bat_weights, park_config, park_rules, use_potential) or 0.0
    base = hitter_baserunning_score(row, base_weights, park_config, park_rules) or 0.0

    pos_scores: Dict[str, Optional[float]] = {}
    def_sum = 0.0
    def_count = 0
    for pos in HITTER_POSITIONS:
        if pos == "DH":
            pos_scores[pos] = bat
            continue
        def_w = def_weights_by_pos.get(pos)
        std = standards.get(pos, {})
        def_score = (
            hitter_defense_score(row, pos, def_w or {}, std, park_config, park_rules)
            if def_w
            else None
        )
        if def_score is None:
            pos_scores[pos] = None
            continue
        def_sum += def_score
        def_count += 1
        cat_w = pos_cat_weights.get(pos, {})
        if not cat_w:
            pos_scores[pos] = def_score
            continue
        bat_w = cat_w.get("batting", 0.0) or 0.0
        def_wt = cat_w.get("defense", 0.0) or 0.0
        base_wt = cat_w.get("baserunning", 0.0) or 0.0
        pos_value = bat * bat_w + def_score * def_wt + base * base_wt
        pos_scores[pos] = pos_value

    def_avg = def_sum / def_count if def_count else 0.0

    # Default behavior favors highest score, with DH starting at pure batting value.
    ideal_value = bat
    ideal_pos = "DH"
    best_field_pos: Optional[str] = None
    best_field_value: Optional[float] = None
    for pos in HITTER_POSITIONS:
        s = pos_scores.get(pos)
        if s is None:
            continue
        if s > ideal_value:
            ideal_value = s
            ideal_pos = pos
        if pos != "DH" and (best_field_value is None or s > best_field_value):
            best_field_value = s
            best_field_pos = pos

    # Optional DH assignment override: if a player has a quality field option,
    # DH must be better by a configurable margin to remain the ideal role.
    dh_cfg = h.get("dh_assignment", {}) or {}
    min_field_quality = float(dh_cfg.get("min_field_quality", 0.0) or 0.0)
    min_dh_margin = float(dh_cfg.get("min_dh_margin_over_field", 0.0) or 0.0)
    if (
        ideal_pos == "DH"
        and best_field_pos is not None
        and best_field_value is not None
        and best_field_value >= min_field_quality
        and (bat - best_field_value) < min_dh_margin
    ):
        ideal_pos = best_field_pos
        ideal_value = best_field_value
    return bat, def_avg, base, pos_scores, ideal_pos, ideal_value


# -----------------------------------------------------------------------------
# Pitcher evaluation
# -----------------------------------------------------------------------------

def pitcher_ability_score(
    row: Dict[str, str],
    role_weights: Dict[str, float],
    park_config: Optional[Dict[str, Any]] = None,
    park_rules: Optional[Dict[str, Any]] = None,
    use_potential: bool = False,
) -> Optional[float]:
    """Ability = weighted sum of Stuff, Movement, Control, HR_Avoid (or Pot* when use_potential); optionally park-adjusted."""
    tool_dict: Dict[str, float] = {}
    for csv_col, config_key in PITCHER_ABILITY_CSV_TO_CONFIG.items():
        if use_potential:
            pot_col = PITCHER_ABILITY_CURRENT_TO_POTENTIAL.get(csv_col, csv_col)
            alts = [pot_col] if pot_col else PITCHER_ABILITY_COL_ALTERNATIVES.get(config_key, [csv_col])
        else:
            alts = PITCHER_ABILITY_COL_ALTERNATIVES.get(config_key, [csv_col])
        v = resolve_float(row, *alts)
        if v is not None:
            tool_dict[config_key] = v
    if park_config and park_rules:
        strength = float(park_rules.get("adjustment_strength", 1.0))
        tool_dict = apply_park_adjustments(
            tool_dict, "pitcher_ability", park_config, strength, None, False
        )
    return _weighted_sum_from_dict(tool_dict, role_weights)


def pitcher_arsenal_score(
    row: Dict[str, str],
    role: str,
    cfg: Dict[str, Any],
) -> Tuple[float, float]:
    """
    Arsenal score and diversity adjustment for SP or RP.
    Returns (arsenal_raw, diversity_adj).
    """
    ae = cfg.get("pitchers", {}).get("arsenal_evaluation", {})
    type_values = ae.get("pitch_type_values", {})
    slot_weights = ae.get("pitch_slot_weights", {}).get(role, {})
    div_req = ae.get("diversity_requirements", {}).get(role, {})
    div_mod = ae.get("diversity_modifiers", {})

    min_pitches = int(div_req.get("min_pitches", 3))
    min_vel = int(div_req.get("min_velocity_tiers", 2))
    min_break = int(div_req.get("min_break_planes", 2))
    vel_bonus = float(div_mod.get("velocity_tier_bonus", 0.0))
    break_bonus = float(div_mod.get("break_plane_bonus", 0.0))
    insufficient_penalty = float(div_mod.get("insufficient_pitches_penalty", 0.0))

    # Rank pitches by (rating * type_value), take top 4 for SP, 3 for RP
    slots = ["primary", "secondary", "tertiary", "quaternary"] if role == "SP" else ["primary", "secondary", "tertiary"]
    pitch_values: List[Tuple[float, str, str]] = []
    speed_tiers: Set[str] = set()
    break_planes: Set[str] = set()

    for col, ptype in POT_PITCH_COLUMN_TO_TYPE.items():
        v = resolve_float(row, col)
        if v is None or v <= 0:
            continue
        val = type_values.get(ptype, 1.0)
        if not isinstance(val, (int, float)):
            val = 1.0
        pitch_values.append((v * val, ptype, col))
        speed_tiers.add(PITCH_SPEED_TIERS.get(ptype, "other"))
        break_planes.add(PITCH_BREAK_PLANES.get(ptype, "other"))

    pitch_values.sort(key=lambda x: -x[0])
    raw_arsenal = 0.0
    for i, slot in enumerate(slots):
        if i >= len(pitch_values):
            break
        w = slot_weights.get(slot, 0.0) or 0.0
        raw_arsenal += pitch_values[i][0] * w
    # Scale to roughly 20–80: assume pitch ratings ~20–80, so sum of weighted ratings
    num_pitches = len(pitch_values)
    diversity_adj = 0.0
    if num_pitches < min_pitches:
        diversity_adj += insufficient_penalty
    if len(speed_tiers) >= min_vel:
        diversity_adj += vel_bonus
    if len(break_planes) >= min_break:
        diversity_adj += break_bonus
    return raw_arsenal, diversity_adj


def pitcher_combined_score(
    row: Dict[str, str],
    role: str,
    cfg: Dict[str, Any],
    park_config: Optional[Dict[str, Any]] = None,
    park_rules: Optional[Dict[str, Any]] = None,
    use_potential: bool = False,
) -> Tuple[float, float, float]:
    """Ability score, arsenal score (with diversity), and combined. use_potential uses Pot* for ability; arsenal already uses Pot* pitches."""
    pit = cfg.get("pitchers", {})
    ability_weights = pit.get("ability_weights", {}).get(role, {})
    role_balance = pit.get("role_balance", {}).get(role, {})
    stamina_cfg = pit.get("stamina_requirements", {}).get("SP", {})

    ability = pitcher_ability_score(row, ability_weights, park_config, park_rules, use_potential) or 0.0
    arsenal_raw, div_adj = pitcher_arsenal_score(row, role, cfg)
    arsenal = arsenal_raw + div_adj  # raw is on scale; div_adj is small bonus/penalty

    ab_w = float(role_balance.get("ability_weight", 0.8))
    ar_w = float(role_balance.get("arsenal_weight", 0.2))
    combined = ability * ab_w + arsenal * ar_w

    stamina_penalty = 0.0
    if role == "SP" and stamina_cfg:
        min_sta = float(stamina_cfg.get("minimum_stamina", 50))
        per_pt = float(stamina_cfg.get("penalty_per_point_below", 0.5))
        sta = resolve_float(row, "Stm")
        if sta is not None and sta < min_sta:
            stamina_penalty = (min_sta - sta) * per_pt
    combined -= stamina_penalty
    return ability, arsenal, combined


# -----------------------------------------------------------------------------
# Adjustments
# -----------------------------------------------------------------------------

def development_adjustment_hitter(row: Dict[str, str], cfg: Dict[str, Any]) -> float:
    """Current rating bonus + (gap to potential * 0.05). Only if avg potential >= 50."""
    tools = ["Gap", "Pow", "Eye", "Ks"]
    pots = ["PotGap", "PotPow", "PotEye", "PotKs"]
    cur = [resolve_float(row, t) for t in tools]
    pot = [resolve_float(row, p) for p in pots]
    cur = [c for c in cur if c is not None]
    pot = [p for p in pot if p is not None]
    if not cur or not pot:
        return 0.0
    avg_current = sum(cur) / len(cur)
    avg_potential = sum(pot) / len(pot)
    dev_cfg = (cfg.get("adjustments") or {}).get("development_trajectory") or {}
    hitter_cfg = dev_cfg.get("hitter") if isinstance(dev_cfg, dict) else {}
    min_pot = float(hitter_cfg.get("minimum_potential_for_bonus", 50)) if isinstance(hitter_cfg, dict) else 50.0
    if avg_potential < min_pot:
        return 0.0
    gap = avg_potential - avg_current
    if avg_current >= 55:
        current_bonus = 2.0
    elif avg_current >= 45:
        current_bonus = 1.0
    elif avg_current >= 35:
        current_bonus = 0.0
    elif avg_current >= 25:
        current_bonus = -0.5
    else:
        current_bonus = -1.5
    return current_bonus + (gap * 0.05)


def development_adjustment_pitcher(row: Dict[str, str], cfg: Dict[str, Any]) -> float:
    """Same idea for pitchers: Stf, Mov, HRA, Ctrl and Pot*."""
    tools = ["Stf", "Mov", "HRA", "Ctrl"]
    pots = ["PotStf", "PotMov", "PotHRA", "PotCtrl"]
    cur = [resolve_float(row, t) for t in tools]
    pot = [resolve_float(row, p) for p in pots]
    cur = [c for c in cur if c is not None]
    pot = [p for p in pot if p is not None]
    if not cur or not pot:
        return 0.0
    avg_current = sum(cur) / len(cur)
    avg_potential = sum(pot) / len(pot)
    dev = (cfg.get("adjustments") or {}).get("development_trajectory") or {}
    pit = dev.get("pitcher") if isinstance(dev, dict) else {}
    min_pot = float(pit.get("minimum_potential_for_bonus", 50)) if isinstance(pit, dict) else 50.0
    if avg_potential < min_pot:
        return 0.0
    gap = avg_potential - avg_current
    if avg_current >= 55:
        current_bonus = 2.0
    elif avg_current >= 45:
        current_bonus = 1.0
    elif avg_current >= 35:
        current_bonus = 0.0
    elif avg_current >= 25:
        current_bonus = -0.5
    else:
        current_bonus = -1.5
    return current_bonus + (gap * 0.05)


# -----------------------------------------------------------------------------
# Draft-specific adjustments
# -----------------------------------------------------------------------------

def readiness_adjustment_pitcher(row: Dict[str, str], cfg: Dict[str, Any]) -> float:
    """
    Reward pitchers whose CURRENT profile can contribute at MLB right now.

    Orthogonal to development_adjustment: catches polished-arm-with-upside cases
    (e.g. a 22yo with 3 current plus pitches and avg current ability) whose
    floor value isn't captured by dev_adj's avg-current-tier + gap math.

    Components (all use CURRENT ratings, not Pot*):
      1. Core ability floor tier: min+avg of Stf/Mov/Ctrl/HRA clears a threshold.
      2. Plus-pitch count: current pitches at or above plus_pitch_threshold.
      3. Elite-pitch kicker: each current pitch above elite_pitch_threshold.
      4. Stamina check for SP: dampened if current Stm below sp_stamina_floor.

    Returns 0.0 if the readiness block is disabled or age < min_age.
    """
    r = ((cfg.get("adjustments") or {}).get("readiness") or {}).get("pitcher") or {}
    if not r.get("enabled", False):
        return 0.0
    age = resolve_float(row, "Age")
    if age is None or age < float(r.get("min_age", 20)):
        return 0.0

    stf  = resolve_float(row, "Stf")  or 0.0
    mov  = resolve_float(row, "Mov")  or 0.0
    hra  = resolve_float(row, "HRA")  or 0.0
    ctrl = resolve_float(row, "Ctrl", "Ctrl_R", "Ctrl_L") or 0.0
    core = [stf, mov, hra, ctrl]
    core_avg = sum(core) / len(core)
    core_min = min(core)

    bonus = 0.0
    for tier in r.get("floor_tiers", []) or []:
        try:
            min_avg = float(tier.get("min_avg", 0))
            min_min = float(tier.get("min_min", 0))
            tier_bonus = float(tier.get("bonus", 0))
        except (TypeError, ValueError):
            continue
        if core_avg >= min_avg and core_min >= min_min:
            if tier_bonus > bonus:
                bonus = tier_bonus

    # Plus / elite pitch counts — uses CURRENT pitch ratings, not Pot*.
    pitch_cols = ["Fst", "Snk", "Cutt", "Crv", "Sld", "Chg", "Splt", "Frk", "CirChg", "Scr", "Kncrv", "Knbl"]
    plus_thr = float(r.get("plus_pitch_threshold", 55))
    elite_thr = float(r.get("elite_pitch_threshold", 70))
    plus_cnt = sum(1 for c in pitch_cols if (resolve_float(row, c) or 0.0) >= plus_thr)
    elite_cnt = sum(1 for c in pitch_cols if (resolve_float(row, c) or 0.0) >= elite_thr)

    per_plus = float(r.get("per_plus_pitch", 0.5))
    max_plus_bonus = float(r.get("max_plus_pitch_bonus", 2.0))
    bonus += min(plus_cnt * per_plus, max_plus_bonus)
    bonus += elite_cnt * float(r.get("per_elite_pitch", 1.0))

    # Stamina floor: SPs who can't work 5+ innings aren't really ML-ready starters.
    pos = (row.get("Pos") or "").strip().upper()
    if pos == "SP":
        sp_floor = float(r.get("sp_stamina_floor", 45))
        stm = resolve_float(row, "Stm")
        if stm is not None and stm < sp_floor:
            bonus *= float(r.get("sp_stamina_penalty_mult", 0.5))
    return bonus


def readiness_adjustment_hitter(row: Dict[str, str], cfg: Dict[str, Any]) -> float:
    """
    Reward hitters whose CURRENT profile can contribute at MLB right now.

    Components (all use CURRENT ratings):
      1. Core batting floor tier: min+avg of Cntct/Gap/Pow/Eye/Ks clears a threshold.
      2. Plus-tool count: current batting tools at or above plus_tool_threshold.
      3. Elite-tool kicker: each current batting tool above elite_tool_threshold.
      4. Position readiness: bonus if current profile clears positional standards
         for at least one non-DH position (multi-position bonus if several).

    Returns 0.0 if the readiness block is disabled or age < min_age.
    """
    r = ((cfg.get("adjustments") or {}).get("readiness") or {}).get("hitter") or {}
    if not r.get("enabled", False):
        return 0.0
    age = resolve_float(row, "Age")
    if age is None or age < float(r.get("min_age", 20)):
        return 0.0

    cnt = resolve_float(row, "Cntct") or 0.0
    gap = resolve_float(row, "Gap")   or 0.0
    pw  = resolve_float(row, "Pow")   or 0.0
    eye = resolve_float(row, "Eye")   or 0.0
    ks  = resolve_float(row, "Ks")    or 0.0
    core = [cnt, gap, pw, eye, ks]
    core_avg = sum(core) / len(core)
    core_min = min(core)

    bonus = 0.0
    for tier in r.get("floor_tiers", []) or []:
        try:
            min_avg = float(tier.get("min_avg", 0))
            min_min = float(tier.get("min_min", 0))
            tier_bonus = float(tier.get("bonus", 0))
        except (TypeError, ValueError):
            continue
        if core_avg >= min_avg and core_min >= min_min:
            if tier_bonus > bonus:
                bonus = tier_bonus

    plus_thr = float(r.get("plus_tool_threshold", 55))
    elite_thr = float(r.get("elite_tool_threshold", 70))
    plus_cnt = sum(1 for v in core if v >= plus_thr)
    elite_cnt = sum(1 for v in core if v >= elite_thr)

    per_plus = float(r.get("per_plus_tool", 0.4))
    max_plus_bonus = float(r.get("max_plus_tool_bonus", 2.0))
    bonus += min(plus_cnt * per_plus, max_plus_bonus)
    bonus += elite_cnt * float(r.get("per_elite_tool", 1.0))

    # Position readiness: reuse existing positional_standards for a "fields now" check.
    standards = ((cfg.get("hitters") or {}).get("positional_standards") or {})
    viable_positions = 0
    for pos in [p for p in HITTER_POSITIONS if p != "DH"]:
        pos_std = standards.get(pos, {})
        if not isinstance(pos_std, dict) or not pos_std:
            continue
        meets = True
        for attr, minimum in pos_std.items():
            if attr.startswith("_"):
                continue
            try:
                min_val = float(minimum)
            except (TypeError, ValueError):
                continue
            v = resolve_float(row, attr)
            if v is None or v < min_val:
                meets = False
                break
        if meets:
            viable_positions += 1

    if viable_positions >= 1:
        bonus += float(r.get("position_ready_bonus", 1.0))
    multi_thr = int(r.get("multi_position_threshold", 3))
    if viable_positions >= multi_thr:
        bonus += float(r.get("multi_position_bonus", 0.5))

    return bonus


def _development_dampener(cfg: Dict[str, Any], draft_mode: bool) -> float:
    """Multiplier applied to dev_adj when draft_mode is active. Default 1.0 (no dampening)."""
    if not draft_mode:
        return 1.0
    dev = ((cfg.get("adjustments") or {}).get("development_trajectory") or {})
    try:
        return float(dev.get("draft_mode_dampener", 1.0))
    except (TypeError, ValueError):
        return 1.0


def draft_age_modifier(age: Optional[float]) -> float:
    """
    Draft age modifier: -1.5 at age 17, +1.5 at age 22, linear in between.
    Ages outside 17-22 are clamped to the endpoints.
    """
    if age is None:
        return 0.0
    if age <= 17:
        return -1.5
    if age >= 22:
        return 1.5
    # Linear: 17 -> -1.5, 22 -> +1.5  =>  slope = 3.0 / 5 = 0.6
    return -1.5 + (age - 17) * 0.6


def draft_role_penalty(role: str, cfg: Dict[str, Any], draft_mode: bool) -> float:
    """Optional draft-only role penalty (e.g., push RPs down draft boards)."""
    if not draft_mode:
        return 0.0
    penalties = ((cfg.get("adjustments") or {}).get("draft_role_penalties") or {})
    if not isinstance(penalties, dict):
        return 0.0
    key = (role or "").strip().upper()
    value = penalties.get(key, 0.0)
    return float(value) if isinstance(value, (int, float)) else 0.0


def age_adjustment(
    age: Optional[float],
    league_label: str,
    cfg: Dict[str, Any],
    role: str,
) -> float:
    """Bonus if young for level, penalty if old (from config level_targets)."""
    if age is None:
        return 0.0
    adj_cfg = (cfg.get("adjustments") or {}).get("age_vs_level") or {}
    role_cfg = adj_cfg.get(role, {}) if isinstance(adj_cfg, dict) else {}
    level_targets = role_cfg.get("level_targets", {}) if isinstance(role_cfg, dict) else {}
    key = get_league_key_for_config(league_label)
    level_cfg = level_targets.get(key) or level_targets.get(league_label) or {}
    if not level_cfg:
        return 0.0
    target_age = float(level_cfg.get("target_age", age))
    tolerance = max(0.1, float(level_cfg.get("tolerance_band", 2.0)))
    max_bonus = float(role_cfg.get("max_bonus", 3.0))
    max_penalty = float(role_cfg.get("max_penalty", -3.0))
    if age < target_age:
        ratio = min(1.0, (target_age - age) / tolerance)
        return ratio * max_bonus
    if age > target_age:
        ratio = min(1.0, (age - target_age) / tolerance)
        return ratio * max_penalty
    return 0.0


def _personality_bucket_from_cell(value: str) -> Optional[str]:
    """Map personality cell to config bucket: U=unknown (no modifier), H=high, N=normal, L=low."""
    if not value or not isinstance(value, str):
        return None
    v = value.strip().upper()
    if v == "H":
        return "high"
    if v == "N":
        return "normal"
    if v == "L":
        return "low"
    # U (unknown) or any other value: no modifier
    return None


def personality_adjustment(row: Dict[str, str], cfg: Dict[str, Any]) -> float:
    """Sum of trait modifiers from personality cells. Cells use U (unknown), H (high), N (normal), L (low).
    U or missing/other = no modifier. Only H/N/L apply the corresponding trait_modifiers."""
    impact = (cfg.get("adjustments") or {}).get("personality_impact") or {}
    if not isinstance(impact, dict):
        return 0.0
    mods = impact.get("trait_modifiers") or {}
    total = 0.0
    for csv_col, config_trait in PERSONALITY_CSV_TO_CONFIG.items():
        trait_mods = mods.get(config_trait) if isinstance(mods, dict) else {}
        if not isinstance(trait_mods, dict):
            continue
        raw = row.get(csv_col, "").strip() if csv_col in row else ""
        bucket = _personality_bucket_from_cell(raw)
        if bucket is None:
            continue
        m = trait_mods.get(bucket, 0.0)
        if isinstance(m, (int, float)):
            total += float(m)
    return total


# -----------------------------------------------------------------------------
# Output row building
# -----------------------------------------------------------------------------

def _normalization_params(cfg: Dict[str, Any]) -> Tuple[float, float, float, float]:
    n = (cfg.get("normalization") or {})
    return (
        float(n.get("target_center", 50.0)),
        float(n.get("scale_parameter", 15.0)),
        float(n.get("hard_floor", 20.0)),
        float(n.get("hard_ceiling", 80.0)),
    )


def build_hitter_row(
    row: Dict[str, str],
    cfg: Dict[str, Any],
    league_lookup: Dict[int, str],
    teams: Dict[int, str],
    park_factors: Optional[Dict[str, Any]] = None,
    draft_mode: bool = False,
) -> Optional[Dict[str, Any]]:
    """Build one output row for a hitter. Returns None if insufficient data. Optionally applies park factors."""
    park_config = (
        get_player_park_config(row, park_factors, teams, league_lookup)
        if park_factors
        else None
    )
    park_rules = (park_factors.get("application_rules", {}) or {}) if park_factors else None
    try:
        bat, def_avg, base, pos_scores, ideal_pos, ideal_value = hitter_position_scores(
            row, cfg, park_config, park_rules, use_potential=False
        )
        _, _, _, pos_scores_pot, ideal_pos_pot, ideal_value_pot = hitter_position_scores(
            row, cfg, park_config, park_rules, use_potential=True
        )
        h = cfg.get("hitters", {})
        bat_weights = (h.get("tool_categories") or {}).get("batting") or {}
        bat_pot = hitter_batting_score(row, bat_weights, park_config, park_rules, use_potential=True) or 0.0
    except Exception as e:
        logger.debug("Hitter score error for %s: %s", row.get("ID"), e)
        return None
    age = resolve_float(row, "Age")
    lg_lvl = resolve_int(row, "LgLvl")
    league_label = get_league_label(lg_lvl, league_lookup)
    team_id = resolve_int(row, "Team")
    org_id = resolve_int(row, "Org")
    dev_adj_raw = development_adjustment_hitter(row, cfg)
    dev_adj = dev_adj_raw * _development_dampener(cfg, draft_mode)
    age_adj = age_adjustment(age, league_label, cfg, "hitter")
    pers_adj = personality_adjustment(row, cfg)
    draft_age_adj = draft_age_modifier(age) if draft_mode else 0.0
    draft_rp_penalty = 0.0
    readiness_adj = readiness_adjustment_hitter(row, cfg) if draft_mode else 0.0
    raw_total = ideal_value + dev_adj + age_adj + pers_adj + draft_age_adj + readiness_adj
    center, scale, floor, ceiling = _normalization_params(cfg)
    vos = normalize_to_20_80(raw_total, center, scale, floor, ceiling)
    # Potential VOS: base from potential ratings only; no development adj (already potential); age/personality apply.
    # Readiness applies to VOS_Potential too: polished-now floor adds real value to projected outlook in draft.
    raw_total_pot = ideal_value_pot + 0.0 + age_adj + pers_adj + draft_age_adj + readiness_adj
    vos_potential = normalize_to_20_80(raw_total_pot, center, scale, floor, ceiling)
    out: Dict[str, Any] = {
        "ID": row.get("ID", ""),
        "Name": row.get("Name", ""),
        "Pos": row.get("Pos", ""),
        "Age": age if age is not None else "",
        "Team": get_team_display(team_id, teams),
        "Org": get_team_display(org_id, teams),
        "League_Level": league_label,
        "VOS_Score": round(vos, 2),
        "VOS_Potential": round(vos_potential, 2),
        "VOS_Tier": classify_vos_tier(vos, "hitter", cfg),
        "VOS_Potential_Tier": classify_vos_tier(vos_potential, "hitter", cfg),
        "Batting_Score": round(bat, 2),
        "Batting_Potential": round(bat_pot, 2),
        "Defense_Score": round(def_avg, 2),
        "Baserunning_Score": round(base, 2),
        "Pitching_Ability_Score": "",
        "Pitching_Ability_Potential": "",
        "Pitching_Arsenal_Score": "",
        "Development_Adj": round(dev_adj, 2),
        "Readiness_Adj": round(readiness_adj, 2) if draft_mode else "",
        "Age_Adj": round(age_adj, 2),
        "Personality_Adj": round(pers_adj, 2),
        "Park_Name": (park_config.get("name", "N/A") if park_config else "N/A"),
        "Park_Applied": park_config is not None,
    }
    if draft_mode:
        out["Draft_Age_Adj"] = round(draft_age_adj, 2)
        out["Draft_RP_Penalty"] = round(draft_rp_penalty, 2)
    for pos in HITTER_POSITIONS:
        s = pos_scores.get(pos)
        col = f"{pos}_Score"
        out[col] = round(s, 2) if s is not None else ""
        s_pot = pos_scores_pot.get(pos)
        out[f"{pos}_Potential"] = round(s_pot, 2) if s_pot is not None else ""

    # Projected position separation and flexibility insights.
    proj_cfg = ((h or {}).get("projection_insights") or {})
    margin_cfg = (proj_cfg.get("margin_tiers") or {})
    tieish_max = float(margin_cfg.get("tieish_max", 0.49))
    lean_max = float(margin_cfg.get("lean_max", 1.49))
    clear_max = float(margin_cfg.get("clear_max", 2.99))

    flex_cfg = (proj_cfg.get("flexibility") or {})
    relative_band = float(flex_cfg.get("relative_band", 2.0))
    include_dh = bool(flex_cfg.get("include_dh", True))

    candidate_positions = HITTER_POSITIONS if include_dh else [p for p in HITTER_POSITIONS if p != "DH"]
    projected_ranked: List[Tuple[str, float]] = []
    for pos in candidate_positions:
        s = pos_scores_pot.get(pos)
        if s is not None:
            projected_ranked.append((pos, float(s)))
    projected_ranked.sort(key=lambda x: x[1], reverse=True)

    if projected_ranked:
        projected_top_score = projected_ranked[0][1]
        projected_second_score = projected_ranked[1][1] if len(projected_ranked) > 1 else None
        projected_margin = (
            projected_top_score - projected_second_score
            if projected_second_score is not None
            else None
        )
        if projected_margin is None:
            margin_tier = "N/A"
        elif projected_margin <= tieish_max:
            margin_tier = "Tie-ish"
        elif projected_margin <= lean_max:
            margin_tier = "Lean"
        elif projected_margin <= clear_max:
            margin_tier = "Clear"
        else:
            margin_tier = "Strong"

        viable_positions = [p for p, s in projected_ranked if s >= (projected_top_score - relative_band)]
        out["Projected_Top_Score"] = round(projected_top_score, 2)
        out["Projected_Second_Score"] = round(projected_second_score, 2) if projected_second_score is not None else ""
        out["Projected_Margin"] = round(projected_margin, 2) if projected_margin is not None else ""
        out["Projected_Margin_Tier"] = margin_tier
        out["Projected_Viable_Positions"] = len(viable_positions)
        out["Projected_Viable_Pos_List"] = ",".join(viable_positions)
    else:
        out["Projected_Top_Score"] = ""
        out["Projected_Second_Score"] = ""
        out["Projected_Margin"] = ""
        out["Projected_Margin_Tier"] = ""
        out["Projected_Viable_Positions"] = ""
        out["Projected_Viable_Pos_List"] = ""

    out["Current_Position"] = ideal_pos
    out["Projected_Position"] = ideal_pos_pot
    out["Ideal_Value"] = round(ideal_value_pot, 2)
    return out


def build_pitcher_row(
    row: Dict[str, str],
    cfg: Dict[str, Any],
    league_lookup: Dict[int, str],
    teams: Dict[int, str],
    role: str = "SP",
    park_factors: Optional[Dict[str, Any]] = None,
    draft_mode: bool = False,
) -> Optional[Dict[str, Any]]:
    """Build one output row for a pitcher (evaluated as SP or RP). Optionally applies park factors to ability."""
    park_config = (
        get_player_park_config(row, park_factors, teams, league_lookup)
        if park_factors
        else None
    )
    park_rules = (park_factors.get("application_rules", {}) or {}) if park_factors else None
    try:
        ability, arsenal, combined = pitcher_combined_score(
            row, role, cfg, park_config, park_rules, use_potential=False
        )
        ability_pot, _, combined_pot = pitcher_combined_score(
            row, role, cfg, park_config, park_rules, use_potential=True
        )
    except Exception as e:
        logger.debug("Pitcher score error for %s: %s", row.get("ID"), e)
        return None
    age = resolve_float(row, "Age")
    lg_lvl = resolve_int(row, "LgLvl")
    league_label = get_league_label(lg_lvl, league_lookup)
    team_id = resolve_int(row, "Team")
    org_id = resolve_int(row, "Org")
    dev_adj_raw = development_adjustment_pitcher(row, cfg)
    dev_adj = dev_adj_raw * _development_dampener(cfg, draft_mode)
    age_adj = age_adjustment(age, league_label, cfg, "pitcher")
    pers_adj = personality_adjustment(row, cfg)
    draft_age_adj = draft_age_modifier(age) if draft_mode else 0.0
    draft_rp_penalty = draft_role_penalty(role, cfg, draft_mode)
    readiness_adj = readiness_adjustment_pitcher(row, cfg) if draft_mode else 0.0
    raw_total = combined + dev_adj + age_adj + pers_adj + draft_age_adj + draft_rp_penalty + readiness_adj
    center, scale, floor, ceiling = _normalization_params(cfg)
    vos = normalize_to_20_80(raw_total, center, scale, floor, ceiling)
    # Potential VOS: ability from PotStf/PotMov/PotHRA/PotCtrl; arsenal already uses Pot* pitches; no dev adj.
    # Readiness applies to VOS_Potential too: concrete floor value persists into the player's projected outlook.
    raw_total_pot = combined_pot + 0.0 + age_adj + pers_adj + draft_age_adj + draft_rp_penalty + readiness_adj
    vos_potential = normalize_to_20_80(raw_total_pot, center, scale, floor, ceiling)
    out: Dict[str, Any] = {
        "ID": row.get("ID", ""),
        "Name": row.get("Name", ""),
        "Pos": row.get("Pos", ""),
        "Age": age if age is not None else "",
        "Team": get_team_display(team_id, teams),
        "Org": get_team_display(org_id, teams),
        "League_Level": league_label,
        "VOS_Score": round(vos, 2),
        "VOS_Potential": round(vos_potential, 2),
        "VOS_Tier": classify_vos_tier(vos, "pitcher", cfg),
        "VOS_Potential_Tier": classify_vos_tier(vos_potential, "pitcher", cfg),
        "Batting_Score": "",
        "Batting_Potential": "",
        "Defense_Score": "",
        "Baserunning_Score": "",
        "Pitching_Ability_Score": round(ability, 2),
        "Pitching_Ability_Potential": round(ability_pot, 2),
        "Pitching_Arsenal_Score": round(arsenal, 2),
        "Development_Adj": round(dev_adj, 2),
        "Readiness_Adj": round(readiness_adj, 2) if draft_mode else "",
        "Age_Adj": round(age_adj, 2),
        "Personality_Adj": round(pers_adj, 2),
        "Park_Name": (park_config.get("name", "N/A") if park_config else "N/A"),
        "Park_Applied": park_config is not None,
    }
    if draft_mode:
        out["Draft_Age_Adj"] = round(draft_age_adj, 2)
        out["Draft_RP_Penalty"] = round(draft_rp_penalty, 2)
    for pos in HITTER_POSITIONS:
        out[f"{pos}_Score"] = ""
        out[f"{pos}_Potential"] = ""
    out["Projected_Top_Score"] = ""
    out["Projected_Second_Score"] = ""
    out["Projected_Margin"] = ""
    out["Projected_Margin_Tier"] = ""
    out["Projected_Viable_Positions"] = ""
    out["Projected_Viable_Pos_List"] = ""
    out["Current_Position"] = role
    out["Projected_Position"] = role
    out["Ideal_Value"] = round(combined_pot, 2)
    return out


def is_pitcher(row: Dict[str, str]) -> bool:
    """True if primary position is pitcher (SP/RP/CL/P)."""
    pos = (row.get("Pos") or "").strip().upper()
    return pos in ("SP", "RP", "CL", "P")


# -----------------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------------

def write_output_csv(
    rows: List[Dict[str, Any]],
    path: Path,
    draft_mode: bool = False,
    include_contracts: bool = False,
) -> None:
    """Write evaluation summary CSV with consistent column order."""
    if not rows:
        logger.warning("No rows to write")
        return
    cols = [
        "ID", "Name", "Pos", "Age", "Team", "Org", "League_Level",
        "VOS_Score", "VOS_Potential", "VOS_Tier", "VOS_Potential_Tier",
        "Batting_Score", "Batting_Potential", "Defense_Score", "Baserunning_Score",
        "Pitching_Ability_Score", "Pitching_Ability_Potential", "Pitching_Arsenal_Score",
        "Development_Adj", "Age_Adj", "Personality_Adj",
        "Park_Name", "Park_Applied",
    ]
    if draft_mode:
        # Readiness_Adj lives next to Development_Adj (only present in draft mode)
        cols.insert(cols.index("Development_Adj") + 1, "Readiness_Adj")
        cols.insert(cols.index("Personality_Adj") + 1, "Draft_Age_Adj")
        cols.insert(cols.index("Draft_Age_Adj") + 1, "Draft_RP_Penalty")
    pos_cols = [f"{p}_Score" for p in HITTER_POSITIONS]
    pos_pot_cols = [f"{p}_Potential" for p in HITTER_POSITIONS]
    cols += pos_cols + pos_pot_cols
    cols += [
        "Projected_Top_Score", "Projected_Second_Score", "Projected_Margin",
        "Projected_Margin_Tier", "Projected_Viable_Positions", "Projected_Viable_Pos_List",
    ]
    cols += ["Current_Position", "Projected_Position", "Ideal_Value"]
    if include_contracts:
        cols += [f"Contract_{f}" for f in CONTRACT_FIELDS]
        cols += [f"ContractExtension_{f}" for f in CONTRACT_FIELDS]
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def _write_eval_summary_md(rows: List[Dict[str, Any]], path: Path, league: str) -> None:
    """Write a focused Markdown summary of evaluation results for Obsidian.

    Sections:
      1. Top MLB players by VOS_Score (up to 50)
      2. Top prospects (non-MLB) by VOS_Potential (up to 75)
    """
    md_cols = ["Name", "Pos", "Age", "Team", "Org", "League_Level", "VOS_Score", "VOS_Potential"]

    def _row(r: Dict[str, Any]) -> str:
        cells = [str(r.get(c, "")) for c in md_cols]
        return "| " + " | ".join(cells) + " |"

    header = "| " + " | ".join(md_cols) + " |"
    sep = "| " + " | ".join("---" for _ in md_cols) + " |"

    mlb_rows = sorted(
        [r for r in rows if str(r.get("League_Level", "")).strip().upper() in ("MLB", "AAA")],
        key=lambda r: float(r.get("VOS_Score") or 0),
        reverse=True,
    )[:50]

    prospect_rows = sorted(
        [r for r in rows if str(r.get("League_Level", "")).strip().upper() not in ("MLB",)],
        key=lambda r: float(r.get("VOS_Potential") or 0),
        reverse=True,
    )[:75]

    lines: List[str] = [
        f"# Evaluation Summary — {league.upper()}",
        "",
        f"_Generated from `{path.name.replace('.md', '.csv')}`._",
        "",
        "## Top MLB/AAA Players by VOS Score",
        "",
        header, sep,
    ]
    lines += [_row(r) for r in mlb_rows]
    lines += [
        "",
        "## Top Prospects by VOS Potential",
        "",
        header, sep,
    ]
    lines += [_row(r) for r in prospect_rows]
    lines.append("")

    path.write_text("\n".join(lines), encoding="utf-8")
    logger.info("Wrote evaluation summary MD: %s", path)


def main() -> int:
    parser = argparse.ArgumentParser(description="VOS v2: Baseball player evaluation (20-80 normalized scores).")
    parser.add_argument("--league", required=True, help="League slug (e.g. woba, sky)")
    parser.add_argument("--output", default=None, help="Output CSV path (default: evaluation_summary_{league}_{timestamp}.csv (or draft_evaluation_* with --draft))")
    parser.add_argument("--ids-file", default=None, type=Path, help="Optional file of player IDs to include")
    parser.add_argument("--park-factors", default=None, type=str, help="Optional path to park-factors.json for ballpark-specific adjustments")
    parser.add_argument("--draft", action="store_true", help="Enable draft-specific adjustments (e.g. age modifier 17→-1.5, 22→+1.5)")
    parser.add_argument("--contracts", action="store_true", help="Include contract and contractextension API data in output.")
    parser.add_argument("--base-url", default=None, type=str, help="Override league API base URL (e.g. https://host/league/api)")
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR, help="Data directory")
    parser.add_argument("--config-dir", type=Path, default=DEFAULT_CONFIG_DIR, help="Config directory")
    parser.add_argument(
        "--per-org-evals",
        action="store_true",
        help=(
            "When the park-factors file is in combined teams[] format, write one eval per team "
            "into {league}/eval/{team_code}/. Each per-team eval grades the WHOLE league through "
            "that team's park context (single-park mode). Useful for sharing team-specific evals "
            "with friends."
        ),
    )
    parser.add_argument(
        "--rating-scale",
        choices=list(RATING_SCALES),
        default=DEFAULT_RATING_SCALE,
        help=(
            "Scale of the component ratings in PlayerData-{league}.csv. Default '20-80' "
            "matches weights_v2.json. Use '1-100' for leagues that export component ratings "
            "(Cntct/Gap/Pow/.../Stf/Mov/.../pitch ratings/Stm) on a 1-100 scale; values are "
            "linearly remapped to 20-80 at load time so cutoffs, hard floors and the output "
            "scale stay unchanged. OVR/POT, ages, IDs and personality cells are not converted."
        ),
    )
    args = parser.parse_args()

    config_dir = args.config_dir
    data_dir = args.data_dir
    league = args.league.strip()
    try:
        id_filter = load_id_filter(args.ids_file)
    except (FileNotFoundError, ValueError) as e:
        logger.error("%s", e)
        return 1

    cfg = load_weights(config_dir)
    if not cfg:
        logger.error("Weights config missing or invalid. Need %s", config_dir / WEIGHTS_FILENAME)
        return 1
    league_lookup = load_id_maps(config_dir)
    teams = load_teams(config_dir, league)
    league_api_base_urls = load_league_api_base_urls(config_dir)
    park_factors = load_park_factors(args.park_factors)
    players = load_player_data(data_dir, league, id_filter, rating_scale=args.rating_scale)
    if not players:
        logger.error("No players loaded.")
        return 1

    contract_lookup: Dict[str, Dict[str, str]] = {}
    extension_lookup: Dict[str, Dict[str, str]] = {}
    include_contracts = bool(args.contracts)
    if include_contracts:
        base_url = get_league_base_url(league, args.base_url, league_api_base_urls)
        if not base_url:
            logger.error(
                "No base URL found for league '%s'. Add it to %s or pass --base-url.",
                league,
                config_dir / LEAGUE_URLS_FILENAME,
            )
            return 1
        try:
            contract_lookup, extension_lookup = load_contract_data(base_url, id_filter)
        except (URLError, TimeoutError, ValueError) as e:
            logger.error("Failed to load contract endpoints from %s: %s", base_url, e)
            return 1

    from datetime import datetime
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    draft_mode = args.draft
    out_prefix = "draft_evaluation" if draft_mode else "evaluation_summary"

    def _run_eval_pass(pass_park_factors: Optional[Dict[str, Any]], out_path: Path) -> None:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        rows: List[Dict[str, Any]] = []
        for row in players:
            if is_pitcher(row):
                pos = (row.get("Pos") or "").strip().upper()
                role = "RP" if pos in ("RP", "CL") else "SP"
                out_row = build_pitcher_row(

                    row, cfg, league_lookup, teams, role=role,
                    park_factors=pass_park_factors, draft_mode=draft_mode,
                )
            else:
                out_row = build_hitter_row(
                    row, cfg, league_lookup, teams,
                    park_factors=pass_park_factors, draft_mode=draft_mode,
                )
            if out_row is not None:
                rows.append(out_row)
            else:
                logger.debug("Skipped row ID %s", row.get("ID"))
            if out_row is not None and include_contracts:
                pid = str(out_row.get("ID", "")).strip()
                attach_contract_fields(out_row, contract_lookup.get(pid), extension_lookup.get(pid))

        write_output_csv(rows, out_path, draft_mode=draft_mode, include_contracts=include_contracts)
        logger.info("Wrote %d rows to %s", len(rows), out_path)
        md_path = out_path.with_suffix(".md")
        _write_eval_summary_md(rows, md_path, league)

        # Validation: VOS scores in 20-80
        vos_values = [r["VOS_Score"] for r in rows if isinstance(r.get("VOS_Score"), (int, float))]
        if vos_values:
            lo, hi = min(vos_values), max(vos_values)
            if lo < 20 or hi > 80:
                logger.warning("VOS range [%.2f, %.2f] outside 20-80", lo, hi)
            else:
                logger.info("VOS range [%.2f, %.2f] (within 20-80)", lo, hi)

    # --per-org-evals: one eval per team in the combined teams[] block, each
    # treating the whole league as if every player batted in that team's park.
    if args.per_org_evals:
        teams_block = (park_factors or {}).get("teams") if isinstance(park_factors, dict) else None
        if not isinstance(teams_block, dict) or not teams_block:
            logger.error(
                "--per-org-evals requires a park-factors file in combined teams[] format. "
                "Either omit the flag or point --park-factors at a file with a top-level 'teams' object."
            )
            return 1

        app_rules = (park_factors or {}).get("application_rules") or {}
        for team_name, team_block in teams_block.items():
            if team_name.startswith("_") or not isinstance(team_block, dict):
                continue
            info = team_block.get("team_info") or {}
            team_code = (info.get("team_code") or "").strip().lower()
            if not team_code:
                # Fall back to slugified team_name if no team_code present.
                team_code = team_name.strip().lower().replace(" ", "_")
            # Synthesize a single-park view: tool_adjustments at root triggers
            # _is_single_park_format -> applies this team's park to everyone.
            synth_pf: Dict[str, Any] = {
                "tool_adjustments": team_block.get("tool_adjustments") or {},
                "handedness_splits": team_block.get("handedness_splits") or {},
                "team_info": info,
                "application_rules": app_rules,
            }
            out_path = (
                SCRIPT_DIR / league / "eval" / team_code
                / f"{out_prefix}_{league}_{ts}.csv"
            )
            logger.info("=" * 60)
            logger.info("Per-org eval: %s (%s)", team_name, team_code)
            _run_eval_pass(synth_pf, out_path)
        return 0

    # Default: single eval pass with the park-factors as loaded.
    out_path = args.output
    if out_path is None:
        out_path = SCRIPT_DIR / league / "eval" / f"{out_prefix}_{league}_{ts}.csv"
    else:
        out_path = Path(out_path)
    _run_eval_pass(park_factors, out_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
