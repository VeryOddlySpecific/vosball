#!/usr/bin/env python3
"""Unit tests — core/league_registry.py (ticket 0003, Phase 0).

Exercises the registry against a temp copy of config/: discovery, load,
round-trip save (preserving _comment + sibling leagues), add, remove, backups,
and validation. Fully offline.

    py tests/test_league_registry.py        # exits non-zero on any failure
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "core"))  # app-consumed modules live under core/

import league_registry as lr  # noqa: E402

_failures: list[str] = []


def check(cond: bool, label: str) -> None:
    if cond:
        print(f"PASS     {label}")
    else:
        print(f"FAIL     {label}")
        _failures.append(label)


def _scaffold(tmp: Path) -> None:
    """Write a realistic mini config/ with two leagues (ndl, sahl) plus
    _comment keys and a _default token, so we can assert preservation."""
    (tmp / "league_url.json").write_text(json.dumps({
        "_comment": "keep me",
        "ndl": "https://atl-02.statsplus.net/therealndl/api",
        "sahl": "https://statsplus.net/sahl/api",
    }, indent=2), encoding="utf-8")

    (tmp / "statsplus_tokens.json").write_text(json.dumps({
        "_comment": "tokens",
        "_default": "019e0000-0000-7000-8000-000000000000",
        "ndl": "019e134f-1287-7d89-bda3-fc8928b1cb68",
        # sahl intentionally omitted -> should resolve to _default
    }, indent=2), encoding="utf-8")

    (tmp / "league_settings.json").write_text(json.dumps({
        "_comment": "settings",
        "ndl": {"rating_scale": "1-100", "org": "Seattle Whalers", "year": 2055,
                "min_comp": 55, "game_version": "OOTP 27", "sim_time": "Early Morning",
                "extra_unmodeled": "preserve me"},
        "sahl": {"rating_scale": "20-80", "org": "Houston Astros", "year": 2062},
    }, indent=2), encoding="utf-8")

    (tmp / "league_ids.json").write_text(json.dumps({
        "_comment": "ids",
        "ndl": {"ML": [200], "AAA": [201], "_international": [225]},
        "sahl": {"ML": [153], "AAA": [154, 155]},
    }, indent=2), encoding="utf-8")

    (tmp / "ndl_orgs.json").write_text(json.dumps(
        ["Seattle Whalers", "Kansas City Kings"], indent=2), encoding="utf-8")
    (tmp / "divisions-ndl.json").write_text(json.dumps(
        {"American League": {"East": ["New England Spitters"]}}, indent=2), encoding="utf-8")
    (tmp / "ndl-gm-slack.json").write_text(json.dumps(
        {"Seattle Whalers": "Seattle - VOS (Alex)"}, indent=2), encoding="utf-8")


def main() -> int:
    # ---- discovery ----
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        _scaffold(tmp)
        reg = lr.LeagueRegistry(tmp)
        check(reg.slugs() == ["ndl", "sahl"], "slugs() unions shared keys, excludes _comment/_default")
        check(reg.exists("ndl") and not reg.exists("nope"), "exists() reflects known/unknown")

        st = reg.files_status("ndl")
        check(st["url"] and st["token"] and st["settings"] and st["league_ids"]
              and st["orgs"] and st["divisions"] and st["gm_slack"],
              "files_status: ndl has all per-league files present")
        check(not st["teams"] and not st["park_factors"],
              "files_status: ndl large files absent in fixture")
        check(reg.files_status("sahl")["token"] is True,
              "files_status: sahl token present via _default")

    # ---- load ----
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        _scaffold(tmp)
        reg = lr.LeagueRegistry(tmp)
        ndl = reg.load("ndl")
        check(ndl.url.endswith("/therealndl/api"), "load: url")
        check(ndl.token == "019e134f-1287-7d89-bda3-fc8928b1cb68", "load: explicit token")
        check(ndl.uses_default_token is False, "load: explicit token -> not using default")
        check(ndl.rating_scale == "1-100" and ndl.year == 2055 and ndl.org == "Seattle Whalers",
              "load: settings scalars")
        check(ndl.league_ids == {"ML": [200], "AAA": [201], "_international": [225]},
              "load: league_ids")
        check(ndl.orgs == ["Seattle Whalers", "Kansas City Kings"], "load: orgs")
        check("American League" in (ndl.divisions or {}), "load: divisions")
        check(ndl.gm_slack == {"Seattle Whalers": "Seattle - VOS (Alex)"}, "load: gm_slack")

        sahl = reg.load("sahl")
        check(sahl.token is None and sahl.uses_default_token is True,
              "load: sahl falls back to _default token")

        try:
            reg.load("ghost")
            check(False, "load: unknown slug raises")
        except lr.RegistryError:
            check(True, "load: unknown slug raises")

    # ---- round-trip preserves _comment + siblings + unmodeled keys ----
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        _scaffold(tmp)
        reg = lr.LeagueRegistry(tmp)
        cfg = reg.load("ndl")
        cfg.year = 2056
        cfg.org = "Tucson Oilmen"
        reg.save(cfg)

        urls = json.loads((tmp / "league_url.json").read_text(encoding="utf-8"))
        check(urls.get("_comment") == "keep me" and "sahl" in urls,
              "save: league_url keeps _comment + sibling")
        settings = json.loads((tmp / "league_settings.json").read_text(encoding="utf-8"))
        check(settings["_comment"] == "settings" and settings["sahl"]["org"] == "Houston Astros",
              "save: league_settings keeps _comment + sibling untouched")
        check(settings["ndl"]["year"] == 2056 and settings["ndl"]["org"] == "Tucson Oilmen",
              "save: edited scalars persisted")
        check(settings["ndl"].get("extra_unmodeled") == "preserve me",
              "save: unmodeled per-league key preserved (merge, not replace)")

        reloaded = reg.load("ndl")
        check(reloaded.year == 2056 and reloaded.org == "Tucson Oilmen", "save: reload reflects edit")

    # ---- add a brand-new league via save ----
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        _scaffold(tmp)
        reg = lr.LeagueRegistry(tmp)
        newc = lr.LeagueConfig(
            slug="bwb", url="https://atl-02.statsplus.net/bwb/api",
            token="019e32e2-3492-7f45-b60f-d9dd5cea9b57",
            rating_scale="20-80", org="Chihuahua Guerreros", year=2028,
            league_ids={"ML": [203]}, orgs=["Chihuahua Guerreros"],
        )
        reg.save(newc)
        check("bwb" in reg.slugs(), "add: new slug discovered")
        urls = json.loads((tmp / "league_url.json").read_text(encoding="utf-8"))
        check(urls["bwb"].endswith("/bwb/api") and "ndl" in urls, "add: url written, siblings intact")
        check((tmp / "bwb_orgs.json").exists(), "add: per-league orgs file created")
        back = reg.load("bwb")
        check(back.year == 2028 and back.token.startswith("019e32e2"), "add: reload new league")

    # ---- None fields are skipped on save ----
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        _scaffold(tmp)
        reg = lr.LeagueRegistry(tmp)
        # save sahl with only a year change; token/orgs/divisions are None
        cfg = lr.LeagueConfig(slug="sahl", year=2063)
        reg.save(cfg)
        tokens = json.loads((tmp / "statsplus_tokens.json").read_text(encoding="utf-8"))
        check("sahl" not in tokens, "save: None token did not create a token key")
        check(not (tmp / "sahl_orgs.json").exists(), "save: None orgs did not create a file")
        settings = json.loads((tmp / "league_settings.json").read_text(encoding="utf-8"))
        check(settings["sahl"]["year"] == 2063 and settings["sahl"]["org"] == "Houston Astros",
              "save: partial settings merge keeps existing org")

    # ---- remove ----
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        _scaffold(tmp)
        reg = lr.LeagueRegistry(tmp)
        touched = reg.remove("ndl")
        check("ndl" not in reg.slugs() and "sahl" in reg.slugs(),
              "remove: ndl gone, sahl remains")
        urls = json.loads((tmp / "league_url.json").read_text(encoding="utf-8"))
        check("ndl" not in urls and urls.get("_comment") == "keep me" and "sahl" in urls,
              "remove: stripped from shared file, _comment + sibling intact")
        check(not (tmp / "ndl_orgs.json").exists()
              and not (tmp / "divisions-ndl.json").exists()
              and not (tmp / "ndl-gm-slack.json").exists(),
              "remove: per-league files deleted")
        check(len(touched) >= 4, "remove: returns the touched paths")
        try:
            reg.remove("ndl")
            check(False, "remove: removing unknown raises")
        except lr.RegistryError:
            check(True, "remove: removing unknown raises")

    # ---- backups ----
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        _scaffold(tmp)
        reg = lr.LeagueRegistry(tmp)
        cfg = reg.load("ndl")
        cfg.year = 2099
        reg.save(cfg)
        backups = list((tmp / ".backups").glob("league_settings.json.*.bak"))
        check(len(backups) == 1, "backup: prior league_settings backed up on overwrite")
        # backup holds the OLD value
        old = json.loads(backups[0].read_text(encoding="utf-8"))
        check(old["ndl"]["year"] == 2055, "backup: contains pre-edit content")
        # backup=False suppresses
        cfg.year = 2100
        reg.save(cfg, backup=False)
        check(len(list((tmp / ".backups").glob("league_settings.json.*.bak"))) == 1,
              "backup: backup=False writes no new backup")

    # ---- validation ----
    reg = lr.LeagueRegistry()  # default dir fine; validate() does no I/O
    cases = [
        ("Bad-Slug", lr.LeagueConfig(slug="NDL")),
        ("empty slug", lr.LeagueConfig(slug="")),
        ("bad url", lr.LeagueConfig(slug="x", url="not-a-url")),
        ("bad token", lr.LeagueConfig(slug="x", token="nope")),
        ("bad rating_scale", lr.LeagueConfig(slug="x", rating_scale="0-10")),
        ("bad year type", lr.LeagueConfig(slug="x", year="2055")),
        ("bad league_ids", lr.LeagueConfig(slug="x", league_ids={"ML": ["a"]})),
    ]
    for label, cfg in cases:
        try:
            reg.validate(cfg)
            check(False, f"validate: rejects {label}")
        except lr.RegistryError:
            check(True, f"validate: rejects {label}")

    good = lr.LeagueConfig(
        slug="ndl", url="https://statsplus.net/ndl/api",
        token="019e134f-1287-7d89-bda3-fc8928b1cb68",
        rating_scale="1-100", year=2055, min_comp=55.0,
        league_ids={"ML": [200], "_international": [225]},
    )
    try:
        reg.validate(good)
        check(True, "validate: accepts a well-formed config")
    except lr.RegistryError as e:
        check(False, f"validate: accepts a well-formed config ({e})")

    print()
    if _failures:
        print(f"{len(_failures)} FAILURE(S): " + "; ".join(_failures))
        return 1
    print("All league_registry tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
