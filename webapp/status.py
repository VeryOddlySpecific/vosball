"""VOSBall web UI — Ops Status (home page).

The landing screen: a band of color-coded league tiles showing each league's
StatsPlus export status, plus a details table. Status comes from the same
preflight / check_exports path the bulk runners use — no live refresh; a manual
"Re-check exports" button re-runs it.

A pure consumer: imports preflight.check_leagues and reads config files. Nothing
in vosball/ or the CLI tools changes.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, date
from pathlib import Path
from typing import Any, Dict, List, Tuple

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import streamlit as st  # noqa: E402
import pandas as pd  # noqa: E402

from preflight import check_leagues  # noqa: E402  (stdlib-only deps; safe to import)

CONFIG_DIR = ROOT / "config"
DATA_DIR = ROOT / "data"
LEAGUE_URL_PATH = CONFIG_DIR / "league_url.json"
LEAGUE_SETTINGS_PATH = CONFIG_DIR / "league_settings.json"


# --- data layer -------------------------------------------------------------

def _read_json(path: Path) -> dict:
    try:
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        pass
    return {}


def configured_leagues() -> List[str]:
    """The leagues the user is in — keys of league_url.json (what check_exports
    defaults to), falling back to league_settings.json keys."""
    urls = _read_json(LEAGUE_URL_PATH)
    keys = [k for k in urls if not k.startswith("_")]
    if not keys:
        keys = [k for k in _read_json(LEAGUE_SETTINGS_PATH) if not k.startswith("_")]
    return sorted(keys)


def league_settings() -> dict:
    return _read_json(LEAGUE_SETTINGS_PATH)


def freshness(league: str) -> str:
    """Short tag for the PlayerData CSV's age (mirrors check_exports.py)."""
    path = DATA_DIR / f"PlayerData-{league}.csv"
    if not path.exists():
        return "no file"
    mtime = datetime.fromtimestamp(path.stat().st_mtime).date()
    today = date.today()
    if mtime == today:
        return "updated today"
    return f"{(today - mtime).days}d old ({mtime})"


@st.cache_data(show_spinner=False)
def export_status(leagues: Tuple[str, ...], nonce: int) -> Dict[str, Any]:
    """Run the /exports preflight for `leagues`. `nonce` busts the cache on a
    manual re-check; `checked_at` is stamped here so it reflects the real check
    time and only changes on an actual re-run."""
    checked_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        res = check_leagues(list(leagues))
        return {"checked_at": checked_at,
                "results": {L: {"skip": bool(r.skip), "reason": r.reason}
                            for L, r in res.items()}}
    except Exception as e:  # noqa: BLE001 — per-league failures already fail open
        return {"checked_at": checked_at, "error": str(e), "results": {}}


# --- render -----------------------------------------------------------------

_TILE_CSS = """
<style>
.lcars-tiles { display:flex; flex-wrap:wrap; gap:8px; margin:4px 0 18px; }
.lcars-tile { flex:1 1 130px; min-width:130px; border-radius:14px; padding:10px 16px;
  color:#000; font-family:var(--lcars-font, sans-serif); }
.lcars-tile .lg { font-weight:700; font-size:1.25rem; letter-spacing:2px; text-transform:uppercase; }
.lcars-tile .stt { font-size:.78rem; letter-spacing:1px; text-transform:uppercase; opacity:.85; }
.lcars-tile.ok { background:#46B36B; }
.lcars-tile.need { background:#E8A33D; }
</style>
"""


def _tiles_html(leagues: List[str], results: Dict[str, dict]) -> str:
    cells = []
    for lg in leagues:
        r = results.get(lg)
        ok = bool(r and r.get("skip"))
        cls = "ok" if ok else "need"
        label = "Current" if ok else "Needs export"
        cells.append(f'<div class="lcars-tile {cls}"><div class="lg">{lg}</div>'
                     f'<div class="stt">{label}</div></div>')
    return f'<div class="lcars-tiles">{"".join(cells)}</div>'


def _table_df(leagues: List[str], results: Dict[str, dict], settings: dict) -> pd.DataFrame:
    rows = []
    for lg in leagues:
        r = results.get(lg, {})
        cfg = settings.get(lg) or {}
        ok = bool(r.get("skip"))
        rows.append({
            "League": lg,
            "Version": cfg.get("game_version", "?"),
            "Status": "Current" if ok else "Needs export",
            "Data": freshness(lg),
            "Sim time": cfg.get("sim_time", "?"),
            "Reason": r.get("reason", "—"),
        })
    return pd.DataFrame(rows)


def page() -> None:
    st.markdown(_TILE_CSS, unsafe_allow_html=True)
    st.subheader("🛰️ Ops Status — league export check")

    leagues = configured_leagues()
    if not leagues:
        st.error("No leagues configured. Expected `config/league_url.json`.")
        return

    st.session_state.setdefault("exports_nonce", 0)
    if st.button("⟳ Re-check exports", help="Re-run the /exports preflight now "
                 "(hits the league API once per league)."):
        st.session_state["exports_nonce"] += 1
        st.rerun()

    with st.spinner("Checking /exports…"):
        data = export_status(tuple(leagues), st.session_state["exports_nonce"])
    results = data.get("results", {})
    if data.get("error"):
        st.warning(f"Export check failed: {data['error']}")

    # League status block-band.
    st.markdown(_tiles_html(leagues, results), unsafe_allow_html=True)

    n_ok = sum(1 for lg in leagues if results.get(lg, {}).get("skip"))
    n_need = len(leagues) - n_ok
    st.caption(f"**{n_ok} current · {n_need} need export** · Last checked: "
               f"{data.get('checked_at', '—')} — not live; click **Re-check** to refresh.")

    st.dataframe(_table_df(leagues, results, league_settings()),
                 hide_index=True, use_container_width=True)
