# Extract Outcomes — Resume Instructions

## What was running

```
py .\scripts\extract_outcomes.py --yearly-only
```

This backfills the `ratings_timeseries` table in `outcomes.db` with end-of-season
player ratings from each yearly dump. The table did not exist before this run.

## Current status (2026-05-19)

**Batch 1 COMPLETE** — yearly dumps 2026–2040 processed and published to `outcomes.db`.

Totals written:
- ratings_timeseries : 264,328 rows
- snapshots          : 344,910
- level_stats        : 2,427,466
- engine_value       : 209,269
- awards             : 94,800
- injuries           : 770,289

**Batch 2 PENDING** — yearly dumps 2041–2055 not yet processed.

## Command to resume

```
py .\scripts\extract_outcomes.py --yearly-only --start-dump dump_2041_yearly
```

Run this from `G:\OOTP Study 27\`. Takes ~46 minutes total (10 min copy +
26 min processing + 10 min publish).

## Why batching is required

The G: drive corrupts SQLite databases after ~58 minutes of sustained writes.
The fix was to split the 30 yearly dumps into two batches of 15. Each batch
finishes well within the safe window. If you're on a machine where G: is a
fast local SSD, you may be able to run all 30 at once:

```
py .\scripts\extract_outcomes.py --yearly-only
```

But if it crashes with `sqlite3.DatabaseError: database disk image is malformed`,
just re-run with `--start-dump dump_YYYY_yearly` pointing to where it left off.
The script is idempotent (INSERT OR REPLACE), so re-running already-processed
dumps is safe but slow.

## Code changes already applied

- `lib\db_io.py` — journal applied before copy, sidecar cleanup, synchronous=EXTRA
- `scripts\extract_outcomes.py` — new --start-dump and --max-dumps flags
