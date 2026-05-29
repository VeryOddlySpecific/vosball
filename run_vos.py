#!/usr/bin/env python3
"""
run_vos.py — VOS v5/v6 scoring engine and standalone CLI.

The refactored successor to scripts/vos_v2.py. v2 stays in place for active
deployments running v2/v3 weights; this file is the home for the v5/v6
schemas and any future iterations.

Produces three normalized 20-80 scores per player:
    VOS_Reach    Predicts P(reach MLB). Two implementations:
                  - v5 heuristic (Pot*-weighted composite) — used when the
                    weights JSON has scoring_modes.vos_reach
                  - v6 logistic (trained model with current+Pot* features
                    + age) — used when scoring_modes.vos_reach_v6 is
                    present; takes precedence over v5
    VOS_Career   Current-rating score with age-decay predicting WAR | MLB.
    VOS_Blended  alpha * Reach + (1 - alpha) * Career.

Reads PlayerData CSV + a weights JSON. Writes
evaluation_summary_{league}_{timestamp}.csv plus a companion Markdown.

Schema sanity is checked at load time — feeding this engine a v2 or v3
weights file will fail loudly rather than silently producing nonsense.
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import math
import sys
from io import StringIO
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple
from urllib.error import URLError
from urllib.request import urlopen

SCRIPT_DIR = Path(__file__).resolve().parent

# Locate the directory containing lib/. Source layout has lib/ as a sibling
# of scripts/ (so SCRIPT_DIR.parent); deployed-alongside layout has lib/ as
# a sibling of run_vos.py itself (so SCRIPT_DIR).
for _lib_root in (SCRIPT_DIR, SCRIPT_DIR.parent):
    if (_lib_root / "lib").is_dir():
        if str(_lib_root) not in sys.path:
            sys.path.insert(0, str(_lib_root))
        break

from lib.vos_decay import apply_age_decay  # noqa: E402

# -----------------------------------------------------------------------------
# Paths and constants
# -----------------------------------------------------------------------------

DEFAULT_DATA_DIR = SCRIPT_DIR / "data"
DEFAULT_CONFIG_DIR = SCRIPT_DIR / "config"
WEIGHTS_FILENAME = "weights_v10.json"
ID_MAPS_FILENAME = "id_maps.json"
LEAGUE_URLS_FILENAME = "league_url.json"
TEAMS_FILENAME_TEMPLATE = "teams-{league}.json"
PLAYER_DATA_FILENAME_TEMPLATE = "PlayerData-{league}.csv"

DEFAULT_LEAGUE_API_BASE_URLS: Dict[str, str] = {}

# CSV column alternatives (first present wins).
BASERUNNING_STEAL_COLS = ["StealAbi", "Steal"]
# Control: CSV often has Ctrl_R/Ctrl_L only, no "Ctrl".
CTRL_COL_ALTERNATIVES = ["Ctrl", "Ctrl_R", "Ctrl_L"]

# Arsenal scoring: Pot* pitch columns -> pitch-type labels used by
# pitch_type_values in the weights JSON. Used in both Reach and Career —
# arsenal is always Pot*-based per v5 design (see v5_design.md §3).
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

# Injury proneness — live PlayerData exposes a 5-bucket categorical under
# the "Prone" column. Training (analysis/fit_reach_v8_injury.py) uses the
# numeric "prone_overall" from the dump (0-200 scale, 0 = Normal baseline,
# higher = more injury-prone). This dict maps the categorical to the
# numeric scale the model was trained on. Added 2026-05-22 for v8.
PRONE_CATEGORY_TO_NUMERIC = {
    "Wrecked":   90.0,    # ~p95 of non-zero dump values
    "Fragile":   30.0,    # ~median of the non-zero range
    "Normal":    0.0,     # matches the 75% dump baseline
    "Durable":  -10.0,    # slight durability bonus (extrapolates below training range)
    "Iron Man": -20.0,    # extra durability
}

HITTER_POSITIONS = ["C", "1B", "2B", "3B", "SS", "LF", "CF", "RF", "DH"]
LEVEL_LABEL_TO_CONFIG = {"R": "Rookie"}

# Rating-scale conversion (ported from vos_v2.py) ----------------------------
# weights_v6.json (cutoffs, hard floors/ceilings, age-decay slopes, learned
# logistic coefficients) all assume the 20-80 scale. Some leagues export
# component ratings on a 1-100 scale instead. When --rating-scale 1-100 is
# set we convert each rating cell down to 20-80 at load time so the rest of
# the pipeline (and the output) stays on the familiar 20-80 scale. OVR/POT,
# ages, IDs and personality cells are NOT converted.
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
    # Stamina (compared against 20-80 cutoffs in weights)
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
    if not path.exists():
        logger.warning("Config not found: %s", path)
        return {}
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def _validate_v5_schema(cfg: Dict[str, Any]) -> None:
    """Fail loudly if cfg isn't a v5/v6 weights file.

    v5/v6 is identified by the top-level scoring_modes block. Career mode
    must be present. For Reach, either the v5 heuristic block (vos_reach)
    or the v6 logistic model block (vos_reach_v6) must be present — v6
    takes precedence when both exist.
    """
    if not isinstance(cfg.get("scoring_modes"), dict):
        raise ValueError(
            "Weights file is not v5/v6: missing top-level 'scoring_modes' "
            "block. run_vos.py only accepts v5/v6 weights. Use "
            "scripts/vos_v2.py for v2/v3 weights."
        )
    modes = cfg["scoring_modes"]
    if not isinstance(modes.get("vos_career"), dict):
        raise ValueError("Weights file missing scoring_modes.vos_career")
    has_v5_reach = isinstance(modes.get("vos_reach"), dict)
    has_v6_reach = isinstance(modes.get("vos_reach_v6"), dict)
    if not (has_v5_reach or has_v6_reach):
        raise ValueError(
            "Weights file must have either scoring_modes.vos_reach "
            "(v5 heuristic) or scoring_modes.vos_reach_v6 (v6 logistic)."
        )
    if not isinstance(cfg.get("age_decay"), dict):
        raise ValueError("Weights file missing top-level 'age_decay' block.")


def load_weights(config_dir: Path,
                 weights_path: Optional[Path] = None) -> Dict[str, Any]:
    """Load a v5 weights JSON. Defaults to config_dir/weights_v5_draft.json.
    Pass weights_path (absolute, or relative to cwd or config_dir) to load
    any other v5 file (e.g. a tuned weights_v5.json post-validation)."""
    if weights_path is not None:
        p = Path(weights_path)
        if not p.is_absolute() and not p.exists():
            p = config_dir / weights_path
        cfg = load_json(p)
    else:
        cfg = load_json(config_dir / WEIGHTS_FILENAME)
    if cfg:
        _validate_v5_schema(cfg)
    return cfg


def load_id_maps(config_dir: Path) -> Dict[int, str]:
    raw = load_json(config_dir / ID_MAPS_FILENAME)
    level_map = raw.get("league_level") or raw.get("league_levels")
    if not isinstance(level_map, dict):
        return {}
    lookup: Dict[int, str] = {}
    for label, value in level_map.items():
        if label.startswith("_"):
            continue
        try:
            lookup[int(value)] = str(label)
        except (TypeError, ValueError):
            continue
    return lookup


def load_teams(config_dir: Path, league: str) -> Dict[int, str]:
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
    path = config_dir / LEAGUE_URLS_FILENAME
    raw = load_json(path)
    if not isinstance(raw, dict):
        logger.warning("league_url.json missing or invalid at %s", path)
        return {}
    return {str(k).strip().lower(): str(v).strip().rstrip("/")
            for k, v in raw.items() if k and v}


def resolve_float(row: Dict[str, str], *col_candidates: str) -> Optional[float]:
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
    v = resolve_float(row, col)
    return int(v) if v is not None else None


def load_player_data(data_dir: Path, league: str,
                     id_filter: Optional[Set[str]] = None,
                     rating_scale: str = DEFAULT_RATING_SCALE) -> List[Dict[str, str]]:
    """When rating_scale='1-100', component-rating cells are converted down to
    the 20-80 scale at load time so all downstream cutoffs/weights apply
    unchanged."""
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
        logger.info("Converted component ratings from %s -> 20-80 for %d players",
                    rating_scale, len(rows))
    logger.info("Loaded %d players from %s", len(rows), path.name)
    return rows


def load_id_filter(file_path: Optional[Path]) -> Optional[Set[str]]:
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
    if base_url_override:
        return base_url_override.rstrip("/")
    lookup = league_api_base_urls if league_api_base_urls is not None else DEFAULT_LEAGUE_API_BASE_URLS
    return lookup.get((league or "").strip().lower())


def _fetch_csv_endpoint(url: str) -> List[Dict[str, str]]:
    with urlopen(url, timeout=30) as resp:
        content_type = (resp.headers.get("Content-Type") or "").lower()
        payload = resp.read().decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(StringIO(payload))
    if not reader.fieldnames:
        if "json" in content_type:
            logger.warning("Endpoint returned JSON instead of CSV: %s", url)
        raise ValueError(f"No CSV header found at endpoint: {url}")
    return [r for r in reader if isinstance(r, dict)]


def _season_year_value(row: Dict[str, str]) -> int:
    try:
        return int(float((row.get("season_year") or "").strip()))
    except (TypeError, ValueError):
        return -1


def _build_contract_lookup(rows: List[Dict[str, str]],
                           id_filter: Optional[Set[str]] = None) -> Dict[str, Dict[str, str]]:
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
    contract_url = f"{base_url.rstrip('/')}/contract"
    extension_url = f"{base_url.rstrip('/')}/contractextension"
    contract_rows = _fetch_csv_endpoint(contract_url)
    extension_rows = _fetch_csv_endpoint(extension_url)
    contract_lookup = _build_contract_lookup(contract_rows, id_filter)
    extension_lookup = _build_contract_lookup(extension_rows, id_filter)
    logger.info("Loaded %d /contract rows, %d /contractextension rows",
                len(contract_rows), len(extension_rows))
    logger.info("Built %d contract entries, %d extension entries",
                len(contract_lookup), len(extension_lookup))
    return contract_lookup, extension_lookup


def attach_contract_fields(
    out_row: Dict[str, Any],
    contract_row: Optional[Dict[str, str]],
    extension_row: Optional[Dict[str, str]],
) -> None:
    for field in CONTRACT_FIELDS:
        out_row[f"Contract_{field}"] = (contract_row or {}).get(field, "")
        out_row[f"ContractExtension_{field}"] = (extension_row or {}).get(field, "")


# -----------------------------------------------------------------------------
# Park factors (optional)
# -----------------------------------------------------------------------------

def load_park_factors(path: Optional[str]) -> Optional[Dict[str, Any]]:
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
    return isinstance(park_factors.get("tool_adjustments"), dict)


def _build_single_park_config(park_factors: Dict[str, Any]) -> Dict[str, Any]:
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


def get_player_park_config(
    row: Dict[str, str],
    park_factors: Optional[Dict[str, Any]],
    teams: Dict[int, str],
    league_lookup: Dict[int, str],
) -> Optional[Dict[str, Any]]:
    if not park_factors:
        return None
    rules = park_factors.get("application_rules", {})
    if not isinstance(rules, dict):
        rules = {}
    lg_lvl = resolve_int(row, "LgLvl")
    league_label = get_league_label(lg_lvl, league_lookup) if lg_lvl is not None else ""
    if not rules.get("apply_to_prospects", False) and league_label != "ML":
        return None
    if not rules.get("apply_to_major_leaguers", True) and league_label == "ML":
        return None

    if _is_single_park_format(park_factors):
        return _build_single_park_config(park_factors)

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
# Normalization (20-80 sigmoid)
# -----------------------------------------------------------------------------

def normalize_to_20_80(
    raw_score: float,
    center: float = 50.0,
    scale: float = 15.0,
    floor: float = 20.0,
    ceiling: float = 80.0,
) -> float:
    shifted = raw_score - center
    denom = scale * (1.0 + abs(shifted / scale))
    normalized = (shifted / denom) * 30.0
    out = center + normalized
    return max(floor, min(ceiling, out))


def _normalization_params(cfg: Dict[str, Any]) -> Tuple[float, float, float, float]:
    n = (cfg.get("normalization") or {})
    return (
        float(n.get("target_center", 50.0)),
        float(n.get("scale_parameter", 15.0)),
        float(n.get("hard_floor", 20.0)),
        float(n.get("hard_ceiling", 80.0)),
    )


# -----------------------------------------------------------------------------
# VOS tier classification (ported from vos_v2.py for drop-in compatibility)
# -----------------------------------------------------------------------------
# Tier bands map a 20-80 score onto a human-readable label. v6 Reach is a
# logistic-mapped probability rather than the v2 Pot*-weighted composite, so
# the same band may carry different distribution mass — recalibration against
# the v6 Reach distribution is a follow-up. Default bands kept identical to
# vos_v2 so consumers reading VOS_Tier / VOS_Potential_Tier see the same labels.

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
    """Map a numeric VOS-style score to a role-aware tier label.

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
    if lg_lvl is None:
        return ""
    return league_lookup.get(lg_lvl, "")


