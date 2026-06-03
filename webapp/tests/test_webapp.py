#!/usr/bin/env python3
"""Regression tests for the VOSBall web UI (webapp/).

Standalone, stdlib + streamlit only (no pytest), mirroring tests/test_golden.py:

    py webapp/tests/test_webapp.py        # verify; exits non-zero on any failure

Two layers:
  • unit  — the pure, Streamlit-free helpers in app.py (league discovery, rating-
            scale defaults, park-factor path resolution, and — from Phase 1 on —
            the per-league result silo).
  • smoke — boots the whole multipage app under Streamlit's AppTest harness and
            asserts it renders without raising and registers its pages. The
            network-touching export-status preflight is stubbed so the suite
            stays offline and fast.

This is the Phase-0 baseline, captured BEFORE the per-league-silo refactor: any
later phase that breaks app boot or a helper contract fails loudly here. Append
new checks (siloing, active-league plumbing, auto-run) as those phases land.
"""
from __future__ import annotations

import sys
import traceback
import types
from pathlib import Path

HERE = Path(__file__).resolve().parent
WEBAPP = HERE.parent
ROOT = WEBAPP.parent
for _p in (ROOT, ROOT / "core", WEBAPP):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))  # ROOT: vosball pkg; core/: app-consumed modules; WEBAPP: app + sibling pages

# Stub the network-touching export-status check before any AppTest boot, so the
# smoke test never hits the league API. status.py did `from preflight import
# check_leagues`, binding its own copy — patch the name on `status`, not on
# `preflight`, so status.export_status() resolves the stub.
import status  # noqa: E402


def _stub_check_leagues(leagues):
    return {lg: types.SimpleNamespace(skip=True, reason="stubbed (offline test)")
            for lg in leagues}


status.check_leagues = _stub_check_leagues

import app  # noqa: E402  (safe to import: side-effect-free; main() is __main__-guarded)
import fetch  # noqa: E402  (fresh-ratings pull generator — exercised fully offline)


# --- unit: pure helpers -----------------------------------------------------

def test_discover_leagues_sorted_and_backed_by_data():
    leagues = app.discover_leagues()
    assert isinstance(leagues, list), "discover_leagues must return a list"
    assert leagues == sorted(leagues), "discover_leagues must be sorted"
    for lg in leagues:  # every discovered slug must have a backing PlayerData CSV
        assert (app.DATA_DIR / f"PlayerData-{lg}.csv").exists(), \
            f"discovered league {lg!r} has no PlayerData CSV"


def test_default_scale_for():
    # ndl is the known 1-100 league; everything else falls back to engine-native.
    assert app.default_scale_for("ndl") == "1-100"
    assert app.default_scale_for("wwoba") == app.DEFAULT_RATING_SCALE
    assert app.default_scale_for("does-not-exist") == app.DEFAULT_RATING_SCALE


def test_park_factors_path_for():
    # Returns a Path only when the file exists; None otherwise.
    for lg in app.discover_leagues():
        p = app.park_factors_path_for(lg)
        assert p is None or p.exists(), f"park path for {lg} points at a missing file"
    assert app.park_factors_path_for("no-such-league-xyz") is None


def test_player_data_mtime_absent_is_zero():
    assert app.player_data_mtime("no-such-league-xyz") == 0.0
    for lg in app.discover_leagues():
        assert app.player_data_mtime(lg) > 0.0


# --- smoke: full app boots under AppTest ------------------------------------

def test_app_boots_and_registers_all_pages():
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_file(str(WEBAPP / "app.py"), default_timeout=60)
    at.run()
    assert not at.exception, f"app.py raised on boot: {at.exception}"
    # AppTest's session_state routes attribute access to key lookups (no .get /
    # .to_dict), but `in` and indexing work.
    assert "_pages" in at.session_state, \
        "app did not register any pages in session_state['_pages']"
    pages = at.session_state["_pages"]
    expected = {"home", "eval", "card", "depth", "prospects", "free_agents",
                "trade_targets", "farm_value", "league"}
    assert expected.issubset(set(pages)), \
        f"missing page registrations: {expected - set(pages)}"


