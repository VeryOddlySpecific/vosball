# Phase 4 — draft_grades.py v10 Refactor (Pending)

**Status:** Not started. Phases 1-3 of the v10 draft refactor are complete
(`lib/draft_score.py`, `draft_pool_analysis.py`, `draft_board.py`). This
document captures the remaining work for `draft_grades.py` so it can be
picked up later.

## Current state

[`draft_grades.py`](../../tools/draft_grades.py) is **already functionally v10-compatible** in
the sense that the grade math is purely rank-delta-based:

1. Loads the master draft pool from `05_draft_pool.md` (now v10-enriched
   thanks to Phase 2).
2. Optionally loads per-team park-adjusted boards from
   `{league}/eval/{team_code}/draft_evaluation_*.csv`.
3. Hits the league's `/draft` API to get actual draft order.
4. Compares each pick to its `Projection Rank` (= rank by `Ideal_Value`
   in the master MD) → assigns a Pick Grade + Stamp Type + Points via
   `_grade_pick`.
5. Aggregates per team into Reach / Steal / Best-Available counts.
6. Writes raw + summary CSV/MD; optionally a PDF.

Because the ranking key is still `Ideal_Value` and v10 preserves that
column unchanged, **the grades are already correct under v10**. What's
missing is **surfacing the new v10 columns** (Outlook, Reach, Career,
Pers, Prone) in the per-pick output so the user can see context for each
pick beyond just the rank delta.

## What needs to change

### 1. Extend `load_projections_from_md` to capture v10 columns

**File:** [`draft_grades.py:118`](../../tools/draft_grades.py)

The function currently returns three dicts: `{name → projection_rank}`,
`{name → projected_position}`, `{name → projected_margin_tier_id}`. Extend
it to also return a `{name → {Outlook, Reach, Career, Pers, Prone}}` dict
populated from the per-row v10 cells of `05_draft_pool.md` (which the
Phase 2 refactor already writes).

The parser is permissive (header-keyed zip), so this is purely an
extraction change — add column lookups by header name with empty-string
fallback for pre-v10 MDs.

### 2. Annotate pick rows with v10 columns

**File:** [`draft_grades.py:352`](../../tools/draft_grades.py) (`compare_draft_to_projections`)

Each row this function emits gets keys `Player Name`, `Team`, `Overall
Pick`, `Projection Rank`, `Delta`, `Pick Grade`, `Stamp Type`, `Points`,
`VOS Stamp`. Add: `Outlook`, `Reach`, `Career`, `Pers`, `Prone` — looked
up from the new dict returned in step 1.

The per-team park-adjusted board path (lines ~382-410) also annotates
`Org Projection` / `Org Delta` / etc. Mirror the same v10 columns there
(`Org Outlook` / `Org Reach` / `Org Career` etc.), pulled from the
per-team eval CSV via `load_per_team_board`.

### 3. Update raw CSV / MD writers

**File:** [`draft_grades.py:511`](../../tools/draft_grades.py) (`write_raw_csv`) and
[`draft_grades.py:520`](../../tools/draft_grades.py) (`write_raw_md`).

CSV fieldnames currently:
```
Player Name, Team, Overall Pick, Projection Rank, Delta, Pick Grade,
Stamp Type, Points, VOS Stamp
[+ Org variants when --park-adjusted]
```

Add per the project convention (lowercase CSV column names, capitalized
MD column labels):

- CSV: `Outlook, Reach, Career, Pers, Prone` (and `Org Outlook` etc.
  when park-adjusted)
- MD: `Outlook`, `Reach`, `Career`, `Pers`, `Prone`

Recommended position: after `Projection Rank` and before `Delta` —
keeps the score context grouped with the projection signal, with the
grade math (Delta / Pick Grade / Stamp Type / Points) trailing.

### 4. `_grade_pick` — no change

The pick-grading function ([`draft_grades.py:297`](../../tools/draft_grades.py)) is pure
rank-delta logic. v10 doesn't change ranks (Ideal_Value stays as the
sort key), so this function is correct as-is.

### 5. `load_per_team_board` — no change

The per-team board ranking function ([`draft_grades.py:216`](../../tools/draft_grades.py))
sorts each team's eval by `Ideal_Value`. Same v10 column, same behavior.

### 6. Summary tables — no change

`aggregate_by_team` and `compute_grades_by_range` count Reach/Steal/etc.
purely from grade points. v10 columns aren't tallied — they only
surface in the per-pick raw output. Summary stays untouched.

### 7. PDF writer (optional)

[`draft_grades_pdf.py`](../../tools/draft_grades_pdf.py) defines `DEFAULT_COLUMNS` at line
19. To surface v10 columns in the PDF, add column specs (`('Outlook',
'Outlook', 0.45, 'CENTER', None)` etc.) for the per-pick layout. Pure
cosmetic — no math change. Optional for phase 4; can be deferred.

## What `draft_grades.py` does NOT need

- ❌ Loading PlayerData or computing Outlook itself. The MD it consumes
  already carries Outlook (Phase 2 handled this). Just read it through.
- ❌ Changes to the API fetch (`fetch_draft_csv`) — that consumes the
  league's draft endpoint, unrelated to v10.
- ❌ New ranking key. `Ideal_Value` remains the projection rank.
- ❌ Validation that the input MD is v10. Pre-v10 MDs simply produce
  empty values in the new columns; that's a graceful degradation, not
  a failure mode.

## Estimated scope

| Change | Lines |
|---|---|
| Extend `load_projections_from_md` return signature + extraction | ~30 |
| Annotate rows in `compare_draft_to_projections` (master + per-team) | ~40 |
| Update `write_raw_csv` fieldnames + writerow dict | ~20 |
| Update `write_raw_md` header + row format | ~20 |
| Tests / smoke-run against a real draft | — |
| (Optional) PDF column specs | ~10 |

Total: ~100-110 LOC of edits, no new files. No behavior change for
grades themselves — just enriches per-pick output with v10 context.

## When to do this

After your next draft is done. The grades aren't broken right now —
they just don't carry the v10 context columns. If you want the
park-adjusted boards to also show Outlook/Reach/Career/etc, that
extension is part of step 2-3 above. The non-park-adjusted path is the
priority.

## Open question for the user

Should Reach / Outlook be candidate columns for a NEW grade variant
("Outlook-grade" or "Reach-grade") that ranks picks by something other
than Ideal_Value? This would be **Phase 4.5** or a separate v11 study,
not Phase 4. Worth flagging because the same Reach-vs-Ideal divergence
that surfaces in `draft_pool_analysis.py` and `draft_board.py` would be
even more informative graded as picks. Decision deferred.

---

**Phase ordering reminder:**
- ✅ Phase 1: `lib/draft_score.py`
- ✅ Phase 2: `draft_pool_analysis.py`
- ✅ Phase 3: `draft_board.py`
- ⏳ Phase 4: `draft_grades.py` (this doc)
- ⏳ Phase 5: `draft_grades_pdf.py` (column specs only, may roll into Phase 4)
- ⏳ Phase 6: `prospect_rankings.py`
- ⏳ Phase 7: Bulk runners (`run_draft_pool_analysis_all.py`, etc.)
