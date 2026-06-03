# `parse_financials.py` — Usage Guide

Parses an OOTP "League Financial Report" HTML page into a per-team CSV. Used to feed team budget / revenue / expense / projected-balance data into `tools/budget_audit.py`.

> VOSBall's primary interface is the local Streamlit web app (`webapp/`). Finances aren't in the app yet (a Finances page is planned); run these from the command line.

## What it does

The OOTP financial report HTML (exported from in-game BNN reports) has one section per team with three sub-tables: GENERAL INFORMATION, CURRENT FINANCIAL OVERVIEW, LAST SEASON OVERVIEW. This script flattens all of them into one CSV row per team. Team names are canonicalized via `config/teams-<league>.json` so they match what `tools/payroll_audit.py` produces.

## Getting the input HTML

In OOTP:

1. Open the league you want to audit.
2. Go to **BNN → League Reports → Financial Report** (path may vary by OOTP version).
3. Save the rendered HTML page to disk. Typical output path: `<game>/news/html/temp/financial_report_<league>.html` — or you can right-click → Save Page As from the in-game browser.
4. Copy that HTML file to a known location, e.g. `<league>/contract_audit/<league>_league_financials.html`.

The HTML doesn't need any cleanup; the script handles OOTP's standard markup directly.

## Inputs

| Input | Required? | Purpose |
|---|---|---|
| `--input` / `-i` | yes | Path to the OOTP financial report HTML |
| `--output` / `-o` | yes | Path for the output CSV |
| `--league` | recommended | League slug (e.g. `sdmb`). When present, team names are looked up in `config/teams-<league>.json` for canonical naming. When absent, raw HTML team names are title-cased as a fallback. |
| `--config-dir` | optional | Defaults to `./config` |

## Outputs

A CSV with one row per team and ~30 columns. Key fields:

| Field | Source | Notes |
|---|---|---|
| `team` | Canonical from `teams-<league>.json`, fallback to title-cased HTML text | Join key for `tools/budget_audit.py` |
| `team_id` | URL in HTML (`team_NN.html`) | Stable numeric ID, preferred join key |
| `current_budget` | GENERAL INFO | Owner-set spending ceiling for the season |
| `player_payroll` | GENERAL INFO | Current cash payroll (this season only) |
| `staff_payroll` | GENERAL INFO | Coaching/scouting payroll |
| `projected_balance` | GENERAL INFO | Projected end-of-season balance |
| `cur_total_revenue` | CURRENT FINANCIAL | Total this-season revenue |
| `cur_media_revenue` | CURRENT FINANCIAL | This-season media deal revenue |
| `cur_gate_revenue` | CURRENT FINANCIAL | Gate receipts |
| `cur_season_ticket_revenue` | CURRENT FINANCIAL | Season ticket sales |
| `cur_merch_revenue` | CURRENT FINANCIAL | Merchandising |
| `cur_total_expenses` | CURRENT FINANCIAL | All expenses combined |
| `cur_current_balance` | CURRENT FINANCIAL | Current actual balance (snapshot) |
| `cur_attendance` | CURRENT FINANCIAL | Total attendance |
| `last_*` | LAST SEASON | Same fields, prior season |

Rows are sorted by `current_budget` descending in the output for convenience.

## Usage

```powershell
py tools\parse_financials.py ^
    --league sdmb ^
    --input  sdmb\contract_audit\sdmb_league_financials.html ^
    --output sdmb\contract_audit\sdmb_team_financials.csv
```

For other leagues:

```powershell
py tools\parse_financials.py ^
    --league sahl ^
    --input  sahl\contract_audit\sahl_league_financials.html ^
    --output sahl\contract_audit\sahl_team_financials.csv
```

## Generalizing to a new league

1. **Confirm the HTML matches OOTP's standard layout.** This script handles the default BNN "League Financial Report" structure (16-team OOTP league as seen in 2049–2050 builds). If your league uses a custom template or a much older version, you may need to adjust the regex selectors near the top of `parse_financials.py`.
2. **Confirm `config/teams-<league>.json` includes every team in the league financial report.** If a team_id from the HTML isn't in the config, the script falls back to title-casing the raw HTML name and logs a warning.
3. **Run.**

## Troubleshooting

**"no team sections matched in the HTML"** — The HTML probably isn't a League Financial Report. The script looks for `boxlink`-class anchors pointing to `team_NN.html`. If you saved a different report by mistake, the script can't parse it. Try saving the Financial Report specifically.

**"WARN: team_ids not in teams-<league>.json: [...]"** — Some teams in the HTML aren't in your league teams config. Add them and re-run, or accept the title-cased fallback for those rows.

**Team names don't match payroll_audit output** — Use `--league` on both scripts and make sure `teams-<league>.json` is current. `tools/budget_audit.py` joins on `team_id` first and then case-insensitive name, so minor naming drift is tolerated but consistency is cleaner.

**Money values look wrong** — Check that the HTML contains real dollar values (`$12,345,678`). Older OOTP versions may use different formats. The parser strips everything that isn't a digit or leading minus.

**Future season data missing** — The script only reads "current" and "last season" overviews. There's no projection data in the OOTP report to read.

## Pipeline position

```
OOTP in-game BNN ──► <league>_league_financials.html
                                │
                                │ tools/parse_financials.py
                                ▼
                  <league>_team_financials.csv
                                │
                                │
                                └────► tools/budget_audit.py
```

See [`USAGE_budget_audit.md`](USAGE_budget_audit.md) and [`USAGE_financial_audit_pipeline.md`](USAGE_financial_audit_pipeline.md).