def test_cold_boot_shows_league_select_no_autorun():
    """Cold boot lands on the Home league-select screen — no league is active and
    nothing is auto-scored until the user picks one."""
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_file(str(WEBAPP / "app.py"), default_timeout=60)
    at.run()
    assert not at.exception
    assert ("selected_league" not in at.session_state
            or not at.session_state["selected_league"]), \
        "cold boot must not pre-select a league"
    assert "results" not in at.session_state or not at.session_state["results"], \
        "cold boot must not auto-run any league"
    assert any("Select a league" in h.value for h in at.header), \
        "Home league-select screen not shown on cold boot"


def test_home_pick_sets_active_league():
    """Clicking a league tile on Home sets it as the active league (then opens
    its hub)."""
    from streamlit.testing.v1 import AppTest
    import home

    at = AppTest.from_file(str(WEBAPP / "app.py"), default_timeout=60)
    at.run()
    lg = home.configured_leagues()[0]
    btn = next(b for b in at.button if b.key == f"home_open_{lg}")
    btn.click().run()
    assert not at.exception
    assert at.session_state["selected_league"] == lg


# --- Phase 1: per-league result silo ----------------------------------------

def _silo_script(body: str) -> str:
    """An inline AppTest script that puts the repo on sys.path, imports app (whose
    main() is __main__-guarded, so importing renders nothing), and runs `body`.
    Lets us exercise the silo helpers without scoring real data."""
    setup = (
        "import sys\n"
        f"for _p in ({str(ROOT)!r}, {str(ROOT / 'core')!r}, {str(WEBAPP)!r}):\n"
        "    if _p not in sys.path:\n"
        "        sys.path.insert(0, _p)\n"
        "import app\n"
    )
    return setup + body


def test_silo_retains_multiple_leagues():
    """Two leagues scored in one session each keep their own entry; the legacy
    pointer tracks the most-recently-set one (Phase-1 back-compat)."""
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_string(_silo_script(
        'app.set_result("aaa", {"rows": [1], "league": "aaa"})\n'
        'app.set_result("bbb", {"rows": [2], "league": "bbb"})\n'
    ), default_timeout=30)
    at.run()
    assert not at.exception, f"silo script raised: {at.exception}"
    results = at.session_state["results"]
    assert set(results) == {"aaa", "bbb"}, f"silo missing leagues: {set(results)}"
    assert results["aaa"]["rows"] == [1]
    assert results["bbb"]["rows"] == [2]
    # Legacy pointer tracks the most recently set league (today's behavior).
    assert at.session_state["result"]["league"] == "bbb"


def test_get_result_isolates_leagues():
    """get_result returns each league's own data and None for an unscored one."""
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_string(_silo_script(
        'app.set_result("aaa", {"rows": [1], "league": "aaa"})\n'
        'app.set_result("bbb", {"rows": [2], "league": "bbb"})\n'
        'import streamlit as st\n'
        'st.session_state["_probe"] = ('
        '    app.get_result("aaa")["league"],'
        '    app.get_result("bbb")["league"],'
        '    app.get_result("zzz"),'
        ')\n'
    ), default_timeout=30)
    at.run()
    assert not at.exception, f"silo script raised: {at.exception}"
    assert at.session_state["_probe"] == ("aaa", "bbb", None)


def test_active_result_resolves_selected_league():
    """active_result() follows st.session_state['selected_league'] and returns
    None when the active league hasn't been scored (Phase-2 resolution)."""
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_string(_silo_script(
        'import streamlit as st\n'
        'app.set_result("aaa", {"rows": [1], "league": "aaa"})\n'
        'app.set_result("bbb", {"rows": [2], "league": "bbb"})\n'
        'st.session_state["selected_league"] = "aaa"\n'
        'st.session_state["_p_a"] = app.active_result()["league"]\n'
        'st.session_state["selected_league"] = "bbb"\n'
        'st.session_state["_p_b"] = app.active_result()["league"]\n'
        'st.session_state["selected_league"] = "zzz"\n'
        'st.session_state["_p_z"] = app.active_result()\n'
    ), default_timeout=30)
    at.run()
    assert not at.exception, f"silo script raised: {at.exception}"
    assert at.session_state["_p_a"] == "aaa"
    assert at.session_state["_p_b"] == "bbb"  # switching the pointer switches data
    assert at.session_state["_p_z"] is None   # unscored active league → None


