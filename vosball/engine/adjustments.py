"""vosball.engine.adjustments — the development/readiness/age/personality/draft adjustment stack.

Scalar bonuses/penalties layered onto the raw composite before normalization. Pure; no I/O. Lifted verbatim from core.py."""
from __future__ import annotations

from typing import Any, Dict, Optional

from vosball.engine.rows import resolve_float
from vosball.engine.constants import HITTER_POSITIONS, PERSONALITY_CSV_TO_CONFIG
from vosball.engine.context import get_league_key_for_config


__all__ = [
    '_development_dampener',
    'development_adjustment',
    'readiness_adjustment_pitcher',
    'readiness_adjustment_hitter',
    'draft_age_modifier',
    'draft_role_penalty',
    'age_adjustment',
    '_personality_bucket_from_cell',
    'personality_adjustment',
    '_blend_alpha',
]


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


def _blend_alpha(cfg: Dict[str, Any]) -> float:
    blend = (cfg.get("scoring_modes") or {}).get("blend") or {}
    try:
        return float(blend.get("alpha", 0.4))
    except (TypeError, ValueError):
        return 0.4
