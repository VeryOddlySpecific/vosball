# vContracts — Design Notes

*Started: 2026-05-15*

A contract builder tool for OOTP. Built on the VOS framework.

---

## Mission Statement

This is not a fair-market-value tool. It is a **rule-exploitation tool**.

The premise: league rules permit contract shapes that other GMs refuse to use on principle. Those refusals are unilateral — the rulebook doesn't enforce them. vContracts identifies and constructs the contract structures that extract maximum advantage from the gap between *what the rules allow* and *what the room considers tasteful*.

If a structure is legal, it's on the table. The only constraint is the rulebook.

## League Context

- Primary target: **SDMB** (where the flat-shape rule proposal was rejected)
- Rule status: no cap on single-year salary relative to lowest year — confirmed open
- Should generalize to other leagues by swapping a rules profile

---

## Primary Use Case — Roster-Internal Extension Planner

vContracts is scoped to **extension planning for the team's current roster**, not free-agent pursuit. The input is who you already have; the output is a multi-year extension schedule.

### Steps

1. Load the team's existing contracts (`players_contract`, `players_contract_extension`) → build the starting **cap calendar** (committed $ per future year).
2. Filter the roster for **extension candidates**: players whose deals expire within the planning horizon AND who clear a VOS quality threshold AND who don't have a credible internal replacement landing before their guaranteed years end (see depth_chart gatekeeper below).
3. For each surviving candidate, call `contract.run_valuation()` to get the **market AAV anchor** — what the agent will expect.
4. **Joint optimization across all candidates simultaneously**: pick `(signing_year, length, structure)` for each, such that every balloon/option year lands on a different future season under the configured ceiling. Signing-year is a real lever — extensions have flexible timing, so holding an extension a year can push its balloon out and prevent stacking.
5. Output a per-candidate recommendation: extend in year N with shape X (balloons land years Y, Z), or **don't extend** if depth chart says the position fills internally before the contract would expire.

### Per-candidate constraints

- **Age-driven shape lock.** Players ≥ ~33 at signing have no plausible "future me trades or releases" exit on a back-loaded balloon — they'll retire on the team's books. The planner must mark these candidates as flat-or-front-loaded only; otherwise the tool schedules dead money. (Exact age threshold is a tunable per-player or per-position parameter.)
- **Depth chart is the gatekeeper, not just an input.** If a prospect with ETA Y4 will man the position, extending the incumbent past Y4 is the cap mistake regardless of how cleanly the balloon schedules. The planner must be able to output "no extension recommended" as a first-class answer.
- **Player AI acceptance is NOT a hard constraint.** The tool's job is to surface the most extreme team-advantage legal structure. If the in-engine player AI rejects it, that's the starting point for negotiation — not a reason to soften the recommendation. Don't pre-filter for acceptability; let the user negotiate from the extreme.

### Reusable components from existing tools

- **`free_agent_market.AAVContext`** already does the VPC calibration + per-player `compute(rec) → AAV` and `suggest_contract(rec, max_years, floor_ratio) → (years, aav, total)` math. Its decline-cutoff heuristic (longest L where year-L value ≥ floor_ratio × year-1 value) is exactly the length-suggestion logic vContracts needs for each extension candidate. Refactor it into a shared module (`market_valuation.py`?) and import from both tools rather than duplicating.
- **`depth_chart.py`** outputs are the gatekeeper signal: gap calendar + prospect ETAs per position.
- **`contract.run_valuation()`** is the fair-value engine vContracts wraps; `AAVContext` is the convenient interface to it.

---

## Core Exploitation Vectors

### 1. Extreme back-loading
Structure: trivial early years, balloon final year(s). e.g. `$1M / $1M / $1M / $50M`.
- Wins now on a near-free contract.
- The balloon year is a future-you problem — and future-you has the option to trade, release (if buyouts are cheap), or have already won.
- Maximally punishing if the league uses actual annual salary for cap accounting, not AAV.

### 2. Extreme front-loading
Structure: pay it all up front. e.g. `$50M / $1M / $1M / $1M`.
- Burn cap now while the window is open.
- Asset becomes near-free in out years — trade chip with negative salary.
- Useful when "going for it" this season and you have cap room you'd otherwise lose.

