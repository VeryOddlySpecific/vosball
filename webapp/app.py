"""VOSBall — local web UI (multipage Streamlit app).

A thin browser front end over the VOS evaluation engine. It is a pure *consumer*
of the layered `vosball` package: it calls vosball.services.evaluate_league()
(the same UI-agnostic seam the CLI uses) and renders the returned rows. Two
pages share one scored result (kept in st.session_state) and the LCARS theme:

  • Eval Browser — sortable/filterable/searchable table + canonical CSV export
    (byte-identical to run_vos.py via vosball.reporting.write_output_csv).
  • Player Card — single-player detail view, rendered entirely from the row
    evaluate_league already returns (no extra data loading).

Run it with:

    py -m streamlit run webapp/app.py        (or double-click run_ui.bat)

Nothing in vosball/ changes for the UI to exist — see LOGIC_UPDATE_PROCESS.md §4.
"""
from __future__ import annotations

import json
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
from vosball.data import (  # noqa: E402
    RATING_SCALES, DEFAULT_RATING_SCALE, load_player_data,
)
from vosball.engine import HITTER_POSITIONS  # noqa: E402  (pure constant)

# Reuse what_if's rating field-group definitions (display label -> PlayerData
# columns) so the player card's scouted-ratings block stays in lockstep with the
# CLI tools. what_if imports cleanly (only vosball.* + stdlib, no side effects).
import what_if as wi  # noqa: E402

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


@st.cache_data(show_spinner=False)
def raw_player_rows(league: str, rating_scale: str,
                    data_mtime: float) -> Dict[str, Dict[str, str]]:
    """Raw PlayerData rows (scouted ratings) for a league, keyed by player ID.

    Loaded at the same rating_scale the eval used, so the displayed ratings match
    what the engine scored. data_mtime is part of the cache key so a fresh fetch
    re-reads the file (mirrors the score cache).
    """
    rows = load_player_data(DATA_DIR, league, rating_scale=rating_scale)
    return {str(r.get("ID", "")): r for r in rows}


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


# --- Persisted UI preferences -----------------------------------------------
# A small local settings file so choices (palette, and future per-module prefs)
# survive a restart. Lives next to the app, gitignored, best-effort — a failed
# read/write just falls back to defaults rather than breaking the UI.
SETTINGS_PATH = Path(__file__).resolve().parent / ".ui_settings.json"


def load_ui_settings() -> Dict[str, Any]:
    try:
        if SETTINGS_PATH.exists():
            data = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
    except (OSError, ValueError):
        pass
    return {}


def save_ui_setting(key: str, value: Any) -> None:
    """Merge one preference into the settings file (keeps other keys intact)."""
    settings = load_ui_settings()
    settings[key] = value
    try:
        SETTINGS_PATH.write_text(json.dumps(settings, indent=2), encoding="utf-8")
    except OSError:
        pass  # read-only dir etc. — preference just won't persist this session


def _persist_palette() -> None:
    """on_change callback for the palette toggle (session_state already updated)."""
    save_ui_setting("palette", st.session_state.get("palette", DEFAULT_PALETTE))


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


# --- App shell (multipage) --------------------------------------------------

# Page objects are (re)created each rerun inside main(); stashed here so the
# Eval Browser's "Open player card" bridge can target the card page via
# st.switch_page within the same run.
_PAGES: Dict[str, Any] = {}


def main() -> None:
    st.set_page_config(page_title="VOSBall", page_icon="⚾", layout="wide")

    # Apply the LCARS reskin using the last-selected palette. Seed from the
    # persisted setting on first load; the toggle below updates
    # st.session_state['palette'] (and writes it back) and reruns.
    saved_palette = load_ui_settings().get("palette", DEFAULT_PALETTE)
    if saved_palette not in PALETTES:
        saved_palette = DEFAULT_PALETTE
    st.session_state.setdefault("palette", saved_palette)
    st.markdown(build_theme_css(PALETTES[st.session_state["palette"]]),
                unsafe_allow_html=True)

    lcars_header("⚾ VOSBall")

    # Global chrome: palette toggle sits above the page nav, on every page.
    with st.sidebar:
        st.segmented_control(
            "LCARS palette", list(PALETTES), key="palette",
            on_change=_persist_palette,
            help="Switch the Deep Space 9 color scheme. Your choice is remembered.")
        st.divider()

    eval_page = st.Page(eval_browser_page, title="Eval Browser", icon="📊",
                        default=True)
    card_page = st.Page(player_card_page, title="Player Card", icon="🪪")
    _PAGES["card"] = card_page
    st.navigation([eval_page, card_page]).run()


