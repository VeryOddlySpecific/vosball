# Organizational Depth Analysis

CLI tools for rolling up an organization's depth across positions, league levels,
and skill sets — surfacing weak spots, stockpiles, and strategic opportunities.

> **Per-org Depth Charts live in the web app.** For interactive per-level
> lineup/depth/staff views, open the **Depth Charts** page in the local app
> (`py -m streamlit run webapp/app.py`, or `run_ui.bat`). The tools below are
> batch/CLI rollups that operate at the **org** level and consume the evaluation
> summary CSV produced by `run_vos.py` (the VOS v10 engine in the `vosball/`
> package).

Two related CLI tools:

- **`tools/org_depth_analysis.py`** (this doc) — positional strength, weak
  spots, and stockpiles for an org, read straight from the eval CSV.
- **`tools/org_strength_report.py`** — rolls up per-level **depth-chart** CSVs
  (from `core/depth_chart.py`) into an org strength report with league-relative
  percentiles. Run `py tools\org_strength_report.py --league <league> --org "<Org>"`
  (or `--all-orgs`).

## Usage

```powershell
py tools\org_depth_analysis.py <evaluation_file>
# or
py tools\org_depth_analysis.py --league <league>
```

**Examples:**

```powershell
# Basic run with specific file
py tools\org_depth_analysis.py evaluation_summary_sky_20260203_200615.csv

# Auto-detect latest evaluation file for league
py tools\org_depth_analysis.py --league sky

# Filter to a single organization
py tools\org_depth_analysis.py evaluation_summary_sky_20260203_200615.csv -o "Atlanta Braves"

# Full export: text, CSV, player details, HTML
py tools\org_depth_analysis.py --league sky -o "Boston Red Sox" --csv --player-details --html

# Custom output path
py tools\org_depth_analysis.py evaluation_summary_tlg.csv --output reports\braves_depth.txt
```

## Options

| Option | Description |
|--------|-------------|
| `eval_path` | Positional path to evaluation_summary CSV. Omit if using `-e`/`--league`. |
| `-e`, `--evaluation-file` | Path to evaluation_summary CSV (alternative to the positional arg). |
| `--league` | League abbreviation (e.g. `sky`, `woba`, `tlg`). Auto-detects latest `evaluation_summary_{league}_*.csv`. |
| `-o`, `--org` | Filter to specific organization. Exact match on `Org` column (e.g. `"Atlanta Braves"`). |
| `--output` | Custom output base path. Default: `org_depth_analysis_{abbrev}.txt` (abbrev from org name or `all`). |
| `--csv` | Export position and skillset CSV reports. |
| `--player-details` | Export player details CSV (all players grouped by ideal position). |
| `--html` | Export interactive HTML report. |
| `--no-level-breakdown` | Omit the "Depth by Level" section from the text report. |
| `--weight-by-level` | Weight players by league level. |

## Inputs

- **evaluation_summary_{league}_{timestamp}.csv** — Output from `run_vos.py`
  (written to `{league}/eval/`). Must include:
  - **Identifiers:** ID, Name, Pos, Age, Team, Org, League_Level
  - **Scores:** VOS_Score, Ideal_Position, Ideal_Value
  - **Component scores (optional):** Batting_Score, Defense_Score, Baserunning_Score (hitters); Pitching_Ability_Score, Pitching_Arsenal_Score (pitchers)

The tool supports both current VOS column names (`Org`, `League_Level`, `Ideal_Position`, etc.) and legacy names (`Organization`, `League Level`, `Ideal Pos`) via built-in mappings.

## Output

### Text Report (default)

Always generated. Writes to `org_depth_analysis_{abbrev}.txt` (or path from `--output`).

