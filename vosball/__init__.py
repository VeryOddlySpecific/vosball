"""VOSBall — shared analytics core for the VOSBall Management Suite.

This package is the refactor target: the VOS scoring engine (and, in later
phases, the data-access, StatsPlus, services, and reporting layers) is being
lifted out of the original flat scripts into a layered package — without
changing any output. Existing scripts keep working unchanged: run_vos.py
re-exports the engine symbols that moved here, so `import run_vos as v2` and
`import run_vos` continue to resolve every name they used before.

Layering (grown incrementally, strangler-fig style):
    vosball.engine    pure VOS scoring — deterministic, no I/O   [in progress]
    vosball.data      PlayerData / config / park loading          [later]
    vosball.statsplus StatsPlus API access                        [later]
    vosball.services  use-case orchestration (UI-agnostic)        [later]
"""
