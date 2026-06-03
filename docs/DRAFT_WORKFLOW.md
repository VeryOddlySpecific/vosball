# Draft Analysis Workflow

End-to-end guide for running a draft analysis under the v10 pipeline,
starting from raw `PlayerData-{league}.csv` and ending with post-draft
grading. Updated 2026-05-27 for the v10 refactor.

> Draft tooling isn't in the app yet (Draft Room is a planned page); run
> it from the command line for now. VOSBall's primary interface is the
> Streamlit web app (`py -m streamlit run webapp/app.py` or `run_ui.bat`),
> but the entire draft pipeline below is CLI-only today. The engine is
> VOS v10 in the `vosball/` package, driven by [run_vos.py](../run_vos.py)
> at the repo root.

## Quick reference

| Stage | Script | Inputs | Outputs |
|---|---|---|---|
| 1 | [run_vos.py](../run_vos.py) `--draft` | `data/PlayerData-{league}.csv` + `config/weights_v10.json` | `{league}/eval/[{org}/]draft_evaluation_{league}_{ts}.csv` |
| 2 | [org_depth_analysis.py](../tools/org_depth_analysis.py) | eval CSV + org name | `{league}/org_depth/{team}_strength_{ts}_positions.csv` |
| 3 | [draft_pool_analysis.py](../tools/draft_pool_analysis.py) | eval CSV + PlayerData + v10 weights | `{league}/drafts/{name}/05_draft_pool.md` + 7 sidecar reports |
| 4 | [draft_board.py](../tools/draft_board.py) | strength CSV + draft pool MD | `{league}/drafts/{name}/draft_board_{team}_{ts}.md` + `.csv` |
| 5 (post-draft) | [draft_grades.py](../tools/draft_grades.py) | draft pool MD + league `/draft` API | `{league}/drafts/{name}/draft_grades_raw.{csv,md}` + summary |

The `{name}` is a folder name like `draft_pool_analysis_2061_test` or
`draft_pool_analysis_{timestamp}` — whichever you choose in stage 3.

---

## Stage 0 (prerequisite) — Export the draft pool IDs from StatsPlus

Before running the analysis, export the league's draft pool to a CSV
that lists every draft-eligible player's `ID`. Save it to:

```
data/draft_pool_{league}.csv
```

This is the standardized filename `draft_pool_analysis.py` auto-detects
when you run with `--league {league}` against a `draft_evaluation_*.csv`
input.

**File format:** either a CSV with an `ID` column (the typical StatsPlus
export format — extra columns are ignored), or a plain text file with one
ID per line.

**Why this matters:** the eval CSV is the *entire league*, not just
amateurs. Outlook (the v10 primary score axis) bakes in `readiness_adj`
and `personality_adj` which give established MLBers a structural boost.
Without the draft-pool filter, sorting by Outlook surfaces peak vets at
the top of the list, not draftable amateurs. The ID filter scopes the
analysis to actually-draft-eligible players so the ranking is meaningful.

**Override flags:**
- `--draft-pool-ids PATH` — explicit path to the ID file (any location)
- `--no-draft-pool-filter` — disable auto-detect; analyze the whole eval

If you skip this stage (or the file is missing), the script warns and
proceeds without a filter — the top of your draft pool MD will be
established players rather than amateurs.

---

## Stage 1 — Generate the eval

The draft pipeline reads `draft_evaluation_{league}_*.csv`, which is what
`run_vos.py --draft` writes. The `--draft` flag enables the draft-mode
adjustment stack (Readiness, Draft_Age, Draft_RP_Penalty) that the
downstream tools surface in their reports.

**Single league:**

```
py run_vos.py --league sahl --draft --contracts --per-org-evals
```

- `--draft` populates `Readiness_Adj`, `Draft_Age_Adj`, `Draft_RP_Penalty`
- `--contracts` pulls /contract data (useful for evaluating signed
  prospects, optional for amateur drafts)
- `--per-org-evals` writes per-team park-adjusted evals to
  `{league}/eval/{team_code}/` so `draft_grades --park-adjusted` works

**All leagues (uses the same flags + auto-resolved league_settings):**

```
py run_vos_all.py
```

This uses the standard `run_vos_all.py` defaults (which include
`--contracts` and `--per-org-evals`). `run_vos_all.py` does NOT currently
pass `--draft` — for draft mode, run `run_vos.py` directly per league
with `--draft`.

