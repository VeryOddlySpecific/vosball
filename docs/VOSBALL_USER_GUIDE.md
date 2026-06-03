# VOSBall Pipeline User Guide

_Last updated: 2026-05-29_

This guide explains how to run and test the VOSBall pipeline now that the suite uses the **layered `vosball/` package structure**. The code has been split into clean layers (`engine` → `data` → `services` → `cli` + `reporting`), but **nothing changed for you as a user**: the `run_vos.py` command, its flags, its defaults, and its output are all **byte-identical** to the previous version. A golden-test harness guards that guarantee. This document targets the **sandbox at `G:\vosball`**; the deployed suite lives at `G:\ratings`. Run and test changes in the sandbox first, then promote.

---

## Data flow

```mermaid
flowchart TD
    SP[StatsPlus API<br/>/ratings export] -->|fetch_player_data.py /<br/>fetch_all_player_data.py| PD[data/PlayerData-{league}.csv<br/>canonical input]
    CFG[config/<br/>weights_v10.json, teams-*.json,<br/>league_settings.json, park-factors] --> EVAL
    PD --> EVAL[run_vos.py<br/>--> vosball.services.evaluate_league]
    EVAL --> OUT[{league}/eval/<br/>evaluation_summary_{league}_{ts}.csv + .md<br/>VOS_Reach / VOS_Career / VOS_Blended]
    OUT --> DOWN[Downstream tools]
    DOWN --> D1[prospect_rankings.py / farm_value.py]
    DOWN --> D2[depth_chart.py / project_season.py / org_*]
    DOWN --> D3[trade_block.py / trade_targets.py / waiver_wire.py]
    DOWN --> D4[draft_pool_analysis.py / draft_board.py / draft_grades.py]
    DOWN --> D5[contract_audit.py / awards_rank.py / hof_grade.py]
```

**Walk-through:**

1. **Ingest** — StatsPlus exposes a `/ratings` export. `fetch_player_data.py` (single league) or `fetch_all_player_data.py` (all leagues, parallel) queues the export, polls until the CSV is ready, and writes it to **`data/PlayerData-{league}.csv`**. This file is the canonical input for everything downstream.
2. **Configure** — Per-league behavior is read from **`config/`** (notably `weights_v10.json` for scoring, `teams-{league}.json`, `{league}-park-factors.json`, and `league_settings.json` for org/year/rating-scale).
3. **Core eval** — `run_vos.py` (now a thin shim over `vosball.cli` → `vosball.services.evaluate_league`) reads the PlayerData CSV plus config, scores every player, and writes **`{league}/eval/evaluation_summary_{league}_{timestamp}.csv`** (plus a `.md` summary). This eval CSV is the **single source of truth** for all downstream tools.
4. **Fan out** — Downstream tools consume the eval CSV (and sometimes live StatsPlus stat/contract APIs) to produce prospect boards, depth charts, trade lists, draft boards, contract audits, awards, and more, each under its own `{league}/` subdirectory.

---

## The expected workflow

