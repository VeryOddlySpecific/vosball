"""VOSBall web UI — Farm Value page (org farm systems, ranked).

Values every org's farm system and ranks them, so your team shows up as
"🏆 Ranked 4th of 30" with its prospect detail beneath. League- and team-siloed,
a consumer of the farm_value core (farm_value.build_farm_values) over the same
in-process prospect board the Prospects page builds.

Model: farm_value = prospect_score × VPC; org total = top-12 + 25%-weighted tail;
rank by that total. The **ranking is always available offline** (VPC is a global
scalar, so it doesn't affect order). **Dollar** figures need a VPC calibrated from
MLB contracts, which is read from the latest on-disk eval
({league}/eval/evaluation_summary_*.csv) — if that eval was generated with
contracts you get $, otherwise the page shows a unitless farm index instead.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import streamlit as st  # noqa: E402
import pandas as pd  # noqa: E402

import prospect_rankings as pr  # noqa: E402  (in-process board — same as Prospects)
import depth_chart as dc  # noqa: E402  (read_eval / find_latest_eval for VPC)
import farm_value as fv  # noqa: E402  (the scoring + ranking core)
from state import active_result, active_league  # noqa: E402  (per-league silo)
from scoring import autorun_result  # noqa: E402  (auto-score on first visit)
from depth import user_org_for_league  # noqa: E402  (default org = your team)

DATA_DIR = ROOT / "data"

# VPC calibration column. VOS_Career is the most defensible $/projection anchor
# for MLB salaries (per FARM_VALUE_README) and is always present in the eval CSV
# — unlike the legacy VOS_Potential alias the CLI defaults to.
VPC_POT_COL = "VOS_Career"


# --- data layer -------------------------------------------------------------

def _data_mtime(lg: str) -> float:
    p = DATA_DIR / f"PlayerData-{lg}.csv"
    return p.stat().st_mtime if p.exists() else 0.0


def _latest_eval_path(league: str) -> Optional[Path]:
    """Newest evaluation_summary_{league}_*.csv (for VPC), or None if none on
    disk — the core then falls back to a unitless index."""
    try:
        return dc.find_latest_eval(league, None, None)
    except FileNotFoundError:
        return None


@st.cache_data(show_spinner=False)
def build_farm_context(_eval_rows, eval_sig, _vpc_eval_path):
    """Build the ranked org farm values + per-player rows (no file writes).

    The prospect board is computed in-process from the silo rows (same call the
    Prospects page uses, offline / no service-time). VPC is calibrated from the
    latest on-disk eval (``_vpc_eval_path``); a no-contracts eval (or none) makes
    the core fall back to a unitless index with the ranking intact. Cached on
    ``eval_sig`` (which folds in the VPC eval's name+mtime). Returns artifacts or
    {'error': msg}.
    """
    try:
        board = pr.compute_prospect_rows(
            list(_eval_rows), "VOS_Score", "VOS_Reach", None,
            90.0, 7.0, 2.0, 0.04, 0.06, 0.70, 1.15, True, 0.93, 1.04, "prospects")
        pr.assign_rankings(board)

        vpc_rows: List[Dict[str, str]] = []
        vpc_name: Optional[str] = None
        if _vpc_eval_path is not None:
            vpc_rows = dc.read_eval(_vpc_eval_path)
            vpc_name = _vpc_eval_path.name

        res = fv.build_farm_values(board, vpc_rows, pot_col=VPC_POT_COL,
                                   players_lookup=None)
        return {
            "org_rows": res["org_rows"],
            "player_rows": res["player_rows"],
            "vpc_ok": res["vpc_ok"],
            "vpc_base": res["vpc_base"],
            "vpc_eval_name": vpc_name,
            "n_board": len(board),
        }
    except Exception as e:  # noqa: BLE001 — surface, don't crash the page
        return {"error": f"{type(e).__name__}: {e}"}


# --- render helpers ---------------------------------------------------------

def _ordinal(n: int) -> str:
    if 11 <= (n % 100) <= 13:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def _fmt_val(v: Any, vpc_ok: bool) -> str:
    """Dollars when VPC calibrated, else a plain model-index number."""
    try:
        x = float(v or 0)
    except (TypeError, ValueError):
        return "—"
    if not vpc_ok:
        return f"{x:,.0f}"
    if x >= 1e6:
        return f"${x / 1e6:.1f}M"
    if x >= 1e3:
        return f"${x / 1e3:.0f}K"
    return f"${x:,.0f}"


def _league_table(org_rows: List[Dict[str, Any]], selected: str,
                  value_label: str, vpc_ok: bool) -> pd.DataFrame:
    rows = []
    for r in org_rows:
        rows.append({
            "": "◀" if r["Org"] == selected else "",
            "Rank": r["rank"],
            "Org": r["Org"],
            value_label: _fmt_val(r["farm_value_total"], vpc_ok),
            "Top 12": _fmt_val(r["farm_value_top12"], vpc_ok),
            "Tail": _fmt_val(r["farm_value_tail_weighted"], vpc_ok),
            "#Prospects": r["num_farm_players"],
        })
    return pd.DataFrame(rows)


def _team_detail(player_rows: List[Dict[str, Any]], org: str,
                 value_label: str, vpc_ok: bool, n: int = 25) -> pd.DataFrame:
    mine = sorted((p for p in player_rows if str(p.get("Org")) == org),
                  key=lambda p: -float(p.get("farm_value") or 0))[:n]
    return pd.DataFrame([{
        "OrgRk": p.get("prospect_rank_org", ""),
        "Name": p.get("Name", ""),
        "Age": p.get("Age", ""),
        "Pos": p.get("Pos", ""),
        "Level": p.get("League_Level", ""),
        "Cur": round(float(p.get("vos") or 0), 1),
        "Ceiling": round(float(p.get("vos_pot") or 0), 1),
        "Score": round(float(p.get("prospect_score") or 0), 1),
        value_label: _fmt_val(p.get("farm_value"), vpc_ok),
    } for p in mine])


# --- page -------------------------------------------------------------------

def page() -> None:
    result = active_result() or autorun_result(active_league())
    if not result or not result.get("rows"):
        st.info("Run an evaluation on the **Eval Browser** page first — farm "
                "values are built from that league's scored players.")
        return
    lg, rows = result["league"], result["rows"]

    with st.sidebar:
        st.header("Farm Value")

    vpc_path = _latest_eval_path(lg)
    eval_sig = "|".join(str(result.get(k)) for k in
                        ("league", "rating_scale", "draft", "apply_park", "contracts"))
    eval_sig += f"|{_data_mtime(lg)}"
    if vpc_path is not None:
        eval_sig += f"|{vpc_path.name}|{vpc_path.stat().st_mtime}"

    with st.spinner("Valuing farm systems…"):
        ctx = build_farm_context(rows, eval_sig, vpc_path)
    if "error" in ctx:
        st.error(ctx["error"])
        return
    org_rows: List[Dict[str, Any]] = ctx["org_rows"]
    if not org_rows:
        st.warning(f"No farm prospects found for {lg.upper()}.")
        return

    vpc_ok = ctx["vpc_ok"]
    value_label = "Farm $" if vpc_ok else "Farm index"
    num_orgs = org_rows[0]["num_orgs"]

    # Org scope (default your team), keyed per league so a switch re-defaults.
    orgs_sorted = sorted(str(r["Org"]) for r in org_rows)
    my = user_org_for_league(lg)
    default_idx = orgs_sorted.index(my) if my in orgs_sorted else 0
    with st.sidebar:
        org = st.selectbox("Organization", orgs_sorted, index=default_idx,
                           key=f"farm_org_{lg}")
        st.caption(f"{num_orgs} orgs · {ctx['n_board']} farm players ranked")

    st.subheader(f"💲 {lg.upper()} farm value — {org}")

    sel = next((r for r in org_rows if str(r["Org"]) == org), None)
    if sel is None:
        st.warning(f"{org} has no ranked farm prospects.")
        return

    # Headline: league rank + farm value for the selected team.
    m = st.columns(4)
    m[0].metric("League rank", f"{_ordinal(sel['rank'])} of {num_orgs}")
    m[1].metric(value_label, _fmt_val(sel["farm_value_total"], vpc_ok))
    m[2].metric("Top-12 value", _fmt_val(sel["farm_value_top12"], vpc_ok))
    m[3].metric("Farm players", sel["num_farm_players"])

    if vpc_ok:
        st.caption(f"Dollar values calibrated (VPC ${ctx['vpc_base']:,.0f}/proj-VOS "
                   f"from `{ctx['vpc_eval_name']}`). Org total = top-12 + 25% tail.")
    else:
        reason = (f"`{ctx['vpc_eval_name']}` has no MLB contract data"
                  if ctx["vpc_eval_name"] else "no eval file on disk")
        st.caption(f"⚠️ Showing a unitless **farm index** (rank is exact) — {reason}, "
                   "so dollars can't be calibrated. Run a contracts-enabled eval "
                   "(`vos_v2.py --contracts` / `run_vos` with contracts) to get $.")

    # Full league ranking.
    st.markdown("### 🏆 League farm rankings")
    st.dataframe(_league_table(org_rows, org, value_label, vpc_ok),
                 hide_index=True, use_container_width=True)

    # Selected team's prospect detail.
    st.markdown(f"### 🌱 {org} — top farm assets")
    st.dataframe(_team_detail(ctx["player_rows"], org, value_label, vpc_ok),
                 hide_index=True, use_container_width=True)
    st.caption("Score = prospect_score (ceiling × age-for-level × position/role); "
               f"{value_label} = score × VPC. Ranked within the org by value.")
