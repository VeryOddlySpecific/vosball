# ratings

A Python toolkit for **baseball player evaluation and league management**, built around **VOS (VOS Optimized Score)** — a configurable, multi-model rating system that produces normalized 20–80 scores from raw tool data.

Designed to be league-agnostic: point it at a player data export, a league slug, and a config directory and it produces evaluation summaries, depth charts, draft boards, trade targets, and more.

---

## VOS v10

The core evaluation engine (`run_vos.py`) produces three complementary scores per player, all normalized to the 20–80 scouting scale via sigmoid:

| Score | What it measures |
|---|---|
| **VOS_Reach** | Ceiling potential — logistic models trained on Pot* ratings for hitters, SP, and RP separately |
| **VOS_Career** | Current production value — Stage-2 Spearman-tuned weights per position |
| **VOS_Blended** | Weighted composite (α = 0.4 reach, 0.6 career) for general use |

**What's new in v10:**
- Logistic reach models (hitter, SP, RP) replace the v9 linear approximations
- Career Personality model v5: Work Ethic carries ±3.0 pts; Leadership zeroed out after v9 audit showed noise
- RP model v9 integrated into the main weight file
- Age decay tuned per tool with peak-age and slope parameters
- Hard floor 20, ceiling 80; target center 50.0, scale 15.0

```bash
python run_vos.py --league <slug>
python run_vos.py --league <slug> --draft-mode
python run_vos.py --league <slug> --weights-override config/weights_v10.json
```

**Output:** `{league}/eval/{org}/evaluation_summary_{league}_{timestamp}.csv` + matching `.md`

---

## Tool Suite

### Roster & Depth

**`depth_chart.py`** — Builds ideal depth charts and lineups for an org at a given level, surfaces promotion/demotion candidates and slot assignments.
```bash
python depth_chart.py --league <slug> --org <org> --level MLB
python depth_chart.py --league <slug> --all-orgs
```

**`org_strength_report.py`** — Rolls up per-level depth charts into a positional strength report. Composites are z-scored within level; surfaces holes, surpluses, and essential players.
```bash
python org_strength_report.py --league <slug> --org <org>
python org_strength_report.py --league <slug> --all-orgs
```

**`project_season.py`** — Pythagorean win projection (Pythagenpat method) from a depth chart roster's run scoring and prevention components.
```bash
python project_season.py --league <slug> --org <org> --level MLB
```

---

### Free Agency, Trades & Waivers

**`free_agent_market.py`** — Ranks free agents by fit against your depth chart slots, blending VOS scores with live stat context.
```bash
python free_agent_market.py --league <slug> --org <org> --level MLB
```

**`trade_targets.py`** — League-wide trade block analysis; matches available players to your roster needs and ranks by fit. Batch runner: `run_trade_targets_all.py`.
```bash
python trade_targets.py --league <slug> --org <org>
```

**`waiver_wire.py`** — Grades the current waiver wire against your depth needs. Uses lower composite thresholds than trade targets (cost is a roster spot, not a trade asset); includes a "Stash" tier for high-upside fliers.
```bash
python waiver_wire.py --league <slug> --org <org>
```

---

### Draft

**`draft_pool_analysis.py`** — Comprehensive pre-draft analysis from a VOS evaluation. Produces six reports: summary, position distribution, position strength, ideal-value distribution, prospect tiers, and a full draft board.
```bash
python draft_pool_analysis.py --league <slug>
```

**`draft_grades.py`** — Post-draft grader. Compares picks against the pre-draft VOS pool projections, awards "VOS Stamps" for players taken at or after their projection slot, and produces A–F grades per org.
```bash
python draft_grades.py --league <slug>
```

---

### Contracts & Financials

**`contract_audit.py`** — Classifies every contract in the league as OVERPRICED, FAIR, or UNDERPRICED using VPC (Value Per Contract) + age-curve + risk discount. Produces a league summary, per-org rollup, and top steals/overpays list.
```bash
python contract_audit.py --league <slug>
python contract_audit.py --league <slug> --org <org>
```

