# VOSBall Web UI — Status

> Where the local web UI stands today. Companion to [README.md](README.md) (how to
> run/use it). This is the architecture + progress snapshot.

The UI is a **local, multipage [Streamlit](https://streamlit.io) app** that runs
in your browser (`py -m streamlit run webapp/app.py`, or `run_ui.bat`). It is a
**pure consumer** of the layered `vosball` package and the suite's existing CLI
modules — it reads the same `data/` + `config/`, computes everything **in-process**,
and (almost) never writes to disk. It's skinned as a *Deep Space 9* LCARS console.

---

## Pages

| Page | File | What it does |
| --- | --- | --- |
| **Eval Browser** (default) | `app.py` | Pick a league + options → `evaluate_league` → sortable/filterable/searchable table. Filters: name, **Organization** (defaults to your org), position, level, VOS ranges. CSV export is byte-identical to `run_vos.py`. Row-click → Player Card. |
| **Player Card** | `app.py` | One player in detail: headline VOS (Reach/Career/Blended/Ceiling) + tiers; component scores; **SP-vs-RP role comparison** (pitchers); **career-WAR projection** (opt-in: actual accumulated + tier-pace percentile-adjusted remaining); adjustments; **scouted ratings**; park/injury; contract (when fetched). |
| **Depth Charts** | `depth.py` | Pick org → level cards (ML/AAA/…/R) → suggested **lineup**, **position depth**, **pitching staff**. VOS-only by default; opt-in in-season-stats toggle unlocks the blended composite + true vs-L/vs-R lineups. Org defaults to your team. |
| **Prospects** | `prospects.py` | Ranked prospect board (`ceiling × age-for-level × position/role`) with league-wide + within-org ranks. Org defaults to your farm (+ All-orgs); ceiling-source / pool / opt-in service-time controls. Row-click → Player Card. |
| **League Hub** | `league.py` | Per-league landing (reached by clicking a header chip or the nav): league header + a short **per-sim checklist** (persisted per league) + a **module quick-link grid** (built + "coming soon" tiles). |

**Global header (every page):** an LCARS title bar + a persistent, **clickable
export-status band** (`status.py`) — one color-coded chip per league (green =
export current, amber = needs export, reason in tooltip) from
`preflight.check_leagues`; a chip opens that league's Hub, ⟳ re-checks. Cached
per session (first load only hits the network).

---

## Architecture & patterns

- **Multipage shell** (`app.py::main`): builds the `st.Page`s, stashes them in
  `st.session_state["_pages"]`, renders the global chrome (LCARS theme → header →
  export-status band → sidebar palette toggle), then `st.navigation([...]).run()`.
- **Shared state:** the scored league lives once in `st.session_state["result"]`
  (`{rows, league, rating_scale, draft, apply_park, contracts}`), set by the Eval
  Browser; every other page consumes those rows. `_pages` lets chips / quick-links
  / row-clicks navigate via `st.switch_page`.
- **Pure consumer / reuse-in-process:** pages call the suite's own logic rather
  than reimplementing it — `vosball.services.evaluate_league` (scoring),
  `depth_chart.py` (`build_team_pool` + slotting), `prospect_rankings.py`
  (`compute_prospect_rows`), `preflight.check_leagues` (export status),
  `hof_grade.py` (accumulated WAR), `what_if.py` (rating field groups). No eval /
  depth / prospect CSVs are written — the app shows them live.
  - **One exception:** pitcher career-WAR needed an *additive* engine/config
    change (`vosball/engine` + `weights_v10.json`, golden re-blessed); everything
    else is read-only against `vosball`.
- **Caching:** `@st.cache_data` keyed on an eval signature that includes the
  `PlayerData` file mtime, so a fresh `fetch_*_player_data.py` pull auto-re-scores;
  navigating between pages is a cache hit (no recompute/refetch).
- **Network is always opt-in.** Offline/VOS-only by default. Network only on
  explicit action/toggle: contracts (Eval/Card), Depth in-season stats, Prospects
  service-time gate, the export-status ⟳, and the card's career-WAR fetch. All
  fail open.
- **Theme + prefs:** dark base in `.streamlit/config.toml`; LCARS CSS injected in
  `app.py`. Two palettes (**Cardassian Ops** / **Starfleet LCARS**) switch live and
  **persist** (with the per-league checklist) in `webapp/.ui_settings.json`
  (gitignored, per-clone).
- **"My org" default everywhere:** Eval filter, Depth Charts, Player Card, and
  Prospects all default to the team you play as (`config/league_settings.json`).

## The module framework

`league.py::MODULES` is the registry the League Hub renders. Built tiles link to
their page; planned tiles render a disabled "coming soon" card. **Shipping a
module = flip its `page` from `None` to the page key.**

- **Built:** Evaluations · Player Card · Depth Charts · **Prospects**
- **Planned:** Farm Value · Trade Targets · Draft Room · Free Agents · Finances

## Files

| File | LOC | Role |
| --- | --- | --- |
| `app.py` | ~900 | Shell + chrome + Eval Browser + Player Card + theming + prefs |
| `depth.py` | ~285 | Depth Charts page (reuses `depth_chart.py`) |
| `prospects.py` | ~180 | Prospect board (reuses `prospect_rankings.py`) |
| `league.py` | ~175 | League Hub: checklist + module registry |
| `status.py` | ~125 | Export-status header band (reuses `preflight`) |
| `career_war.py` | ~85 | Card career-WAR: accumulated-WAR fetch + tier-percentile blend |
| `.streamlit/config.toml` | — | Dark base theme |
| `requirements.txt`, `run_ui.bat` | — | Deps + Windows launcher |

## Extending the UI — conventions

1. New page = a module with a `page()` function; register in `app.py::main`
   (`st.Page` + add to `_pages` + the `st.navigation` list). If the function is
   literally named `page`, give the `st.Page` an explicit `url_path` (paths are
   inferred from the function name → collide otherwise).
2. Read the loaded league from `st.session_state["result"]`; don't re-score.
3. Default to offline; make any network an opt-in toggle that fails open.
4. Key per-league widgets `f"…_{league}"` so a league switch re-defaults instead
   of carrying stale state into new options.
5. Don't shadow an imported module with a local var (e.g. `league` the module vs
   `league` the slug — alias the import, as `app.py` does with `league_hub`).
6. Flip the matching `MODULES` tile to live so the hub quick-link works.

## Known constraints / caveats

- The app **reads** `data/PlayerData-{league}.csv`; it does **not** fetch — pull
  fresh data with the CLI fetch scripts (a ~1–2×/season task; the band flags
  staleness).
- It writes **no** eval CSV, so CLI tools that read `evaluation_summary_*.csv`
  (`project_season`, `farm_value`, `free_agent_market`, draft tools) still need
  `run_vos_all.py` first.
- Org/level come from the eval (`PlayerData`) snapshot, not a live `/players`
  roster pull — see `tickets/0001-playerdata-ratings-only-truth.md`.
- The suite is unaffected: `py tests/test_golden.py` stays green.

## Roadmap (next)

Build out the planned hub tiles (Farm Value next — it builds on the prospect
board), pass the Hub's selected league/org into the target module pages, live-roster
+ promotion/cut signals on depth charts, and multi-league comparison.
