#!/usr/bin/env python3
"""
rules_profile.py — League rule environment for vContracts.

A RulesProfile is the swappable config object that defines what contract
structures are legal in a given league. The builder and validator consume
this; the design doc treats the rulebook as the only constraint.

Conventions
-----------
- Money is integer dollars throughout (no float drift on cap math).
- Booleans default to the most permissive setting; restrictions are opt-in
  so future leagues with stricter rules are explicit, not silent.

Factories
---------
- sdmb()                — confirmed rule state as of 2026-05-15
- sdmb_post_option_rule() — same, but with the pending 6-3 option restriction
                            applied. Flip the import the day the vote lands.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class CapBasis(str, Enum):
    ANNUAL = "annual"   # actual annual salary hits the cap in that year (SDMB)
    AAV = "aav"         # total / years, charged evenly to each year


@dataclass(frozen=True)
class OptionRules:
    """How team and player options are constrained."""
    # Pending 6-3 SDMB rule: cap on the number of *team* options per contract.
    # None = unlimited (current SDMB state). 1 = the proposed restriction.
    max_team_options: Optional[int] = None
    # Pending 6-3 SDMB rule: a team option cannot precede a player option.
    # False = unrestricted (current). True = restricted (post-vote).
    team_option_can_precede_player_option: bool = True
    # Buyout structure — unconstrained in SDMB. Tuple = (min_pct, max_pct) of
    # the option-year salary; None = no rule.
    buyout_pct_range: Optional[tuple[float, float]] = None


@dataclass(frozen=True)
class ShapeRules:
    """Year-over-year salary shape constraints."""
    # Max ratio of highest annual salary to lowest. None = no flat-shape rule.
    # The SDMB flat-shape proposal would have set this to ~2.0; it was defeated.
    max_high_low_ratio: Optional[float] = None
    # Max absolute jump between consecutive years. None = no jump cap.
    max_yoy_jump: Optional[int] = None


@dataclass(frozen=True)
class TradeRules:
    """Trade-side constraints. SDMB has none."""
    salary_retention_allowed: bool = True
    max_retention_pct: Optional[float] = None  # None = up to 100%
    no_trade_clauses_allowed: bool = True


@dataclass(frozen=True)
class RulesProfile:
    """Complete rule environment for one league at one point in time."""
    name: str
    cap_basis: CapBasis
    max_contract_years: int
    deferrals_allowed: bool
    signing_bonuses_allowed: bool
    options: OptionRules = field(default_factory=OptionRules)
    shape: ShapeRules = field(default_factory=ShapeRules)
    trade: TradeRules = field(default_factory=TradeRules)
    # Free-form notes; useful for documenting the political context of a rule
    # (e.g. "flat-shape rule defeated 2026-05; back-loading openly accepted").
    notes: str = ""


# ---------------------------------------------------------------------------
# League factories
# ---------------------------------------------------------------------------

def sdmb() -> RulesProfile:
    """SDMB rule state confirmed 2026-05-15. Pending option rule NOT applied."""
    return RulesProfile(
        name="SDMB (pre-option-rule)",
        cap_basis=CapBasis.ANNUAL,
        max_contract_years=10,  # assumed; confirm before relying on this
        deferrals_allowed=False,
        signing_bonuses_allowed=False,
        options=OptionRules(
            max_team_options=None,
            team_option_can_precede_player_option=True,
            buyout_pct_range=None,
        ),
        shape=ShapeRules(
            max_high_low_ratio=None,   # flat-shape rule defeated
            max_yoy_jump=None,
        ),
        trade=TradeRules(),
        notes=(
            "Budget cap, annual basis. Flat-shape rule defeated; back-loading "
            "is fully legal. Pending 6-3 rule on option stacking — see "
            "sdmb_post_option_rule()."
        ),
    )


def sdmb_post_option_rule() -> RulesProfile:
    """SDMB state IF the pending 6-3 option rule passes."""
    base = sdmb()
    return RulesProfile(
        name="SDMB (post-option-rule)",
        cap_basis=base.cap_basis,
        max_contract_years=base.max_contract_years,
        deferrals_allowed=base.deferrals_allowed,
        signing_bonuses_allowed=base.signing_bonuses_allowed,
        options=OptionRules(
            max_team_options=1,
            team_option_can_precede_player_option=False,
            buyout_pct_range=None,
        ),
        shape=base.shape,
        trade=base.trade,
        notes=(
            "Same as sdmb() but with the option-stacking restriction applied. "
            "Default contract template (stacked team options) is no longer "
            "available; builder must compensate with longer guaranteed tails "
            "and steeper balloons."
        ),
    )
