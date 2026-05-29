"""VOS tier classification — map a 20-80 score onto a human-readable label.

Ported verbatim from run_vos.py (Phase 1 extraction). v6 Reach is a
logistic-mapped probability rather than the v2 Pot*-weighted composite, so the
same band may carry different distribution mass — recalibration against the v6
Reach distribution is a follow-up. Default bands are kept identical to vos_v2 so
consumers reading VOS_Tier / VOS_Potential_Tier see the same labels.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

_DEFAULT_HITTER_TIERS: List[Dict[str, Any]] = [
    {"min": 65.0, "label": "Star"},
    {"min": 58.0, "label": "Above-Avg Regular"},
    {"min": 52.0, "label": "Reliable Starter"},
    {"min": 47.0, "label": "Fringe Regular"},
    {"min": 42.0, "label": "Bench"},
    {"min": 37.0, "label": "Replacement"},
    {"min": 0.0,  "label": "Org Filler"},
]
_DEFAULT_PITCHER_TIERS: List[Dict[str, Any]] = [
    {"min": 58.0, "label": "Ace"},
    {"min": 51.0, "label": "#2/#3 Starter"},
    {"min": 46.0, "label": "Mid-Rotation"},
    {"min": 41.0, "label": "Back-End / Setup"},
    {"min": 36.0, "label": "Long Relief / Swing"},
    {"min": 31.0, "label": "Replacement"},
    {"min": 0.0,  "label": "Org Filler"},
]


def _resolve_tier_bands(cfg: Optional[Dict[str, Any]], role: str) -> List[Dict[str, Any]]:
    """Return tier bands for 'hitter' or 'pitcher' from cfg, falling back to defaults."""
    role_key = "pitcher" if role == "pitcher" else "hitter"
    tiers_cfg = (cfg or {}).get("tiers") or {}
    bands = tiers_cfg.get(role_key)
    if not isinstance(bands, list) or not bands:
        return _DEFAULT_PITCHER_TIERS if role_key == "pitcher" else _DEFAULT_HITTER_TIERS
    return bands


def classify_vos_tier(
    score: Any,
    role: str = "hitter",
    cfg: Optional[Dict[str, Any]] = None,
) -> str:
    """Map a numeric VOS-style score to a role-aware tier label.

    role: 'hitter' for batting/positional/hitter-overall scores, 'pitcher' for SP/RP
    or pitcher-overall scores. Bands are read from cfg['tiers'][role] (top-down,
    first band whose 'min' is <= score wins). Returns empty string for missing /
    non-numeric scores so downstream display can stay clean.
    """
    try:
        if score is None or score == "":
            return ""
        val = float(score)
    except (TypeError, ValueError):
        return ""
    bands = _resolve_tier_bands(cfg, role)
    sorted_bands = sorted(bands, key=lambda b: float(b.get("min", 0.0)), reverse=True)
    for band in sorted_bands:
        try:
            if val >= float(band.get("min", 0.0)):
                return str(band.get("label", ""))
        except (TypeError, ValueError):
            continue
    return ""


def tier_for_player_role(row_or_pos: Any) -> str:
    """Return 'pitcher' or 'hitter' given either a Pos string or a dict-like row."""
    if isinstance(row_or_pos, dict):
        pos = (row_or_pos.get("Pos") or "").strip().upper()
    else:
        pos = (str(row_or_pos or "")).strip().upper()
    return "pitcher" if pos in ("SP", "RP", "CL", "P") else "hitter"
