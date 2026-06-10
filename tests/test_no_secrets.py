#!/usr/bin/env python3
"""test_no_secrets.py — guard: no real credential values in tracked files.

Harvests every token-shaped value from the local, gitignored secret stores
(config/statsplus_tokens.json, config/statsplus_session.json) and fails if any
of them appears in ANY git-tracked file. This is exactly the mistake it guards
against: a real token pasted into a test fixture or docstring as an "example"
(it happened — found and scrubbed 2026-06-10; the tokens were rotated).

Run:  py tests/test_no_secrets.py

Skips cleanly (exit 0, with a notice) when the secret files don't exist
(fresh clone) or git isn't available — the guard only means something on a
machine that actually holds secrets.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SECRET_FILES = [
    REPO_ROOT / "config" / "statsplus_tokens.json",
    REPO_ROOT / "config" / "statsplus_session.json",
]

# StatsPlus tokens are UUIDs. Harvesting by shape from the raw text (rather
# than parsing JSON keys) catches values regardless of nesting or key names.
UUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)


def harvest_secrets() -> set[str]:
    found: set[str] = set()
    for path in SECRET_FILES:
        try:
            if path.exists():
                found.update(UUID_RE.findall(path.read_text(encoding="utf-8")))
        except OSError:
            pass
    return found


def tracked_files() -> list[Path]:
    out = subprocess.run(
        ["git", "ls-files", "-z"], cwd=REPO_ROOT,
        capture_output=True, text=True, check=True,
    ).stdout
    return [REPO_ROOT / p for p in out.split("\0") if p]


def main() -> int:
    secrets = harvest_secrets()
    if not secrets:
        print("SKIP     no local secret files found — nothing to guard")
        return 0
    try:
        files = tracked_files()
    except (OSError, subprocess.CalledProcessError) as e:
        print(f"SKIP     git unavailable ({e}) — cannot enumerate tracked files")
        return 0

    leaks: list[tuple[Path, str]] = []
    for f in files:
        try:
            text = f.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for s in secrets:
            if s in text:
                leaks.append((f, s))

    if leaks:
        print(f"FAIL     {len(leaks)} secret value(s) found in tracked files:")
        for f, s in leaks:
            # Print only a prefix — enough to locate it, not enough to leak
            # the rest via CI logs.
            print(f"  {f.relative_to(REPO_ROOT)}  contains  {s[:8]}…")
        print("Replace with a fake placeholder (e.g. "
              "'019e0000-0000-7000-8000-000000000000') and ROTATE the token.")
        return 1

    print(f"OK       {len(secrets)} secret value(s) checked against "
          f"{len(files)} tracked files — no leaks")
    return 0


if __name__ == "__main__":
    sys.exit(main())
