#!/usr/bin/env python3
"""Phase C unit tests — in-process FA fit core (biggest-holes-first).

Exercises score_fa_records + compute_fa_fit fully offline (ratings-only) against
the ndl eval, building the org pool the same way the depth UI page does. No
network, no file writes.

    py tests/test_fa_fit.py        # exits non-zero on any failure
"""
from __future__ import annotations

import argparse
import glob
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

import depth_chart as dc  # noqa: E402
import free_agent_market as fam  # noqa: E402

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


def _build_org_slots(eval_rows, level="ML", org="Seattle Whalers"):
    cfg = dc.load_config(dc.DEFAULT_CONFIG)
    args = argparse.Namespace(
        league="ndl", org=org, base_url=None,
        league_url_config=dc.DEFAULT_LEAGUE_URL, league_ids_config=dc.DEFAULT_LEAGUE_IDS,
        all_levels=False, lids=None, no_cache=False, no_stats=True, cache_dir=None,
    )
    year = dc.league_default_year("ndl") or 2055
    pool = dc.build_team_pool(level, args, cfg, eval_rows, year)
    level_cfg = pool["level_cfg"]
    placed = dc.assign_positions(pool["hitter_pool"], level_cfg)
    pslots = dc.assign_pitchers(pool["pitcher_pool"], level_cfg)
    starters = {pos: (placed[pos][0] if placed.get(pos) else None) for pos in dc.HITTER_POSITIONS}
    return cfg, level_cfg, starters, pslots


def main() -> int:
    eval_path = _latest_ndl_eval()
    eval_rows = dc.read_eval(eval_path)
    cfg, level_cfg, starters, pslots = _build_org_slots(eval_rows)
    floors = cfg.get("stat_floors", {})

    fa_records = fam.score_fa_records(eval_rows, level_cfg, floors)
    check(len(fa_records) > 0, "score_fa_records returns scored FAs")
    check(all("pos_scores_blended" in r and "composite" in r for r in fa_records),
          "FA records carry pos_scores_blended + composite")
    fa_hitters = [r for r in fa_records if not r["is_pitcher"]]
    fa_pitchers = [r for r in fa_records if r["is_pitcher"]]

    # High threshold (80) -> every slot is a hole; lets us test ranking + shape.
    hi = {p: 80.0 for p in dc.HITTER_POSITIONS}
    hi.update({role: 80.0 for role in fam.PITCHER_ROLES})
    fit = fam.compute_fa_fit(starters, pslots, fa_hitters, fa_pitchers, hi, top_n=3)

    hh, ph = fit["hitter_holes"], fit["pitcher_holes"]
    check(len(hh) == len(dc.HITTER_POSITIONS), "threshold 80 -> all hitter positions are holes")
    check(len(ph) >= 1, "threshold 80 -> pitcher roles surface as holes")

    # Biggest-holes-first ordering.
    check(all(hh[i]["gap"] >= hh[i + 1]["gap"] for i in range(len(hh) - 1)),
          "hitter holes sorted by gap descending")
    check(all(ph[i]["gap"] >= ph[i + 1]["gap"] for i in range(len(ph) - 1)),
          "pitcher holes sorted by gap descending")

    # Per-hole invariants.
    sample = hh[0]
    check(abs(sample["gap"] - (sample["threshold"] - sample["slot_score"])) < 1e-6,
          "gap == threshold - slot_score")
    check(all(len(h["fas"]) <= 3 for h in hh + ph), "FA candidates capped at top_n")
    check(all(abs(f["edge"] - (f["fit_score"] - h["slot_score"])) < 1e-6
              for h in hh for f in h["fas"]),
          "FA edge == fit_score - slot_score")
    # Candidates within a hole ranked by fit_score descending.
    holes_with_multi = [h for h in hh if len(h["fas"]) > 1]
    check(all(h["fas"][i]["fit_score"] >= h["fas"][i + 1]["fit_score"]
              for h in holes_with_multi for i in range(len(h["fas"]) - 1)),
          "FA candidates within a hole ranked by fit_score")

    # Low threshold (0) -> nothing qualifies as a hole.
    lo = {p: 0.0 for p in dc.HITTER_POSITIONS}
    lo.update({role: 0.0 for role in fam.PITCHER_ROLES})
    fit0 = fam.compute_fa_fit(starters, pslots, fa_hitters, fa_pitchers, lo)
    check(fit0["hitter_holes"] == [] and fit0["pitcher_holes"] == [],
          "threshold 0 -> no holes")

    _test_service_gate(eval_rows, level_cfg, floors)

    print()
    if _failures:
        print(f"{len(_failures)} check(s) FAILED: {', '.join(_failures)}")
        return 1
    print("All FA-fit checks passed.")
    return 0


def _test_service_gate(eval_rows, level_cfg, floors) -> None:
    """score_fa_records' /players-backed gate: amateurs (0 / unknown service) and
    retired players are dropped; real pros are kept; pro_service_days is attached.
    Offline — uses a synthetic /players lookup over real FA ids."""
    fa_rows = fam.fa_pool(eval_rows)
    p_pro, p_amateur, p_absent, p_retired = (fa_rows[i]["ID"].strip() for i in range(4))
    lookup = {
        p_pro: {"pro_service_days": "500"},
        p_amateur: {"pro_service_days": "0"},
        p_retired: {"pro_service_days": "900", "retired": "1"},
        # p_absent intentionally NOT in /players → unknown service.
    }
    recs = fam.score_fa_records(eval_rows, level_cfg, floors,
                                players_lookup=lookup, min_pro_service_days=1)
    ids = {r["pid"] for r in recs}
    check(p_pro in ids, "service gate keeps pro FA (500d)")
    check(p_amateur not in ids, "service gate excludes amateur (0d)")
    check(p_absent not in ids, "service gate excludes FA absent from /players")
    check(p_retired not in ids, "retired FA excluded")

    recs0 = fam.score_fa_records(eval_rows, level_cfg, floors,
                                 players_lookup=lookup, min_pro_service_days=0)
    ids0 = {r["pid"] for r in recs0}
    check(p_absent in ids0, "min=0 keeps unknown-service FA (gate off)")
    check(p_retired not in ids0, "retired excluded even with gate off")
    rec_pro = next(r for r in recs0 if r["pid"] == p_pro)
    check(rec_pro.get("pro_service_days") == 500, "pro_service_days attached as int")

    # No /players at all → gate can't run; nothing dropped for service.
    recs_no_pl = fam.score_fa_records(eval_rows, level_cfg, floors,
                                      players_lookup=None, min_pro_service_days=1)
    check(len(recs_no_pl) == len(fa_rows), "no /players -> service gate skipped (no drops)")


if __name__ == "__main__":
    sys.exit(main())
