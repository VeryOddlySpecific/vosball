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
# The scoring core was split out of core.py into focused submodules (Phase-5
# polish). Re-export every public name from each so `from vosball.engine import X`
# keeps working no matter which submodule X now lives in.
from vosball.engine import context as _context
from vosball.engine import park as _park
from vosball.engine import reach as _reach
from vosball.engine import scoring as _scoring
from vosball.engine import adjustments as _adjustments
from vosball.engine import war as _war
from vosball.engine import core as _core
from vosball.engine.context import *  # noqa: F401,F403
from vosball.engine.park import *  # noqa: F401,F403
from vosball.engine.reach import *  # noqa: F401,F403
from vosball.engine.scoring import *  # noqa: F401,F403
from vosball.engine.adjustments import *  # noqa: F401,F403
from vosball.engine.war import *  # noqa: F401,F403
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
] + list(_context.__all__) + list(_park.__all__) + list(_reach.__all__) \
  + list(_scoring.__all__) + list(_adjustments.__all__) + list(_war.__all__) \
  + list(_core.__all__)
