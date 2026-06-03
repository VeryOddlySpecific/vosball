# VOSBall

Baseball player evaluation & league management for **OOTP** leagues run on the
**StatsPlus** platform, built around **VOS (VOS Optimized Score)** — a
configurable, multi-model rating system that normalizes raw scouting tools to
the 20–80 scouting scale.

**The primary way to use VOSBall is the local web app.** It scores a league
in-process and gives you sortable evaluations, depth charts, prospect boards,
ranked farm systems, trade targets, and free-agent fits — all from the same VOS
engine the command line uses. The CLI tools and the `vosball` engine package sit
underneath for batch runs, automation, and analyses not yet surfaced in the UI.

---

## Quick start

```powershell
# one-time: install the web UI dependencies
py -m pip install -r webapp/requirements.txt

# launch the app  (or just double-click run_ui.bat on Windows)
py -m streamlit run webapp/app.py
```

Pick a league on the **Home** screen and the app evaluates it on first visit.
Everything is **offline / point-in-time by default** — any network call (pulling
a fresh ratings export, contracts, or live in-season stats) is an explicit,
opt-in button or toggle that fails open.

The app reads `data/PlayerData-{league}.csv` (a scouting-ratings export) plus the
per-league files under `config/`. Pull a fresh export from the **League Hub →
⟳ Pull fresh ratings** button, or with the CLI fetch tools.

---

## The web app

