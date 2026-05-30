"""VOSBall — local web UI (Streamlit eval browser).

A thin browser front end over the VOS evaluation engine. It is a pure *consumer*
of the layered `vosball` package: it calls vosball.services.evaluate_league()
(the same UI-agnostic seam the CLI uses), shows the scored players in a
sortable / filterable table, and offers a CSV download that is byte-identical to
`run_vos.py` output (written through vosball.reporting.write_output_csv).

Run it with:

    py -m streamlit run webapp/app.py        (or double-click run_ui.bat)

Nothing in vosball/ changes for the UI to exist — see LOGIC_UPDATE_PROCESS.md §4.
"""
from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

# --- Path setup -------------------------------------------------------------
# Streamlit sets sys.path[0] to this file's dir (webapp/), not the repo root, so
# make the repo root importable: it holds both the `vosball` package and `lib`
# (the engine imports lib.vos_decay). Mirrors run_vos.py's sys.path handling.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DATA_DIR = ROOT / "data"
CONFIG_DIR = ROOT / "config"

import streamlit as st  # noqa: E402
import pandas as pd  # noqa: E402

from vosball.services import evaluate_league  # noqa: E402
from vosball.reporting import write_output_csv  # noqa: E402
from vosball.data import RATING_SCALES, DEFAULT_RATING_SCALE  # noqa: E402

# Leagues whose PlayerData exports component ratings on a 1-100 scale. Everything
# else defaults to 20-80 (weights_v10 native). Always overridable in the sidebar.
LEAGUE_SCALE_DEFAULTS: Dict[str, str] = {"ndl": "1-100"}

# Columns shown by default; "Show all columns" reveals the full output schema.
DEFAULT_COLUMNS = [
    "ID", "Name", "Pos", "Age", "Team", "Org", "League_Level",
    "VOS_Reach", "VOS_Career", "VOS_Blended", "VOS_Ceiling", "Ceiling_Tier",
    "VOS_Tier",
]
VOS_SCORE_COLUMNS = ["VOS_Reach", "VOS_Career", "VOS_Blended"]


# --- Streamlit-free core (importable + unit-testable) -----------------------

def discover_leagues() -> List[str]:
    """League slugs for which PlayerData exists, sorted. No hard-coded list."""
    return sorted(p.name[len("PlayerData-"):-len(".csv")]
                  for p in DATA_DIR.glob("PlayerData-*.csv"))


def default_scale_for(league: str) -> str:
    """Smart default rating scale for a league (overridable in the UI)."""
    return LEAGUE_SCALE_DEFAULTS.get(league, DEFAULT_RATING_SCALE)


def park_factors_path_for(league: str) -> Path | None:
    """Path to the league's park-factors file if one is shipped in config/."""
    p = CONFIG_DIR / f"{league}-park-factors.json"
    return p if p.exists() else None


def player_data_mtime(league: str) -> float:
    """Modification time of the league's PlayerData CSV (0.0 if absent).

    Folded into the score cache key so a fresh `fetch_*_player_data.py` pull
    auto-invalidates the cache — same file → instant hit, new file → re-scored.
    """
    p = DATA_DIR / f"PlayerData-{league}.csv"
    return p.stat().st_mtime if p.exists() else 0.0


def evaluate(
    league: str,
    rating_scale: str,
    draft: bool,
    contracts: bool,
    apply_park: bool,
) -> List[Dict[str, Any]]:
    """Score a league via the same services seam the CLI uses; return row-dicts.

    Pure glue — no Streamlit. Raises ValueError / FileNotFoundError on fatal
    input problems (missing weights, no players, missing contract base URL),
    exactly as evaluate_league does, for the caller to surface.
    """
    park_path = park_factors_path_for(league) if apply_park else None
    return evaluate_league(
        league,
        data_dir=DATA_DIR,
        config_dir=CONFIG_DIR,
        rating_scale=rating_scale,
        draft=draft,
        contracts=contracts,
        park_factors_path=str(park_path) if park_path else None,
    )


