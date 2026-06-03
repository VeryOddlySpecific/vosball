# Session Notes — 2026-05-27

Working session that took the ratings pipeline from "v10 weights shipped, draft tooling still on v6/v3 conventions" to a fully v10-aware draft chain with empirical calibration, tunable config, and StatsPlus-ready output formats. Also added two new bulk tools (waivers + bulk trade targets) and cleaned up stale column labeling across the trade/waiver chain.

Conversation spanned ~25 tracked tasks. This doc groups them into work streams in roughly the order they happened.

---

## TL;DR

| Work stream | Outcome |
|---|---|
| **v10 compliance audit** | Validated run_vos_all → run_vos → weights_v10 chain. Updated 5 stale "v6" labels. |
| **Trade/waiver tooling** | Added `--league` auto-resolve to `trade_targets.py`. Built `waiver_wire.py` + `run_waiver_wire_all.py` + `run_trade_targets_all.py`. |
| **Column rename** | MD `VOS`/`VOS Pot` → `Career`/`Reach`; CSV `vos`/`vos_potential` → `career`/`reach` across `trade_block`, `trade_targets`, `waiver_wire`. |
| **Draft tooling refactor** | Phases 1-4 of the 7-phase plan complete. New `lib/draft_score.py` with `Draft_Outlook` (Career weights × Pot* + draft adjustments). |
| **Outlook as primary sort axis** | `draft_pool_analysis` and `draft_board` BPA both sort by Outlook now; Ideal Value preserved as cross-reference. |
| **Draft pool ID filter** | `data/draft_pool_{league}.csv` auto-detected when running against `draft_evaluation_*.csv` — fixes the "vets-on-top" issue when sorting full eval by Outlook. |
| **DH-routing policy** | New `draft_strict` rule in `lib/draft_score.py`; ~21% of young hitters re-routed from DH to viable field positions. |
| **Outputs for StatsPlus** | `board_a.txt` / `board_b.txt` — plain ID-per-line files, top-200, for direct import. |
| **Empirical calibration** | Built `OOTP Study 27/analysis/calibrate_draft_grades_v10.py`. Found 4.73× top-100/later expected-value premium. |
| **Tiered grading + config** | New `config/draft_grades.json` drives projection tiers. Added "Top 25" tier (7.0 pts) per Option C calibration result. |
| **Side-test: Career-as-logistic** | Built `OOTP Study 27/analysis/fit_career_v10.py`. Confirmed heuristic Career composite beats individual-tool ridge regression at this sample size. |
| **HoF grading tool** | New `hof_grade.py` — career WAR + JAWS + Bill James Monitor/Standards + postseason boost. Single-player scorecard + cached MD reports + batch mode with sorted resume-strength table. |
| **Awards rankings tool** | New `awards_rank.py` — MVP / Cy / RotY / Gold Glove / Silver Slugger for a single season. Defaults to current sim year via `/date`. Auto-splits AL/NL via existing `divisions-{league}.json` configs. |

---

## Work stream 1: v10 compliance audit

Audited whether `run_vos_all` and `run_depth_chart_all` (plus their component modules) follow v10 standards.

### Findings

- `run_vos.py` line 59: `WEIGHTS_FILENAME = "weights_v10.json"` — defaults to v10. ✓
- `run_vos_all.py`, `run_depth_chart_all.py`, `depth_chart.py`: all functionally compliant via downstream — they read the eval CSV produced by run_vos.py rather than loading weights directly.
- Stale labels: 4 in `run_vos_all.py`, 1 in `depth_chart.py:1894` ("v6 model" comment). All updated to "v10".
- Spawned task: fix `SyntaxWarning` from `\*` escape sequence at [trade_block.py:1111](trade_block.py:1111) (use raw string).

### Files touched
- [run_vos_all.py](../../tools/run_vos_all.py) — labels
- [depth_chart.py](../../core/depth_chart.py) — one comment line

---

## Work stream 2: New tools

### `waiver_wire.py` (new)

