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
                "trade_targets", "farm_value", "league", "league_admin"}
    assert expected.issubset(set(pages)), \
        f"missing page registrations: {expected - set(pages)}"


def test_league_admin_page_renders_readonly():
    """League Admin renders its coverage matrix + per-league detail against the
    real config/ with no exception (Phase 1 is read-only — no writes happen)."""
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_string(_silo_script(
        'import league_admin\n'
        'league_admin.page()\n'
    ), default_timeout=60)
    at.run()
    assert not at.exception, f"league_admin.page() raised: {at.exception}"
    assert any("League Admin" in h.value for h in at.header), \
        "League Admin header not rendered"


def _admin_scaffold(tmp):
    """Minimal config/ with one league + _comment, for write-path tests."""
    import json
    (tmp / "league_url.json").write_text(json.dumps({
        "_comment": "keep", "ndl": "https://statsplus.net/ndl/api",
    }), encoding="utf-8")
    (tmp / "league_settings.json").write_text(json.dumps({
        "ndl": {"rating_scale": "1-100", "org": "Seattle Whalers", "year": 2055},
    }), encoding="utf-8")


def test_league_admin_apply_edits_persists_only_changes():
    """apply_edits writes just the changed fields, leaves _comment/siblings and
    token untouched, and reports which fields changed."""
    import json
    import tempfile
    import league_admin
    from league_registry import LeagueRegistry

    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        _admin_scaffold(tmp)
        reg = LeagueRegistry(tmp)

        changed = league_admin.apply_edits(
            reg, "ndl", url="https://statsplus.net/ndl/api", token="",
            rating_scale="1-100", org="Tucson Oilmen", year=2056,
            min_comp=None, game_version="OOTP 27", sim_time="",
        )
        assert set(changed) == {"org", "year", "game_version"}, changed
        urls = json.loads((tmp / "league_url.json").read_text(encoding="utf-8"))
        assert urls.get("_comment") == "keep", "url _comment lost"
        back = reg.load("ndl")
        assert back.org == "Tucson Oilmen" and back.year == 2056
        assert back.game_version == "OOTP 27"
        assert not (tmp / "statsplus_tokens.json").exists(), \
            "blank token must not write a tokens file"


def test_league_admin_apply_edits_noop_and_token_and_invalid():
    """No-op returns []; a provided token is written; an invalid URL raises."""
    import tempfile
    import league_admin
    from league_registry import LeagueRegistry, RegistryError

    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        _admin_scaffold(tmp)
        reg = LeagueRegistry(tmp)

        # identical values -> no change
        noop = league_admin.apply_edits(
            reg, "ndl", url="https://statsplus.net/ndl/api", token="",
            rating_scale="1-100", org="Seattle Whalers", year=2055,
            min_comp=None, game_version="", sim_time="",
        )
        assert noop == [], noop

        # provide a token -> written
        changed = league_admin.apply_edits(
            reg, "ndl", url="https://statsplus.net/ndl/api",
            token="019e134f-1287-7d89-bda3-fc8928b1cb68",
            rating_scale="1-100", org="Seattle Whalers", year=2055,
            min_comp=None, game_version="", sim_time="",
        )
        assert changed == ["token"], changed
        assert reg.load("ndl").token.startswith("019e134f")

        # invalid URL -> RegistryError, nothing persisted
        try:
            league_admin.apply_edits(
                reg, "ndl", url="not-a-url", token="",
                rating_scale="1-100", org="Seattle Whalers", year=2055,
                min_comp=None, game_version="", sim_time="",
            )
            assert False, "invalid URL should raise"
        except RegistryError:
            pass


