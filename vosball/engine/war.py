"""vosball.engine.war — archetype WAR projection and ceiling tiers.

Ballpark career-WAR projection (averages, age-tied) and ceiling-tier classification via 1-D interpolation. Pure; no I/O. Lifted verbatim from core.py."""
from __future__ import annotations

from typing import Any, Dict, Optional, Sequence


__all__ = [
    '_interp1d',
    'project_archetype_war',
    '_ceiling_tier_band',
    '_classify_ceiling_tier',
]


def _interp1d(x: float, xs: Sequence[float], ys: Sequence[float]) -> float:
    """Linear interpolation of x against ascending xs -> ys, clamped at ends."""
    xs = [float(v) for v in xs]
    ys = [float(v) for v in ys]
    if x <= xs[0]:
        return ys[0]
    if x >= xs[-1]:
        return ys[-1]
    for i in range(1, len(xs)):
        if x <= xs[i]:
            x0, x1, y0, y1 = xs[i - 1], xs[i], ys[i - 1], ys[i]
            return y0 if x1 == x0 else y0 + (y1 - y0) * (x - x0) / (x1 - x0)
    return ys[-1]


def project_archetype_war(
    vos_ceiling: Optional[float],
    vos_career: Optional[float],
    age: Optional[float],
    in_mlb: bool,
    cfg: Dict[str, Any],
    ceiling_table: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Optional[float]]]:
    """Archetype 'ballpark' career-WAR projection (averages only, not a
    per-player forecast). Reads cfg['war_archetype']:

      ceiling_to_career: score[]/war[]  -> avg career WAR for this ceiling
                                           profile *if he reaches MLB*
      frac_ahead:        age[]/frac[]   -> share of career WAR still ahead
      career_to_ttd:     score[]/years[]-> projected years to debut

    Returns {arch_career, remaining, debut_age}:
      - arch_career: the profile's average MLB career WAR.
      - remaining:   arch_career               for prospects (full career ahead)
                     arch_career*frac_ahead(age) for players already in MLB.
      - debut_age:   current_age + ttd(VOS_Career) for prospects, else None.
    Returns None if no war_archetype table is present.
    """
    blk = cfg.get("war_archetype") or {}
    # ceiling_table overrides the ceiling->career curve (e.g. a role-specific
    # pitcher table) while frac_ahead / career_to_ttd stay shared from blk.
    c2c = ceiling_table if ceiling_table is not None else (blk.get("ceiling_to_career") or {})
    score, war = c2c.get("score"), c2c.get("war")
    if not (isinstance(score, list) and isinstance(war, list)
            and len(score) == len(war) and len(score) >= 2):
        return None
    if vos_ceiling is None:
        return None
    arch = _interp1d(float(vos_ceiling), score, war)
    war_hi = c2c.get("war_hi")
    hi = (_interp1d(float(vos_ceiling), score, war_hi)
          if isinstance(war_hi, list) and len(war_hi) == len(score) else arch)
    frac = 1.0
    debut: Optional[float] = None
    fa = blk.get("frac_ahead") or {}
    if in_mlb and age is not None and isinstance(fa.get("age"), list) \
            and isinstance(fa.get("frac"), list):
        frac = _interp1d(float(age), fa["age"], fa["frac"])
    elif not in_mlb:
        c2t = blk.get("career_to_ttd") or {}
        if age is not None and vos_career is not None \
                and isinstance(c2t.get("score"), list) and isinstance(c2t.get("years"), list):
            debut = float(age) + _interp1d(float(vos_career), c2t["score"], c2t["years"])
    return {"arch_career": arch, "arch_upside": hi,
            "remaining": arch * frac, "remaining_upside": hi * frac,
            "debut_age": debut}


def _ceiling_tier_band(vos_ceiling: Optional[float],
                       cfg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Return the matching ceiling_tiers band dict ({min,label,median_war,...})
    for a VOS_Ceiling score, or None. Top-down: first band whose 'min' <= score."""
    if vos_ceiling is None:
        return None
    bands = (((cfg.get("war_archetype") or {}).get("ceiling_tiers") or {}).get("bands")) or []
    try:
        x = float(vos_ceiling)
    except (TypeError, ValueError):
        return None
    for band in sorted(bands, key=lambda b: float(b.get("min", 0.0)), reverse=True):
        try:
            if x >= float(band.get("min", 0.0)):
                return band
        except (TypeError, ValueError):
            continue
    return None


def _classify_ceiling_tier(vos_ceiling: Optional[float], cfg: Dict[str, Any]) -> str:
    """Flavor label for a VOS_Ceiling score (from ceiling_tiers bands)."""
    band = _ceiling_tier_band(vos_ceiling, cfg)
    return str(band.get("label", "")) if band else ""
