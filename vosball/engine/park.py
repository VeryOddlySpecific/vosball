"""vosball.engine.park — park-factor selection and tool adjustment.

Resolves the park config that applies to a player and scales tool scores by it. Pure; no I/O. Lifted verbatim from core.py."""
from __future__ import annotations

from typing import Any, Dict, Optional

from vosball.engine.rows import resolve_int
from vosball.engine.context import get_league_label, get_team_display


__all__ = [
    '_is_single_park_format',
    '_build_single_park_config',
    'get_player_park_config',
    'apply_park_adjustments',
]


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