def get_league_key_for_config(display_label: str) -> str:
    return LEVEL_LABEL_TO_CONFIG.get(display_label, display_label)


def get_team_display(team_id: Optional[int], teams: Dict[int, str]) -> str:
    if team_id is None:
        return ""
    return teams.get(team_id, str(team_id) if team_id else "")


# -----------------------------------------------------------------------------
# Mode helpers
# -----------------------------------------------------------------------------

def _mode_block(cfg: Dict[str, Any], mode: str) -> Dict[str, Any]:
    """Return the scoring_modes.{mode} sub-block. mode is 'reach', 'career',
    or 'ceiling'.

    'ceiling' = Stage-2 (Career) weights applied to POTENTIAL ratings, i.e.
    projected quality at maturity. It inherits Career's defense / baserunning /
    position-category weights wholesale and only swaps the batting block for
    vos_ceiling's Pot*-keyed weights — so retuning Career keeps ceiling in sync
    automatically. Defensive/baserunning ratings have no Pot* counterpart, so
    they use current ratings (decay is suppressed for this mode in
    _decay_cfg_for_mode, giving peak/undecayed values)."""
    modes = cfg.get("scoring_modes") or {}
    if mode == "reach":
        return modes.get("vos_reach") or {}
    if mode == "ceiling":
        ceil = modes.get("vos_ceiling") or {}
        if not ceil:
            return {}
        career = modes.get("vos_career") or {}
        ceil_bat = (((ceil.get("hitters") or {}).get("tool_categories") or {})
                    .get("batting"))
        if not ceil_bat:
            return career
        career_h = career.get("hitters") or {}
        merged_tc = dict(career_h.get("tool_categories") or {})
        merged_tc["batting"] = ceil_bat
        merged_h = dict(career_h)
        merged_h["tool_categories"] = merged_tc
        merged = dict(career)
        merged["hitters"] = merged_h
        return merged
    return modes.get("vos_career") or {}


def _has_ceiling(cfg: Dict[str, Any]) -> bool:
    return isinstance((cfg.get("scoring_modes") or {}).get("vos_ceiling"), dict)


def _decay_cfg_for_mode(cfg: Dict[str, Any], mode: str) -> Optional[Dict[str, Any]]:
    """Career mode applies age decay; Reach and Ceiling modes do not (Pot*
    already encodes the ceiling)."""
    return cfg.get("age_decay") if mode == "career" else None


