"""VOS score normalization: map a raw composite onto the 20-80 scale.

A soft-saturating sigmoid centered on `center` with linear-ish behavior near
the middle and asymptotes at `floor`/`ceiling`. Ported verbatim from run_vos.py
(Phase 1 extraction) — no behavior change.
"""
from __future__ import annotations

from typing import Any, Dict, Tuple


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
