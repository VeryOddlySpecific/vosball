# `budget_audit.py` — Usage Guide

Joins the service-time payroll audit with team financial data to produce a per-team integrated view (committed contracts × budget × revenue) plus floor/cap stress-test scenarios.

This is the third stage of the pipeline. It depends on outputs from `payroll_audit.py` and `parse_financials.py`.

## What it does

1. Loads the per-contract audit CSV from `payroll_audit.py`.
2. Loads the per-team financials CSV from `parse_financials.py`.
3. Joins them — preferring `team_id` (numeric, stable), falling back to case-insensitive normalized team name.
4. Produces:
   - A per-team table combining committed contract $ (CC/EXT/FA buckets) with budget, payroll, media revenue, total revenue, and projected balance.
   - Floor stress tests at 70/80/90% of median, both on current cash payroll and on total committed contract $.
   - Cap stress tests at 110/125/150% of median, same two bases.
   - For each scenario: who's affected, dollars required to comply, % of budget that represents, and projected balance after.

## Inputs

| Input | Required? | Purpose |
|---|---|---|
| `--audit-csv` | yes | `payroll_audit_contracts_<league>_<TS>_svc.csv` from `payroll_audit.py` |
| `--financials` | yes | `<league>_team_financials.csv` from `parse_financials.py` |
| `--league` | optional | League slug for output filenames |
| `--out-dir` | optional | Defaults to `dirname(--audit-csv)` |

## Outputs

| File | What it contains |
|---|---|
| `payroll_budget_per_team_<league>_<TS>.md` | Narrated table joining bucket commitment with financials, plus dispersion summary |
| `payroll_budget_per_team_<league>_<TS>.csv` | Same data as a CSV for downstream analysis |
| `cap_floor_scenarios_<league>_<TS>.md` | 12 stress-test scenarios (6 floor × 2 bases, 6 cap × 2 bases) |

## Usage

```
python budget_audit.py \
    --league sdmb \
    --audit-csv  sdmb/contract_audit/payroll_audit_contracts_sdmb_<TS>_svc.csv \
    --financials sdmb/contract_audit/sdmb_team_financials.csv
```

For other leagues:

```
python budget_audit.py \
    --league sahl \
    --audit-csv  sahl/contract_audit/payroll_audit_contracts_sahl_<TS>_svc.csv \
    --financials sahl/contract_audit/sahl_team_financials.csv
```

## Generalizing to a new league

This script is league-agnostic. Just point it at the two input CSVs.

The only place where league-specific defaults are baked in is the scenario thresholds:

```python
# Floor levels (as % of median)
for p in (0.70, 0.80, 0.90):  ...
# Cap levels (as % of median)
for p in (1.10, 1.25, 1.50):  ...
```

If your league wants to model different thresholds, edit those tuples in `write_scenarios()`.

## Reading the outputs

### Per-team table

The headline columns:

- **Budget** — owner-set ceiling for the season
- **Payroll** — current cash payroll (this year)
- **Headroom** — `Budget − Payroll`. Positive = room to add. Negative = team is already over.
- **Media rev / Total rev** — current-season revenue
- **Proj bal** — projected end-of-season balance
- **Committed** — sum of remaining future $ on all active contracts (multi-year deals included)
- **EXT $ (n)** — committed dollars in the pre-FA extension bucket, with contract count
- **Pre-FA %** — share of committed $ that's in CC + EXT (pre-FA buckets)

A team with high **Committed** but moderate **Payroll** has back-loaded deals or hasn't started cashing the big years yet. A team with low **Pre-FA %** is buying its roster on the open market each year — those are the teams hit hardest by any floor proposal because they have nothing else to lean on.

### Floor/cap scenarios

Each scenario:

- **Floor at X% of median**: teams below that level would be forced to add payroll. Shows required +$, % of budget, and projected balance after.
- **Cap at X% of median**: teams above that level would be forced to cut. Shows required -$ and what EXT $ they have on the books (since EXT contracts are typically guaranteed).

The **committed-contract basis** scenarios are mostly illustrative — most leagues can't retroactively void guaranteed contracts, so a cap on total committed $ usually isn't implementable. But the gap shows how unrealistic raw-$ caps become at the committed level.

The cash-payroll basis is the realistic one for current-season cap/floor proposals.

## Troubleshooting

**"WARN: teams in audit with no financials match: [...]"** — One or more teams from the audit CSV didn't find a match in the financials CSV. Causes:

1. The team isn't in the financial report HTML (was it not yet promoted to ML?). Fix the input or accept the gap.
2. Team name in the audit CSV doesn't match the financial CSV. Re-run `payroll_audit.py` with `--league <slug>` so canonical names come from `config/teams-<league>.json` on both sides.
3. `team_id` in the audit CSV is 0 (the eval CSV's `Contract_team_id` column was empty for that team). Check the eval CSV.

**Floor scenarios show "No teams below this floor"** — Median payroll is already below the floor level on a small league. Lower the floor percentage in the script, or check that you're using the right basis (cash payroll vs committed).

**Numbers don't reconcile against the OOTP UI** — `committed_total` is *remaining future $ on active ML contracts*, not historical paid-out money and not the team's current-season cash payroll. They're different metrics and won't equal what you see in OOTP's team finance view for the current season. `current_payroll` from the financials CSV is the cash-this-season number.

**Want different scenario thresholds** — Edit the loop in `write_scenarios()`. The two tuples `(0.70, 0.80, 0.90)` and `(1.10, 1.25, 1.50)` are the only knobs.

## Pipeline position

```
payroll_audit.py ──► payroll_audit_contracts_<...>.csv ──┐
                                                          │
parse_financials.py ──► <league>_team_financials.csv ────┤
                                                          │
                                                          ▼
                                                  budget_audit.py
                                                          │
                                                          ├──► payroll_budget_per_team_<...>.md
                                                          ├──► payroll_budget_per_team_<...>.csv
                                                          └──► cap_floor_scenarios_<...>.md
```
