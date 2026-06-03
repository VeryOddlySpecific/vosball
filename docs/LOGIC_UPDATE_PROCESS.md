# VOSBall — Logic Update Process

> How to safely change scoring logic or add tools on top of the layered
> `vosball` package. Companion to [REFACTOR_LOG.md](archive/REFACTOR_LOG.md), which
> records how the package was built. **Read this before editing anything in
> `vosball/`.**

The whole package was extracted from the old monolithic `run_vos.py` **without
changing a single output number**, and a golden harness keeps it that way. The
process below exists to preserve that guarantee while the suite keeps evolving.

---

## 1. Layer map & the one rule

```
vosball/
├── engine/          PURE SCORING — deterministic transforms, NO I/O.
│   ├── constants.py     lookup tables (positions, pitch tiers, personality maps)
│   ├── rows.py          resolve_float / resolve_int cell readers
│   ├── normalization.py normalize_to_20_80 + params
│   ├── tiers.py         tier classification
│   ├── context.py       league/team labels, mode/decay block selection
│   ├── park.py          park-factor selection + tool adjustment
│   ├── reach.py         the v6 logistic Reach model
│   ├── scoring.py       per-tool hitter/pitcher + per-position composites
│   ├── adjustments.py   development/readiness/age/personality/draft stack
│   ├── war.py           archetype WAR projection + ceiling tiers
│   └── core.py          build_hitter_row / build_pitcher_row / is_pitcher
├── data/            I/O + PARSING — path-agnostic (takes explicit dirs/paths).
│   ├── config.py        weights (+ schema validation), id/team/URL maps
│   ├── players.py       PlayerData CSV load + 1-100 → 20-80 conversion
│   ├── contracts.py     StatsPlus /contract + /contractextension
│   ├── parks.py         park-factors file loader
│   └── loaders.py       back-compat re-export shim over the four above
├── services.py      ORCHESTRATION — evaluate_players() / evaluate_league().
├── reporting.py     OUTPUT WRITERS — write_output_csv / _write_eval_summary_md.
└── cli.py           argv parsing; wires data → services → reporting.
run_vos.py           thin entry point + back-compat re-export shim.
```

**Dependency direction is strictly downward:** `engine → data → services →
cli/reporting`. Within the engine: `constants/rows/normalization/tiers` and
`context`/`park` are leaves; `scoring` builds on them; `adjustments`/`war` are
leaves; `core` sits on top and pulls everything together. **Nothing reaches
upward, and there are no cycles** — keep it that way.

**The one rule: the engine is pure.** No file reads, no network, no `argv`, no
config-path defaults, no logging used for control flow. If your change needs to
*read* something, it belongs in `data/`; if it needs to *orchestrate* loading +
scoring, it belongs in `services.py`; the engine only *computes*.

**Where a change goes:**

| Your change… | …lands in |
| --- | --- |
| Scoring math / a new rating rule / adjustment | the matching `engine/` submodule |
| A new input source / file format / API endpoint | `data/` (config/players/contracts/parks) |
| A new "load a league and score it" use case | `services.py` |
| A new output column / report format | `reporting.py` |
| A new CLI flag | `cli.py` |
| A standalone tool (depth charts, drafts, trades…) | a top-level script that calls `services` |

---

## 2. The golden-harness workflow (always green)

The contract is [tests/test_golden.py](../tests/test_golden.py): two leagues
(`wwoba` 20-80, `ndl` 1-100) over committed 200-row fixtures, each run through
**two modes** — `cli` (the `run_vos.py` subprocess) and `service`
(`evaluate_league` in-process) — asserted **byte-identical** (timestamps
stripped) to committed snapshots in `tests/golden/`.

```bash
cd F:\vosball
py tests\test_golden.py            # before AND after every change — must stay green
py tests\test_golden.py --update   # ONLY after an intentional output change
```

- **Refactors / cleanups that must NOT move numbers** (extractions, renames,
  splits): the harness must stay green with **no** `--update`. If it goes red,
  you changed behavior — investigate before committing.
- **Intentional output changes** (new weights, a new rule): run `--update`,
  **review the regenerated `tests/golden/*.csv` diff** to confirm the delta is
  exactly what you intended, and say so in the commit message.
- **Trust but verify the net:** after touching the engine, confirm the harness
  still *fails on drift* — perturb one number, see red, revert. (It does today.)

---

