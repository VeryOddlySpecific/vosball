# VOS v2 (VOS Optimized Score)

Python script for baseball player evaluation using a weighted scoring system. Replaces the legacy `analyze.py` with a smaller, maintainable design and proper 20–80 normalization.

## Usage

```bash
python vos_v2.py --league <league>
```

**Examples:**

```bash
python vos_v2.py --league sky
python vos_v2.py --league woba --output evaluation_summary_woba.csv
python vos_v2.py --league sky --ids-file filter.txt
python vos_v2.py --league sky --park-factors config/park-factors-example.json
```

**Options:**

| Option | Description |
|--------|-------------|
| `--league` | **Required.** League slug (e.g. `sky`, `woba`). |
| `--output` | Output CSV path. Default: `evaluation_summary_{league}_{timestamp}.csv` |
| `--ids-file` | Optional file of player IDs (one per line or comma/semicolon separated) to limit evaluation. |
| `--park-factors` | Optional path to park-factors.json for ballpark-specific tool adjustments. |
| `--data-dir` | Data directory (default: `data`). |
| `--config-dir` | Config directory (default: `config`). |

## Inputs

- **PlayerData-{league}.csv** — In `data/`. Must include ID, Name, Pos, Age, Team, Org, LgLvl, and position-specific ratings (see script docstring).
- **weights_v2.json** — In `config/`. Defines batting/defense/baserunning and pitcher weights, positional standards, adjustments, and normalization.
- **teams-{league}.json** — In `config/`. Maps team IDs to names (e.g. `{"31": {"Name": "Arizona", "Nickname": "Diamondbacks", ...}}`).
- **id_maps.json** — In `config/`. Maps league level labels to numeric IDs (e.g. `{"league_level": {"ML": 1, "AAA": 2, ...}}`).

## Output

**evaluation_summary_{league}_{timestamp}.csv** (or path given by `--output`) with:

- **ID, Name, Pos, Age, Team, Org, League_Level**
- **VOS_Score** — Normalized to 20–80 scale (sigmoid-based).
- **Component scores:** Batting_Score, Defense_Score, Baserunning_Score (hitters); Pitching_Ability_Score, Pitching_Arsenal_Score (pitchers).
- **Adjustments:** Development_Adj, Age_Adj, Personality_Adj.
- **Position scores:** C_Score, 1B_Score, … DH_Score (hitters; empty for pitchers).
- **Park_Name, Park_Applied** — Home park name (or "N/A") and whether park factors were applied.
- **Ideal_Position, Ideal_Value** — Best position and its composite score (or SP/RP and combined score for pitchers).

## Park factors (optional)

When `--park-factors path/to/park-factors.json` is provided, two formats are supported:

### Single-park format (e.g. park-factors-lvk.json)

- **Use case:** Compare **all** players to one reference park (e.g. “how would everyone look in Las Vegas Knights Ballpark?”). No team lookup.
- **Input:** JSON with **top-level** `tool_adjustments` (batting, defense, baserunning, pitcher_ability), `team_info.park_name` (display name), optional `handedness_splits` (RHB/LHB), and `application_rules`.
- **Behavior:** The same park is applied to every player (subject to application_rules: apply_to_prospects, apply_to_major_leaguers).

### Multi-park format (e.g. park-factors.json)

- **Use case:** Apply each player’s **home team** park (team_to_park_mapping).
- **Input:** JSON with `parks` (park key → tool_adjustments, name), `team_to_park_mapping` (team ID or name → park key), and `application_rules`.

**Common:** Park multipliers are applied to **raw tool values before weighting**. Only tools with explicit multipliers are adjusted. `adjustment_strength` (0.0–1.0) scales strength. **Output:** `Park_Name` and `Park_Applied` in the CSV. **Fallback:** Missing or invalid file → warning and no park factors.

See `config/park-factors-lvk.json` (single-park) and `config/park-factors.json` (multi-park).

## Validation

The script logs the **VOS_Score** range after writing. All scores are clamped to the 20–80 band by the normalization function; the log confirms they fall within it.

## Architecture

- **Data loading** — CSV and JSON configs; missing columns handled via alternatives (e.g. `Steal` vs `StealAbi`).
- **Hitter evaluation** — Batting (Gap/Pow/Eye/Ks), defense per position (with positional standards), baserunning; composite position scores and ideal position.
- **Pitcher evaluation** — Ability (Stuff/Movement/Control/HR_Avoid), arsenal (pitch type + slot weights, diversity bonuses/penalties), stamina penalty for SP; combined score.
- **Adjustments** — Development (current vs potential tiers + gap), age vs level (target age and tolerance from config), personality (trait modifiers).
- **Park factors (optional)** — Multiplicative tool adjustments by home park (batting, defense, baserunning, pitcher_ability) before weighting; applied only when `--park-factors` is set and application_rules/team mapping match.
- **Normalization** — `normalize_to_20_80()`: sigmoid-style compression so values near 50 stay similar and extremes map into 20–80.
- **Output** — Single CSV with one row per player (pitchers evaluated as SP), including Park_Name and Park_Applied when park factors are used.

