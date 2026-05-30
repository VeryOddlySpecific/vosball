# VOSBall — Local Web UI

A small Streamlit app for browsing VOS player evaluations in your web browser.
It's a multipage app with two screens (pick them from the sidebar):

- **Eval Browser** — pick a league, run the scoring, then sort / filter / search
  the results and download the CSV.
- **Player Card** — a single-player detail view (scores, adjustments, projected
  WAR, positional breakdown, contract) for any player from the last evaluation.

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
6. To inspect one player, **click their row** in the table to jump straight to
   their Player Card — or open the **Player Card** page from the sidebar and pick
   a player there. (Tip: use the name filter to narrow the list first.)

## Player Card

The **Player Card** page shows a single player in detail, rendered entirely from
the row the evaluation already produced (no extra scoring run):

- headline VOS metrics — Reach / Career / Blended / Ceiling — plus tiers;
- component scores — hitters: batting / defense / baserunning; **pitchers get a
  side-by-side SP-vs-RP role comparison** (graded as both a starter and a
  reliever) — and the adjustment stack (development / age / personality, plus
  the draft adjustments when draft mode is on);
- **projected career WAR** (archetype average, not a per-player forecast) —
  hitters get a single-profile section plus projection insights (ideal position,
  viable spots, margins) and an **all-positions Current / Potential** table;
  pitchers get an **SP-vs-RP WAR table** (ceiling + career/remaining WAR for each
  role), since a starter and a reliever project very differently;
- a **Scouted ratings** block with the raw underlying ratings (hitters: batting
  / position / defense / baserunning / personality; pitchers: ability / pitches
  / splits / personality), shown at the same rating scale the eval used;
- a park / injury line, and a compact **contract** summary when contracts were
  included in the run.

Pick a player from the searchable selector at the top (or click a row on the
Eval Browser). You need to have run an evaluation on the Eval Browser page first
— the card reads that result. Full contract / fair-value (VPC) economics is the
remaining planned follow-up.

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

Multipage app: **Eval Browser** (filter/sort/search/export) + **Player Card**
(single-player detail), with the LCARS reskin and persisted preferences. Future
additions: raw scouted-ratings + pitcher SP/RP + fair-value on the card, plus
multi-league comparison and a draft board as their own pages.
