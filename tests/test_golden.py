#!/usr/bin/env python3
"""Golden-output regression harness for the VOSBall refactor.

Guarantees the refactor changes NO numbers: each case runs a tool on a small,
pinned, committed input fixture and asserts the output is byte-identical (after
stripping volatile timestamps) to a committed snapshot.

    py tests/test_golden.py            # verify; exits non-zero on any drift
    py tests/test_golden.py --update   # regenerate fixtures + snapshots after an INTENTIONAL change

Layout (all committed, so the baseline travels with the repo):
    tests/fixtures/data/PlayerData-<league>.csv   pinned input subset
    tests/golden/<case>.csv                        expected output

Phase 0 covers the VOS engine (run_vos.py) on a 20-80 league (wwoba) and a
1-100 league (ndl) — both rating scales. Append to CASES as more tools are
migrated onto the new core in later phases.
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent                       # sandbox root (G:\vosball)
FIX_DATA = HERE / "fixtures" / "data"
GOLD = HERE / "golden"
SAMPLE_ROWS = 200

# (case_id, league) — each league must have config + data present.
CASES = [
    ("engine_wwoba_20-80", "wwoba"),
    ("engine_ndl_1-100", "ndl"),
]

TS = re.compile(r"\d{8}_\d{6}")          # a run-timestamp token, if one leaks into content


def ensure_data_fixture(league: str) -> Path:
    """A small, committed PlayerData subset for `league`: header + first
    SAMPLE_ROWS rows of the real export. VOS scores are per-player absolute
    (fixed center/scale, no cohort-relative terms), so a subset yields the same
    per-player numbers as the full file — but far faster and tiny enough to commit."""
    dst = FIX_DATA / f"PlayerData-{league}.csv"
    if dst.exists():
        return dst
    src = ROOT / "data" / f"PlayerData-{league}.csv"
    if not src.exists():
        raise SystemExit(f"cannot build fixture - missing {src}")
    FIX_DATA.mkdir(parents=True, exist_ok=True)
    rows = []
    with src.open("r", encoding="utf-8", errors="replace", newline="") as fh:
        for i, line in enumerate(fh):
            rows.append(line)
            if i >= SAMPLE_ROWS:         # header (line 0) + SAMPLE_ROWS data rows
                break
    dst.write_text("".join(rows), encoding="utf-8", newline="")
    return dst


def run_engine(league: str, out_csv: Path) -> None:
    ensure_data_fixture(league)
    cmd = [sys.executable, str(ROOT / "run_vos.py"),
           "--league", league,
           "--data-dir", str(FIX_DATA),
           "--config-dir", str(ROOT / "config"),
           "--output", str(out_csv)]
    res = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True)
    if res.returncode != 0 or not out_csv.exists():
        sys.stderr.write((res.stdout or "") + "\n" + (res.stderr or "") + "\n")
        raise SystemExit(f"run_vos failed for {league} (exit {res.returncode})")


def normalize(text: str) -> str:
    """Drop any line carrying a run timestamp so identical computation compares
    equal; collapse CRLF/LF so the diff is content-only."""
    out = [ln.rstrip("\r") for ln in text.splitlines() if not TS.search(ln)]
    return "\n".join(out).strip("\n")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="VOSBall golden-output regression tests.")
    ap.add_argument("--update", action="store_true",
                    help="Regenerate fixtures + golden snapshots (only after an INTENTIONAL change).")
    args = ap.parse_args(argv)
    GOLD.mkdir(parents=True, exist_ok=True)

    failures = []
    for case_id, league in CASES:
        gold = GOLD / f"{case_id}.csv"
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "out.csv"
            run_engine(league, out)
            got = normalize(out.read_text(encoding="utf-8", errors="replace"))
        rows = got.count("\n")           # data rows (header is line 0)
        if args.update:
            gold.write_text(got + "\n", encoding="utf-8")
            print(f"updated  {case_id:22s} {rows} rows")
        elif not gold.exists():
            failures.append(case_id)
            print(f"MISSING  {case_id:22s} (run --update first)")
        elif got == normalize(gold.read_text(encoding="utf-8", errors="replace")):
            print(f"PASS     {case_id:22s} {rows} rows")
        else:
            failures.append(case_id)
            print(f"FAIL     {case_id:22s} output differs from golden")

    if args.update:
        print("\nFixtures + golden snapshots written. Review & commit tests/.")
        return 0
    if failures:
        print(f"\n{len(failures)} FAILED: {', '.join(failures)} "
              f"(refactor changed output - investigate before committing)")
        return 1
    print(f"\nAll {len(CASES)} golden cases passed - output is byte-identical to baseline.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
