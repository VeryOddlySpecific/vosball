# VOSBall Layering Refactor — Work Log

> **Status:** Phases 0–4 complete · **Date:** 2026-05-29 · **Sandbox:** `G:\vosball` · **Deployed suite:** `G:\ratings` (untouched)

## Overview

VOSBall is a baseball-analytics suite that had grown to ~60 flat scripts, with the VOS scoring logic, I/O, and CLI all tangled inside a single ~2,159-line `run_vos.py`. On **2026-05-29** we refactored it into a layered, UI-agnostic Python package — **without changing a single byte of output**. All work was done in an **isolated sandbox at `G:\vosball`**: a fresh-git copy of the deployed suite that includes the code and config but deliberately **excludes the ~20 GB of generated league output and caches**. The live, deployed suite at `G:\ratings` was **never touched** at any point. The end state is a clean dependency stack — `vosball.engine` → `vosball.data` → `vosball.services` → `vosball.cli` / `vosball.reporting` — with `run_vos.py` reduced to a thin entry point plus back-compat shim, so every existing tool that imports it keeps working unchanged.

---

## The approach

We used a **strangler-fig** strategy executed entirely inside a sandbox:

- **Sandbox, not in-place.** We never edited the deployed suite. We worked in a fresh-git copy (`G:\vosball`, no remote) so that any mistake was contained and the production tree at `G:\ratings` stayed live and intact.
- **Golden-output harness as the safety net.** Before moving any code, we built a regression harness (`tests/test_golden.py`) that pins the engine's full evaluation CSV for two leagues to byte-identical committed snapshots. This was the contract every refactor step had to honor.
- **AST surgery for verbatim moves.** Code was relocated by deterministic AST node extraction — functions and constant tables were lifted **verbatim** out of `run_vos.py` and dropped into their new modules, rather than retyped or paraphrased. This minimized the chance of accidental logic drift.
- **Golden green after every step.** The harness was run after each extraction. It was confirmed to **pass on the baseline** and to **fail on injected drift** (so we know it actually catches regressions), and it stayed **green after every single extraction** across all four phases.

---

## What we did, phase by phase

The `run_vos.py` size arc tells the story:

```
2,159  →  833  →  554  →  57/58 lines
baseline   P1     P2      P3 / P4
```

### Phase 0 — Safety net (commit `d3f4b56`)

- **Goal:** Make output drift impossible to miss before touching any logic.
- **What landed:** The golden-output regression harness, `tests/test_golden.py`. It pins the VOS engine's evaluation CSV for **two leagues** — `wwoba` on the **20-80** rating scale and `ndl` on the **1-100** scale (linearly remapped to 20-80 at load) — over **committed 200-row input fixtures**, and asserts byte-identical output (timestamps stripped).
  - Verify: `py tests/test_golden.py`
  - Regenerate after an intentional change: `py tests/test_golden.py --update`
- **`run_vos.py` size:** 2,159 lines (unchanged — no logic moved yet).
- **Verified:** Confirmed to **PASS on the baseline** and **FAIL on injected drift**. (Baseline sandbox commit: `8098f02` — fresh git, no remote, 124 files tracked, guard confirmed no secrets committed.)

### Phase 1 — Extract the VOS scoring engine (commits `2de0c12`, then `76b4276`)

- **Goal:** Pull the pure scoring logic out of `run_vos.py` into a dedicated, I/O-free `vosball.engine`.
- **What moved:** **40 functions** plus **9 constant tables**, via deterministic AST surgery (verbatim node extraction). The moved logic includes:
  - park adjustment; mode/decay resolution; the **v6 logistic Reach model**;
  - hitter/pitcher per-tool scoring;
  - the development / readiness / age / personality / draft adjustment stack;
  - WAR projection; `build_hitter_row`, `build_pitcher_row`, `is_pitcher`.
  - Landed in `vosball/engine/` across modules: `normalization`, `tiers`, `rows`, `constants`, `core`.
- **`run_vos.py` size:** dropped to **833 lines**.
- **Verified:** golden green; the park path was smoke-tested; all importers load; the **25-symbol back-compat surface** stayed intact.

### Phase 2 — Extract the data-access layer (commit `3ac046d`)

- **Goal:** Isolate all file/network I/O and input parsing into a path-agnostic `vosball.data`.
- **What moved:** **17 loaders** + the **1-100 → 20-80 rating-scale conversion** + **10 constants** into `vosball/data/loaders.py`. The loaders cover config/weights, id-maps, teams, league URLs, the PlayerData CSV, the StatsPlus contract fetchers, and park factors.
- **What stayed:** `DEFAULT_DATA_DIR` / `DEFAULT_CONFIG_DIR` (the `SCRIPT_DIR`-relative **app** paths) deliberately remained in `run_vos.py` — the data layer itself is **path-agnostic** and takes explicit directories.
- **`run_vos.py` size:** dropped to **554 lines**.
- **Verified:** golden green.

### Phase 3 — Extract CLI + reporting; `run_vos.py` becomes a thin entry point (commit `7cc8876`)