def to_csv_bytes(rows: List[Dict[str, Any]], draft: bool, contracts: bool) -> bytes:
    """Serialize rows through the canonical writer → byte-identical CLI CSV."""
    fd, name = tempfile.mkstemp(suffix=".csv")
    os.close(fd)  # close the handle so Windows lets us unlink it afterward
    tmp = Path(name)
    try:
        write_output_csv(rows, tmp, draft_mode=draft, include_contracts=contracts)
        return tmp.read_bytes()
    finally:
        tmp.unlink(missing_ok=True)


# --- LCARS theming ----------------------------------------------------------
# Two DS9-flavored palettes, switchable live from the sidebar. Each maps the
# same set of semantic CSS variables, so the stylesheet below just swaps values.
PALETTES: Dict[str, Dict[str, str]] = {
    # The Cardassian-built station look: warm ambers/bronze, deep red, teal.
    "Cardassian Ops": {
        "bg": "#000000", "panel": "#15110A", "text": "#F4E8D0",
        "primary": "#E8A33D", "accent": "#CC4422", "accent2": "#3FB6A8",
        "accent3": "#B5762A", "muted": "#7A6A4F",
    },
    # The classic TNG/DS9 Starfleet LCARS palette: orange, peach, lavender, blue.
    "Starfleet LCARS": {
        "bg": "#000000", "panel": "#0A0A14", "text": "#F2F2F2",
        "primary": "#FF9900", "accent": "#CC6666", "accent2": "#9999FF",
        "accent3": "#CC99CC", "muted": "#6F6F8F",
    },
}
DEFAULT_PALETTE = "Cardassian Ops"


def build_theme_css(p: Dict[str, str]) -> str:
    """LCARS reskin stylesheet for the given palette. Targets stable Streamlit
    test-ids / baseweb attributes so it survives version bumps."""
    return f"""
<style>
@import url('https://fonts.googleapis.com/css2?family=Antonio:wght@400;600;700&display=swap');
:root {{
  --lcars-bg: {p['bg']}; --lcars-panel: {p['panel']}; --lcars-text: {p['text']};
  --lcars-primary: {p['primary']}; --lcars-accent: {p['accent']};
  --lcars-accent2: {p['accent2']}; --lcars-accent3: {p['accent3']};
  --lcars-muted: {p['muted']};
  --lcars-font: 'Antonio','Oswald','Arial Narrow',sans-serif;
}}
.stApp {{ background: var(--lcars-bg); color: var(--lcars-text); }}
h1, h2, h3, h4 {{
  font-family: var(--lcars-font) !important; text-transform: uppercase;
  letter-spacing: 2px; color: var(--lcars-primary) !important;
}}
/* LCARS top bar (rendered by lcars_header) */
.lcars-topbar {{ display:flex; align-items:stretch; gap:8px; height:46px; margin:0 0 14px 0; }}
.lcars-topbar .cap {{ width:46px; background:var(--lcars-primary);
  border-radius:23px 0 0 23px; }}
.lcars-topbar .title {{ flex:0 0 auto; display:flex; align-items:center;
  padding:0 22px; background:var(--lcars-primary); color:#000;
  font-family:var(--lcars-font); font-weight:700; font-size:1.7rem;
  letter-spacing:3px; text-transform:uppercase; }}
.lcars-topbar .b1 {{ flex:1 1 auto; background:var(--lcars-accent3); }}
.lcars-topbar .b2 {{ width:70px; background:var(--lcars-accent); }}
.lcars-topbar .b3 {{ width:34px; background:var(--lcars-accent2);
  border-radius:0 23px 23px 0; }}
/* Sidebar = LCARS side panel */
[data-testid="stSidebar"] {{ background: var(--lcars-panel);
  border-right: 3px solid var(--lcars-primary); }}
/* Buttons -> LCARS pills */
.stButton > button, .stDownloadButton > button {{
  background: var(--lcars-primary); color:#000; border:none;
  border-radius: 18px; font-family: var(--lcars-font); font-weight:600;
  text-transform: uppercase; letter-spacing:1px; }}
.stButton > button:hover, .stDownloadButton > button:hover {{
  filter: brightness(1.15); color:#000; }}
.stButton > button:active {{ filter: brightness(0.9); }}
/* Inputs / selects / radios accent */
[data-baseweb="select"] > div, .stTextInput input, .stNumberInput input {{
  border-color: var(--lcars-accent3) !important; }}
[data-testid="stDataFrame"] {{ border: 2px solid var(--lcars-primary);
  border-radius: 8px; }}
/* Palette toggle (segmented control) -> connected LCARS pills */
[data-testid="stSegmentedControl"] button {{
  font-family: var(--lcars-font); text-transform: uppercase; letter-spacing:1px; }}
hr {{ border-color: var(--lcars-accent3); }}
.stCaption, [data-testid="stCaptionContainer"] {{ color: var(--lcars-muted) !important; }}
</style>
"""


