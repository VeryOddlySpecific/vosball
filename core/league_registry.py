#!/usr/bin/env python3
"""
league_registry.py — One safe place to read and write the per-league slice of
every ``config/*.json`` file the suite consumes.

A "league" in vosball is not a single record; it is an implicit slug (``ndl``,
``sahl``, …) that appears as a key across several config files, plus a few
per-league files. This module unifies that scattered state behind a single
``LeagueRegistry`` so the League Admin UI (and any tool) can load, edit, add,
and remove a league without hand-tracking which file holds what — and without
clobbering ``_comment`` keys or other leagues' entries on write.

Two kinds of file
-----------------
**Shared** — one file, many leagues as top-level keys. Editing one league must
preserve the siblings and any ``_``-prefixed comment keys, so we load the whole
document, mutate only ``data[slug]``, and re-dump (``json`` preserves dict
insertion order):

    league_url.json        {slug: "https://…/api"}
    statsplus_tokens.json  {slug: token, "_default": token}   (gitignored secret)
    league_settings.json   {slug: {rating_scale, org, year, min_comp, …}}
    league_ids.json        {slug: {level_label: [statsplus_lid, …]}}

**Per-league** — one file per league; add/remove = create/delete the file:

    {slug}_orgs.json       ["Org Name", …]
    divisions-{slug}.json  {subleague: {division: [team, …]}}
    {slug}-gm-slack.json   {team: handle}                      (gitignored)

The two large generated files (``teams-{slug}.json``,
``{slug}-park-factors.json``) and ``data/PlayerData-{slug}.csv`` are owned by the
provisioning pipeline (ticket 0003, Phase 4), not by this registry. We only
*report* their presence via :meth:`LeagueRegistry.files_status`.

Every write is atomic (temp file in the same dir → ``os.replace``) and, by
default, makes a timestamped backup under ``config/.backups/`` first.
"""

from __future__ import annotations
# --- repo-root + core/ path bootstrap ---
import os as _os, sys as _sys
_ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
for _p in (_ROOT, _os.path.join(_ROOT, "core")):
    if _p not in _sys.path:
        _sys.path.insert(0, _p)
# --- end bootstrap ---

import json
import re
import shutil
import tempfile
from dataclasses import dataclass, field, fields
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

SCRIPT_DIR = Path(__file__).resolve().parent.parent
CONFIG_DIR = SCRIPT_DIR / "config"

# Filenames of the shared (many-leagues-per-file) documents.
URL_FILE = "league_url.json"
TOKENS_FILE = "statsplus_tokens.json"
SETTINGS_FILE = "league_settings.json"
IDS_FILE = "league_ids.json"

# Filename templates of the per-league (one-file-per-league) documents.
ORGS_TEMPLATE = "{slug}_orgs.json"
DIVISIONS_TEMPLATE = "divisions-{slug}.json"
GM_SLACK_TEMPLATE = "{slug}-gm-slack.json"

# Large files owned by the provisioning pipeline — reported, never edited here.
TEAMS_TEMPLATE = "teams-{slug}.json"
PARK_FACTORS_TEMPLATE = "{slug}-park-factors.json"

BACKUP_DIRNAME = ".backups"

# Settings keys (and their defaults when absent) understood by the tools. The
# defaults mirror what the consuming code applies when a key is missing.
SETTINGS_DEFAULTS: Dict[str, Any] = {
    "rating_scale": "20-80",
    "org": None,
    "year": None,
    "min_comp": None,        # free_agents applies 50.0 at read time if None
    "game_version": None,
    "sim_time": None,
}

VALID_RATING_SCALES = ("20-80", "1-100")

_SLUG_RE = re.compile(r"^[a-z][a-z0-9]*$")
_TOKEN_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


class RegistryError(ValueError):
    """Raised for validation failures and unknown-league lookups."""