- **Goal:** Move presentation (output writers and the CLI) out of `run_vos.py`, reducing it to an entry point.
- **What moved:**
  - the output writers `write_output_csv` and `_write_eval_summary_md` → `vosball/reporting.py`;
  - the CLI `main()` → `vosball/cli.py`.
  - `main()` gained an `app_root` parameter; `run_vos.py` passes its own `SCRIPT_DIR`, so the default data/config dirs and the `<root>/<league>/eval/` output location are **unchanged**.
- **`run_vos.py` size:** became a **~57-line** thin entry point + back-compat shim.
- **Verified:** golden green, **plus** the default-output path was verified beyond the golden's coverage.

### Phase 4 — Add the UI-agnostic services layer (commit `489abcb`)

- **Goal:** Provide a clean orchestration API that a future UI can call directly — no argv, no files.
- **What landed:** `vosball/services.py` with two entry points:
  - `evaluate_players(players, cfg, league_lookup, teams, *, park_factors=None, draft_mode=False, contract_lookups=None) -> rows` — the **pure scoring loop** over an already-loaded roster.
  - `evaluate_league(league, *, data_dir, config_dir, weights=None, ids_file=None, park_factors_path=None, rating_scale='20-80', draft=False, contracts=False, base_url=None) -> rows` — **loads + scores** one league; this is the entry point a UI calls.
  - `cli.main` now **delegates its scoring loop to `evaluate_players`**.
- **`run_vos.py` size:** **~58 lines** (entry point + shim).
- **Verified:** golden green; `evaluate_league` was verified to reproduce the golden rows **byte-for-byte**.

**Result:** `run_vos.py` went from **2,159 lines to ~58**. The suite now has a clean layered package, and the three tools that import `run_vos` — `player_card.py`, `what_if.py`, and `lib/draft_score.py` — work **unchanged**. `G:\ratings` (deployed) was never touched.

---

## Architecture now

A strict bottom-up dependency stack. Each layer depends only on the ones below it; nothing reaches upward.

```
G:\vosball\
├── run_vos.py              Entry point + back-compat shim (~58 lines).
│                           Sets SCRIPT_DIR as app root, calls cli.main(app_root=SCRIPT_DIR),
│                           re-exports engine/data/services symbols for legacy importers.
├── vosball/
│   ├── __init__.py         Package docstring; layering notes.
│   ├── engine/             PURE SCORING — no I/O, no config defaults.
│   │   ├── core.py            (~1,300 lines) build_hitter_row / build_pitcher_row,
│   │   │                      per-tool scoring, the adjustment stack, WAR projection.
│   │   ├── constants.py       HITTER_POSITIONS, PITCH_SPEED_TIERS, etc.
│   │   ├── normalization.py   normalize_to_20_80, 1-100 → 20-80 conversion.
│   │   ├── tiers.py           classify_vos_tier, tier_for_player_role.
│   │   └── rows.py            resolve_float / resolve_int row utilities.
│   ├── data/               I/O + PARSING — path-agnostic (takes explicit dirs).
│   │   └── loaders.py         load_player_data, load_weights, load_teams,
│   │                          load_park_factors, load_contract_data, load_id_maps, …
│   ├── services.py         ORCHESTRATION — evaluate_players() (scores a loaded roster),
│   │                          evaluate_league() (loads + scores). Returns lists of dicts;
│   │                          no files written, no argv parsed.
│   ├── reporting.py        OUTPUT WRITERS — write_output_csv, _write_eval_summary_md.
│   │                          Pure I/O; takes explicit paths.
│   └── cli.py              CLI — parses argv, loads via vosball.data,
│                              calls services, writes via vosball.reporting.
├── config/                weights JSON, team/league maps, park factors.
├── data/                  PlayerData CSV exports, one per league slug.
└── tests/                 test_golden.py, fixtures/data/, golden/.
```

**Layer purposes at a glance:**

| Layer | Responsibility | Depends on |
| --- | --- | --- |
| `vosball.engine` | Pure transforms; deterministic per-player scoring. No I/O. | `lib.vos_decay` only |
| `vosball.data` | File/network loading + parsing. No scoring logic. | (stdlib / fetch) |
| `vosball.services` | Use-case orchestration (`evaluate_players`, `evaluate_league`). | engine, data |
| `vosball.reporting` | CSV + Markdown writers. | (stdlib) |
| `vosball.cli` | argv parsing + wiring it all together. | data, services, reporting |
| `run_vos.py` | Thin entry point + back-compat re-export shim. | cli (+ re-exports all) |

---

## Verification & safety

