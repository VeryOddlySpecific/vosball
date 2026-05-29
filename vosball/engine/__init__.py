"""vosball.engine — the pure VOS scoring core.

Everything here is a deterministic transform over an already-parsed player row
(a plain dict of CSV cells) and a weights/config dict. No file, network, or
config loading happens in this layer — that stays in the data layer / run_vos.py
for now. Output is byte-identical to the pre-refactor run_vos.py, guarded by
tests/test_golden.py.

Submodules are re-exported here so callers can `from vosball.engine import X`
without caring which file X lives in.
"""
from vosball.engine.normalization import (
    normalize_to_20_80,
    _normalization_params,
)
from vosball.engine.tiers import (
    classify_vos_tier,
    tier_for_player_role,
    _resolve_tier_bands,
    _DEFAULT_HITTER_TIERS,
    _DEFAULT_PITCHER_TIERS,
)
from vosball.engine.rows import resolve_float, resolve_int
from vosball.engine import core as _core
from vosball.engine.core import *  # noqa: F401,F403
from vosball.engine.constants import (
    BASERUNNING_STEAL_COLS,
    CTRL_COL_ALTERNATIVES,
    POT_PITCH_COLUMN_TO_TYPE,
    PITCH_SPEED_TIERS,
    PITCH_BREAK_PLANES,
    PERSONALITY_CSV_TO_CONFIG,
    PRONE_CATEGORY_TO_NUMERIC,
    HITTER_POSITIONS,
    LEVEL_LABEL_TO_CONFIG,
)

__all__ = [
    "normalize_to_20_80",
    "_normalization_params",
    "classify_vos_tier",
    "tier_for_player_role",
    "_resolve_tier_bands",
    "_DEFAULT_HITTER_TIERS",
    "_DEFAULT_PITCHER_TIERS",
    "resolve_float",
    "resolve_int",
    "BASERUNNING_STEAL_COLS",
    "CTRL_COL_ALTERNATIVES",
    "POT_PITCH_COLUMN_TO_TYPE",
    "PITCH_SPEED_TIERS",
    "PITCH_BREAK_PLANES",
    "PERSONALITY_CSV_TO_CONFIG",
    "PRONE_CATEGORY_TO_NUMERIC",
    "HITTER_POSITIONS",
    "LEVEL_LABEL_TO_CONFIG",
] + list(_core.__all__)
