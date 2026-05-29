#!/usr/bin/env python3
"""
Baseline draft pick value calculator.
Uses power-law decay by round; each round has base value with ±15% min/max.
"""
import argparse


# k interpolates linearly: round 1 -> k_start, round num_rounds -> k_end
K_START = 0.8
K_END = 2.5


def _k_for_round(round_num, num_rounds):
    """Linear interpolation: k = K_START at round 1, k = K_END at round num_rounds."""
    if num_rounds <= 1:
        return K_START
    t = (round_num - 1) / (num_rounds - 1)  # 0 at round 1, 1 at last round
    return K_START + t * (K_END - K_START)


def calculate_draft_values(num_rounds, first_round_value):
    """
    Calculate draft pick values with power law decay.
    Decay exponent k is interpolated linearly: 0.8 at round 1, 2.5 at last round.

    Args:
        num_rounds: Total number of draft rounds
        first_round_value: Value of 1st round pick (center value)

    Returns:
        List of dicts with 'round', 'low', 'center', 'high'
    """
    results = []

    for r in range(1, num_rounds + 1):
        k = _k_for_round(r, num_rounds)
        # Power law: value decreases as round^-k
        raw_value = first_round_value * (r ** -k)

        # Round center to nearest 1000, then ±15% for low/high
        center = raw_value
        center = round(center / 1000) * 1000
        low = round(center * 0.85 / 1000) * 1000
        high = round(center * 1.15 / 1000) * 1000

        results.append({
            'round': r,
            'low': low,
            'center': center,
            'high': high
        })

    return results


def main():
    parser = argparse.ArgumentParser(
        description="Calculate draft pick values by round (power-law decay, ±15%% spread)."
    )
    parser.add_argument(
        "num_rounds",
        type=int,
        help="Number of draft rounds",
    )
    parser.add_argument(
        "first_round_value",
        type=float,
        help="Value of the 1st round pick (center value)",
    )
    args = parser.parse_args()

    if args.num_rounds < 1:
        parser.error("num_rounds must be at least 1")
    if args.first_round_value <= 0:
        parser.error("first_round_value must be positive")

    values = calculate_draft_values(args.num_rounds, args.first_round_value)

    print(f"{'Round':>6} {'Value (low)':>14} {'Value (center)':>16} {'Value (high)':>14}")
    print("-" * 54)
    for v in values:
        print(f"{v['round']:>6} {v['low']:>14,} {v['center']:>16,} {v['high']:>14,}")


if __name__ == "__main__":
    main()
