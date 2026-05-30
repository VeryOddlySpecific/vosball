# VOSBall — Local Web UI (Eval Browser)

A small Streamlit app that lets you browse VOS player evaluations for any league
in your web browser — pick a league, run the scoring, then sort, filter, and
search the results, and download the CSV.

It's a thin front end over the existing engine: it calls
`vosball.services.evaluate_league` (the same code path the `run_vos.py` CLI
uses) and reads the same `data/` and `config/` directories. Nothing about the
core suite changes to run it.

## Prerequisites

- **Python 3.12** (the suite is developed on 3.12.2).
- The repo's `data/PlayerData-<league>.csv` files and `config/` directory
  (already present in this repo).

## Install

From the repo root (the folder that contains `webapp/`, `vosball/`, `data/`,
`config/`):

```bash
py -m pip install -r requirements.txt
```

## Run

```bash
py -m streamlit run webapp/app.py
```

On Windows you can also just **double-click `run_ui.bat`** in the repo root.

Streamlit prints a local URL (usually <http://localhost:8501>) and opens it in
your default browser automatically.

## Using it

1. In the left sidebar, pick a **League** (auto-discovered from the
   `PlayerData-*.csv` files in `data/`).
2. Adjust options as needed:
   - **Rating scale** — `20-80` for most leagues; `1-100` for leagues that
     export component ratings on the 1-100 scale (defaulted per league, but
     overridable).
   - **Draft mode** — adds draft-specific adjustments and columns.
   - **Apply park factors** — uses `config/<league>-park-factors.json` when one
     exists.
   - **Include contracts** — fetches live contract data from the league API
     (needs the league's base URL in `config/league_url.json` and network
     access).
3. Click **Run evaluation**.
4. Sort by clicking column headers; narrow with the **Filters** (name search,
   position, league level, VOS score ranges); toggle **Show all columns** for
   the full output schema.
5. **Download** either the full canonical eval CSV (byte-identical to the CLI's
   output) or just the filtered view.

## Notes

- Scoring is cached per (league + options), so filtering and sorting are instant
  after the first run.
- This is **v1** — the core eval table. Player cards, multi-league comparisons,
  and other views are possible future additions.
