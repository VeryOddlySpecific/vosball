#!/usr/bin/env python3
"""
top_salary_avg.py — Average salary of the top X% (or top N) contracts in a given year.

Reads the latest evaluation_summary_<league>_*.csv from <league>/eval/ and computes
each player's salary for a given league year using Contract_* and ContractExtension_*
fields. Then averages the top --pct (default 16%) or top --cnum salaries.

Usage:
  py top_salary_avg.py --league sdmb
  py top_salary_avg.py --league sdmb --year 2049
  py top_salary_avg.py --league sdmb --cnum 50
  py top_salary_avg.py --league sdmb --pct 10
"""

from __future__ import annotations
# --- tools/ -> repo-root bootstrap (added during tools/ move) ---
import os as _os, sys as _sys
_ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
if _ROOT not in _sys.path:
    _sys.path.insert(0, _ROOT)
# --- end bootstrap ---


import argparse
import csv
import sys
from collections import Counter
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent.parent


def _f(row, key, default=0.0):
    v = row.get(key, "")
    if v in ("", None):
        return default
    try:
        return float(v)
    except ValueError:
        return default


def _i(row, key, default=0):
    return int(_f(row, key, default))


def salary_for_year(row, year: int) -> float | None:
    """Return the player's salary in `year`, or None if not under contract that year."""
    # Main contract
    cy = _i(row, "Contract_years")
    cs = _i(row, "Contract_season_year")
    if cy > 0 and cs > 0:
        idx = year - cs
        if 0 <= idx < cy:
            sal = _f(row, f"Contract_salary{idx}")
            if sal > 0:
                return sal
    # Extension picks up after main contract ends
    ey = _i(row, "ContractExtension_years")
    es = _i(row, "ContractExtension_season_year")
    if ey > 0 and es > 0:
        idx = year - es
        if 0 <= idx < ey:
            sal = _f(row, f"ContractExtension_salary{idx}")
            if sal > 0:
                return sal
    return None


def latest_eval(league: str) -> Path:
    eval_dir = SCRIPT_DIR / league / "eval"
    cands = sorted(eval_dir.glob(f"evaluation_summary_{league}_*.csv"))
    if not cands:
        # fall back to any pattern (handles the smdb typo etc.)
        cands = sorted(eval_dir.glob("evaluation_summary_*.csv"))
    if not cands:
        sys.exit(f"No evaluation_summary CSV in {eval_dir}")
    return cands[-1]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--league", required=True)
    ap.add_argument("--eval", help="Override eval CSV path (else latest in <league>/eval/)")
    ap.add_argument("--year", type=int, help="League year (defaults to most common Contract_season_year + current_year - 1)")
    ap.add_argument("--pct", type=float, default=16.0, help="Top percentile cutoff (default 16). Ignored if --cnum given.")
    ap.add_argument("--cnum", type=int, help="Explicit number of top contracts to average.")
    args = ap.parse_args()

    eval_path = Path(args.eval) if args.eval else latest_eval(args.league)
    with eval_path.open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    # Resolve year: pick the modal "current season" if not specified
    if args.year is None:
        seasons = Counter()
        for r in rows:
            cs = _i(r, "Contract_season_year")
            cur = _i(r, "Contract_current_year")
            if cs > 0 and cur > 0:
                seasons[cs + cur - 1] += 1
        if not seasons:
            sys.exit("Could not infer year; pass --year")
        year = seasons.most_common(1)[0][0]
    else:
        year = args.year

    salaries = []
    for r in rows:
        s = salary_for_year(r, year)
        if s is not None:
            salaries.append((s, r.get("Name", ""), r.get("Org", "")))
    salaries.sort(key=lambda t: -t[0])

    total = len(salaries)
    if total == 0:
        sys.exit(f"No salaries found for year {year}")

    if args.cnum:
        n = min(args.cnum, total)
        label = f"top {n} contracts"
    else:
        n = max(1, int(round(total * args.pct / 100.0)))
        label = f"top {args.pct:.1f}% ({n} of {total})"

    top = salaries[:n]
    avg = sum(s for s, _, _ in top) / n

    print(f"League: {args.league}   Year: {year}")
    print(f"Eval file: {eval_path.name}")
    print(f"Contracts in pool: {total}")
    print(f"Cutoff: {label}")
    print(f"Min salary in top group: ${top[-1][0]:,.0f}")
    print(f"Max salary in top group: ${top[0][0]:,.0f}")
    print(f"Average salary of top group: ${avg:,.0f}")


if __name__ == "__main__":
    main()