def test_clear_results_empties_silo_and_pointer():
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_string(_silo_script(
        'app.set_result("aaa", {"rows": [1], "league": "aaa"})\n'
        'app.set_result("bbb", {"rows": [2], "league": "bbb"})\n'
        'app.clear_results()\n'
    ), default_timeout=30)
    at.run()
    assert not at.exception, f"silo script raised: {at.exception}"
    assert "results" not in at.session_state, "silo not cleared"
    assert "result" not in at.session_state, "legacy pointer not cleared"


def _league_select(at):
    return next(s for s in at.sidebar.selectbox if s.label == "League")


def _run_button(at):
    return next(x for x in at.sidebar.button if "Run evaluation" in x.label)


def test_eval_silo_and_active_league_via_ui():
    """End-to-end behavior of the real Eval Browser (Phases 2 + 3):

      • each selected league lands in its own silo entry (set_result is wired in);
      • switching the picker back to an already-run league shows THAT league with
        no recompute (per-league siloing — the core fix);
      • switching to an as-yet-unrun league AUTO-RUNS it (Phase 3): its own table
        appears with no manual click and without leaking the previous league.

    The page is rendered directly (Home is the app's default page now), so this
    drives eval_browser_page itself rather than navigating to it. Skipped if
    fewer than three leagues have PlayerData."""
    from streamlit.testing.v1 import AppTest

    leagues = app.discover_leagues()
    if len(leagues) < 3:
        print("SKIP     test_eval_silo_and_active_league_via_ui (need >=3 leagues)")
        return
    a, b, c = leagues[0], leagues[1], leagues[2]

    at = AppTest.from_string(_silo_script("app.eval_browser_page()\n"),
                             default_timeout=300)
    at.run()  # first render auto-runs the defaulted first league (a)
    _league_select(at).set_value(b).run()  # selecting b auto-runs it
    assert not at.exception, f"eval raised: {at.exception}"

    results = at.session_state["results"]
    assert a in results and b in results, \
        f"silo missing a league: have {set(results)}, expected {a!r} and {b!r}"
    assert results[a]["league"] == a and results[b]["league"] == b

    # Switch back to A → shows A's table, and C (never visited) is untouched.
    _league_select(at).set_value(a).run()
    assert any(f"{a.upper()} —" in s.value for s in at.subheader), \
        "switching back to a run league did not show its results"
    assert at.session_state["selected_league"] == a
    assert c not in at.session_state["results"], "C scored before it was selected"

    # Switch to C (never run) → Phase 3 auto-runs it; its own table shows, and the
    # previously-run league B does not leak through.
    _league_select(at).set_value(c).run()
    assert at.session_state["selected_league"] == c
    assert c in at.session_state["results"], "Phase 3 should auto-run C on selection"
    assert any(f"{c.upper()} —" in s.value for s in at.subheader), \
        "auto-run league C's table is not shown"
    assert not any(f"{b.upper()} —" in s.value for s in at.subheader), \
        "selecting C leaked the previously-run league B's table"


def test_autorun_result_offline_defaults_and_unknown_league():
    """autorun_result scores a real league once with offline-safe defaults
    (no draft, no contracts) and returns None for a league with no PlayerData."""
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_string(_silo_script(
        'import streamlit as st\n'
        'lg = app.discover_leagues()[0]\n'
        'r = app.autorun_result(lg)\n'
        'st.session_state["_lg"] = lg\n'
        'st.session_state["_r_league"] = r["league"] if r else None\n'
        'st.session_state["_r_draft"] = r["draft"] if r else None\n'
        'st.session_state["_r_contracts"] = r["contracts"] if r else None\n'
        'st.session_state["_r_scale"] = r["rating_scale"] if r else None\n'
        'st.session_state["_in_silo"] = app.get_result(lg) is not None\n'
        'st.session_state["_unknown"] = app.autorun_result("no-such-league-xyz")\n'
    ), default_timeout=120)
    at.run()
    assert not at.exception, f"autorun script raised: {at.exception}"
    lg = at.session_state["_lg"]
    assert at.session_state["_r_league"] == lg
    assert at.session_state["_r_draft"] is False, "auto-run must not enable draft"
    assert at.session_state["_r_contracts"] is False, \
        "auto-run must not enable contracts (network)"
    assert at.session_state["_r_scale"] == app.default_scale_for(lg)
    assert at.session_state["_in_silo"] is True
    assert at.session_state["_unknown"] is None, \
        "autorun_result must return None for a league with no PlayerData"


