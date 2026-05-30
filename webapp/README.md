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
   `PlayerData-*.csv` files in `data/`). The sidebar shows **"Data updated: …"**
   so you can see how fresh that league's snapshot is.
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

## Theme (LCARS)

The UI is skinned to look like a *Deep Space 9* LCARS console. A **palette
toggle** at the top of the sidebar switches live between two schemes:

- **Cardassian Ops** — the station's amber / bronze / red / teal.
- **Starfleet LCARS** — the classic orange / peach / lavender / blue.

Your choice is **remembered between sessions**: it's written to
`webapp/.ui_settings.json` (a small, gitignored, per-clone preferences file) and
reloaded on startup. Delete that file to reset to the default (Cardassian Ops).
The dark base theme lives in `.streamlit/config.toml`; the LCARS styling itself
is injected as CSS from `app.py`.

## Refreshing data

The app reads `data/PlayerData-<league>.csv` from disk — it does **not** fetch
from StatsPlus itself. To pull fresh ratings, run the existing fetch scripts,
then re-score in the UI:

```bash
py fetch_all_player_data.py          # all leagues (skips ones already current)
py fetch_player_data.py --league ndl # just one
```

Scoring is **cached per (league + options + the PlayerData file's modification
time)**, so:

- filtering/sorting after a run is instant (cache hit), and
- a freshly fetched CSV is **re-scored automatically** the next time you Run —
  no restart needed. There's also a **"Clear cache & re-score"** button for a
  manual force-refresh.

(Reminder: `PlayerData` is the source of truth for the scouted ratings *and* for
roster state — team / org / league level / age — so a stale snapshot can mean
stale team/level labels and slightly stale level-sensitive scores. See
`tickets/0001-playerdata-ratings-only-truth.md` for the planned improvement.)

## Extending the UI

The app is a pure consumer of `vosball.services` — see
[`LOGIC_UPDATE_PROCESS.md`](../LOGIC_UPDATE_PROCESS.md) §4. To persist a new
preference (a default league, saved filters, a module's view options, …), reuse
the generic settings store in `app.py`:

```python
save_ui_setting("default_league", "wwoba")   # merge one key, keep the rest
settings = load_ui_settings()                  # -> dict (｛｝ if missing/bad)
```

## Status

**v1 + theming.** Covers the core eval table (filter/sort/search/export), the
LCARS reskin, and persisted preferences. Next up: a **player card** drill-down;
multi-league comparison and other views are possible future additions.
