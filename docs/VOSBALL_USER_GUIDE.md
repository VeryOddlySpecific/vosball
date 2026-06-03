# VOSBall User Guide

VOSBall is a baseball player-evaluation suite for OOTP leagues run on **StatsPlus**. Everything is built on **VOS (VOS Optimized Score)** — a 20–80-scale rating (50 = MLB average, σ ≈ 15) produced by the **VOS v10** engine.

There are two ways to use it:

1. **The web app (primary).** A local Streamlit app that scores a league in-process and presents it through interactive pages. This is the day-to-day interface.
2. **The CLI + batch runners (power use).** `run_vos.py` writes eval CSVs to `{league}/eval/...`, and the CLI tools (`core/`, `tools/`) consume those CSVs for depth charts, prospect boards, trade lists, draft packages, contract audits, and more.

---

## Quick start — the web app

Launch it from the repo root:

```powershell
py -m streamlit run webapp\app.py
# or just double-click / run:
run_ui.bat
```

It opens in your browser. Pick a league in the **League Hub**, then work through the pages.

**Built pages:**

| Page | What it does |
| --- | --- |
| **Eval Browser** | Sortable / filterable / searchable eval table; canonical CSV export (byte-identical to `run_vos.py`) |
| **Player Card** | Single-player detail, rendered entirely from the eval row |
| **Depth Charts** | Lineups & staff by level for your org |
| **Prospects** | Prospect board |
| **Farm Value** | Org farm systems, ranked by dollar value |
| **Trade Targets** | League trade blocks scored against your needs |
| **Free Agents** | Biggest roster holes → best-fit free agents |
| **League Hub** | Pick the active league; per-sim checklist; quick-link grid to every module |

*Planned:* Draft Room, Finances.

**How it works.** The app scores the active league **in-process** by calling `vosball.services.evaluate_league(...)` over the player CSV in `data/`. Network access — pulling **fresh ratings** from StatsPlus, contract fields, and live stats — is **opt-in and fails open**: if you don't trigger it (or it can't reach StatsPlus), the app still renders from whatever local data and last eval you have. Pulling fresh ratings is roughly a once-or-twice-per-season action, not a per-sim chore.

The rest of this guide covers the CLI/batch path that produces and consumes the eval CSVs.

---

## Data flow

```mermaid
flowchart TD
    SP[StatsPlus API<br/>/ratings export] -->|core/fetch_player_data.py /<br/>tools/fetch_all_player_data.py| PD[data/PlayerData-{league}.csv<br/>canonical input]
    CFG[config/<br/>weights_v10.json, teams-*.json,<br/>league_settings.json, park-factors] --> EVAL
    PD --> EVAL[run_vos.py<br/>--> vosball.services.evaluate_league]
    EVAL --> OUT[{league}/eval/<br/>evaluation_summary_{league}_{ts}.csv + .md<br/>VOS_Reach / VOS_Career / VOS_Blended]
    OUT --> DOWN[Downstream tools / web app pages]
    DOWN --> D1[core/prospect_rankings.py · core/farm_value.py]
    DOWN --> D2[core/depth_chart.py · tools/project_season.py · tools/org_*]
    DOWN --> D3[core/trade_block.py · core/trade_targets.py · tools/waiver_wire.py]
    DOWN --> D4[tools/draft_pool_analysis.py · tools/draft_board.py · tools/draft_grades.py]
    DOWN --> D5[tools/contract_audit.py · tools/awards_rank.py · core/hof_grade.py]
```

**Walk-through:**

