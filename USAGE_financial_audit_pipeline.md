# Financial Audit Pipeline — Quick Guide

Three scripts, run in order. Each one's output feeds the next.

## Flow

```
   ┌─────────────────────────────┐    ┌──────────────────────────────┐
   │  evaluation_summary_<TS>.csv │   │  league_financials.html      │
   │  (from vos_v2 --contracts)   │   │  (saved from OOTP BNN report) │
   └────────────┬─────────────────┘   └──────────────┬───────────────┘
                │                                    │
                │ + players.csv (service times)      │
                ▼                                    ▼
   ┌─────────────────────────────┐    ┌──────────────────────────────┐
   │       payroll_audit.py      │    │      parse_financials.py     │
   │  bucket contracts by svc    │    │   OOTP HTML → CSV            │
   └────────────┬─────────────────┘   └──────────────┬───────────────┘
                │                                    │
                │  payroll_audit_contracts_*.csv     │  *_team_financials.csv
                ▼                                    ▼
                └──────────────┬─────────────────────┘
                               ▼
                ┌─────────────────────────────┐
                │       budget_audit.py        │
                │  join + cap/floor scenarios  │
                └────────────┬─────────────────┘
                             ▼
                ┌────────────────────────────┐
                │  payroll_budget_per_team_* │
                │  cap_floor_scenarios_*     │
                └────────────────────────────┘
```

## Step 1 — payroll_audit.py

**Needs:**
- `<league>/eval/evaluation_summary_<league>_<TS>.csv` (from `vos_v2 --contracts`)
- `<league>/cache/stats/players.csv` (already maintained by the ratings pipeline)

**Run:**
```
python G:\ratings\payroll_audit.py ^
    --league <league> ^
    --eval    G:\ratings\<league>\eval\evaluation_summary_<league>_<TS>.csv ^
    --players G:\ratings\<league>\cache\stats\players.csv
```

**Produces** in `<league>/contract_audit/`:
- `payroll_composition_audit_<league>_<TS>_svc.md` — commissioner report
- `payroll_audit_compare_<league>_<TS>_svc.md` — age vs service-time methodology diff
- `payroll_audit_contracts_<league>_<TS>_svc.csv` — per-contract dump (input to step 3)

## Step 2 — parse_financials.py

**Needs:**
- A saved OOTP BNN financial report HTML. In game: **BNN → League Reports → Financial Report → Save As** (place it in `<league>/contract_audit/`).

**Run:**
```
python G:\ratings\parse_financials.py ^
    --league <league> ^
    --input   G:\ratings\<league>\contract_audit\<league>_league_financials.html ^
    --output  G:\ratings\<league>\contract_audit\<league>_team_financials.csv
```

**Produces:** `<league>_team_financials.csv` — one row per team with budget, payroll, revenue lines, expenses, projected balance, attendance.

## Step 3 — budget_audit.py

**Needs:** outputs of steps 1 and 2.

**Default run (% scenarios only):**
```
python G:\ratings\budget_audit.py ^
    --league <league> ^
    --audit-csv  G:\ratings\<league>\contract_audit\payroll_audit_contracts_<league>_<TS>_svc.csv ^
    --financials G:\ratings\<league>\contract_audit\<league>_team_financials.csv
```

**With proposed thresholds + cap phase-in:**
```
python G:\ratings\budget_audit.py ^
    --league <league> ^
    --audit-csv  ...payroll_audit_contracts_<league>_<TS>_svc.csv ^
    --financials ...<league>_team_financials.csv ^
    --floor-dollars 135000000 ^
    --cap-dollars   200000000 ^
    --cap-phase     3 ^
    --basis         budget
```

**Produces:**
- `payroll_budget_per_team_<league>_<TS>.md` — joined team table (payroll × budget × revenue)
- `payroll_budget_per_team_<league>_<TS>.csv` — same data as CSV
- `cap_floor_scenarios_<league>_<TS>.md` — full stress test (existing % scenarios + your explicit-$ scenarios if provided)

## Inputs checklist for a new league

Before step 1, make sure these exist (most are maintained automatically):

- [ ] `config/teams-<league>.json`
- [ ] `<league>/eval/evaluation_summary_<league>_<TS>.csv` (run `vos_v2 --contracts --league <league>`)
- [ ] `<league>/cache/stats/players.csv` (refreshed by the ratings pipeline)
- [ ] `<league>/contract_audit/<league>_league_financials.html` (you save this manually from OOTP)

## CLI reference

For per-script flag details:
- `USAGE_payroll_audit.md`
- `USAGE_parse_financials.md`
- `USAGE_budget_audit.md`

For methodology and findings interpretation, see `<league>/contract_audit/PAYROLL_AUDIT_README.md` (SDMB version is the canonical example).
