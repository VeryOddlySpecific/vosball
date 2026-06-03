# `payroll_audit.py` — Usage Guide

Service-time-based payroll composition audit for an OOTP league. Buckets every active ML contract into CC / EXT / FA based on **MLB service time at signing** (not age), then aggregates per team and per bucket to surface where payroll dispersion comes from.

Methodology details live in `<league>/contract_audit/PAYROLL_AUDIT_README.md`. This file covers how to run the script.

## What it does

For each active ML contract in the evaluation CSV:

1. Computes `signing_svc = current_mlb_service_years - (Contract_current_year - 1)`.
2. Buckets:
   - **FA** if `signing_svc >= 6` (free-agent signing)
   - **EXT** if `signing_svc < 6` AND `aav_remaining >= $8M` (real pre-FA extension)
   - **CC** if `signing_svc < 6` AND `aav_remaining < $8M` (rookie scale / arb tender)
3. Falls back to the legacy `signing_age < 28` rule if service-time data is missing.
4. Rolls up per team. Computes variance contribution per bucket.

## Inputs

| Input | Source | Purpose |
|---|---|---|
| `--eval` | `<league>/eval/evaluation_summary_<league>_<TS>.csv` (from `vos_v2 --contracts`) | Per-player evaluation + contract data |
| `--players` | `<league>/cache/stats/players.csv` (auto-cached by the ratings pipeline) | Service-time lookup (`mlb_service_years` column) |
| `--league` | League slug — `sdmb`, `sahl`, `tlg`, etc. | Used for output filenames and to load `config/teams-<league>.json` for canonical team names |
| `--config-dir` (optional) | Defaults to `./config` next to the script | Where `teams-<league>.json` lives |
| `--out` (optional) | Defaults to `<eval_dir>/../contract_audit` | Output directory |

## Outputs

Written to the output directory, all stamped with the current timestamp:

| File | What it contains |
|---|---|
| `payroll_composition_audit_<league>_<TS>_svc.md` | Main commissioner-facing report: bucket totals, dispersion, variance contribution, per-team breakdown, full EXT roster |
| `payroll_audit_compare_<league>_<TS>_svc.md` | Diff of legacy age-based buckets vs new service-time buckets — useful for explaining "why these numbers are different than last time" |
| `payroll_audit_contracts_<league>_<TS>_svc.csv` | One row per active ML contract with all derived fields (svc, bucket, AAV, etc.) — input for `budget_audit.py` |

## Usage

```
python payroll_audit.py \
    --league sdmb \
    --eval   sdmb/eval/evaluation_summary_sdmb_20260513_145214.csv \
    --players sdmb/cache/stats/players.csv
```

For other leagues, swap the slug and paths:

```
python payroll_audit.py \
    --league sahl \
    --eval   sahl/eval/evaluation_summary_sahl_<TS>.csv \
    --players sahl/cache/stats/players.csv
```

## Generalizing to a new league

The script is league-agnostic by design. To run it on a league you haven't analyzed before:

1. **Have a current eval CSV.** Run `vos_v2.py --contracts --league <slug>` if you don't have one. The eval CSV must include the `Contract_*` columns.
2. **Have a current players cache.** This is `<league>/cache/stats/players.csv`. It's populated by the broader ratings pipeline (`contract_audit.py`, `farm_value_old.py`, etc.). If missing, those scripts populate it the first time they hit the `/players` endpoint. Or refresh it manually.
3. **Confirm `config/teams-<league>.json` exists.** If yes, team names are canonicalized automatically. If no, the script falls back to the raw `Org` field from the eval CSV (works, just less clean).
4. **Run with `--league <slug>`.** Outputs land in `<league>/contract_audit/` by default.

## Tuning the bucket thresholds

If your league's economy looks materially different from MLB (e.g., earlier free agency, no arbitration), edit the three constants at the top of the script:

```python
FA_SVC_THRESHOLD = 6.0          # MLB free-agent eligibility (service years)
ARB_SVC_THRESHOLD = 3.0         # MLB arbitration eligibility
EXT_AAV_THRESHOLD = 8_000_000   # split CC vs EXT within pre-FA
```

The `$8M` AAV threshold is where "extension that's actually paying real money" cuts in. If your league's salary scale is materially different, tune it.

## Troubleshooting

**"Contracts analyzed: 0"** — Almost certainly the eval CSV's `League_Level` column isn't being set to `"ML"`. Check what values are there:
```
awk -F, 'NR==1{for(i=1;i<=NF;i++)if($i=="League_Level")c=i} NR>1{print $c}' eval.csv | sort -u
```

**"Missing service-time fallbacks: N"** — N players didn't match in `players.csv` (or had blank `mlb_service_years`). Those rows used the age<28 heuristic instead. If N is large, refresh the players cache and re-run. <5% is fine to ignore.

**`Contract_current_year = 0` in the data?** That's OOTP's encoding for not-yet-started 1-year tenders. The script treats them as `cur = 1` automatically; no action needed.

**Team names look wrong / weird casing?** Either you forgot `--league`, or `config/teams-<league>.json` is missing the team_id. Add the team to the config and re-run, or run without `--league` and live with the raw eval-CSV names.

**Bucket boundaries feel off** — the CC/EXT split is heuristic (a 5.9-svc player at $7.9M AAV is CC; same player at $8.1M is EXT). Edge cases get a deterministic placement but are debatable. The FA/non-FA boundary is non-heuristic (6.0 service years is the bright line).

## Pipeline position

```
vos_v2 --contracts ──► evaluation_summary_<league>_<TS>.csv
                                │
                                │
              players.csv ──► payroll_audit.py ──► payroll_audit_contracts_<...>.csv
                                │                  │
                                │                  ├──► payroll_composition_audit_<...>.md
                                │                  └──► payroll_audit_compare_<...>.md
                                │
                                └─────────────────► budget_audit.py (next stage)
```