# --- Page: Eval Browser -----------------------------------------------------

def eval_browser_page() -> None:
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
            "rating_scale": rating_scale,
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
               f"· {len(display_cols)} of {len(df.columns)} columns · "
               "click a row to open that player's card")
    # Key the table on the filter signature: changing any filter yields a fresh
    # table with no carried-over selection, so typing in a filter can never drag
    # a stale row-selection into a spurious navigation. Within a stable filter
    # the selection persists, so a click still registers.
    filt_sig = "|".join([
        str(result["league"]), name_q.strip(), ",".join(pos_sel), ",".join(lvl_sel),
        ";".join(f"{k}={v}" for k, v in sorted(score_ranges.items())),
    ])
    event = st.dataframe(
        view[display_cols], use_container_width=True, hide_index=True,
        on_select="rerun", selection_mode="single-row",
        key=f"eval_table::{filt_sig}")

    # Row click -> jump to the Player Card. selection.rows are positional indices
    # into the data as passed (independent of the user's column sort), so map via
    # view.iloc. Guard with _last_table_sel so returning to this page (selection
    # still set) doesn't re-trigger navigation.
    sel = list(event.selection.rows) if (event and event.selection) else []
    if sel and sel[0] < len(view):
        pid = str(view.iloc[sel[0]].get("ID", "")).strip()
        if pid and st.session_state.get("_last_table_sel") != pid:
            st.session_state["_last_table_sel"] = pid
            st.session_state["card_pid"] = pid
            st.switch_page(_PAGES["card"])
    elif not sel:
        st.session_state["_last_table_sel"] = None

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


# --- Page: Player Card ------------------------------------------------------

def _player_label(r: Dict[str, Any]) -> str:
    return (f"{r.get('Name', '?')} · {(r.get('Pos') or '').strip()} · "
            f"{r.get('Team', '')}  (#{r.get('ID', '')})")


def _num(v: Any, prec: int = 2) -> str:
    try:
        return f"{float(v):.{prec}f}"
    except (TypeError, ValueError):
        return "—"


def _is_pitcher_row(r: Dict[str, Any]) -> bool:
    if str(r.get("Pitching_Ability_Score", "")).strip():
        return True
    return (r.get("Pos") or "").strip().upper() in {"SP", "RP", "CL", "P"}


def _ratings_df(raw: Dict[str, str], fields: List, with_pot: bool):
    """Build a small DataFrame from a what_if field group, dropping blank rows.

    fields are what_if's (label, col[, pot_col]) tuples. Returns None if every
    value is blank (so the caller can skip the block entirely).
    """
    data = []
    for entry in fields:
        if with_pot:
            lbl, cur_col = entry[0], entry[1]
            pot_col = entry[2] if len(entry) == 3 else None
            cur = wi._fmt_val(raw.get(cur_col, ""))
            pot = wi._fmt_val(raw.get(pot_col, "")) if pot_col else "—"
            if cur == "—" and pot == "—":
                continue
            data.append({"Rating": lbl, "Cur": cur, "Pot": pot})
        else:
            lbl, col = entry[0], entry[1]
            v = wi._fmt_val(raw.get(col, ""))
            if v == "—":
                continue
            data.append({"Rating": lbl, "Val": v})
    return pd.DataFrame(data) if data else None


def _ratings_into(container, label: str, raw: Dict[str, str],
                  fields: List, with_pot: bool) -> None:
    df = _ratings_df(raw, fields, with_pot)
    if df is None:
        return
    container.caption(label)
    container.dataframe(df, hide_index=True, use_container_width=True)


