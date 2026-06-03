#!/usr/bin/env python3
"""Phase 0+1 unit tests — trade_targets token auth + the pure scoring core.

Two things, both fully offline (no network, no file writes):

  • fetch_tradeblock's auth wiring — token is appended as ?token= and wins over
    a cookie; cookie-only path sends the Cookie header and no token. urlopen is
    monkeypatched so nothing leaves the box.
  • build_trade_targets — the extracted core that grades org needs and scores the
    tradeblock pool. Exercised against the ndl eval with a stubbed pid list
    derived from the eval itself (ratings-only: empty stats dicts).

    py tests/test_trade_targets_core.py        # exits non-zero on any failure
"""
from __future__ import annotations

import argparse
import glob
import sys
from pathlib import Path
from typing import Any, Dict, List

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "core"))  # app-consumed modules live under core/

import depth_chart as dc  # noqa: E402
import trade_targets as tt  # noqa: E402

_failures: list[str] = []


def check(cond: bool, label: str) -> None:
    if cond:
        print(f"PASS     {label}")
    else:
        print(f"FAIL     {label}")
        _failures.append(label)


def _latest_ndl_eval() -> Path:
    matches = sorted(glob.glob(str(ROOT / "ndl" / "eval" / "evaluation_summary_ndl_*.csv")))
    if not matches:
        raise SystemExit("No ndl eval found — run `py run_vos.py --league ndl` first.")
    return Path(matches[-1])


# -----------------------------------------------------------------------------
# Phase 0 — fetch_tradeblock auth wiring (urlopen monkeypatched)
# -----------------------------------------------------------------------------

class _FakeResp:
    def __init__(self, body: str) -> None:
        self._body = body.encode("utf-8")

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "_FakeResp":
        return self

    def __exit__(self, *exc: Any) -> bool:
        return False


def _patch_urlopen(payload: str) -> Dict[str, Any]:
    """Replace tt.urlopen with a recorder. Returns a dict the test reads after
    the call: {'url': <request full_url>, 'headers': <request headers dict>}."""
    seen: Dict[str, Any] = {}

    def fake(req: Any, timeout: int = 60) -> _FakeResp:  # noqa: ARG001
        seen["url"] = req.full_url
        seen["headers"] = dict(req.headers)
        return _FakeResp(payload)

    tt.urlopen = fake  # type: ignore[assignment]
    return seen


def test_fetch_auth() -> None:
    orig = tt.urlopen
    payload = '{"player_ids": [101, 202, 303]}'
    try:
        # Token present (with a cookie also passed): token wins.
        seen = _patch_urlopen(payload)
        pids = tt.fetch_tradeblock(
            "https://example.statsplus.net/api/leagues/1",
            cache_dir=None, cookie="sessionid=abc", token="TOKVALUE",
        )
        check(pids == ["101", "202", "303"], "token fetch parses pids")
        check("token=TOKVALUE" in seen["url"], "token appended as ?token= in request URL")
        check("Cookie" not in seen["headers"],
              "cookie header suppressed when a token is used (token wins)")

        # Cookie only (no token): cookie header sent, no token in URL.
        seen = _patch_urlopen(payload)
        pids = tt.fetch_tradeblock(
            "https://example.statsplus.net/api/leagues/1",
            cache_dir=None, cookie="sessionid=abc", token=None,
        )
        check(pids == ["101", "202", "303"], "cookie-only fetch parses pids")
        check("token=" not in seen["url"], "no token in URL on cookie-only path")
        check(seen["headers"].get("Cookie") == "sessionid=abc",
              "cookie header sent on cookie-only path")
    finally:
        tt.urlopen = orig


def test_resolve_token() -> None:
    # A clearly-bogus league must resolve to None and never raise.
    check(tt.resolve_token("___no_such_league___") is None,
          "resolve_token returns None for an unknown league")


# -----------------------------------------------------------------------------
# Phase 1 — build_trade_targets core (offline, ratings-only)
# -----------------------------------------------------------------------------

ORG = "Seattle Whalers"
VALID_TIERS = {"Critical", "Major", "Depth", "Set"}
VALID_CATS = {"Priority Target", "Need Fit", "Depth Add",
              "Premium (no need)", "Lottery", "Pass"}