def test_league_admin_structured_parsers():
    """The UI-free parse_* helpers convert editor rows to the on-disk shapes,
    dropping blanks and rejecting bad integers."""
    import league_admin as la
    from league_registry import RegistryError

    ids = la.parse_league_ids([
        {"Level": "ML", "League IDs": "200"},
        {"Level": "AAA", "League IDs": "201, 202"},
        {"Level": "_international", "League IDs": "217,218"},
        {"Level": "", "League IDs": "999"},          # blank level dropped
    ])
    assert ids == {"ML": [200], "AAA": [201, 202], "_international": [217, 218]}, ids
    try:
        la.parse_league_ids([{"Level": "ML", "League IDs": "20x"}])
        assert False, "non-integer ID should raise"
    except RegistryError:
        pass

    assert la.parse_orgs([{"Org": "A"}, {"Org": " "}, {"Org": "B"}]) == ["A", "B"]

    div = la.parse_divisions([
        {"Sub-league": "AL", "Division": "East", "Team": "Spitters"},
        {"Sub-league": "AL", "Division": "East", "Team": "Vice"},
        {"Sub-league": "AL", "Division": "West", "Team": "Drifters"},
        {"Sub-league": "NL", "Division": "East", "Team": "Cubs"},
        {"Sub-league": "AL", "Division": "", "Team": "Orphan"},   # incomplete dropped
    ])
    assert div == {"AL": {"East": ["Spitters", "Vice"], "West": ["Drifters"]},
                   "NL": {"East": ["Cubs"]}}, div

    slack = la.parse_gm_slack([
        {"Team": "Whalers", "Handle": "Alex"},
        {"Team": "Vice", "Handle": ""},        # blank handle kept
        {"Team": "", "Handle": "ghost"},       # blank team dropped
    ])
    assert slack == {"Whalers": "Alex", "Vice": ""}, slack


def test_league_admin_structured_roundtrip():
    """Saving parsed ID-map and divisions through the registry round-trips, and
    leaves the league_url _comment + sibling intact."""
    import json
    import tempfile
    import league_admin as la
    from league_registry import LeagueRegistry, LeagueConfig

    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        _admin_scaffold(tmp)
        reg = LeagueRegistry(tmp)

        ids = la.parse_league_ids([{"Level": "ML", "League IDs": "200"},
                                   {"Level": "AAA", "League IDs": "201, 202"}])
        reg.save(LeagueConfig(slug="ndl", league_ids=ids))
        div = la.parse_divisions([{"Sub-league": "AL", "Division": "East", "Team": "Spitters"}])
        reg.save(LeagueConfig(slug="ndl", divisions=div))

        back = reg.load("ndl")
        assert back.league_ids == {"ML": [200], "AAA": [201, 202]}
        assert back.divisions == {"AL": {"East": ["Spitters"]}}
        assert (tmp / "divisions-ndl.json").exists()
        # untouched scalar settings + url comment survive the per-league writes
        urls = json.loads((tmp / "league_url.json").read_text(encoding="utf-8"))
        assert urls.get("_comment") == "keep"
        assert back.org == "Seattle Whalers"


# /teams sample (quoted CSV, per api.txt) + /ballparks sample (the real shape).
_TEAMS_CSV = (
    '"ID","Name","Nickname","Parent Team ID"\n'
    '"31","Arizona","Diamondbacks","0"\n'
    '"5","Reno","Aces","31"\n'
    '"32","Boston","Beans",""\n'
)
_BALLPARKS = {
    "league_id": 0,
    "ballparks": [
        {"team_id": 31, "league_id": 153, "park_id": 13, "name": "Arizona",
         "nickname": "Diamondbacks", "display_name": "Arizona Diamondbacks",
         "abbr": "ARI", "avg_r": 1, "avg_l": 1.06, "avg": 1.021, "d": 1.09,
         "t": 1.67, "hr_r": 0.98, "hr_l": 1.01, "hr": 0.9905, "capacity": 48633,
         "stadium_type": "Retractable Roof", "surface": "Grass"},
        {"team_id": 32, "league_id": 153, "park_id": 14, "name": "Boston",
         "nickname": "Beans", "display_name": "Boston Beans", "abbr": "BOS",
         "avg_r": 1.0, "avg_l": 1.0, "avg": 1.0, "d": 1.0, "t": 1.0,
         "hr_r": 1.0, "hr_l": 1.0, "hr": 1.0, "capacity": 38000,
         "stadium_type": "Open", "surface": "Grass"},
    ],
}