def _resolve_with_decay(
    row: Dict[str, str],
    csv_col: str,
    decay_tool: Optional[str],
    age: Optional[float],
    decay_cfg: Optional[Dict[str, Any]],
    floor: float,
    *alt_cols: str,
) -> Optional[float]:
    """Look up csv_col (with optional alternates) in row, then apply decay
    keyed by decay_tool. decay_tool is None for ratings that should never
    decay (Pot* values). Returns None if no value found."""
    cols = (csv_col, *alt_cols)
    raw = resolve_float(row, *cols)
    if raw is None:
        return None
    if decay_tool is None or decay_cfg is None:
        return raw
    return apply_age_decay(raw, decay_tool, age, decay_cfg, floor)


def _decay_tool_for_key(weight_key: str) -> Optional[str]:
    """Map a v5 weight-dict key to the age_decay key for that rating, or
    None if no decay should apply.

    v5 keys are CSV column names directly. Pot* keys never decay. The
    age_decay block keys current-rating tool names (Pow, Stf, OFR, etc.) —
    in v5 that's identical to the CSV column name for current ratings, so
    the mapping is trivial: 'Pow' -> 'Pow', 'PotPow' -> None.

    Ctrl variants (Ctrl_R, Ctrl_L) map back to 'Ctrl' for decay purposes.
    """
    if weight_key.startswith("Pot"):
        return None
    if weight_key in ("Ctrl_R", "Ctrl_L"):
        return "Ctrl"
    return weight_key


# -----------------------------------------------------------------------------
# v6 Reach — trained logistic regression model
# -----------------------------------------------------------------------------
# The v6 Reach block lives at scoring_modes.vos_reach_v6 in the weights JSON.
# Three models — hitter_model, sp_model, rp_model — each with:
#   features:  list of feature names in fixed order
#   means:     per-feature mean (for standardization)
#   stds:      per-feature std (for standardization)
#   medians:   per-feature median (used to impute missing values)
#   coefs:     per-feature logistic coefficient
#   intercept: model intercept
#
# Scoring math: z_i = (x_i - mean_i) / std_i;
#               logit = intercept + sum(coefs_i * z_i);
#               p = 1 / (1 + exp(-logit));
#               VOS_Reach = 20 + 60 * p.
# No further adjustments (age_adj, dev_adj, etc.) — the model already
# incorporates age, current ratings, defense, and position. Applying the
# v5-style adjustment stack on top would double-count.

def _v6_extract_features(row: Dict[str, str], cfg: Dict[str, Any],
                          is_pit: bool) -> Dict[str, float]:
    """Build the full feature dict for one player. Schema and computation
    must match analysis/fit_reach_v6.py.extract_features exactly — if it
    drifts, the trained coefficients will be applied to misaligned values."""
    pos = (row.get("Pos") or "").strip().upper()
    age = resolve_float(row, "Age")
    feats: Dict[str, float] = {"age": float("nan") if age is None else age}

    def _f(col: str) -> float:
        v = resolve_float(row, col)
        return float("nan") if v is None else float(v)

    # Injury proneness (v8). Source on the dump is "ProneOverall" (numeric);
    # source on live PlayerData is "Prone" (categorical) which we map via
    # PRONE_CATEGORY_TO_NUMERIC. Try numeric first, fall back to categorical.
    # Models that don't reference "ProneOverall" ignore this key (no-op).
    prone_num = resolve_float(row, "ProneOverall")
    if prone_num is None:
        prone_cat = (row.get("Prone") or "").strip()
        prone_num = PRONE_CATEGORY_TO_NUMERIC.get(prone_cat)
    feats["ProneOverall"] = float("nan") if prone_num is None else float(prone_num)

    # Personality dummies (v9). Each trait → two one-hot features
    # (trait_H, trait_L); Normal/Unknown → both 0. Models that don't
    # reference these keys ignore them (no-op for v6/v7/v8).
    for _t in ("Int", "WrkEthic", "Greed", "Loy", "Lead"):
        _val = (row.get(_t) or "").strip().upper()[:1]
        feats[f"{_t}_H"] = 1.0 if _val == "H" else 0.0
        feats[f"{_t}_L"] = 1.0 if _val == "L" else 0.0

    if is_pit:
        for c in ("PotStf", "PotMov", "PotHRA", "PotCtrl", "PotPBABIP",
                  "Stf", "Mov", "HRA", "PBABIP",
                  "PotFst", "PotSnk", "PotCutt", "PotCrv", "PotSld",
                  "PotChg", "PotSplt", "Stm"):
            feats[c] = _f(c)
        # PBABIP / PotPBABIP added 2026-05-21 for v7 (BABIP-vs-PBABIP is the
        # engine's hit-check per the OOTP AB resolution tree). v6 model
        # doesn't reference these keys so they're harmless when scoring v6.
        ctrl = resolve_float(row, "Ctrl", "Ctrl_R", "Ctrl_L")
        feats["Ctrl"] = float("nan") if ctrl is None else float(ctrl)
        # Pitch arsenal stats
        pitches: List[float] = []
        for c in ("PotFst", "PotSnk", "PotCutt", "PotCrv", "PotSld",
                  "PotChg", "PotSplt", "PotFrk", "PotCirChg", "PotScr",
                  "PotKncrv", "PotKnbl"):
            v = resolve_float(row, c)
            if v is not None and v > 0:
                pitches.append(float(v))
        pitches.sort(reverse=True)
        if len(pitches) >= 3:
            feats["pitch_top3"] = sum(pitches[:3]) / 3.0
        elif pitches:
            feats["pitch_top3"] = sum(pitches) / len(pitches)
        else:
            feats["pitch_top3"] = float("nan")
        feats["plus_pitches"] = float(sum(1 for p in pitches if p >= 55))
        feats["elite_pitches"] = float(sum(1 for p in pitches if p >= 70))
    else:
        for c in ("PotGap", "PotPow", "PotEye", "PotKs", "PotBABIP",
                  "Cntct", "Gap", "Pow", "Eye", "Ks", "BABIP",
                  "Speed", "Run", "StealAbi", "StlRt",
                  "CFrm", "CBlk", "CArm",
                  "IFR", "IFE", "IFA", "TDP",
                  "OFR", "OFE", "OFA"):
            feats[c] = _f(c)
        # BABIP / PotBABIP added 2026-05-21 for v7 (see PBABIP comment above).
        # pos_flex — count of non-DH positions where standards met
        standards = (cfg.get("hitters") or {}).get("positional_standards") or {}
        flex = 0
        for p_label in ("C", "1B", "2B", "3B", "SS", "LF", "CF", "RF"):
            std = standards.get(p_label, {})
            if not std:
                continue
            meets = True
            for attr, mn in std.items():
                if attr.startswith("_"):
                    continue
                v = resolve_float(row, attr)
                if v is None or v < float(mn):
                    meets = False
                    break
            if meets:
                flex += 1
        feats["pos_flex"] = float(flex)
        # pos_def_avg — actual-position defensive composite
        if pos == "C":
            d = [feats["CArm"], feats["CFrm"], feats["CBlk"]]
        elif pos in ("1B", "2B", "3B", "SS"):
            d = [feats["IFR"], feats["IFE"], feats["IFA"], feats["TDP"]]
        elif pos in ("LF", "CF", "RF"):
            d = [feats["OFR"], feats["OFE"], feats["OFA"]]
        else:
            d = []
        d = [x for x in d if not math.isnan(x)]
        feats["pos_def_avg"] = sum(d) / len(d) if d else float("nan")
        # Position one-hots
        for p_label in ("C", "1B", "2B", "3B", "SS", "LF", "CF", "RF", "DH"):
            feats[f"pos_{p_label}"] = 1.0 if pos == p_label else 0.0

    return feats


