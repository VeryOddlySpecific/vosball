# Ticket 0002 — Update NDL league game version to OOTP 27

**Status:** ✅ Completed · **Type:** Chore (config) · **Filed:** 2026-05-30 · **Completed:** 2026-05-30

## Resolution

Applied: `ndl.game_version` set to `"OOTP 27"` in `config/league_settings.json`.
Verified — `py check_exports.py --leagues ndl` now prints the `(OOTP 27)` tag
and the JSON loads cleanly.

## Summary

Bump the NDL league's `game_version` from `OOTP 26` to `OOTP 27` in
`config/league_settings.json`. NDL has moved to OOTP 27, so the per-league
setting should reflect the new game version.

## Change

In `config/league_settings.json`, the `ndl` block (currently line 9):

```json
"game_version": "OOTP 26",
```

→

```json
"game_version": "OOTP 27",
```

This is the single source of truth for the field — `game_version` appears only
in the eight league blocks of `config/league_settings.json`. `OOTP 27` is
already the value used by `sdmb`, `sahl`, `woba`, and `bwb`, so no new value
format is introduced.

## Blast radius

`game_version` is consumed in exactly one place today: `check_exports.py` reads
it (via `_load_league_settings()`) purely to print the version tag in the
export-status report — e.g. `[NEED] ndl (OOTP 26) — ...` (`check_exports.py:105`).
It does **not** feed any evaluation, scoring, fetch, or pre-flight logic. The
change is a metadata/label update only and carries no computational risk.

## Acceptance criteria

- [x] `config/league_settings.json` → `ndl.game_version` reads `"OOTP 27"`.
- [x] `py check_exports.py --leagues ndl` prints the `(OOTP 27)` tag.
- [x] No other NDL behavior changes (the field is display-only).

## Notes / out of scope

- A real OOTP 26 → 27 upgrade usually coincides with a fresh ratings export from
  the new game build. Confirm separately whether `data/PlayerData-ndl.csv` should
  be re-pulled from the OOTP 27 export — that's an operational step, not part of
  this one-field config edit.
- NDL's `rating_scale` (`1-100`) and the other `league_settings.json` keys are
  unrelated to the version bump and stay as-is.