def lcars_header(title: str) -> None:
    st.markdown(
        f'<div class="lcars-topbar"><span class="cap"></span>'
        f'<span class="title">{title}</span><span class="b1"></span>'
        f'<span class="b2"></span><span class="b3"></span></div>',
        unsafe_allow_html=True,
    )


# --- UI ---------------------------------------------------------------------

def main() -> None:
    st.set_page_config(page_title="VOSBall — Eval Browser", page_icon="⚾",
                       layout="wide")

    # Apply the LCARS reskin using the last-selected palette (the toggle below
    # updates st.session_state['palette'] and reruns, so this reads the new one).
    st.session_state.setdefault("palette", DEFAULT_PALETTE)
    st.markdown(build_theme_css(PALETTES[st.session_state["palette"]]),
                unsafe_allow_html=True)

    lcars_header("⚾ VOSBall · Eval Browser")
    st.caption(
        "Browse VOS player evaluations for any league. Reads the same "
        "`data/` and `config/` the CLI uses; scores with "
        "`vosball.services.evaluate_league`.")

    leagues = discover_leagues()
    if not leagues:
        st.error(f"No PlayerData files found in {DATA_DIR}. "
                 "Expected files like `data/PlayerData-wwoba.csv`.")
        return

    # --- Sidebar: run controls ---
    with st.sidebar:
        # Palette toggle (LCARS reskin). Keyed on session_state so the choice
        # persists and the theme injection above picks it up on rerun.
        st.segmented_control(
            "LCARS palette", list(PALETTES), key="palette",
            help="Switch the Deep Space 9 color scheme.")

        st.header("Evaluate")
        league = st.selectbox("League", leagues, index=0)

        mtime = player_data_mtime(league)
        if mtime:
            st.caption(f"Data updated: {datetime.fromtimestamp(mtime):%Y-%m-%d %H:%M}")

        scales = list(RATING_SCALES)
        default_scale = default_scale_for(league)
        rating_scale = st.radio(
            "Rating scale",
            scales,
            index=scales.index(default_scale) if default_scale in scales else 0,
            help="Scale of the component ratings in PlayerData-{league}.csv. "
                 "Most leagues are 20-80; some export 1-100 (remapped at load).",
        )

        draft = st.checkbox(
            "Draft mode", value=False,
            help="Enable draft-specific adjustments (readiness, draft age, "
                 "draft-role penalty). Adds draft columns to the output.")

        park_path = park_factors_path_for(league)
        apply_park = st.checkbox(
            "Apply park factors", value=park_path is not None,
            disabled=park_path is None,
            help=(f"Use config/{league}-park-factors.json."
                  if park_path else "No park-factors file shipped for this league."))

        contracts = st.checkbox(
            "Include contracts", value=False,
            help="Fetch live contract + extension data from the league API. "
                 "Requires the league's base URL in config/league_url.json and "
                 "network access.")
        if contracts:
            st.caption("⚠️ Contracts hit the league API over the network and need "
                       "`config/league_url.json`.")

        run = st.button("Run evaluation", type="primary", use_container_width=True)
        if st.button("Clear cache & re-score", use_container_width=True,
                     help="Force a fresh score, e.g. after re-fetching data."):
            st.cache_data.clear()
            st.session_state.pop("result", None)
            st.rerun()

    # Cache scoring so re-renders (sorting/filtering) don't re-score ~12k players.
    # data_mtime is part of the cache key (not used in the body): when the
    # PlayerData CSV changes on disk, the key changes and the league is re-scored.
    @st.cache_data(show_spinner=False)
    def cached_eval(league, rating_scale, draft, contracts, apply_park, data_mtime):
        return evaluate(league, rating_scale, draft, contracts, apply_park)

    # Persist the last run across reruns triggered by filter widgets.
    if run:
        with st.spinner(f"Scoring {league}…"):
            try:
                rows = cached_eval(league, rating_scale, draft, contracts, apply_park,
                                   player_data_mtime(league))
            except (ValueError, FileNotFoundError) as e:
                st.error(str(e))
                return
        st.session_state["result"] = {
            "rows": rows, "league": league, "draft": draft, "contracts": contracts,
        }

    result = st.session_state.get("result")
    if not result:
        st.info("Pick a league and options in the sidebar, then **Run evaluation**.")
        return

    rows = result["rows"]
    if not rows:
        st.warning("No players were scored for this league.")
        return

    df = pd.DataFrame(rows)
    for col in VOS_SCORE_COLUMNS:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    st.subheader(f"{result['league'].upper()} — {len(df)} players scored")

    # --- Filters ---
    with st.expander("Filters", expanded=True):
        c1, c2, c3 = st.columns(3)
        name_q = c1.text_input("Search name", "")
        pos_opts = sorted(x for x in df.get("Pos", pd.Series(dtype=str))
                          .dropna().astype(str).unique() if x)
        pos_sel = c2.multiselect("Position", pos_opts)
        lvl_opts = sorted(x for x in df.get("League_Level", pd.Series(dtype=str))
                          .dropna().astype(str).unique() if x)
        lvl_sel = c3.multiselect("League level", lvl_opts)

        score_ranges = {}
        score_cols_present = [c for c in VOS_SCORE_COLUMNS if c in df.columns
                              and df[c].notna().any()]
        if score_cols_present:
            cols = st.columns(len(score_cols_present))
            for col, widget in zip(score_cols_present, cols):
                lo = float(df[col].min())
                hi = float(df[col].max())
                if lo == hi:
                    continue
                score_ranges[col] = widget.slider(
                    col, min_value=round(lo, 1), max_value=round(hi, 1),
                    value=(round(lo, 1), round(hi, 1)))

    view = df
    if name_q.strip():
        view = view[view["Name"].astype(str).str.contains(name_q.strip(), case=False, na=False)]
    if pos_sel:
        view = view[view["Pos"].astype(str).isin(pos_sel)]
    if lvl_sel:
        view = view[view["League_Level"].astype(str).isin(lvl_sel)]
    for col, (lo, hi) in score_ranges.items():
        view = view[view[col].between(lo, hi)]

    # --- Column view ---
    show_all = st.toggle("Show all columns", value=False)
    if show_all:
        display_cols = list(df.columns)
    else:
        display_cols = [c for c in DEFAULT_COLUMNS if c in df.columns]

    st.caption(f"Showing {len(view)} of {len(df)} players "
               f"· {len(display_cols)} of {len(df.columns)} columns")
    st.dataframe(view[display_cols], use_container_width=True, hide_index=True)

    # --- Downloads ---
    d1, d2 = st.columns(2)
    full_csv = to_csv_bytes(rows, result["draft"], result["contracts"])
    d1.download_button(
        "⬇ Download full eval CSV", data=full_csv,
        file_name=f"evaluation_summary_{result['league']}.csv",
        mime="text/csv", use_container_width=True,
        help="Full canonical schema — byte-identical to run_vos.py output.")
    d2.download_button(
        "⬇ Download filtered view (CSV)",
        data=view[display_cols].to_csv(index=False).encode("utf-8"),
        file_name=f"eval_{result['league']}_filtered.csv",
        mime="text/csv", use_container_width=True,
        help="Just the rows/columns currently shown above.")


if __name__ == "__main__":
    main()