- **Positional strength scores** — Per-position table: Count, Quality, Avg Value, Top 3 Avg, Strength (0–100)
- **Depth by level** — Top 5 positions with player counts per level (ML, AAA, AA, A, A-, R, Unassigned)
- **Position group summary** — Infield, Outfield, Pitching aggregates
- **Weak spots** — Positions flagged for low depth, quality, or strength
- **Stockpiles** — Positions with excess depth (>150% of expected)
- **Skill set distribution** — Archetype counts (if archetype column exists; otherwise a note)
- **Strategic recommendations** — Actionable suggestions based on analysis

### CSV Reports (`--csv`)

- **org_depth_analysis_{abbrev}_positions.csv** — One row per position: Count, Quality_Count, Avg_Value, Top3_Avg, Expected, Threshold, Strength, Level_Breakdown
- **org_depth_analysis_{abbrev}_skillsets.csv** — Archetype distribution (only if archetype data present)

### Player Details CSV (`--player-details`)

- **org_depth_analysis_{abbrev}_player_details.csv** — All players grouped by `Ideal_Position`, sorted by `Ideal_Value` descending. Includes original player data and component scores.

### HTML Report (`--html`)

- **org_depth_analysis_{abbrev}.html** — Interactive report with:
  - Full text summary at top
  - Collapsible sections per position (click header to expand/collapse)
  - Sortable tables: click column headers to sort players
  - Player columns: ID, Name, Pos, Age, Team, Org, League_Level, VOS_Score, Ideal_Position, Ideal_Value, component scores

## Metrics Explained

### Strength Score (0–100)

Composite score per position:

- **Depth component (40 pts)** — Actual count vs. expected depth (capped at 2× expected)
- **Quality component (40 pts)** — Quality players vs. expected (players above position threshold)
- **Top talent component (20 pts)** — Top 3 average vs. threshold (capped at 1.5× threshold)

### Weak Spot Criteria

A position is flagged as weak if any of:

- Depth &lt; 70% of expected
- Quality count &lt; 50% of expected
- Top 3 average below quality threshold
- Strength score &lt; 50

### Stockpile Criteria

- **Positional:** Depth &gt; 150% of expected
- **Skill set:** Single archetype &gt; 15% of org with &gt; 5 players (when archetype data exists)

### Quality Thresholds (20–80 scale)

Position-specific thresholds for "quality" players:

- C, 2B, SS: 52
- 3B, CF: 54
- 1B, LF, RF, DH, SP: 55–56

These are tuned for VOS's sigmoid-normalized 20–80 scale (50 = average).

## File Naming

Output filenames use organization abbreviations:

- **MLB teams** — Standard abbreviations (ATL, BOS, NYY, etc.)
- **Other orgs** — First letters of words (e.g. "Portland Hops" → PH)
- **All orgs** — `all`

## Workflow

1. Run `run_vos.py` to generate `{league}/eval/evaluation_summary_{league}_{timestamp}.csv`
2. Run `org_depth_analysis.py` with that file (or `--league` to use latest)
3. Use `-o "Org Name"` to analyze a single organization
4. Add `--csv`, `--player-details`, `--html` as needed for exports

## Compatibility

- **Current VOS (`run_vos.py`):** Primary target. Uses `Org`, `League_Level`, `Ideal_Position`, `Ideal_Value`, and component score columns.
- **Legacy:** Falls back to `Organization`, `League Level`, `Ideal Pos`, `Ideal Value` if the current names are missing.
- **Pitchers:** the eval evaluates all pitchers as SP; the tool uses SP expected depth (40) for all pitchers.

## See also

- `../run_vos.py` and the `vosball/` package — VOS v10 scoring and evaluation
  summary generation (replaces the retired `vos_v2.py`; its old doc is archived at
  `archive/README_VOS_V2.md`).
- `../tools/org_strength_report.py` — per-level depth-chart rollup with
  league-relative percentiles.
- `../core/depth_chart.py` — produces the per-level depth CSVs `org_strength_report.py` consumes.
- `FARM_VALUE_README.md` — org farm-system valuation and ranking.
