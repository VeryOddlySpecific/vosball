"""vosball.engine.core — row assembly (the VOS integrators).

build_hitter_row / build_pitcher_row pull together the scoring, adjustment,
park, reach, and WAR layers into the final output dicts; is_pitcher routes
rows. The per-layer logic now lives in sibling submodules (context, park,
reach, scoring, adjustments, war), re-exported via vosball.engine. No file or
network I/O. Output is byte-identical to the pre-split run_vos.py (guarded by
tests/test_golden.py)."""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

from vosball.engine.constants import HITTER_POSITIONS
from vosball.engine.rows import resolve_float, resolve_int
from vosball.engine.normalization import normalize_to_20_80, _normalization_params
from vosball.engine.tiers import classify_vos_tier
from vosball.engine.context import get_league_label, get_team_display, _has_ceiling
from vosball.engine.park import get_player_park_config
from vosball.engine.reach import _has_v6_reach, _v6_reach_score
from vosball.engine.scoring import hitter_position_scores, pitcher_combined_score
from vosball.engine.adjustments import (
    development_adjustment, _development_dampener, age_adjustment,
    personality_adjustment, draft_age_modifier, draft_role_penalty,
    readiness_adjustment_hitter, readiness_adjustment_pitcher, _blend_alpha,
)
from vosball.engine.war import project_archetype_war, _classify_ceiling_tier

logger = logging.getLogger(__name__)


__all__ = [
    'build_hitter_row',
    'build_pitcher_row',
    'is_pitcher',
]


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
