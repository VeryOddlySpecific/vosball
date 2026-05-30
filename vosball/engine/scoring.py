"""vosball.engine.scoring — per-tool hitter/pitcher scoring.

Weighted batting/defense/baserunning and pitcher ability/arsenal/combined scores, plus the per-position composite. Pure; no I/O. Lifted verbatim from core.py."""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Set, Tuple

from vosball.engine.rows import resolve_float
from vosball.engine.normalization import _normalization_params
from vosball.engine.constants import (
    HITTER_POSITIONS, POT_PITCH_COLUMN_TO_TYPE, PITCH_SPEED_TIERS,
    PITCH_BREAK_PLANES,
)
from vosball.engine.context import (
    _mode_block, _decay_cfg_for_mode, _decay_tool_for_key, _resolve_with_decay,
)
from vosball.engine.park import apply_park_adjustments


__all__ = [
    '_weighted_sum_from_dict',
    'hitter_batting_score',
    'hitter_defense_score',
    'hitter_baserunning_score',
    'hitter_position_scores',
    'pitcher_ability_score',
    'pitcher_arsenal_score',
    'pitcher_combined_score',
]


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