1. **Ingest** — StatsPlus exposes a `/ratings` export. `core\fetch_player_data.py` (single league) or `tools\fetch_all_player_data.py` (all leagues, parallel) queues the export, polls until the CSV is ready, and writes it to **`data/PlayerData-{league}.csv`** — the canonical input for everything downstream. (The web app's "fetch fresh ratings" button drives the same flow.)
2. **Configure** — Per-league behavior is read from **`config/`** (notably `weights_v10.json` for scoring, `teams-{league}.json`, `{league}-park-factors.json`, and `league_settings.json` for org/year/rating-scale).
3. **Core eval** — `run_vos.py` (a thin entry point over `vosball.cli` → `vosball.services.evaluate_league`) reads the PlayerData CSV plus config, scores every player, and writes **`{league}/eval/evaluation_summary_{league}_{timestamp}.csv`** (plus a `.md` summary). This eval CSV is the **single source of truth** for the CLI tools.
4. **Fan out** — Downstream tools (and the app pages) consume the eval CSV — and sometimes live StatsPlus stat/contract APIs — to produce prospect boards, depth charts, trade lists, draft boards, contract audits, awards, and more, each under its own `{league}/` subdirectory.

---

## The CLI workflow

A normal per-sim session moves **ingest → core eval → downstream**. `py` is the project's Python launcher; substitute `python` if you prefer.

### Per-sim (single league)

```powershell
# 1. Download fresh data
py core\fetch_player_data.py --league {league}
py tools\current_standings.py --league {league}    # optional, for live win projections

# 2. Run the core evaluation + farm
py run_vos.py --league {league} --park-factors config/{league}-park-factors.json --contracts --per-org-evals
py core\prospect_rankings.py --league {league}
py core\farm_value.py --league {league}

# 3. Analyze your org
py core\depth_chart.py --league {league} --org "{org}" --year {year} --all-level-charts --no-pdf
py tools\project_season.py --league {league} --org "{org}" --level ML --year {year}
py tools\player_card.py --league {league} --id <player_id>    # optional deep dive
```

4. **Make roster decisions + upload** — set lineups / rotation / bullpen per the depth-chart output, process trades and waiver claims, and upload changes back to StatsPlus.
5. **Daily flavor (optional)** — `py tools\statsplus_paper_news.py --league {league}`.

### All leagues at once (batch)

The batch runners resolve org/year/flags from `league_settings.json`, so you don't repeat CLI args for every league:

```powershell
py tools\fetch_all_player_data.py     # all leagues, parallel (~3-5 min)
py tools\run_vos_all.py               # eval CSVs for every league (applies --contracts, --per-org-evals, --park-factors, --rating-scale per league_settings.json)
py tools\run_depth_chart_all.py       # all-level depth charts for your org in every league
```

Then check trade offers / waiver claims, glance at injuries, and do your inbox sweep.

### Periodic / situational

- **Weekly:** `tools\org_depth_analysis.py`, `tools\org_strength_report.py --all-levels`, `tools\contract_audit.py`, `core\trade_block.py`, `core\trade_targets.py`.
- **Trade deadline:** daily `core\trade_targets.py` per league, refresh `core\trade_block.py` after moves, `core\what_if.py` to vet targets, `tools\top_salary_avg.py` for salary matching.
- **Pre-draft:**
  ```powershell
  py tools\draft_pool_analysis.py --league {league} --name {year}_draft
  py tools\draft_board.py --team {team} --league {league}
  ```
- **Post-draft:** `py tools\draft_grades.py --league {league} --num-teams 30 {league}/drafts/draft_pool_analysis_{name}/` (optional `tools\draft_grades_pdf.py`).
- **Offseason:** `core\free_agent_market.py --league {league} --org "{org}" --level ML`, `tools\fa_cohort_analysis.py` (UBA), `tools\park_recommender.py`, `core\contract.py`, `tools\spring_training_invites.py` (SAHL).

---

## Running the core evaluation

`run_vos.py` is the entry point. Minimum invocation:

```powershell
py run_vos.py --league wwoba
```

This writes (by default):

```
{league}/eval/evaluation_summary_{league}_{timestamp}.csv
{league}/eval/evaluation_summary_{league}_{timestamp}.md
```

### Common flags

```powershell
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

```powershell
py run_vos.py --league ndl --rating-scale 1-100
```

> Tip: `tools\run_vos_all.py` already applies the correct `--rating-scale`, `--park-factors`, `--contracts`, and `--per-org-evals` per league from `league_settings.json`, so you rarely set the scale by hand in batch runs.

The eval CSV columns include `ID, Name, Org, Pos, Age, Level`, the headline scores `VOS_Reach` (logistic P(reach MLB)), `VOS_Career` (current + age decay), `VOS_Blended` (0.4·reach + 0.6·career), plus all positional composites, age adjustments, personality, proneness, and BABIP. All scores are normalized to the 20–80 scale (hard floor 20, ceiling 80, center 50.0, scale 15.0).

---

## Programmatic API

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

The web app's Eval Browser is the canonical consumer of this API — copy its shape for new tools.

---

## Testing

The `vosball` package is guarded by a **golden harness** that proves the output has not drifted.

```powershell
# Verify current output is byte-identical to the committed baseline
py tests\test_golden.py

# Regenerate fixtures + snapshots AFTER an intentional logic change
py tests\test_golden.py --update
```

**What it checks:**

- Two cases: **`engine_wwoba_20-80`** (20–80 scale) and **`engine_ndl_1-100`** (1–100 scale with remap) — both supported rating scales — each run through both the `cli` and `service` paths.
- Input: pinned 201-row fixture subsets (header + first 200 rows) at `tests/fixtures/data/PlayerData-{league}.csv`.
- Output: the full evaluation CSV compared **byte-for-byte** (timestamps stripped) against `tests/golden/engine_{case}.csv`.
- A 200-row subset is enough because VOS scores are **per-player absolute** (fixed center/scale, no cohort-relative terms), so a subset yields the same per-player numbers as a full file — but runs in seconds.

See [tests/test_golden.py](../tests/test_golden.py) and [LOGIC_UPDATE_PROCESS.md](LOGIC_UPDATE_PROCESS.md) for the full maintenance workflow.

**Sanity-test your own league end-to-end:**

1. Confirm `data/PlayerData-{league}.csv` and `config/weights_v10.json` are present.
2. Run it to a scratch file: `py run_vos.py --league <league> --output test_eval.csv`.
3. Eyeball `test_eval.csv` for the expected columns and reasonable ranges — VOS scores should sit in 20–80; watch the INFO logs for out-of-range warnings.
4. For a regression check, save that CSV, make your change, re-run, and diff. **Zero diff means no logic changed.**

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

Plus, at the league root: `{org}_contract_audit_{ts}.md` + `.csv` (from `tools\contract_audit.py`) and `awards/awards_{year}_{ts}.md` (from `tools\awards_rank.py`).

---

## Notes & caveats

- **Credentials for fetching.** `core\fetch_player_data.py` / `tools\fetch_all_player_data.py` (and the app's fetch button) need StatsPlus auth. API tokens in `config/statsplus_tokens.json` (keyed by league, or `_default`) are preferred and expire about every 90 days; the session-cookie fallback in `config/statsplus_session.json` is pulled from browser DevTools and used for `/tradeblock` auth. If a fetch fails, refresh the token/cookie first.
- **No surprise flags.** Everything in this guide uses flags that exist today. If you're unsure of a tool's options, run it with `--help` or read its source under `core/` or `tools/`.
