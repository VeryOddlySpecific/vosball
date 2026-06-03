# VOS Ratings Toolkit — Project Instructions

Context handoff for a Cowork agent picking up this project on a new machine.

## Project overview

The user (Alex) is the creator of **VOS** (VOS Optimized Score), a proprietary
20-80-scaled player evaluation algorithm for **OOTP Baseball** played on the
**StatsPlus** league management platform. This directory (`D:\ratings`) holds
the VOS algorithm plus a toolkit of analysis utilities built on top of it.

The user is a competent power-user, not a professional engineer. He runs his
tools in multiple OOTP commissioner leagues (sahl, woba, wwoba, tlg). He
prefers short, direct responses without sycophancy or over-explanation. He
appreciates honest assessments — call out limitations, don't oversell.

## Data flow

```
PlayerData-{league}.csv          ─┐
config/weights_v2.json           ─┼─→ vos_v2.py ──→ evaluation_summary_*.csv
config/teams-{league}.json       ─┤                  (+ companion .md)
config/park-factors-*.json (opt) ─┤
config/id_maps.json              ─┘

evaluation_summary_*.csv  ─→ prospect_rankings.py    ──→ prospect_rankings_*.csv
                          ─→ farm_value.py           ──→ farm_values_*.csv
                          ─→ farm_value_old.py       ──→ farm_values_*.csv (legacy/full)
                          ─→ contract.py             ──→ stdout (calls contract_builder)
                          ─→ org_depth_analysis.py   ──→ org_depth_*.{csv,md,html}
                          ─→ draft_pool_analysis.py  ──→ draft_pool_*.md
                          ─→ depth_chart.py          ──→ {org}_{level}_*.{csv,md,_constants.json}

depth_chart output         ─→ project_season.py     ──→ {org}_{level}_projection_*.md
```

## File inventory

### Core algorithm

- **`vos_v2.py`** — The heart. Reads `data/PlayerData-{league}.csv` + `config/weights_v2.json`,
  emits `{league}/eval/evaluation_summary_{league}_{ts}.csv` with VOS_Score (current)
  and VOS_Potential on a 20-80 scale. Per-position scores for hitters,
  ability+arsenal for pitchers, plus development/age/personality/park
  adjustments. CLI: `--league`, `--park-factors`, `--draft`, `--contracts`.
  Optional `--contracts` pulls `/contract` and `/contractextension` from the
  league API for downstream tools.

### Valuation tools

- **`farm_value_old.py`** — Original farm valuation library. Calibrates VPC
  (dollars per VOS point) from MLB salaries vs `VOS_Potential`. Holds
  shared utilities: `read_csv_rows`, `write_csv`, `write_md_table`,
  `resolve_base_url`, `build_players_lookup`, `compute_vpc_base`, age curves,
  position bucketing. Many other modules import from this.
- **`farm_value.py`** — Newer org valuation. Uses VPC from `farm_value_old`,
  multiplies by `prospect_score` from `prospect_rankings` output.
- **`prospect_rankings.py`** — Ceiling-focused board:
  `prospect_score = VOS_Potential × m_age × m_pos_role`. Pools: prospects,
  free_agents, non_org, all.

### Contract tools

- **`contract.py`** — Single-player contract valuator. Calibrates VPC,
  projects per-year VOS through age curve, applies type multipliers
  (pre_arb / arb ladder / extension_fa / market) and elite-tier premiums,
  hands total to `contract_builder`. Has a `fallback_structure` if builder
  can't hit target.
- **`contract_builder.py`** — Pure structurer. Builds 2x-rule-compliant
  contracts minimizing guaranteed money.

### Draft tools

- **`draft_values.py`** — Power-law decay pick value table.
- **`draft_pool_analysis.py`** — Reports off VOS tier cutoffs (62/54/48).
- **`draft_grades.py`** — Grades actual draft vs your pre-draft pool projections.
- **`draft_grades_pdf.py`** — Reportlab PDF of grades.

### Depth chart / projection tools (recent)

- **`stats.py`** — Fetcher + derived stat math for the StatsPlus v2 stat
  endpoints (`playerbatstatsv2`, `playerpitchstatsv2`, `playerfieldstatsv2`).
  Year-weights counting stats, computes wOBA / FIP / K-BB% / GB% / FPCT etc.
  League constants (lg_wOBA, cFIP) calibrated from same fetched data.
  Disk cache (calendar-day TTL) keyed by URL. `aggregate_ratings` for
  non-additive cols (framing/arm/zr) separate from `aggregate_counting`.