A local, multipage [Streamlit](https://streamlit.io) app, skinned as an LCARS
console. Each page is a **pure in-process consumer** of the engine — it reads the
same `data/` + `config/` and computes live; it does not write eval CSVs.

| Page | What it does |
| --- | --- |
| **Eval Browser** | Score a league → sortable / filterable / searchable table of every player's VOS. CSV export is byte-identical to `run_vos.py`. |
| **Player Card** | One player in depth: VOS (Reach / Career / Blended) + tiers, component scores, SP-vs-RP role split, opt-in career-WAR projection, scouted ratings, park / injury / contract. |
| **Depth Charts** | Per-level lineup, position depth, and pitching staff for an org. VOS-only by default; opt-in in-season stats unlock the blended composite and true vs-L/vs-R lineups. |
| **Prospects** | Ranked prospect board (ceiling × age-for-level × position/role), league-wide and within-org. |
| **Farm Value** | Every org's farm system valued and **ranked** ("🏆 4th of 30"), with your top farm assets. Dollars when the eval carries contracts, else a unitless farm index. |
| **Trade Targets** | The league trade block scored against your roster needs — biggest holes first. |
| **Free Agents** | Biggest-holes-first FA targeting with best-fit free agents per need, gated to keep amateurs/draft-eligibles out. |

**Planned tiles:** Draft Room · Finances. The League Hub renders the full module
grid (built tiles link to their page; planned tiles show as "coming soon").

See [`webapp/README.md`](webapp/README.md) for how the UI is built and
[`webapp/UI_STATUS.md`](webapp/UI_STATUS.md) for the architecture snapshot.

---

## The VOS engine

The evaluation engine lives in the **`vosball/`** package
(`engine` → `data` → `services` → `reporting` / `cli`). **`run_vos.py`** is the
entry point and back-compat shim; it produces three complementary scores per
player, all normalized to 20–80 via sigmoid:

| Score | What it measures |
|---|---|
| **VOS_Reach** | Ceiling potential — logistic models trained on Pot\* ratings for hitters, SP, and RP separately |
| **VOS_Career** | Current production value — Stage-2 Spearman-tuned weights per position |
| **VOS_Blended** | Weighted composite (α = 0.4 reach, 0.6 career) for general use |

VOS **v10** highlights: logistic reach models replace the v9 linear ones;
Career Personality model v5 (Work Ethic ±3.0, Leadership zeroed after a noise
audit); RP model v9 folded into the main weights; per-tool age decay; hard floor
20 / ceiling 80, center 50.0, scale 15.0.

The app scores in-process, so for day-to-day use you don't run this directly.
Use `run_vos.py` when a **CLI tool needs an eval CSV on disk**, or for batch /
scripted evaluation:

```powershell
py run_vos.py --league <slug>                 # writes {league}/eval/.../evaluation_summary_*.csv (+ .md)
py run_vos.py --league <slug> --draft
py tools\run_vos_all.py                        # every configured league
```

> A golden test (`tests/test_golden.py`) guards that engine output stays
> byte-identical across refactors. The previous monolithic **VOS v2** engine is
> retired but kept as a rollback escape hatch at
> [`tools/archive/vos_v2.py`](tools/archive/vos_v2.py).

---

## Command-line tools

The app covers the common workflows. **`tools/`** holds the full standalone CLI
suite — for batch runs, automation, and analyses without a UI page yet (draft
prep, finances, season projections, awards, waivers, …). Most read the eval CSVs
that `run_vos.py` writes, so run an eval first.

A few entry points:

```powershell
py tools\draft_pool_analysis.py --league <slug>     # pre-draft pool reports
py tools\contract_audit.py --league <slug>          # OVERPRICED / FAIR / UNDERPRICED
py tools\waiver_wire.py --league <slug> --org <org> # grade the wire vs your needs
py tools\project_season.py --league <slug> --org <org> --level ML
```

Per-tool guides live in **[`docs/`](docs/)** (financials, draft workflow, org
depth, farm value, league setup, the per-sim checklist, and the logic-update
playbook). Superseded notes are kept under `docs/archive/`.

---

## Project layout

```
vosball/
├── run_vos.py            # VOS engine entry point + back-compat shim (only .py at root)
│
├── vosball/             # the engine, as a layered package
│   ├── engine/          #   scoring · reach · adjustments · WAR · normalization
│   ├── data/            #   PlayerData / config / contracts / parks loaders
│   ├── services.py      #   evaluate_league(...) — the one call the UI + CLI share
│   ├── reporting.py     #   CSV / Markdown writers
│   └── cli.py           #   argparse front end (run_vos.py delegates here)
│
├── webapp/              # local Streamlit UI — the primary interface
│
├── core/               # modules the UI imports in-process (run as `python core/<x>.py`)
│   ├── depth_chart.py · free_agent_market.py · trade_targets.py · trade_block.py
│   ├── prospect_rankings.py · farm_value.py · farm_value_old.py
│   ├── contract.py · contract_builder.py · hof_grade.py
│   ├── stats.py · fetch_player_data.py · preflight.py
│   └── org_summary_pdf.py · what_if.py
│
├── tools/              # standalone CLI suite (run as `python tools/<x>.py`)
│   ├── draft_pool_analysis.py · draft_grades.py · draft_board.py · draft_values.py
│   ├── contract_audit.py · budget_audit.py · payroll_audit.py · parse_financials.py
│   ├── waiver_wire.py · project_season.py · org_strength_report.py · org_depth_analysis.py
│   ├── player_card.py · awards_rank.py · playoff_planner.py · rule5_draft.py · rule5_protect.py
│   ├── run_vos_all.py · run_depth_chart_all.py · run_trade_targets_all.py · run_waiver_wire_all.py
│   └── archive/vos_v2.py     # retired v2 engine (rollback hatch)
│
├── docs/               # tool guides & references  (archive/ = superseded notes)
├── config/             # weights_v10.json, id_maps, league_settings, per-league teams/parks
├── data/               # PlayerData-{league}.csv exports (gitignored; one example shipped)
├── lib/                # vos_decay.py · draft_score.py
└── tests/              # golden engine test + core/UI suites
```

---

## Requirements

- **Engine + CLI tools:** Python 3.9+, standard library only — no third-party deps.
- **Web app:** `streamlit` + `pandas` (`py -m pip install -r webapp/requirements.txt`).
- **Network tools** (fresh ratings, contracts, live stats) need access to a
  StatsPlus API instance and a token in `config/statsplus_tokens.json` (not
  committed). The app works fully offline against a committed `PlayerData` export
  until you ask it to fetch.