def test_eval_per_league_scale_reseeds_via_ui():
    """Phase 4: the rating-scale toggle re-seeds to each league's smart default
    on switch — ndl → 1-100, a 20-80 league → engine-native — so a manual run
    never inherits the previous league's scale."""
    from streamlit.testing.v1 import AppTest

    leagues = app.discover_leagues()
    other = next((lg for lg in leagues if lg != "ndl"), None)
    if "ndl" not in leagues or other is None:
        print("SKIP     test_eval_per_league_scale_reseeds_via_ui (need ndl + 1 more)")
        return

    at = AppTest.from_string(_silo_script("app.eval_browser_page()\n"),
                             default_timeout=300)
    at.run()
    _league_select(at).set_value("ndl").run()
    assert at.session_state["eval_scale"] == "1-100", "ndl should default to 1-100"
    _league_select(at).set_value(other).run()
    assert at.session_state["eval_scale"] == app.default_scale_for(other), \
        "switching leagues must re-seed the rating scale to the new league's default"


def test_eval_warns_for_active_league_without_data():
    """Phase 4: an active league with no PlayerData file gets a clear warning
    instead of silently scoring a different league."""
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_string(_silo_script(
        'import streamlit as st\n'
        'st.session_state["selected_league"] = "zzz-no-data-league"\n'
        'app.eval_browser_page()\n'
    ), default_timeout=120)
    at.run()
    assert not at.exception
    assert any("PlayerData" in w.value for w in at.warning), \
        "expected a no-PlayerData warning for an active league without data"


# --- fresh-ratings pull (League Hub) ----------------------------------------

def _swap(mod, **kw):
    """Set attrs on `mod`, returning the originals so a finally can restore them."""
    old = {k: getattr(mod, k) for k in kw}
    for k, v in kw.items():
        setattr(mod, k, v)
    return old


def _restore(mod, old):
    for k, v in old.items():
        setattr(mod, k, v)


def test_fetch_league_ratings_success():
    """The poll loop kicks off, polls past an in-progress response, then saves
    the CSV — all offline via patched network building blocks."""
    csv_body = "ID,Name\n" + "\n".join(f"{i},Player{i}" for i in range(10))
    seen = {"polls": 0, "saved": None}

    def fake_get(url, cookie, timeout=60):
        seen["polls"] += 1
        if seen["polls"] == 1:
            return 200, "Request ID ABC still in progress, check back soon."
        return 200, csv_body

    old = _swap(
        fetch,
        load_league_base=lambda lg: "https://host/slug/api",
        load_token_for=lambda lg: "tok123",
        load_cookie_for=lambda base: None,
        kick_off=lambda base, token, cookie, osa: "https://host/slug/api/mycsv/?request=GUID",
        _get=fake_get,
        save_csv=lambda body, path: seen.update(saved=(str(path), body)),
    )
    try:
        events = list(fetch.fetch_league_ratings(
            "testlg", poll_interval=0, timeout_minutes=5))
    finally:
        _restore(fetch, old)

    kinds = [e["type"] for e in events]
    assert kinds[-1] == "done", f"expected terminal 'done', got {kinds}"
    assert "progress" in kinds, "no progress events emitted"
    assert seen["polls"] >= 2, "should poll past the in-progress response"
    saved_path, saved_body = seen["saved"]
    assert saved_path.endswith("PlayerData-testlg.csv")
    assert saved_body == csv_body
    assert events[-1]["bytes"] == len(csv_body)


def test_fetch_league_ratings_no_auth():
    old = _swap(
        fetch,
        load_league_base=lambda lg: "https://host/slug/api",
        load_token_for=lambda lg: None,
        load_cookie_for=lambda base: None,
    )
    try:
        events = list(fetch.fetch_league_ratings("testlg", poll_interval=0))
    finally:
        _restore(fetch, old)
    assert len(events) == 1 and events[0]["type"] == "error"
    assert "auth" in events[0]["msg"].lower()