- **`depth_chart.py`** — Builds depth chart, lineups, pitching staff for
  one org at one level. Uses VOS + stats blended composite. Surfaces
  promotion (vs starter + vs bench), replacement, and demotion candidates
  from the level below. Lineup follows *The Book* — best 3 hitters at 1/2/4,
  4th-best at 5, 5th-best at 3, descending after that. Outputs `.md` + `.csv`
  + `_constants.json` sidecar.
- **`project_season.py`** — Pythagenpat win projection from a depth_chart
  CSV + sidecar. RS from PA-share-weighted lineup wOBA blended 70/30 R/L,
  RA from IP-share-weighted FIP across roles. Defense shade off VOS.
  Knobs: `--blend-current-fip` (default 0.5), `--defense-shade-strength`
  (default 0.005), `--vs-r-share` (0.70), `--games`.
- **`free_agent_market.py`** — Filters eval CSV to FAs (no Org), runs the
  same composite math, compares each against your depth chart at the chosen
  level. Imports from `depth_chart.py` for shared logic. MD has four
  sections: FA hitters by composite, FA pitchers by composite, hitter
  upgrade targets vs starters, pitcher upgrade targets vs weakest in role.
  CLI: `--league`, `--org`, `--level`, `--year`, `--top-n-hitters`,
  `--top-n-pitchers`, `--min-pa`, `--min-ip`. Fetches stats from every lid
  in `league_ids.json` for the league (FAs could be at any level), with
  cache reuse across tools.

### Other

- **`org_depth_analysis.py`** — Per-org depth report.
- **`scrape_prospects.py`** — Pulls SAHL top-100 player pages locally.
- **`statsplus_paper_news.py`** — Newspaper-style HTML/PDF from league news.
- **`what_if.py`** — Interactive single-player VOS rating sandbox.

## Config files (`config/`)

- **`weights_v2.json`** — Master VOS configuration: tool category weights,
  position weights, positional standards, role balance for pitchers,
  arsenal evaluation, development/age/personality adjustments, normalization.
- **`teams-{league}.json`** — Team ID → display name mapping per league.
- **`park-factors-{slug}.json`** — Optional park adjustments for VOS.
- **`league_url.json`** — League slug → API base URL mapping.
- **`league_ids.json`** — League slug → level → list of `lid` IDs (3-digit
  StatsPlus league IDs). Powers stat endpoint filtering by minor league
  level. Currently configured for sahl, tlg, wwoba, woba. Keys starting
  with `_` (like `_independents`) are ignored by lookup.
- **`depth_config.json`** — Per-level config for depth_chart.py:
  roster_size, hitter_count, pitcher_count, hitter_position_min,
  pitcher_role_count, ratings_weight + stats_weight (per level),
  year_weights (default 55/35/10), wOBA linear weights, promotion
  thresholds (`min_advantage_for_promote` default 2.5,
  `underperform_threshold` default -1.5, sample-size floors).
- **`id_maps.json`** — League level ID → label (e.g. `1 → ML`, `2 → AAA`).
- **`contract_config.json`** — Type multipliers, age curves, elite tiers,
  risk discount, contract defaults for `contract.py`.

## Output directories (per league)

- `{league}/eval/` — VOS evaluation_summary CSVs + companion MD
- `{league}/farm/` — farm value outputs
- `{league}/prospects/` — prospect ranking outputs
- `{league}/depth/` — depth chart + projection outputs
- `{league}/cache/stats/` — stats endpoint disk cache (1-day TTL)
- `{league}/drafts/{year}_*/` — per-draft pool/grades

## Typical workflows

**Refresh full evaluation + downstream:**
```
py vos_v2.py --league sahl --park-factors config/park-factors-sahl.json --contracts
py prospect_rankings.py --league sahl
py farm_value.py --league sahl
```

**Build a depth chart and project the season:**
```
py vos_v2.py --league sahl --park-factors config/park-factors-sahl.json
py depth_chart.py --league sahl --org "Houston Astros" --level ML --year 2061
py project_season.py --league sahl --org "Houston Astros" --level ML --blend-current-fip 0.7
```

**Scout the free agent market:**
```
py free_agent_market.py --league sahl --org "Houston Astros" --level ML --year 2061
```

**Investigate one player interactively:**
```
py what_if.py --league sahl --id 12345
```

## Conventions and gotchas

1. **`--year` is the OOTP season, not real time.** Stats fetch defaults to
   `datetime.now().year` if not passed, which is wrong for OOTP unless the
   league happens to be real-time-aligned. **Always pass `--year` to
   depth_chart and project_season.**

