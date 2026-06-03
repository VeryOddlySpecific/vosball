# Ratings Pipeline — User Guide

A workflow-focused reference for the player evaluation + roster construction pipeline. Written for future-you. Assumes the baseball-sim context is already in your head.

---

## The pipeline at a glance

```
   StatsPlus API + PlayerData CSV
                |
          [ vos_v2.py ]                 # 20–80 ratings → eval CSV
                |
                v
        evaluation_summary_{league}_{ts}.csv
                |
   ┌────────────┼────────────┬──────────────────────┐
   |            |            |                      |
[depth_chart] [free_agent  [project_season]   (other tools:
     |        _market]          |              draft_grades,
     v            |             v              prospect_rankings,
{org}_{lvl}      v        {org}_{lvl}_         farm_value, etc.)
  .csv/.md   FA report     projection_{ts}.md
     |
     v
[org_strength_report]
     |
     v
{org}_strength_{ts}.md
```

Each module reads the artifact upstream of it. If you change something at the top, re-run downward.

---

## Workflow 1 — Refresh ratings (VOS)

**When:** Start of any session. After a sim. After importing fresh PlayerData.

**Run:**
```
py vos_v2.py --league sahl
```

**What it does:** Pulls `data/PlayerData-{league}.csv` + `config/weights_v2.json` + `config/teams-{league}.json` + `config/id_maps.json`, computes weighted 20–80 scores for hitters and pitchers (current + potential), writes `evaluation_summary_{league}_{ts}.csv`.

**Common flags:**
- `--park-factors config/park-factors-{league}.json` — apply ballpark adjustments
- `--draft` — apply draft-age modifiers (17 → −1.5, 22 → +1.5)
- `--contracts` — include contract API data in the output
- `--ids-file <path>` — restrict to a subset of player IDs
- `--base-url <url>` — override the league API base

**Output is the foundation** for every downstream module. Don't skip.

---

## Workflow 2 — Build a depth chart for one level

**When:** Want the ideal lineup, rotation, and bullpen for a level. Need promotion / replacement / demotion candidates.

**Run:**
```
py depth_chart.py --league sahl --org "Houston Astros" --level ML --year 2061
```

**What it does:** Reads the latest eval CSV, blends VOS with a level-relative stats z-score (composite = `ratings_weight * VOS + stats_weight * (50 + 15z)`), assembles lineup vs L/R, rotation, bullpen by role, and pulls candidates from the level below.

**Outputs:**
- `{league}/depth/{org_slug}_{level}_{ts}.md` (full report)
- `{league}/depth/{org_slug}_{level}_{ts}.csv` (player-level: composite, slot, tier, primary_pos)
- `{league}/depth/{org_slug}_{level}_{ts}_constants.json` (league constants — needed by `project_season`)

**Common flags:**
- `--all-orgs` — every org at the given level
- `--all-levels` — every level for one org (use this before any org-wide rollup)
- `--all-level-charts` — every level × every org (slow, but feeds league-wide rollups)
- `--no-stats` — VOS-only composite (debugging when API is down)
- `--no-cache` / `--cache-dir <path>` — control stat caching
- `--players-override-csv <path>` — force-include certain players (repeatable)

**Tip:** Always pass `--year` matching the OOTP season, not calendar year. Stats fetch defaults to today's calendar year.

---

## Workflow 3 — Project the season's wins

**When:** After running a depth chart. Want to know if the team you've assembled is actually good.

**Run:**
```
py project_season.py --league sahl --org "Houston Astros" --level ML
```

**What it does:** Reads the latest `{org}_{level}_{ts}.csv` + `_constants.json` from `depth_chart`, computes:
- **RS** from lineup-PA-weighted team wOBA (70% vs RHP / 30% vs LHP default)
- **RA** from IP-weighted team FIP, scaled to runs
- Optional **defense shade** (`±0.5%` per point of avg starter defense, capped ±10%)
- **Wins** via Pythagenpat

**Output:** `{league}/depth/{org}_{level}_projection_{ts}.md`

**Common flags:**
- `--all-orgs` — project every org at one level
- `--all-levels` — project every level for one org
- `--vs-r-share 0.7` — adjust handedness mix
- `--blend-current-fip 0.5` / `--blend-current-woba 0.5` — blend current-season stats with full window
- `--use-current-standings` — incorporate actual W/L into projection
- `--no-defense-shade` — disable defensive adjustment
- `--leader-mode {comprehensive,individual,both}` — leaderboard format in the MD
- `--games <n>` — override games in the season

**Run depth_chart first.** This script can't run without the constants sidecar.

---

## Workflow 4 — Shop the free agent market

**When:** You know what your roster looks like and want to find upgrades. Pre-deadline, pre-FA period, or just opportunistic.

**Run:**
```
py free_agent_market.py --league sahl --org "Houston Astros" --level ML --year 2061
```

**What it does:** Filters the eval CSV to FAs (no Org), scores them with the same composite as `depth_chart`, then **compares each FA against your starter at the same slot**. Surfaces actual upgrades, not just "best available."

**Output (in `{league}/depth/`):**
- `free_agents_{org_slug}_{level}_{ts}.csv`
- `free_agents_{org_slug}_{level}_{ts}.md` with four sections:
  1. FA Hitters (ranked by composite)
  2. FA Pitchers (ranked by composite)
  3. **Hitter Upgrade Targets** — beats your starter at that pos
  4. **Pitcher Upgrade Targets** — beats your weakest in role

