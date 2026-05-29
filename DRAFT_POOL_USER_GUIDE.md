# Draft Pool Analysis — User Guide

Quick reference for `draft_pool_analysis.py`. Generates a multi-file analysis package from a VOS v2 evaluation summary CSV (post-draft or pre-draft pool).

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

```
py draft_pool_analysis.py <draft_pool.csv>
```

Input must have `Projected_Position` (or `Ideal_Position`/`Ideal Pos`) and `Ideal_Value` (or `VOS_Potential`/`VOS Potential`) columns. Rows missing either are silently dropped.

**Flags:**

- `--output-dir PATH` — write reports to a specific folder (skips auto-naming).
- `--name LABEL` — folder becomes `draft_pool_analysis_{LABEL}` instead of a timestamp.

**Path resolution:** Relative input paths are tried against the script dir, then the parent dir, then used as-is.

**Auto-output location:** If `--output-dir` is omitted, the script infers the league from filenames starting with `evaluation_summary_{league}_*.csv` and writes to `{league}/drafts/draft_pool_analysis_{timestamp}/`. Otherwise it drops the folder next to the script.

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

1. Run `vos_v2.py` on the draft pool data → produces `evaluation_summary_{league}_{ts}.csv`.
2. Feed that CSV into `draft_pool_analysis.py`.
3. Open `00_summary.txt` first for the overview. Use `05_draft_pool.md` as the working board during the draft. Use `summary_data.md` for quick-reference tables in Obsidian.

---

## Common gotchas

- Empty output means the CSV has no rows where both `Projected_Position` and `Ideal_Value` are present. Check column names.
- Tier cutoffs are fixed values, **not** percentiles of this pool — a weak draft class can legitimately have zero Elite prospects.
- `Viable Pos Potentials` in `05_draft_pool.md` reads `Projected_Viable_Pos_List` and looks up `{pos}_Potential` columns. If those columns aren't in the CSV, the cell is blank.
- The script doesn't compare against prior draft pools — for cross-year context, run it on each year's CSV and diff the summaries manually.
