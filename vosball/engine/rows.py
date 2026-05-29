"""Row-cell readers: pull a typed value out of a PlayerData CSV row dict.

These are the foundational accessors the scoring engine uses to read component
ratings, ages, etc. from a parsed CSV row (a plain dict of string cells).
Ported verbatim from run_vos.py (Phase 1 extraction) — no behavior change.
"""
from __future__ import annotations

from typing import Dict, Optional


def resolve_float(row: Dict[str, str], *col_candidates: str) -> Optional[float]:
    for col in col_candidates:
        if col not in row:
            continue
        val = row.get(col, "").strip()
        if val == "" or val.upper() in ("NA", "N/A", "."):
            continue
        try:
            return float(val)
        except (TypeError, ValueError):
            continue
    return None


def resolve_int(row: Dict[str, str], col: str) -> Optional[int]:
    v = resolve_float(row, col)
    return int(v) if v is not None else None