def test_fetch_league_ratings_timeout():
    old = _swap(
        fetch,
        load_league_base=lambda lg: "https://host/slug/api",
        load_token_for=lambda lg: "tok",
        load_cookie_for=lambda base: None,
        kick_off=lambda *a, **k: "https://host/slug/api/mycsv/?request=G",
        _get=lambda url, cookie, timeout=60: (200, "still in progress"),
    )
    try:
        events = list(fetch.fetch_league_ratings(
            "testlg", poll_interval=0, timeout_minutes=0))
    finally:
        _restore(fetch, old)
    assert events[-1]["type"] == "error"
    assert "timed out" in events[-1]["msg"].lower()


def test_fetch_league_ratings_non_csv_is_error():
    old = _swap(
        fetch,
        load_league_base=lambda lg: "https://host/slug/api",
        load_token_for=lambda lg: "tok",
        load_cookie_for=lambda base: None,
        kick_off=lambda *a, **k: "https://host/slug/api/mycsv/?request=G",
        _get=lambda url, cookie, timeout=60: (200, "<html>login required</html>"),
    )
    try:
        events = list(fetch.fetch_league_ratings(
            "testlg", poll_interval=0, timeout_minutes=5))
    finally:
        _restore(fetch, old)
    assert events[-1]["type"] == "error"
    assert "non-csv" in events[-1]["msg"].lower()


def test_drop_result_evicts_one_league_and_pointer():
    """drop_result removes just the named league and clears the legacy pointer
    only when it referenced that league."""
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_string(_silo_script(
        'import streamlit as st\n'
        'from state import drop_result\n'
        'app.set_result("aaa", {"rows": [1], "league": "aaa"})\n'
        'app.set_result("bbb", {"rows": [2], "league": "bbb"})\n'  # legacy ptr → bbb
        'drop_result("bbb")\n'
        'st.session_state["_a_kept"] = app.get_result("aaa") is not None\n'
        'st.session_state["_b_gone"] = app.get_result("bbb") is None\n'
        'st.session_state["_ptr_cleared"] = "result" not in st.session_state\n'
    ), default_timeout=30)
    at.run()
    assert not at.exception, f"drop_result script raised: {at.exception}"
    assert at.session_state["_a_kept"] is True
    assert at.session_state["_b_gone"] is True
    assert at.session_state["_ptr_cleared"] is True


def test_hub_shows_fetch_button():
    """The League Hub renders a 'Pull fresh ratings' button for the active league."""
    from streamlit.testing.v1 import AppTest
    import home

    lg = home.configured_leagues()[0]
    at = AppTest.from_string(_silo_script(
        'import streamlit as st\n'
        f'st.session_state["selected_league"] = {lg!r}\n'
        'import league\n'
        'league.page()\n'
    ), default_timeout=60)
    at.run()
    assert not at.exception, f"hub raised: {at.exception}"
    assert any(b.key == f"fetch_btn_{lg}" for b in at.button), \
        "League Hub is missing the fresh-ratings button"


# --- Free Agents page -------------------------------------------------------

def test_free_agents_page_renders_priority_needs():
    """The Free Agents page scores the active league live (ratings-only, offline)
    and renders the biggest-holes-first Priority Needs section plus its weak-spot
    threshold control."""
    from streamlit.testing.v1 import AppTest

    if "ndl" not in app.discover_leagues():
        print("SKIP     test_free_agents_page_renders_priority_needs (need ndl)")
        return

    at = AppTest.from_string(_silo_script(
        'import streamlit as st\n'
        'st.session_state["selected_league"] = "ndl"\n'
        'import free_agents\n'
        'free_agents.page()\n'
    ), default_timeout=300)
    at.run()
    assert not at.exception, f"free agents page raised: {at.exception}"
    assert any("Priority Needs" in m.value for m in at.markdown), \
        "Free Agents page did not render the Priority Needs section"
    assert any(s.label == "Weak-spot threshold" for s in at.slider), \
        "weak-spot threshold slider missing from the Free Agents sidebar"


# --- Trade Targets page -----------------------------------------------------