Grades the current /players waiver wire against your org's needs. Pipeline:
1. Auto-resolve org/year from `league_settings.json` (via `tt.resolve_org_year`)
2. Load `evaluation_summary_{league}_*.csv` (newest, preferring per-org subdir if `--org-code`)
3. Hit `/players` API; filter `is_on_waivers` truthy; drop retired by default
4. Drop own-org players (can't claim yourself)
5. Score through `trade_targets` machinery (build candidate records + match to need-tier)
6. Categorize: Priority Claim / Need Claim / Depth Claim / Premium Stash / Stash / Pass
7. Write `{league}/waiver_wire/{org}_waiver_wire_{ts}.md` + `.csv`

Key design choice: lower composite floor than `trade_targets` (38 vs 42) because claiming is free.

### `run_waiver_wire_all.py` (new)

Bulk orchestrator matching the `run_vos_all` / `run_depth_chart_all` convention. Auto-iterates `league_url.json`, skips leagues missing `org` in `league_settings.json`, supports `--leagues`/`--skip`/`--dry-run`/`--no-preflight`.

### `run_trade_targets_all.py` (new)

Bulk orchestrator for `trade_targets.py`. Auto-resolves the statsplus session cookie per league via `fetch_player_data.load_cookie_for(base_url)` (same file `statsplus_session.json` other tools already use). Masks cookies in dry-run output for terminal-scrollback safety.

### `trade_targets.py` enhancement

Added `resolve_org_year(league, cli_org, cli_year, settings_path)` helper. Now `--org` and `--year` are optional and auto-resolved from `league_settings.json` when omitted. Matches the bulk-runner ergonomics already in place for other tools.

### Files created / touched
- [waiver_wire.py](../../tools/waiver_wire.py) (new)
- [run_waiver_wire_all.py](../../tools/run_waiver_wire_all.py) (new)
- [run_trade_targets_all.py](../../tools/run_trade_targets_all.py) (new)
- [trade_targets.py](../../core/trade_targets.py) (auto-resolve helper)

---

## Work stream 3: Column rename cleanup

The MD column headers `VOS` and `VOS Pot` were misleading (the underlying values are Career and Reach respectively under v10). Renamed across the three downstream tools to use the v10-native labels.

**MD column labels** (capitalized, human-facing):
- `VOS` → `Career`
- `VOS Pot` → `Reach`

**CSV column names** (lowercase, machine-facing):
- `vos` → `career`
- `vos_potential` → `reach`

**Internal in-memory dict keys** (`player.get("vos")` etc.) — intentionally **not** renamed. Those come from `dc.build_player_record` in `depth_chart.py` and are consumed by many other scripts. The rename happens inline at write time via key-mapping in the CSV/MD writers.

### Files touched
- [trade_block.py](../../core/trade_block.py) — 2 MD headers, CSV fieldnames + writerow keys
- [trade_targets.py](../../core/trade_targets.py) — 1 MD header, CSV fieldnames + writerow keys
- [waiver_wire.py](../../tools/waiver_wire.py) — 1 MD header, CSV fieldnames + writerow keys

---

## Work stream 4: Draft tooling refactor (Phases 1-4)

Started with a Plan-agent audit of all 7 draft scripts under v10. Found that `Ideal_Value` (the script's primary sort key) is **structurally fine** under v10 because run_vos.py still computes it — but the underlying semantics needed clarification.

### Side decision: `Ideal_Value` audit

Traced `Ideal_Value` from `run_vos.py` and clarified what it actually measures:

- **For hitters**: best per-position composite in Reach mode (Pot*-weighted heuristic) at the player's best position — pre-adjustment, raw scale.
- **For pitchers**: `combined_r` = ability + arsenal at the player's role, pre-adjustment, raw scale.

Critically: under v10, `VOS_Reach` is the logistic-model output (sigmoid → probability). `Ideal_Value` is the **heuristic** per-position composite — they answer different questions. Both useful; both ride in the eval CSV.

### Phase 1: `lib/draft_score.py` (new)

The "Draft_Outlook" composite — answers the actual draft question: **"If this amateur realizes their ceiling, how good will they be as an MLB player?"**

Math:
- Hitters: batting composite uses Career batting weights (`Gap=.4, Pow=.1, Eye=.1, Ks=.4`) applied to `PotGap/PotPow/PotEye/PotKs`. Defense uses Career defense weights × current defensive ratings (no Pot* equivalents). Baserunning similar. Combine via Career `position_category_weights` at best position.
- Pitchers: ability uses Career ability weights × Pot* ability ratings. Arsenal uses Pot* arsenal (mode-independent in run_vos). Combine via Career `role_balance`.
- Adjustments: `personality_adj + draft_age_modifier + readiness_adj`. Stamina penalty **off by default** (calibrated for current-MLB readiness, unfair to amateurs).
- Normalize to 20-80 via the same sigmoid as other VOS scores.

Reuses `run_vos.py`'s existing scoring helpers via "translated weight dicts" (swap Career keys for their Pot* equivalents). Single source of truth — if v10 weights change, Outlook tracks automatically.

### DH-routing policy

Added compound rule `draft_strict` to `compute_hitter_outlook`. Routes to DH only when:
- No field position is viable (no positional standards met anywhere), OR
- Bat dominates field by ≥8 points, OR
- Best field score < 45 AND bat ≥ 55 (field unrescuable + bat elite)

Otherwise routes to best field position regardless of how high DH score is.

**Impact:** across 3,935 young hitters in sahl, 21.2% (836 players) re-route from DH to viable field positions. Eric Counts (CF, 20, PotGap=80) was the prototype case — default rule put him at DH; strict catches that LF is a viable field option.

`ideal_reason` annotation flows through to draft_pool MD ("field_max", "field_routed", "dh_bat_dominates", "dh_no_viable_field", "dh_unrescuable_with_elite_bat") so the routing decision is visible.

### Phase 2: `draft_pool_analysis.py` refactor

Major refactor. Key changes:
- New `--league` flag with auto-resolve to newest `draft_evaluation_*.csv` (falls back to `evaluation_summary_*.csv`)
- Cross-prefix timestamp comparison so newer per-org evals beat older top-level files
- `--org-code` and `--no-prefer-draft` flags for explicit control
- Loads `data/PlayerData-{league}.csv` for Pot* inputs to Outlook
- Loads `config/weights_v10.json`
- Calls `lib/draft_score.compute_draft_outlook` per player
- **New MD columns**: Outlook, Outlook Pos, Outlook Reason, Reach, Career, Blend, Pers, Prone, Ready
- Renamed `03_vos_potential_distribution.txt` → `03_ideal_value_distribution.txt`
- All "VOS Potential" labels → "Ideal Value"

### Phase 3: `draft_board.py` refactor

- Extended Board A MD + CSV to surface v10 columns (Outlook, Reach, Career, Pers, Prone)
- Parser already permissive (zip header_cells with cells) — flows new columns through automatically
- Board B unchanged (need-adjusted scoring is independent)
- Fixed `find_latest_strength` to accept BOTH `org_depth_analysis_{team}_positions.csv` (current) AND `{team}_strength_*_positions.csv` (legacy). Picks newest by mtime.

### Outlook as primary sort axis

After surfacing Outlook as a column, user requested it become the primary sort key. Made the change:
- `draft_pool_analysis.py`: `_primary_value()` helper returns Outlook with Ideal Value fallback. Sort key + tier categorization both use this.
- `draft_board.py`: Board A BPA sorts by `_primary_value` parsed from MD's Outlook column (Ideal Value fallback).
- MD blurb + labels updated to call out Outlook as the new primary axis.
- Reports labeled dynamically ("Outlook" when available, "Ideal Value" otherwise).

### Draft pool ID filter

Sorting by Outlook on the full league eval surfaced a structural issue: **established MLBers floated to the top** because Outlook bakes in `readiness_adj` (+3 for MLB-ready) and `personality_adj` (+3 to +5 for WrkEthic-high vets). Out of 18,860 sahl players, 8,024 are 17-22 (true amateurs) but they were buried under 1,068 age-31+ MLB veterans.

Resolution: added a draft-pool-IDs filter.
- Standard path: `data/draft_pool_{league}.csv` (one ID per line, or CSV with `ID` column — matches StatsPlus export format)
- Auto-detected when input is `draft_evaluation_*.csv` AND `--league` is set
- `--draft-pool-ids PATH` explicit override
- `--no-draft-pool-filter` opt-out

Verified end-to-end on a synthetic test pool. With the filter, top of the MD is now ages 20-21 amateurs scoring 63-67 Outlook — exactly the draft cohort.

### Phase 4: `draft_grades.py` refactor

The grade math (`_grade_pick`) is purely rank-delta-based, so already v10-compatible. What was missing: surfacing v10 columns in per-pick output.

- `load_projections_from_md` now returns a 4-tuple: added `name_to_v10` carrying `{Outlook, Reach, Career, Pers, Prone}` cells per player
- `load_per_team_board` returns `(name_to_rank, v10_by_name)` — per-team eval CSV mapped from `VOS_Reach`/`VOS_Career`/`Personality_Adj`/`Prone` to short labels
- `compare_draft_to_projections` annotates each pick row with v10 cells (plus Org variants for park-adjusted)
- `write_raw_csv` + `write_raw_md` extended with Outlook/Reach/Career/Pers/Prone columns

Outlook has no Org variant because `run_vos.py` doesn't park-adjust Pot* ratings (only the heuristic batting/defense composites).

### `board_a.txt` + `board_b.txt` outputs

Added two plain ID-per-line files alongside the existing MD + CSV outputs:
- `draft_board_{team}_{ts}_board_a.txt` — BPA (Outlook) order
- `draft_board_{team}_{ts}_board_b.txt` — need-adjusted order

For direct StatsPlus draft-prep import. Limited to `--top` (default 200) so MD/CSV/txt all stay in sync on the same flag.

### Files created / touched
- [lib/draft_score.py](../../lib/draft_score.py) (new)
- [draft_pool_analysis.py](../../tools/draft_pool_analysis.py) (major refactor)
- [draft_board.py](../../tools/draft_board.py) (major refactor + .txt outputs + filename pattern fix)
- [draft_grades.py](../../tools/draft_grades.py) (v10 column surfacing — major changes happen in work stream 6)

---

## Work stream 5: Documentation

Wrote two reference docs alongside the draft refactor:

### [DRAFT_WORKFLOW.md](../DRAFT_WORKFLOW.md)

End-to-end guide for running a draft analysis under v10:
- 6 stages (eval → org_depth → pool analysis → board → draft → grades)
- File layouts at each step
- Common workflows (cold-start recipe, mid-draft re-run, post-draft grading)
- v10 column glossary

### [DRAFT_GRADES_PHASE4.md](DRAFT_GRADES_PHASE4.md)

Initially written as a "queued for later" plan. Phase 4 was then implemented later in the session.

---

## Work stream 6: Empirical calibration in OOTP Study 27

User asked: "Looking at the concept of Reach, is it fair to say X player has a 77/80 chance of making the majors?"

Walked through the actual math: `predicted_probability = (VOS_Reach - 20) / 60`. So VOS_Reach=77 → 95% probability, not 77%. Provided three communication framings (tier language safest, probability with hedge for sabermetric audiences, just-the-score for non-numerate readers).

### vDraft+ audit

User asked whether the vDraft+ metric (used in `draft_grades` summary tables) still fits under v10.

Conclusion: structurally fine. The metric is rank-delta-based and axis-agnostic. Semantically narrowed under v10 (now measuring Outlook-axis efficiency, not Ideal-Value-axis), but the math is correct.

Flagged three caveats:
1. The 3.5/1.5 ratio was calibrated under Ideal Value distribution — might need retuning for Outlook
2. vDraft+ only sees one axis (Outlook) — doesn't reward Reach-axis or Blended-axis decisions
3. `TOP_PROJECTION_CAP = 100` is league-size-blind

### Career-as-logistic side-test

User asked: "If an individual-tool model is a better outlook for Reach, why wouldn't it be for Career?"

Built [OOTP Study 27/analysis/fit_career_v10.py](../OOTP%20Study%2027/analysis/fit_career_v10.py): mirror of `fit_reach_v9_personality.py` but with ridge regression on continuous `career_war_ml` instead of logistic on binary reach.

**Result: heuristic wins decisively.**

| Model | CV Spearman ρ | vs heuristic |
|---|---|---|
| Heuristic v3 Career | **+0.213** | baseline |
| Engine pot_rating | +0.129 | −0.084 |
| Logistic hitter (ridge) | +0.116 | −0.097 |
| Logistic SP (ridge) | +0.188 | −0.025 |
| Logistic RP (ridge) | +0.009 | −0.204 |

Three structural reasons:
1. Career's MLB-only restriction crushes the sample (403/362/158 vs Reach's 2,839/2,012/663)
2. Career's signal is position-specific in ways Reach's isn't
3. Hitter coefficients showed defense ratings ALL negative — restriction-of-range confound that the heuristic's per-position weighting handles correctly

Wrote [OOTP Study 27/career_logistic_followup.md](../OOTP%20Study%2027/career_logistic_followup.md) documenting the result. Kept the draft config at `OOTP Study 27/config/weights_v10_career_logistic_draft.json` for future inspection.

**Implication for draft work:** the heuristic Career composite is empirically validated, so reusing its weight structure in `lib/draft_score.compute_draft_outlook` (Career weights × Pot*) is on solid ground.

### Draft grades calibration

User asked whether the `POINTS_TOP_100 = 3.5` / `POINTS_LATER = 1.5` ratio (2.33×) holds up under v10's Outlook ranking.

Built [OOTP Study 27/analysis/calibrate_draft_grades_v10.py](../OOTP%20Study%2027/analysis/calibrate_draft_grades_v10.py): compute Outlook for all 22,994 cohort players, rank within-cohort, bucket by rank, measure career WAR + reach rate.

**Result: empirical premium is 4.73× expected value (vs current 2.33×).**

| Bucket | n | reach_rate | mean_war_mlb | expected_value |
|---|---|---|---|---|
| **1-25** | 97 | **57.7%** | **14.99** | **8.67** |
| 26-50 | 85 | 40.0% | 8.66 | 3.45 |
| 51-100 | 187 | 35.8% | 7.55 | 2.69 |
| 101-200 | 368 | 26.4% | 7.15 | 1.88 |
| 201-500 | 975 | 18.7% | 7.04 | 1.31 |
| 501+ | 3,802 | 12.8% | 5.97 | 0.75 |

Top-100 / Later EV ratio = 4.44 / 0.94 = **4.73×**

Two structural findings beyond the headline:
1. The premium is almost entirely a **reach-rate story**. Conditional WAR (mean_war_mlb) barely moves between rank 26 and rank 500 (range 7.0-8.7). What changes is hit rate.
2. The top-100 bucket is **not uniform**. Rank 1-25 produces 8.67 EV; rank 51-100 produces 2.69 EV. The "top 100" label averages a sharp gradient into one number.

Three recommendations (Option A: keep current; B: modest bump to 5.0; C: add Top 25 tier).

Wrote [OOTP Study 27/draft_grades_calibration.md](../OOTP%20Study%2027/draft_grades_calibration.md) documenting design + results + interpretation.

### Option C implementation (tiered grades + config file)

User chose Option C: add a Top 25 tier with `7.0` base points. Also requested a config file to hold the gradient values for on-the-fly tuning.

### New file: [config/draft_grades.json](config/draft_grades.json)

```json
{
  "projection_tiers": [
    {"name": "Top 25",  "max_rank": 25,   "base_points": 7.0},
    {"name": "Top 100", "max_rank": 100,  "base_points": 3.5},
    {"name": "Later",   "max_rank": null, "base_points": 1.5}
  ],
  "managed_risk": {"base_points": 0.75, "log_scale": 0.25},
  "delta_bonus":  {"log_scale": 0.5},
  "grade_bands":  [{"position_max": 0.2, "grade": "F"}, ..., {"position_max": 1.0, "grade": "A"}]
}
```

Empirical 4.73× ratio is reflected in Top-25 vs Later (7.0/1.5 = 4.67×). The Top-100 tier sits in the middle at 3.5/1.5 = 2.33× (matches empirical mid-tier 3.07/0.94 = 3.27×).

### `draft_grades.py` refactor for config-driven tiers

Major changes:
- `load_grades_config()` reads JSON with normalization (sorts tiers ascending, ensures last is open-ended)
- `_tier_for_rank()` lookup helper
- `_grade_pick()` now tier-driven
- `_base_for_projection()` now tier-driven
- `aggregate_by_team()` produces `counts` and `org_counts` sub-dicts keyed by stamp name (adapts to any tier schema)
- `compute_grades_by_range()` reads grade_bands from config
- `write_summary_md()` + `build_summary_rows()` + CSV writer generate stamp-count columns from `_all_stamp_names()`
- New `--grades-config` CLI flag
- Startup banner prints active tiers
- Built-in defaults match v10 calibration so the script works even without the config file

Verified on tlg/2053 draft pool with synthesized picks:
- Pick #1 at projection #1 → `Top 25`, 7.00 pts
- Pick #40 at projection #15 → `Top 25`, 8.63 pts (7.0 + log_bonus 1.63)
- Pick #160 at projection #150 → `Later`, 2.70 pts (1.5 + log_bonus 1.20)
- `aggregate_by_team` counts: `{'Top 25': 9, 'Top 100': 0, 'Later': 1, 'Managed Risk': 0}` ✓

---

## Work stream 7: Hall of Fame + Awards rankings

Late-session addition. Two new player-evaluation tools that hit the season-level stat endpoints (`/playerbatstatsv2`, `/playerpitchstatsv2`, `/playerfieldstatsv2`) directly — synchronous, no polling like `/ratings`.

### `hof_grade.py` (new)

Career-long HoF candidacy grader for a single player. Pulls all-years ML stats (level_id=1, split=1), runs a battery of well-known sabermetric HoF heuristics, prints a scorecard, and caches the result as a markdown report.

**Signals computed:**
- Career ML WAR (sum of batting + pitching across all stints)
- 7-year peak WAR + **JAWS** (Jaffe's career-vs-peak average — standard for HoF voting)
- **Bill James HoF Monitor** — milestone-based (~100 = likely HoFer, 130 = lock)
- **Bill James HoF Standards** — career rate + counting stats (~50 = avg HoFer)
- **Postseason boost** (uses `split=21` on the same endpoints) — small nudge for heroic playoff stretches
- **Primary position** derived from fielding innings, with position-specific JAWS target (C/SS bars lower than 1B/LF)
- **Resume Strength** — weighted blend of the four signals, normalized to a 0-100 scale; verdict band: LOCK / LIKELY / BORDERLINE / LONGSHOT / NOT HOF-WORTHY

**Output:**
- Console scorecard
- `reports/hof_review/{league}/{id}-{Last_First}.md` — saved + auto-loaded on re-run (`--refresh` to recompute)
- `--json` for piping

**Naming history:** the composite started as "Composite %" — renamed to **"Resume Strength"** mid-session after user clarified it's a measure of HoF resume quality, NOT a vote-share prediction. The verdict bands are calibrated against that scale; a real vote projection would need actual voter-behavior modeling, which isn't in scope.

**Thresholds:** MLB-historical defaults baked into `DEFAULT_THRESHOLDS`. Drop `config/hof_thresholds-{league}.json` to override per league (deep-merges `jaws_target_by_pos` etc.).

**Batch mode:** added in same session.
- `--ids 67384,15416,32523` — inline comma list
- `--ids-file path.txt` — one ID per line, supports `# Frank Velez` inline comments
- Per-player MD reports still save (and cache) just like single-id mode
- Console output is a sorted table: ID · Name · Pos · WAR · Peak7 · JAWS (vs Tgt) · Mon · Stds · Resume% · Verdict
- `--table-out path.md` also saves the table
- Failures on individual players log to stderr and don't stop the batch

### `awards_rank.py` (new)

Single-season awards rankings for the whole league. One fetch each of the three stat endpoints scoped to `year=YYYY` pulls the entire ML pool, then ranks for all five awards.

**Awards:**
| Award | Filter | Score formula |
|---|---|---|
| **MVP** | ≥300 PA *or* ≥100 IP | bat WAR + pit WAR + small OPS/ERA tie-breaker |
| **Cy Young** | ≥120 IP (SP) / ≥50 IP (RP) | pitching WAR + small ERA + K/9 tie-breakers |
| **Reliever of the Year** | non-starters, ≥40 IP + ≥30 G | WAR + WPA + small SV+HLD/closer bonus + ERA tiebreak − BS penalty |
| **RotY** | `mlb_service_years < 1` + ≥200 PA / ≥40 IP | total WAR |
| **Gold Glove** | per position, ≥600 IP (≥450 for C) | ZR + framing+arm for C + arm·0.5 for OFs |
| **Silver Slugger** | per position, ≥350 PA (≥250 for C), primary pos = slot | wOBA-ish (OBP×1.7 + SLG) + small volume bonus |

**Reliever of the Year design choice:** WPA (Win Probability Added) sits alongside WAR in the score because reliever value is concentrated in high-leverage innings — a 1-out save in a 1-run game is worth orders of magnitude more than a mop-up inning. WAR alone undervalues that. Bonuses for SV+HLD reward closer/setup workload; blown saves get a small penalty. Splits AL/NL like every other award.

**Output:**
- Console: one ranked table per award
- `reports/awards/{league}/{year}.md` — combined, cached, auto-loaded on re-run

**Year resolution:** `--year` optional; defaults to current sim year by hitting `/date` and parsing the YYYY prefix.

**Thresholds:** `DEFAULT_THRESHOLDS` dict; overridable via `config/awards_thresholds-{league}.json`.

### Sub-league split (AL / NL)

**The problem:** every StatsPlus-league API returns a single `league_id` (SAHL is all `153`, even though SAHL has American League + National League sub-leagues). The AL/NL structure lives in `divisions-{league}.json`, not in the API response.

**The reconstruction:**
1. `load_team_subleagues(league)` joins `divisions-{league}.json` (`{sub_league: {division: [team_name, ...]}}`) with `teams-{league}.json` (`{team_id: {Name, Nickname}}`) to produce `team_id → sub_league_name`. Exact-match first, case-insensitive fallback, warning logged on unmatched teams.
2. `final_team_per_player(bat_rows, pitch_rows)` walks stat rows and picks the row with the highest `stint` per player — that's the team they ended the season on. Ties tiebreak on playing time.
3. Each award row gets a `sub_league` tag; renderer groups by sub-league preserving config order (AL before NL).

**Behavior:**
- **Auto-splits:** sahl, ndl, uba, bwb, wwoba (have both configs)
- **Falls back to combined section:** sdmb, woba, tlg (missing one or both configs)
- No flag needed — fallback is automatic and logged

**Traded players:** count to the final team's sub-league (user choice; closest to common MLB voting conventions).

### Key design decisions for hof_grade / awards_rank

**Reused `fetch_player_data._get`, `load_token_for`, `load_cookie_for`** rather than duplicating auth logic. Same token / cookie config files the existing tools use.

**Cached raw CSVs separately from rendered reports.**
- `cache/hof/{league}/{playerid}-{endpoint}-split{N}.csv` — raw API responses, per-player
- `cache/awards/{league}/{year}-{endpoint}-split{N}.csv` — raw API responses, per-season
- `reports/hof_review/{league}/{id}-{name}.md` — rendered HoF scorecard
- `reports/awards/{league}/{year}.md` — rendered awards report

Re-running with `--refresh` busts both caches; default behavior loads the rendered MD if present, else recomputes from cached CSVs.

**`level_id=1` + `split_id ∈ {0, 1}` filter** consistently across both tools — keeps ML-only / overall-split rows. Fielding rows use `split_id=0`, batting/pitching use `split_id=1` for overall.

**Postseason via `split=21`** on the same endpoints. No separate API needed.

**Honest caveats documented in module docstrings:**
- hof_grade: black ink / gray ink (league-leading) would need 30+ per-year calls — deferred. Awards data not exposed by the API — Monitor compensates by weighting milestones more.
- awards_rank: pitcher MVP is included with pitching WAR but no narrative/team-success weighting. Silver Slugger primary-position uses this season's fielding innings; a 50/50 LF/CF player lands wherever they had more innings.

### Files created

- [hof_grade.py](../../core/hof_grade.py) — single-player HoF grader + batch mode
- [awards_rank.py](../../tools/awards_rank.py) — season awards rankings with AL/NL split

### Files NOT changed (deliberate)

- `config/divisions-{league}.json` — reused as-is for the sub-league split. No new config schema introduced.
- `config/teams-{league}.json` — reused as-is for team_id → name lookup.
- `fetch_player_data.py` — imported but not modified.

---

## Files changed (full list)

### `ratings/` — new files

- [lib/draft_score.py](../../lib/draft_score.py) — Draft_Outlook computation
- [waiver_wire.py](../../tools/waiver_wire.py) — single-league waiver evaluator
- [run_waiver_wire_all.py](../../tools/run_waiver_wire_all.py) — bulk runner
- [run_trade_targets_all.py](../../tools/run_trade_targets_all.py) — bulk runner with cookie auto-resolve
- [hof_grade.py](../../core/hof_grade.py) — Hall of Fame candidacy grader (single + batch)
- [awards_rank.py](../../tools/awards_rank.py) — season awards rankings with AL/NL split
- [config/draft_grades.json](config/draft_grades.json) — tunable grading config
- [DRAFT_WORKFLOW.md](../DRAFT_WORKFLOW.md) — end-to-end usage guide
- [DRAFT_GRADES_PHASE4.md](DRAFT_GRADES_PHASE4.md) — phase 4 planning doc (implemented mid-session)
- [SESSION_NOTES_2026-05-27.md](SESSION_NOTES_2026-05-27.md) — this doc

### `ratings/` — major refactors

- [draft_pool_analysis.py](../../tools/draft_pool_analysis.py) — auto-resolve, PlayerData loading, Outlook computation, v10 columns in MD, sort-by-Outlook, draft-pool-IDs filter
- [draft_board.py](../../tools/draft_board.py) — v10 columns in Board A, txt outputs for StatsPlus, filename pattern fix
- [draft_grades.py](../../tools/draft_grades.py) — v10 columns + config-driven tier system + Option C tiered grading
- [trade_targets.py](../../core/trade_targets.py) — auto-resolve org/year, column renames
- [trade_block.py](../../core/trade_block.py) — column renames (Career/Reach)
- [waiver_wire.py](../../tools/waiver_wire.py) — column renames (in same session as build)

### `ratings/` — minor edits

- [run_vos_all.py](../../tools/run_vos_all.py) — stale "v6" labels → "v10"
- [depth_chart.py](../../core/depth_chart.py) — 1 stale comment line

### `OOTP Study 27/` — new files

- [analysis/fit_career_v10.py](../OOTP%20Study%2027/analysis/fit_career_v10.py) — Career-logistic side-test
- [analysis/calibrate_draft_grades_v10.py](../OOTP%20Study%2027/analysis/calibrate_draft_grades_v10.py) — top-100/later premium calibration
- [analysis/output/draft_grades_calibration.csv](../OOTP%20Study%2027/analysis/output/draft_grades_calibration.csv) — calibration result table
- [config/weights_v10_career_logistic_draft.json](../OOTP%20Study%2027/config/weights_v10_career_logistic_draft.json) — Career-logistic fit results (kept for reference, not deployed)
- [career_logistic_followup.md](../OOTP%20Study%2027/career_logistic_followup.md) — Career-logistic findings doc
- [draft_grades_calibration.md](../OOTP%20Study%2027/draft_grades_calibration.md) — calibration design + results + recommendations

---

## Key technical decisions

### 1. Outlook math: Career weights × Pot* ratings
**Why:** the actual draft question is "if amateur realizes ceiling, how good in MLB?" Career weights are tuned for MLB-WAR projection (validated by `career_logistic_followup.md` — heuristic Career beats individual-tool ridge). Pot* are the ceiling inputs. Combining them answers the right question.

### 2. Stamina penalty off by default in Outlook
**Why:** `minimum_stamina=50` is calibrated for current-MLB SP viability. Every amateur has Stm < 50 because stamina develops with workload. Penalizing would tank elite SP prospects unfairly. Flag is exposed (`apply_stamina_penalty=True`) for non-draft use cases.

### 3. `dh_policy="draft_strict"` as default for amateur drafts
**Why:** the cfg's `dh_assignment` block uses a 3-point bat-over-field margin, which mislabels developable defenders as "DH" when their bat is mildly stronger. Strict requires (a) no viable field position, OR (b) ≥8-point bat dominance, OR (c) field unrescuable + elite bat. Caught 836/3,935 (21%) of young sahl hitters that were misrouted to DH.

### 4. Outlook becomes primary sort axis (not Ideal Value)
**Why:** user-driven decision after seeing Outlook in the column lineup. Aligned `draft_pool_analysis` and `draft_board` Board A to use Outlook as the rank source. `Ideal_Value` preserved as a cross-reference column for downstream-tool compatibility.

### 5. Draft pool ID filter required for honest rankings
**Why:** sorting full eval by Outlook puts established MLBers at the top because Outlook's adjustment stack (readiness, personality) rewards MLB-readiness. The eval is the entire league, not just amateurs. The filter scopes to actually-draft-eligible players. Standard path: `data/draft_pool_{league}.csv`.

### 6. Heuristic Career stays (no v11 logistic Career)
**Why:** ridge regression on individual tools (mirroring the v6+ Reach approach) consistently underperformed the heuristic on Stage-2 Spearman across all three positions (hitter/SP/RP). Sample size constraint (MLB-only n=403/362/158) and position-specific signal favor the heuristic's structured priors. Documented in `career_logistic_followup.md`.

### 7. Option C tiered grading (Top 25 / Top 100 / Later)
**Why:** calibration showed the top-100 bucket is non-uniform (rank 1-25 produces 8.67 EV; rank 51-100 produces 2.69 EV). Splitting into 3 tiers surfaces the structural gradient. Empirical premium of 4.73× compressed to a manageable 4.67× (7.0/1.5) for top-25 vs later. Game-feel preserved by not going to direct empirical 7.09/1.5.

### 8. Config-driven grading (vs hardcoded constants)
**Why:** retuning the ratio post-deployment shouldn't require a code change. User can edit `config/draft_grades.json` to add tiers, change point values, or revert to a 2-tier system, all without touching `draft_grades.py`. Defaults preserved so the script works without the config too.

### 9. "Resume Strength" not "Composite %" or "HoF Probability"
**Why:** user clarified the score measures HoF resume quality, not vote-share. The word "probability" would imply we're predicting voter behavior — which we're not (no awards data, no past-ballot data). Resume Strength is a more honest label for what the math actually does.

### 10. Sub-league split via divisions config, not API
**Why:** every StatsPlus-league API returns one `league_id`. The AL/NL structure is a local config artifact. Reusing `divisions-{league}.json` (already maintained by the user for other tools) means no new config schema and the split stays consistent across the whole pipeline. Leagues without that config fall back cleanly to a combined section.

### 11. Traded players count to their final team's sub-league
**Why:** user choice between "final team" / "most games" / "eligible in both". Final team matches real-MLB award voting conventions most closely. Determined via max `stint` in the stat rows, with playing-time tiebreak.

---

## Open items / future work

### Deferred from the draft refactor plan
- **Phase 5**: `draft_grades_pdf.py` — add v10 column specs. Cosmetic only; doesn't affect grades.
- **Phase 6**: `prospect_rankings.py` — already partially v6-aware per the original Plan-agent audit. Add `--draft` flag, surface new v10 columns.
- **Phase 7**: Bulk runners — `run_draft_pool_analysis_all.py` and `run_prospect_rankings_all.py`. Standard pattern, ~100 LOC each.

### Calibration follow-ups
- **Multi-sim validation**: SAHL Studies (4-sim sensitivity test) is in motion per `OOTP Study 27/NEXT_STEPS.md`. Re-run `calibrate_draft_grades_v10.py` once those complete to confirm the 4.73× ratio holds across settings.
- **Per-cohort calibration**: the script aggregates across all 10 cohorts. Adding a `--by-cohort` mode would show inter-cohort variance and validate pool-deflation effects.
- **Holdout validation**: the calibration was run on the same data that trained the v10 weights. A leave-one-cohort-out study would harden the conclusion.

### Possible v11 directions
- **GBM Career model** (NEXT_STEPS.md item #7): if linear models can't beat the heuristic, non-linear interactions might. Worth a one-off diagnostic.
- **Reach-axis vDraft+ companion**: same machinery as current vDraft+ but against Reach-ranked projections. Surfaces ceiling-vs-floor draft strategy as a team-summary signal.
- **Injury history features** (per `v6_followup_notes.md`): the Outcomes DB has injury data; adding it to the v7+ Reach model is probably the highest-payoff free AUC bump available.

### Quality-of-life
- **`prospect_rankings.py` --draft flag** (Phase 6): point at `draft_evaluation_*.csv` automatically when in draft mode.
- **Recalibrate `TOP_PROJECTION_CAP` per league size**: currently hardcoded at 100 in the calibration script. League with 16 teams × 5 rounds = 80 picks; a tier called "Top 100" is meaningless there. Config could store per-league overrides.
- **Auto-create `data/draft_pool_{league}.csv` from an OOTP-export shape**: currently a manual export step. If the user always exports the same StatsPlus CSV format, a small wrapper could standardize it.

### Documentation gaps
- `prospect_rankings.py` doesn't have a Phase 6 spec doc analogous to `DRAFT_GRADES_PHASE4.md`. Write one before tackling.
- `DRAFT_WORKFLOW.md` mentions Phase 4 was deferred — update once it's confirmed to be working end-to-end against a real live draft.

---

## Quick reference: which file does what?

| File | Purpose |
|---|---|
| `run_vos.py` | v10 scoring engine; produces eval CSV |
| `run_vos_all.py` | Bulk run_vos.py across all leagues |
| `depth_chart.py` | Build depth chart from eval CSV + stats |
| `run_depth_chart_all.py` | Bulk depth_chart |
| `org_depth_analysis.py` | Org strength → `org_depth_analysis_{team}_positions.csv` (input to draft_board need scoring) |
| `trade_block.py` | Your org's trade chips |
| `trade_targets.py` | League /tradeblock filtered to your needs |
| `run_trade_targets_all.py` | Bulk trade_targets across all leagues |
| `waiver_wire.py` | /players is_on_waivers → your needs |
| `run_waiver_wire_all.py` | Bulk waiver_wire |
| `draft_pool_analysis.py` | Draft eval → 8 reports + 05_draft_pool.md (master MD) |
| `draft_board.py` | Per-team draft board (BPA Board A + need-adjusted Board B) |
| `draft_grades.py` | Post-draft grading vs MD projections |
| `draft_grades_pdf.py` | PDF renderer for draft_grades |
| `lib/draft_score.py` | `compute_draft_outlook` — used by draft_pool_analysis |
| `hof_grade.py` | HoF candidacy grader: career stats → JAWS / Monitor / Standards → Resume Strength. Single or batch. |
| `awards_rank.py` | Season awards rankings: MVP / Cy / RotY / Gold / Silver. Auto-splits AL/NL when divisions config exists. |
| `config/weights_v10.json` | v10 scoring weights (used by run_vos.py) |
| `config/draft_grades.json` | Tunable grading parameters (used by draft_grades.py) |
| `config/hof_thresholds-{league}.json` | Optional per-league HoF threshold overrides (used by hof_grade.py) |
| `config/awards_thresholds-{league}.json` | Optional per-league awards threshold overrides (used by awards_rank.py) |
| `config/divisions-{league}.json` | Sub-league + division structure; reused by awards_rank for AL/NL split |
| `config/teams-{league}.json` | team_id → "City Nickname"; reused by awards_rank for team→sub-league lookup |
| `config/league_url.json` | Per-league API URLs |
| `config/league_settings.json` | Per-league org / year / rating_scale |
| `config/statsplus_session.json` | Cookie storage for /tradeblock auth |
| `data/PlayerData-{league}.csv` | Raw league export (input to run_vos.py + Outlook) |
| `data/draft_pool_{league}.csv` | StatsPlus draft pool IDs (input filter for draft_pool_analysis) |

---

## End-state summary

The ratings pipeline now has a fully v10-aware draft chain:

```
PlayerData CSV → run_vos.py --draft → draft_evaluation_*.csv
                                       ↓
                              (with data/draft_pool_{league}.csv filter)
                                       ↓
                          draft_pool_analysis.py --league X
                                       ↓
                              05_draft_pool.md (sorted by Outlook)
                                       ↓
                          draft_board.py --team Y
                                       ↓
                  Board A (BPA) + Board B (need) + board_a.txt + board_b.txt
                                       ↓
                            (during live draft, OOTP)
                                       ↓
                          draft_grades.py {draft folder}
                                       ↓
                  Per-pick grades with Top 25/Top 100/Later tier stamps
                  Tunable via config/draft_grades.json (no code edit needed)
```

Two new tools added that operate outside the draft pipeline:
- `waiver_wire.py` for free-agent claim recommendations
- `run_trade_targets_all.py` for bulk trade-target sweeps

And one calibration artifact in OOTP Study 27 that can be re-run any time the v10 weights are tuned to refresh the empirical ratio.

Two new player-evaluation tools added late in the session, operating on the season-stat endpoints:
- `hof_grade.py` for Hall of Fame candidacy grading (single player or batch, with cached MD reports + sorted resume-strength table)
- `awards_rank.py` for season awards rankings (MVP / Cy / RotY / Gold / Silver, AL/NL split via existing divisions config)
