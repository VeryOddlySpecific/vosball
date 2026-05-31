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

_BAND_CSS = """
<style>
.lcars-band { display:flex; flex-wrap:wrap; gap:6px; align-items:center; margin:0 0 12px; }
.lcars-chip { border-radius:11px; padding:3px 11px; color:#000;
  font-family:var(--lcars-font, sans-serif); font-weight:700; font-size:.82rem;
  letter-spacing:1.5px; text-transform:uppercase; }
.lcars-chip.ok { background:#46B36B; }
.lcars-chip.need { background:#E8A33D; }
.lcars-band .ck { color:var(--lcars-muted, #888); font-size:.72rem;
  letter-spacing:.5px; margin-left:4px; }
</style>
"""


def _band_html(leagues: List[str], results: Dict[str, dict], checked_at: str) -> str:
    chips = []
    for lg in leagues:
        r = results.get(lg) or {}
        ok = bool(r.get("skip"))
        word = "Current" if ok else "Needs export"
        reason = (r.get("reason") or "").replace('"', "'")
        chips.append(
            f'<span class="lcars-chip {"ok" if ok else "need"}" '
            f'title="{word}: {reason}">{lg}</span>')
    n_need = sum(1 for lg in leagues if not (results.get(lg) or {}).get("skip"))
    note = f"{n_need} need export"
    if checked_at:
        note += f" · checked {checked_at[11:16]}"
    chips.append(f'<span class="ck">{note} — hover a chip for why</span>')
    return f'<div class="lcars-band">{"".join(chips)}</div>'


def render_band() -> None:
    """Compact export-status strip for the global header (call from app.main)."""
    leagues = configured_leagues()
    if not leagues:
        return
    st.markdown(_BAND_CSS, unsafe_allow_html=True)
    st.session_state.setdefault("exports_nonce", 0)

    band_col, btn_col = st.columns([13, 1])
    with btn_col:
        if st.button("⟳", key="recheck_exports",
                     help="Re-check league export status now (one API call per league)."):
            st.session_state["exports_nonce"] += 1
            st.rerun()

    with st.spinner("Checking exports…"):
        data = export_status(tuple(leagues), st.session_state["exports_nonce"])
    band_col.markdown(
        _band_html(leagues, data.get("results", {}), data.get("checked_at", "")),
        unsafe_allow_html=True)
