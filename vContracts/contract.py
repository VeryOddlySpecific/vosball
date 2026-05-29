#!/usr/bin/env python3
"""
contract.py — Canonical contract data model for vContracts.

A ContractOffer is the unit of work for every other module: builders produce
it, validators check it against a RulesProfile, the cap calendar schedules it
against the team's multi-year ledger.

Money is integer dollars throughout. Years are 1-indexed for human-facing
display but the internal `years` list is 0-indexed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class OptionType(str, Enum):
    TEAM = "team"          # team decides whether to exercise
    PLAYER = "player"      # player decides — generally hostile to the team
    VESTING = "vesting"    # auto-exercises on a performance trigger
    MUTUAL = "mutual"      # both sides must agree


@dataclass(frozen=True)
class Option:
    """An option year attached to a contract."""
    type: OptionType
    salary: int
    buyout: int = 0
    # Free-form trigger description for vesting options (e.g. "500 PA in Y4").
    # Modeling the probability of the trigger firing is a builder concern,
    # not a data-model concern.
    vesting_trigger: Optional[str] = None


@dataclass(frozen=True)
class Incentive:
    """A performance bonus. Probability is what the *team* believes, not the
    headline number quoted to the agent."""
    description: str           # e.g. "MVP", "200 IP", "Silver Slugger"
    amount: int
    # Team-side probability estimate the trigger is hit. The headline TCV the
    # agent sees treats this as 1.0; expected cost uses this value.
    estimated_probability: float = 0.0


@dataclass(frozen=True)
class ContractYear:
    """One guaranteed year of a contract."""
    salary: int
    incentives: tuple[Incentive, ...] = ()


@dataclass(frozen=True)
class ContractOffer:
    """A complete contract structure.

    `years` is the guaranteed portion. Options follow the guaranteed years
    in order; the contract's total length is len(years) + len(options).
    """
    player_id: str
    player_name: str
    years: tuple[ContractYear, ...]
    options: tuple[Option, ...] = ()
    no_trade_clause: bool = False
    # The AAV target the agent was sold on. Useful for the validator to check
    # the offer actually delivers what was negotiated, and for the builder to
    # confirm the headline number it advertised.
    target_aav: Optional[int] = None

    # ------------------------------------------------------------------
    # Derived views — keep these as methods, not stored fields, so the
    # dataclass remains the single source of truth.
    # ------------------------------------------------------------------

    @property
    def guaranteed_years(self) -> int:
        return len(self.years)

    @property
    def total_years(self) -> int:
        return len(self.years) + len(self.options)

    @property
    def guaranteed_total(self) -> int:
        """Sum of base salaries on the guaranteed years only."""
        return sum(y.salary for y in self.years)

    @property
    def headline_tcv(self) -> int:
        """Total contract value as quoted to the agent: every year's salary,
        every option salary, and every incentive at face value."""
        guaranteed = self.guaranteed_total
        option_salaries = sum(o.salary for o in self.options)
        all_incentives = sum(
            inc.amount for y in self.years for inc in y.incentives
        )
        return guaranteed + option_salaries + all_incentives

    def expected_cost(
        self,
        team_option_exercise_prob: float = 1.0,
        player_option_exercise_prob: float = 0.5,
    ) -> float:
        """Probability-weighted true cost.

        Defaults reflect the design doc's posture: team options are exercised
        when convenient (so assume 100% when modeling worst case, override
        with a lower estimate for specific offers); player options fire when
        the player has had a good year, i.e. when the team would rather walk.
        """
        cost = float(self.guaranteed_total)
        cost += sum(
            inc.amount * inc.estimated_probability
            for y in self.years
            for inc in y.incentives
        )
        for opt in self.options:
            if opt.type == OptionType.TEAM:
                p = team_option_exercise_prob
            elif opt.type == OptionType.PLAYER:
                p = player_option_exercise_prob
            elif opt.type == OptionType.VESTING:
                # Without a probability model, treat as 50/50 — builder should
                # override this when it has a real estimate.
                p = 0.5
            else:  # MUTUAL — generally won't fire if either side has a reason
                p = 0.25
            cost += p * opt.salary + (1 - p) * opt.buyout
        return cost