A normal per-sim session moves **ingest → core eval → downstream**. Commands below are exact to the findings (`py` is the project's Python launcher; substitute `python` if you prefer).

### Per-sim checklist (single league)

1. **Download fresh data:**
   ```bash
   py fetch_player_data.py --league {league}
   # (optional, for live win projections)
   py current_standings.py --league {league}
   ```

2. **Run the core evaluation + farm:**
   ```bash
   py run_vos.py --league {league} --park-factors config/{league}-park-factors.json --contracts --per-org-evals
   py prospect_rankings.py --league {league}
   py farm_value.py --league {league}
   ```

3. **Analyze your org:**
   ```bash
   py depth_chart.py --league {league} --org "{org}" --year {year} --all-level-charts --no-pdf
   py project_season.py --league {league} --org "{org}" --level ML --year {year}
   # (optional, single-player deep dive)
   py player_card.py --league {league} --id <player_id>
   ```

4. **Make roster decisions + upload:** set lineups / rotation / bullpen per the depth-chart output, process trades and waiver claims, and upload changes back to StatsPlus.

5. **Daily flavor (optional):**
   ```bash
   py statsplus_paper_news.py --league {league}
   ```

### Daily routine across all leagues

When running every league at once, prefer the batch runners — they resolve org/year/flags from `league_settings.json` so you don't repeat CLI args eight times:

```bash
py fetch_all_player_data.py        # all leagues, parallel (~3-5 min)
py run_vos_all.py                  # eval CSVs for every league (applies --contracts, --per-org-evals, --park-factors, --rating-scale per league_settings.json)
py run_depth_chart_all.py          # all-level depth charts for your org in every league
```
Then check trade offers / waiver claims in the league dashboards, glance at injuries, and do your inbox sweep.

### Periodic / situational

- **Weekly:** `py org_depth_analysis.py`, `py org_strength_report.py --all-levels`, `py contract_audit.py --league {league}`, `py trade_block.py --league {league} --org "{org}"`, `py trade_targets.py --league {league}`.
- **Trade deadline:** daily `py trade_targets.py` per league, refresh `trade_block.py` after moves, `py what_if.py` to vet targets, `py top_salary_avg.py` for salary matching.
- **Pre-draft:**
  ```bash
  py draft_pool_analysis.py --league {league} --name {year}_draft
  py draft_board.py --team {team} --league {league}
  ```
- **Post-draft:** `py draft_grades.py --league {league} --num-teams 30 {league}/drafts/draft_pool_analysis_{name}/` (optional `py draft_grades_pdf.py`).
- **Offseason:** `py free_agent_market.py --league {league} --org "{org}" --level ML`, `py fa_cohort_analysis.py --league {league}` (UBA), `py park_recommender.py`, `py contract.py`, `py spring_training_invites.py --league {league}` (SAHL).

---

## Running the core evaluation

`run_vos.py` is the entry point. Minimum invocation:

```bash
py run_vos.py --league wwoba
```

This writes (by default) to:

```
{league}/eval/evaluation_summary_{league}_{timestamp}.csv
{league}/eval/evaluation_summary_{league}_{timestamp}.md
```

### Common flags

```bash
# Apply ballpark adjustments
py run_vos.py --league wwoba --park-factors config/wwoba-park-factors.json

# Pull /contract + /contractextension fields into the eval
py run_vos.py --league wwoba --contracts

# Write one eval per team into {league}/eval/{team_code}/ (requires combined teams[] park-factors)
py run_vos.py --league wwoba --per-org-evals

# Draft mode: adds Readiness_Adj, Draft_Age_Adj, Draft_RP_Penalty columns
# (and writes draft_evaluation_{league}_{ts}.csv)
py run_vos.py --league wwoba --draft

# Send output to a custom path (handy for testing)
py run_vos.py --league wwoba --output wwoba_test.csv

# Override paths / inputs if needed
py run_vos.py --league wwoba --data-dir ./data --config-dir ./config
py run_vos.py --league wwoba --weights config/weights_v10.json
```

### Rating scale: 20–80 vs 1–100

- **Default is `20-80`** (the scouting convention: 50 = MLB average, σ ≈ 15). Most leagues use this.
- **NDL exports on a `1-100` scale.** For those, pass `--rating-scale 1-100`; the loader linearly remaps the input components to 20–80 at load time, so the **output scores are normalized the same way regardless** — the scale is just the label for the input CSV.

```bash
py run_vos.py --league ndl --rating-scale 1-100
```

> Tip: `run_vos_all.py` already applies the correct `--rating-scale`, `--park-factors`, `--contracts`, and `--per-org-evals` per league from `league_settings.json`, so you rarely need to set the scale by hand in batch runs.

The eval CSV columns include `ID, Name, Org, Pos, Age, Level`, the headline scores `VOS_Reach` (logistic P(reach MLB)), `VOS_Career` (current + age decay), `VOS_Blended` (0.4·reach + 0.6·career), plus all positional composites, age adjustments, personality, proneness, and BABIP. All scores are normalized to the 20–80 scale (hard floor 20, ceiling 80, center 50.0, scale 15.0).

---

## Using the new programmatic API

When you want VOS scores **inside your own Python** (a notebook, an ad-hoc analysis, a new tool) without shelling out and re-reading a CSV, call `vosball.services.evaluate_league()` directly. Use the **CLI** for normal runs and the **API** for embedding / experimentation.

```python
from vosball.services import evaluate_league

rows = evaluate_league(
    league='wwoba',
    data_dir='./data',
    config_dir='./config',
    rating_scale='20-80',   # use '1-100' for NDL
    draft=False,
    contracts=False,
)
# rows -> list[dict], one per evaluated player, same schema as the eval CSV
# (VOS_Reach, VOS_Career, VOS_Blended, VOS_Ceiling, component scores, adjustments, WAR projections)

for r in rows[:5]:
    print(r['Name'], r['VOS_Blended'])
```

It returns a **list of dicts** (no file written, no argv parsed). It raises `ValueError` / `FileNotFoundError` on fatal input problems (missing weights, no players loaded, or a missing contract URL when `contracts=True`). To persist the results, write the list yourself or use `vosball.reporting.write_output_csv`.

If you already have a roster loaded and only want the scoring step, use the lower-level `evaluate_players()` with the `vosball.data` loaders:

```python
from vosball.services import evaluate_players
from vosball.data import load_player_data, load_weights, load_id_maps, load_teams

players = load_player_data('./data', 'wwoba', id_filter=None, rating_scale='20-80')
cfg = load_weights('./config', weights_path=None)   # defaults to weights_v10.json
league_lookup = load_id_maps('./config')
teams = load_teams('./config', 'wwoba')

rows = evaluate_players(
    players, cfg, league_lookup, teams,
    park_factors=None, draft_mode=False, contract_lookups=None,
)
```

**When to use which:**

| Use the CLI (`run_vos.py`) | Use the API (`evaluate_league` / `evaluate_players`) |
| --- | --- |
| Normal per-sim and batch runs | Embedding scores in a notebook or new tool |
| You want the standard CSV + MD outputs in `{league}/eval/` | You want the rows in memory to process further |
| You want timestamped files and per-org variants | You want to control I/O / output format yourself |

---

## Testing the new structure

The refactor is guarded by a **golden harness** that proves the output has not drifted.

```bash
# Verify current output is byte-identical to the committed baseline
py tests/test_golden.py

# Regenerate fixtures + snapshots AFTER an intentional logic change
py tests/test_golden.py --update
```

**What it checks:**

- Two cases: **`engine_wwoba_20-80`** (20–80 scale) and **`engine_ndl_1-100`** (1–100 scale with remap) — both supported rating scales are covered.
- Input: pinned 201-row fixture subsets (header + first 200 rows) committed at `tests/fixtures/data/PlayerData-{league}.csv`.
- Output: the full evaluation CSV (VOS_Reach, VOS_Career, VOS_Blended, VOS_Ceiling, component scores, adjustments) compared **byte-for-byte** (after stripping timestamps) against `tests/golden/engine_{case}.csv`.
- Why a 200-row subset is enough: VOS scores are **per-player absolute** (fixed center/scale, no cohort-relative terms), so a subset yields the same per-player numbers as a full file — but runs in seconds.

**Sanity-test your own league end-to-end:**

1. Confirm `data/PlayerData-{league}.csv` and `config/weights_v10.json` are present.
2. Run it to a scratch file:
   ```bash
   py run_vos.py --league <league> --output test_eval.csv
   ```
3. Eyeball `test_eval.csv` for the expected columns and reasonable ranges — VOS scores should sit in 20–80; watch the INFO logs for any out-of-range warnings.
4. For a regression check, save that CSV, make your code change, re-run, and diff. **Zero diff means no logic changed.**

**Back-compat is preserved.** `run_vos.py` is now a small shim that re-exports all the engine/data/services symbols and calls `cli.main(app_root=SCRIPT_DIR)`. Tools that `import run_vos` — `player_card.py`, `what_if.py`, and `lib/draft_score.py` — continue to work unchanged (they still reach `run_vos.DEFAULT_CONFIG_DIR`, `run_vos.build_hitter_row()`, `build_pitcher_row()`, etc.). The CLI command, flags, defaults, and output location are all unchanged.

> The `G:\vosball` sandbox data is a **point-in-time snapshot** of the deployed suite as of **2026-05-29**, so you can run and test offline without touching live data. Golden tests currently pass on both rating scales.

---

## Reference

### `config/` files

| File | Purpose |
| --- | --- |
| `weights_v10.json` | VOS v10 scoring weights (shared across all leagues; not per-league) |
| `league_url.json` | League slug → StatsPlus API base URL (required for all fetches) |
| `league_settings.json` | Per-league org name, OOTP year, rating-scale override, optional `min_comp` |
| `league_ids.json` | League slug → 3-digit StatsPlus level IDs (ML/AAA/AA/A+/A/R + `_independents`/`_international`) |
| `teams-{league}.json` | Team ID → {Name, Nickname, Parent} (Parent=0 for ML orgs) |
| `{league}_orgs.json` | Flat array of org display names |
| `{league}-park-factors.json` | Per-team batting/defense/baserunning/pitching adjustments (optional) |
| `divisions-{league}.json` | League → Division → [Teams] (needed for `project_season.py`, org rollups) |
| `id_maps.json` | League-level label constants (R, A, AA, AAA, ML, IND, INT, COL, HS) |
| `depth_config.json` | Per-level roster sizes, role counts, stats weights (shared) |
| `contract_config.json` | Contract valuation defaults (shared) |
| `statsplus_tokens.json` | API tokens keyed by league slug (preferred auth; ~90-day TTL) |
| `statsplus_session.json` | Session-cookie fallback, keyed by hostname (for `/tradeblock` auth) |

### `{league}/` output subdirectories

| Subdirectory | Contents |
| --- | --- |
| `eval/` | `evaluation_summary_{league}_{ts}.csv` + `.md` (master eval — all downstream depend on this); `draft_evaluation_*.csv` when `--draft`; `{org_code}/` per-team variants when `--per-org-evals` |
| `prospects/` | `prospect_rankings_{league}_{ts}.csv` (prospect board) |
| `farm/` | `{org}_farm_value_{ts}.csv` + `.md` (farm-system dollar valuation) |
| `depth/` | `{org}_{level}_{ts}.csv` / `.md` / `_constants.json`; `{org}_{level}_projection_{ts}.md`; `free_agents_{org}_{level}_{ts}.csv` + `.md` |
| `org_depth/` | `{org}_strength_{ts}.md` / `_positions.csv` / `_player_details.csv`; `league_strength_{ts}.md` when `--all-orgs` |
| `drafts/{name}/` | `00_summary.txt` … `04_prospect_tiers.txt`, `05_draft_pool.md` (downstream contract), `summary_data.*`, `draft_board_{team}_{ts}.*`, `draft_grades_raw.*`, `draft_grades_summary.*` |
| `trade_block/` | `{org}_tradeblock_{ts}.md` + `.csv` (who to shop) |
| `trade_targets/` | `{org}_trade_targets_{ts}.md` + `.csv` (league-wide shopping list) |
| `waiver_wire/` | `{org}_waiver_wire_{ts}.md` + `.csv` (tiered claim grades) |
| `cache/` | Stat-fetch cache (calendar-day TTL) |

Plus, at the league root: `{org}_contract_audit_{ts}.md` + `.csv` (from `contract_audit.py`) and `awards/awards_{year}_{ts}.md` (from `awards_rank.py`).

---

## Notes & caveats

- **Credentials for fetching.** `fetch_player_data.py` / `fetch_all_player_data.py` need StatsPlus auth. API tokens in `config/statsplus_tokens.json` (keyed by league, or `_default`) are preferred and expire about every 90 days; the session-cookie fallback in `config/statsplus_session.json` is pulled from browser DevTools and is used for `/tradeblock` auth. If a fetch fails, refresh the token/cookie first.
- **Snapshot data.** The `G:\vosball` sandbox `data/` and `config/` are a frozen copy as of **2026-05-29** (covering `bwb`, `ndl`, `sahl`, `sdmb`, `sky`, `tlg`, `uba`, `woba`, `wwoba`). You can run `run_vos.py` and the golden tests against it without network access, but the numbers reflect that date — re-fetch for live decisions.
- **Sandbox vs deployed.** `G:\vosball` is the development sandbox (new layered package + tests). The live suite is `G:\ratings`. Validate code changes in the sandbox (run the golden harness, eyeball a real league) before promoting to the deployed suite.
- **No new flags.** Everything in this guide uses flags that exist today. The package refactor changed internals only; the CLI surface is unchanged.
