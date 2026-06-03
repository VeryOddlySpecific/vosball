# Draft Pool Analysis — User Guide

Quick reference for [`tools/draft_pool_analysis.py`](../tools/draft_pool_analysis.py). Generates a multi-file analysis package from a VOS v10 draft-pool evaluation CSV.

> Draft tooling isn't in the app yet (Draft Room is a planned page); run it from the command line for now. The primary VOSBall interface is the Streamlit web app (`py -m streamlit run webapp/app.py`), but the draft pipeline is CLI-only today.

---

## What it does

Takes one evaluation summary CSV → writes 8 report files into a timestamped folder. Reports cover position distribution, position strength (mean/median/percentiles), VOS Potential distribution, and prospect tier breakdown using fixed cutoffs (not percentiles).

Tier cutoffs (hard-coded in `VOS_TIER_BENCHMARKS`):

- **Elite:** VOS Potential ≥ 62
- **Plus:** 54–61
- **Average:** 48–53
- **Org Depth:** < 48

---

## Run it

Auto-resolve from a league slug (recommended) — finds the newest draft eval and computes the Outlook column:

```
py tools\draft_pool_analysis.py --league sahl --name 2061_draft
```

Or pass an explicit CSV path:

```
py tools\draft_pool_analysis.py <draft_pool.csv>
```

Input must have `Projected_Position` (or `Ideal_Position`/`Ideal Pos`) and `Ideal_Value` (or `VOS_Potential`/`VOS Potential`) columns. Rows missing either are silently dropped.

**Common flags:**

- `--league SLUG` — auto-resolve the newest `draft_evaluation_{league}_*.csv` under `{league}/eval/` (falls back to `evaluation_summary_*`). Mutually exclusive with the positional CSV path.
- `--name LABEL` — folder becomes `draft_pool_analysis_{LABEL}` instead of a timestamp.
- `--output-dir PATH` — write reports to a specific folder (skips auto-naming).
- `--org-code CODE` — with `--league`, look in `{league}/eval/{org_code}/` first.
- `--no-prefer-draft` — skip `draft_evaluation_*` and use `evaluation_summary_*` (mid-season, when `--draft` hasn't been re-run).
- `--skip-outlook` — don't compute the Outlook column (use when PlayerData isn't available).
- `--draft-pool-ids PATH` / `--no-draft-pool-filter` — control the draft-eligible ID filter (auto-detected at `data/draft_pool_{league}.csv` with `--league`).

**Path resolution:** Relative input paths are tried against the script dir, then the parent dir, then used as-is.

**Auto-output location:** With `--league` (or when the script infers the league from `draft_evaluation_{league}_*.csv` / `evaluation_summary_{league}_*.csv`), reports land in `{league}/drafts/draft_pool_analysis_{name|timestamp}/`. Otherwise the folder drops next to the script.

---

## What you get

| File | Contents |
| --- | --- |
| `00_summary.txt` | Headline numbers: pool size, mean/median VOS Potential, top/bottom 5 positions, tier counts |
| `01_position_distribution.txt` | Player counts per position + Infield/Outfield/Pitching/DH rollup |
| `02_position_strength.txt` | Mean, median, min, max, P25/P75/P95 per position; strongest/weakest rankings |
| `03_vos_potential_distribution.txt` | Pool-wide stats, percentiles, histogram by 10-pt buckets |
| `04_prospect_tiers.txt` | Tier counts and per-tier stats |
| `summary_data.csv` | Machine-readable version of the three core tables |
| `summary_data.md` | Same tables in Markdown (for Obsidian) |
| `05_draft_pool.md` | Full pool sorted by Ideal Value desc, with viable position potentials and tier label |

---

## Typical workflow

1. Run the engine in draft mode: `py run_vos.py --league sahl --draft` → produces `{league}/eval/draft_evaluation_{league}_{ts}.csv` (the draft-pool eval). The `--draft` flag enables the draft-mode adjustment stack (Readiness, Draft_Age, Draft_RP_Penalty).
2. Feed that into `py tools\draft_pool_analysis.py --league sahl --name 2061_draft` (or pass the CSV path explicitly).
3. Open `00_summary.txt` first for the overview. Use `05_draft_pool.md` as the working board during the draft. Use `summary_data.md` for quick-reference tables in Obsidian.

For the full end-to-end draft pipeline (org depth, draft board, post-draft grading) see [DRAFT_WORKFLOW.md](DRAFT_WORKFLOW.md).

---

## Common gotchas

- Empty output means the CSV has no rows where both `Projected_Position` and `Ideal_Value` are present. Check column names.
- Tier cutoffs are fixed values, **not** percentiles of this pool — a weak draft class can legitimately have zero Elite prospects.
- `Viable Pos Potentials` in `05_draft_pool.md` reads `Projected_Viable_Pos_List` and looks up `{pos}_Potential` columns. If those columns aren't in the CSV, the cell is blank.
- The script doesn't compare against prior draft pools — for cross-year context, run it on each year's CSV and diff the summaries manually.
