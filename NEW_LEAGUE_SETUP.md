# New League Setup Guide

This guide covers everything required to onboard a new league into the ratings pipeline. The pipeline is config-driven: no code changes are needed for a new league (with one documented exception). Throughout this guide, replace `xxx` with the new league's lowercase ID (e.g. `ndl`).

## Information to gather first

Before touching any files, collect the following from the league:

1. **StatsPlus URL** — the API base, of the form `https://[host]/[league-slug]/api`
2. **League level IDs** — the 3-digit `NNN` values from each `league_NNN_home.html` page on the StatsPlus BNN, one per level (ML, AAA, AA, A+, A, R, plus any independent or international circuits)
3. **Team roster** — every team's internal ID, full city name, nickname, and parent-organization affiliation (ID of the parent ML team, or `0` if the team is itself an ML org or unaffiliated)
4. **Park factor source** — either per-park dimensions/factors, or a decision to run unadjusted
5. **Division structure** — only required if you intend to run season projections or org-depth rollups
6. **OOTP export** — a full player CSV with the complete tool-rating column set

## Config files to create

All of the following live in `F:\ratings\config\`.

### league_url.json (edit existing)

Add an entry:

```json
"xxx": "https://[host]/xxx/api"
```

The trailing `/api` is required. Scripts strip a trailing slash if present.

### league_ids.json (edit existing)

Add a block for the new league:

```json
"xxx": {
  "ML":  [100],
  "AAA": [101, 102],
  "AA":  [103, 104, 105],
  "A+":  [106, 107, 108],
  "A":   [109, 110],
  "R":   [111, 112, 113],
  "_independents": [150, 151],
  "_international": [160, 161]
}
```

Each value is a list of StatsPlus league IDs at that level. Keys prefixed with `_` are excluded from normal level-specific fetches and are only included when `--all-levels` is passed.

### teams-xxx.json (new file)

Maps every team's internal ID to its display info:

```json
{
  "1": {"Name": "Team City", "Nickname": "Team Name", "Parent": 0},
  "2": {"Name": "Affiliate City", "Nickname": "Affiliate", "Parent": 1}
}
```

Use `Parent: 0` for ML orgs and unaffiliated teams; use the parent's integer team ID otherwise.

### xxx_orgs.json (new file)

A flat array of org display names:

```json
["Arizona Diamondbacks", "Atlanta Braves", "Baltimore Orioles"]
```

### park-factors-xxx.json (new file, recommended)

Per-team adjustments for batting, defense, baserunning, and pitching. Use `sahl-park-factors.json` as a multi-park template, or `park-factors-lvk.json` for a single-park league. If omitted, evaluations run unadjusted.

### divisions-xxx.json (new file, optional)

Required only for `project_season.py` and org-depth rollups:

```json
{
  "League Name": {
    "Division 1": ["Team A", "Team B"],
    "Division 2": ["Team C", "Team D"]
  }
}
```

Team names here must match the `Org` column in the player data and the team keys in the park-factors file exactly (case-sensitive).

## Player data file

Save the OOTP export as `F:\ratings\data\PlayerData-xxx.csv`. Three column-level requirements:

- `Team` — integer matching a key in `teams-xxx.json`
- `Org` — display name matching the park-factors team keys and division lists exactly
- `LgLvl` — one of the level constants defined in `id_maps.json` (R, A, AA, AAA, ML, IND, INT, COL, HS)

The export must include all standard OOTP tool columns (batting, potential batting, fielding by position, baserunning, pitching ratings and pitch types, personality). Missing columns will fail VOS computation.

## Directory skeleton

Create `F:\ratings\xxx\` with the following subdirectories:

```
xxx/
├── cache/
├── depth/
├── drafts/
├── eval/
├── farm/
├── news/
├── org_depth/
├── prospects/
└── projections/    (only if running season projections)
```

## Files NOT to touch

These are shared across all leagues and must not be modified during onboarding:

- `weights_v2.json` — VOS weights, position standards, age curve
- `id_maps.json` — league-level label constants
- `contract_config.json` — contract valuation defaults
- `depth_config.json` — depth chart analysis settings

## Known code-level exception

`scrape_prospects.py` is hardcoded to SAHL. If the new league needs prospect scraping, update the `SOURCE_FILE` path in that script. Every other script in the pipeline reads `--league xxx` from the CLI and resolves all configs by convention.

## Validation steps

After the configs and data file are in place, run these to confirm wiring:

1. `vos_v2.py --league xxx` — produces `xxx/eval/` output
2. `depth_chart.py --league xxx --org "[Team Name]" --level ML` — confirms team/org lookup
3. `stats.py --league xxx --lids [ML id]` — confirms API connectivity
4. `org_depth_analysis.py` on the eval CSV — confirms org and division parsing

If any step fails, the cause is almost always a mismatch between `Team` IDs, `Org` names, or league IDs across the config files and the player CSV.

## Fastest path

Copy the `wwoba` config set as a starting template — it has the cleanest and smallest footprint of the existing leagues. Rename every key from `wwoba` to the new league ID, then fill in the league-specific values.
