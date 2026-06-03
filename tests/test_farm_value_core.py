#!/usr/bin/env python3
"""Phase 1 unit tests — farm_value scoring core (VPC + org ranking).

Fully offline with self-contained fixtures (no prospect_rankings / eval files,
no network):

  • build_farm_values with a contracts-bearing eval -> real VPC, dollar values,
    ranked org totals.
  • build_farm_values with a no-contracts eval -> graceful VPC=1.0 fallback
    (model points), with the *same* org ranking (VPC is a global scalar).
  • rank_org_values stamps rank / num_orgs and orders by farm_value_total.

    py tests/test_farm_value_core.py        # exits non-zero on any failure
"""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "core"))  # app-consumed modules live under core/

import farm_value as fv  # noqa: E402

_failures: list[str] = []


def check(cond: bool, label: str) -> None:
    if cond:
        print(f"PASS     {label}")
    else:
        print(f"FAIL     {label}")
        _failures.append(label)


# Prospect board: org A=180, C=90, B=60 in summed prospect_score -> rank A,C,B.
RANK_ROWS = [
    {"ID": "p1", "Name": "Aaa", "Org": "A", "Team": "", "League_Level": "AAA", "Age": "21", "Pos": "SS", "prospect_score": "100"},
    {"ID": "p2", "Name": "Bbb", "Org": "A", "Team": "", "League_Level": "AA", "Age": "20", "Pos": "CF", "prospect_score": "80"},
    {"ID": "p3", "Name": "Ccc", "Org": "B", "Team": "", "League_Level": "AAA", "Age": "22", "Pos": "1B", "prospect_score": "60"},
    {"ID": "p4", "Name": "Ddd", "Org": "C", "Team": "", "League_Level": "A", "Age": "19", "Pos": "3B", "prospect_score": "50"},
    {"ID": "p5", "Name": "Eee", "Org": "C", "Team": "", "League_Level": "A+", "Age": "20", "Pos": "RF", "prospect_score": "40"},
]

EVAL_CONTRACTS = [
    {"ID": "m1", "Org": "A", "League_Level": "ML", "Contract_is_major": "1", "Contract_salary0": "10000000", "VOS_Potential": "50"},
    {"ID": "m2", "Org": "B", "League_Level": "ML", "Contract_is_major": "1", "Contract_salary0": "5000000", "VOS_Potential": "40"},
    {"ID": "m3", "Org": "A", "League_Level": "ML", "Contract_is_major": "1", "Contract_salary0": "20000000", "VOS_Potential": "60"},
    {"ID": "m4", "Org": "C", "League_Level": "ML", "Contract_is_major": "1", "Contract_salary0": "8000000", "VOS_Potential": "45"},
]

# No contract columns -> VPC calibration finds no MLB rows -> fallback path.
EVAL_NO_CONTRACTS = [
    {"ID": "x1", "Org": "A", "League_Level": "ML", "VOS_Potential": "50"},
    {"ID": "x2", "Org": "B", "League_Level": "AAA", "VOS_Potential": "40"},
]


def _ranks(org_rows):
    """{Org: rank} for quick assertions."""
    return {r["Org"]: r["rank"] for r in org_rows}


def test_with_contracts() -> None:
    res = fv.build_farm_values(RANK_ROWS, EVAL_CONTRACTS)
    check(res["vpc_ok"] is True, "contracts eval -> vpc_ok True")
    check(res["vpc_base"] > 0, "contracts eval -> positive VPC")
    check(res["mlb_count"] == 4, "all 4 MLB contract rows used for calibration")
    check(len(res["player_rows"]) == len(RANK_ROWS), "every prospect row is valued")

    org_rows = res["org_rows"]
    check(len(org_rows) == 3, "three orgs summarized")
    check(all(r["num_orgs"] == 3 for r in org_rows), "num_orgs == 3 on every row")
    ranks = _ranks(org_rows)
    check(ranks == {"A": 1, "C": 2, "B": 3}, "org ranking is A(1) > C(2) > B(3)")
    check(org_rows[0]["Org"] == "A" and org_rows[0]["rank"] == 1,
          "list is ordered by rank (A first)")
    # Dollars scale by VPC: A's total should exceed C's should exceed B's.
    tot = {r["Org"]: float(r["farm_value_total"]) for r in org_rows}
    check(tot["A"] > tot["C"] > tot["B"], "dollar totals follow the ranking")


def test_no_contracts_fallback() -> None:
    res = fv.build_farm_values(RANK_ROWS, EVAL_NO_CONTRACTS)
    check(res["vpc_ok"] is False, "no-contracts eval -> vpc_ok False (fallback)")
    check(res["vpc_base"] == 1.0, "fallback VPC is exactly 1.0")
    ranks = _ranks(res["org_rows"])
    check(ranks == {"A": 1, "C": 2, "B": 3},
          "ranking identical to the contracts run (VPC is a global scalar)")
    tot = {r["Org"]: float(r["farm_value_total"]) for r in res["org_rows"]}
    check(abs(tot["A"] - 180.0) < 1e-6,
          "with VPC=1.0 a farm total equals the summed prospect_score (A=180)")


def test_rank_org_values_standalone() -> None:
    rows = [
        {"Org": "X", "farm_value_total": 10.0},
        {"Org": "Y", "farm_value_total": 30.0},
        {"Org": "Z", "farm_value_total": 20.0},
    ]
    out = fv.rank_org_values(rows)
    check(out[0]["Org"] == "Y" and out[0]["rank"] == 1, "highest total ranks 1st")
    check([r["rank"] for r in out] == [1, 2, 3], "ranks are 1..N in order")
    check(all(r["num_orgs"] == 3 for r in out), "num_orgs stamped on every row")


def main() -> int:
    test_with_contracts()
    test_no_contracts_fallback()
    test_rank_org_values_standalone()
    print()
    if _failures:
        print(f"{len(_failures)} FAILURE(S): " + ", ".join(_failures))
        return 1
    print("All farm_value core tests passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
