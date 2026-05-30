"""vosball.engine.reach — the v6 logistic Reach model.

Extracts the feature vector and computes the sigmoid-mapped 20-80 Reach score. Schema must match analysis/fit_reach_v6.py. Lifted verbatim from core.py."""
from __future__ import annotations

import math
from typing import Any, Dict, List

from vosball.engine.rows import resolve_float
from vosball.engine.constants import PRONE_CATEGORY_TO_NUMERIC


__all__ = [
    '_v6_extract_features',
    '_v6_reach_score',
    '_has_v6_reach',
]


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