**`contract_builder.py`**, **`budget_audit.py`**, **`payroll_audit.py`**, **`parse_financials.py`** — Supporting tools for building contracts, auditing payroll against budget, and parsing financial exports.

---

### Player Analysis

**`player_card.py`** — Single-player profile: name, VOS/Potential scores, full ratings block, all positional composite scores.
```bash
python player_card.py --league <slug> --id <player_id>
python player_card.py --league <slug> --id <player_id> --compare
```

**`farm_value.py`** — Farm system dollar valuation from prospect rankings using VPC calibration. Supports reach/career/blended as the score source.
```bash
python farm_value.py --league <slug> --score-source blended
```

**`hof_grade.py`** — Hall of Fame candidacy grader: WAR, JAWS, 7-year peak, Bill James Monitor/Standards, counting milestones, postseason boost.
```bash
python hof_grade.py --league <slug> --id <player_id>
```

**`awards_rank.py`** — Season-end awards rankings (MVP, Cy Young, ROTY, Gold Glove, Silver Slugger) using a transparent WAR + context blend. Supports AL/NL splits when division configs are present.
```bash
python awards_rank.py --league <slug> --year <year>
```

---

### Stats & Data

**`stats.py`** — Fetches and aggregates StatsPlus v2 stat endpoints (hitter/pitcher/fielder) over a 3-year window; computes derived stats (wOBA, FIP, K%, BB%) and league constants. Used internally by depth_chart, trade_targets, and waiver_wire.

**`fetch_player_data.py`** / **`fetch_all_player_data.py`** — Pull fresh player data exports from the StatsPlus API.

---

## Project Layout

```
ratings/
├── run_vos.py                  # Core VOS v10 evaluation engine
├── depth_chart.py              # Depth charts and lineup construction
├── project_season.py           # Pythagorean win projections
├── free_agent_market.py        # Free agent ranking by roster fit
├── trade_targets.py            # Trade block analysis
├── waiver_wire.py              # Waiver wire grader
├── draft_pool_analysis.py      # Pre-draft pool reports
├── draft_grades.py             # Post-draft VOS stamp grader
├── contract_audit.py           # Contract valuation audit
├── player_card.py              # Single-player profile
├── farm_value.py               # Farm system dollar valuation
├── hof_grade.py                # Hall of Fame candidacy grader
├── awards_rank.py              # Season awards rankings
├── stats.py                    # StatsPlus stat fetcher / aggregator
│
├── config/
│   ├── weights_v10.json        # VOS v10 weights (current)
│   ├── weights_rp_leverage_v1.json
│   ├── id_maps.json            # League level ID mapping
│   ├── league_ids.json
│   ├── league_settings.json
│   ├── contract_config.json
│   ├── depth_config.json
│   ├── draft_grades.json
│   ├── teams-wwoba.json        # Example league config
│   ├── divisions-wwoba.json
│   ├── wwoba_orgs.json
│   └── wwoba-park-factors.json
│
├── data/
│   └── PlayerData-wwoba.csv    # Example player data export
│
├── wwoba/                      # Example league output
│   ├── eval/                   # Evaluation summaries (CSV + MD per org)
│   ├── depth/                  # Depth charts
│   ├── farm/                   # Farm valuations
│   ├── drafts/                 # Draft pool analysis and grades
│   ├── trade_block/            # Trade targets
│   ├── waiver_wire/            # Waiver wire reports
│   └── org_depth/              # Org strength reports
│
├── lib/
│   ├── vos_decay.py
│   └── draft_score.py
│
└── run_vos_all.py              # Batch runners
    run_depth_chart_all.py
    run_trade_targets_all.py
    run_waiver_wire_all.py
```

---

## Requirements

Python 3.7+, standard library only. No external dependencies for the core evaluation engine.

The stat-fetching tools (`stats.py`, `fetch_player_data.py`, etc.) require network access to a StatsPlus API instance and credentials in `config/statsplus_tokens.json` (not included).
