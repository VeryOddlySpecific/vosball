# Farm value tool (`farm_value.py`)

This document explains **what the script does**, **why the formulas look the way they do**, **assumptions and limitations**, and **how another developer or agent should extend or debug it**. It is meant to sit beside `README_VOS_V2.md` (VOS scoring) and the StatsPlus/`vos_v2.py` pipeline.

---

## Purpose

`farm_value.py` estimates **organization farm system value** from an **evaluation summary CSV** produced by `vos_v2.py`. It:

1. Calibrates a **VPC** (dollars per projected VOS point) from **MLB** rows in the same file.
2. Values each **non-MLB** player with a **projection-first** model (potential VOS × multipliers × VPC).
3. Aggregates to **org-level** totals using **top 12 + discounted tail** (not a raw sum of everyone).

Output is useful for **league-internal rankings** and discussion; dollar amounts are **model units** calibrated from salaries, not literal trade prices.

---

## Prerequisites

### Input CSV

- Path: typically `evaluation_summary_{league}_{YYYYMMDD_HHMMSS}.csv` in the project directory (or pass `--input`).
- **Must** be generated with **`vos_v2.py --contracts`** so rows include contract columns (`Contract_*`).
- Required columns include: `ID`, `Org`, `League_Level`, `Age`, `Contract_is_major`, `Contract_salary0`, `VOS_Score`, `VOS_Potential`, plus contract fields used for VPC filtering and multi-year detection.

**Recommended:** `Projected_Position` is present (from `vos_v2`); role/scarcity logic falls back to `Pos` if missing.

### Python

- **Standard library only** (no pandas): `csv`, `json`, `argparse`, `urllib`, etc.

### StatsPlus API (optional but recommended)

- **`/players`** is fetched when a **base URL** resolves (`--league` + `config/league_url.json`, or `--base-url`).
- Used for:
  - **VPC calibration:** “market comp” filter on MLB rows (service / arbitration / multi-year).
  - **Prospect vs vet gap-risk:** `mlb_service_days`, `pro_service_years`.
  - **Org totals:** `is_prospect_org` uses the same prospect definition as gentler risk.

If `/players` fails or no URL is provided, the script **still runs** but uses **fallbacks** (e.g. all MLB rows for VPC that pass salary/VOS filters; gentler risk for everyone; everyone counts as prospect for org filter unless you rely on API fields).

### League API URLs

- Shared config: **`config/league_url.json`** — map league slug → API base URL (same idea as `vos_v2.py`’s `load_league_api_base_urls`).
- Override: `--base-url` or `--league-url-config`.

---

## High-level theory

### 1. VPC (VOS point cost)

**Idea:** Big-league **realized salary** (chosen year column, default `Contract_salary0`) reflects what teams pay per unit of **projected** talent. Divide total adjusted salary by total adjusted **projected VOS** (`--pot-col`, default `VOS_Potential`) to get **dollars per VOS point** for the league snapshot.

**MLB rows used:**

- `League_Level == ML`
- `Contract_is_major == 1`
- Positive salary, projected VOS ≥ `--vos-floor` (default 25)

**Winsorization:** Salaries and VOS values are clipped to quantiles (`--winsor-lower` / `--winsor-upper`, default 2.5%–97.5%) before summing, to limit a few mega-contracts or outliers from defining VPC.

**Market-comp filter (when `/players` is loaded):** Only include MLB calibration rows whose `/players` metadata suggests less team-controlled pricing:

- `mlb_service_years >= 6`, **or**
- `has_received_arbitration`, **or**
- Multi-year guarantee: `Contract_years > 1` or any `Contract_salary1..14 > 0`

Also drops inactive/retired players when flags indicate. If a player is missing from `/players`, the row is **kept** for VPC (conservative default).

### 2. Per-player farm value (non-MLB)

**Farm pool:** `League_Level != ML` and non-empty `Org`.

**Base score:** **`VOS_Potential`** (projection-first), not current `VOS_Score`.

**Multipliers (conceptual):**

| Factor | Role |
|--------|------|
| `m_level` | Level difficulty / value (AAA … Rookie) — see `LEVEL_MULT` in code |
| `m_prox` | Proximity to MLB — see `PROX_MULT` |
| `m_risk` | Gap between potential and **current** VOS; **gentler** for “prospects” (see below) |
| `m_age` | **Age-for-level** curve: neutral plateau, bonus young-for-level, penalty old-for-level |
| `m_control` | Small bump if `Contract_is_major == 1` (40-man type in minors) |
| `m_pos_role` | Optional: RP debuff, C/SS/CF boost (unless `--disable-position-adjust`) |
| `m_pos_scarcity` | Optional: league share vs equal buckets (**only if** `--league-scarcity`) |

**Formula:**

`farm_value = proj_vos * VPC * m_level * m_prox * m_risk * m_age * m_control * m_pos_role * m_pos_scarcity`

(With `m_pos_*` = 1 when disabled.)

### 3. Prospect vs vet (gap risk + org inclusion)

Uses `/players` when available:

- **Prospect** (gentler gap penalty, `is_prospect_org == 1`): `mlb_service_days` ≤ `--prospect-max-mlb-days` (default 90) **and** `pro_service_years` < `--prospect-max-pro-years` (default 8).
- **Else:** vet curve (stronger gap penalty).