## 3. Changing or adding a scoring rule

1. Edit the right `engine/` submodule. Keep the function **pure** (row dict +
   cfg dict in → number out; no I/O).
2. If it's a new public function, add it to that submodule's `__all__`. It is
   auto-re-exported: [engine/__init__.py](../vosball/engine/__init__.py) does
   `from vosball.engine.<sub> import *` and aggregates every submodule's
   `__all__`, so `from vosball.engine import X` keeps working regardless of
   which file `X` lives in. (Same pattern for `data/` via the `loaders.py`
   shim + [data/__init__.py](../vosball/data/__init__.py).)
3. Decide intent:
   - **Not meant to move numbers?** Golden must stay green with no `--update`.
   - **Meant to move numbers?** `--update`, review the diff, note it in the
     commit.
4. Most scoring knobs live in `config/weights_v10.json`, not in code — prefer a
   config change over a code change when the rule is already data-driven.
   Schema is enforced by `data/config.py::_validate_v5_schema` (the
   `scoring_modes` / `vos_career` / reach / `age_decay` blocks).

---

## 4. Adding a new tool

- Call **`vosball.services.evaluate_league(...)`** (loads + scores a league and
  returns plain dicts) or **`evaluate_players(...)`** (scores an
  already-loaded roster). Don't re-implement loading or reach into engine
  internals.
- Write output through **`vosball.reporting`** (or your own writer), not by
  hand-rolling the column order — `write_output_csv` owns the canonical schema.
- New tools should **`import vosball.* directly`** — do not add new dependencies
  on the `run_vos` shim (see §5).
- The services functions raise `ValueError`/`FileNotFoundError` on fatal input
  problems and otherwise just return rows — the caller decides how to surface
  errors. No `argv`, no files written by the service itself.

**Worked example — the web UI.** [webapp/app.py](../webapp/app.py) (the local
Streamlit eval browser) is the canonical consumer to copy: it calls
`evaluate_league(...)`, drops the returned rows into a table, and writes its CSV
download through `vosball.reporting.write_output_csv` (so the export is
byte-identical to the CLI). It never imports from `vosball.engine.*` internals
and never touches a file the engine owns — it just consumes the seam. New tools
should follow the same shape.

---

## 5. The back-compat shim policy

`run_vos.py` re-exports the entire engine + data + services + reporting + cli
surface so legacy `import run_vos` code keeps working. As of the Phase-5 polish,
the three remaining real consumers — `player_card.py`, `what_if.py`,
`lib/draft_score.py` — were migrated to import `vosball.*` directly.

- **New code:** import `vosball.*` directly. Never add a new `import run_vos`
  dependency.
- **The shim shrinks/retires** only when the last legacy consumer is gone. To
  check who still depends on it:
  ```bash
  grep -rnE "import run_vos|from run_vos" --include=*.py .
  ```
  When that returns only `run_vos.py` itself (and the test harness, which
  intentionally exercises the CLI path), the shim's re-exports can be retired.

---

## 6. Extending golden coverage

When a new path becomes load-bearing, pin it. In
[tests/test_golden.py](../tests/test_golden.py):

- **A new league / rating scale:** add a `(case_id, league)` to `CASES`, then
  `py tests\test_golden.py --update` to mint its snapshot (review it first).
- **A new orchestration entry point or output writer:** add a mode alongside
  `cli`/`service` (see `produce_output` / `run_engine_service`) so it's checked
  against the same golden, or add a dedicated case. The point is that *every*
  load-bearing way to produce the eval CSV is pinned byte-for-byte.
- Keep fixtures small (200 rows is enough — VOS scores are per-player absolute,
  so a subset reproduces full-file numbers) and **committed**, so the baseline
  travels with the repo.

---

## 7. Cut-over note (deferred)

This is still the **sandbox** (`F:\vosball`, fresh git, no remote). The live
suite is `F:\ratings` (still the pre-refactor monolith) and has **not** been
touched. Promoting the sandbox to live is a separate, deferred phase — see
[REFACTOR_LOG.md](archive/REFACTOR_LOG.md) §"Next phases → Cut-over". Key things not to
forget when that day comes:

- keep the `vos_v2.py` rollback path available as an escape hatch;
- reconcile the point-in-time `data/` snapshot (a 2026-05-29 copy) against live
  data and the ~20 GB of generated output/caches excluded from the sandbox.
