"""vosball.data.config — config/weights/map JSON loaders.

Weights (with v5/v6 schema validation), league-level id maps, team maps, and league API base-URL maps. Path-agnostic; takes explicit dirs. Lifted verbatim from loaders.py."""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


__all__ = [
    'WEIGHTS_FILENAME',
    'ID_MAPS_FILENAME',
    'LEAGUE_URLS_FILENAME',
    'TEAMS_FILENAME_TEMPLATE',
    'load_json',
    '_validate_v5_schema',
    'load_weights',
    'load_id_maps',
    'load_teams',
    'load_league_api_base_urls',
]


WEIGHTS_FILENAME = "weights_v10.json"


ID_MAPS_FILENAME = "id_maps.json"


LEAGUE_URLS_FILENAME = "league_url.json"


TEAMS_FILENAME_TEMPLATE = "teams-{league}.json"


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
