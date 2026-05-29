#!/usr/bin/env python3
"""
cli.py — Command-line entry point for vContracts.

NOT YET IMPLEMENTED — the builder it would call is still a stub.

Planned usage
-------------
    python -m vContracts.cli --player-id 12345 --aav 15000000 --years 5 \\
        --league sdmb

    python -m vContracts.cli --player-id 12345 --template bonilla --years 4

Flags
-----
- --aav <amount>     target AAV to deliver. Builder minimizes guaranteed money.
- --years <n>        total contract length (guaranteed + options)
- --league <name>    rules profile to load (sdmb | sdmb_post_option_rule | ...)
- --template <name>  apply a named structure instead of optimizing from scratch
- --balloon-ceiling  max acceptable balloon weight per future year (for the
                     cap calendar feasibility check)

Output
------
- Year-by-year breakdown (salary, option/buyout, incentives)
- Headline TCV (what the agent sees)
- Expected cost (probability-weighted)
- Exploitation tags (which vector(s) the contract leans on)
- Validator output: every violation, or "clean" if the offer is legal
"""

from __future__ import annotations

import argparse
import sys


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="vContracts",
        description="Rule-exploitation contract builder (see vContracts_design.md)",
    )
    parser.add_argument("--player-id", required=True)
    parser.add_argument("--aav", type=int, help="target annual average value ($)")
    parser.add_argument("--years", type=int, default=4)
    parser.add_argument("--league", default="sdmb")
    parser.add_argument("--template", help="named template (bonilla, cliff, ...)")
    parser.add_argument("--balloon-ceiling", type=int, default=15_000_000)
    args = parser.parse_args(argv)

    print(
        f"vContracts CLI is a stub. Args parsed: {vars(args)}",
        file=sys.stderr,
    )
    print(
        "Builder is not yet implemented. See vContracts_design.md.",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