def _v6_reach_score(row: Dict[str, str], cfg: Dict[str, Any],
                     is_pit: bool, role: str = "SP") -> float:
    """Compute the v6 Reach score for one player. Returns a 20-80 value
    (sigmoid-mapped probability of reaching MLB)."""
    v6 = (cfg.get("scoring_modes") or {}).get("vos_reach_v6") or {}
    if is_pit:
        model = v6.get("rp_model" if role in ("RP", "CL") else "sp_model")
    else:
        model = v6.get("hitter_model")
    if not model:
        # Fallback if a model is missing — neutral score
        return 50.0

    features: List[str] = model["features"]
    means: List[float] = model["means"]
    stds: List[float] = model["stds"]
    medians: List[float] = model["medians"]
    coefs: List[float] = model["coefs"]
    intercept: float = float(model["intercept"])

    feats = _v6_extract_features(row, cfg, is_pit)

    logit = intercept
    for i, name in enumerate(features):
        v = feats.get(name, float("nan"))
        if v is None or math.isnan(v):
            v = medians[i]
        sd = stds[i] if stds[i] != 0 else 1.0
        z = (v - means[i]) / sd
        logit += coefs[i] * z

    # Sigmoid → probability, then map to 20-80.
    try:
        p = 1.0 / (1.0 + math.exp(-logit))
    except OverflowError:
        p = 0.0 if logit < 0 else 1.0
    return 20.0 + 60.0 * p


def _has_v6_reach(cfg: Dict[str, Any]) -> bool:
    return isinstance(
        (cfg.get("scoring_modes") or {}).get("vos_reach_v6"), dict
    )


# -----------------------------------------------------------------------------
# Hitter scoring
# -----------------------------------------------------------------------------

def _weighted_sum_from_dict(tool_dict: Dict[str, float],
                            weights: Dict[str, float]) -> Optional[float]:
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
    age: Optional[float],
    decay_cfg: Optional[Dict[str, Any]],
    floor: float,
    park_config: Optional[Dict[str, Any]] = None,
    park_rules: Optional[Dict[str, Any]] = None,
) -> Optional[float]:
    """Weighted batting score. Weight keys are CSV column names directly
    (Gap/Pow/Eye/Ks/Cntct in Career, PotGap/PotPow/.../Cntct in Reach).
    Career mode applies age-decay to current ratings; Reach mode does not."""
    tool_dict: Dict[str, float] = {}
    for tool, w in weights.items():
        if tool.startswith("_") or w <= 0:
            continue
        decay_tool = _decay_tool_for_key(tool)
        v = _resolve_with_decay(row, tool, decay_tool, age, decay_cfg, floor)
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
    age: Optional[float],
    decay_cfg: Optional[Dict[str, Any]],
    floor: float,
    park_config: Optional[Dict[str, Any]] = None,
    park_rules: Optional[Dict[str, Any]] = None,
) -> Optional[float]:
    """Position-specific defense score. Returns None if positional standards
    not met. Defensive ratings have no Pot* counterparts, so the same
    current ratings are used in both modes; only decay differs."""
    if pos == "3B":
        throws = (row.get("Throws") or "").strip().upper()
        if throws and throws[:1] == "L":
            return None
    # Standards use raw (un-decayed) ratings — we're checking whether the
    # player CAN play the position, not whether decayed value clears the bar.
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
        decay_tool = _decay_tool_for_key(attr)
        v = _resolve_with_decay(row, attr, decay_tool, age, decay_cfg, floor)
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
    age: Optional[float],
    decay_cfg: Optional[Dict[str, Any]],
    floor: float,
    park_config: Optional[Dict[str, Any]] = None,
    park_rules: Optional[Dict[str, Any]] = None,
) -> Optional[float]:
    """Weighted baserunning score. Speed/Run/StealAbi/StlRt have no Pot*
    counterparts, so the same current ratings are used in both modes;
    only decay differs."""
    tool_dict: Dict[str, float] = {}
    for tool, w in weights.items():
        if tool.startswith("_") or w <= 0:
            continue
        decay_tool = _decay_tool_for_key(tool)
        if tool == "StealAbi":
            v = _resolve_with_decay(row, "StealAbi", decay_tool, age, decay_cfg,
                                    floor, "Steal")
        else:
            v = _resolve_with_decay(row, tool, decay_tool, age, decay_cfg, floor)
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
    mode: str,
    age: Optional[float],
    park_config: Optional[Dict[str, Any]] = None,
    park_rules: Optional[Dict[str, Any]] = None,
) -> Tuple[float, float, float, Dict[str, Optional[float]], str, float]:
    """Returns (bat, def_avg, base, pos_scores, ideal_pos, ideal_value) for
    one scoring mode ('reach' or 'career').

    pos_scores is per-position composite (or None where positional standards
    not met); ideal_pos is the highest-scoring slot (DH gets a margin
    requirement before it can beat a viable field position)."""
    mode_cfg = _mode_block(cfg, mode)
    h = mode_cfg.get("hitters", {})
    tool_cats = h.get("tool_categories", {})
    bat_weights = tool_cats.get("batting", {})
    base_weights = tool_cats.get("baserunning", {})
    def_weights_by_pos = tool_cats.get("defense", {})
    pos_cat_weights = h.get("position_category_weights", {})

    # positional_standards lives at top level in v5 (carried-forward block).
    standards = (cfg.get("hitters") or {}).get("positional_standards") or {}
    # dh_assignment likewise lives at top level.
    dh_cfg = (cfg.get("hitters") or {}).get("dh_assignment") or {}

    _, _, floor, _ = _normalization_params(cfg)
    decay_cfg = _decay_cfg_for_mode(cfg, mode)

    bat = hitter_batting_score(row, bat_weights, age, decay_cfg, floor,
                               park_config, park_rules) or 0.0
    base = hitter_baserunning_score(row, base_weights, age, decay_cfg, floor,
                                    park_config, park_rules) or 0.0

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
            hitter_defense_score(row, pos, def_w or {}, std, age, decay_cfg,
                                 floor, park_config, park_rules)
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
        pos_scores[pos] = bat * bat_w + def_score * def_wt + base * base_wt

    def_avg = def_sum / def_count if def_count else 0.0

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
# Pitcher scoring
# -----------------------------------------------------------------------------

def pitcher_ability_score(
    row: Dict[str, str],
    role_weights: Dict[str, float],
    age: Optional[float],
    decay_cfg: Optional[Dict[str, Any]],
    floor: float,
    park_config: Optional[Dict[str, Any]] = None,
    park_rules: Optional[Dict[str, Any]] = None,
) -> Optional[float]:
    """Weighted pitcher-ability score. Weight keys are CSV column names
    directly (Stf/Mov/Ctrl/HRA in Career, PotStf/PotMov/PotCtrl/PotHRA in
    Reach). Career mode applies age-decay; Reach mode does not.

    Ctrl falls back to Ctrl_R/Ctrl_L when the unified Ctrl column is absent
    in the CSV. PotCtrl has no equivalent alternates."""
    tool_dict: Dict[str, float] = {}
    for tool, w in role_weights.items():
        if tool.startswith("_") or w <= 0:
            continue
        decay_tool = _decay_tool_for_key(tool)
        if tool == "Ctrl":
            v = _resolve_with_decay(row, "Ctrl", decay_tool, age, decay_cfg,
                                    floor, "Ctrl_R", "Ctrl_L")
        else:
            v = _resolve_with_decay(row, tool, decay_tool, age, decay_cfg, floor)
        if v is not None:
            tool_dict[tool] = v
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
    """Arsenal score and diversity adjustment. Uses Pot* pitch columns in
    both modes per v5 design (arsenal_evaluation carries forward from v3
    unchanged). Mode-independent."""
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
    mode: str,
    age: Optional[float],
    park_config: Optional[Dict[str, Any]] = None,
    park_rules: Optional[Dict[str, Any]] = None,
) -> Tuple[float, float, float]:
    """Ability + arsenal + combined for one scoring mode. Returns
    (ability, arsenal, combined). Stamina-floor penalty applies to SPs in
    both modes (it's a structural roster-fit constraint, not an aging
    effect; Stm decay is already handled inside ability scoring when
    Stm is a weighted tool — currently it isn't, but the decay would apply
    if v5 weights ever added it)."""
    mode_cfg = _mode_block(cfg, mode)
    pit = mode_cfg.get("pitchers", {})
    ability_weights = pit.get("ability_weights", {}).get(role, {})
    role_balance = pit.get("role_balance", {}).get(role, {})

    # stamina_requirements lives at top level (carried-forward block).
    stamina_cfg = ((cfg.get("pitchers") or {}).get("stamina_requirements") or {}).get("SP", {})

    _, _, floor, _ = _normalization_params(cfg)
    decay_cfg = _decay_cfg_for_mode(cfg, mode)

    ability = pitcher_ability_score(row, ability_weights, age, decay_cfg,
                                    floor, park_config, park_rules) or 0.0
    arsenal_raw, div_adj = pitcher_arsenal_score(row, role, cfg)
    arsenal = arsenal_raw + div_adj

    ab_w = float(role_balance.get("ability_weight", 0.8))
    ar_w = float(role_balance.get("arsenal_weight", 0.2))
    combined = ability * ab_w + arsenal * ar_w

    stamina_penalty = 0.0
    if role == "SP" and stamina_cfg:
        min_sta = float(stamina_cfg.get("minimum_stamina", 50))
        per_pt = float(stamina_cfg.get("penalty_per_point_below", 0.5))
        # Stamina penalty uses raw Stm (not decayed) — it's a hard floor for
        # SP viability, separate from in-game decay.
        sta = resolve_float(row, "Stm")
        if sta is not None and sta < min_sta:
            stamina_penalty = (min_sta - sta) * per_pt
    combined -= stamina_penalty
    return ability, arsenal, combined


