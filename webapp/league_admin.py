"""VOSBall web UI — League Admin.

A top-level admin screen for managing leagues. Surfaces, in one place, every
per-league setting the suite consumes plus a coverage matrix of which config
files each league has.

Phases (ticket 0003):
  • Phase 1 — read-only browse + coverage matrix.  [done]
  • Phase 2 — edit the scalar/connection settings (URL, token, the six
    league_settings keys).                          [this phase]
  • Phase 3 — structured editors (league_ids, orgs, divisions, gm-slack).
  • Phase 4 — add / remove league + provisioning.

All persistence goes through ``core/league_registry.py`` (atomic, backed up).
The scalar edits are split into a UI-free :func:`apply_edits` so the write path
is unit-testable without Streamlit. Tokens are never displayed in full.
"""
from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path
from typing import List, Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent
for _p in (ROOT, ROOT / "core"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import pandas as pd  # noqa: E402
import streamlit as st  # noqa: E402

from league_registry import (  # noqa: E402
    LeagueRegistry, LeagueConfig, RegistryError, VALID_RATING_SCALES,
)
from league_provision import provision_league, ProvisionError  # noqa: E402

DATA_DIR = ROOT / "data"

# Columns shown in the coverage matrix, in display order.
_COVERAGE_COLS = [
    ("url", "URL"), ("token", "Token"), ("settings", "Settings"),
    ("league_ids", "League IDs"), ("orgs", "Orgs"), ("divisions", "Divisions"),
    ("gm_slack", "GM·Slack"), ("teams", "Teams"), ("park_factors", "Parks"),
    ("playerdata", "PlayerData"),
]

_FLASH_KEY = "_admin_flash"


def _flash(msg: str) -> None:
    """Stash a success banner and rerun so the whole page reflects the write."""
    st.session_state[_FLASH_KEY] = msg
    st.rerun()


# --- token-age tracking ------------------------------------------------------
# Tokens are bare UUIDs with no embedded date, so we record the day a token is
# set *through this app* in the (gitignored) ui-settings file and reason about
# expiry from that. Tokens set outside the app have no record → "age unknown".

UI_SETTINGS_PATH = Path(__file__).resolve().parent / ".ui_settings.json"
TOKEN_TTL_DAYS = 90
TOKEN_WARN_DAYS = 75
_TOKEN_DATES_KEY = "token_set_dates"
_LEVEL_ICON = {"ok": "🟢", "warn": "🟠", "expired": "🔴", "unknown": "⚪"}


def token_age_note(set_date_iso: Optional[str], today_iso: str) -> Tuple[str, str]:
    """Pure: (human note, level) for a token set on ``set_date_iso``. Level is
    one of ok / warn / expired / unknown. Tokens age out at ~90 days."""
    if not set_date_iso:
        return ("age unknown (set outside the app)", "unknown")
    try:
        age = (date.fromisoformat(today_iso) - date.fromisoformat(set_date_iso)).days
    except ValueError:
        return ("age unknown", "unknown")
    left = TOKEN_TTL_DAYS - age
    if left < 0:
        return (f"set {age}d ago — likely EXPIRED ({-left}d past {TOKEN_TTL_DAYS})", "expired")
    if age >= TOKEN_WARN_DAYS:
        return (f"set {age}d ago — expires in ~{left}d", "warn")
    return (f"set {age}d ago — ~{left}d left", "ok")


def _load_ui_settings() -> dict:
    try:
        if UI_SETTINGS_PATH.exists():
            data = json.loads(UI_SETTINGS_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
    except (OSError, ValueError):
        pass
    return {}


def _record_token_date(slug: str) -> None:
    """Stamp today's date as when ``slug``'s token was set (merge-write)."""
    settings = _load_ui_settings()
    dates = settings.get(_TOKEN_DATES_KEY)
    if not isinstance(dates, dict):
        dates = {}
    dates[slug] = date.today().isoformat()
    settings[_TOKEN_DATES_KEY] = dates
    try:
        UI_SETTINGS_PATH.write_text(json.dumps(settings, indent=2), encoding="utf-8")
    except OSError:
        pass  # read-only dir — the reminder just won't persist


def _token_set_date(slug: str) -> Optional[str]:
    dates = _load_ui_settings().get(_TOKEN_DATES_KEY)
    return dates.get(slug) if isinstance(dates, dict) else None


# --- write path (UI-free, unit-testable) ------------------------------------

def apply_edits(
    reg: LeagueRegistry,
    slug: str,
    *,
    url: str,
    token: str,
    rating_scale: str,
    org: str,
    year: Optional[int],
    min_comp: Optional[float],
    game_version: str,
    sim_time: str,
) -> List[str]:
    """Diff the submitted scalar values against the league's current config and
    persist only what changed. Returns the list of changed field names (empty
    if nothing changed). Raises :class:`RegistryError` on invalid input.

    Conventions: a blank ``token`` means "keep the current token". For the text
    fields, an emptied value is written through (clears the setting); for
    ``year`` / ``min_comp``, ``None`` means "no value".
    """
    cur = reg.load(slug)
    new = LeagueConfig(slug=slug)
    changed: List[str] = []

    def _text(field: str, value: str, current: Optional[str]) -> None:
        if value.strip() != (current or ""):
            setattr(new, field, value.strip())
            changed.append(field)

    _text("url", url, cur.url)

    if token.strip() and token.strip() != (cur.token or ""):
        new.token = token.strip()
        changed.append("token")

    seed_scale = cur.rating_scale or "20-80"
    if rating_scale != seed_scale:
        new.rating_scale = rating_scale
        changed.append("rating_scale")

    _text("org", org, cur.org)

    if year != cur.year:
        new.year = year
        changed.append("year")
    if min_comp != cur.min_comp:
        new.min_comp = min_comp
        changed.append("min_comp")

    _text("game_version", game_version, cur.game_version)
    _text("sim_time", sim_time, cur.sim_time)

    if changed:
        reg.save(new)
    return changed


# --- structured-editor parsing (UI-free, unit-testable) ---------------------

def _cell(v) -> str:
    """Normalise a data_editor cell to a stripped string ('' for blank/NaN)."""
    if isinstance(v, str):
        return v.strip()
    if v is None:
        return ""
    try:
        if pd.isna(v):
            return ""
    except (TypeError, ValueError):
        pass
    return str(v).strip()


def parse_league_ids(rows) -> dict:
    """Editor rows [{Level, League IDs}] → {level: [int, …]}. Blank levels are
    dropped; non-integer IDs raise. Underscore meta keys are allowed."""
    out: dict = {}
    for r in rows:
        level = _cell(r.get("Level"))
        if not level:
            continue
        lids = []
        for tok in _cell(r.get("League IDs")).split(","):
            tok = tok.strip()
            if not tok:
                continue
            try:
                lids.append(int(tok))
            except ValueError:
                raise RegistryError(
                    f"League IDs for {level!r} must be integers — got {tok!r}.")
        out[level] = lids
    return out


def parse_orgs(rows) -> list:
    """Editor rows [{Org}] → ['Org', …], dropping blanks (order preserved)."""
    return [name for r in rows if (name := _cell(r.get("Org")))]


def parse_divisions(rows) -> dict:
    """Editor rows [{Sub-league, Division, Team}] → {sub: {div: [team, …]}}.
    Rows missing any of the three fields are dropped."""
    out: dict = {}
    for r in rows:
        sub = _cell(r.get("Sub-league"))
        div = _cell(r.get("Division"))
        team = _cell(r.get("Team"))
        if not (sub and div and team):
            continue
        out.setdefault(sub, {}).setdefault(div, []).append(team)
    return out


def parse_gm_slack(rows) -> dict:
    """Editor rows [{Team, Handle}] → {team: handle}. Blank teams dropped;
    a blank handle is kept (matches the existing files' empty-string entries)."""
    out: dict = {}
    for r in rows:
        team = _cell(r.get("Team"))
        if team:
            out[team] = _cell(r.get("Handle"))
    return out


# --- read helpers ------------------------------------------------------------

def _playerdata_exists(slug: str) -> bool:
    return (DATA_DIR / f"PlayerData-{slug}.csv").exists()


def _mask_token(tok: str) -> str:
    """Fingerprint a token without exposing it (UUIDs are 36 chars)."""
    if not tok:
        return "—"
    return f"{tok[:4]}…{tok[-4:]}" if len(tok) > 8 else "set"


def _yn(flag: bool) -> str:
    return "✅" if flag else "—"


def _coverage_frame(reg: LeagueRegistry, slugs: List[str]) -> pd.DataFrame:
    rows = []
    for slug in slugs:
        st_map = reg.files_status(slug)
        st_map["playerdata"] = _playerdata_exists(slug)
        rows.append({"League": slug.upper(),
                     **{label: _yn(st_map[key]) for key, label in _COVERAGE_COLS}})
    return pd.DataFrame(rows).set_index("League")


# --- edit form ---------------------------------------------------------------

def _render_edit_form(reg: LeagueRegistry, slug: str) -> None:
    cfg = reg.load(slug)

    with st.form(f"edit_{slug}", border=True):
        st.subheader(f"{slug.upper()} — edit settings")
        c1, c2 = st.columns(2)

        with c1:
            st.markdown("**Connection**")
            url = st.text_input("API base URL", value=cfg.url or "",
                                placeholder="https://statsplus.net/<slug>/api")
            token = st.text_input(
                "API token", value="", type="password",
                placeholder="leave blank to keep current",
                help="StatsPlus Preferences token (UUID). Valid ~90 days. "
                     "Stored in the gitignored statsplus_tokens.json.")
            if cfg.token:
                note, level = token_age_note(_token_set_date(slug), date.today().isoformat())
                st.caption(f"Current token: `{_mask_token(cfg.token)}` (explicit) · "
                           f"{_LEVEL_ICON[level]} {note}")
            elif cfg.uses_default_token:
                st.caption("Current token: using shared `_default`")
            else:
                st.caption("Current token: — (none → cookie auth fallback)")

        with c2:
            st.markdown("**Metadata**")
            scale_idx = (list(VALID_RATING_SCALES).index(cfg.rating_scale)
                         if cfg.rating_scale in VALID_RATING_SCALES else 0)
            rating_scale = st.selectbox("Rating scale", list(VALID_RATING_SCALES),
                                        index=scale_idx)
            org = st.text_input("Your org", value=cfg.org or "")
            year = st.number_input("Season year", value=cfg.year, step=1,
                                   min_value=1900, max_value=2200, format="%d",
                                   placeholder="(unset)")
            min_comp = st.number_input(
                "Min comp",
                value=float(cfg.min_comp) if cfg.min_comp is not None else None,
                step=1.0, min_value=0.0, max_value=100.0,
                placeholder="(default 50.0)")
            game_version = st.text_input("Game version", value=cfg.game_version or "",
                                         placeholder="e.g. OOTP 27")
            sim_time = st.text_input("Sim time", value=cfg.sim_time or "")

        submitted = st.form_submit_button("💾 Save changes", type="primary")

    if submitted:
        year_val = int(year) if year is not None else None
        mc_val = float(min_comp) if min_comp is not None else None
        try:
            changed = apply_edits(
                reg, slug, url=url, token=token, rating_scale=rating_scale,
                org=org, year=year_val, min_comp=mc_val,
                game_version=game_version, sim_time=sim_time,
            )
        except RegistryError as e:
            st.error(f"Not saved — {e}")
            return
        if "token" in changed:
            _record_token_date(slug)
        if changed:
            _flash(f"Saved {slug.upper()}: {', '.join(changed)}.")
        else:
            st.info("No changes to save.")


# --- structured editors ------------------------------------------------------

def _render_structured_editors(reg: LeagueRegistry, slug: str) -> None:
    cfg = reg.load(slug)

    st.markdown("##### Structured settings")
    st.caption("Add/remove rows freely; each section saves to its own file.")

    # --- minor-league ID map ---
    with st.expander(f"Minor-league ID map ({len(cfg.league_ids or {})} levels)"):
        df = pd.DataFrame(
            [{"Level": lvl, "League IDs": ", ".join(map(str, lids))}
             for lvl, lids in (cfg.league_ids or {}).items()],
            columns=["Level", "League IDs"])
        edited = st.data_editor(
            df, num_rows="dynamic", use_container_width=True, key=f"ids_ed_{slug}",
            column_config={
                "Level": st.column_config.TextColumn("Level", help="ML, AAA, … or _international"),
                "League IDs": st.column_config.TextColumn("League IDs", help="comma-separated integers"),
            })
        if st.button("💾 Save ID map", key=f"ids_save_{slug}"):
            try:
                ids = parse_league_ids(edited.to_dict("records"))
            except RegistryError as e:
                st.error(f"Not saved — {e}")
            else:
                reg.save(LeagueConfig(slug=slug, league_ids=ids))
                _flash(f"Saved {slug.upper()} ID map ({len(ids)} levels).")

    # --- orgs ---
    with st.expander(f"Orgs ({len(cfg.orgs or [])})"):
        df = pd.DataFrame({"Org": cfg.orgs or []}, columns=["Org"])
        edited = st.data_editor(df, num_rows="dynamic", use_container_width=True,
                                key=f"orgs_ed_{slug}")
        if st.button("💾 Save orgs", key=f"orgs_save_{slug}"):
            orgs = parse_orgs(edited.to_dict("records"))
            reg.save(LeagueConfig(slug=slug, orgs=orgs))
            _flash(f"Saved {slug.upper()} orgs ({len(orgs)}).")

    # --- divisions ---
    with st.expander("Divisions"):
        rows = []
        for sub, divs in (cfg.divisions or {}).items():
            if isinstance(divs, dict):
                for div_name, teams in divs.items():
                    for team in (teams or []):
                        rows.append({"Sub-league": sub, "Division": div_name, "Team": team})
        df = pd.DataFrame(rows, columns=["Sub-league", "Division", "Team"])
        st.caption("One row per team — regrouped into {sub-league: {division: [teams]}} on save.")
        edited = st.data_editor(df, num_rows="dynamic", use_container_width=True,
                                key=f"div_ed_{slug}")
        if st.button("💾 Save divisions", key=f"div_save_{slug}"):
            div = parse_divisions(edited.to_dict("records"))
            reg.save(LeagueConfig(slug=slug, divisions=div))
            n = sum(len(v) for v in div.values())
            _flash(f"Saved {slug.upper()} divisions ({len(div)} sub-leagues, {n} divisions).")

    # --- gm-slack ---
    with st.expander(f"GM · Slack ({len(cfg.gm_slack or {})})"):
        df = pd.DataFrame(
            [{"Team": t, "Handle": h} for t, h in (cfg.gm_slack or {}).items()],
            columns=["Team", "Handle"])
        edited = st.data_editor(df, num_rows="dynamic", use_container_width=True,
                                key=f"slack_ed_{slug}")
        if st.button("💾 Save GM·Slack", key=f"slack_save_{slug}"):
            mapping = parse_gm_slack(edited.to_dict("records"))
            reg.save(LeagueConfig(slug=slug, gm_slack=mapping))
            _flash(f"Saved {slug.upper()} GM·Slack map ({len(mapping)}).")


# --- add / remove league -----------------------------------------------------

def _run_initial_fetch(slug: str, box) -> bool:
    """Pull the initial PlayerData via the existing /ratings flow. Returns True
    on success. Mirrors league.py's _run_fetch event loop."""
    from fetch import fetch_league_ratings
    ok = False
    for ev in fetch_league_ratings(slug):
        kind = ev.get("type")
        if kind == "progress":
            box.write(ev["msg"])
        elif kind == "done":
            box.write("✅ " + ev["msg"])
            ok = True
        else:
            box.write("❌ " + ev["msg"])
    return ok


def _render_add_league(reg: LeagueRegistry) -> None:
    with st.expander("➕ Add a league", expanded=not reg.slugs()):
        st.caption("Supply the StatsPlus URL + token. We pull `/teams` and "
                   "`/ballparks` to build the teams & park-factors files and the "
                   "org list, then write the registry entries.")
        with st.form("add_league"):
            c1, c2 = st.columns(2)
            with c1:
                slug = st.text_input("Slug", placeholder="e.g. ndl",
                                     help="lowercase, starts with a letter")
                url = st.text_input("API base URL",
                                    placeholder="https://statsplus.net/<slug>/api")
                token = st.text_input("API token", type="password",
                                      placeholder="UUID from S+ Preferences")
            with c2:
                org = st.text_input("Your org (optional)")
                rating_scale = st.selectbox("Rating scale", list(VALID_RATING_SCALES))
                year = st.number_input("Season year (optional)", value=None, step=1,
                                       min_value=1900, max_value=2200, format="%d",
                                       placeholder="(unset)")
            fetch_now = st.checkbox(
                "Fetch initial PlayerData now (slow — the /ratings export can take "
                "minutes)", value=False)
            submitted = st.form_submit_button("🚀 Provision league", type="primary")

        if submitted:
            slug = (slug or "").strip().lower()
            settings = {"org": org.strip() or None, "rating_scale": rating_scale,
                        "year": int(year) if year is not None else None}
            with st.status(f"Provisioning {slug or '(no slug)'}…", expanded=True) as box:
                try:
                    box.write("Contacting StatsPlus (/teams, /ballparks)…")
                    manifest = provision_league(reg, slug, (url or "").strip(),
                                                (token or "").strip() or None,
                                                settings=settings)
                except (ProvisionError, RegistryError) as e:
                    box.update(label="Provisioning failed", state="error")
                    st.error(f"Not provisioned — {e}")
                    return
                box.write(f"teams: {manifest['teams_count']} · "
                          f"parks: {manifest['parks_count']} · "
                          f"orgs: {manifest['orgs_count']} · "
                          f"ML lid: {manifest['ml_lid']}")
                for w in manifest["warnings"]:
                    box.write("⚠ " + w)
                if fetch_now:
                    box.write("Pulling initial PlayerData…")
                    if not _run_initial_fetch(slug, box):
                        box.update(label="Provisioned, but initial fetch failed",
                                   state="error")
                        st.warning("Config written, but the PlayerData fetch failed "
                                   "— retry from the League Hub.")
                        st.session_state["selected_league"] = slug
                        return
                box.update(label=f"{slug.upper()} provisioned ✓", state="complete")
            if (token or "").strip():
                _record_token_date(slug)
            st.session_state["selected_league"] = slug
            _flash(f"Provisioned {slug.upper()} "
                   f"({manifest['teams_count']} teams, {manifest['orgs_count']} orgs).")


def _render_remove_league(reg: LeagueRegistry, slugs: List[str]) -> None:
    with st.expander("🗑 Remove a league"):
        st.caption("Strips the league from all shared config files and deletes "
                   "its per-league files. A timestamped backup of every touched "
                   "file is kept in `config/.backups/`.")
        target = st.selectbox("League to remove", slugs, format_func=str.upper,
                              key="remove_league_select")
        remove_large = st.checkbox(
            "Also delete the large data files (teams-…, park-factors)",
            value=False, key=f"rm_large_{target}")
        confirm = st.text_input(
            f"Type **{target}** to confirm", key=f"rm_confirm_{target}",
            placeholder=target)
        if st.button("Remove league", type="primary", disabled=(confirm.strip() != target)):
            try:
                touched = reg.remove(target, remove_large=remove_large)
            except RegistryError as e:
                st.error(f"Not removed — {e}")
                return
            if st.session_state.get("selected_league") == target:
                st.session_state["selected_league"] = None
            _flash(f"Removed {target.upper()} ({len(touched)} files touched). "
                   "Backups in config/.backups/. "
                   "PlayerData CSV (if any) left in place.")


def _render_generated_panel(reg: LeagueRegistry, slug: str) -> None:
    status = reg.files_status(slug)
    with st.container(border=True):
        st.markdown("**Generated / imported data**")
        st.caption("Built by the provisioning pipeline & fetch (Phase 4) — "
                   "not hand-edited here.")
        g1, g2, g3 = st.columns(3)
        g1.metric("teams-…json", "present" if status["teams"] else "missing")
        g2.metric(f"{slug}-park-factors", "present" if status["park_factors"] else "missing")
        g3.metric("PlayerData CSV", "present" if _playerdata_exists(slug) else "missing")


def page() -> None:
    st.header("⚙️ League Admin")
    st.caption("Every per-league setting the suite reads, in one place. "
               "Edit connection & metadata below; structured settings and "
               "add/remove arrive in upcoming phases.")

    flash = st.session_state.pop(_FLASH_KEY, None)
    if flash:
        st.success(flash)

    reg = LeagueRegistry()
    slugs = reg.slugs()

    _render_add_league(reg)
    if slugs:
        _render_remove_league(reg, slugs)

    if not slugs:
        st.info("No leagues configured yet — add one above.")
        return

    st.markdown("#### Coverage")
    st.caption("Which config files exist for each league.")
    st.dataframe(_coverage_frame(reg, slugs), use_container_width=True)

    st.divider()

    active = st.session_state.get("selected_league")
    default_idx = slugs.index(active) if active in slugs else 0
    chosen = st.selectbox("Manage a league", slugs, index=default_idx,
                          format_func=str.upper, key="admin_league_select")

    _render_edit_form(reg, chosen)
    _render_structured_editors(reg, chosen)
    _render_generated_panel(reg, chosen)