### 3. Step-function jumps
Structure: flat-then-spike. e.g. `$5M / $5M / $5M / $30M`.
- Built-in untradeable year creates a de facto NTC without granting one.
- Locks the player to your roster on your terms.

### 4. Deferred money (if engine allows)
- Pay nominal dollars years after the contract ends.
- Real cost is heavily discounted; reported total contract value is inflated, which can satisfy a player's "total guaranteed" preference cheaply.
- The Bobby Bonilla move.

### 5. Signing bonus mechanics
- Determine how signing bonuses hit the cap in this league (prorated vs. lump).
- If prorated: huge bonus + minimum salaries is a back-loading variant.
- If lump and league uses actual salary: front-loading variant.

### 6. Stacked team options (default posture)
- **Every contract carries two team options.** This is the standing template, not a special case.
- Stacking gives the team unilateral control over the back end of the contract — effectively guaranteed years that can be walked away from for the cost of a buyout.
- Pair with cheap buyouts so the cost-to-walk is trivial.
- Effect: a "4-year" deal is actually a 4 / 5 / 6-year deal at the team's option.
- Player options and vesting options are off the menu unless they buy a meaningful discount elsewhere.

### 7. Performance incentives at the ceiling of attainability
- Build incentives around milestones the player is statistically *unlikely* to hit. Not impossible — that reads as bad-faith — but **the hardest reachable benchmarks** for that player's profile.
- For hitters: MVP, batting title, 40+ HR, 200 hits, Silver Slugger
- For pitchers: Cy Young, 20 wins, 200+ IP for arms with workload risk, sub-2.50 ERA
- Layer them. A contract with $1M each at MVP/Silver Slugger/All-Star/Gold Glove looks lucrative on paper — true expected cost is a fraction of the stated bonus pool.
- Headline TCV (total contract value) goes up, real EV cost stays low, agent gets a number to point at.

### 8. Length-as-leverage
- Long tail of minimum-salary years to drag AAV down.
- Cheap years bank as trade assets even if the player is washed.

### 9. Opt-out asymmetry (use sparingly)
- Generally bad for the team — player walks when good, stays when bad.
- Only viable when paired with an injury-risk discount you're underwriting.

---

## Inputs the Tool Needs

- **Roster + existing contracts**: every player on the team with a current contract, their expiration year, and their per-year salary commitments. Drives the cap calendar's starting ledger.
- **Per extension candidate**:
  - VOS current + potential, age, position — from `evaluation_summary_{league}_*.csv`
  - **Shape feasibility flag** — derived from age (and optionally injury history). `back_loadable: bool`. Players ≥ ~33 at signing default to `False`.
  - Market AAV anchor — from `AAVContext.compute(rec)` / `suggest_contract(rec)`
- **Depth chart projection** across the planning horizon: per-position gap year + internal replacement ETA. Used to gatekeep extension recommendations.
- **Team cap state**: current and multi-year projection from existing contracts. Competitive window phase (configurable — affects how aggressively to load near-term years).
- **League rules profile** (see `rules_profile.py`): cap basis, deferrals/bonuses, option restrictions, shape rules, trade rules.

## Outputs

- **Recommended structure**: year-by-year salary, options, bonuses, deferrals
- **Reported headline**: total $ and years (the number other GMs will react to)
- **True expected cost**: present-value adjusted, probability-weighted on escalators/options
- **Exploitation tag**: which vector(s) this contract leans on, so it's visible what the structure is doing
- **Risk profile**: what has to be true in year N for this to remain a win

---

---

## Rolling Multi-Year Cap Planning

The single-contract exploitation vectors above only work if their balloon/option years don't **stack** on the same future season. A $15M balloon in 2028 is a tool; three of them landing on 2028 is a self-inflicted cap crisis.

### The cap calendar

A forward-looking ledger, by future season, showing:
- **Hard committed $**: guaranteed salary already on the books
- **Balloon weight**: back-loaded years from prior signings that fall in this season
- **Probable-option $**: team-option years we expect to exercise
- **Buyout outs**: cost to walk away from each option in this season
- **Free space**: cap remaining if we exercise all expected options
- **Decline space**: cap remaining if we buy out everything optional

### Scheduling constraint