# -----------------------------------------------------------------------------
# Adjustments (carried forward from v2/v3, all read from cfg.adjustments.*)
# -----------------------------------------------------------------------------

def _development_dampener(cfg: Dict[str, Any], draft_mode: bool) -> float:
    if not draft_mode:
        return 1.0
    dev = ((cfg.get("adjustments") or {}).get("development_trajectory") or {})
    try:
        return float(dev.get("draft_mode_dampener", 1.0))
    except (TypeError, ValueError):
        return 1.0


def development_adjustment(row: Dict[str, str], cfg: Dict[str, Any], role: str) -> float:
    """Current rating bonus + (gap to potential * 0.05). Only fires when
    avg potential >= configured minimum. role: 'hitter' or 'pitcher'.

    NOTE: The thresholds/modifiers blocks in adjustments.development_trajectory
    are NOT read here — the ladder below is hardcoded for backwards-compat
    with v2/v3 behavior. The minimum_potential_for_bonus key IS read."""
    if role == "hitter":
        tools = ["Gap", "Pow", "Eye", "Ks"]
        pots = ["PotGap", "PotPow", "PotEye", "PotKs"]
    else:
        tools = ["Stf", "Mov", "HRA", "Ctrl"]
        pots = ["PotStf", "PotMov", "PotHRA", "PotCtrl"]
    cur_vals = [resolve_float(row, t) for t in tools]
    pot_vals = [resolve_float(row, p) for p in pots]
    cur_vals = [c for c in cur_vals if c is not None]
    pot_vals = [p for p in pot_vals if p is not None]
    if not cur_vals or not pot_vals:
        return 0.0
    avg_current = sum(cur_vals) / len(cur_vals)
    avg_potential = sum(pot_vals) / len(pot_vals)
    dev = (cfg.get("adjustments") or {}).get("development_trajectory") or {}
    sub = dev.get(role) if isinstance(dev, dict) else {}
    min_pot = float(sub.get("minimum_potential_for_bonus", 50)) if isinstance(sub, dict) else 50.0
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


def readiness_adjustment_pitcher(row: Dict[str, str], cfg: Dict[str, Any]) -> float:
    r = ((cfg.get("adjustments") or {}).get("readiness") or {}).get("pitcher") or {}
    if not r.get("enabled", False):
        return 0.0
    age = resolve_float(row, "Age")
    if age is None or age < float(r.get("min_age", 20)):
        return 0.0
    stf = resolve_float(row, "Stf") or 0.0
    mov = resolve_float(row, "Mov") or 0.0
    hra = resolve_float(row, "HRA") or 0.0
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
    pitch_cols = ["Fst", "Snk", "Cutt", "Crv", "Sld", "Chg", "Splt", "Frk", "CirChg", "Scr", "Kncrv", "Knbl"]
    plus_thr = float(r.get("plus_pitch_threshold", 55))
    elite_thr = float(r.get("elite_pitch_threshold", 70))
    plus_cnt = sum(1 for c in pitch_cols if (resolve_float(row, c) or 0.0) >= plus_thr)
    elite_cnt = sum(1 for c in pitch_cols if (resolve_float(row, c) or 0.0) >= elite_thr)
    per_plus = float(r.get("per_plus_pitch", 0.5))
    max_plus_bonus = float(r.get("max_plus_pitch_bonus", 2.0))
    bonus += min(plus_cnt * per_plus, max_plus_bonus)
    bonus += elite_cnt * float(r.get("per_elite_pitch", 1.0))
    pos = (row.get("Pos") or "").strip().upper()
    if pos == "SP":
        sp_floor = float(r.get("sp_stamina_floor", 45))
        stm = resolve_float(row, "Stm")
        if stm is not None and stm < sp_floor:
            bonus *= float(r.get("sp_stamina_penalty_mult", 0.5))
    return bonus


def readiness_adjustment_hitter(row: Dict[str, str], cfg: Dict[str, Any]) -> float:
    r = ((cfg.get("adjustments") or {}).get("readiness") or {}).get("hitter") or {}
    if not r.get("enabled", False):
        return 0.0
    age = resolve_float(row, "Age")
    if age is None or age < float(r.get("min_age", 20)):
        return 0.0
    cnt = resolve_float(row, "Cntct") or 0.0
    gap = resolve_float(row, "Gap") or 0.0
    pw = resolve_float(row, "Pow") or 0.0
    eye = resolve_float(row, "Eye") or 0.0
    ks = resolve_float(row, "Ks") or 0.0
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


def draft_age_modifier(age: Optional[float]) -> float:
    """Draft age modifier: -1.5 at age 17, +1.5 at age 22, linear in between.
    Endpoints are hardcoded — calibrated against this sim's default aging
    speed. Leagues with different aging settings may want this configurable
    (flagged in vos_v2_audit.md §4.8)."""
    if age is None:
        return 0.0
    if age <= 17:
        return -1.5
    if age >= 22:
        return 1.5
    return -1.5 + (age - 17) * 0.6


def draft_role_penalty(role: str, cfg: Dict[str, Any], draft_mode: bool) -> float:
    if not draft_mode:
        return 0.0
    penalties = ((cfg.get("adjustments") or {}).get("draft_role_penalties") or {})
    if not isinstance(penalties, dict):
        return 0.0
    value = penalties.get((role or "").strip().upper(), 0.0)
    return float(value) if isinstance(value, (int, float)) else 0.0


def age_adjustment(age: Optional[float], league_label: str,
                   cfg: Dict[str, Any], role: str) -> float:
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
    if not value or not isinstance(value, str):
        return None
    v = value.strip().upper()[:1]
    if v == "H":
        return "high"
    if v == "N":
        return "normal"
    if v == "L":
        return "low"
    return None


def personality_adjustment(row: Dict[str, str], cfg: Dict[str, Any]) -> float:
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

def _blend_alpha(cfg: Dict[str, Any]) -> float:
    blend = (cfg.get("scoring_modes") or {}).get("blend") or {}
    try:
        return float(blend.get("alpha", 0.4))
    except (TypeError, ValueError):
        return 0.4


def _interp1d(x: float, xs: Sequence[float], ys: Sequence[float]) -> float:
    """Linear interpolation of x against ascending xs -> ys, clamped at ends."""
    xs = [float(v) for v in xs]
    ys = [float(v) for v in ys]
    if x <= xs[0]:
        return ys[0]
    if x >= xs[-1]:
        return ys[-1]
    for i in range(1, len(xs)):
        if x <= xs[i]:
            x0, x1, y0, y1 = xs[i - 1], xs[i], ys[i - 1], ys[i]
            return y0 if x1 == x0 else y0 + (y1 - y0) * (x - x0) / (x1 - x0)
    return ys[-1]


