"""Draft_Outlook — Career-weighted composite on Pot* ratings, adjusted for
draft-mode signals, normalized to 20-80.

# What this answers

Draft prospects pose a question no existing VOS score quite answers:
*"If this amateur realizes their ceiling, how good will they be as an MLB
player?"*

Existing v10 scores answer adjacent but-different questions:

- `VOS_Reach`: P(reach MLB). Uses Pot* but tuned for Stage-1 AUC.
- `VOS_Career`: WAR | MLB. Uses CURRENT ratings — meaningless for an
  18-year-old whose current Cntct is 25/80.
- `VOS_Blended`: α·Reach + (1-α)·Career. Inherits Career's current-rating
  problem for amateurs.
- `Ideal_Value`: heuristic Reach composite at best position (tuned for
  Stage-1 AUC, not WAR projection).

The hole: nothing combines Career's WAR-tuned weight structure with Pot*'s
ceiling inputs. `Draft_Outlook` fills it.

# Construction

For each player:

1. **Batting** (hitters): Career batting weights (`Gap:0.4, Pow:0.1,
   Eye:0.1, Ks:0.4` — Stage-2 Spearman tuned) applied to `PotGap, PotPow,
   PotEye, PotKs` instead of current ratings. `Cntct` has no Pot*
   equivalent and isn't in Career batting weights anyway.
2. **Defense** (hitters): unchanged from Career — defensive ratings have
   no Pot* counterparts in OOTP.
3. **Baserunning** (hitters): unchanged from Career — same reason.
4. **Ability** (pitchers): Career ability weights (`Stf:0.1, Mov:0.3,
   Ctrl:0.35, HRA:0.25`) applied to `PotStf, PotMov, PotCtrl, PotHRA`.
5. **Arsenal** (pitchers): unchanged — arsenal evaluation already uses
   Pot* pitch ratings in both Reach and Career modes (mode-independent).
6. **Combine** via Career `position_category_weights` / `role_balance`
   at best position (DH margin rule applies, same as run_vos).
7. **Adjust** with the v10 draft-mode adjustment stack:
   - `personality_adjustment` (v10 recalibrated: Lead=0, WrkEthic=±3)
   - `draft_age_modifier` (-1.5 at 17 → +1.5 at 22)
   - `readiness_adjustment_{hitter,pitcher}` (tools-vs-MLB-readiness)
   - **Not** added: `age_vs_level` (irrelevant for amateurs) or
     `dev_adj` (no track record vs potential yet).
8. **Normalize** to 20-80 via the same sigmoid as other VOS scores.

# Why the Career weight structure is the right thing to reuse

`career_logistic_followup.md` in the OOTP Study 27 project validates that
the heuristic Career composite outperforms a flat-feature ridge regression
on Stage-2 Spearman (+0.213 vs +0.116/+0.188/+0.009 for the ridge variants).
The two-level position-aware structure provides regularization that an
individual-tool model can't match at the available sample size.

That's the structure being repurposed here. The empirical validation
applies: Draft_Outlook inherits a tested weight schema.

# Implementation note: helpers reused, not duplicated

This module is intentionally thin. It constructs "translated" weight dicts
(swapping Career current-rating keys for their Pot* equivalents) and then
calls `run_vos.py`'s existing scoring helpers. If v10 weights change,
Draft_Outlook tracks automatically.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, Optional

SCRIPT_DIR = Path(__file__).resolve().parent
# lib/ is a sibling of run_vos.py in the deployed layout; add parent to
# sys.path so we can import the engine's helpers without duplicating them.
_ratings_root = SCRIPT_DIR.parent
if str(_ratings_root) not in sys.path:
    sys.path.insert(0, str(_ratings_root))

from vosball import engine as run_vos  # noqa: E402 — engine helpers; needs sys.path fix above


# ---------------------------------------------------------------------------
# Translation maps — Career weight keys → Pot* equivalents
# ---------------------------------------------------------------------------
# When a key has no Pot* counterpart (Cntct for hitters; nothing for pitcher
# ability), it stays unchanged. The Career batting weight block doesn't
# include Cntct anyway, but if a future v-version adds it the translation
# falls through cleanly to the current rating.

CAREER_BATTING_TO_POT: Dict[str, str] = {
    "Gap": "PotGap",
    "Pow": "PotPow",
    "Eye": "PotEye",
    "Ks": "PotKs",
}

CAREER_PITCHER_ABILITY_TO_POT: Dict[str, str] = {
    "Stf": "PotStf",
    "Mov": "PotMov",
    "Ctrl": "PotCtrl",
    "HRA": "PotHRA",
}


def _translate_weights(weights: Dict[str, Any],
                        translation: Dict[str, str]) -> Dict[str, float]:
    """Swap current-rating keys for Pot* equivalents. Keys not in the
    translation map (including `_comment`-style annotations) flow through
    unchanged; the underscore-key skip happens inside the scoring helpers
    themselves, so we just preserve them here.
    """
    out: Dict[str, float] = {}
    for k, v in weights.items():
        if k.startswith("_"):
            continue
        try:
            w = float(v)
        except (TypeError, ValueError):
            continue
        out[translation.get(k, k)] = w
    return out


# ---------------------------------------------------------------------------
# DH-routing policy — varies between draft (strict) and roster (default).
# ---------------------------------------------------------------------------
# The default v10 rule (cfg's dh_assignment block) is calibrated for MLB
# roster construction: it sends a player to DH when their bat exceeds their
# best field position by ≥3 points OR no field position is "viable" (≥50).
#
# For amateur draft prospects this is too generous:
#   - Amateur defense ratings are uniformly suppressed because defense
#     develops with experience. The 50-floor for "viable field" is an
#     MLB-readiness threshold, not a ceiling-projection threshold.
#   - A 3-point bat-over-field margin is small enough that a 60-IF / 63-DH
#     amateur — clearly a developable infielder — gets the DH tag.
#   - DH-labeling at the draft locks roster flexibility and signals "we
#     gave up on his glove" too early in the development arc.
#
# The "draft_strict" policy below applies a compound rule: DH wins only
# when (a) the bat dominates by a meaningful margin, OR (b) no field
# position is even close to viable AND the bat is plus-grade.

_DH_POLICIES = ("default", "draft_strict", "never")


def _select_ideal_position(
    pos_scores: Dict[str, Optional[float]],
    bat: float,
    cfg_dh_assignment: Dict[str, Any],
    *,
    dh_policy: str = "draft_strict",
    dh_field_floor: float = 45.0,
    dh_bat_floor: float = 55.0,
    dh_min_margin: float = 8.0,
) -> Tuple[str, float, str]:
    """Pick the player's projected position based on per-position composites.

    Args:
        pos_scores: per-position composite. DH entry is the bat-only score.
            None values indicate the player doesn't meet positional standards.
        bat: pure batting composite (= DH score). Surfaced separately so the
            policy can reason about bat-vs-field margins.
        cfg_dh_assignment: cfg["hitters"]["dh_assignment"] block; only read
            by the "default" policy.
        dh_policy: one of `_DH_POLICIES`. See module docstring above.
            - "default"      mirrors run_vos.hitter_position_scores exactly.
            - "draft_strict" applies the compound rule (default for drafts).
            - "never"        always picks best field position; DH only used
                              as last resort when no field standards met.
        dh_field_floor: under "draft_strict", best-field score below this
            counts as "field unrescuable" and unlocks the DH route.
        dh_bat_floor: under "draft_strict", bat score must reach this for DH
            to be allowed even when field is unrescuable.
        dh_min_margin: under "draft_strict", bat-over-field margin at or
            above this routes to DH regardless of field viability.

    Returns:
        (position, score, reason) — the routing reason is a short tag like
        "field_max", "field_routed", "dh_bat_dominates", "dh_no_viable_field",
        or "dh_unrescuable_with_elite_bat", useful for downstream reports
        that want to explain WHY a player got their projected position.
    """
    # Find best field position from viable scores
    best_field_pos: Optional[str] = None
    best_field_value: Optional[float] = None
    for pos in run_vos.HITTER_POSITIONS:
        if pos == "DH":
            continue
        s = pos_scores.get(pos)
        if s is None:
            continue
        if best_field_value is None or s > best_field_value:
            best_field_value = s
            best_field_pos = pos

    # No viable field position at all → DH is the only choice, regardless of
    # policy. (Could happen for a hitter who misses every positional standard
    # — exotic but possible at extreme defense levels.)
    if best_field_pos is None or best_field_value is None:
        return "DH", bat, "dh_no_viable_field"

    # If a field position scores higher than bat, it wins under every policy
    # (the standard "max over positions" outcome). No DH consideration needed.
    if best_field_value > bat:
        return best_field_pos, best_field_value, "field_max"

    # Otherwise DH is the natural max (bat ≥ best_field). Apply the policy
    # to decide whether to route to field anyway.

    if dh_policy == "never":
        return best_field_pos, best_field_value, "field_routed"

    if dh_policy == "draft_strict":
        margin = bat - best_field_value
        # (a) Bat dominates field by a wide margin — DH wins regardless of
        #     how viable the field position is. Catches the rare true
        #     bat-only profile where defense is irrelevant.
        if margin >= dh_min_margin:
            return "DH", bat, "dh_bat_dominates"
        # (b) Field unrescuable AND bat is at least plus-grade — DH wins.
        #     A 35-OF / 60-DH profile genuinely projects as DH at this age.
        if best_field_value < dh_field_floor and bat >= dh_bat_floor:
            return "DH", bat, "dh_unrescuable_with_elite_bat"
        # (c) Otherwise route to field. Preserves position eligibility for
        #     developable defenders even when bat is the higher tool.
        return best_field_pos, best_field_value, "field_routed"

    # "default" — mirror run_vos's existing behavior verbatim. Used when
    # this helper is called from non-draft contexts.
    min_field_quality = float(cfg_dh_assignment.get("min_field_quality", 0.0) or 0.0)
    min_dh_margin = float(cfg_dh_assignment.get("min_dh_margin_over_field", 0.0) or 0.0)
    if (best_field_value >= min_field_quality
            and (bat - best_field_value) < min_dh_margin):
        return best_field_pos, best_field_value, "field_routed"
    return "DH", bat, "dh_default_margin"


# ---------------------------------------------------------------------------
# Hitter Draft_Outlook
# ---------------------------------------------------------------------------

def compute_hitter_outlook(
    row: Dict[str, str],
    cfg: Dict[str, Any],
    age: Optional[float],
    *,
    dh_policy: str = "draft_strict",
    dh_field_floor: float = 45.0,
    dh_bat_floor: float = 55.0,
    dh_min_margin: float = 8.0,
) -> Dict[str, Any]:
    """Returns dict with composite + breakdown for one hitter.

    DH-routing policy defaults to "draft_strict" — see
    `_select_ideal_position` for the rule. Callers grading non-draft
    contexts should pass dh_policy="default" to mirror run_vos behavior.

    Keys:
        composite          — raw best-position score (pre-adjustment, pre-normalize)
        ideal_pos          — position label producing the composite
        ideal_reason       — short tag explaining why this position was picked
                              (field_max, field_routed, dh_bat_dominates,
                               dh_no_viable_field, dh_unrescuable_with_elite_bat,
                               dh_default_margin)
        batting            — translated-Pot* batting composite
        baserunning        — Career-weighted baserunning composite (current ratings)
        defense_avg        — average defense composite across viable positions
        pos_scores         — per-position composite (or None where standards fail)
    """
    career_h = (cfg.get("scoring_modes", {}).get("vos_career", {})
                .get("hitters", {}))
    tool_cats = career_h.get("tool_categories", {})
    pos_cat_weights = career_h.get("position_category_weights", {})

    bat_weights = _translate_weights(
        tool_cats.get("batting", {}), CAREER_BATTING_TO_POT,
    )
    # Defense and baserunning have no Pot* equivalents in OOTP. Use Career
    # weights on current ratings.
    base_weights = _translate_weights(
        tool_cats.get("baserunning", {}), {},  # identity map → no rename
    )
    def_weights_by_pos = tool_cats.get("defense", {})

    standards = (cfg.get("hitters") or {}).get("positional_standards") or {}
    dh_cfg = (cfg.get("hitters") or {}).get("dh_assignment") or {}
    _, _, floor, _ = run_vos._normalization_params(cfg)

    # Batting (Pot*-fed). Baserunning (current-fed). decay_cfg=None across
    # the board: Pot* values shouldn't decay, and decaying current
    # baserunning for an amateur doesn't make sense (they're all in the
    # same young age band).
    bat = run_vos.hitter_batting_score(
        row, bat_weights, age, decay_cfg=None, floor=floor,
    ) or 0.0
    base = run_vos.hitter_baserunning_score(
        row, base_weights, age, decay_cfg=None, floor=floor,
    ) or 0.0

    pos_scores: Dict[str, Optional[float]] = {}
    def_sum = 0.0
    def_count = 0
    for pos in run_vos.HITTER_POSITIONS:
        if pos == "DH":
            # DH inherits the batting composite (no defense to score).
            pos_scores[pos] = bat
            continue
        def_w = def_weights_by_pos.get(pos)
        std = standards.get(pos, {})
        def_score = (
            run_vos.hitter_defense_score(
                row, pos, def_w or {}, std, age, decay_cfg=None, floor=floor,
            )
            if def_w else None
        )
        if def_score is None:
            # Position not viable (standards not met or no weight block).
            pos_scores[pos] = None
            continue
        def_sum += def_score
        def_count += 1
        cat_w = pos_cat_weights.get(pos, {})
        if not cat_w:
            # Defensible default: defense-only score (rare edge case).
            pos_scores[pos] = def_score
            continue
        bw = float(cat_w.get("batting", 0.0))
        dw = float(cat_w.get("defense", 0.0))
        rw = float(cat_w.get("baserunning", 0.0))
        pos_scores[pos] = bat * bw + def_score * dw + base * rw

    # Best position selection — policy-driven (see _select_ideal_position).
    # For draft contexts, "draft_strict" tightens DH routing so amateurs
    # don't get DH'd just because their developing defense ratings fall
    # below the MLB-readiness floor.
    ideal_pos, ideal_value, ideal_reason = _select_ideal_position(
        pos_scores, bat, dh_cfg,
        dh_policy=dh_policy,
        dh_field_floor=dh_field_floor,
        dh_bat_floor=dh_bat_floor,
        dh_min_margin=dh_min_margin,
    )

    return {
        "composite": ideal_value,
        "ideal_pos": ideal_pos,
        "ideal_reason": ideal_reason,
        "batting": bat,
        "baserunning": base,
        "defense_avg": def_sum / def_count if def_count else 0.0,
        "pos_scores": pos_scores,
    }


# ---------------------------------------------------------------------------
# Pitcher Draft_Outlook
# ---------------------------------------------------------------------------

def compute_pitcher_outlook(
    row: Dict[str, str],
    cfg: Dict[str, Any],
    age: Optional[float],
    role: str = "SP",
    *,
    apply_stamina_penalty: bool = False,
) -> Dict[str, Any]:
    """Returns dict with composite + breakdown for one pitcher.

    Keys:
        composite          — raw role-aggregated score (pre-adjustment)
        ability            — translated-Pot* ability composite
        arsenal_raw        — pitcher_arsenal_score raw value
        arsenal_diversity  — diversity bonus/penalty applied on top of raw
        stamina_penalty    — SP-only stamina-floor penalty (0 by default)

    apply_stamina_penalty defaults to False because the SP stamina floor
    is calibrated for current-MLB viability (`minimum_stamina=50` etc.) —
    that bar is unreasonable for amateur draft prospects, every one of
    whom has raw Stm < 50 because Stm develops with workload. A 22-year-
    old SP with elite arsenal Pot* and Stm=30 should not be tanked by a
    20-point penalty for "not being ready today" — the entire point of
    Draft_Outlook is ceiling projection. Set this to True if you
    specifically want the readiness penalty layered on top.
    """
    career_p = (cfg.get("scoring_modes", {}).get("vos_career", {})
                .get("pitchers", {}))
    ability_weights_raw = career_p.get("ability_weights", {}).get(role, {})
    role_balance = career_p.get("role_balance", {}).get(role, {})

    ability_weights = _translate_weights(
        ability_weights_raw, CAREER_PITCHER_ABILITY_TO_POT,
    )
    _, _, floor, _ = run_vos._normalization_params(cfg)

    ability = run_vos.pitcher_ability_score(
        row, ability_weights, age, decay_cfg=None, floor=floor,
    ) or 0.0
    # Arsenal scoring is mode-independent in run_vos — uses Pot* pitch
    # columns in both Reach and Career. Reuse directly.
    arsenal_raw, diversity_adj = run_vos.pitcher_arsenal_score(row, role, cfg)
    arsenal = arsenal_raw + diversity_adj

    ab_w = float(role_balance.get("ability_weight", 0.85))
    ar_w = float(role_balance.get("arsenal_weight", 0.15))
    combined = ability * ab_w + arsenal * ar_w

    # Stamina-floor penalty for SP (mirrors pitcher_combined_score). Off by
    # default — see the apply_stamina_penalty docstring above. When enabled,
    # the math is identical to run_vos.pitcher_combined_score.
    stamina_penalty = 0.0
    if role == "SP" and apply_stamina_penalty:
        stam_cfg = (((cfg.get("pitchers") or {}).get("stamina_requirements")
                    or {}).get("SP", {}))
        if stam_cfg:
            min_sta = float(stam_cfg.get("minimum_stamina", 50))
            per_pt = float(stam_cfg.get("penalty_per_point_below", 0.5))
            sta = run_vos.resolve_float(row, "Stm")
            if sta is not None and sta < min_sta:
                stamina_penalty = (min_sta - sta) * per_pt

    composite = combined - stamina_penalty

    return {
        "composite": composite,
        "ability": ability,
        "arsenal_raw": arsenal_raw,
        "arsenal_diversity": diversity_adj,
        "stamina_penalty": stamina_penalty,
    }


# ---------------------------------------------------------------------------
# Top-level: assemble Draft_Outlook + adjustments + normalize
# ---------------------------------------------------------------------------

def compute_draft_outlook(
    row: Dict[str, str],
    cfg: Dict[str, Any],
    *,
    role: Optional[str] = None,
    dh_policy: str = "draft_strict",
    dh_field_floor: float = 45.0,
    dh_bat_floor: float = 55.0,
    dh_min_margin: float = 8.0,
) -> Dict[str, Any]:
    """Full Draft_Outlook pipeline for one PlayerData row.

    Returns dict:
        draft_outlook       — final 20-80 score
        composite           — raw composite (pre-adjustment)
        composite_normalized— composite alone, normalized 20-80 (no adjustments)
        ideal_pos           — best position (hitters); role label (pitchers)
        breakdown           — per-component scores (batting / def_avg / etc.)
        adjustments         — dict of applied adjustments (pers / draft_age / readiness)

    role: 'SP' / 'RP' to force a pitcher role. None infers from Pos ('SP',
    'RP', 'CL' → P; otherwise → hitter).

    dh_policy: routing rule for the DH-vs-field decision (hitters only —
    pitchers ignore). Default "draft_strict" applies the compound rule
    appropriate for amateur draft prospects. Pass "default" to mirror
    run_vos behavior, "never" to disable DH routing entirely. See
    `_select_ideal_position` for the exact rule.

    The score is built to be comparable to VOS_Reach / VOS_Career /
    VOS_Blended (same 20-80 scale, same sigmoid). The adjustments mirror
    the run_vos --draft adjustment stack but skip dev_adj and age_vs_level
    which don't make sense for amateurs.
    """
    age = run_vos.resolve_float(row, "Age")
    is_pit = run_vos.is_pitcher(row) if role is None else (role in ("SP", "RP", "CL"))

    if is_pit:
        # Infer SP vs RP from Pos if role wasn't passed.
        if role is None:
            pos = (row.get("Pos") or "").strip().upper()
            role = "RP" if pos in ("RP", "CL") else "SP"
        outlook = compute_pitcher_outlook(row, cfg, age, role=role)
        ideal_pos = role
        readiness_adj = run_vos.readiness_adjustment_pitcher(row, cfg)
    else:
        outlook = compute_hitter_outlook(
            row, cfg, age,
            dh_policy=dh_policy,
            dh_field_floor=dh_field_floor,
            dh_bat_floor=dh_bat_floor,
            dh_min_margin=dh_min_margin,
        )
        ideal_pos = outlook["ideal_pos"]
        readiness_adj = run_vos.readiness_adjustment_hitter(row, cfg)

    composite = float(outlook["composite"])

    # Adjustment stack — v10 draft-mode signals, no dev_adj / age_vs_level.
    pers_adj = run_vos.personality_adjustment(row, cfg)
    draft_age_adj = run_vos.draft_age_modifier(age) if age is not None else 0.0

    raw_total = composite + pers_adj + draft_age_adj + readiness_adj

    center, scale, floor, ceiling = run_vos._normalization_params(cfg)
    # `composite_normalized` shows what the score looks like without any
    # of the v10 adjustments — useful for the draft tools to surface
    # "tool-only" vs "adjusted" views side-by-side.
    composite_normalized = run_vos.normalize_to_20_80(
        composite, center, scale, floor, ceiling,
    )
    draft_outlook = run_vos.normalize_to_20_80(
        raw_total, center, scale, floor, ceiling,
    )

    return {
        "draft_outlook": round(draft_outlook, 2),
        "composite": round(composite, 2),
        "composite_normalized": round(composite_normalized, 2),
        "ideal_pos": ideal_pos,
        "is_pitcher": is_pit,
        "breakdown": outlook,
        "adjustments": {
            "personality_adj": round(pers_adj, 2),
            "draft_age_adj": round(draft_age_adj, 2),
            "readiness_adj": round(readiness_adj, 2),
        },
    }
