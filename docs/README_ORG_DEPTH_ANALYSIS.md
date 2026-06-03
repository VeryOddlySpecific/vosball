# Organizational Depth Analysis

Python tool for analyzing baseball organizational depth across positions, league levels, and skill sets. Reads VOS v2 `evaluation_summary_*.csv` output and identifies weak spots, stockpiles, and strategic opportunities.

## Usage

```bash
python org_depth_analysis.py <evaluation_file>
# or
python org_depth_analysis.py --league <league>
```

**Examples:**

```bash
# Basic run with specific file
python org_depth_analysis.py evaluation_summary_sky_20260203_200615.csv

# Auto-detect latest evaluation file for league
python org_depth_analysis.py --league sky

# Filter to a single organization
python org_depth_analysis.py evaluation_summary_sky_20260203_200615.csv -o "Atlanta Braves"

# Full export: text, CSV, player details, HTML
python org_depth_analysis.py --league sky -o "Boston Red Sox" --csv --player-details --html

# Custom output path
python org_depth_analysis.py evaluation_summary_tlg.csv --output reports/braves_depth.txt
```

## Options

| Option | Description |
|--------|-------------|
| `evaluation_file` | Path to evaluation_summary CSV. Omit if using `--league`. |
| `--league` | League abbreviation (e.g. `sky`, `woba`, `tlg`). Auto-detects latest `evaluation_summary_{league}_*.csv` in current directory. |
| `-o`, `--org` | Filter to specific organization. Exact match on `Org` column (e.g. `"Atlanta Braves"`). |
| `--output` | Custom output base path. Default: `org_depth_analysis_{abbrev}.txt` (abbrev from org name or `all`). |
| `--csv` | Export position and skillset CSV reports. |
| `--player-details` | Export player details CSV (all players grouped by ideal position). |
| `--html` | Export interactive HTML report. |
| `--no-level-breakdown` | Omit the "Depth by Level" section from the text report. |
| `--weight-by-level` | Reserved for future use. |
| `--data-dir` | Directory for data files when using `--league` (default: current directory). |

## Inputs

- **evaluation_summary_{league}_{timestamp}.csv** — Output from VOS v2 (`vos_v2.py`). Must include:
  - **Identifiers:** ID, Name, Pos, Age, Team, Org, League_Level
  - **Scores:** VOS_Score, Ideal_Position, Ideal_Value
  - **Component scores (optional):** Batting_Score, Defense_Score, Baserunning_Score (hitters); Pitching_Ability_Score, Pitching_Arsenal_Score (pitchers)

The tool supports both VOS v2 column names (`Org`, `League_Level`, `Ideal_Position`, etc.) and legacy names (`Organization`, `League Level`, `Ideal Pos`) via built-in mappings.

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

These are tuned for VOS v2’s sigmoid-normalized 20–80 scale (50 = average).

## File Naming

Output filenames use organization abbreviations:

- **MLB teams** — Standard abbreviations (ATL, BOS, NYY, etc.)
- **Other orgs** — First letters of words (e.g. "Portland Hops" → PH)
- **All orgs** — `all`

## Workflow

1. Run VOS v2 to generate `evaluation_summary_{league}_{timestamp}.csv`
2. Run org_depth_analysis with that file (or `--league` to use latest)
3. Use `-o "Org Name"` to analyze a single organization
4. Add `--csv`, `--player-details`, `--html` as needed for exports

## Compatibility

- **VOS v2:** Primary target. Uses `Org`, `League_Level`, `Ideal_Position`, `Ideal_Value`, and component score columns.
- **Legacy:** Falls back to `Organization`, `League Level`, `Ideal Pos`, `Ideal Value` if VOS v2 names are missing.
- **Pitchers:** VOS v2 evaluates all pitchers as SP; the tool uses SP expected depth (40) for all pitchers.