def project_archetype_war(
    vos_ceiling: Optional[float],
    vos_career: Optional[float],
    age: Optional[float],
    in_mlb: bool,
    cfg: Dict[str, Any],
) -> Optional[Dict[str, Optional[float]]]:
    """Archetype 'ballpark' career-WAR projection (averages only, not a
    per-player forecast). Reads cfg['war_archetype']:

      ceiling_to_career: score[]/war[]  -> avg career WAR for this ceiling
                                           profile *if he reaches MLB*
      frac_ahead:        age[]/frac[]   -> share of career WAR still ahead
      career_to_ttd:     score[]/years[]-> projected years to debut

    Returns {arch_career, remaining, debut_age}:
      - arch_career: the profile's average MLB career WAR.
      - remaining:   arch_career               for prospects (full career ahead)
                     arch_career*frac_ahead(age) for players already in MLB.
      - debut_age:   current_age + ttd(VOS_Career) for prospects, else None.
    Returns None if no war_archetype table is present.
    """
    blk = cfg.get("war_archetype") or {}
    c2c = blk.get("ceiling_to_career") or {}
    score, war = c2c.get("score"), c2c.get("war")
    if not (isinstance(score, list) and isinstance(war, list)
            and len(score) == len(war) and len(score) >= 2):
        return None
    if vos_ceiling is None:
        return None
    arch = _interp1d(float(vos_ceiling), score, war)
    war_hi = c2c.get("war_hi")
    hi = (_interp1d(float(vos_ceiling), score, war_hi)
          if isinstance(war_hi, list) and len(war_hi) == len(score) else arch)
    frac = 1.0
    debut: Optional[float] = None
    fa = blk.get("frac_ahead") or {}
    if in_mlb and age is not None and isinstance(fa.get("age"), list) \
            and isinstance(fa.get("frac"), list):
        frac = _interp1d(float(age), fa["age"], fa["frac"])
    elif not in_mlb:
        c2t = blk.get("career_to_ttd") or {}
        if age is not None and vos_career is not None \
                and isinstance(c2t.get("score"), list) and isinstance(c2t.get("years"), list):
            debut = float(age) + _interp1d(float(vos_career), c2t["score"], c2t["years"])
    return {"arch_career": arch, "arch_upside": hi,
            "remaining": arch * frac, "remaining_upside": hi * frac,
            "debut_age": debut}