2. **`lid` filtering matters.** The StatsPlus v2 stat endpoints default to
   "top-level leagues" (i.e. ML only) when no `lid` is supplied. For non-ML
   levels the lid mapping in `league_ids.json` must be present, or the
   stats fetch will return empty. When fetching for a level, depth_chart
   automatically pulls both target level lids + level_below lids (so
   promotion candidates also have stats). `target_lids` is a separate
   subset used to scope league-average constants (lg_wOBA, lg_FIP, etc.)
   to the projection level only — no cross-level dilution.

3. **OOTP IP encoding.** Pitching IP is stored as `outs / 3` decimal
   (like 65.2 = 65 ⅔ innings), not real decimal. `stats.py` prefers the
   `outs` column when available; falls back to thirds-decimal conversion.

4. **Year-weighted aggregation.** `aggregate_counting` is for additive
   stats (PA, AB, IP, etc.) and applies year_weights without renormalizing.
   `aggregate_ratings` is for non-additive cols (framing/arm/zr) and
   normalizes by the sum of present-year weights — so a single-year sample
   resolves to that year's value, not `weights[0] × value`.

5. **Composite math (depth_chart).** `composite = rw·VOS + sw·stat_score`
   with `effective_sw = sw × sample_weight` (linear ramp from 0 PA / 0 IP
   to the floors in `depth_config.json`), then renormalized so weights
   sum to 1 even when stats are dampened. Effect: low-sample players fall
   back toward VOS instead of being penalized by an unstable z-score.

6. **Markdown + CSV pattern.** Most tools write both `.csv` and `.md`
   alongside each other. The Markdown is for Obsidian-style quick reference;
   the CSV is for downstream consumption.

7. **MD report sort order in promotion candidates** is by best edge
   (max of vs-starter and vs-bench). Sutter's actual upgrade vs Dwyer
   (the starter) shows alongside vs Rojel (the worst LF), making the
   real decision explicit.

## Calibration notes (observed across leagues)

The depth_chart → project_season pipeline has been validated against two
real seasons in progress:

- **Houston Astros (sahl ML 2061)**: projected 77 wins, actual pace 79.
  RS within 5%, RA within 2%, team FIP within 0.1.
- **Arizona Diamondbacks (wwoba ML 2039)**: projected 64 wins, actual
  pace 65. RS within 2%, RA understated by ~10%.

Known systematic biases (small but worth knowing):

- **lg_R_per_PA derived from API data tends ~5% low** vs reality. RS is
  consistently slightly underprojected. Could expose a `--rs-scale` knob
  if a third sample confirms the direction.
- **Defense shade can pull RA the wrong way** when VOS rates defense above
  what real run-prevention bears out. `--defense-shade-strength 0.002`
  is more conservative than the default 0.005; `--no-defense-shade`
  disables entirely.
- **Team FIP undershoots** when the depth chart's intended top-13 doesn't
  match the manager's actual deployed staff. Real-life call-ups, injuries,
  swing roles add high-FIP innings the model doesn't see. Future fix
  candidate: an `--actual-ip-weighted` mode using fetched IP totals.

## Recent feature work

In rough order of build:

1. `depth_chart.py` and `stats.py` introduced.
2. `league_ids.json` added, lid filtering wired through stats fetcher.
3. `project_season.py` introduced (Pythagenpat).
4. Lg constants filtered to target level only.
5. Current-year FIP exposed alongside 3-year-weighted; `--blend-current-fip`
   in projection.
6. `--defense-shade-strength` knob.
7. Sabermetric (The Book) lineup ordering — top 3 hitters at 1/2/4.
8. Disk cache for stats fetches (calendar-day TTL).
9. Promotion candidate table now shows vs-starter + vs-bench comparisons
   for hitters; sorted by largest edge.
10. New "Hitter Composites" section showing all level hitters ranked by
    composite (mirrors pitching staff structure).
11. Replacements list deduped.
12. `free_agent_market.py` introduced — imports shared logic from
    `depth_chart.py`, reuses the stats cache, evaluates FAs at the chosen
    level's weights and compares them against your depth chart.

## Style preferences when responding

- Short answers, no repetition, no sycophancy.
- Honest assessment over reassurance. Say what's likely wrong, not just
  what's right.
- Don't over-format — minimal headers/bullets unless the structure helps.
- Don't wrap up every code change with a long restatement of what was done.
- Code comments should explain *why* and tradeoffs, not narrate *what*.
- The user is comfortable with terse technical exchanges and prefers them.
