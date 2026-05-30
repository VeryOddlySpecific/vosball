# Ticket 0001 — Reduce `PlayerData` to ratings-only truth; source roster state from the API

**Status:** Open · **Type:** Enhancement (data layer) · **Filed:** 2026-05-30

## Summary

Make `data/PlayerData-{league}.csv` the source of truth **only for data the
StatsPlus API does not expose** — the slow-changing scouted component ratings.
Source the fast-moving *roster-state* fields (team, org, league level, age,
position) live from the API at eval time, the same opt-in way contract data is
already pulled. This decouples two refresh cadences that are currently welded
together.

## Background / problem

The suite **never calls a `/players` endpoint today** — the only network I/O in
the package is `/contract` + `/contractextension` (`vosball/data/contracts.py`).
So `PlayerData` is the *sole* source of truth for two very different kinds of
data with very different change rates:

- **Slow (true to PlayerData):** scouted component ratings (current + potential),
  personality, handedness, BABIP — the ~70 columns in
  `vosball/data/players.py::RATING_COLUMNS`. These only change meaningfully
  **once or twice a season** (scouting updates / development).
- **Fast (also from PlayerData today, but volatile):** `Team`, `Org`, `LgLvl`,
  `Age`, `Pos` — these change with routine roster churn (trades, call-ups,
  demotions, birthdays).

The fast fields are **not just display** — they feed the scoring math:

- `LgLvl` → `get_league_label` → **age-vs-level adjustment** (per-level
  `target_age`; `vosball/engine/adjustments.py::age_adjustment`, ~line 206) **and**
  the `league_label == "ML"` flag in **WAR projection**
  (`vosball/engine/core.py:122`).
- `Team` → **park context selection** when park factors are applied
  (`vosball/engine/park.py`).
- `Age` → age-vs-level adj, draft-age modifier, WAR debut projection, Reach
  feature, blend alpha.
- `Pos` → hitter/pitcher routing + position-eligibility scores.

**Consequence:** to keep team/level/age correct (in the UI and the ~50 tools),
you must re-pull the **entire `/ratings` export** — a heavy, rate-limited,
async job — far more often than the ratings themselves change, just to refresh a
handful of cheap, fast-moving fields. Skipping it leaves stale team/level labels
*and* subtly wrong level-sensitive scores for any player who moved levels since
the last pull.

(OOTP's own `OVR`/`POT` are **not** consumed by the engine — VOS computes its
own scores — so they are out of scope here.)

## Goal

- `PlayerData` becomes authoritative only for fields the API can't give us
  (ratings, personality, handedness, BABIP).
- Roster-state fields come from the API at eval time, opt-in, behind a flag —
  mirroring the existing `contracts` pattern.
- Net effect: pull the expensive ratings export on a **seasonal** cadence;
  refresh roster state **cheaply / live** whenever desired.

## Investigation (resolve before building)

1. **What does `/players` actually expose?** Confirm the exact fields and that
   the IDs line up with PlayerData semantics: `Team`/`Org`/`LgLvl` are integer
   IDs mapped via `config/id_maps.json` + `config/teams-{league}.json` /
   `get_league_label`. The API level encoding must map to the same
   `get_league_key_for_config` keys.
2. **Which fields are worth overlaying?** `LgLvl`/`Team`/`Org` (transactions) and
   `Age` (birthdays) and possibly `Pos` (conversions) move; `Bats`/`Throws`/
   personality are effectively static and can stay PlayerData-truth even if the
   API also exposes them.
3. **Auth + rate-limit profile** of `/players` vs `/ratings` (token in
   `config/statsplus_tokens.json`, same as the fetch scripts).

## Proposed design (follows the existing layering)

- **`vosball/data/roster.py`** (sibling of `contracts.py`):
  `load_roster_state(base_url, id_filter) -> {player_id: {Team, Org, LgLvl, Age,
  Pos, ...}}`. Pure I/O, endpoint-agnostic, raises `ValueError`/`URLError` like
  the other loaders. Re-export through `vosball/data/__init__.py`.
- **`vosball/services.py`**: add an opt-in param mirroring `contracts`, e.g.
  `evaluate_league(..., live_roster: bool = False, base_url=None)`. When on,
  after `load_player_data()` and **before** scoring, overlay the API roster
  fields onto each row dict — so age-vs-level, the WAR ML flag, and park context
  all use the fresh values. On fetch failure, **fall back** to the PlayerData
  values with a warning (same graceful-degradation posture as contracts).
- **Engine: unchanged.** It only reads row-dict keys and stays pure — overlaying
  values upstream needs no engine edits.
- **Reporting: unchanged.**
- **`vosball/cli.py`**: add `--live-roster` (parallels `--contracts`).
- **`webapp/app.py`**: add a sidebar toggle "Live roster (team/level/age from
  API)", wired through `evaluate(...)` → `evaluate_league(live_roster=...)`.

## Golden / testing

- **`--live-roster` OFF must be byte-identical to today** — the existing golden
  (`tests/test_golden.py`, 2 leagues × cli/service) stays green with no
  `--update`.
- For ON: do **not** let the harness hit the live network. Add a separate test
  with a **recorded/mocked `/players` payload** asserting that the overlay lands
  (e.g. a player whose level differs between the fixture CSV and the mock scores
  with the new level's age-vs-level adjustment). Keep it out of the
  byte-identical golden so the golden never goes flaky.

## Acceptance criteria

- [ ] `--live-roster` off → zero behavior change; golden green.
- [ ] `--live-roster` on → `Team`/`Org`/`LgLvl`/`Age`/`Pos` reflect the API; a
      player whose level changed since the last `/ratings` pull scores with the
      new level's adjustment and correct WAR ML flag.
- [ ] `PlayerData` can be refreshed on a seasonal (ratings) cadence without
      roster-derived staleness in the output.
- [ ] Graceful fallback to PlayerData values on API failure, with a logged
      warning.
- [ ] Docs updated: `LOGIC_UPDATE_PROCESS.md` (new input source → `data/`
      layer), `VOSBALL_USER_GUIDE.md` / `README`, and the UI `webapp/README.md`.

## Risks / notes

- If `/players` lacks a needed field or uses different semantics, that field
  **stays** PlayerData-truth — this is "reduce", not necessarily "eliminate".
- Adds a network dependency at eval time when enabled; must stay **opt-in** so
  the offline / point-in-time-snapshot workflow keeps working unchanged.
- The `contracts` implementation (`data/contracts.py` + the `contracts` flag
  through `services`/`cli`) is the working template to copy.
