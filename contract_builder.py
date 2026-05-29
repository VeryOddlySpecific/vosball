#!/usr/bin/env python3
"""
Contract Builder: Compute contract structures that minimize guaranteed money
while satisfying the 2x rule, option constraints, incentive caps, and hitting
an exact total maximum value.

Rules:
1. 2x Rule: For multi-year contracts where annual values >= 10M, the highest
   annual value must be <= 2 * the lowest annual value.
2. Option: Final year can be a team option with buyout >= buyout_pct * option_year_value.
3. Incentives: Each incentive <= incentive_cap_pct * highest_annual_value.
4. Total Max Value: Must equal exactly the provided target, where:
   total_max = guaranteed_total + (option_year if exercised) + (all incentives if achieved)
"""

import argparse
from decimal import Decimal, ROUND_DOWN, ROUND_UP
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass


@dataclass
class ContractParams:
    """Parameters for contract construction."""
    years: int
    total_max_value: int
    incentives: int
    incentive_cap_pct: float
    use_option: bool
    option_year_value: Optional[int]
    buyout_pct: float
    rounding: int
    incentives_per_year: bool
    min_annual_value: int
    threshold_ip: Optional[float]
    apply_2x: bool = True


@dataclass
class YearData:
    """Data for a single contract year."""
    year: int
    base_salary: int
    is_option: bool
    buyout: Optional[int]
    guaranteed: int
    max_incentives: List[int]


@dataclass
class ContractResult:
    """Result of contract construction."""
    years: List[YearData]
    lowest_annual_value: int
    highest_annual_value: int
    total_guaranteed: int
    total_max_incentives: int
    total_max_value: int
    threshold_ip: Optional[float]
    warnings: List[str]


def parse_args() -> ContractParams:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description='Build contract structures that minimize guaranteed money.'
    )
    parser.add_argument(
        '--years', '-y',
        type=int,
        required=True,
        help='Number of contract years'
    )
    parser.add_argument(
        '--total-max-value', '-V',
        type=int,
        required=True,
        help='Total maximum contract value in dollars'
    )
    parser.add_argument(
        '--incentives', '-m',
        type=int,
        default=4,
        help='Number of incentive categories (default: 4)'
    )
    parser.add_argument(
        '--incentive-cap-pct', '-p',
        type=float,
        default=0.10,
        help='Incentive cap as fraction of highest annual value (default: 0.10)'
    )
    parser.add_argument(
        '--use-option',
        action='store_true',
        default=False,
        help='Include team option in final year'
    )
    parser.add_argument(
        '--option-year-value', '-O',
        type=int,
        default=None,
        help='Option year stated value in dollars (if not provided, compute optimal)'
    )
    parser.add_argument(
        '--buyout-pct',
        type=float,
        default=0.25,
        help='Minimum buyout fraction of option year (default: 0.25)'
    )
    parser.add_argument(
        '--rounding',
        type=int,
        default=1,
        help='Salaries and incentives should be multiples of this (default: 1)'
    )
    parser.add_argument(
        '--incentives-per-year',
        action='store_true',
        default=True,
        help='Incentives apply each year (default: True)'
    )
    parser.add_argument(
        '--no-incentives-per-year',
        dest='incentives_per_year',
        action='store_false',
        help='Incentives are one-time total across contract'
    )
    parser.add_argument(
        '--min-annual-value',
        type=int,
        default=0,
        help='Optional floor for any annual salary value (default: 0)'
    )
    parser.add_argument(
        '--threshold-ip',
        type=float,
        default=None,
        help='IP incentive threshold (store/print only)'
    )
    parser.add_argument(
        '--apply-2x',
        action='store_true',
        default=True,
        help='Apply 2x rule when annual values >= 10M (default: True)'
    )
    parser.add_argument(
        '--no-apply-2x',
        dest='apply_2x',
        action='store_false',
        help='Do not apply 2x rule'
    )

    args = parser.parse_args()
    return ContractParams(
        years=args.years,
        total_max_value=args.total_max_value,
        incentives=args.incentives,
        incentive_cap_pct=args.incentive_cap_pct,
        use_option=args.use_option,
        option_year_value=args.option_year_value,
        buyout_pct=args.buyout_pct,
        rounding=args.rounding,
        incentives_per_year=args.incentives_per_year,
        min_annual_value=args.min_annual_value,
        threshold_ip=args.threshold_ip,
        apply_2x=args.apply_2x
    )