def test_trade_targets_page_renders_shopping_list():
    """The Trade Targets page boots, grades needs, and renders the fit-sorted
    shopping list. The two network seams (the /tradeblock pull and the heavy
    stats/override build) are stubbed so the render + filter wiring is exercised
    fully offline; the active league's eval still auto-runs (ndl, ratings-only)."""
    from streamlit.testing.v1 import AppTest

    if "ndl" not in app.discover_leagues():
        print("SKIP     test_trade_targets_page_renders_shopping_list (need ndl)")
        return

    at = AppTest.from_string(_silo_script(
        'import streamlit as st\n'
        'import trade_targets_page as ttp\n'
        'st.session_state["selected_league"] = "ndl"\n'
        # Stub the /tradeblock pull (token-authed network) and the heavy build
        # (stats + /players override) with canned, offline data.
        'ttp.fetch_block_pids = lambda league: {'
        '    "pids": ["1", "2", "3"], "had_token": True, "has_base_url": True}\n'
        'ttp.build_targets_context = lambda *a, **k: {'
        '    "targets": [{"pos": "SS", "tier": "Critical", "summary": "thin",'
        '                 "archetype": "SS bat", "reasoning": "weak starter"}],'
        '    "scored_all": [{"name": "Joe Target", "_current_org": "Rivals",'
        '                    "_level": "ML", "age": "27", "primary_pos": "SS",'
        '                    "proj_role": "", "_fit_pos": "SS",'
        '                    "_need_entry": {"tier": "Critical"}, "vos": 55.0,'
        '                    "vos_potential": 58.0, "composite": 56.0,'
        '                    "_fit_score": 170.0, "_category": "Priority Target",'
        '                    "_status_flags": ""}],'
        '    "org_pool": True, "players_available": True, "stats_available": True}\n'
        'ttp.page()\n'
    ), default_timeout=300)
    at.run()
    assert not at.exception, f"trade targets page raised: {at.exception}"
    assert any("Shopping list" in m.value for m in at.markdown), \
        "Trade Targets page did not render the Shopping list section"
    assert any(s.label == "Min composite" for s in at.slider), \
        "min-composite slider missing from the Trade Targets sidebar"
    assert any("Joe Target" in str(df.value.to_dict()) for df in at.dataframe), \
        "the canned candidate did not appear in any rendered table"


# --- Farm Value page --------------------------------------------------------

def test_farm_value_page_renders_rankings():
    """The Farm Value page values + ranks every org's farm from the active
    league's eval (in-process board + on-disk eval for VPC) and renders the
    league ranking table plus the selected team's 'League rank' metric. Fully
    offline for ndl (no network; VPC degrades to a farm index if the on-disk
    eval lacks contracts — ranking still renders)."""
    from streamlit.testing.v1 import AppTest

    if "ndl" not in app.discover_leagues():
        print("SKIP     test_farm_value_page_renders_rankings (need ndl)")
        return

    at = AppTest.from_string(_silo_script(
        'import streamlit as st\n'
        'st.session_state["selected_league"] = "ndl"\n'
        'import farm_value_page\n'
        'farm_value_page.page()\n'
    ), default_timeout=300)
    at.run()
    assert not at.exception, f"farm value page raised: {at.exception}"
    assert any("League farm rankings" in m.value for m in at.markdown), \
        "Farm Value page did not render the league rankings section"
    assert any(s.label == "Organization" for s in at.selectbox), \
        "organization selector missing from the Farm Value sidebar"
    assert any(m.label == "League rank" for m in at.metric), \
        "the 'League rank' headline metric is missing"


# --- runner -----------------------------------------------------------------

def _tests():
    return [(n, o) for n, o in sorted(globals().items())
            if n.startswith("test_") and callable(o)]


def main() -> int:
    failures = []
    for name, fn in _tests():
        try:
            fn()
        except Exception:  # noqa: BLE001 — report and keep going
            failures.append(name)
            print(f"FAIL     {name}")
            traceback.print_exc()
        else:
            print(f"PASS     {name}")
    if failures:
        print(f"\n{len(failures)} FAILED: {', '.join(failures)}")
        return 1
    print(f"\nAll {len(_tests())} webapp checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
