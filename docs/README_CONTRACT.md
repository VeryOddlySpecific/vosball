# core/contract.py — Player Contract Valuation

Part of VOSBall, a baseball player-evaluation suite for OOTP leagues run on StatsPlus, built on **VOS (VOS Optimized Score)** on a 20–80 scale. The current engine is **VOS v10** in the `vosball/` package; run it via **`run_vos.py`** at the repo root, which writes eval CSVs to `{league}/eval/...`.

`core/contract.py` computes fair contract value for a player by combining three ingredients:

1. **VPC** (dollars per VOS point), calibrated from the league's MLB salaries
2. **Per-year projected VOS**, using an age-decline curve
3. **Context multipliers**: contract type (arb / extension / FA), elite-premium tier, and a risk discount for prospect-bust exposure

Then hands the resulting `total_max_value` to [`../core/contract_builder.py`](../core/contract_builder.py) for structuring (or falls back to a built-in min-guaranteed structurer if the main solver can't hit the target).

---

## Quick start

```powershell
# Open-market FA, 5 years
py core\contract.py --league sahl --id 73114 --years 5 --type market

# Pre-FA extension, 6 years, 3 arb years remaining
py core\contract.py --league sahl --id 84280 --years 6 --type extension --arb-years 3

# Valuation only, no structuring
py core\contract.py --league woba --id 52898 --years 3 --type market --no-structure

# Strict FA-only VPC calibration (excludes arb deals from the pool)
py core\contract.py --league woba --id 52898 --years 3 --type market --market-only
```

---

## Pipeline

```
  evaluation_summary_<league>_*.csv
           │
           ▼
  compute_vpc_base  ──►  VPC ($ per VOS point)
    (filtered rows)
           │
           │  + target player's VOS_Current, VOS_Potential, Age, Pos
           ▼
  project_vos_year (N years, age curve)  ──►  projected_VOS[1..N]
           │
           ▼
  × type_mult (arb ladder / extension_fa / market)
           │
           ▼
  × elite_premium tier (Regular / All-Star / Star / Superstar / ...)
           │
           ▼
  Σ per-year fair values  ──►  subtotal
           │
           │  × (1 − risk_discount)
           ▼
  total_fair_value
           │
           ▼
  contract_builder.build_contract()  ──►  structured contract
           │    (fallback if it misses target)
           ▼
  Final: per-year base salaries, max incentives, 2x-rule compliant
```

---

## CLI reference

### Required

| Flag | Description |
|---|---|
| `--league SLUG` | League slug (sahl, wwoba, woba, tlg, sdmb, uba) |
| `--id PID` | Player ID |
| `--years N` | Contract length |

### Contract framing

| Flag | Default | Notes |
|---|---|---|
| `--type {market,extension}` | `market` | Market = open-market FA. Extension = pre-FA with optional arb years |
| `--arb-years N` | `0` | Arb years remaining (extension only). Year-1-arb = 40% of market, Y2 = 55%, Y3 = 70% |
| `--pre-arb-years N` | `0` | Pre-arb years covered by a very early extension (league minimum ~10% of market) |

### VPC calibration

| Flag | Default | Notes |
|---|---|---|
| `--market-only` | off | Calibrate VPC only on 6+ MLB service-year players (excludes arb). Higher VPC, smaller sample |
| `--no-players-filter` | off | Skip the /players filter entirely (includes pre-arb minimums). Not recommended |
| `--base-url URL` | from `config/league_url.json` | Override the /players endpoint |
| `--league-url-config PATH` | `config/league_url.json` | Override the URL mapping file |
| `--vos-floor`, `--winsor-lower`, `--winsor-upper` | from config | Calibration thresholds |
| `--salary-col`, `--vos-col`, `--pot-col` | from config | Override CSV column names |

### Contract structuring (passed through to `contract_builder`)

| Flag | Default | Notes |
|---|---|---|
| `--incentives N` | `4` | Number of incentive categories |
| `--incentive-cap-pct F` | `0.10` | Each incentive ≤ F × highest annual value |
| `--use-option` | off | Treat final year as team option with buyout |
| `--option-year-value $` | auto | Stated option year value |
| `--buyout-pct F` | `0.25` | Buyout ≥ F × option year value |
| `--rounding $` | `100000` | Round salaries/incentives to this multiple |
| `--min-annual-value $` | `0` | Floor on any annual salary |
| `--threshold-ip F` | none | IP threshold (recorded only) |
| `--no-apply-2x` | off | Disable the 2x rule (H ≤ 2L when any annual ≥ $10M) |
| `--structure / --no-structure` | on | Skip structuring to get pure valuation |

### Misc

| Flag | Default |
|---|---|
| `--config PATH` | `config/contract_config.json` |
| `--input PATH` | Auto-resolved to latest `<league>/eval/evaluation_summary_*.csv` |
| `--log-level` | `INFO` |

---

## Config file: `config/contract_config.json`

All numerical knobs live here. Change values and re-run — no code edits needed.

### `age_curve`

Separate hitter/pitcher blocks. Each year, starting from `VOS_Current`:

- **Pre-peak** (`age < peak_start`): close `ramp_per_year` fraction of the gap to `VOS_Potential`
- **In peak window** (`peak_start ≤ age ≤ peak_end`): held at `VOS_Potential`
- **Post-peak** (`age > peak_end`): lose `decline_per_year_post_peak` VOS/year, multiplied by `decline_accel_multiplier` after `accelerating_decline_age`

Defaults lean steeper for pitchers (injury exposure):

| | Hitter | Pitcher |
|---|---|---|
| Peak window | 27–29 | 26–28 |
| Post-peak decline/yr | 0.5 | 0.7 |
| Accelerating decline age | 33 | 32 |
| Accel multiplier | 1.5× | 1.6× |

### `type_multipliers`

Applied to the projected-VOS × VPC raw value, per year.

| Phase | Default | When applied |
|---|---|---|
| `pre_arb` | 0.10 | Pre-arb years covered by very early extensions |
| `arb_per_year` | `[0.40, 0.55, 0.70]` | Cost-controlled arb years (Y1/Y2/Y3). Longer ladders extend the last rung |
| `extension_fa` | 0.85 | FA years bought out as part of a pre-FA extension (discount for early commit) |
| `market` | 1.00 | Open-market FA |

### `elite_premium`

Corrects for VOS sigmoid compression at the top. Applied per year, **after** type multipliers and **before** risk discount. Pick the highest matching tier by `min_vos`.

| Tier | `min_vos` | Multiplier | Label |
|---|---|---|---|
| 6 | 70 | 2.15 | Superstar |
| 5 | 65 | 1.75 | Star |
| 4 | 60 | 1.45 | All-Star |
| 3 | 55 | 1.25 | Above-Avg |
| 2 | 45 | 1.15 | Regular |
| 1 | 0 | 1.00 | Below-Avg |

Disable with `"enabled": false` to run pure-linear.

### `risk_discount`

Applied to the per-year subtotal (after all multipliers) as a single fraction.

- **Disabled for established players**: if `VOS_Current ≥ min_mlb_vos_floor` (default 45), discount = 0
- **Otherwise**: `per_gap_point × (VOS_Potential − VOS_Current)`, capped at `max_discount`
- **Pitcher add-on**: `pitcher_extra_discount` (default +3%) stacked on top for any pitcher

### `vpc`

Calibration thresholds fed to `farm_value_old.compute_vpc_base`:

- `vos_floor` (25.0) — minimum MLB VOS to be included in the pool
- `winsor_lower` / `winsor_upper` (0.025 / 0.975) — percentile clipping on salary and VOS
- `salary_col` / `vos_col` / `pot_col` — CSV column names

### `contract_defaults`

Defaults for the structuring pass. Overridable at CLI.

---

## VPC calibration modes

| Mode | Flag | Keeps | Rationale |
|---|---|---|---|
| Default | (none) | 6+ service years **or** arb **or** multi-year guarantee | Matches `farm_value_old` baseline. Good mix of market-comp contracts |
| Market-only | `--market-only` | 6+ MLB service years only | True open-market $/VOS. Excludes arb suppression. Produces higher VPC. Use for pricing veterans and open-market FAs |
| No filter | `--no-players-filter` | All ML players with salary | Inflates VPC by including pre-arb minimums. Not recommended |

All modes require the `/players` endpoint to be reachable; `--market-only` hard-errors if it isn't. The endpoint is looked up from `config/league_url.json` by league slug.

---

## Output

### Valuation table

```
Yr  Age  Phase       VOS     Tier        xType  xTier  Raw $         Fair $
----------------------------------------------------------------------------
1   29   FA          62.25   All-Star    1.00   1.45   $4,541,286    $6,584,865
2   30   FA          61.75   All-Star    1.00   1.45   $4,504,810    $6,531,975
3   31   FA          61.25   All-Star    1.00   1.45   $4,468,334    $6,479,084
----------------------------------------------------------------------------
Subtotal (pre-risk):  $19,595,924
Total fair value:     $19,600,000
Implied AAV:          $6,533,333
```

### Structured contract

Per-year base salaries, max incentives, guaranteed totals, and 2x-rule compliance check. Same format as `core/contract_builder.py` standalone output.

If `contract_builder`'s solver misses the target by more than one rounding unit, the tool falls back to a built-in min-guaranteed 2x-compliant structurer and labels the output:

```
[contract_builder missed target by $X — using fallback structurer]
========================================
CONTRACT STRUCTURE (fallback: min-guaranteed 2x pattern)
========================================
...
```

Fallback pattern: `(N−1)` years at `L`, one year at `H = 2L`, with per-year incentives at the cap. Distributes any rounding residual into the top year. Does not support `--use-option`.

---

## Tuning notes

- **Elite premium not hitting real star salaries.** Bump the `60–65` and `65+` tier multipliers in config. A 60-VOS vet in a high-payroll league can comfortably sit at 1.5–1.7x.
- **Extensions feel light.** Raise `type_multipliers.extension_fa` from 0.85 toward 0.90–0.95, or adjust the arb ladder if your league's arb awards run hot.
- **Age decline feels too gentle.** Raise `decline_per_year_post_peak` or lower `accelerating_decline_age`. Pitchers especially — 0.9–1.0/yr post-peak is defensible.
- **Risk discount not firing on prospects.** Lower `min_mlb_vos_floor` from 45 (e.g. to 40), or tie the gate to age instead of VOS by editing the logic.
- **VPC too low.** Try `--market-only` or confirm `/players` is reachable — default mode silently falls back to including pre-arb if the fetch fails.

---

## Files

```
core/contract.py                     # CLI tool (this doc)
core/contract_builder.py             # Structuring engine (imported, unmodified)
core/farm_value_old.py               # VPC calibration + /players lookup (imported, unmodified)
config/contract_config.json          # All tunable knobs
config/league_url.json               # League slug → /players base URL
<league>/eval/evaluation_summary_*.csv   # Input — latest auto-resolved (from run_vos.py)
```