def floor_to_rounding(x: int, rounding: int) -> int:
    """Round down to nearest multiple of rounding."""
    if rounding <= 0:
        return x
    return int(Decimal(x) // Decimal(rounding) * Decimal(rounding))


def ceil_to_rounding(x: int, rounding: int) -> int:
    """Round up to nearest multiple of rounding."""
    if rounding <= 0:
        return x
    return int((Decimal(x) + Decimal(rounding) - 1) // Decimal(rounding) * Decimal(rounding))


def round_to_rounding(x: int, rounding: int) -> int:
    """Round to nearest multiple of rounding."""
    if rounding <= 0:
        return x
    return int(round(Decimal(x) / Decimal(rounding)) * Decimal(rounding))


def compute_max_incentive_per_incentive(highest_annual: int, incentive_cap_pct: float, rounding: int) -> int:
    """Compute maximum dollar amount per incentive."""
    max_incentive = floor_to_rounding(int(highest_annual * incentive_cap_pct), rounding)
    return max_incentive


def compute_total_incentives(
    years: int,
    incentives: int,
    max_incentive_per: int,
    incentives_per_year: bool
) -> int:
    """Compute total maximum incentive value across all years."""
    if incentives_per_year:
        return years * incentives * max_incentive_per
    else:
        return incentives * max_incentive_per


def allocate_salaries_minimal_guaranteed(
    base_needed: int,
    years: int,
    use_option: bool,
    option_year_value: Optional[int],
    buyout_pct: float,
    lowest: int,
    highest: int,
    rounding: int,
    min_annual_value: int
) -> Tuple[List[int], Optional[int], Optional[int], List[str]]:
    """
    Allocate base_needed into salaries to minimize guaranteed money.
    
    Returns:
        (guaranteed_salaries, option_year_value, buyout, warnings)
    """
    warnings = []
    
    if use_option:
        # Years 1..N-1 are guaranteed, year N is option
        guaranteed_years = years - 1
        
        # Determine option year value O
        if option_year_value is not None:
            O = round_to_rounding(option_year_value, rounding)
            if O < min_annual_value:
                warnings.append(f"Option year value {O} is below min_annual_value {min_annual_value}")
        else:
            # Set O = L to minimize guaranteed (option is lowest)
            O = lowest
        
        # Compute buyout B
        min_buyout = ceil_to_rounding(int(O * buyout_pct), rounding)
        buyout = min_buyout
        
        # base_needed includes O (for max value calculation), but guaranteed only includes buyout
        # So: base_needed = sum(guaranteed_salaries) + O
        # Guaranteed = sum(guaranteed_salaries) + buyout
        # We want to minimize: sum(guaranteed_salaries) + buyout
        # Subject to: sum(guaranteed_salaries) + O = base_needed
        # So: sum(guaranteed_salaries) = base_needed - O
        guaranteed_salary_total = base_needed - O
        
        if guaranteed_salary_total < 0:
            warnings.append(f"Cannot allocate: base_needed={base_needed}, O={O} (need O <= base_needed)")
            return [], None, None, warnings
        
        # Allocate guaranteed_salary_total across guaranteed_years
        # Minimize guaranteed means keep salaries as low as possible
        # But we need at least one year at H to maximize incentive cap
        # And all years must be between L and H
        
        # Strategy: Set all years to L, then distribute remainder
        # But we need at least one at H if H > L
        guaranteed_salaries = [lowest] * guaranteed_years
        
        # Check if we need to raise one to H
        current_sum = sum(guaranteed_salaries)
        remainder = guaranteed_salary_total - current_sum
        
        if remainder < 0:
            warnings.append(f"Cannot satisfy constraints: need {guaranteed_salary_total} but minimum is {current_sum}")
            return [], None, None, warnings
        
        # Distribute remainder while keeping all between L and H
        # Prefer to raise one year to H first (for incentive cap), then distribute rest
        if highest > lowest and remainder > 0:
            # Raise one year to H
            raise_amount = highest - lowest
            if remainder >= raise_amount:
                guaranteed_salaries[0] = highest
                remainder -= raise_amount
            else:
                # Can't fully raise to H, just add remainder to first year
                guaranteed_salaries[0] = lowest + remainder
                remainder = 0
        
        # Distribute remaining remainder across years (front-loading: earlier years get more)
        idx = 0
        while remainder > 0 and idx < guaranteed_years:
            max_add = highest - guaranteed_salaries[idx]
            add = min(remainder, max_add)
            guaranteed_salaries[idx] += add
            remainder -= add
            idx += 1
        
        # Round all to rounding first
        guaranteed_salaries = [round_to_rounding(s, rounding) for s in guaranteed_salaries]
        
        # Adjust to hit exact guaranteed_salary_total after rounding
        current_sum = sum(guaranteed_salaries)
        rounding_diff = guaranteed_salary_total - current_sum
        
        if rounding_diff != 0:
            # Distribute rounding_diff to hit exact value
            if rounding_diff > 0:
                # Add to salaries (prefer earlier years)
                idx = 0
                while rounding_diff > 0 and idx < guaranteed_years:
                    max_add = highest - guaranteed_salaries[idx]
                    add = min(rounding_diff, max_add)
                    if add > 0:
                        guaranteed_salaries[idx] = round_to_rounding(guaranteed_salaries[idx] + add, rounding)
                        rounding_diff -= add
                    idx += 1
            else:
                # Subtract from salaries (prefer later years)
                rounding_diff = abs(rounding_diff)
                idx = guaranteed_years - 1
                while rounding_diff > 0 and idx >= 0:
                    max_sub = guaranteed_salaries[idx] - max(lowest, min_annual_value)
                    sub = min(rounding_diff, max_sub)
                    if sub > 0:
                        guaranteed_salaries[idx] = round_to_rounding(guaranteed_salaries[idx] - sub, rounding)
                        rounding_diff -= sub
                    idx -= 1
            
            if rounding_diff != 0:
                warnings.append(f"Could not fully allocate {rounding_diff} dollars after rounding (may need to adjust L/H)")
        
        # Verify constraints
        for s in guaranteed_salaries:
            if s < min_annual_value:
                warnings.append(f"Salary {s} is below min_annual_value {min_annual_value}")
            if s < lowest or s > highest:
                warnings.append(f"Salary {s} violates L={lowest} <= salary <= H={highest}")
        
        return guaranteed_salaries, O, buyout, warnings
    
    else:
        # No option: all years are guaranteed
        # base_needed = sum(all salaries)
        # We want to minimize guaranteed, but guaranteed = base_needed (fixed)
        # So we just need to distribute base_needed across years
        
        guaranteed_salaries = [lowest] * years
        current_sum = sum(guaranteed_salaries)
        remainder = base_needed - current_sum
        
        if remainder < 0:
            warnings.append(f"Cannot satisfy constraints: need {base_needed} but minimum is {current_sum}")
            return [], None, None, warnings
        
        # Raise one year to H first (for incentive cap)
        if highest > lowest and remainder > 0:
            raise_amount = highest - lowest
            if remainder >= raise_amount:
                guaranteed_salaries[0] = highest
                remainder -= raise_amount
            else:
                guaranteed_salaries[0] = lowest + remainder
                remainder = 0
        
        # Distribute remainder (front-loading)
        idx = 0
        while remainder > 0 and idx < years:
            max_add = highest - guaranteed_salaries[idx]
            add = min(remainder, max_add)
            guaranteed_salaries[idx] += add
            remainder -= add
            idx += 1
        
        # Round all to rounding first
        guaranteed_salaries = [round_to_rounding(s, rounding) for s in guaranteed_salaries]
        
        # Adjust to hit exact base_needed after rounding
        current_sum = sum(guaranteed_salaries)
        rounding_diff = base_needed - current_sum
        
        if rounding_diff != 0:
            # Distribute rounding_diff to hit exact value
            if rounding_diff > 0:
                # Add to salaries (prefer earlier years)
                idx = 0
                while rounding_diff > 0 and idx < years:
                    max_add = highest - guaranteed_salaries[idx]
                    add = min(rounding_diff, max_add)
                    if add > 0:
                        guaranteed_salaries[idx] = round_to_rounding(guaranteed_salaries[idx] + add, rounding)
                        rounding_diff -= add
                    idx += 1
            else:
                # Subtract from salaries (prefer later years)
                rounding_diff = abs(rounding_diff)
                idx = years - 1
                while rounding_diff > 0 and idx >= 0:
                    max_sub = guaranteed_salaries[idx] - max(lowest, min_annual_value)
                    sub = min(rounding_diff, max_sub)
                    if sub > 0:
                        guaranteed_salaries[idx] = round_to_rounding(guaranteed_salaries[idx] - sub, rounding)
                        rounding_diff -= sub
                    idx -= 1
            
            if rounding_diff != 0:
                warnings.append(f"Could not fully allocate {rounding_diff} dollars after rounding")
        
        return guaranteed_salaries, None, None, warnings


def solve_for_exact_value(
    params: ContractParams,
    l_candidate: int,
    h_candidate: int
) -> Optional[Dict]:
    """
    Try to solve for exact total_max_value given L and H.
    Returns solution dict or None if infeasible.
    """
    # Compute max incentive per incentive
    max_incentive_per = compute_max_incentive_per_incentive(
        h_candidate, params.incentive_cap_pct, params.rounding
    )
    
    if max_incentive_per == 0 and params.incentives > 0:
        return None
    
    # Compute total incentive value
    incentive_total = compute_total_incentives(
        params.years,
        params.incentives,
        max_incentive_per,
        params.incentives_per_year
    )
    
    # Compute base needed (guaranteed salaries + option year value if used)
    base_needed = params.total_max_value - incentive_total
    
    if base_needed < 0:
        return None
    
    # Allocate salaries
    guaranteed_salaries, option_year_value, buyout, alloc_warnings = \
        allocate_salaries_minimal_guaranteed(
            base_needed,
            params.years,
            params.use_option,
            params.option_year_value,
            params.buyout_pct,
            l_candidate,
            h_candidate,
            params.rounding,
            params.min_annual_value
        )
    
    if not guaranteed_salaries:
        return None
    
    # Compute actual annual values (including option year value, not buyout)
    annual_values = list(guaranteed_salaries)
    if params.use_option:
        annual_values.append(option_year_value)
    
    actual_lowest = min(annual_values)
    actual_highest = max(annual_values)
    
    # Check if 2x rule should apply (any annual >= 10M)
    should_apply_2x = params.apply_2x and any(av >= 10_000_000 for av in annual_values)
    
    if should_apply_2x and actual_highest > 2 * actual_lowest:
        return None
    
    # Recompute with actual H (which may have changed)
    actual_max_incentive_per = compute_max_incentive_per_incentive(
        actual_highest, params.incentive_cap_pct, params.rounding
    )
    actual_incentive_total = compute_total_incentives(
        params.years,
        params.incentives,
        actual_max_incentive_per,
        params.incentives_per_year
    )
    
    # Compute total guaranteed
    if params.use_option:
        total_guaranteed = sum(guaranteed_salaries) + buyout
    else:
        total_guaranteed = sum(guaranteed_salaries)
    
    # Iterative refinement to hit exact value
    # This is needed because changing salaries changes H, which changes incentive cap
    max_iterations = 20
    for iteration in range(max_iterations):
        # Compute actual annual values
        annual_values = list(guaranteed_salaries)
        if params.use_option:
            annual_values.append(option_year_value)
        actual_lowest = min(annual_values)
        actual_highest = max(annual_values)
        
        # Recompute incentive with current H
        actual_max_incentive_per = compute_max_incentive_per_incentive(
            actual_highest, params.incentive_cap_pct, params.rounding
        )
        actual_incentive_total = compute_total_incentives(
            params.years,
            params.incentives,
            actual_max_incentive_per,
            params.incentives_per_year
        )
        
        # Compute total guaranteed
        if params.use_option:
            total_guaranteed = sum(guaranteed_salaries) + buyout
        else:
            total_guaranteed = sum(guaranteed_salaries)
        
        # Compute actual max value
        option_contribution = option_year_value if params.use_option else 0
        computed_max_value = total_guaranteed - (buyout if params.use_option else 0) + option_contribution + actual_incentive_total
        
        diff = params.total_max_value - computed_max_value
        
        if abs(diff) <= params.rounding:
            # Close enough
            break
        
        # Adjust to reduce diff
        if abs(diff) > 0:
            adjustment_made = False
            
            if diff > 0:
                # Need to add diff - add to salaries (increases H, which increases incentives)
                remaining = diff
                # Add to highest salary first to maximize H
                for idx in range(len(guaranteed_salaries)):
                    if remaining <= 0:
                        break
                    # Allow going slightly above h_candidate if needed to hit exact value
                    max_add = (h_candidate * 2) - guaranteed_salaries[idx]  # Allow some flexibility
                    add = min(remaining, max_add)
                    if add > 0:
                        guaranteed_salaries[idx] = round_to_rounding(guaranteed_salaries[idx] + add, params.rounding)
                        remaining -= add
                        adjustment_made = True
                
                if remaining > 0 and params.use_option:
                    # Add to option year value
                    option_year_value = round_to_rounding(option_year_value + remaining, params.rounding)
                    adjustment_made = True
            else:
                # Need to subtract diff - subtract from salaries (may decrease H)
                remaining = abs(diff)
                # Subtract from lowest salaries first
                for idx in range(len(guaranteed_salaries) - 1, -1, -1):
                    if remaining <= 0:
                        break
                    max_sub = guaranteed_salaries[idx] - max(l_candidate, params.min_annual_value)
                    sub = min(remaining, max_sub)
                    if sub > 0:
                        guaranteed_salaries[idx] = round_to_rounding(guaranteed_salaries[idx] - sub, params.rounding)
                        remaining -= sub
                        adjustment_made = True
            
            if not adjustment_made:
                # Can't adjust further
                break
    
    # Final validation and computation
    annual_values = list(guaranteed_salaries)
    if params.use_option:
        annual_values.append(option_year_value)
    actual_lowest = min(annual_values)
    actual_highest = max(annual_values)
    
    should_apply_2x = params.apply_2x and any(av >= 10_000_000 for av in annual_values)
    if should_apply_2x and actual_highest > 2 * actual_lowest:
        return None
    
    # Recompute with final values
    actual_max_incentive_per = compute_max_incentive_per_incentive(
        actual_highest, params.incentive_cap_pct, params.rounding
    )
    actual_incentive_total = compute_total_incentives(
        params.years,
        params.incentives,
        actual_max_incentive_per,
        params.incentives_per_year
    )
    
    # Final total_guaranteed
    if params.use_option:
        total_guaranteed = sum(guaranteed_salaries) + buyout
    else:
        total_guaranteed = sum(guaranteed_salaries)
    
    # Verify final max value
    option_contribution = option_year_value if params.use_option else 0
    computed_max_value = total_guaranteed - (buyout if params.use_option else 0) + option_contribution + actual_incentive_total
    diff = params.total_max_value - computed_max_value
    
    if abs(diff) > params.rounding:
        alloc_warnings.append(f"Could not hit exact total_max_value: computed {computed_max_value}, target {params.total_max_value}, diff {diff}")
    
    return {
        'guaranteed_salaries': guaranteed_salaries,
        'option_year_value': option_year_value,
        'buyout': buyout,
        'lowest': actual_lowest,
        'highest': actual_highest,
        'max_incentive_per': actual_max_incentive_per,
        'total_guaranteed': total_guaranteed,
        'warnings': alloc_warnings
    }


def compute_feasible_l_bounds(params: ContractParams) -> Tuple[int, int]:
    """
    Compute feasible bounds for L (lowest annual value) based on constraints.
    Returns (l_lower, l_upper).
    """
    l_lower = max(params.min_annual_value, params.rounding)
    
    # Upper bound: rough estimate
    # For option: base_needed = total_max - incentives, and base_needed >= years * L
    # So: total_max - incentives >= years * L
    # With H = 2*L, max_incentive_per = 0.10 * 2*L = 0.20*L (roughly)
    # total_incentives = years * incentives * 0.20*L = 0.20*years*incentives*L
    # So: total_max - 0.20*years*incentives*L >= years * L
    # total_max >= years * L + 0.20*years*incentives*L = years * L * (1 + 0.20*incentives)
    # L <= total_max / (years * (1 + 0.20*incentives))
    
    if params.incentives_per_year:
        incentive_multiplier = params.years * params.incentives * params.incentive_cap_pct
    else:
        incentive_multiplier = params.incentives * params.incentive_cap_pct
    
    if params.apply_2x:
        # H = 2*L, so max_incentive_per = incentive_cap_pct * 2*L
        # total_incentives = incentive_multiplier * 2*L * incentive_cap_pct
        # Actually, let's be more precise
        # For H = 2*L: max_incentive_per = floor(incentive_cap_pct * 2*L / rounding) * rounding
        # This is approximately incentive_cap_pct * 2*L
        # total_incentives ≈ incentive_multiplier * incentive_cap_pct * 2*L
        # base_needed = total_max - total_incentives
        # For option: base_needed = sum(guaranteed) + option_year >= years * L
        # So: total_max - incentive_multiplier * incentive_cap_pct * 2*L >= years * L
        # total_max >= years * L + 2 * incentive_multiplier * incentive_cap_pct * L
        # total_max >= L * (years + 2 * incentive_multiplier * incentive_cap_pct)
        # L <= total_max / (years + 2 * incentive_multiplier * incentive_cap_pct)
        denominator = params.years + 2 * incentive_multiplier * params.incentive_cap_pct
    else:
        denominator = params.years
    
    l_upper = int((params.total_max_value * 2) // max(1, int(denominator)))  # Conservative upper bound
    
    return int(l_lower), int(l_upper)


def build_contract(params: ContractParams) -> ContractResult:
    """
    Build a contract structure that minimizes guaranteed money.
    
    Algorithm:
    1. Search over candidate L (lowest annual value) values
    2. For each L, set H = 2*L (if 2x rule applies) or higher
    3. Compute max incentives
    4. Compute base_needed = total_max_value - incentive_total
    5. Allocate base_needed into salaries with minimal guaranteed
    6. Choose solution with smallest guaranteed
    """
    warnings = []
    
    # Compute feasible bounds for L
    l_lower, l_upper = compute_feasible_l_bounds(params)
    
    best_result = None
    best_guaranteed = float('inf')
    
    # Try several initial guesses for L around the expected value
    # Try L values that would give us H around expected range
    initial_guesses = []
    
    # Guess 1: Based on rough calculation
    if params.apply_2x and params.incentives > 0:
        if params.incentives_per_year:
            total_inc_approx = params.years * params.incentives * params.incentive_cap_pct * 2
        else:
            total_inc_approx = params.incentives * params.incentive_cap_pct * 2
        
        if total_inc_approx > 0:
            l_guess1 = int(params.total_max_value / (params.years + total_inc_approx))
            l_guess1 = round_to_rounding(max(l_guess1, l_lower), params.rounding)
            l_guess1 = min(l_guess1, l_upper)
            initial_guesses.append(l_guess1)
    
    # Guess 2: Try L around total_max_value / (years * 2) to get H around total_max_value / years
    l_guess2 = round_to_rounding(int(params.total_max_value / (params.years * 2)), params.rounding)
    l_guess2 = max(l_guess2, l_lower)
    l_guess2 = min(l_guess2, l_upper)
    if l_guess2 not in initial_guesses:
        initial_guesses.append(l_guess2)
    
    # Try initial guesses
    for l_guess in initial_guesses:
        if params.apply_2x:
            h_guess = floor_to_rounding(2 * l_guess, params.rounding)
        else:
            h_guess = floor_to_rounding(10 * l_guess, params.rounding)
        
        if h_guess < l_guess:
            h_guess = l_guess
        
        solution = solve_for_exact_value(params, l_guess, h_guess)
        if solution:
            # Check if exact
            annual_vals = list(solution['guaranteed_salaries'])
            if params.use_option:
                annual_vals.append(solution['option_year_value'])
            actual_h = max(annual_vals)
            actual_max_inc_per = compute_max_incentive_per_incentive(
                actual_h, params.incentive_cap_pct, params.rounding
            )
            actual_inc_total = compute_total_incentives(
                params.years, params.incentives, actual_max_inc_per, params.incentives_per_year
            )
            opt_contrib = solution['option_year_value'] if params.use_option else 0
            computed_max = solution['total_guaranteed'] - (solution['buyout'] if params.use_option else 0) + opt_contrib + actual_inc_total
            if abs(computed_max - params.total_max_value) <= params.rounding:
                # Found exact solution
                best_guaranteed = solution['total_guaranteed']
                best_result = solution
                break
            elif solution['total_guaranteed'] < best_guaranteed:
                best_guaranteed = solution['total_guaranteed']
                best_result = solution
    
    # If option year value is fixed, try using it as L
    if params.use_option and params.option_year_value is not None:
        fixed_option_value = round_to_rounding(params.option_year_value, params.rounding)
        l_candidate = fixed_option_value
        if params.apply_2x:
            h_candidate = floor_to_rounding(2 * l_candidate, params.rounding)
        else:
            h_candidate = floor_to_rounding(10 * l_candidate, params.rounding)
        
        if h_candidate < l_candidate:
            h_candidate = l_candidate
        
        solution = solve_for_exact_value(params, l_candidate, h_candidate)
        if solution:
            if solution['total_guaranteed'] < best_guaranteed:
                best_guaranteed = solution['total_guaranteed']
                best_result = solution
    
    # Search over L values - use binary search approach
    # Start with coarse search, then refine
    step_sizes = [
        max(int(params.rounding * 10000), 100000),  # Very coarse
        max(int(params.rounding * 1000), 10000),    # Coarse
        max(int(params.rounding * 100), 1000),      # Fine
        max(int(params.rounding * 10), 100),        # Very fine
        int(params.rounding)                        # Finest
    ]
    
    for step in step_sizes:
        if best_result:
            # Check if we have exact solution
            # Recompute to check
            annual_vals = list(best_result['guaranteed_salaries'])
            if params.use_option:
                annual_vals.append(best_result['option_year_value'])
            actual_h = max(annual_vals)
            actual_max_inc_per = compute_max_incentive_per_incentive(
                actual_h, params.incentive_cap_pct, params.rounding
            )
            actual_inc_total = compute_total_incentives(
                params.years, params.incentives, actual_max_inc_per, params.incentives_per_year
            )
            opt_contrib = best_result['option_year_value'] if params.use_option else 0
            computed_max = best_result['total_guaranteed'] - (best_result['buyout'] if params.use_option else 0) + opt_contrib + actual_inc_total
            if abs(computed_max - params.total_max_value) <= params.rounding:
                # Found exact solution
                break
        
        # Search in current range - try a wider range
        search_range = min(l_upper - l_lower + 1, step * 500)  # Try up to 500 steps
        for l_candidate in range(l_lower, min(l_upper + 1, l_lower + search_range), step):
            # Set H based on 2x rule (if applicable)
            if params.apply_2x:
                h_candidate = floor_to_rounding(2 * l_candidate, params.rounding)
            else:
                h_candidate = floor_to_rounding(10 * l_candidate, params.rounding)
            
            if h_candidate < l_candidate:
                h_candidate = l_candidate
            
            solution = solve_for_exact_value(params, l_candidate, h_candidate)
            if solution:
                # Check if this is better
                if solution['total_guaranteed'] < best_guaranteed:
                    best_guaranteed = solution['total_guaranteed']
                    best_result = solution
                    
                    # If we found an exact solution, we can stop
                    # Recompute to verify
                    annual_vals = list(solution['guaranteed_salaries'])
                    if params.use_option:
                        annual_vals.append(solution['option_year_value'])
                    actual_h = max(annual_vals)
                    actual_max_inc_per = compute_max_incentive_per_incentive(
                        actual_h, params.incentive_cap_pct, params.rounding
                    )
                    actual_inc_total = compute_total_incentives(
                        params.years, params.incentives, actual_max_inc_per, params.incentives_per_year
                    )
                    opt_contrib = solution['option_year_value'] if params.use_option else 0
                    computed_max = solution['total_guaranteed'] - (solution['buyout'] if params.use_option else 0) + opt_contrib + actual_inc_total
                    if abs(computed_max - params.total_max_value) <= params.rounding:
                        break
    
    if best_result is None:
        raise ValueError(
            f"Cannot find feasible contract structure. "
            f"Try adjusting parameters or constraints. "
            f"Total max value: ${params.total_max_value:,}, Years: {params.years}, "
            f"Option: {params.use_option}"
        )
    
    # Build year data with front-loading (highest salaries in earlier years)
    years_data = []
    
    # Get guaranteed salaries and sort descending for front-loading
    guaranteed_salaries_sorted = sorted(best_result['guaranteed_salaries'], reverse=True)
    
    # Assign guaranteed years (years 1 to N-1 if option, or 1 to N if no option)
    guaranteed_years = params.years - 1 if params.use_option else params.years
    
    for i in range(guaranteed_years):
        year_num = i + 1
        salary = guaranteed_salaries_sorted[i]
        max_incentives = [best_result['max_incentive_per']] * params.incentives
        
        years_data.append(YearData(
            year=year_num,
            base_salary=salary,
            is_option=False,
            buyout=None,
            guaranteed=salary,
            max_incentives=max_incentives
        ))
    
    # Add option year if used
    if params.use_option:
        max_incentives = [best_result['max_incentive_per']] * params.incentives
        years_data.append(YearData(
            year=params.years,
            base_salary=best_result['option_year_value'],
            is_option=True,
            buyout=best_result['buyout'],
            guaranteed=best_result['buyout'],
            max_incentives=max_incentives
        ))
    
    # Compute final totals
    total_guaranteed = sum(y.guaranteed for y in years_data)
    total_max_incentives = sum(sum(y.max_incentives) for y in years_data)
    
    # Recompute total max value
    option_contribution = best_result['option_year_value'] if params.use_option else 0
    total_max_value = total_guaranteed - (best_result['buyout'] if params.use_option else 0) + option_contribution + total_max_incentives
    
    return ContractResult(
        years=years_data,
        lowest_annual_value=best_result['lowest'],
        highest_annual_value=best_result['highest'],
        total_guaranteed=total_guaranteed,
        total_max_incentives=total_max_incentives,
        total_max_value=total_max_value,
        threshold_ip=params.threshold_ip,
        warnings=best_result['warnings']
    )


def validate_contract(result: ContractResult, params: ContractParams) -> List[str]:
    """Validate contract against constraints and return warnings/errors."""
    errors = []
    warnings = []
    
    # Check 2x rule
    annual_values = [y.base_salary for y in result.years]
    should_apply_2x = params.apply_2x and any(av >= 10_000_000 for av in annual_values)
    
    if should_apply_2x:
        if result.highest_annual_value > 2 * result.lowest_annual_value:
            errors.append(
                f"2x rule violation: H={result.highest_annual_value} > 2*L={2*result.lowest_annual_value}"
            )
    
    # Check option buyout
    if params.use_option:
        option_year = next((y for y in result.years if y.is_option), None)
        if option_year:
            min_buyout = int(option_year.base_salary * params.buyout_pct)
            if option_year.buyout < min_buyout:
                errors.append(
                    f"Buyout {option_year.buyout} < required {min_buyout} "
                    f"({params.buyout_pct*100}% of {option_year.base_salary})"
                )
    
    # Check incentive caps
    for year_data in result.years:
        for incentive in year_data.max_incentives:
            max_allowed = int(result.highest_annual_value * params.incentive_cap_pct)
            if incentive > max_allowed:
                errors.append(
                    f"Incentive {incentive} in year {year_data.year} exceeds cap "
                    f"{max_allowed} ({params.incentive_cap_pct*100}% of {result.highest_annual_value})"
                )
    
    # Check total max value
    if abs(result.total_max_value - params.total_max_value) > params.rounding:
        warnings.append(
            f"Total max value {result.total_max_value} does not match target "
            f"{params.total_max_value} (difference: {result.total_max_value - params.total_max_value})"
        )
    
    # Check min annual value
    for year_data in result.years:
        if year_data.base_salary < params.min_annual_value:
            warnings.append(
                f"Year {year_data.year} salary {year_data.base_salary} < min_annual_value {params.min_annual_value}"
            )
    
    return errors + warnings


def format_output(result: ContractResult) -> str:
    """Format contract result for display."""
    lines = []
    lines.append("=" * 80)
    lines.append("CONTRACT STRUCTURE")
    lines.append("=" * 80)
    lines.append("")
    
    # Year table
    lines.append(f"{'Year':<6} {'Base Salary':<15} {'Option?':<10} {'Buyout':<15} {'Guaranteed':<15} {'Max Incentives':<20}")
    lines.append("-" * 80)
    
    for year_data in result.years:
        option_str = "Yes" if year_data.is_option else "No"
        buyout_str = f"${year_data.buyout:,}" if year_data.buyout else "N/A"
        incentives_str = f"${sum(year_data.max_incentives):,}" if year_data.max_incentives else "$0"
        
        lines.append(
            f"{year_data.year:<6} "
            f"${year_data.base_salary:,}{'':<4} "
            f"{option_str:<10} "
            f"{buyout_str:<15} "
            f"${year_data.guaranteed:,}{'':<4} "
            f"{incentives_str:<20}"
        )
    
    lines.append("")
    lines.append("-" * 80)
    lines.append("")
    
    # Summary
    lines.append("SUMMARY:")
    lines.append(f"  Lowest Annual Value (L):  ${result.lowest_annual_value:,}")
    lines.append(f"  Highest Annual Value (H): ${result.highest_annual_value:,}")
    
    if result.highest_annual_value > 0:
        ratio = result.highest_annual_value / result.lowest_annual_value
        lines.append(f"  H/L Ratio: {ratio:.2f}x")
        if ratio <= 2.0:
            lines.append(f"  [OK] 2x Rule: Satisfied (H <= 2*L)")
        else:
            lines.append(f"  [X] 2x Rule: Violated (H > 2*L)")
    
    lines.append("")
    lines.append(f"  Total Guaranteed Money: ${result.total_guaranteed:,}")
    lines.append(f"  Total Max Incentives:    ${result.total_max_incentives:,}")
    lines.append(f"  Total Max Value:        ${result.total_max_value:,}")
    
    if result.threshold_ip is not None:
        lines.append(f"  IP Threshold:          {result.threshold_ip}")
    
    lines.append("")
    
    if result.warnings:
        lines.append("WARNINGS:")
        for warning in result.warnings:
            lines.append(f"  [WARNING] {warning}")
        lines.append("")
    
    return "\n".join(lines)


def main():
    """Main entry point."""
    params = parse_args()
    
    try:
        result = build_contract(params)
        validation_issues = validate_contract(result, params)
        
        if validation_issues:
            result.warnings.extend(validation_issues)
        
        output = format_output(result)
        print(output)
        
        # Exit with error code if there are critical issues
        errors = [w for w in validation_issues if "violation" in w.lower() or "exceeds" in w.lower()]
        if errors:
            return 1
        
        return 0
    
    except ValueError as e:
        print(f"ERROR: {e}", file=__import__('sys').stderr)
        return 1
    except Exception as e:
        print(f"UNEXPECTED ERROR: {e}", file=__import__('sys').stderr)
        import traceback
        traceback.print_exc()
        return 1


if __name__ == '__main__':
    # Run self-checks
    import sys
    
    # Test 1: Simple 4-year contract with option
    print("Running self-checks...", file=sys.stderr)
    
    # Test basic functions
    assert floor_to_rounding(12345, 1000) == 12000
    assert ceil_to_rounding(12345, 1000) == 13000
    assert round_to_rounding(12345, 1000) == 12000
    
    print("Self-checks passed.", file=sys.stderr)
    
    sys.exit(main())

