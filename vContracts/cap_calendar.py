#!/usr/bin/env python3
"""
cap_calendar.py — Forward-looking team cap ledger for multi-contract planning.

A single back-loaded contract is a tool. Three back-loaded contracts that all
balloon in the same year is a self-inflicted cap crisis. The cap calendar
prevents the second case by tracking what's already committed across future
seasons before the builder commits another balloon to the pile.

NOT YET IMPLEMENTED. See vContracts_design.md §"Rolling Multi-Year Cap
Planning" for the spec.

Planned shape
-------------
- CapCalendar: dict-like {year: YearLedger}
- YearLedger: hard_committed, balloon_weight, probable_options, buyout_outs
- CapCalendar.feasible(offer, start_year, balloon_ceiling) -> bool
- CapCalendar.add(offer, start_year) -> CapCalendar (returns updated copy)
- CapCalendar.from_team(team_id, horizon_years) -> CapCalendar

The signing-window planner (which player to sign in which offseason, with
which back-end shape) lives here too — it's a search over (player_order,
contract_shapes) that maximizes headline TCV under the constraint that no
future year exceeds the balloon ceiling.
"""

from __future__ import annotations

from contract import ContractOffer


def feasible(offer: ContractOffer, start_year: int, balloon_ceiling: int) -> bool:
    """Stub. Will check whether adding `offer` starting in `start_year` keeps
    every future year's balloon weight below `balloon_ceiling`."""
    raise NotImplementedError(
        "cap_calendar.feasible not yet implemented — see vContracts_design.md "
        "§'Rolling Multi-Year Cap Planning'"
    )
