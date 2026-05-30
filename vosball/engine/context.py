"""vosball.engine.context — league/team labels, scoring-mode blocks, and age-decay resolution.

Small config-reading helpers shared by the scoring and assembly layers: mode/decay block selection and the decay-aware rating lookup. Pure; no I/O. Lifted verbatim from core.py."""
from __future__ import annotations

from typing import Any, Dict, Optional

from lib.vos_decay import apply_age_decay
from vosball.engine.rows import resolve_float
from vosball.engine.constants import LEVEL_LABEL_TO_CONFIG


__all__ = [
    'get_league_label',
    'get_league_key_for_config',
    'get_team_display',
    '_mode_block',
    '_has_ceiling',
    '_decay_cfg_for_mode',
    '_resolve_with_decay',
    '_decay_tool_for_key',
]


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
        merged = dict(career)
        # Hitter ceiling: swap the batting tool_categories to the Pot*-keyed weights.
        ceil_bat = (((ceil.get("hitters") or {}).get("tool_categories") or {})
                    .get("batting"))
        if ceil_bat:
            career_h = career.get("hitters") or {}
            merged_tc = dict(career_h.get("tool_categories") or {})
            merged_tc["batting"] = ceil_bat
            merged_h = dict(career_h)
            merged_h["tool_categories"] = merged_tc
            merged["hitters"] = merged_h
        # Pitcher ceiling: swap ability_weights to the Pot*-keyed weights (per
        # role). role_balance / arsenal stay inherited from career. Mirrors the
        # hitter-batting swap; only populated since the vos_ceiling.pitchers block
        # was added, so the hitter ceiling path is unchanged.
        ceil_pit_aw = (ceil.get("pitchers") or {}).get("ability_weights")
        if ceil_pit_aw:
            career_p = career.get("pitchers") or {}
            merged_p = dict(career_p)
            merged_p["ability_weights"] = ceil_pit_aw
            merged["pitchers"] = merged_p
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