No hardcoded values; weights, thresholds, and modifiers come from `weights_v2.json` (and park multipliers from park-factors.json when provided).

---

## Organizational Depth Analysis

`org_depth_analysis.py` analyzes organizational depth across positions, league levels, and skill sets. It reads VOS v2 `evaluation_summary_*.csv` output and identifies weak spots, stockpiles, and strategic opportunities.

### Usage

```bash
# Basic usage with specific file
python org_depth_analysis.py evaluation_summary_sky_20260203_200615.csv

# Auto-detect latest file for league
python org_depth_analysis.py --league sky

# Filter to specific organization
python org_depth_analysis.py evaluation_summary_sky.csv -o "Atlanta Braves"

# Export all formats
python org_depth_analysis.py --league sky -o "Atlanta Braves" --csv --player-details --html
```

### Options

| Option | Description |
|--------|-------------|
| `evaluation_file` | Path to evaluation_summary CSV (or use `--league`) |
| `--league` | Auto-detect latest `evaluation_summary_{league}_*.csv` |
| `-o/--org` | Filter to specific organization (exact match on `Org` column) |
| `--output` | Custom output path (default: `org_depth_analysis_{abbrev}.txt`) |
| `--csv` | Export position and skillset CSV reports |
| `--player-details` | Export player details CSV grouped by position |
| `--html` | Export interactive HTML report with sortable tables |
| `--no-level-breakdown` | Hide level breakdown in text report |

### Output

- **Text report** — Positional strength scores, depth by level, weak spots, stockpiles, recommendations
- **Positions CSV** — Position-by-position metrics with level breakdowns
- **Player details CSV** — All players grouped by ideal position, sorted by Ideal_Value
- **HTML report** — Collapsible sections, sortable player tables per position

Uses `Ideal_Position` and `Ideal_Value` from VOS v2. Supports both VOS v2 and legacy column naming via built-in mappings.

---

## Draft Grades

`draft_grades.py` compares actual draft results (from the league API) to your VOS draft pool projections. It awards a **VOS Stamp** when a player was projected in the top 100 and was drafted at or after that projection (a “steal”), then grades each team A–F by how many stamps they received.

### Usage

```bash
# Grade using draft pool in directory (looks for 05_draft_pool.md or draft_pool.md)
python draft_grades.py 2038_wwoba_draft --num-teams <N>

# Use a different league API
python draft_grades.py 2038_wwoba_draft --num-teams <N> --api-url "https://other-league.statsplus.net/wwoba/api/draft/"

# Write outputs to a different folder
python draft_grades.py 2038_wwoba_draft --num-teams <N> --output-dir ./grades_out

# Grade only through a given overall pick (inclusive)
python draft_grades.py 2038_wwoba_draft --num-teams <N> --through-pick 50
```

### Options

| Option | Description |
|--------|-------------|
| `directory` | **Required.** Directory containing draft analysis output (e.g. `05_draft_pool.md`). |
| `--num-teams` | **Required.** Number of teams in the draft (used for managed-risk tier: reach < N spots earns 0.75 pts). |
| `--api-url` | Draft status API URL (default: `https://atl-01.statsplus.net/wwoba/api/draft/`). |
| `--output-dir` | Where to write CSVs (default: same as `directory`). |
| `--through-pick` | Grade only picks with `Overall <= N` (inclusive). Example: `--through-pick 50` grades draft results through overall pick #50. |
| `--raw-name` | Filename for raw comparison CSV (default: `draft_grades_raw.csv`). |
| `--summary-name` | Filename for team summary CSV (default: `draft_grades_summary.csv`). |

### Output

- **draft_grades_raw.csv** — One row per drafted player: Player Name, Team, Overall Pick, Projection Rank, Delta, Stamp Type (Top 100 / Later / blank), Points, VOS Stamp (Y/N).
- **draft_grades_summary.csv** — One row per team: Team, Top 100 Stamps, Later Stamps, Total Points, Rank, Grade. Sorted by rank. Grades are percentile-based (relative to league).

### Points and grading

- **Top 100** (projection rank 1–100), drafted at or after projection: **3 points** per stamp.
- **Later** (projection rank 101+), drafted at or after projection: **1 point** per stamp.
- Team **total points** = sum of all stamp points.
- **Grades** are assigned by **percentile rank** (relative to all teams in that draft), not fixed point thresholds:
  - **A** — Top 10%
  - **B** — Next 20%
  - **C** — Next 40%
  - **D** — Next 20%
  - **F** — Bottom 10%

  This adapts to the draft class: in a strong year you need more points for an A; in a weak year, fewer points can still earn top grades. Tied teams receive the same rank (best rank in the group).