**Common flags:**
- `--scan-depth-dir` — multi-level FA scan across all your depth chart files
- `--age-cap-override <n>` — relax/tighten the per-level age caps
- `--ignore-last-level` — show FAs even if their last appearance was below your target level
- `--min-pro-service-days <n>` — filter out non-pros
- `--top-n-hitters` / `--top-n-pitchers` — list length
- `--min-pa` / `--min-ip` — stat thresholds

**Run depth_chart first.** FA comparisons need your slot-by-slot composites.

---

## Workflow 5 — Audit org-wide strength

**When:** Big-picture review. Holes, surplus, single points of failure across every level.

**Run:**
```
py org_strength_report.py --league sahl --org "Houston Astros"
```

**What it does:** Rolls up every level's `{org}_{level}_{ts}.csv` into a positional strength view. Z-scores composites within each level so a 60 at AAA and a 60 at A both mean "top of the level." Adds a league-relative percentile if other orgs share the same timestamp.

**Outputs (`{league}/org_depth/`):**
- `{org_slug}_strength_{ts}.md`
- `{org_slug}_strength_{ts}_positions.csv`
- `{org_slug}_strength_{ts}_player_details.csv`

**Common flags:**
- `--all-orgs` — every org + a `league_strength_{ts}.md` rollup
- `--org-slug <slug>` — use slug instead of full name
- `--timestamp <ts>` — pin to a specific depth_chart batch (otherwise picks latest)
- `--depth-dir <path>` — override input dir
- `--no-league-summary` — skip the league-wide MD

**Prereq:** Run `depth_chart --all-levels` for the org first (or `--all-level-charts` for the league). Without per-level CSVs, there's nothing to roll up.

---

## End-to-end weekly routine

After a sim, in this order:

```
# 1. Refresh ratings
py vos_v2.py --league sahl --park-factors config/park-factors-sahl.json

# 2. Build every level's depth chart for your org
py depth_chart.py --league sahl --org "Houston Astros" --all-levels --year 2061

# 3. Project ML wins (and minors if interesting)
py project_season.py --league sahl --org "Houston Astros" --level ML

# 4. Shop FAs
py free_agent_market.py --league sahl --org "Houston Astros" --level ML --year 2061

# 5. Org-wide audit
py org_strength_report.py --league sahl --org "Houston Astros"
```

For league-wide work, swap `--org` for `--all-orgs` / `--all-level-charts` where each script supports it.

---

## File-output cheat sheet

| Module | Lives in | Key files |
|---|---|---|
| vos_v2 | `./` | `evaluation_summary_{league}_{ts}.csv` |
| depth_chart | `{league}/depth/` | `{org}_{level}_{ts}.{md,csv}`, `_constants.json` |
| project_season | `{league}/depth/` | `{org}_{level}_projection_{ts}.md` |
| free_agent_market | `{league}/depth/` | `free_agents_{org}_{level}_{ts}.{md,csv}` |
| org_strength_report | `{league}/org_depth/` | `{org}_strength_{ts}.md`, `_positions.csv`, `_player_details.csv`, optionally `league_strength_{ts}.md` |

`stats.py` is a shared library (StatsPlus v2 fetcher + wOBA/FIP/K%/BB% derivations) consumed by `depth_chart` and `free_agent_market`. Not run directly.

---

## Troubleshooting

- **"No constants sidecar found"** in `project_season` → re-run `depth_chart` for that org/level; the JSON sidecar is required.
- **Stat fetch fails / very slow** → `--no-cache` once to bust, or pass `--no-stats` for VOS-only debugging in `depth_chart`.
- **Composites look way off** → check `--year` matches the OOTP season, not the calendar year. Same gotcha across `depth_chart`, `project_season`, `free_agent_market`.
- **`org_strength_report` skips levels** → that org doesn't have a fresh `{level}_{ts}.csv`. Run `depth_chart --all-levels` for the org first.
- **League-relative percentiles missing** in `org_strength_report` → other orgs need depth_chart CSVs sharing the same timestamp. Use `--all-level-charts` or `--timestamp` to pin.
- **FA list is suspiciously thin** → the per-level age cap is tight by design; try `--age-cap-override` or `--ignore-last-level`.
- **VOS draft adjustments** only fire with `--draft`. If your draft pool scores look wrong, you probably forgot the flag.

---

## Related modules (not part of the core 5)

These live in the same repo and share the eval CSV:

- `prospect_rankings.py` — prospect rankings off VOS + age/level
- `draft_values.py`, `draft_grades.py`, `draft_grades_pdf.py`, `draft_pool_analysis.py`, `scrape_prospects.py` — draft prep + post-draft grading
- `farm_value.py` — farm system valuation (newer; `farm_value_old.py` is the legacy version)
- `org_depth_analysis.py`, `org_summary_pdf.py` — alt org rollups / PDF formatting
- `contract.py`, `contract_builder.py` — contract math
- `current_standings.py` — pulls live standings (used by `project_season --use-current-standings`)
- `what_if.py` — scenario tweaks
- `statsplus_paper_news.py` — news scrape

If/when you formalize these, fold them into the workflows above.
