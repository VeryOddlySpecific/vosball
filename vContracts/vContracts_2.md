# vContracts — Notes on Unattended Long-Run Sim Viability

Working notes captured while evaluating whether the study can run 30 sim-years unattended and have retroactive VOS analysis still produce equivalent results to the previous pause-per-season workflow.

## Snapshot Coverage Assessment

Monthly CSV dumps capture ~70 tables, including (relevant to VOS analysis):

- Player state: `players`, `players_batting`, `players_pitching`, `players_fielding`, `players_career_*`, `players_value`, `players_scouted_ratings`, `players_salary_history`, `players_awards`, `players_injury_history`, `players_streak`, `players_league_leader`, `players_roster_status`
- Game-level granularity: `players_game_batting`, `players_game_pitching`, `players_individual_batting_stats`, `players_at_bat_batting_stats`, `games`, `games_score`
- Contracts: `players_contract`, `players_contract_extension`
- Transactions / league state: `trade_history`, `team_relations`, `team_financials`, `team_history_*`, `league_history_*`, `league_events`, `league_playoffs`, `league_playoff_fixtures`
- Org / staff: `coaches`, `team_roster`, `team_roster_staff`, `team_affiliations`
- Messaging: `messages`

Conclusion: monthly snapshots are dense enough that game-level resolution survives inside each snapshot, and monthly cadence is fine for trajectory-level VOS work.

## Known / Suspected Gaps

### Draft pool (amateur prospects pre-draft)

No obvious dedicated table for the amateur draft pool exists in the dump set. `players.csv` may carry pre-draft prospects with a status flag (e.g. `LgLvl = 10` COL / `LgLvl = 11` HS), but this needs to be verified:

- Are all draft-eligible amateurs guaranteed to appear in `players.csv` of the dump immediately before the draft month?
- Does OOTP retain non-drafted amateurs in subsequent dumps, or are they purged?
- Does `players_scouted_ratings` carry rows for amateurs, or only signed pros?

**Action:** during the next draft-month dump (June/July sim time), explicitly verify cohort members are present and that scouting ratings exist for them. If gaps exist, an additional pre-draft export step would be required.

### `messages.csv` retention

OOTP prunes old messages on a rolling window. Monthly snapshots should capture every message if the in-game retention window is ≥30 days, but the retention setting needs to be confirmed and ideally pushed higher (90+ days) as a safety margin. If VOS analysis ever wants to mine message history for events (signings, demotions, injuries), shorter retention silently truncates the record.

### `league_events.csv` retention

Same family of concern as `messages.csv`. If league events age out, awards / milestone events near the front of a sim-month might be lost by the time the snapshot is written. Verify retention.

### `players_scouted_ratings` is point-in-time only

Each snapshot captures the scouting view at that moment. The longitudinal record is built by stacking snapshots — there's no in-OOTP history table for scouting drift. That's actually fine for the study (one row per snapshot per player = exactly what we need), but worth knowing: if a snapshot is missed, that month of scouting opinion is gone for good. Under perfect-scouting conditions this is largely moot, but if scouting accuracy is ever varied in a follow-up study, snapshot continuity becomes load-bearing.

### Ephemeral pre-decision state

Trade rumors, FA negotiation drafts, GM job-offer queues, and the live storyline pipeline are not in any dumped table — they exist only in-memory during the sim and resolve into outcomes in `trade_history`, contracts, and message logs. VOS analysis doesn't need these, but flagging for completeness.

## Operational Risks for Unattended 30-Year Runs

Data fidelity is not the binding constraint — sim stability is.

- **Popup stalls.** Manager job offers, contract extension prompts, league events, retirement decisions, and Hall of Fame ceremonies can block the sim. Confirm every AI-side auto-resolve setting is enabled before walking away.
- **Crash recovery.** A single crash at year 19 with no human present kills the run. Mitigations: enable OOTP's in-game auto-save on the most aggressive cadence available; keep the snapshot export running on its own scheduler so it doesn't depend on the sim being in a stable state.
- **Disk drift.** 360 monthly snapshots × ~70 tables is many tens of thousands of CSVs. Confirm the snapshot path has headroom and isn't subject to any rotation policy that would prune older months.
- **Backup window.** Don't overwrite earlier snapshots. Disk cost is negligible relative to the value of a 30-year unbroken record.

## Recommended Validation Before Committing

1. **Verify draft-month coverage.** Run through the next sim draft (June/July Year_1) and confirm: (a) all 990 cohort prospects appear in the pre-draft dump with full ratings, (b) `cohort_draft` populates correctly post-draft, (c) `players_scouted_ratings` is populated for amateurs.
2. **Audit message and event retention.** Check OOTP's retention settings for `messages` and `league_events`. Push to maximum.
3. **Dry-run a 3–5 year auto-sim.** With all human-side prompts auto-resolved and snapshot export running. End conditions to check: no popup stalls, no crashes, all monthly dumps present and well-formed, retroactive VOS pass against any snapshot produces the same correlation numbers as a fresh-export pass at that sim-month.
4. **Only then commit to the full 30-year unattended run.**

## Bottom Line

Monthly snapshots at current table coverage are almost certainly sufficient to replace the manual mid-season export workflow. The remaining open question is the draft pool, which should be verifiable on the next sim draft. The actual risk to the 30-year unattended plan is OOTP stability, not data capture.
