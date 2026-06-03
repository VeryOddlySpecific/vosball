# Farm value

Value and rank every org's farm system in a league. The easiest way to do this
is the **Farm Value** page in the local web app; the `core/farm_value.py` CLI
backs the same engine for batch/scripted use. This doc explains both, then the
algorithm (VPC calibration, `prospect_score × VPC`, top-12 + tail roll-up) and
its assumptions.

---

## The app (start here)

The primary interface is the local Streamlit web app:

```powershell
py -m streamlit run webapp/app.py
# or: run_ui.bat
```

Open the **Farm Value** page. It values every org's farm system and ranks them,
so your team shows up as "Ranked 4th of 30" with its top prospects listed
beneath. It scores in-process over the same prospect board the Prospects page
builds — no network call required.

**Units:** the *ranking* is always available offline (VPC is a global scalar, so
it never changes the order). **Dollar** figures need a VPC calibrated from MLB
contracts, read from the latest on-disk eval
(`{league}/eval/evaluation_summary_*.csv`). If that eval was generated with
contracts you get `$`; otherwise the page shows a **unitless farm index**.

---

## The CLI (`core/farm_value.py`)

For batch runs, scripting, or per-player audit exports, run the CLI from the repo
root:

```powershell
py core\farm_value.py --league woba
```

It values a league's farm from the latest **prospect rankings board**
(`prospect_rankings_{league}_*.csv`, from `core/prospect_rankings.py`) and rolls
up org totals. VPC dollar calibration is read from an evaluation summary CSV
produced by **`run_vos.py`** (the VOS v10 engine in the `vosball/` package).

### Inputs

- **Prospect board** — `prospect_rankings_{league}_*.csv` (auto-picked latest for
  `--league`, or pass `--rankings-input`). Supplies `prospect_score`, `Org`, and
  the per-player breakdown columns. The multipliers (level, proximity, risk,
  age-for-level, position/role) are already baked into `prospect_score` by
  `prospect_rankings.py` — see `farm_value_old.py` for the original multiplier
  derivation.
- **Evaluation summary** (for VPC only) — `{league}/eval/evaluation_summary_*.csv`
  from `run_vos.py`. Auto-picked, or pass `--evaluation-input`. For a **dollar**
  VPC it must include MLB contract columns (`Contract_is_major`,
  `Contract_salary0`, the projected-VOS calibration column). If no MLB contract
  rows are available, VPC falls back to `1.0`, farm values become **model
  points** (not dollars), and the org ranking is unaffected.

### StatsPlus API (optional)

`/players` is fetched when a base URL resolves (`--league` +
`config/league_url.json`, or `--base-url`) and is used only to refine the VPC
**market-comp** filter on MLB calibration rows (service / arbitration / multi-year
guarantee). Without it, VPC uses the legacy salary/VOS filter and the script still
runs.

### CLI flags

| Flag | Purpose |
|------|---------|
| `--league` | League slug; auto-picks latest rankings + eval files. |
| `--rankings-input` | Explicit prospect_rankings CSV path. |
| `--evaluation-input` | Explicit evaluation_summary CSV (VPC calibration only). |
| `--output-org` | Output CSV for org farm values (default: timestamped, next to input). |
| `--output-players` | Optional per-player breakdown CSV (audit why a player ranks where it does). |
| `--salary-col` | Salary column for VPC (default `Contract_salary0`). |
| `--pot-col` | Projected-VOS column for VPC (default `VOS_Potential`, a v6 alias of `VOS_Reach`). |
| `--score-source` | `reach` \| `career` \| `blended` — picks the v6 VPC anchor by intent; overrides `--pot-col`. `career` ($/career-projection) is the most defensible for MLB salaries. |
| `--vos-floor` | Minimum MLB VOS for calibration (default 25). |
| `--winsor-lower` / `--winsor-upper` | Salary/VOS winsorization quantiles (default 0.025 / 0.975). |
| `--non40-only` | Org totals use only non-40-man players. |
| `--org-include-non-prospects` | Include non-prospect rows in org totals. |
| `--base-url` / `--league-url-config` | Override `/players` API URL / its config file. |
| `--org-config` | JSON file limiting which org names to include. |
| `--log-level` | `DEBUG` \| `INFO` \| `WARNING` \| `ERROR`. |

Full list: `py core\farm_value.py --help`.

---

## Algorithm

### 1. VPC (VOS point cost)

