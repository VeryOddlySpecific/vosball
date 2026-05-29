"""Per-tool age-decay helper for VOS v5 Career mode.

Reads peak_age and decay_per_year from the v5 weights schema's age_decay
block; applies linear post-peak decay to a single rating value. Pre-peak
ratings (player_age <= peak_age) pass through unchanged. Tools with no
configured peak_age or decay_per_year also pass through unchanged so the
helper is safe to call on any rating regardless of whether decay was
calibrated for it.

Used only in Career mode. Reach mode operates on Pot* ratings, which
already encode ceiling at evaluation time, so decay would double-count.
"""

from __future__ import annotations

from typing import Any, Dict, Optional


def apply_age_decay(
    raw_value: Optional[float],
    tool: str,
    age: Optional[float],
    age_decay_cfg: Optional[Dict[str, Any]],
    floor: float = 20.0,
) -> Optional[float]:
    """Apply linear age-decay to a single rating value.

    Args:
        raw_value: The rating value to decay (e.g. 55.0 for a 55 Pow). None
            passes through.
        tool: The tool name as it appears in age_decay.peak_age / decay_per_year
            (e.g. "Pow", "OFR", "Stf"). Must match the JSON keys.
        age: Player's age. None passes through (treats player as pre-peak for
            all tools — safest default when age data is missing).
        age_decay_cfg: The age_decay block from the v5 weights JSON. None or
            empty passes through.
        floor: Lower bound for the decayed value. Default 20.0 matches the
            normalization hard_floor in the v5 schema.

    Returns:
        max(raw_value + slope * years_past_peak, floor) when all inputs are
        present and the player is post-peak; raw_value otherwise.
    """
    if raw_value is None or age is None or not age_decay_cfg:
        return raw_value
    peak_map = age_decay_cfg.get("peak_age") or {}
    slope_map = age_decay_cfg.get("decay_per_year") or {}
    peak = peak_map.get(tool)
    slope = slope_map.get(tool)
    if peak is None or slope is None:
        return raw_value
    try:
        years_past = max(float(age) - float(peak), 0.0)
        slope_f = float(slope)
    except (TypeError, ValueError):
        return raw_value
    if years_past <= 0 or slope_f == 0:
        return raw_value
    decayed = raw_value + slope_f * years_past
    return max(decayed, floor)
