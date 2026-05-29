"""vosball.data — data-access layer for the VOSBall suite.

Loaders for PlayerData CSV, weights/config JSON, team & league-level maps,
StatsPlus contract endpoints, and park factors. Pure I/O + parsing: each takes
explicit directories / paths / URLs and returns plain dicts and lists. No
scoring logic (that's vosball.engine) and no application path defaults (those
stay with the CLI in run_vos.py). Lifted verbatim from run_vos.py in the
Phase 2 extraction; output is byte-identical (guarded by tests/test_golden.py).
"""
from vosball.data.loaders import *  # noqa: F401,F403
from vosball.data import loaders as _loaders

__all__ = list(_loaders.__all__)
