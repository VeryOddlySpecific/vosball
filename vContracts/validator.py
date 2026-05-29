#!/usr/bin/env python3
"""
validator.py — Check a ContractOffer against a RulesProfile.

The validator is what makes the rules profile load-bearing: without it, the
profile is decorative documentation. Every builder output should pass through
validate() before being surfaced to the user.

A Violation describes *what* rule was broken and *where* (year index or
option index); it does not editorialize about whether the structure is
strategically wise — that's the builder's job.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from contract import ContractOffer, OptionType
from rules_profile import RulesProfile


Severity = Literal["error", "warn"]


@dataclass(frozen=True)
class Violation:
    rule: str          # short identifier — "max_team_options", "deferrals_disallowed"
    severity: Severity
    message: str       # human-readable explanation
    location: str = "" # e.g. "year[3]", "option[1]" — empty for whole-contract


def validate(offer: ContractOffer, rules: RulesProfile) -> list[Violation]:
    violations: list[Violation] = []
    violations.extend(_check_length(offer, rules))
    violations.extend(_check_options(offer, rules))
    violations.extend(_check_shape(offer, rules))
    violations.extend(_check_disallowed_features(offer, rules))
    return violations


def _check_length(offer: ContractOffer, rules: RulesProfile) -> list[Violation]:
    if offer.total_years > rules.max_contract_years:
        return [Violation(
            rule="max_contract_years",
            severity="error",
            message=(
                f"Contract length {offer.total_years} exceeds league max "
                f"{rules.max_contract_years}"
            ),
        )]
    return []


def _check_options(offer: ContractOffer, rules: RulesProfile) -> list[Violation]:
    out: list[Violation] = []
    team_options = [o for o in offer.options if o.type == OptionType.TEAM]
    if (rules.options.max_team_options is not None
            and len(team_options) > rules.options.max_team_options):
        out.append(Violation(
            rule="max_team_options",
            severity="error",
            message=(
                f"Contract has {len(team_options)} team options; league "
                f"allows max {rules.options.max_team_options}"
            ),
        ))

    if not rules.options.team_option_can_precede_player_option:
        # Walk options in order; once we've seen a team option, a later
        # player option is illegal.
        seen_team = False
        for i, opt in enumerate(offer.options):
            if opt.type == OptionType.TEAM:
                seen_team = True
            elif opt.type == OptionType.PLAYER and seen_team:
                out.append(Violation(
                    rule="team_option_precedes_player_option",
                    severity="error",
                    message=(
                        "Team option appears before a player option in the "
                        "option sequence; league rule forbids this ordering"
                    ),
                    location=f"option[{i}]",
                ))

    if rules.options.buyout_pct_range is not None:
        lo, hi = rules.options.buyout_pct_range
        for i, opt in enumerate(offer.options):
            if opt.salary <= 0:
                continue
            pct = opt.buyout / opt.salary
            if pct < lo or pct > hi:
                out.append(Violation(
                    rule="buyout_pct_range",
                    severity="error",
                    message=(
                        f"Buyout {pct:.0%} of option salary is outside "
                        f"allowed range [{lo:.0%}, {hi:.0%}]"
                    ),
                    location=f"option[{i}]",
                ))
    return out


def _check_shape(offer: ContractOffer, rules: RulesProfile) -> list[Violation]:
    out: list[Violation] = []
    salaries = [y.salary for y in offer.years]
    if not salaries:
        return out

    if rules.shape.max_high_low_ratio is not None:
        lo = min(s for s in salaries if s > 0) if any(s > 0 for s in salaries) else 0
        hi = max(salaries)
        if lo > 0 and hi / lo > rules.shape.max_high_low_ratio:
            out.append(Violation(
                rule="max_high_low_ratio",
                severity="error",
                message=(
                    f"Salary spread {hi}:{lo} exceeds league flat-shape "
                    f"ratio {rules.shape.max_high_low_ratio}"
                ),
            ))

    if rules.shape.max_yoy_jump is not None:
        for i in range(1, len(salaries)):
            jump = abs(salaries[i] - salaries[i - 1])
            if jump > rules.shape.max_yoy_jump:
                out.append(Violation(
                    rule="max_yoy_jump",
                    severity="error",
                    message=(
                        f"Year-over-year jump {jump} exceeds league cap "
                        f"{rules.shape.max_yoy_jump}"
                    ),
                    location=f"year[{i}]",
                ))
    return out


def _check_disallowed_features(
    offer: ContractOffer, rules: RulesProfile
) -> list[Violation]:
    """Catch features the offer uses that the league doesn't permit. These
    aren't expressible on the current ContractOffer (no deferrals or bonus
    fields exist yet) but the hook is here so adding those fields later
    surfaces the rule check immediately."""
    out: list[Violation] = []
    # Placeholder — extend when ContractOffer gains deferrals/bonuses.
    if not rules.options.team_option_can_precede_player_option:
        # No additional check needed here; the ordering check in _check_options
        # already covers it. This branch exists as a reminder that
        # disallowed-feature checks live in this function as the model grows.
        pass
    return out
