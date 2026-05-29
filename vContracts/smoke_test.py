#!/usr/bin/env python3
"""smoke_test.py — sanity check the scaffold end-to-end.

Builds a back-loaded contract with two stacked team options, validates it
against both SDMB rule profiles, and confirms:
  - pre-option-rule profile: clean
  - post-option-rule profile: catches the second team option

Run from inside vContracts/:
    python smoke_test.py
"""

from __future__ import annotations

from contract import ContractOffer, ContractYear, Option, OptionType, Incentive
from rules_profile import sdmb, sdmb_post_option_rule
from validator import validate


def build_sample_offer() -> ContractOffer:
    """A back-loaded 4-year deal with two stacked team options and a ceiling-
    of-attainability incentive. Headline reads big, real cost is modest."""
    return ContractOffer(
        player_id="P0001",
        player_name="Sample Player",
        years=(
            ContractYear(salary=2_000_000),
            ContractYear(salary=2_000_000),
            ContractYear(salary=2_000_000),
            ContractYear(
                salary=18_000_000,
                incentives=(
                    Incentive("MVP", 1_000_000, 0.03),
                    Incentive("Silver Slugger", 500_000, 0.10),
                ),
            ),
        ),
        options=(
            Option(type=OptionType.TEAM, salary=18_000_000, buyout=500_000),
            Option(type=OptionType.TEAM, salary=20_000_000, buyout=500_000),
        ),
        target_aav=10_000_000,
    )


def main() -> int:
    offer = build_sample_offer()
    print(f"Offer: {offer.player_name}")
    print(f"  Guaranteed years   : {offer.guaranteed_years}")
    print(f"  Total years        : {offer.total_years}")
    print(f"  Guaranteed total   : ${offer.guaranteed_total:,}")
    print(f"  Headline TCV       : ${offer.headline_tcv:,}")
    print(f"  Expected cost      : ${offer.expected_cost():,.0f}")
    print()

    for profile in (sdmb(), sdmb_post_option_rule()):
        print(f"--- {profile.name} ---")
        violations = validate(offer, profile)
        if not violations:
            print("  clean")
        for v in violations:
            loc = f" [{v.location}]" if v.location else ""
            print(f"  {v.severity.upper()} {v.rule}{loc}: {v.message}")
        print()

    # Self-check assertions
    pre = validate(offer, sdmb())
    post = validate(offer, sdmb_post_option_rule())
    assert pre == [], f"expected pre-rule profile clean, got {pre}"
    assert any(v.rule == "max_team_options" for v in post), \
        "expected post-rule profile to flag stacked team options"
    print("Assertions passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
