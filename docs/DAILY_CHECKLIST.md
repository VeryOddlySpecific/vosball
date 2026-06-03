# Daily OOTP Checklist

> Two-tier checklist: real-life **daily routine** (do once per calendar day) + **per-sim steps** (do every time a league advances). Designed for managing 8 leagues without losing track. Edit as workflow changes.
>
> **The web app is the primary interface.** Launch it with `py -m streamlit run webapp\app.py` (or `run_ui.bat`), pick the active league in the **League Hub**, and work the pages (Eval Browser, Depth Charts, Prospects, Farm Value, Trade Targets, Free Agents). The League Hub even renders this per-sim checklist interactively, persisted per league. The CLI/batch runners below are the all-leagues-at-once path.
>
> **Config sources of truth:**
> - `config/league_url.json` — list of leagues + API base URLs
> - `config/league_settings.json` — your `org`, `year`, `rating_scale`, optional `min_comp` per league
> - `config/statsplus_tokens.json` — API tokens (preferred auth, 90-day TTL)
> - `config/{league}-park-factors.json` — combined teams[] park factors per league

---

## League roster at a glance

You are a **GM in all leagues** — not commish anywhere right now. Commissioner-specific items are parked at the bottom under "For Later Use (if/when you commish)".

Org + year + rating_scale per league are stored in `config/league_settings.json` and consumed by the bulk scripts — single source of truth.

| League | Your Org | Year | Source | Park Factors | Notes |
|--------|----------|------|--------|--------------|-------|
| **NDL**   | Seattle Whalers | 2055 | StatsPlus | yes | rating scale 1-100 |
| **UBA**   | Atlanta Bandits | 2041 | StatsPlus | yes | — |
| **SDMB**  | Chihuahua Division del Norte | 2049 | StatsPlus | yes (neutral 1.000 league-wide) | — |
| **SAHL**  | Houston Astros | 2061 | StatsPlus | yes | spring training workflow |
| **TLG**   | Washington Nationals | 2053 | StatsPlus | yes | — |
| **WWOBA** | Arizona Diamondbacks | 2039 | StatsPlus | yes | — |
| **WOBA**  | St. Louis Cardinals | 2041 | StatsPlus | yes | — |
| **BWB**   | Chihuahua Guerreros | 2028 | StatsPlus | yes | league in final testing — confirm year is real |

---

## Daily routine (real-life day, all leagues)

Do these once each morning regardless of which leagues sim today.

- [ ] **Check StatsPlus dashboards** for each league — sim status, league news, GM messages
- [ ] **`py tools\fetch_all_player_data.py`** — staged parallel CSV pull across all 8 leagues. Drops files at `data/PlayerData-{league}.csv`. ~3-5 min total.
- [ ] **`py tools\run_vos_all.py`** — refresh eval CSVs for every league (depends on PlayerData step). ~1-3 min.
- [ ] **`py tools\run_depth_chart_all.py`** — all-level depth charts for your org in every league.
- [ ] **Check trade offers / waiver claims** in every league
- [ ] **Glance at injuries** across your orgs — flag anyone needing a re-run with `--min-comp` to see open starter slots
- [ ] **Inbox sweep** — Slack/Discord for each league's channels

> **Bulk script behavior notes:**
> - `fetch_all_player_data.py` runs parallel-staged (kickoff all leagues, then poll all pending in 30s cycles). Use `--sequential` if it misbehaves.
> - `run_vos_all.py` always applies `--contracts`, `--per-org-evals`, `--park-factors`, and `--rating-scale` (per `league_settings.json`).
> - `run_depth_chart_all.py` always applies `--all-level-charts`, `--no-pdf`, plus `--org`/`--year`/`--park-factors`. Pass `--min-comp 50` (or set per-league `min_comp` in settings) to flag empty starter slots.
> - All three accept `--leagues a,b,c` for a subset and `--skip x,y` for exclusions.
> - All three continue past per-league failures and print an OK/FAILED summary at the end.

---

## Per-sim checklist (single league focus)

When only one league has advanced, do this in the **web app** (open it, pick the league in the League Hub, run the eval and walk the pages) — or use the CLI steps below. Either way the rhythm is: refresh eval → review depth/lineups → check waivers/trades/FAs → set roster moves → export to StatsPlus.

