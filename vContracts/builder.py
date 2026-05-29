#!/usr/bin/env python3
"""
builder.py — Construct ContractOffers that maximize team advantage against
a RulesProfile.

The builder is the engine that implements the exploitation vectors from the
design doc (back-loading, step-function, stacked team options, ceiling-of-
attainability incentives, length-as-leverage). Each strategy is a function
that takes player/team inputs + a rules profile and returns a ContractOffer.

NOT YET IMPLEMENTED. This module is a stub; the contract_builder.py at the
repo root is a fair-value tool, not the exploitation tool. See
vContracts_design.md §"Core Exploitation Vectors" for the strategy list.

Planned entry points
--------------------
- build_min_guaranteed(player, team, rules, target_aav)
    Deliver `target_aav` to the agent using whatever legal structure commits
    the least guaranteed money. This is the `--aav` CLI flag from the design
    doc.

- build_from_template(template_name, player, team, rules)
    Apply a named template ("the Bonilla", "the Cliff", "the Trojan Horse")
    that combines specific vectors.
"""

from __future__ import annotations

from contract import ContractOffer
from rules_profile import RulesProfile


def build_min_guaranteed(
    player_id: str,
    player_name: str,
    target_aav: int,
    years: int,
    rules: RulesProfile,
) -> ContractOffer:
    """Stub. Will distribute `target_aav` across `years` using the structure
    that minimizes guaranteed money against `rules`."""
    raise NotImplementedError(
        "builder.build_min_guaranteed not yet implemented — see "
        "vContracts_design.md §'CLI / Interface Ideas'"
    )
