# Ticket 0003 — League Management (admin UI + provisioning pipeline)

**Status:** Open · **Type:** Feature (UI + data layer) · **Filed:** 2026-06-03

## Summary

Add a top-level **League Admin** screen to the vosball Streamlit app that lets
the user **add / edit / remove leagues** from one place. A league is not a single
record today — it is an implicit slug (`ndl`, `sahl`, …) keyed across ~7 separate
`config/*.json` files, plus two large generated/imported data files. This ticket
introduces a unified registry service that reads and consistently *writes* the
per-league slice across all those files, an admin UI on top of it, and an
automated **provisioning pipeline** that builds a brand-new league's data files
straight from the StatsPlus API.

## Background / problem

- There is **no central league record**. The de-facto registry is "the set of
  slugs in `config/league_url.json`" (`webapp/status.py:44`,
  `configured_leagues()`), with `league_settings.json` as fallback.
- **Nothing writes `config/*.json` today.** The only persisted UI state is
  `webapp/.ui_settings.json` (`app.py:159`, merge-write pattern). Every config
  file is hand-edited.
- Onboarding a new league means hand-authoring entries across many files and
  hand-importing two large data files. That is the friction this ticket removes.

## Per-league settings inventory

Every per-league item the suite consumes, by file:

| File | Shape | Req? | Default / notes |
| --- | --- | --- | --- |
| `league_url.json` | `{slug: url}` | **Required** for any fetch | none |
| `statsplus_tokens.json` | `{slug: token}` + `_default` | Optional | falls back to `_default`, then cookie. **Secret — gitignored** |
| `league_settings.json` | `{slug: {…}}` | Optional | per-key defaults below |
| → `rating_scale` | `"20-80"` \| `"1-100"` | — | default `20-80` |
| → `org` | string (your team) | — | none |
| → `year` | int (in-game season) | — | none |
| → `min_comp` | float | — | default `50.0` |
| → `game_version` | string ("OOTP 27") | — | display only |
| → `sim_time` | string | — | display only |
| `league_ids.json` | `{slug: {level: [lids]}}` | Optional | levels `ML/AAA/AA/A+/A/A-/R` + `_independents`/`_international` (underscore keys skipped) |
| `{slug}_orgs.json` | `["Org name", …]` | Optional | falls back to teams Parent==0 scan |
| `divisions-{slug}.json` | `{subleague: {division: [teams]}}` | Optional | awards / rollups |
| `{slug}-gm-slack.json` | `{team: handle}` | Optional | draft_grades headlines (only ndl/sahl exist) |

**Large generated/imported files** (not hand-entered form fields):

| File | Size | Origin |
| --- | --- | --- |
| `teams-{slug}.json` | 13–39 KB | built from `/teams` API |
| `{slug}-park-factors.json` | 84–173 KB | built from `/ballparks` API |
| `data/PlayerData-{slug}.csv` | 1.5–7 MB | `fetch_player_data.py` (`/ratings`) |

**Out of scope** (global, not slug-keyed — a separate "engine settings" concern):
`id_maps.json`, `contract_config.json`, `depth_config.json`, `draft_grades.json`,
`weights_*.json`.

## Engine fact that shapes park-factor generation

