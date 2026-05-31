"""VOSBall web UI — persistent export-status band.

Renders a compact, color-coded league export-status strip in the app's global
header (under the VOSBALL bar) on every page. Status comes from the same
preflight / check_exports path the bulk runners use — no live refresh: the check
runs once per session (cached) and a ⟳ button re-runs it.

A pure consumer: imports preflight.check_leagues and reads config files. Nothing
in vosball/ or the CLI tools changes.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Tuple

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import streamlit as st  # noqa: E402

from preflight import check_leagues  # noqa: E402  (stdlib-only deps; safe to import)

CONFIG_DIR = ROOT / "config"
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
    keys = [k for k in _read_json(LEAGUE_URL_PATH) if not k.startswith("_")]
    if not keys:
        keys = [k for k in _read_json(LEAGUE_SETTINGS_PATH) if not k.startswith("_")]
    return sorted(keys)


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

def _chip_color_css(leagues: List[str], results: Dict[str, dict]) -> str:
    """Per-key CSS so each chip button is colored by status (green/amber). Keyed
    via Streamlit's `st-key-<key>` class; the 🟢/🟠 label emoji is the fallback."""
    rules = ["<style>"]
    for lg in leagues:
        ok = bool((results.get(lg) or {}).get("skip"))
        rules.append(
            f'.st-key-chip_{lg} button {{ background:{"#46B36B" if ok else "#E8A33D"} '
            f'!important; color:#000 !important; border:none !important; '
            f'font-family:var(--lcars-font, sans-serif); font-weight:700; '
            f'letter-spacing:1px; }}')
    rules.append("</style>")
    return "".join(rules)


def render_band() -> None:
    """Clickable export-status strip for the global header (call from app.main).

    Each league is a chip-button that navigates to its League Hub; the trailing
    ⟳ re-checks. Cached per session, so only the first load hits the network.
    """
    leagues = configured_leagues()
    if not leagues:
        return
    st.session_state.setdefault("exports_nonce", 0)

    with st.spinner("Checking exports…"):
        data = export_status(tuple(leagues), st.session_state["exports_nonce"])
    results = data.get("results", {})

    st.markdown(_chip_color_css(leagues, results), unsafe_allow_html=True)
    cols = st.columns(len(leagues) + 1)
    for col, lg in zip(cols, leagues):
        r = results.get(lg) or {}
        ok = bool(r.get("skip"))
        clicked = col.button(
            f"{'🟢' if ok else '🟠'} {lg.upper()}", key=f"chip_{lg}",
            use_container_width=True,
            help=f"{'Current' if ok else 'Needs export'}: {r.get('reason', '')}"
                 "\n\nClick to open this league's hub.")
        if clicked:
            # Defer the actual navigation to app.main (after st.navigation is
            # built) — switch_page from the pre-nav chrome isn't reliable. The
            # click already triggered this rerun, so the flag flows downstream
            # in the same run.
            st.session_state["selected_league"] = lg
            st.session_state["_pending_page"] = "league"
    if cols[-1].button("⟳", key="recheck_exports", use_container_width=True,
                       help="Re-check league export status now (one API call per league)."):
        st.session_state["exports_nonce"] += 1
        st.rerun()

    n_need = sum(1 for lg in leagues if not (results.get(lg) or {}).get("skip"))
    checked = (data.get("checked_at") or "")[11:16]
    st.caption(f"{n_need} need export · checked {checked or '—'} — click a league "
               "to open its hub; ⟳ to re-check.")
