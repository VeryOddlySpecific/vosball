"""vosball.data.loaders — file/network loaders + their constants, lifted
from run_vos.py.

PlayerData CSV, weights/config JSON, id/team maps, StatsPlus contract
endpoints, park factors, and the 1-100 -> 20-80 rating-scale conversion.
Path-agnostic (takes explicit dirs/paths/URLs); no scoring and no app path
defaults. Lifted verbatim in the Phase 2 extraction — output unchanged,
guarded by tests/test_golden.py.
"""
from __future__ import annotations

import csv
import json
import logging
from io import StringIO
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.request import urlopen

logger = logging.getLogger(__name__)

__all__ = ['_build_contract_lookup', '_fetch_csv_endpoint', '_season_year_value', '_validate_v5_schema', 'apply_rating_scale_to_row', 'attach_contract_fields', 'convert_1_100_to_20_80', 'get_league_base_url', 'load_contract_data', 'load_id_filter', 'load_id_maps', 'load_json', 'load_league_api_base_urls', 'load_park_factors', 'load_player_data', 'load_teams', 'load_weights', 'CONTRACT_FIELDS', 'DEFAULT_LEAGUE_API_BASE_URLS', 'DEFAULT_RATING_SCALE', 'ID_MAPS_FILENAME', 'LEAGUE_URLS_FILENAME', 'PLAYER_DATA_FILENAME_TEMPLATE', 'RATING_COLUMNS', 'RATING_SCALES', 'TEAMS_FILENAME_TEMPLATE', 'WEIGHTS_FILENAME']


WEIGHTS_FILENAME = "weights_v10.json"


ID_MAPS_FILENAME = "id_maps.json"


LEAGUE_URLS_FILENAME = "league_url.json"


TEAMS_FILENAME_TEMPLATE = "teams-{league}.json"


PLAYER_DATA_FILENAME_TEMPLATE = "PlayerData-{league}.csv"


DEFAULT_LEAGUE_API_BASE_URLS: Dict[str, str] = {}


RATING_SCALES = ("20-80", "1-100")


DEFAULT_RATING_SCALE = "20-80"


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