def test_provision_parsers_map_api_to_files():
    """parse_teams_csv / build_park_factors / build_orgs convert the API
    payloads to the on-disk shapes with neutral park adjustments."""
    import league_provision as lp

    teams = lp.parse_teams_csv(_TEAMS_CSV)
    assert teams["31"] == {"Name": "Arizona", "Nickname": "Diamondbacks", "Parent": 0}
    assert teams["5"]["Parent"] == 31
    assert teams["32"]["Parent"] == 0  # blank parent -> 0

    pf = lp.build_park_factors(_BALLPARKS)
    ari = pf["teams"]["Arizona Diamondbacks"]
    assert ari["team_info"] == {"team_name": "Arizona Diamondbacks",
                                "team_code": "ARI", "park_name": ""}
    raw = ari["raw_park_factors"]
    assert (raw["avg_overall"], raw["avg_rhb"], raw["avg_lhb"]) == (1.021, 1.0, 1.06)
    assert (raw["doubles"], raw["triples"], raw["hr_overall"]) == (1.09, 1.67, 0.9905)
    assert (raw["hr_rhb"], raw["hr_lhb"]) == (0.98, 1.01)
    # batting computed from the raw factors; defense/pitcher stay neutral
    assert ari["tool_adjustments"]["batting"]["Pow"] == 0.994   # 1+(0.9905-1)*0.6
    assert ari["tool_adjustments"]["defense"]["OFR"] == 1.0
    assert ari["tool_adjustments"]["pitcher_ability"]["Stuff"] == 1.0
    assert ari["handedness_splits"]["RHB"]["Pow"] == 0.988      # 1+(0.98-1)*0.6
    assert ari["park_profile"]["capacity"] == 48633
    assert ari["park_profile"]["type"]                          # classified

    assert lp.build_orgs(_BALLPARKS) == ["Arizona Diamondbacks", "Boston Beans"]
    assert lp.ml_lid_from_ballparks(_BALLPARKS) == 153


def test_park_factor_formulas_match_handauthored():
    """The ported batting + handedness formulas reproduce the hand-authored
    Alabama Bears values exactly (config/ndl-park-factors.json)."""
    import league_provision as lp
    raw = {"avg_overall": 1.017, "avg_rhb": 1.019, "avg_lhb": 1.014,
           "doubles": 1.107, "triples": 1.117,
           "hr_overall": 0.916, "hr_rhb": 0.927, "hr_lhb": 0.897}
    assert lp.compute_batting_adjustments(raw) == {
        "Pow": 0.95, "Gap": 1.089, "Eye": 1.005, "Ks": 0.997}
    h = lp.compute_handedness_pow(raw)
    assert h["RHB"]["Pow"] == 0.956 and h["LHB"]["Pow"] == 0.938


def test_token_age_note_levels():
    """token_age_note classifies by age against the ~90-day TTL."""
    import league_admin as la
    assert la.token_age_note(None, "2026-06-03")[1] == "unknown"
    assert la.token_age_note("bad-date", "2026-06-03")[1] == "unknown"
    assert la.token_age_note("2026-06-01", "2026-06-03")[1] == "ok"
    assert la.token_age_note("2026-03-15", "2026-06-03")[1] == "warn"     # ~80d
    assert la.token_age_note("2026-02-01", "2026-06-03")[1] == "expired"  # >90d


def test_provision_league_orchestration():
    """provision_league writes the three files + registry entries from stubbed
    fetchers, and refuses to clobber an existing league without overwrite."""
    import tempfile
    import league_provision as lp
    from league_registry import LeagueRegistry

    def text_fetcher(url, endpoint, token, **kw):
        assert endpoint == "teams"
        return _TEAMS_CSV

    def json_fetcher(url, endpoint, token, **kw):
        assert endpoint == "ballparks"
        return _BALLPARKS

    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        reg = LeagueRegistry(tmp)
        manifest = lp.provision_league(
            reg, "abc", "https://statsplus.net/abc/api", None,
            settings={"org": "Arizona Diamondbacks", "rating_scale": "20-80", "year": 2050},
            text_fetcher=text_fetcher, json_fetcher=json_fetcher,
        )
        assert manifest["teams_count"] == 3 and manifest["parks_count"] == 2
        assert manifest["orgs_count"] == 2 and manifest["ml_lid"] == 153
        assert (tmp / "teams-abc.json").exists()
        assert (tmp / "abc-park-factors.json").exists()
        assert (tmp / "abc_orgs.json").exists()

        assert reg.exists("abc")
        cfg = reg.load("abc")
        assert cfg.url.endswith("/abc/api")
        assert cfg.league_ids == {"ML": [153]}
        assert cfg.org == "Arizona Diamondbacks" and cfg.year == 2050

        # re-provision without overwrite is refused
        try:
            lp.provision_league(reg, "abc", "https://statsplus.net/abc/api", None,
                                text_fetcher=text_fetcher, json_fetcher=json_fetcher)
            assert False, "should refuse to clobber existing league"
        except lp.ProvisionError:
            pass


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