- **Golden harness (`tests/test_golden.py`).** Two cases — `engine_wwoba_20-80` and `engine_ndl_1-100` — driven by committed **201-row** fixture files (header + 200 rows) at `tests/fixtures/data/PlayerData-{league}.csv`, asserting byte-identical (timestamp-stripped) output against `tests/golden/engine_{case}.csv`. Because VOS scores are **per-player absolute** (fixed center/scale, no cohort-relative terms), a 200-row subset yields identical per-player numbers as a full file — so it runs in seconds. It was proven to **fail on injected drift** and stayed **green after every extraction** in all four phases. `evaluate_league` was additionally checked to reproduce the golden rows byte-for-byte.
- **Back-compat surface preserved.** `run_vos.py` re-exports the full engine and data surfaces (`from vosball.engine import *`, `from vosball.data import *`) plus explicit `services.evaluate_players` / `evaluate_league`, `reporting.write_output_csv` / `_write_eval_summary_md`, `cli.main`, and the module attrs `DEFAULT_DATA_DIR` / `DEFAULT_CONFIG_DIR`. The dependent tools — `player_card.py`, `what_if.py`, `lib/draft_score.py` — were confirmed to work with **no changes**.
- **Deployed suite untouched.** Everything happened in the `G:\vosball` sandbox (fresh git, no remote). `G:\ratings` was never modified.
- **User-facing behavior unchanged.** Same CLI command and flags, same default output location (`{app_root}/{league}/eval/...`), same columns and numeric precision.

**Commit list (sandbox, in order):**

| Commit | Phase | What |
| --- | --- | --- |
| `8098f02` | baseline | Sandbox created — fresh git, no remote, 124 files tracked, no-secrets guard confirmed |
| `d3f4b56` | 0 | Golden-output regression harness |
| `2de0c12` | 1 | Create `vosball` package; extract leaf engine modules |
| `76b4276` | 1 | Extract full VOS engine into `vosball.engine` |
| `3ac046d` | 2 | Extract data-access layer into `vosball.data` |
| `7cc8876` | 3 | Extract CLI + reporting; `run_vos.py` becomes a thin entry point |
| `489abcb` | 4 | Add UI-agnostic services layer (`vosball.services`) |

---

## Next phases

### 1. UI (deliberately deferred) — **the next major decision**

- **Status:** Not started. **Intentionally held off** — this is the next big call to make.
- **Decision pending:** which platform to build on — a **WordPress plugin** vs a **local web app**. Both options sit on top of the same foundation: **`vosball.services.evaluate_league`**. That entry point already returns plain lists of dicts with no argv/file dependencies, so whichever platform is chosen consumes the same API; the choice is about deployment/hosting/UX surface, not engine plumbing.
- **Rationale:** The layering work was sequenced precisely so the UI choice could be deferred without blocking anything. The services layer is the stable seam the UI will attach to.
- **Rough scope:** Pick the platform, then build a presentation/transport layer that calls `evaluate_league` (and renders/serves the resulting rows). No engine changes expected.

### 2. Polish (optional, golden-protected, low risk)

All items below are guarded by the golden harness, so they carry low regression risk. None are required for the suite to function.

- **(a) Split the two still-monolithic modules.**
  - **Status:** Not started.
  - **Scope:** `vosball/engine/core.py` (~1,300 lines) → finer submodules such as `scoring` / `adjustments` / `park` / `assembly`; and `vosball/data/loaders.py` → `config` / `players` / `contracts` / `parks`.
  - **Rationale:** Improves navigability and review surface; the AST-extraction precedent makes this mechanical.
- **(b) Add a permanent in-process golden case that drives `evaluate_league` directly.**
  - **Status:** Not started (we have already done a one-off byte-for-byte verification of `evaluate_league`).
  - **Scope:** Add a standing test case that exercises the services entry point end-to-end, not just the engine, so the orchestration layer is permanently pinned.
  - **Rationale:** Locks in the new public API the UI will depend on.
- **(c) Migrate suite tools off the `run_vos` back-compat shim.**
  - **Status:** Not started.
  - **Scope:** Move `player_card`, `what_if`, `lib/draft_score`, and the **~55 other** suite scripts to import `vosball.*` directly instead of going through `run_vos`.
  - **Rationale:** Eventually lets the shim shrink/retire; reduces coupling to the legacy entry point. Can be done incrementally, one tool at a time, with the shim staying in place until the last consumer is migrated.

### 3. Cut-over (sandbox → live)

- **Status:** Not started — to be done **when confident**.
- **Scope:** Promote `G:\vosball` from sandbox to the live suite, and retire or re-sync the deployed `G:\ratings`.
- **Key considerations:**
  - the **`vos_v2.py` rollback path** (must remain available as an escape hatch);
  - the **point-in-time data snapshot** — the sandbox `data/` is a 2026-05-29 copy, so cut-over needs a plan to reconcile/refresh against live data and the ~20 GB of generated output/caches that were excluded from the sandbox.

---

## How to pick up where we left off

Everything lives in the **sandbox at `G:\vosball`** (fresh git, no remote; 7 commits, `8098f02` … `489abcb`). The deployed suite at `G:\ratings` is untouched — start from the sandbox.

Before and after any change, confirm zero output drift:

```bash
cd G:\vosball
py tests/test_golden.py            # must stay green
py tests/test_golden.py --update   # only after an INTENTIONAL output change
```

Quick smoke test of the full pipeline (writes `{app_root}/{league}/eval/...` by default):

```bash
py run_vos.py --league wwoba --output wwoba_test.csv
```

The next decision is the **UI platform** (WordPress plugin vs local web app); whatever is chosen attaches to `vosball.services.evaluate_league`.