`vosball/engine/park.py:89` (`apply_park_adjustments`) consumes the **derived
`tool_adjustments`** block, *not* `raw_park_factors`. The raw factors are
provenance only. The raw→`tool_adjustments` conversion ("SAHL conversion
formulas") exists **only as `_calculation_*` comment strings in the JSON — there
is no Python implementation**. Decision (this ticket): generate `raw_park_factors`
+ `team_info` and set all `tool_adjustments` to **1.0 (neutral)** — park factors
effectively off until tuned. Porting the real formulas is a Phase 5 follow-up.

## API field mappings

**`/teams`** (CSV `ID,Name,Nickname,Parent Team ID`) → `teams-{slug}.json`
`{ "<ID>": {"Name", "Nickname", "Parent"} }` — direct reshape.

**`/ballparks`** (JSON, ML teams only) → `{slug}-park-factors.json` entry per
team, keyed by `display_name`:

| Target | Source |
| --- | --- |
| key + `team_info.team_name` | `display_name` |
| `team_info.team_code` | `abbr` |
| `team_info.park_name` | — (not in `/ballparks` → blank) |
| `raw_park_factors.avg_overall` | `avg` |
| `raw_park_factors.avg_rhb` / `avg_lhb` | `avg_r` / `avg_l` |
| `raw_park_factors.doubles` / `triples` | `d` / `t` |
| `raw_park_factors.hr_overall` | `hr` |
| `raw_park_factors.hr_rhb` / `hr_lhb` | `hr_r` / `hr_l` |
| `tool_adjustments.*` | — (all 1.0, neutral) |
| (stash) `capacity` / `stadium_type` / `surface` | same (for Phase 5 profile gen) |

Bonus: `/ballparks` `display_name`s are exactly the contents of
`{slug}_orgs.json`, so provisioning generates the orgs list for free.

## Phased plan

- **Phase 0 — registry service (no UI).** `core/league_registry.py`:
  enumerate/load/save a unified `LeagueConfig` across all per-league files;
  atomic write + timestamped backup; preserve `_comment` and other leagues'
  entries; validation (slug shape, URL, token UUID, level labels). Unit tests.
- **Phase 1 — read-only admin page.** New top-level `League Admin` nav entry
  (`st.navigation`, `app.py:261/301`); list + detail.
- **Phase 2 — edit scalar settings**, incl. masked token field (writes the
  gitignored `statsplus_tokens.json`).
- **Phase 3 — structured editors**: `league_ids`, orgs, divisions, gm-slack
  (`st.data_editor`).
- **Phase 4 — add / remove league + provisioning pipeline.**
  `core/league_provision.py`: given slug + URL + token →
  (1) `GET /teams` → `teams-{slug}.json`;
  (2) `GET /ballparks` → `{slug}-park-factors.json` + `{slug}_orgs.json`;
  (3) write `league_url`/`statsplus_tokens`/`league_settings`/`league_ids` via
  Phase 0; (4) kick off initial `fetch_player_data`. Reuses
  `fetch_player_data.py` auth (`load_league_base`, `load_token_for`); `/teams`
  and `/ballparks` are single-shot (no polling). Remove = reverse, with
  confirmation + backup.
- **Phase 5 (optional)** — port SAHL conversion formulas (raw → real
  `tool_adjustments`) + generate `park_profile` from `capacity`/`stadium_type`/
  `surface`; token-expiry reminders.

## Open question to resolve in Phase 0/4

`vosball/engine/park.py` reads a `tool_adjustments`/`team_to_park_mapping`+`parks`
shape, while the actual `{slug}-park-factors.json` is a `teams{}`-keyed shape that
`depth_chart.py` reads. Verify exactly which consumer reads the `teams{}` format
and confirm the generated file matches each consumer's expectation before wiring
it into Phase 4.

## Acceptance criteria

- [ ] Registry service round-trips every per-league file without dropping
      `_comment`/sibling-league keys; writes are atomic with a backup.
- [ ] `configured_leagues()` and the new admin page agree on the league list.
- [ ] Edit a scalar setting in the UI → persisted to the right config file;
      reload reflects it.
- [ ] Add-league pipeline produces valid `teams-{slug}.json`,
      `{slug}-park-factors.json` (neutral adjustments), `{slug}_orgs.json`, all
      registry entries, and triggers the initial PlayerData fetch.
- [ ] Remove-league cleanly reverses, behind a confirmation, with a backup.
- [ ] Existing tests stay green (`webapp/tests/test_webapp.py`, core suites).

## Notes / out of scope

- Engine-tuning configs (weights, contract/depth/draft) are not "league" settings
  and are excluded.
- `park_name` is unavailable from `/ballparks`; left blank until a source appears.
- Tokens remain gitignored; the UI edits them in place (masked input).