> **Fresh ratings are ~1–2× per season, not per sim.** The app re-scores in-process from the local `data/` CSV every time; you only pull a fresh `/ratings` export (the app's fetch button, or the commands below) when ratings have actually moved.

### 1. Download new save / data

- [ ] Pull latest league save from StatsPlus
- [ ] **`py core\fetch_player_data.py --league {league}`** — auto-pulls + polls + saves to `data/PlayerData-{league}.csv`. `--osa` for OSA ratings, `--request-id GUID` to resume a queued export without hitting the rate limit. (Or use the app's "fetch fresh ratings" button.)
- [ ] If standings/news scraping wanted: **`py tools\current_standings.py --league {league}`**

### 2. Post-sim eval

```
py run_vos.py --league {league} --park-factors config/{league}-park-factors.json --contracts --per-org-evals
```

- [ ] **`run_vos.py`** — refresh eval CSV (always first; everything downstream depends on this). In the app, this is the **Run the eval** checklist item / Eval Browser page.
- [ ] **`py core\prospect_rankings.py --league {league}`** — refresh prospect board (app: Prospects page)
- [ ] **`py core\farm_value.py --league {league}`** — refresh farm $ values (app: Farm Value page)

### 3. Analyze your org

```
py core\depth_chart.py --league {league} --org "{org}" --year {year} --park-factors config/{league}-park-factors.json --all-level-charts --no-pdf
py tools\project_season.py --league {league} --org "{org}" --level ML --year {year} --blend-current-fip 0.7
```

- [ ] **`core\depth_chart.py`** — all-level depth chart with `--min-comp X` to flag empty starter slots (app: Depth Charts page)
- [ ] **`tools\project_season.py`** — ML projection vs current standings (not yet in the app)
- [ ] If anything notable (slumping starter, hot prospect): check **`tools\player_card.py`** (app: Player Card page) or **`core\what_if.py`**

### 4. Roster decisions + upload

- [ ] Set lineups / rotation / bullpen roles per depth chart output
- [ ] Process any incoming trade offers, waiver claims
- [ ] Save and **upload changes back to StatsPlus**

### 5. Daily flavor / news (optional)

- [ ] **`py tools\statsplus_paper_news.py --league {league}`** — newspaper-style recap

> **⚠ Note on `statsplus_paper_news.py`:** Currently outputs a hardcoded HTML file that renders fine on desktop but breaks badly on mobile. Pending rewrite to **WordPress block formatting** for `vosiverse.com`. Hold off on heavy use until then.

---

## Per-league quick reference

For most of the daily pipeline, the bulk scripts read everything from `config/league_settings.json` and you don't need to type these out. The per-league notes below are mainly league-specific reminders and one-off scripts that aren't in the bulk runners (yet).

### NDL — Seattle Whalers (2055, rating scale 1-100)
- Rating scale is 1-100, not 20-80 (handled automatically via `league_settings.json`)
- **Remember:** NDL finance is a performance feedback loop, not market-size — frame budget conversations that way

### UBA — Atlanta Bandits (2041)
- FA cohort analysis is UBA-specific: `py tools\fa_cohort_analysis.py --league uba` in offseason
- Cohort cutoff: `draft_year >= 2036` (engine started OOTP 26 at game-year 2036)

### SDMB — Chihuahua Division del Norte (2049)
- Park factors file exists with neutral 1.000 values league-wide (so `--park-factors` is still applied, no-op effect)
- Active cap/floor debate context
- **Reminder:** dispersion in SDMB is driven by extensions, not FA spending
- Future: vContracts rollout planned here

### SAHL — Houston Astros (2061)
- Spring training: `py tools\spring_training_invites.py --league sahl` pre-season for non-roster invites
- Top-100 prospect scraping (`tools\scrape_prospects.py`) is SAHL-only

### TLG — Washington Nationals (2053)
- Standard pipeline, nothing special

### WWOBA — Arizona Diamondbacks (2039)
- Standard pipeline, nothing special

### WOBA — St. Louis Cardinals (2041)
- Trade block management lives in `woba/trade_block/`
- Use `py core\free_agent_market.py --league woba --org "St. Louis Cardinals" --level ML --year 2041` during FA windows

### BWB — Chihuahua Guerreros (2028, setup phase)
- **League still in final testing.** Year in settings is 2028 — confirm it's the real sim year before running bulk pipelines
- `league_ids.json` entries may not be complete yet; non-ML depth charts could come back sparse
- Use `--skip bwb` on bulk runners if you want to exclude during setup

---

## Weekly tasks (any day, batch across leagues)

- [ ] **`tools\org_depth_analysis.py`** — full org-wide depth report for your team in each league
- [ ] **`tools\org_strength_report.py --all-levels`** — positional strength roll-up
- [ ] **`core\org_summary_pdf.py`** — PDF render for archiving
- [ ] **`tools\contract_audit.py`** — fair-value audit across the league (especially useful for NDL/SDMB context)
- [ ] **`core\trade_block.py`** — refresh your own trade block in each league
- [ ] **`core\trade_targets.py`** — shopping list against current /tradeblock (app: Trade Targets page)

## Trade deadline window

- [ ] Daily: `core\trade_targets.py` for each league with your team
- [ ] Update `core\trade_block.py` after every roster move
- [ ] Use **`core\what_if.py`** to vet specific acquisition targets
- [ ] `tools\top_salary_avg.py` for salary-matching reference

## Pre-draft (per league)

- [ ] **`tools\draft_pool_analysis.py --league {league} --name {label}`** — tier the pool (`--name` sets the `draft_pool_analysis_{label}` folder; there is no `--year` flag)
- [ ] **`tools\draft_board.py --league {league} --team {team}`** — your suggested board based on org strength
- [ ] **`tools\draft_values.py`** — pick value table for trade evaluation
- [ ] On the clock: have the draft board open

## Post-draft (per league)

- [ ] **`tools\draft_grades.py --league {league} --num-teams N {league}/drafts/draft_pool_analysis_{label}/`** — pass the draft folder positionally; `--num-teams` is required
- [ ] **`tools\draft_grades_pdf.py`** — PDF for posting to league
- [ ] Archive pre-draft pool CSV in `{league}/drafts/{label}/`

## Offseason (per league)

- [ ] **`core\free_agent_market.py`** during FA window for each league with your team (app: Free Agents page)
- [ ] **`tools\fa_cohort_analysis.py`** — UBA specifically (engine-cohort gating)
- [ ] **`tools\park_recommender.py`** if considering park changes
- [ ] **`core\contract.py`** for individual extension talks
- [ ] **Spring training:** `tools\spring_training_invites.py` (SAHL) — non-roster invites
- [ ] Plan **vContracts** rollout in SDMB once builder is wired up

---

## For later use (if/when you commish)

Park these tasks separately — only relevant when running a league, not as a GM.

**Pre-sim (commish duties):**
- Verify all GMs have submitted lineups / depth charts (or are in vacation mode)
- Process pending trades, waiver claims, roster moves
- Resolve rules disputes flagged in commish channel
- Push prior-sim flavor recap to league channel

**Sim + export:**
- Open OOTP, load league save
- Sim to next break
- Tools → Import/Export → Export League / League File → upload to StatsPlus
- Confirm StatsPlus dashboard reflects new sim date

**Commish-only periodic:**
- Financial audit pipeline:
  1. Save finances HTML from StatsPlus
  2. `py tools\parse_financials.py --league {league}`
  3. `py tools\payroll_audit.py --league {league}`
  4. `py tools\budget_audit.py --league {league}`
- League-wide `tools\contract_audit.py` parity briefs (current NDL/SDMB pattern)
- `tools\park_recommender.py` for league-wide park rebalancing decisions

---

## Sanity rules (always)

1. **Bulk script order is fixed:** `tools\fetch_all_player_data.py` → `tools\run_vos_all.py` → `tools\run_depth_chart_all.py`. Each step depends on the previous one's output.
2. **`--year` is critical** for depth_chart/project_season — defaults to real calendar year, wrong for OOTP unless aligned. The bulk scripts pull it from `league_settings.json`; the single-league scripts need it explicitly.
3. **Park factors** live at `config/{league}-park-factors.json` (combined teams[] format) for all 8 leagues. Legacy single-park files are in `config/archive/`.
4. **`league_settings.json` is the source of truth** for org/year/rating_scale/min_comp. Update it once when a sim year ticks over; every bulk script picks it up.
5. **lid filtering:** non-ML stats need `league_ids.json` entries; currently configured for sahl/tlg/wwoba/woba. NDL/UBA/SDMB/BWB may need entries before non-ML depth charts return real data.
6. **StatsPlus API tokens expire every ~90 days.** When `tools\fetch_all_player_data.py` (or the app's fetch button) starts erroring with 401/403, regenerate from your S+ Preferences page and update `config/statsplus_tokens.json`.

---

## Daily "did I finish?" smoke test

End-of-day:
- [ ] Bulk runner summaries showed all 8 leagues `OK` (or expected `FAILED` count if you skipped any)
- [ ] Fresh `evaluation_summary_{league}_*.csv` in each `{league}/eval/`
- [ ] Depth chart MDs in each `{league}/depth/`
- [ ] Lineups / rotation submitted; changes uploaded back to StatsPlus
