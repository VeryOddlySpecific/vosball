"""VOSBall web UI — Prospect Board.

A ranked prospect board for the loaded league, a pure consumer of
prospect_rankings.py's in-process functions over the eval rows the Eval Browser
already produced (st.session_state['result']). Defaults to your org's farm (an
All-orgs option widens it). Offline by default; an opt-in toggle fetches /players
service time to refine rookie eligibility.

prospect_score = ceiling (VOS_*) × age-for-level × position/role.
Nothing in vosball/ or the CLI tools changes.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import streamlit as st  # noqa: E402
import pandas as pd  # noqa: E402

import prospect_rankings as pr  # noqa: E402  (stdlib-only deps; safe to import)

CONFIG_DIR = ROOT / "config"
DATA_DIR = ROOT / "data"

SCORE_SOURCES = {"Reach (upside)": "VOS_Reach", "Career": "VOS_Career",
                 "Blended": "VOS_Blended"}
POOLS = ["prospects", "free_agents", "all"]

DISPLAY_COLS = [  # (header, source key)
    ("Rank", "prospect_rank_overall"), ("OrgRk", "prospect_rank_org"),
    ("Name", "Name"), ("Age", "Age"), ("Pos", "Pos"), ("Role", "projected_role"),
    ("Org", "Org"), ("Level", "League_Level"), ("Cur", "vos"),
    ("Ceiling", "vos_pot"), ("Score", "prospect_score"),
    ("Age×", "m_age"), ("Pos×", "m_pos_role"),
]


# --- helpers ----------------------------------------------------------------

def _read_json(path: Path) -> dict:
    try:
        if path.exists():
            d = json.loads(path.read_text(encoding="utf-8"))
            return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        pass
    return {}


def _my_org(lg: str) -> Optional[str]:
    e = _read_json(CONFIG_DIR / "league_settings.json").get(lg)
    if isinstance(e, dict) and isinstance(e.get("org"), str) and e["org"].strip():
        return e["org"].strip()
    return None


def _base_url(lg: str) -> Optional[str]:
    u = _read_json(CONFIG_DIR / "league_url.json").get(lg)
    return u if isinstance(u, str) and u.strip() else None


def _data_mtime(lg: str) -> float:
    p = DATA_DIR / f"PlayerData-{lg}.csv"
    return p.stat().st_mtime if p.exists() else 0.0


@st.cache_data(show_spinner=False)
def build_board(_eval_rows, eval_sig, pot_col, pool, use_service_time, base_url):
    """Rank prospects from the eval rows (no file writes). `_eval_rows` is
    un-hashed; `eval_sig` keys the cache."""
    players_lookup = None
    err = None
    if use_service_time and base_url:
        try:
            players_lookup = pr.build_players_lookup(base_url)
        except Exception as e:  # noqa: BLE001 — network/parse failure → fail open
            err = f"{type(e).__name__}: {e}"
    try:
        out = pr.compute_prospect_rows(
            list(_eval_rows), "VOS_Score", pot_col, players_lookup,
            90.0, 7.0, 2.0, 0.04, 0.06, 0.70, 1.15, True, 0.93, 1.04, pool)
        pr.assign_rankings(out)
    except Exception as e:  # noqa: BLE001
        return {"rows": [], "service_time": False, "error": f"{type(e).__name__}: {e}"}
    return {"rows": out, "service_time": players_lookup is not None, "error": err}


def _board_df(rows: List[Dict[str, Any]]) -> pd.DataFrame:
    df = pd.DataFrame(rows)
    for _, key in DISPLAY_COLS:
        if key not in df.columns:
            df[key] = None
    if "prospect_rank_overall" in df.columns:
        df = df.sort_values("prospect_rank_overall", kind="stable")
    return df


# --- page -------------------------------------------------------------------

def page() -> None:
    result = st.session_state.get("result")
    if not result or not result.get("rows"):
        st.info("Run an evaluation on the **Eval Browser** page first — the "
                "prospect board ranks that league's scored players.")
        return
    lg, rows = result["league"], result["rows"]

    with st.sidebar:
        st.header("Prospect Board")
        src_label = st.radio("Ceiling source", list(SCORE_SOURCES),
                             help="Which VOS score is the prospect ceiling. Reach "
                                  "(the reach-the-majors model) fits prospect upside.")
        pool = st.selectbox("Pool", POOLS, index=0,
                            help="prospects = org-affiliated & rookie-eligible; "
                                 "free_agents = unsigned; all = every non-MLB player.")
        use_st = st.checkbox("Use MLB service time (network)", value=False,
                            help="Fetch /players to exclude players past rookie "
                                 "eligibility (>90 MLB days or ≥7 pro years). Needs "
                                 "config/league_url.json + network; off = all non-ML.")

    pot_col = SCORE_SOURCES[src_label]
    eval_sig = "|".join(str(result.get(k)) for k in
                        ("league", "rating_scale", "draft", "apply_park", "contracts"))
    eval_sig += f"|{_data_mtime(lg)}"
    with st.spinner("Ranking prospects…"):
        board = build_board(rows, eval_sig, pot_col, pool, use_st, _base_url(lg))
    if board.get("error"):
        st.warning(f"Prospect ranking issue: {board['error']}")
    brows = board.get("rows", [])
    if not brows:
        st.warning("No prospects found for this league/pool.")
        return

    st.subheader(f"🌱 {lg.upper()} prospect board — {len(brows)} prospects")

    # Org scope (default my farm), keyed per league so a switch re-defaults.
    orgs = sorted({str(r.get("Org", "")).strip() for r in brows if str(r.get("Org", "")).strip()})
    options = ["All orgs"] + orgs
    my = _my_org(lg)
    default_idx = options.index(my) if my in options else 0
    scope = st.selectbox("Organization", options, index=default_idx,
                         key=f"prospect_org_{lg}")

    df = _board_df(brows)
    if scope != "All orgs":
        df = df[df["Org"].astype(str) == scope]

    show_all = st.toggle("Show all columns", value=False)
    if show_all:
        view_cols = list(df.columns)
        disp = df
    else:
        view_cols = [k for _, k in DISPLAY_COLS if k in df.columns]
        disp = df[view_cols].rename(columns={k: h for h, k in DISPLAY_COLS})

    st.caption(
        f"Showing {len(df)} prospects · score = ceiling ({src_label}) × "
        f"age-for-level × position/role · pool: {pool} · "
        + ("service-time eligibility applied" if board.get("service_time")
           else "all non-ML included (service time off)")
        + " · click a row for the player card")

    event = st.dataframe(disp, hide_index=True, use_container_width=True,
                         on_select="rerun", selection_mode="single-row",
                         key=f"prospect_table_{lg}_{scope}")
    sel = list(event.selection.rows) if (event and event.selection) else []
    if sel and sel[0] < len(df):
        pid = str(df.iloc[sel[0]].get("ID", "")).strip()
        if pid and st.session_state.get("_last_prospect_sel") != pid:
            st.session_state["_last_prospect_sel"] = pid
            st.session_state["card_pid"] = pid
            pages = st.session_state.get("_pages", {})
            if "card" in pages:
                st.switch_page(pages["card"])
    elif not sel:
        st.session_state["_last_prospect_sel"] = None