**Idea:** big-league realized salary reflects what teams pay per unit of
*projected* talent. Divide total adjusted salary (`--salary-col`) by total
adjusted projected VOS (`--pot-col` / `--score-source`) over MLB rows to get
**dollars per VOS point** for the league snapshot.

**MLB rows used:** `League_Level == ML`, `Contract_is_major == 1`, positive
salary, projected VOS ≥ `--vos-floor`.

**Winsorization:** salaries and VOS values are clipped to the
`--winsor-lower`/`--winsor-upper` quantiles before summing, so a few
mega-contracts or outliers don't define VPC.

**Market-comp filter (when `/players` loads):** keep only MLB calibration rows
whose `/players` metadata suggests less team-controlled pricing — `service ≥ 6
yrs`, **or** has received arbitration, **or** a multi-year guarantee
(`Contract_years > 1` or any `Contract_salary1..14 > 0`). Rows missing from
`/players` are kept (conservative). Inactive/retired players are dropped.

### 2. Per-player farm value

```
farm_value = prospect_score × VPC
```

`prospect_score` comes straight from the prospect-rankings board, which has
already applied the level / proximity / risk / age-for-level / position-role
multipliers to projected VOS. `farm_value.py` only re-anchors that score into
dollars via VPC. (The original multiplier model lives in `farm_value_old.py`; it
was migrated into `prospect_rankings.py`.)

### 3. Org aggregation: top 12 + tail

Org totals are **not** a raw roster sum (`summarize_org_values`):

- Sort each org's players by `farm_value`.
- **Top 12** summed at full weight.
- **Remaining** players summed at **25%** weight (`tail_weight = 0.25`).

This emphasizes star/upside concentration, the way most people discuss farm
systems. Org rows are then ranked (`rank` / `num_orgs` stamped) by
`farm_value_total`.

---

## Outputs

### Org CSV (default)

Timestamped `farm_values_{league}_{run_timestamp}.csv` next to the input (run
time = script start, so re-runs don't overwrite). Columns include `rank`,
`num_orgs`, `farm_value_total`, `farm_value_top12`, `farm_value_tail_weighted`,
counts, and non-40-man variants.

### Player CSV (`--output-players`)

One row per valued farm player with breakdown columns: `prospect_score`,
`vpc_base`, `vos`, `vos_pot`, `vos_gap`, `m_age`, `m_pos_role`, age-for-level
fields, `farm_value`, and prospect ranks — to audit **why** a player ranks where
it does.

---

## Assumptions and limitations

1. **Dollar interpretability:** totals scale with VPC and roster size — comparable
   *within one run/league snapshot*, not absolute asset values. When VPC falls
   back to `1.0`, treat values as a unitless index.
2. **VPC ≠ perfect market:** salaries mix bargains, dead money, extensions, and
   timing; the market-comp filter is a heuristic.
3. **Projection quality:** garbage-in/garbage-out from `run_vos.py` weights and
   player data. Disagreement with human lists may be the VOS scoring, not the farm
   math.
4. **`/players` schema:** headers are normalized (lowercase, spaces →
   underscores). If StatsPlus changes CSV headers, update `build_players_lookup`.
5. **Top 12 + 25% tail** defaults live in `summarize_org_values` (`build_farm_values`
   exposes `top_n` / `tail_weight`).

---

## Code map

| Area | Location / symbols |
|------|---------------------|
| CLI | `core/farm_value.py` — `parse_args`, `main` |
| Pure core (shared with app) | `build_farm_values`, `build_player_rows`, `rank_org_values` |
| App page | `webapp/farm_value_page.py` |
| VPC + org roll-up (reused) | `core/farm_value_old.py` — `compute_vpc_base`, `summarize_org_values`, `build_players_lookup`, `resolve_base_url` |
| Prospect scoring | `core/prospect_rankings.py` (produces `prospect_score`) |
| URL config | `config/league_url.json` |
| Eval source | `run_vos.py` → `{league}/eval/evaluation_summary_*.csv` |

---

## See also

- `../run_vos.py` and the `vosball/` package — VOS v10 scoring and evaluation
  summary generation (replaces the retired `vos_v2.py`; its old doc is archived at
  `archive/README_VOS_V2.md`).
- `../core/prospect_rankings.py` — the prospect board farm values are built on.
- `README_ORG_DEPTH_ANALYSIS.md` — org-level positional depth rollup.
- `config/league_url.json` — league → StatsPlus API base URL.
