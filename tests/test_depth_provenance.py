#!/usr/bin/env python3
"""Phase A unit tests — depth-chart eval provenance.

Exercises the pure provenance helpers added so free_agent_market.py can tell a
stale depth batch (built from an older eval than the current latest) from a
fresh one. No network, no full pipeline — just the timestamp parser and the
meta-sidecar writer.

    py tests/test_depth_provenance.py        # exits non-zero on any failure
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

import depth_chart as dc  # noqa: E402

_failures: list[str] = []


def check(cond: bool, label: str) -> None:
    if cond:
        print(f"PASS     {label}")
    else:
        print(f"FAIL     {label}")
        _failures.append(label)


def test_eval_ts_from_path() -> None:
    ts = dc.eval_ts_from_path(Path("wwoba/eval/evaluation_summary_wwoba_20260414_082248.csv"))
    check(ts == "20260414_082248", "eval_ts_from_path extracts embedded timestamp")
    check(dc.eval_ts_from_path(None) is None, "eval_ts_from_path(None) -> None")
    check(
        dc.eval_ts_from_path(Path("eval/no_timestamp_here.csv")) is None,
        "eval_ts_from_path returns None when no timestamp present",
    )
    # Newer eval sorts lexically after older — the basis for the staleness check.
    older = dc.eval_ts_from_path(Path("evaluation_summary_uba_20260101_000000.csv"))
    newer = dc.eval_ts_from_path(Path("evaluation_summary_uba_20260601_120000.csv"))
    check(older < newer, "eval timestamps are lexically comparable (stale < fresh)")


def test_write_depth_meta() -> None:
    args = argparse.Namespace(
        min_comp=45.0,
        min_comp_pos_map={"SS": 50.0},
    )
    eval_path = Path("uba/eval/evaluation_summary_uba_20260601_120000.csv")
    with tempfile.TemporaryDirectory() as tmp:
        out_dir = Path(tmp)
        meta_path = dc.write_depth_meta(
            out_dir, "hou", "20260601_130000", eval_path,
            ["ML", "AAA", "AA"], args,
        )
        check(
            meta_path == out_dir / "hou_20260601_130000_depth_meta.json",
            "write_depth_meta filename = {org}_{ts}_depth_meta.json",
        )
        check(meta_path.exists(), "write_depth_meta creates the sidecar file")
        payload = json.loads(meta_path.read_text(encoding="utf-8"))
        check(payload["source_eval"] == eval_path.name, "meta records source_eval filename")
        check(payload["source_eval_ts"] == "20260601_120000", "meta records source_eval_ts")
        check(payload["org_slug"] == "hou", "meta records org_slug")
        check(payload["batch_ts"] == "20260601_130000", "meta records batch_ts")
        check(payload["levels"] == ["ML", "AAA", "AA"], "meta records levels covered")
        check(payload["min_comp_global"] == 45.0, "meta records global min-comp")
        check(payload["min_comp_per_pos"] == {"SS": 50.0}, "meta records per-pos min-comp")

    # No-eval / no-threshold case still writes a valid sidecar (freshness must be
    # knowable even without --min-comp).
    bare = argparse.Namespace(min_comp=None, min_comp_pos_map={})
    with tempfile.TemporaryDirectory() as tmp:
        meta_path = dc.write_depth_meta(Path(tmp), "stl", "20260601_140000", None, ["ML"], bare)
        payload = json.loads(meta_path.read_text(encoding="utf-8"))
        check(payload["source_eval"] is None, "meta source_eval=None when no eval path")
        check(payload["source_eval_ts"] is None, "meta source_eval_ts=None when no eval path")
        check(payload["min_comp_global"] is None, "meta global min-comp None when unset")


def main() -> int:
    test_eval_ts_from_path()
    test_write_depth_meta()
    print()
    if _failures:
        print(f"{len(_failures)} check(s) FAILED: {', '.join(_failures)}")
        return 1
    print("All depth-provenance checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