@dataclass
class LeagueConfig:
    """The full per-league slice, assembled from every config file.

    A value of ``None`` means "not present / not loaded" and is skipped on
    save (the existing file content is left untouched). Empty containers
    (``[]`` / ``{}``) are written through, so clearing a list persists.
    """

    slug: str
    url: Optional[str] = None
    token: Optional[str] = None          # explicit per-league token (not _default)
    uses_default_token: bool = False     # True when only _default would apply
    rating_scale: Optional[str] = None
    org: Optional[str] = None
    year: Optional[int] = None
    min_comp: Optional[float] = None
    game_version: Optional[str] = None
    sim_time: Optional[str] = None
    league_ids: Optional[Dict[str, List[int]]] = None
    orgs: Optional[List[str]] = None
    divisions: Optional[Dict[str, Any]] = None
    gm_slack: Optional[Dict[str, str]] = None

    def settings_dict(self) -> Dict[str, Any]:
        """The ``league_settings.json`` entry for this league — only the keys
        that are set (non-None), so we never write spurious nulls."""
        out: Dict[str, Any] = {}
        for key in SETTINGS_DEFAULTS:
            val = getattr(self, key)
            if val is not None:
                out[key] = val
        return out


class LeagueRegistry:
    """Read/write the per-league slice across all config files.

    ``config_dir`` is injectable so the whole thing is unit-testable against a
    temporary copy of ``config/`` — production code just uses the default.
    """

    def __init__(self, config_dir: Path | str = CONFIG_DIR) -> None:
        self.config_dir = Path(config_dir)

    # ----- paths ------------------------------------------------------------

    def _path(self, name: str) -> Path:
        return self.config_dir / name

    def _per_league_path(self, template: str, slug: str) -> Path:
        return self.config_dir / template.format(slug=slug)

    # ----- low-level JSON I/O ----------------------------------------------

    @staticmethod
    def _read_json(path: Path, default: Any) -> Any:
        """Read JSON, returning ``default`` on a missing or unreadable file.

        Invalid JSON raises — a corrupt config is a real error we should not
        silently paper over (and certainly not overwrite).
        """
        if not path.exists():
            return default
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            return default
        if not text.strip():
            return default
        return json.loads(text)

    def _atomic_write_json(self, path: Path, data: Any, *, backup: bool = True) -> None:
        """Write ``data`` as pretty JSON atomically, backing up any existing
        file first. Temp file is created in the destination dir so the final
        ``os.replace`` is a same-filesystem atomic rename (also on Windows)."""
        path.parent.mkdir(parents=True, exist_ok=True)
        if backup and path.exists():
            self._backup(path)
        fd, tmp_name = tempfile.mkstemp(
            dir=str(path.parent), prefix=path.name + ".", suffix=".tmp"
        )
        try:
            with _os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2, ensure_ascii=False)
                fh.write("\n")
            _os.replace(tmp_name, path)
        except BaseException:
            try:
                _os.unlink(tmp_name)
            except OSError:
                pass
            raise

    def _backup(self, path: Path) -> Path:
        """Copy ``path`` into ``config/.backups/`` with a timestamp suffix."""
        backup_dir = self.config_dir / BACKUP_DIRNAME
        backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        dest = backup_dir / f"{path.name}.{stamp}.bak"
        shutil.copy2(path, dest)
        return dest

    # ----- discovery --------------------------------------------------------

    def slugs(self) -> List[str]:
        """All known league slugs: the union of keys across the shared files
        plus any per-league files on disk. ``_``-prefixed keys (comments,
        ``_default``) are excluded. Sorted."""
        found: set[str] = set()
        for fname in (URL_FILE, SETTINGS_FILE, IDS_FILE):
            data = self._read_json(self._path(fname), {})
            if isinstance(data, dict):
                found.update(k for k in data if not k.startswith("_"))
        # tokens too, but skip the _default sentinel
        tokens = self._read_json(self._path(TOKENS_FILE), {})
        if isinstance(tokens, dict):
            found.update(k for k in tokens if not k.startswith("_"))
        # per-league files: orgs / divisions / gm-slack
        for p in self.config_dir.glob("*_orgs.json"):
            found.add(p.name[: -len("_orgs.json")])
        for p in self.config_dir.glob("divisions-*.json"):
            found.add(p.name[len("divisions-"): -len(".json")])
        for p in self.config_dir.glob("*-gm-slack.json"):
            found.add(p.name[: -len("-gm-slack.json")])
        found.discard("")
        return sorted(found)

    def exists(self, slug: str) -> bool:
        return slug in self.slugs()

    def files_status(self, slug: str) -> Dict[str, bool]:
        """Which config files currently exist for ``slug`` — drives the admin
        page's completeness/coverage display. Shared files report whether the
        slug has a key inside them; per-league/large files report presence."""
        def _has_key(fname: str) -> bool:
            data = self._read_json(self._path(fname), {})
            return isinstance(data, dict) and slug in data

        tokens = self._read_json(self._path(TOKENS_FILE), {})
        token_present = isinstance(tokens, dict) and (
            slug in tokens or "_default" in tokens
        )
        return {
            "url": _has_key(URL_FILE),
            "token": token_present,
            "settings": _has_key(SETTINGS_FILE),
            "league_ids": _has_key(IDS_FILE),
            "orgs": self._per_league_path(ORGS_TEMPLATE, slug).exists(),
            "divisions": self._per_league_path(DIVISIONS_TEMPLATE, slug).exists(),
            "gm_slack": self._per_league_path(GM_SLACK_TEMPLATE, slug).exists(),
            "teams": self._per_league_path(TEAMS_TEMPLATE, slug).exists(),
            "park_factors": self._per_league_path(PARK_FACTORS_TEMPLATE, slug).exists(),
        }

    # ----- load -------------------------------------------------------------

    def load(self, slug: str) -> LeagueConfig:
        """Assemble the full :class:`LeagueConfig` for ``slug`` from every
        file. Raises :class:`RegistryError` if the slug is unknown."""
        if not self.exists(slug):
            raise RegistryError(
                f"Unknown league {slug!r}. Known: {', '.join(self.slugs()) or '(none)'}"
            )
        cfg = LeagueConfig(slug=slug)

        urls = self._read_json(self._path(URL_FILE), {})
        if isinstance(urls, dict):
            cfg.url = urls.get(slug)

        tokens = self._read_json(self._path(TOKENS_FILE), {})
        if isinstance(tokens, dict):
            tok = tokens.get(slug)
            if isinstance(tok, str) and tok and not tok.startswith("PASTE"):
                cfg.token = tok
            else:
                default = tokens.get("_default")
                cfg.uses_default_token = bool(
                    isinstance(default, str) and default and not default.startswith("PASTE")
                )

        settings = self._read_json(self._path(SETTINGS_FILE), {})
        entry = settings.get(slug) if isinstance(settings, dict) else None
        if isinstance(entry, dict):
            for key in SETTINGS_DEFAULTS:
                if key in entry:
                    setattr(cfg, key, entry[key])

        ids = self._read_json(self._path(IDS_FILE), {})
        ids_entry = ids.get(slug) if isinstance(ids, dict) else None
        if isinstance(ids_entry, dict):
            cfg.league_ids = ids_entry

        orgs_path = self._per_league_path(ORGS_TEMPLATE, slug)
        if orgs_path.exists():
            data = self._read_json(orgs_path, [])
            cfg.orgs = data if isinstance(data, list) else []

        div_path = self._per_league_path(DIVISIONS_TEMPLATE, slug)
        if div_path.exists():
            data = self._read_json(div_path, {})
            cfg.divisions = data if isinstance(data, dict) else {}

        slack_path = self._per_league_path(GM_SLACK_TEMPLATE, slug)
        if slack_path.exists():
            data = self._read_json(slack_path, {})
            cfg.gm_slack = data if isinstance(data, dict) else {}

        return cfg

    # ----- save -------------------------------------------------------------

    def save(self, cfg: LeagueConfig, *, backup: bool = True, validate: bool = True) -> None:
        """Persist ``cfg`` back across all files. ``None`` fields are left
        untouched in their files; non-None fields (incl. empty containers) are
        written. Each touched file is written atomically with a backup."""
        if validate:
            self.validate(cfg)
        slug = cfg.slug

        # --- shared files: load whole doc, set this slug's key, re-dump ---
        if cfg.url is not None:
            urls = self._read_json(self._path(URL_FILE), {})
            if not isinstance(urls, dict):
                urls = {}
            urls[slug] = cfg.url
            self._atomic_write_json(self._path(URL_FILE), urls, backup=backup)

        if cfg.token is not None:
            tokens = self._read_json(self._path(TOKENS_FILE), {})
            if not isinstance(tokens, dict):
                tokens = {}
            tokens[slug] = cfg.token
            self._atomic_write_json(self._path(TOKENS_FILE), tokens, backup=backup)

        settings_entry = cfg.settings_dict()
        if settings_entry:
            settings = self._read_json(self._path(SETTINGS_FILE), {})
            if not isinstance(settings, dict):
                settings = {}
            # merge so we don't drop keys this registry doesn't model
            existing = settings.get(slug)
            if isinstance(existing, dict):
                existing.update(settings_entry)
                settings[slug] = existing
            else:
                settings[slug] = settings_entry
            self._atomic_write_json(self._path(SETTINGS_FILE), settings, backup=backup)

        if cfg.league_ids is not None:
            ids = self._read_json(self._path(IDS_FILE), {})
            if not isinstance(ids, dict):
                ids = {}
            ids[slug] = cfg.league_ids
            self._atomic_write_json(self._path(IDS_FILE), ids, backup=backup)

        # --- per-league files: write the file when the field is provided ---
        if cfg.orgs is not None:
            self._atomic_write_json(
                self._per_league_path(ORGS_TEMPLATE, slug), cfg.orgs, backup=backup
            )
        if cfg.divisions is not None:
            self._atomic_write_json(
                self._per_league_path(DIVISIONS_TEMPLATE, slug), cfg.divisions, backup=backup
            )
        if cfg.gm_slack is not None:
            self._atomic_write_json(
                self._per_league_path(GM_SLACK_TEMPLATE, slug), cfg.gm_slack, backup=backup
            )

    # ----- large generated files (provisioning) -----------------------------

    def write_teams(self, slug: str, data: Dict[str, Any], *, backup: bool = True) -> Path:
        """Write ``teams-{slug}.json`` (the /teams reshape). Returns the path."""
        path = self._per_league_path(TEAMS_TEMPLATE, slug)
        self._atomic_write_json(path, data, backup=backup)
        return path

    def write_park_factors(self, slug: str, data: Dict[str, Any], *, backup: bool = True) -> Path:
        """Write ``{slug}-park-factors.json`` (the /ballparks build). Returns path."""
        path = self._per_league_path(PARK_FACTORS_TEMPLATE, slug)
        self._atomic_write_json(path, data, backup=backup)
        return path

    # ----- remove -----------------------------------------------------------

    def remove(self, slug: str, *, backup: bool = True, remove_large: bool = False) -> List[Path]:
        """Strip ``slug`` from the shared files and delete its per-league
        files. Returns the list of paths that were edited or deleted. Set
        ``remove_large`` to also delete ``teams-`` / ``park-factors`` files.
        ``PlayerData-{slug}.csv`` is never touched here (it lives under
        ``data/`` and is the fetch pipeline's concern)."""
        if not self.exists(slug):
            raise RegistryError(f"Unknown league {slug!r}")
        touched: List[Path] = []

        for fname in (URL_FILE, TOKENS_FILE, SETTINGS_FILE, IDS_FILE):
            path = self._path(fname)
            data = self._read_json(path, {})
            if isinstance(data, dict) and slug in data:
                del data[slug]
                self._atomic_write_json(path, data, backup=backup)
                touched.append(path)

        per_league = [ORGS_TEMPLATE, DIVISIONS_TEMPLATE, GM_SLACK_TEMPLATE]
        if remove_large:
            per_league += [TEAMS_TEMPLATE, PARK_FACTORS_TEMPLATE]
        for template in per_league:
            path = self._per_league_path(template, slug)
            if path.exists():
                if backup:
                    self._backup(path)
                path.unlink()
                touched.append(path)

        return touched

    # ----- validation -------------------------------------------------------

    def validate(self, cfg: LeagueConfig) -> None:
        """Raise :class:`RegistryError` on any invalid field. Empty/None
        optional fields pass — only malformed *present* values are rejected."""
        self.validate_slug(cfg.slug)
        if cfg.url is not None:
            self.validate_url(cfg.url)
        if cfg.token is not None:
            self.validate_token(cfg.token)
        if cfg.rating_scale is not None and cfg.rating_scale not in VALID_RATING_SCALES:
            raise RegistryError(
                f"rating_scale must be one of {VALID_RATING_SCALES}, got {cfg.rating_scale!r}"
            )
        if cfg.year is not None and not isinstance(cfg.year, int):
            raise RegistryError(f"year must be an int, got {cfg.year!r}")
        if cfg.min_comp is not None and not isinstance(cfg.min_comp, (int, float)):
            raise RegistryError(f"min_comp must be numeric, got {cfg.min_comp!r}")
        if cfg.league_ids is not None:
            self.validate_league_ids(cfg.league_ids)

    @staticmethod
    def validate_slug(slug: str) -> None:
        if not isinstance(slug, str) or not _SLUG_RE.match(slug):
            raise RegistryError(
                f"Invalid slug {slug!r}: must be lowercase, start with a letter, "
                "and contain only letters/digits (e.g. 'ndl', 'wwoba')."
            )

    @staticmethod
    def validate_url(url: str) -> None:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise RegistryError(
                f"Invalid URL {url!r}: must be an http(s) URL with a host "
                "(e.g. 'https://statsplus.net/ndl/api')."
            )

    @staticmethod
    def validate_token(token: str) -> None:
        if not isinstance(token, str) or not _TOKEN_RE.match(token):
            raise RegistryError(
                f"Invalid token {token!r}: expected a UUID like "
                "'019e134f-1287-7d89-bda3-fc8928b1cb68'."
            )

    @staticmethod
    def validate_league_ids(ids: Dict[str, Any]) -> None:
        if not isinstance(ids, dict):
            raise RegistryError("league_ids must be a dict of level -> [ids].")
        for level, lids in ids.items():
            if not isinstance(lids, list) or not all(isinstance(x, int) for x in lids):
                raise RegistryError(
                    f"league_ids[{level!r}] must be a list of integer league IDs, got {lids!r}."
                )


# Convenience module-level functions over a default-dir registry --------------

def list_leagues() -> List[str]:
    return LeagueRegistry().slugs()


def load_league(slug: str) -> LeagueConfig:
    return LeagueRegistry().load(slug)


__all__ = [
    "LeagueRegistry",
    "LeagueConfig",
    "RegistryError",
    "list_leagues",
    "load_league",
    "CONFIG_DIR",
]


if __name__ == "__main__":  # pragma: no cover — quick CLI smoke
    reg = LeagueRegistry()
    print("Known leagues:", ", ".join(reg.slugs()))
    for s in reg.slugs():
        status = reg.files_status(s)
        have = [k for k, v in status.items() if v]
        print(f"  {s:6} url={status['url']} token={status['token']} "
              f"settings={status['settings']} ids={status['league_ids']} "
              f"| files: {', '.join(have)}")