**Output:**
- `{league}/eval/draft_evaluation_{league}_{ts}.csv` (or
  `{league}/eval/{org_code}/...` when `--per-org-evals`)

**Verify v10:** the CSV should have these columns:
`VOS_Reach`, `VOS_Career`, `VOS_Blended`, `Ideal_Value`,
`Personality_Adj`, `Prone`, `BABIP`, `PotBABIP`, `Readiness_Adj`,
`Draft_Age_Adj`, `Draft_RP_Penalty`. If any are missing, you're on a
pre-v10 eval — re-run [run_vos.py](../run_vos.py).

---

## Stage 2 — Generate org_depth (required for draft_board need scores)

`draft_board.py` reads a strength CSV to compute per-position need
scores. Generate it for your team before drafting.

```
py tools\org_depth_analysis.py --league sahl --org "Houston Astros" --csv
```

`--csv` writes the `*_positions.csv` file [draft_board.py](../tools/draft_board.py) expects.

**Output:**
- `{league}/org_depth/{team_slug}_strength_{ts}.md`
- `{league}/org_depth/{team_slug}_strength_{ts}_positions.csv`

Without this step, the draft board's "need-adjusted" Board B section
won't have anything to weight against. (Board A — pure BPA — works
regardless, but the whole point of having a board is the need-adjusted
view, so don't skip this.)

---

## Stage 3 — Generate the draft pool analysis

This is the heart of the v10 refactor. `draft_pool_analysis.py` reads
the eval CSV, joins to PlayerData for the raw Pot* ratings, computes
the new **Outlook** column via [`lib/draft_score.py`](../lib/draft_score.py),
and emits 8 reports.

**Auto-resolve (recommended):**

```
py tools\draft_pool_analysis.py --league sahl --name 2061_draft
```

This will:

1. Search `sahl/eval/**` recursively for the newest `draft_evaluation_sahl_*.csv`
   (falling back to `evaluation_summary_*` if no draft-mode eval exists).
2. Load `data/PlayerData-sahl.csv` for Pot* tool ratings.
3. Load `config/weights_v10.json`.
4. Compute Draft_Outlook for every player.
5. Write to `sahl/drafts/draft_pool_analysis_2061_draft/`.

**Override flags:**