def player_card_page() -> None:
    result = st.session_state.get("result")
    if not result or not result.get("rows"):
        st.info("Run an evaluation on the **Eval Browser** page first, then pick "
                "a player here.")
        return
    rows = result["rows"]
    league = result["league"]
    labels = [_player_label(r) for r in rows]
    row_by_label = {lbl: r for lbl, r in zip(labels, rows)}
    id_to_label = {str(r.get("ID", "")): lbl for lbl, r in zip(labels, rows)}

    with st.sidebar:
        st.header("Player Card")
        st.caption(f"{league.upper()} · {len(rows)} scored")

    # Preselect the player the bridge sent us (or the last one viewed).
    pre = st.session_state.get("card_pid")
    idx = labels.index(id_to_label[pre]) if pre in id_to_label else 0
    pick = st.selectbox("Player", labels, index=idx)
    row = row_by_label[pick]
    st.session_state["card_pid"] = str(row.get("ID", ""))

    _render_card(row, result)


def _render_card(row: Dict[str, Any], result: Dict[str, Any]) -> None:
    is_pit = _is_pitcher_row(row)

    st.subheader(f"{row.get('Name', '?')} — {(row.get('Pos') or '').strip()}")
    bio = [b for b in (
        f"Age {row.get('Age')}" if str(row.get("Age", "")).strip() else "",
        str(row.get("Team", "")).strip(),
        f"Org {row.get('Org')}" if str(row.get("Org", "")).strip() else "",
        f"Level {row.get('League_Level')}" if str(row.get("League_Level", "")).strip() else "",
    ) if b]
    if bio:
        st.caption(" · ".join(bio))
    tiers = [f"{lbl}: {row.get(col)}" for lbl, col in (
        ("Tier", "VOS_Tier"), ("Potential", "VOS_Potential_Tier"),
        ("Ceiling", "Ceiling_Tier")) if str(row.get(col, "")).strip()]
    if tiers:
        st.caption(" · ".join(tiers))

    # Headline VOS metrics
    m = st.columns(4)
    m[0].metric("VOS Reach", _num(row.get("VOS_Reach")))
    m[1].metric("VOS Career", _num(row.get("VOS_Career")))
    m[2].metric("VOS Blended", _num(row.get("VOS_Blended")))
    m[3].metric("VOS Ceiling", _num(row.get("VOS_Ceiling")))

    # Component scores
    st.markdown("**Component scores**")
    if is_pit:
        c = st.columns(3)
        c[0].metric("Ability", _num(row.get("Pitching_Ability_Score")))
        c[1].metric("Ability (Pot)", _num(row.get("Pitching_Ability_Potential")))
        c[2].metric("Arsenal", _num(row.get("Pitching_Arsenal_Score")))
    else:
        c = st.columns(4)
        c[0].metric("Batting", _num(row.get("Batting_Score")))
        c[1].metric("Batting (Pot)", _num(row.get("Batting_Potential")))
        c[2].metric("Defense", _num(row.get("Defense_Score")))
        c[3].metric("Baserunning", _num(row.get("Baserunning_Score")))

    # Adjustments
    adj_specs = [("Development", "Development_Adj"), ("Age", "Age_Adj"),
                 ("Personality", "Personality_Adj")]
    if result.get("draft"):
        adj_specs += [("Readiness", "Readiness_Adj"), ("Draft age", "Draft_Age_Adj"),
                      ("Draft RP pen.", "Draft_RP_Penalty")]
    st.markdown("**Adjustments**")
    ac = st.columns(len(adj_specs))
    for col, (lbl, key) in zip(ac, adj_specs):
        col.metric(lbl, _num(row.get(key)))

    # Hitter-only: projected WAR, insights, positional scores
    if not is_pit:
        if str(row.get("Arch_Career_WAR", "")).strip():
            st.markdown("**Projected career WAR** — archetype average for this "
                        "profile, *not* a per-player forecast")
            w = st.columns(4)
            w[0].metric("Career WAR", _num(row.get("Arch_Career_WAR"), 1))
            w[1].metric("Career WAR (hi)", _num(row.get("Arch_Career_WAR_Hi"), 1))
            w[2].metric("Remaining WAR", _num(row.get("Remaining_WAR"), 1))
            debut = str(row.get("Proj_Debut_Age", "")).strip()
            w[3].metric("Proj. debut age", debut or "—")

        insights = [f"**{lbl}:** {row.get(col)}" for lbl, col in (
            ("Current pos", "Current_Position"), ("Projected pos", "Projected_Position"),
            ("Top score", "Projected_Top_Score"), ("2nd score", "Projected_Second_Score"),
            ("Margin", "Projected_Margin"), ("Margin tier", "Projected_Margin_Tier"),
            ("Viable positions", "Projected_Viable_Positions"),
            ("Viable list", "Projected_Viable_Pos_List"),
            ("Ideal value", "Ideal_Value"),
        ) if str(row.get(col, "")).strip()]
        if insights:
            st.markdown("**Projection insights**")
            st.markdown("  ·  ".join(insights))

        st.markdown("**Positional scores** (Current / Potential)")
        ideal_cur = (row.get("Current_Position") or "").strip()
        ideal_pot = (row.get("Projected_Position") or "").strip()
        prows = []
        for pos in HITTER_POSITIONS:
            if pos == ideal_cur and pos == ideal_pot:
                marker = "◀ current & projected"
            elif pos == ideal_cur:
                marker = "◀ current"
            elif pos == ideal_pot:
                marker = "◀ projected"
            else:
                marker = ""
            prows.append({"Pos": pos, "Current": _num(row.get(f"{pos}_Score")),
                          "Potential": _num(row.get(f"{pos}_Potential")), " ": marker})
        st.dataframe(pd.DataFrame(prows), hide_index=True, use_container_width=True)

    # Scouted ratings — raw PlayerData, loaded at the run's rating scale so the
    # numbers line up with the scores above. Reuses what_if's field groups.
    raw = raw_player_rows(
        result["league"], result.get("rating_scale", DEFAULT_RATING_SCALE),
        player_data_mtime(result["league"])).get(str(row.get("ID", "")))
    if raw:
        with st.expander("Scouted ratings", expanded=True):
            left, right = st.columns(2)
            if is_pit:
                _ratings_into(left, "Ability", raw, wi.PITCHER_ABILITY_FIELDS, True)
                _ratings_into(left, "Splits", raw, wi.PITCHER_SPLIT_FIELDS, False)
                _ratings_into(right, "Pitches", raw, wi.PITCH_FIELDS, True)
                _ratings_into(right, "Personality", raw, wi.PERSONALITY_FIELDS, False)
            else:
                _ratings_into(left, "Batting", raw, wi.HITTER_RATING_FIELDS, True)
                _ratings_into(left, "Position ratings", raw, wi.POS_RATING_COLS, True)
                _ratings_into(right, "Defense", raw, wi.HITTER_DEFENSE_FIELDS, False)
                _ratings_into(right, "Baserunning", raw, wi.HITTER_BASERUNNING_FIELDS, False)
                _ratings_into(right, "Personality", raw, wi.PERSONALITY_FIELDS, False)

    # Park / injury
    park_bits = []
    park_name = str(row.get("Park_Name", "")).strip()
    if park_name and park_name != "N/A":
        applied = row.get("Park_Applied") in (True, "True", "true")
        park_bits.append(f"Park: {park_name} ({'applied' if applied else 'not applied'})")
    if str(row.get("Prone", "")).strip():
        park_bits.append(f"Injury prone: {row.get('Prone')}")
    if park_bits:
        st.caption(" · ".join(park_bits))

    # Contract (only when contracts were fetched for this run)
    if result.get("contracts"):
        _render_contract(row)


def _render_contract(row: Dict[str, Any]) -> None:
    def ci(col: str) -> int:
        try:
            return int(float(str(row.get(col, "")).strip() or 0))
        except (TypeError, ValueError):
            return 0

    st.markdown("**Contract**")
    years = ci("Contract_years")
    if years < 1:
        st.caption("No active contract (free agent / unsigned).")
        return
    salaries = [ci(f"Contract_salary{i}") for i in range(min(years, 15))]
    total = sum(salaries)
    aav = total // years if years else 0
    cur_yr = ci("Contract_current_year")
    yr_descr = f"Year {cur_yr} of {years}" if cur_yr else f"{years} yr(s)"
    no_trade = ci("Contract_no_trade") == 1
    st.write(f"{yr_descr} · total ${total:,} · AAV ${aav:,} · "
             f"no-trade: {'yes' if no_trade else 'no'}")
    if any(salaries):
        st.caption("Salaries: " + " / ".join(f"${s:,}" for s in salaries))


if __name__ == "__main__":
    main()
