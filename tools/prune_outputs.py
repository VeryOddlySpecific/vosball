#!/usr/bin/env python3
"""Retention tool for VOSBall's timestamped generated output.

The suite writes a fresh timestamped artifact every run (evaluation_summary_*,
depth/*, org_depth/*_positions.csv, farm/*, draft pools, etc.). Over a season
these accumulate into gigabytes of near-duplicate history. This keeps the
newest N of each output "series" per league and moves the rest aside.

Safety: dry-run by default — it prints what it WOULD move and changes nothing.
Even with --apply it *moves* files into archive/output_pruned/ mirroring their
relative path; it never deletes, so a wrong --keep is always recoverable.

A "series" is one logical output that gets re-emitted each run: the filename
with its `_YYYYmmdd_HHMMSS` timestamp token blanked out, scoped to its own
directory. eval CSV vs eval MD, and per-org subdirs, are therefore tracked
separately and each keeps its own newest N. Files with no timestamp token are
never touched.

Usage:
    py prune_outputs.py                      # dry-run, all leagues, keep 5
    py prune_outputs.py --league ndl         # dry-run one league
    py prune_outputs.py --keep 3 --verbose   # show every prunable file
    py prune_outputs.py --apply              # actually move (after reviewing the dry-run)
"""
from __future__ import annotations
# --- repo-root + core/ path bootstrap ---
import os as _os, sys as _sys
_ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
for _p in (_ROOT, _os.path.join(_ROOT, "core")):
    if _p not in _sys.path:
        _sys.path.insert(0, _p)
# --- end bootstrap ---


import argparse
import re
import shutil
import sys
from collections import defaultdict
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent.parent

# League slugs the suite manages. Only those present on disk are scanned.
KNOWN_LEAGUES = ["ndl", "sahl", "tlg", "uba", "sdmb", "wwoba", "woba", "bwb"]

# `_20260529_101126` — the timestamp token every tool stamps into output names.
TS_RE = re.compile(r"_(\d{8})_(\d{6})")

ARCHIVE_SUBDIR = "output_pruned"  # created under <root>/archive/


def series_key(rel_dir: str, name: str):
    """Return (group_key, sort_value) for a file, or None if it carries no
    timestamp token (those are left untouched). The group_key blanks the
    timestamp so re-runs of the same output collapse into one series; the
    sort_value is the 14-digit stamp as an int for newest-first ordering.
    rel_dir scopes the series to its own folder so identically-named files in
    different per-org subdirs don't merge."""
    m = TS_RE.search(name)
    if not m:
        return None
    stamp = int(m.group(1) + m.group(2))
    blanked = name[: m.start()] + "_<TS>" + name[m.end():]
    return (rel_dir + "|" + blanked, stamp)


def scan_league(league_dir: Path, keep: int):
    """Group every timestamped file under league_dir into series and return the
    files beyond the newest `keep` in each series."""
    groups = defaultdict(list)  # group_key -> [(stamp, Path)]
    for path in league_dir.rglob("*"):
        if not path.is_file():
            continue
        rel_dir = str(path.parent.relative_to(league_dir))
        key = series_key(rel_dir, path.name)
        if key is None:
            continue
        group_key, stamp = key
        groups[group_key].append((stamp, path))

    prune_list = []
    for items in groups.values():
        items.sort(key=lambda t: t[0], reverse=True)  # newest first
        prune_list.extend(path for _stamp, path in items[keep:])
    return prune_list


def output_type(path: Path, league_dir: Path) -> str:
    """First path component under the league dir — the output 'type'
    (eval, depth, org_depth, farm, ...). Used only for the summary rollup."""
    rel = path.relative_to(league_dir)
    return rel.parts[0] if len(rel.parts) > 1 else "(root)"


def human_bytes(n: int) -> str:
    f = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if f < 1024 or unit == "TB":
            return f"{f:.1f}{unit}"
        f /= 1024
    return f"{f:.1f}TB"


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        description="Prune old timestamped VOSBall outputs (dry-run by default)."
    )
    p.add_argument("--root", type=Path, default=SCRIPT_DIR,
                   help="Suite root (default: this script's directory)")
    p.add_argument("--league", action="append",
                   help="League slug; repeatable or comma-separated. Default: all present.")
    p.add_argument("--keep", type=int, default=5,
                   help="Newest copies to keep per output series (default 5)")
    p.add_argument("--apply", action="store_true",
                   help="Actually move files (default: dry-run, no changes)")
    p.add_argument("--verbose", action="store_true",
                   help="List every prunable file")
    args = p.parse_args(argv)

    if args.keep < 1:
        p.error("--keep must be >= 1")

    root = args.root.resolve()
    if args.league:
        requested = []
        for item in args.league:
            requested.extend(s.strip() for s in item.split(",") if s.strip())
    else:
        requested = KNOWN_LEAGUES
    leagues = [lg for lg in requested if (root / lg).is_dir()]
    for lg in (lg for lg in requested if not (root / lg).is_dir()):
        print(f"skip (no dir): {lg}", file=sys.stderr)
    if not leagues:
        print("No league output directories found to scan.", file=sys.stderr)
        return 1

    archive_root = root / "archive" / ARCHIVE_SUBDIR
    grand_files = 0
    grand_bytes = 0

    for lg in leagues:
        league_dir = root / lg
        prune_list = scan_league(league_dir, args.keep)
        if not prune_list:
            print(f"\n{lg}: nothing to prune (keep={args.keep}).")
            continue
        by_type = defaultdict(lambda: [0, 0])  # type -> [count, bytes]
        for path in prune_list:
            size = path.stat().st_size
            t = output_type(path, league_dir)
            by_type[t][0] += 1
            by_type[t][1] += size
            grand_files += 1
            grand_bytes += size
        print(f"\n{lg}:")
        for t in sorted(by_type):
            count, nbytes = by_type[t]
            print(f"  {t:<16} {count:>5} files  {human_bytes(nbytes):>9}")
        if args.verbose:
            for path in sorted(prune_list):
                print(f"    - {path.relative_to(root)}")
        if args.apply:
            for path in prune_list:
                dest = archive_root / lg / path.relative_to(league_dir)
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(path), str(dest))

    print("\n" + "=" * 52)
    print(f"{'moved' if args.apply else 'would move'}: "
          f"{grand_files} files  ({human_bytes(grand_bytes)})")
    if args.apply:
        print(f"destination: {archive_root}")
    else:
        print("DRY RUN - nothing changed. Re-run with --apply to move these to")
        print(f"          {archive_root}  (files are moved, never deleted).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