- `--org-code wsh` — scope auto-resolve to a specific per-org eval subdir
- `--no-prefer-draft` — skip `draft_evaluation_*`, use `evaluation_summary_*`
- `--skip-outlook` — disable Outlook computation (use when PlayerData
  isn't available)
- Positional path arg — pass an explicit CSV path instead of using
  `--league`

**Generated files (in `{league}/drafts/{name}/`):**

| File | Contents |
|---|---|
| `00_summary.txt` | Pool size, position distribution, tier counts, Outlook + Ideal pool means |
| `01_position_distribution.txt` | Players per projected position |
| `02_position_strength.txt` | Mean/median/percentile Ideal Value per position |
| `03_ideal_value_distribution.txt` | Histogram of Ideal Value |
| `04_prospect_tiers.txt` | Elite (≥62) / Plus (54-61) / Average (48-53) / Org Depth (<48) breakdown |
| `05_draft_pool.md` | **The master draft pool MD** — full table with all v10 columns. Downstream draft_board and draft_grades parse this. |
| `summary_data.csv` | Machine-readable summary stats |
| `summary_data.md` | Obsidian-friendly summary tables |

**The v10 columns in `05_draft_pool.md`:**

```
Rank | ID | Name | Pos | Age | Org | Projected Position |
Projected Margin Tier | Projected Viable Pos List | Viable Pos Potentials |
Ideal Value | Outlook | Outlook Pos | Outlook Reason |
Reach | Career | Blend | Pers | Prone | Ready | Tier
```

The header section of the MD includes a blurb explaining what each new
column means.

---

## Stage 4 — Generate the draft board

`draft_board.py` produces two boards in one MD file plus a combined CSV:

- **Board A** — Best Player Available, sorted by Ideal Value, now with
  Outlook / Reach / Career / Pers / Prone columns surfaced (Phase 3)
- **Board B** — Need-adjusted, scored as `pos_value + α × need_score(pos)`

**Auto-resolve (uses newest draft folder in `{league}/drafts/`):**

```
py tools\draft_board.py --team houston_astros --league sahl
```

**Tuning the need weight:**

```
py tools\draft_board.py --team houston_astros --league sahl --need-alpha 1.5
```

Higher α gives need-fit more sway over raw BPA. Default is 1.0.

**Output (in `{league}/drafts/{name}/`):**

- `draft_board_{team}_{ts}.md` — both boards + position need scores
- `draft_board_{team}_{ts}.csv` — full pool, both rank columns, all v10
  signal columns (outlook, reach, career, blend, pers, prone, ready,
  outlook_pos, outlook_reason)

**Reading Board A under v10:**

A row now looks like:
```
| Rank | ID | Name | Pos | Age | Proj | Outlook | Ideal Value | Reach | Career | Pers | Prone | Tier |
```

Each score column tells you something slightly different:

- **Ideal Value** — primary sort key. Heuristic Reach composite at the
  player's best position. Same metric the v3-v10 pipeline has used.
- **Outlook** — Career-weights applied to Pot* ratings. "If this
  prospect realizes their ceiling, how good as an MLB player?" See
  [`lib/draft_score.py`](../lib/draft_score.py) for the math.
- **Reach** — logistic-model probability of reaching MLB. Independently
  trained; uses BABIP, personality, K%, etc. that the heuristic doesn't
  see.
- **Career** — current-rating MLB projection. For amateurs this is
  usually low (current ratings are far below ceiling), but useful for
  college-aged or older prospects.
- **Pers** — Personality_Adj. Positive values = high Work_Ethic etc.;
  negative = low. From v10's recalibrated personality block.
- **Prone** — Injury proneness categorical (Iron Man / Durable / Normal
  / Fragile / Wrecked).

**Where divergence is informative:**

- High Reach + low Outlook → "model loves them, tools suggest moderate
  ceiling." Often a high-floor / low-ceiling profile.
- Low Reach + high Outlook → "model has concerns, tools look fine."
  Often a tool-rich late-round flier; cross-reference Pers / Prone for
  why the model is skeptical.
- High Career + low Reach → close to MLB-ready in tools but model
  doesn't see them sticking. Often a college senior or older
  international signee.

---

## Stage 5 — Run the draft

No script for this — actually drafting happens in OOTP itself or via
the league's draft interface. Your draft board MD is the live reference.

If you want to re-rank after early picks have happened (board changes
when high-ranked players come off the board), just re-run
[draft_board.py](../tools/draft_board.py) — it'll pick up the same draft pool MD
but reflect updated need scores if your org_depth was regenerated.

---

## Stage 6 — Post-draft grading (Phase 4 — not yet refactored)

Currently [draft_grades.py](../tools/draft_grades.py) works correctly under v10
(the grade math is rank-delta-based on Ideal_Value, which v10 preserves
unchanged), but **doesn't yet surface the v10 columns** in its
per-pick output. See [DRAFT_GRADES_PHASE4.md](archive/DRAFT_GRADES_PHASE4.md)
for the planned refactor.

To run it as-is:

```
py tools\draft_grades.py --league sahl --num-teams 30 `
   sahl/drafts/draft_pool_analysis_2061_draft
```

`--num-teams` is the league size (used for managed-risk tiering).

**Optional flags:**

- `--park-adjusted` — also grade against per-team park-adjusted boards
  (requires `--per-org-evals` in Stage 1)
- `--through-pick N` — grade only the first N picks
- `--pdf` — generate a PDF report
- `--slack-headlines` — emit Slack-friendly summaries

**Output:**

- `draft_grades_raw.csv` / `.md` — per-pick grades
- `draft_grades_summary.csv` / `.md` — per-team aggregates
- (optional) `draft_grades_headlines.md` for Slack
- (optional) `draft_grades.pdf`

---

## File layout (after the full pipeline)

```
ratings/
├── data/PlayerData-{league}.csv          ← Stage 1 input
├── config/weights_v10.json               ← Stage 1 + 3 input
└── {league}/
    ├── eval/
    │   ├── draft_evaluation_{league}_{ts}.csv     ← Stage 1 output
    │   └── {org_code}/draft_evaluation_{league}_{ts}.csv   ← --per-org-evals
    ├── org_depth/
    │   ├── {team}_strength_{ts}.md                ← Stage 2 output
    │   └── {team}_strength_{ts}_positions.csv     ← Stage 2 output (CSV)
    └── drafts/{name}/
        ├── 00_summary.txt                         ← Stage 3 outputs
        ├── 01_position_distribution.txt
        ├── 02_position_strength.txt
        ├── 03_ideal_value_distribution.txt
        ├── 04_prospect_tiers.txt
        ├── 05_draft_pool.md                       ← Master MD, downstream contract
        ├── summary_data.csv
        ├── summary_data.md
        ├── draft_board_{team}_{ts}.md             ← Stage 4 outputs
        ├── draft_board_{team}_{ts}.csv
        ├── draft_grades_raw.csv                   ← Stage 6 outputs (post-draft)
        ├── draft_grades_raw.md
        ├── draft_grades_summary.csv
        └── draft_grades_summary.md
```

---

## Common workflows

### Pre-draft analysis (cold start)

```
# 0. (One-time per draft) Export the draft pool IDs from StatsPlus and
#    save to data/draft_pool_sahl.csv (CSV with an `ID` column).

# 1. Get a fresh v10 eval with draft adjustments + per-org variants
py run_vos.py --league sahl --draft --contracts --per-org-evals

# 2. Generate the org_depth strength CSV your team needs
py tools\org_depth_analysis.py --league sahl --org "Houston Astros" --csv

# 3. Build the draft pool analysis (auto-applies the data/draft_pool_sahl.csv
#    ID filter; sorted by Outlook with Ideal Value as cross-reference)
py tools\draft_pool_analysis.py --league sahl --name 2061_draft

# 4. Generate your team's draft board (auto-detects newest draft folder;
#    Board A is BPA sorted by Outlook, Board B is need-adjusted)
py tools\draft_board.py --team houston_astros --league sahl
```

### Re-running the board mid-draft

```
# Just re-run the board after picks have happened (need scores stay
# the same unless you regenerate org_depth)
py tools\draft_board.py --team houston_astros --league sahl
```

### Post-draft grading

```
py tools\draft_grades.py --league sahl --num-teams 30 `
   sahl/drafts/draft_pool_analysis_2061_draft `
   --park-adjusted --pdf
```

### Mid-season prospect ranking (separate from draft)

`prospect_rankings.py` is a separate tool for ranking your org's
existing prospects, not amateurs. v10 refactor for that is Phase 6.

---

## v10 column glossary

| Column (MD label) | CSV column | What it measures | Source |
|---|---|---|---|
| **Outlook** *(primary sort)* | `outlook` | Career-weights × Pot* composite, 20-80. Used as the sort + tier axis in draft_pool_analysis (Phase 2) and draft_board Board A (Phase 3) | `lib/draft_score.py` (Phase 1) |
| Ideal Value *(cross-reference)* | `ideal_value` | Heuristic Reach composite at best position. Legacy ranking key; kept for downstream parsers and as Outlook fallback when PlayerData is unavailable | `run_vos.py` |
| Outlook Pos | `outlook_pos` | Best-position under draft-strict DH rule | `lib/draft_score.py` |
| Outlook Reason | `outlook_reason` | Why Outlook Pos was picked (field_max, dh_*, etc.) | `lib/draft_score.py` |
| Reach | `reach` | VOS_Reach — logistic P(reach MLB) | `run_vos.py` (v10 logistic model) |
| Career | `career` | VOS_Career — current ratings + age decay | `run_vos.py` (heuristic) |
| Blend | `blend` | 0.4 × Reach + 0.6 × Career | `run_vos.py` |
| Pers | `pers` | Personality_Adj from v10 recalibrated block | `run_vos.py` |
| Prone | `prone` | Categorical injury proneness | PlayerData passthrough |
| Ready | `ready` | Readiness_Adj — populated only in `--draft` mode | `run_vos.py` |

---

## See also

- [DRAFT_GRADES_PHASE4.md](archive/DRAFT_GRADES_PHASE4.md) — what's left to do
  on `draft_grades.py`
- [lib/draft_score.py](../lib/draft_score.py) — Outlook computation
- `OOTP Study 27/v5_design.md` *(external research notes, not in this repo)* — the
  two-track scoring architecture v10 inherits
- `OOTP Study 27/career_logistic_followup.md` *(external research notes, not in this
  repo)* — empirical validation that the heuristic Career composite that
  Draft_Outlook reuses is the right choice