Each future season carries a max acceptable balloon weight (configurable — e.g. "no more than one $15M+ balloon in any one year"). New contract structures must be feasible against the calendar before they're proposed.

### Signing-window planner

Given:
- A multi-year list of target players (from depth_chart gap analysis, see below)
- The current cap calendar
- Each player's market window (when they're a FA, arb-eligible, extendable)

The planner outputs: **who to sign in which offseason, and which back-end shape to use**, such that balloon/option years spread across the calendar instead of stacking. Optimizes for max headline TCV with min guaranteed money against the constraint that no single future year exceeds the balloon ceiling.

Example trace:
- Year 1 sign Player A: `$2M / $2M / $15M(opt) / $15M(opt)` → balloons land 2028, 2029
- Year 2 sign Player B: balloon years must avoid 2028 and 2029 → planner produces `$3M / $14M(opt) / $3M / $14M(opt)` → balloons land 2027, 2030
- Year 3 sign Player C: must avoid 2027–2030 stack → planner produces a front-loaded or flat shape, or pushes balloons to 2031+

---

## Integration with depth_chart

depth_chart.py is the **gatekeeper** for extension recommendations — not just a demand signal. If the position fills internally before an extension's guaranteed years end, the right answer is "don't extend," regardless of contract economics.

### What depth_chart feeds vContracts

- **Internal replacement ETA** per position: when does a prospect (or existing younger player) project to take this slot? The extension's guaranteed length must not push meaningfully past that ETA without strong justification.
- **Positional gap calendar**: by year, which positions/roles project below replacement. Used both for free-agent planning (future scope) and as a sanity check that the extension candidate is actually filling a need vs. blocking a prospect.
- **Composite-tier needs**: drives the dollar tier of the offer.

### What vContracts feeds back to depth_chart

- **Contract-state overlay** on the depth chart: each player's guaranteed years remaining, option status, balloon-year flag.
- **Forced-roster flag**: players whose contract structure makes them un-cuttable in a given year.

### Workflow (extension planner)

1. Pull the team's current `players_contract` + roster.
2. Run depth_chart across the planning horizon to get internal replacement ETAs.
3. For each player whose deal expires within the horizon: pass through the gatekeeper (VOS threshold + no internal replacement before extension would end).
4. Survivors → call `AAVContext.suggest_contract()` for the market anchor.
5. Joint planner picks `(signing_year, length, structure)` per candidate, staggering balloons against the cap calendar.
6. Output the schedule. Each accepted extension updates the cap calendar; re-run next offseason.

---

## Open Questions

Resolved (2026-05-16):
- Cap is a **budget cap, annual basis** — back-loading is the dominant exploitation vector.
- **No deferrals**, **no signing bonuses** — vectors #4 and #5 are off the menu.
- **No trade or retained-salary restrictions** — full flexibility.
- **Flat-shape rule defeated** — back-loading is openly legal.
- **Length cap unconfirmed** — assume 10, configurable in `rules_profile.py`.
- **Pending option rule (6-3 to pass)**: only one team option per contract, cannot precede a player option. Captured as `sdmb_post_option_rule()` feature flag; flip the import the day the vote lands.

Still open:
- Exact age threshold above which a candidate is locked to flat-or-front-loaded (33? 34? position-dependent?).
- How aggressively to weight competitive-window phase in the planner's objective (heavier near-term loading when "going for it"; longer guaranteed tails when rebuilding).
- Balloon ceiling default — what's the max $ in any single future year the team is willing to risk on potential commitments? (Configurable; needs a starting value.)

## CLI / Interface Ideas

- `--aav <amount>` — target AAV to deliver to the player. Tool distributes that AAV across the contract years using the structure that **minimizes guaranteed money** (maximizes back-loading into options, signing-bonus games, and ceiling-of-attainability incentives). The agent sees the AAV number they wanted; the team commits as little of it as the rules allow.
- (future flags to capture as they come up)

## Ideas Parking Lot

- Auto-generate the *competing* offer the principled GM would make, so the gap is visible and quantifiable
- Library of named templates ("the Bonilla", "the Cliff", "the Trojan Horse") for fast deployment
- Sensitivity analysis: at what win% does each structure stop being a win?
