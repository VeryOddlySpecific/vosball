#!/usr/bin/env python3
"""Phase B unit tests — free_agent_market depth-freshness guard.

Verifies ensure_fresh_depth_batch's decision matrix: a batch current with the
latest eval is scanned as-is; a stale, legacy (no-provenance), or missing batch
triggers regeneration; and --no-auto-refresh suppresses regeneration. The
subprocess regen itself is stubbed so the test runs fully offline.

    py tests/test_fa_freshness.py        # exits non-zero on any failure
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

import free_agent_market as fam  # noqa: E402
import depth_chart as dc  # noqa: E402

_failures: list[str] = []


def check(cond: bool, label: str) -> None:
    if cond:
        print(f"PASS     {label}")
    else:
        print(f"FAIL     {label}")
        _failures.append(label)


def _make_batch(depth_dir: Path, org: str, ts: str, *, source_eval_ts, write_meta=True):
    """Create a minimal depth batch: one level CSV (+ optional meta sidecar)."""
    (depth_dir / f"{org}_ML_{ts}.csv").write_text("pid,name\n", encoding="utf-8")
    if write_meta:
        meta = {
            "org_slug": org, "batch_ts": ts,
            "source_eval": f"evaluation_summary_ndl_{source_eval_ts}.csv" if source_eval_ts else None,
            "source_eval_ts": source_eval_ts,
            "levels": ["ML"], "min_comp_global": 55.0, "min_comp_per_pos": {},
        }
        (depth_dir / f"{org}_{ts}_depth_meta.json").write_text(json.dumps(meta), encoding="utf-8")


def _args(no_refresh=False):
    return argparse.Namespace(
        league="ndl", input=None, org_code="sea", org="Seattle Whalers",
        no_auto_refresh=no_refresh, park_factors=None, min_comp=55.0, min_comp_pos=None,
    )


def run_case(label, *, setup, latest_eval_ts, no_refresh, expect_regen, expect_ts_kind):
    """expect_ts_kind: 'existing' | 'new' | None"""
    orig_find = dc.find_latest_eval
    orig_regen = fam.regenerate_depth_batch
    regen_calls = {"n": 0}
    try:
        with tempfile.TemporaryDirectory() as tmp:
            depth_dir = Path(tmp)
            existing_ts = setup(depth_dir)
            dc.find_latest_eval = lambda *a, **k: Path(f"x/evaluation_summary_ndl_{latest_eval_ts}.csv")

            def fake_regen(args, dd, org_slug, target_year, prior_meta):
                regen_calls["n"] += 1
                new_ts = "29991231_235959"
                _make_batch(dd, org_slug, new_ts, source_eval_ts=latest_eval_ts)
                return new_ts
            fam.regenerate_depth_batch = fake_regen

            result = fam.ensure_fresh_depth_batch(_args(no_refresh), depth_dir, "sea", 2055)

            check(regen_calls["n"] == (1 if expect_regen else 0),
                  f"{label}: regen {'called' if expect_regen else 'NOT called'}")
            if expect_ts_kind == "existing":
                check(result == existing_ts, f"{label}: returns existing batch ts")
            elif expect_ts_kind == "new":
                check(result == "29991231_235959", f"{label}: returns regenerated batch ts")
            elif expect_ts_kind is None:
                check(result is None, f"{label}: returns None")
    finally:
        dc.find_latest_eval = orig_find
        fam.regenerate_depth_batch = orig_regen


def main() -> int:
    # Fresh: batch built from the latest eval → scan as-is, no regen.
    def s_fresh(dd):
        _make_batch(dd, "sea", "20260601_000000", source_eval_ts="20260601_201216")
        return "20260601_000000"
    run_case("fresh", setup=s_fresh, latest_eval_ts="20260601_201216",
             no_refresh=False, expect_regen=False, expect_ts_kind="existing")

    # Stale: batch built from an older eval → regenerate.
    def s_stale(dd):
        _make_batch(dd, "sea", "20260101_000000", source_eval_ts="20260101_000000")
        return "20260101_000000"
    run_case("stale", setup=s_stale, latest_eval_ts="20260601_201216",
             no_refresh=False, expect_regen=True, expect_ts_kind="new")

    # Legacy: batch with no provenance sidecar → treated as stale, regenerate.
    def s_legacy(dd):
        _make_batch(dd, "sea", "20260101_000000", source_eval_ts=None, write_meta=False)
        return "20260101_000000"
    run_case("legacy", setup=s_legacy, latest_eval_ts="20260601_201216",
             no_refresh=False, expect_regen=True, expect_ts_kind="new")

    # Missing: no batch at all → regenerate.
    run_case("missing", setup=lambda dd: None, latest_eval_ts="20260601_201216",
             no_refresh=False, expect_regen=True, expect_ts_kind="new")

    # Stale + --no-auto-refresh → warn, scan stale batch, no regen.
    run_case("stale+no-refresh", setup=s_stale, latest_eval_ts="20260601_201216",
             no_refresh=True, expect_regen=False, expect_ts_kind="existing")

    print()
    if _failures:
        print(f"{len(_failures)} check(s) FAILED: {', '.join(_failures)}")
        return 1
    print("All FA-freshness checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