def _args(min_composite: float, include_no_need: bool) -> argparse.Namespace:
    return argparse.Namespace(
        league="ndl", org=ORG, org_code=None,
        min_composite=min_composite, include_no_need=include_no_need,
    )


def _pick_pids(eval_rows: List[Dict[str, str]]):
    """Return (candidate_pids, own_org_pids) drawn from the eval itself so the
    core has real rows to evaluate without any network."""
    cand: List[str] = []
    own: List[str] = []
    for r in eval_rows:
        pid = (r.get("ID") or "").strip()
        if not pid:
            continue
        lvl = (r.get("League_Level") or "").strip().upper()
        org = (r.get("Org") or "").strip()
        if lvl not in tt.BLOCKING_LEVELS:
            continue
        if org.lower() == ORG.lower():
            if len(own) < 5:
                own.append(pid)
        elif org and len(cand) < 60:
            cand.append(pid)
    return cand, own


def test_core() -> None:
    eval_rows = dc.read_eval(_latest_ndl_eval())
    cfg = dc.load_config(tt.DEFAULT_CONFIG)
    levels = list(cfg["levels"].keys())
    year = dc.league_default_year("ndl") or 2055
    cand_pids, own_pids = _pick_pids(eval_rows)
    check(len(cand_pids) > 0, "test fixture found ML/AAA non-org candidate pids")
    check(len(own_pids) > 0, "test fixture found own-org ML/AAA pids")

    # Permissive run: keep everything so we reliably get a populated pool.
    res = tt.build_trade_targets(
        _args(min_composite=0.0, include_no_need=True),
        cfg, eval_rows, cand_pids + own_pids, levels, year, {}, {},
    )

    check(len(res["targets"]) == len(dc.HITTER_POSITIONS) + 4,
          "needs assessment returns one entry per hitter position + 4 pitcher roles")
    check(all(t["tier"] in VALID_TIERS for t in res["targets"]),
          "every need entry carries a valid tier")
    check(bool(res["all_org_hitters"] or res["all_org_pitchers"]),
          "org pool is non-empty for a real org")

    scored = res["scored"]
    check(len(scored) > 0, "core produces scored candidates")
    check(all(isinstance(c.get("_fit_score"), float) for c in scored),
          "each scored candidate carries a float _fit_score")
    check(all(c.get("_category") in VALID_CATS for c in scored),
          "each scored candidate carries a valid category")
    check(all("_need_entry" in c for c in scored),
          "each scored candidate carries a _need_entry slot")
    check(all((c.get("_level") or "").upper() in tt.BLOCKING_LEVELS for c in scored),
          "all candidates are scoped to ML/AAA")
    check(all((c.get("_current_org") or "").lower() != ORG.lower() for c in scored),
          "own-org players are excluded from candidates")
    scored_pids = {c.get("pid") for c in scored}
    check(not (scored_pids & set(own_pids)),
          "none of the injected own-org pids leaked into the scored list")

    # Filtering: the default floors strictly narrow the pool vs the permissive run.
    res_strict = tt.build_trade_targets(
        _args(min_composite=tt.MIN_TARGET_COMPOSITE, include_no_need=False),
        cfg, eval_rows, cand_pids + own_pids, levels, year, {}, {},
    )
    check(len(res_strict["scored"]) <= len(scored),
          "default floors (min-composite + drop no-need) don't widen the pool")

    # Empty tradeblock: no candidates, but needs are still graded, no crash.
    res_empty = tt.build_trade_targets(
        _args(min_composite=0.0, include_no_need=True),
        cfg, eval_rows, [], levels, year, {}, {},
    )
    check(res_empty["scored"] == [], "empty tradeblock yields no scored candidates")
    check(len(res_empty["targets"]) == len(dc.HITTER_POSITIONS) + 4,
          "needs are still graded with an empty tradeblock")


def main() -> int:
    test_fetch_auth()
    test_resolve_token()
    test_core()
    print()
    if _failures:
        print(f"{len(_failures)} FAILURE(S):")
        for f in _failures:
            print(f"  - {f}")
        return 1
    print("All trade_targets core tests passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