def _ceiling_tier_band(vos_ceiling: Optional[float],
                       cfg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Return the matching ceiling_tiers band dict ({min,label,median_war,...})
    for a VOS_Ceiling score, or None. Top-down: first band whose 'min' <= score."""
    if vos_ceiling is None:
        return None
    bands = (((cfg.get("war_archetype") or {}).get("ceiling_tiers") or {}).get("bands")) or []
    try:
        x = float(vos_ceiling)
    except (TypeError, ValueError):
        return None
    for band in sorted(bands, key=lambda b: float(b.get("min", 0.0)), reverse=True):
        try:
            if x >= float(band.get("min", 0.0)):
                return band
        except (TypeError, ValueError):
            continue
    return None


def _classify_ceiling_tier(vos_ceiling: Optional[float], cfg: Dict[str, Any]) -> str:
    """Flavor label for a VOS_Ceiling score (from ceiling_tiers bands)."""
    band = _ceiling_tier_band(vos_ceiling, cfg)
    return str(band.get("label", "")) if band else ""


def build_hitter_row(
    row: Dict[str, str],
    cfg: Dict[str, Any],
    league_lookup: Dict[int, str],
    teams: Dict[int, str],
    park_factors: Optional[Dict[str, Any]] = None,
    draft_mode: bool = False,
) -> Optional[Dict[str, Any]]:
    """Build one v5 output row for a hitter. Emits three scores:
    VOS_Reach, VOS_Career, VOS_Blended."""
    park_config = (
        get_player_park_config(row, park_factors, teams, league_lookup)
        if park_factors
        else None
    )
    park_rules = (park_factors.get("application_rules", {}) or {}) if park_factors else None
    age = resolve_float(row, "Age")
    use_v6_reach = _has_v6_reach(cfg)
    try:
        bat_c, def_c, base_c, pos_c, ideal_pos_c, ideal_val_c = hitter_position_scores(
            row, cfg, "career", age, park_config, park_rules
        )
        # Reach position scores are still computed for the per-position
        # output columns and projection insights, regardless of which
        # Reach model is in use.
        bat_r, def_r, base_r, pos_r, ideal_pos_r, ideal_val_r = hitter_position_scores(
            row, cfg, "reach", age, park_config, park_rules
        )
    except Exception as e:
        logger.debug("Hitter score error for %s: %s", row.get("ID"), e)
        return None

    lg_lvl = resolve_int(row, "LgLvl")
    league_label = get_league_label(lg_lvl, league_lookup)
    team_id = resolve_int(row, "Team")
    org_id = resolve_int(row, "Org")

    dev_adj_raw = development_adjustment(row, cfg, "hitter")
    dev_adj = dev_adj_raw * _development_dampener(cfg, draft_mode)
    age_adj = age_adjustment(age, league_label, cfg, "hitter")
    pers_adj = personality_adjustment(row, cfg)
    draft_age_adj = draft_age_modifier(age) if draft_mode else 0.0
    readiness_adj = readiness_adjustment_hitter(row, cfg) if draft_mode else 0.0

    center, scale, floor, ceiling = _normalization_params(cfg)

    # Career: includes dev_adj (gap-to-potential applies to current outlook).
    raw_career = ideal_val_c + dev_adj + age_adj + pers_adj + draft_age_adj + readiness_adj
    vos_career = normalize_to_20_80(raw_career, center, scale, floor, ceiling)

    # Reach: v6 model output is already 20-80 calibrated (sigmoid-mapped
    # probability) AND already incorporates age + current ratings + defense.
    # No adjustment stack on top. v5 heuristic Reach still uses the
    # adjustment stack since the heuristic doesn't have those inputs.
    if use_v6_reach:
        vos_reach = _v6_reach_score(row, cfg, is_pit=False)
    else:
        raw_reach = ideal_val_r + age_adj + pers_adj + draft_age_adj + readiness_adj
        vos_reach = normalize_to_20_80(raw_reach, center, scale, floor, ceiling)
    alpha = _blend_alpha(cfg)
    vos_blended = alpha * vos_reach + (1.0 - alpha) * vos_career

    # VOS_Ceiling: the VOS_Career formula evaluated on POTENTIAL ratings (no
    # age-decay) -> projected quality at maturity. It carries the SAME
    # adjustment stack as Career so the two differ ONLY by current-vs-potential
    # ratings; this guarantees Ceiling >= Career whenever there is growth room.
    # Present only when the weights file carries a vos_ceiling block.
    vos_ceiling: Optional[float] = None
    if _has_ceiling(cfg):
        try:
            _, _, _, _, _, ideal_val_ceil = hitter_position_scores(
                row, cfg, "ceiling", age, park_config, park_rules)
            raw_ceiling = (ideal_val_ceil + dev_adj + age_adj + pers_adj
                           + draft_age_adj + readiness_adj)
            vos_ceiling = normalize_to_20_80(raw_ceiling, center, scale,
                                             floor, ceiling)
        except Exception as e:
            logger.debug("Ceiling score error for %s: %s", row.get("ID"), e)
            vos_ceiling = None

    # Archetype 'ballpark' career WAR (averages, age-tied). Present only when
    # the weights file carries a war_archetype table; blank otherwise.
    arche = project_archetype_war(
        vos_ceiling, vos_career, age, league_label == "ML", cfg)

    out: Dict[str, Any] = {
        "ID": row.get("ID", ""),
        "Name": row.get("Name", ""),
        "Pos": row.get("Pos", ""),
        "Age": age if age is not None else "",
        "Team": get_team_display(team_id, teams),
        "Org": get_team_display(org_id, teams),
        "League_Level": league_label,
        # Back-compat aliasing per v5_design.md §1: legacy column names
        # carry the new semantics (Career -> VOS_Score, Reach -> VOS_Potential).
        "VOS_Score": round(vos_career, 2),
        "VOS_Potential": round(vos_reach, 2),
        "VOS_Tier": classify_vos_tier(vos_career, "hitter", cfg),
        "VOS_Potential_Tier": classify_vos_tier(vos_reach, "hitter", cfg),
        "VOS_Reach": round(vos_reach, 2),
        "VOS_Career": round(vos_career, 2),
        "VOS_Blended": round(vos_blended, 2),
        "VOS_Ceiling": round(vos_ceiling, 2) if vos_ceiling is not None else "",
        "Ceiling_Tier": _classify_ceiling_tier(vos_ceiling, cfg) if vos_ceiling is not None else "",
        "Arch_Career_WAR": round(arche["arch_career"], 1) if arche else "",
        "Arch_Career_WAR_Hi": round(arche["arch_upside"], 1) if arche else "",
        "Remaining_WAR": round(arche["remaining"], 1) if arche else "",
        "Remaining_WAR_Hi": round(arche["remaining_upside"], 1) if arche else "",
        "Proj_Debut_Age": (round(arche["debut_age"])
                           if arche and arche.get("debut_age") is not None else ""),
        "Batting_Score": round(bat_c, 2),
        "Batting_Potential": round(bat_r, 2),
        "Defense_Score": round(def_c, 2),
        "Baserunning_Score": round(base_c, 2),
        "Pitching_Ability_Score": "",
        "Pitching_Ability_Potential": "",
        "Pitching_Arsenal_Score": "",
        "Development_Adj": round(dev_adj, 2),
        "Readiness_Adj": round(readiness_adj, 2) if draft_mode else "",
        "Age_Adj": round(age_adj, 2),
        "Personality_Adj": round(pers_adj, 2),
        "Park_Name": (park_config.get("name", "N/A") if park_config else "N/A"),
        "Park_Applied": park_config is not None,
        # Passthrough of the raw Prone categorical for diagnostic / display use.
        # The numeric mapping is what feeds the v8 model; this column lets
        # downstream tools (depth_chart, v8_sidetest) show the human-readable
        # label without needing a join back to PlayerData.
        "Prone": (row.get("Prone") or "").strip(),
        # BABIP / PotBABIP passthrough (v7 feature). Surfaces for downstream
        # tools (free_agent_market) so a high-BABIP, low-batting-avg FA can
        # be flagged as a regression candidate without re-joining PlayerData.
        "BABIP": (row.get("BABIP") or "").strip(),
        "PotBABIP": (row.get("PotBABIP") or "").strip(),
    }
    if draft_mode:
        out["Draft_Age_Adj"] = round(draft_age_adj, 2)
        # Pitcher-only column kept in schema for csv-writer consistency; always 0 for hitters.
        out["Draft_RP_Penalty"] = 0.0

    for pos in HITTER_POSITIONS:
        s_c = pos_c.get(pos)
        s_r = pos_r.get(pos)
        out[f"{pos}_Score"] = round(s_c, 2) if s_c is not None else ""
        out[f"{pos}_Potential"] = round(s_r, 2) if s_r is not None else ""

    # Projection insights run off Reach mode (Pot*-based "future projection").
    proj_cfg = ((cfg.get("hitters") or {}).get("projection_insights") or {})
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
        s = pos_r.get(pos)
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

    out["Current_Position"] = ideal_pos_c
    out["Projected_Position"] = ideal_pos_r
    out["Ideal_Value"] = round(ideal_val_r, 2)
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
    """Build one v5 output row for a pitcher. Emits three scores:
    VOS_Reach, VOS_Career, VOS_Blended."""
    park_config = (
        get_player_park_config(row, park_factors, teams, league_lookup)
        if park_factors
        else None
    )
    park_rules = (park_factors.get("application_rules", {}) or {}) if park_factors else None
    age = resolve_float(row, "Age")
    use_v6_reach = _has_v6_reach(cfg)
    try:
        ability_c, arsenal_c, combined_c = pitcher_combined_score(
            row, role, cfg, "career", age, park_config, park_rules
        )
        # Reach combined still computed for output columns (Pitching_Ability_Potential).
        ability_r, arsenal_r, combined_r = pitcher_combined_score(
            row, role, cfg, "reach", age, park_config, park_rules
        )
    except Exception as e:
        logger.debug("Pitcher score error for %s: %s", row.get("ID"), e)
        return None

    lg_lvl = resolve_int(row, "LgLvl")
    league_label = get_league_label(lg_lvl, league_lookup)
    team_id = resolve_int(row, "Team")
    org_id = resolve_int(row, "Org")

    dev_adj_raw = development_adjustment(row, cfg, "pitcher")
    dev_adj = dev_adj_raw * _development_dampener(cfg, draft_mode)
    age_adj = age_adjustment(age, league_label, cfg, "pitcher")
    pers_adj = personality_adjustment(row, cfg)
    draft_age_adj = draft_age_modifier(age) if draft_mode else 0.0
    draft_rp_penalty = draft_role_penalty(role, cfg, draft_mode)
    readiness_adj = readiness_adjustment_pitcher(row, cfg) if draft_mode else 0.0

    center, scale, floor, ceiling = _normalization_params(cfg)

    raw_career = combined_c + dev_adj + age_adj + pers_adj + draft_age_adj + draft_rp_penalty + readiness_adj
    vos_career = normalize_to_20_80(raw_career, center, scale, floor, ceiling)

    # v6 model already incorporates age + current ratings + arsenal stats —
    # no adjustment stack on top.
    if use_v6_reach:
        vos_reach = _v6_reach_score(row, cfg, is_pit=True, role=role)
    else:
        raw_reach = combined_r + age_adj + pers_adj + draft_age_adj + draft_rp_penalty + readiness_adj
        vos_reach = normalize_to_20_80(raw_reach, center, scale, floor, ceiling)
    alpha = _blend_alpha(cfg)
    vos_blended = alpha * vos_reach + (1.0 - alpha) * vos_career

    out: Dict[str, Any] = {
        "ID": row.get("ID", ""),
        "Name": row.get("Name", ""),
        "Pos": row.get("Pos", ""),
        "Age": age if age is not None else "",
        "Team": get_team_display(team_id, teams),
        "Org": get_team_display(org_id, teams),
        "League_Level": league_label,
        "VOS_Score": round(vos_career, 2),
        "VOS_Potential": round(vos_reach, 2),
        "VOS_Tier": classify_vos_tier(vos_career, "pitcher", cfg),
        "VOS_Potential_Tier": classify_vos_tier(vos_reach, "pitcher", cfg),
        "VOS_Reach": round(vos_reach, 2),
        "VOS_Career": round(vos_career, 2),
        "VOS_Blended": round(vos_blended, 2),
        "Batting_Score": "",
        "Batting_Potential": "",
        "Defense_Score": "",
        "Baserunning_Score": "",
        "Pitching_Ability_Score": round(ability_c, 2),
        "Pitching_Ability_Potential": round(ability_r, 2),
        "Pitching_Arsenal_Score": round(arsenal_c, 2),
        "Development_Adj": round(dev_adj, 2),
        "Readiness_Adj": round(readiness_adj, 2) if draft_mode else "",
        "Age_Adj": round(age_adj, 2),
        "Personality_Adj": round(pers_adj, 2),
        "Park_Name": (park_config.get("name", "N/A") if park_config else "N/A"),
        "Park_Applied": park_config is not None,
        # Passthrough of the raw Prone categorical for diagnostic / display use.
        # The numeric mapping is what feeds the v8 model; this column lets
        # downstream tools (depth_chart, v8_sidetest) show the human-readable
        # label without needing a join back to PlayerData.
        "Prone": (row.get("Prone") or "").strip(),
        # PBABIP / PotPBABIP passthrough (v7 feature). Surfaces for downstream
        # tools so an FA pitcher with unlucky BABIP-against can be flagged
        # as a buy-low candidate without re-joining PlayerData.
        "PBABIP": (row.get("PBABIP") or "").strip(),
        "PotPBABIP": (row.get("PotPBABIP") or "").strip(),
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
    out["Ideal_Value"] = round(combined_r, 2)
    return out


def is_pitcher(row: Dict[str, str]) -> bool:
    pos = (row.get("Pos") or "").strip().upper()
    return pos in ("SP", "RP", "CL", "P")


# -----------------------------------------------------------------------------
# Output writers
# -----------------------------------------------------------------------------

def write_output_csv(
    rows: List[Dict[str, Any]],
    path: Path,
    draft_mode: bool = False,
    include_contracts: bool = False,
) -> None:
    if not rows:
        logger.warning("No rows to write")
        return
    cols = [
        "ID", "Name", "Pos", "Age", "Team", "Org", "League_Level",
        "VOS_Reach", "VOS_Career", "VOS_Blended", "VOS_Ceiling", "Ceiling_Tier",
        "Arch_Career_WAR", "Arch_Career_WAR_Hi",
        "Remaining_WAR", "Remaining_WAR_Hi", "Proj_Debut_Age",
        "VOS_Score", "VOS_Potential", "VOS_Tier", "VOS_Potential_Tier",
        "Batting_Score", "Batting_Potential", "Defense_Score", "Baserunning_Score",
        "Pitching_Ability_Score", "Pitching_Ability_Potential", "Pitching_Arsenal_Score",
        "Development_Adj", "Age_Adj", "Personality_Adj",
        "Park_Name", "Park_Applied", "Prone",
        "BABIP", "PotBABIP", "PBABIP", "PotPBABIP",
    ]
    if draft_mode:
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
    """Markdown summary for Obsidian. Shows all three v5 scores side by side."""
    md_cols = ["Name", "Pos", "Age", "Team", "Org", "League_Level",
               "VOS_Reach", "VOS_Career", "VOS_Blended"]

    def _row(r: Dict[str, Any]) -> str:
        cells = [str(r.get(c, "")) for c in md_cols]
        return "| " + " | ".join(cells) + " |"

    header = "| " + " | ".join(md_cols) + " |"
    sep = "| " + " | ".join("---" for _ in md_cols) + " |"

    mlb_rows = sorted(
        [r for r in rows if str(r.get("League_Level", "")).strip().upper() in ("MLB", "AAA")],
        key=lambda r: float(r.get("VOS_Career") or 0),
        reverse=True,
    )[:50]
    prospect_rows = sorted(
        [r for r in rows if str(r.get("League_Level", "")).strip().upper() not in ("MLB",)],
        key=lambda r: float(r.get("VOS_Reach") or 0),
        reverse=True,
    )[:75]

    lines: List[str] = [
        f"# Evaluation Summary — {league.upper()}  (v5)",
        "",
        f"_Generated from `{path.name.replace('.md', '.csv')}`._",
        "",
        "## Top MLB/AAA Players by VOS Career",
        "",
        header, sep,
    ]
    lines += [_row(r) for r in mlb_rows]
    lines += [
        "",
        "## Top Prospects by VOS Reach",
        "",
        header, sep,
    ]
    lines += [_row(r) for r in prospect_rows]
    lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")
    logger.info("Wrote evaluation summary MD: %s", path)


# -----------------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="VOS v5: two-track (Reach + Career + Blended) player evaluation.")
    parser.add_argument("--league", required=True, help="League slug (e.g. woba, sky)")
    parser.add_argument("--output", default=None, help="Output CSV path (default: evaluation_summary_{league}_{timestamp}.csv)")
    parser.add_argument("--ids-file", default=None, type=Path, help="Optional file of player IDs to include")
    parser.add_argument("--park-factors", default=None, type=str, help="Optional path to park-factors.json")
    parser.add_argument("--draft", action="store_true", help="Enable draft-specific adjustments (readiness, draft_age, draft_role)")
    parser.add_argument("--contracts", action="store_true", help="Include contract and contractextension API data in output")
    parser.add_argument("--base-url", default=None, type=str, help="Override league API base URL")
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR, help="Data directory")
    parser.add_argument("--config-dir", type=Path, default=DEFAULT_CONFIG_DIR, help="Config directory")
    parser.add_argument("--weights", type=Path, default=None,
                        help=f"Path to v5 weights JSON. Defaults to {{config-dir}}/{WEIGHTS_FILENAME}.")
    parser.add_argument(
        "--per-org-evals",
        action="store_true",
        help=(
            "When the park-factors file is in combined teams[] format, write one eval per team "
            "into {league}/eval/{team_code}/. Each per-team eval grades the WHOLE league through "
            "that team's park context (single-park mode). Useful for sharing team-specific evals."
        ),
    )
    parser.add_argument(
        "--rating-scale",
        choices=list(RATING_SCALES),
        default=DEFAULT_RATING_SCALE,
        help=(
            "Scale of the component ratings in PlayerData-{league}.csv. Default '20-80' "
            "matches weights_v6.json. Use '1-100' for leagues that export component ratings "
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

    try:
        cfg = load_weights(config_dir, args.weights)
    except ValueError as e:
        logger.error("%s", e)
        return 1
    if not cfg:
        weights_label = args.weights or (config_dir / WEIGHTS_FILENAME)
        logger.error("Weights config missing or invalid: %s", weights_label)
        return 1
    weights_used = args.weights if args.weights else (config_dir / WEIGHTS_FILENAME)
    logger.info("Using v5 weights file: %s", weights_used)

    league_lookup = load_id_maps(config_dir)
    teams = load_teams(config_dir, league)
    league_api_base_urls = load_league_api_base_urls(config_dir)
    park_factors = load_park_factors(args.park_factors)
    players = load_player_data(data_dir, league, id_filter,
                               rating_scale=args.rating_scale)
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
                league, config_dir / LEAGUE_URLS_FILENAME,
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
    # Match vos_v2 naming: draft runs get a different filename prefix so
    # consumers can tell at a glance which CSV they're looking at.
    out_prefix = "draft_evaluation" if draft_mode else "evaluation_summary"

    def _run_eval_pass(pass_park_factors: Optional[Dict[str, Any]],
                       out_path: Path) -> None:
        """Score every player against pass_park_factors and write the CSV/MD.
        Factored out so --per-org-evals can call it once per team-park."""
        out_path.parent.mkdir(parents=True, exist_ok=True)
        rows: List[Dict[str, Any]] = []
        for row in players:
            if is_pitcher(row):
                pos = (row.get("Pos") or "").strip().upper()
                role = "RP" if pos in ("RP", "CL") else "SP"
                out_row = build_pitcher_row(
                    row, cfg, league_lookup, teams,
                    role=role, park_factors=pass_park_factors, draft_mode=draft_mode,
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
                attach_contract_fields(out_row, contract_lookup.get(pid),
                                       extension_lookup.get(pid))

        write_output_csv(rows, out_path, draft_mode=draft_mode,
                         include_contracts=include_contracts)
        logger.info("Wrote %d rows to %s", len(rows), out_path)
        md_path = out_path.with_suffix(".md")
        _write_eval_summary_md(rows, md_path, league)

        # Sanity range checks on all three scores.
        for col in ("VOS_Reach", "VOS_Career", "VOS_Blended"):
            vals = [r[col] for r in rows if isinstance(r.get(col), (int, float))]
            if not vals:
                continue
            lo, hi = min(vals), max(vals)
            if lo < 20 or hi > 80:
                logger.warning("%s range [%.2f, %.2f] outside 20-80", col, lo, hi)
            else:
                logger.info("%s range [%.2f, %.2f] (within 20-80)", col, lo, hi)

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