**Org-level totals** include only `is_prospect_org == 1` by default (drops many AAA org fillers). Use `--org-include-non-prospects` to revert to all farm players in the org sum.

### 4. Org aggregation: top 12 + tail

Not a full roster sum:

- Sort each org’s players by `farm_value` (after all multipliers).
- **Top 12** summed at full weight.
- **Remaining** players summed at **25%** weight (`tail_weight=0.25` in code).

This emphasizes **star/upside concentration** similar to how many people discuss farm systems.

### 5. Position / role adjustments (defaults)

- **Role static** (on unless `--disable-position-adjust`):
  - RP/CL/MR/SU/LR → debuff (`--rp-debuff`, default 0.93)
  - C, SS, CF → boost (`--premium-pos-boost`, default 1.04)
  - SP/P and other hitter buckets → 1.0 for role
- **League scarcity** (**off by default**): pass `--league-scarcity` to scale by farm-pool share vs equal split across fixed buckets (`POSITION_BUCKETS` in code). Tunables: `--scarcity-strength`, `--scarcity-min`, `--scarcity-max`.

**Design note:** `VOS_Potential` is treated as **position-neutral** in the scoring pipeline; role multipliers add a **separate** market-shaped layer (especially relief vs rotation). If that ever double-counts your intent, use `--disable-position-adjust` or tune defaults.

---

## Outputs

### Org CSV (default path)

- Pattern: `farm_values_{league}_{run_timestamp}.csv` next to the input (run time = when the script starts, so re-runs do not overwrite).
- Columns include: `farm_value_total`, `farm_value_top12`, `farm_value_tail_weighted`, counts, `farm_value_non40` / `num_non40`, etc.

### Optional player CSV (`--output-players`)

- One row per valued farm player with breakdown columns: `vos`, `vos_pot`, `proj_vos`, `vos_gap`, `m_risk_mode`, service fields from API, `is_prospect_org`, age-for-level fields, position multipliers, `farm_value`, etc.

Use this to audit **why** a player ranks where they do.

---

## Running (examples)

From the repo root (where `evaluation_summary_*.csv` and `config/` live):

```bash
python farm_value.py --league woba
```

Explicit input and player export:

```bash
python farm_value.py --input evaluation_summary_woba_20260410_093926.csv --output-players woba_players.csv
```

Enable league scarcity:

```bash
python farm_value.py --league woba --league-scarcity
```

Full flag list:

```bash
python farm_value.py --help
```

---

## Assumptions and limitations (for agents and maintainers)

1. **Dollar interpretability:** Totals scale with VPC and roster size; treat as **comparable within one run/league snapshot**, not as absolute asset values.
2. **VPC ≠ perfect market:** Salaries mix bargains, dead money, extensions, and timing; market-comp filter is a **heuristic**.
3. **Potential VOS quality:** Garbage-in/garbage-out from `vos_v2` weights and player data. Disagreement with human lists may be **VOS**, not farm math.
4. **`/players` schema:** Column names are normalized (lowercase, spaces → underscores). If StatsPlus changes CSV headers, update `build_players_lookup` / field access.
5. **Prospect definition** is **rule-based** (days/years), not a scouting definition; tune flags if the league disagrees.
6. **Top 12 + 25% tail** is hard-coded in `summarize_org_values`; expose CLI if you need N/α configurable.
7. **League scarcity** changes meaning to “value inside this league’s current farm pool”; default **off** so headline ranks stay closer to talent + shape, not supply.

---

## Code map (for agents)

| Area | Location / symbols |
|------|---------------------|
| CLI | `parse_args()` |
| VPC | `compute_vpc_base`, `is_market_comp_player`, `has_multi_year_guarantee` |
| `/players` fetch | `build_players_lookup`, `_fetch_csv_endpoint`, `normalize_key` |
| Prospect / risk | `is_prospect_for_risk`, gap logic inside `apply_player_valuation` |
| Age-for-level | `age_for_level_multiplier`, `BASELINE_AGE` |
| Position | `canonical_position_bucket`, `projected_role_field`, `role_static_multiplier`, `scarcity_multiplier`, `compute_league_position_shares`, `POSITION_BUCKETS` |
| Org roll-up | `summarize_org_values` (top 12, tail, prospect filter) |
| URL config | `load_league_api_base_urls`, `resolve_base_url`; file `config/league_url.json` |
| Related | `vos_v2.py` — `LEAGUE_URLS_FILENAME`, `--contracts`, evaluation summary columns |

**Extension ideas (not implemented):** CLI for top-N/tail weight; separate “rank index” output without dollars; scarcity-only without role multipliers; age ceiling in addition to age-for-level.

---

## Changelog (conceptual)

Evolution of this tool in conversation:

- Stdlib-only farm valuation from evaluation summaries with contracts.
- VPC on **projected** VOS; market-comp MLB filter via `/players`.
- Top 12 + 25% tail org totals.
- Prospect service rules for gentler risk and **org** inclusion.
- Age-for-level plateau curve.
- Role multipliers (RP / premium positions); **league scarcity opt-in** (`--league-scarcity`, default off).
- Timestamped default org output filenames to avoid overwrites.
- Shared `config/league_url.json` with `vos_v2.py`.

---

## See also

- `README_VOS_V2.md` — VOS scoring and evaluation summary generation.
- `vos_v2.py` — `--contracts`, `--league`, output column order.
- `config/league_url.json` — league → StatsPlus API base URL.
