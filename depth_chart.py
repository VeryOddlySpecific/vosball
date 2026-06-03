#!/usr/bin/env python3
"""
depth_chart.py — Build an ideal depth chart, lineups, and pitching staff for one
organization at one league level, and surface promotion / replacement / demotion
candidates from the level below.

Inputs
------
- Latest evaluation_summary_{league}_*.csv (VOS scores + per-position scores).
- StatsPlus v2 stat endpoints (hitter/pitcher/fielder) for current + prior years.
- config/depth_config.json (per-level roster sizes, role counts, weights).

Composite score
---------------
For each player, blend VOS with a level-relative stats z-score:
    composite = ratings_weight * VOS + stats_weight * (50 + 15*z)

Hitter z-score: weighted mean of z(wOBA), z(BB%-K%), z(SB%) — with vs-L and vs-R
variants for lineup construction.
Pitcher z-score: weighted mean of z(-FIP), z(K-BB%), z(WHIP-inverted), z(GB%).

Output
------
- {league}/depth/{org}_{level}_{ts}.md   (full report)
- {league}/depth/{org}_{level}_{ts}.csv  (player-level composite + slot)

Usage
-----
    python depth_chart.py --league sahl --org "Atlanta Braves" --level AAA \\
        --year 2026
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import math
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import stats as sapi  # local module

logger = logging.getLogger(__name__)
SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG = SCRIPT_DIR / "config" / "depth_config.json"
DEFAULT_LEAGUE_URL = SCRIPT_DIR / "config" / "league_url.json"
DEFAULT_LEAGUE_IDS = SCRIPT_DIR / "config" / "league_ids.json"
DEFAULT_LEAGUE_SETTINGS = SCRIPT_DIR / "config" / "league_settings.json"

HITTER_POSITIONS = ["C", "1B", "2B", "3B", "SS", "LF", "CF", "RF", "DH"]
PITCHER_POSITIONS = {"SP", "RP", "CL", "P"}

# -----------------------------------------------------------------------------
# CLI / config
# -----------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Build a depth chart and lineup recommendations for one org / level.")
    p.add_argument("--league", required=True, help="League slug (e.g. sahl).")
    p.add_argument("--org", default=None, help="Organization name as it appears in the eval Org column. Required unless --all-orgs is passed.")
    p.add_argument("--all-orgs", action="store_true",
                   help="Iterate every org in {league}'s teams config. Combine with --all-level-charts to build a full report set for the whole league in one run.")
    p.add_argument("--park-factors", type=Path, default=None,
                   help="Path to combined teams[] park-factors file. Only used for auto-mapping team names to per-org eval subdirectory codes when --all-orgs is set.")
    p.add_argument("--level", default=None, help="Target level (e.g. ML, AAA, AA, A+, A, A-, R). Required unless --all-level-charts is passed.")
    p.add_argument("--all-level-charts", action="store_true",
                   help="Build a depth chart for every level in depth_config.json in one run (single shared timestamp).")
    p.add_argument("--no-pdf", action="store_true",
                   help="Skip PDF generation for the org summary (multi-level runs only).")
    p.add_argument("--org-code", type=str, default=None,
                   help="Subdirectory under {league}/eval/ to look in first for per-org evals (e.g. 'hou' for sahl/eval/hou/). Falls back to top-level eval/ if missing.")
    p.add_argument("--year", type=int, default=None, help="Latest year for stats window (default: current calendar year).")
    p.add_argument("--input", type=Path, default=None, help="Override evaluation_summary CSV.")
    p.add_argument("--config", type=Path, default=DEFAULT_CONFIG, help="depth_config.json path.")
    p.add_argument("--league-url-config", type=Path, default=DEFAULT_LEAGUE_URL)
    p.add_argument("--league-ids-config", type=Path, default=DEFAULT_LEAGUE_IDS,
                   help="JSON mapping {league_slug -> {level -> [lid, ...]}} for stats endpoints.")
    p.add_argument("--base-url", type=str, default=None, help="Override league API base URL.")
    p.add_argument("--output-dir", type=Path, default=None, help="Output directory (default: {league}/depth/).")
    p.add_argument("--no-archive", action="store_true",
                   help="Skip the auto-archive step. By default, the script moves any pre-existing "
                        "files in the output directory into an archive/ subdirectory before writing "
                        "this run's outputs, so the top-level depth/ folder only contains the most "
                        "recent run. Pass --no-archive to keep prior outputs alongside the new ones.")
    p.add_argument("--no-stats", action="store_true", help="Skip stat fetch; composite uses VOS only (debugging).")
    p.add_argument("--no-cache", action="store_true",
                   help="Skip disk cache; force fresh API fetches even if today's responses are cached.")
    p.add_argument("--cache-dir", type=Path, default=None,
                   help="Override cache directory (default: {league}/cache/stats/).")
    p.add_argument("--all-levels", action="store_true",
                   help="Fetch stats for every level in league_ids.json (slower; useful when prior-year stats span multiple levels).")
    p.add_argument("--lids", type=str, default=None,
                   help="Comma-separated lid override (e.g. '154,155,156'). Bypasses league_ids.json lookup.")
    p.add_argument("--no-players-override", action="store_true",
                   help="Skip the /players API override of League_Level/Org/Team. The depth chart will "
                        "trust whatever the eval CSV says — useful only when the API is down or you "
                        "specifically want to reproduce an old eval-driven run.")
    p.add_argument("--players-override-csv", type=Path, default=None, action="append",
                   help="Path to an OOTP roster CSV export with columns ID, Lev, ORG (and "
                        "optionally TM, INJ, Left). Applied as a PATCH on top of the /players "
                        "API payload — entries here win where they overlap. Use to reflect "
                        "in-app roster moves between sims when the API hasn't refreshed yet. "
                        "Repeatable — pass once per org export.")
    p.add_argument("--include-inactive", action="store_true",
                   help="Keep retired/DFA/waivered/DL60 players in the depth chart instead of "
                        "filtering them out. Short DL (is_on_dl) is never auto-filtered — it only "
                        "appears as a status flag on the player.")
    p.add_argument("--min-comp", type=float, default=None,
                   help="Minimum composite score (20-80 scale) required for a player to occupy "
                        "the Starter slot at any position. Players below this bar can still appear "
                        "as Util1/Util2/Def Sub. Use when shopping for FA upgrades — empty starter "
                        "slots highlight where the roster needs help. Composite is already "
                        "position-adjusted (via VOS), so one global threshold applies fairly across "
                        "C/SS/2B/etc.")
    p.add_argument("--min-comp-pos", type=str, default=None,
                   help="Per-position overrides for --min-comp, e.g. 'C:50,SS:52,1B:58'. "
                        "Unlisted positions fall back to --min-comp (or no threshold if --min-comp "
                        "is also unset). Useful when you want to demand a higher bar at a premium "
                        "spot or relax it for a position with no good options.")
    p.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    return p.parse_args()


def parse_min_comp_pos(spec: Optional[str]) -> Dict[str, float]:
    """Parse a 'C:50,SS:52,...' spec into {pos: threshold}.

    Tolerates whitespace and lowercased position names. Raises ValueError on
    malformed entries so the user gets a clear error at startup rather than a
    silent miss when a typo'd position gets ignored downstream.
    """
    if not spec:
        return {}
    out: Dict[str, float] = {}
    for chunk in spec.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if ":" not in chunk:
            raise ValueError(f"--min-comp-pos: bad entry '{chunk}' (expected POS:VALUE)")
        pos, val = chunk.split(":", 1)
        pos = pos.strip().upper()
        if pos not in HITTER_POSITIONS:
            raise ValueError(
                f"--min-comp-pos: unknown position '{pos}'. "
                f"Valid: {', '.join(HITTER_POSITIONS)}"
            )
        try:
            out[pos] = float(val.strip())
        except ValueError as exc:
            raise ValueError(f"--min-comp-pos: bad value for {pos}: '{val.strip()}'") from exc
    return out


def resolve_min_comp(pos: str, global_min: Optional[float], per_pos: Dict[str, float]) -> Optional[float]:
    """Per-position threshold lookup. Per-pos override wins over global."""
    if pos in per_pos:
        return per_pos[pos]
    return global_min


def load_config(path: Path) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        cfg = json.load(f)
    if "levels" not in cfg or not isinstance(cfg["levels"], dict):
        raise ValueError(f"Bad config: 'levels' missing in {path}")
    return cfg


def load_league_ids(path: Path) -> Dict[str, Dict[str, List[int]]]:
    """Load league_ids.json. Returns empty dict if file missing/invalid."""
    if not path.exists():
        logger.warning("league_ids config not found: %s", path)
        return {}
    with path.open("r", encoding="utf-8") as f:
        raw = json.load(f)
    if not isinstance(raw, dict):
        return {}
    out: Dict[str, Dict[str, List[int]]] = {}
    for league, levels in raw.items():
        if league.startswith("_") or not isinstance(levels, dict):
            continue
        cleaned: Dict[str, List[int]] = {}
        for lvl, ids in levels.items():
            if lvl.startswith("_") or not isinstance(ids, list):
                continue
            cleaned[lvl.upper()] = [int(x) for x in ids if isinstance(x, (int, str)) and str(x).strip().isdigit()]
        out[league.lower()] = cleaned
    return out


def league_default_year(league: str, settings_path: Path = DEFAULT_LEAGUE_SETTINGS) -> Optional[int]:
    """In-game season year for ``league`` from league_settings.json, or None.

    OOTP leagues run in their own season (e.g. ndl=2055, sahl=2061), which almost
    never matches the real-world calendar year. Defaulting --year to
    ``datetime.now().year`` fetches a stats window with no data for the live
    season, so the stat-blend silently degrades to ratings-only (empty wOBA/FIP).
    Reading the configured season fixes the stats join without the user having to
    pass --year on every invocation.
    """
    try:
        with settings_path.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    entry = data.get(league.lower()) if isinstance(data, dict) else None
    if isinstance(entry, dict):
        try:
            y = entry.get("year")
            return int(y) if y is not None else None
        except (TypeError, ValueError):
            return None
    return None


def resolve_lids(
    league: str,
    level: str,
    level_below: Optional[str],
    league_ids_map: Dict[str, Dict[str, List[int]]],
    all_levels: bool,
    cli_override: Optional[str],
) -> List[int]:
    """Decide which lids to fetch.

    Order of precedence: --lids override → --all-levels (everything for the league)
    → target level + level_below from config. Returns [] if no mapping found
    (caller should warn — without lids the API returns ML only).
    """
    if cli_override:
        return [int(x.strip()) for x in cli_override.split(",") if x.strip().isdigit()]
    league_map = league_ids_map.get(league.lower(), {})
    if not league_map:
        return []
    if all_levels:
        seen: set = set()
        out: List[int] = []
        for ids in league_map.values():
            for lid in ids:
                if lid not in seen:
                    seen.add(lid)
                    out.append(lid)
        return out
    out = list(league_map.get(level.upper(), []))
    if level_below:
        for lid in league_map.get(level_below.upper(), []):
            if lid not in out:
                out.append(lid)
    return out


def _default_park_factors_path(league: str) -> Path:
    """Best-effort default location of the combined teams[] park-factors file.

    We prefer ``config/{league}-park-factors.json`` because that's the format
    that holds the full league mapping ``team_name -> team_code``. The
    ``config/park-factors-{league}.json`` shape is a single-team file in many
    leagues and won't have every org's code.
    """
    primary = SCRIPT_DIR / "config" / f"{league}-park-factors.json"
    if primary.exists():
        return primary
    return SCRIPT_DIR / "config" / f"park-factors-{league}.json"


def _name_to_code_map(park_factors_path: Optional[Path]) -> Dict[str, str]:
    """Load {team_display_name: team_code_lower} from a combined teams[] park-
    factors file. Returns {} if the file is missing or doesn't have a teams
    block."""
    if not park_factors_path or not park_factors_path.exists():
        return {}
    try:
        with park_factors_path.open("r", encoding="utf-8") as f:
            pf = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Could not read park-factors %s: %s", park_factors_path, exc)
        return {}
    out: Dict[str, str] = {}
    if isinstance(pf, dict):
        tb = pf.get("teams")
        if isinstance(tb, dict):
            for team_name, block in tb.items():
                if team_name.startswith("_") or not isinstance(block, dict):
                    continue
                code = ((block.get("team_info") or {}).get("team_code") or "").strip().lower()
                if code:
                    out[team_name] = code
        # Single-team format: top-level team_info.
        if not out:
            info = pf.get("team_info")
            if isinstance(info, dict):
                code = (info.get("team_code") or "").strip().lower()
                name = (info.get("team_name") or "").strip()
                if code and name:
                    out[name] = code
    return out


def filename_org_slug(args: argparse.Namespace) -> str:
    """Return the slug to use in depth-chart output filenames.

    Priority:
      1. ``args.org_code`` if set (lowercased).
      2. Code resolved from ``args.park_factors`` (or the league's default
         park-factors file) keyed by ``args.org``.
      3. Fallback to the legacy ``org.lower().replace(" ", "_")`` slug — with a
         warning so the user knows nothing was resolved.

    The fallback path also lower-cases and strips spaces from the org name so
    downstream regexes don't have to deal with mixed case.
    """
    code = (getattr(args, "org_code", None) or "").strip().lower()
    if code:
        return code

    pf_path = args.park_factors or _default_park_factors_path(args.league)
    mapping = _name_to_code_map(pf_path)
    if args.org and args.org in mapping:
        resolved = mapping[args.org]
        # Cache back onto args so downstream code (eval lookup etc.) sees it.
        args.org_code = resolved
        return resolved

    logger.warning(
        "No team code resolved for org=%r (park-factors=%s). Falling back to "
        "slugified org name in filenames; pass --org-code or --park-factors "
        "to use the team code.",
        args.org, pf_path,
    )
    return (args.org or "").lower().replace(" ", "_")


def resolve_all_orgs(
    league: str,
    config_dir: Path,
    park_factors_path: Optional[Path],
) -> List[Tuple[str, Optional[str]]]:
    """Return [(team_display_name, org_code_or_None), ...] for every ML org in the league.

    Resolution priority:
      1. If ``park_factors_path`` is provided and the file is in combined
         teams[] format, use the teams[] block as the canonical org list.
         This is preferred — the park-factors file is hand-curated to only
         include real MLB orgs and excludes independents, NPB, KBO, college,
         winter leagues, etc., AND carries the team_code mapping needed for
         per-org eval lookups.
      2. ``config/{league}_orgs.json`` — flat JSON array of org display names.
         Use this for leagues that don't have a combined park-factors file
         (e.g. uniform-park-factors leagues like SDMB). If park_factors_path
         is also present, codes from it are merged in; otherwise codes are None.
      3. Fallback: scan teams-{league}.json for entries with Parent==0.
         WARNING: this catches more than just MLB orgs in many leagues
         (independent leagues, international, college often also have
         Parent==0). A warning is logged.
    """
    teams_path = config_dir / f"teams-{league}.json"
    if not teams_path.exists():
        raise FileNotFoundError(f"Teams config not found: {teams_path}")
    with teams_path.open("r", encoding="utf-8") as f:
        raw = json.load(f)

    # Build {team_display_name: team_code} from park-factors if provided.
    name_to_code: Dict[str, str] = {}
    teams_block: Dict[str, Any] = {}
    if park_factors_path and park_factors_path.exists():
        with park_factors_path.open("r", encoding="utf-8") as f:
            pf = json.load(f)
        if isinstance(pf, dict):
            tb = pf.get("teams")
            if isinstance(tb, dict):
                teams_block = tb
                for team_name, block in tb.items():
                    if team_name.startswith("_") or not isinstance(block, dict):
                        continue
                    code = ((block.get("team_info") or {}).get("team_code") or "").strip().lower()
                    if code:
                        name_to_code[team_name] = code

    # Path 1: park-factors is the canonical list when available.
    if teams_block:
        orgs: List[Tuple[str, Optional[str]]] = []
        for team_name in teams_block.keys():
            if team_name.startswith("_"):
                continue
            orgs.append((team_name, name_to_code.get(team_name)))
        return orgs

    # Path 2: explicit flat orgs file. Lets uniform-park-factors leagues
    # scope --all-orgs without maintaining a full teams[] park-factors file.
    orgs_file = config_dir / f"{league}_orgs.json"
    if orgs_file.exists():
        try:
            with orgs_file.open("r", encoding="utf-8") as f:
                names = json.load(f)
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Could not read %s: %s — falling through.", orgs_file, exc)
        else:
            if not isinstance(names, list):
                logger.warning("%s is not a JSON array — falling through.", orgs_file)
            else:
                orgs = []
                for n in names:
                    if not isinstance(n, str):
                        continue
                    nm = n.strip()
                    if not nm:
                        continue
                    orgs.append((nm, name_to_code.get(nm)))
                if orgs:
                    logger.info("--all-orgs using orgs list from %s (%d orgs).",
                                orgs_file, len(orgs))
                    return orgs

    # Path 3: fall back to teams config with Parent==0. This is fragile in
    # leagues that have many non-affiliated teams (independents, intl, etc.).
    logger.warning(
        "resolve_all_orgs falling back to Parent==0 filter from teams-%s.json. "
        "This catches non-MLB teams (indy leagues, NPB, KBO, college) too. "
        "Provide either config/%s-park-factors.json (combined teams[] format) "
        "or config/%s_orgs.json (flat JSON array of org names) to scope cleanly.",
        league, league, league,
    )
    orgs = []
    for tid_str, info in raw.items():
        if tid_str.startswith("_") or not isinstance(info, dict):
            continue
        parent = info.get("Parent", 0)
        try:
            parent_int = int(parent) if parent != "" else 0
        except (TypeError, ValueError):
            parent_int = 0
        if parent_int != 0:
            continue
        name = (info.get("Name") or "").strip()
        nick = (info.get("Nickname") or "").strip()
        display = f"{name} {nick}".strip()
        if not display:
            continue
        orgs.append((display, None))
    return orgs


def archive_previous_runs(depth_dir: Path) -> Tuple[int, Optional[Path]]:
    """Move any files currently sitting at the top of ``depth_dir`` into an
    ``archive/`` subdirectory, preserving filenames. Subdirectories (including
    a pre-existing ``archive/``) are left in place.

    Returns ``(moved_count, archive_dir)``. ``moved_count`` is 0 when there's
    nothing to archive (first run, or the dir is already clean). Caller is
    responsible for logging.

    Errors on individual moves are swallowed and logged at WARNING level — a
    single sticky file shouldn't break the whole run. The archive dir is
    created only when there's actually something to move.
    """
    if not depth_dir.exists():
        return 0, None
    pending = [p for p in depth_dir.iterdir() if p.is_file()]
    if not pending:
        return 0, None
    archive_dir = depth_dir / "archive"
    archive_dir.mkdir(exist_ok=True)
    moved = 0
    for src in pending:
        dst = archive_dir / src.name
        # Disambiguate if a file with the same name was already archived from
        # a prior run (timestamps make collisions unlikely, but be defensive).
        if dst.exists():
            stem, suffix = dst.stem, dst.suffix
            i = 1
            while True:
                candidate = archive_dir / f"{stem}__dup{i}{suffix}"
                if not candidate.exists():
                    dst = candidate
                    break
                i += 1
        try:
            src.rename(dst)
            moved += 1
        except OSError as exc:
            logger.warning("Could not archive %s: %s", src.name, exc)
    return moved, archive_dir


def find_latest_eval(league: str, override: Optional[Path], org_code: Optional[str] = None) -> Path:
    """Resolve the eval CSV path. Precedence:

    1. ``override`` (--input) — explicit path wins.
    2. ``{league}/eval/{org_code}/`` — per-org subdir produced by ``vos_v2 --per-org-evals``.
       Falls through to (3) if the subdir is empty / missing.
    3. ``{league}/eval/`` — default top-level location.
    """
    if override is not None:
        if not override.exists():
            raise FileNotFoundError(f"--input not found: {override}")
        return override

    pattern = f"evaluation_summary_{league}_*.csv"

    if org_code:
        org_dir = SCRIPT_DIR / league / "eval" / org_code.strip().lower()
        if org_dir.exists():
            matches = sorted(org_dir.glob(pattern), key=lambda p: p.name)
            if matches:
                return matches[-1]
            logger.warning("No eval found under %s; falling back to %s", org_dir, org_dir.parent)

    eval_dir = SCRIPT_DIR / league / "eval"
    matches = sorted(eval_dir.glob(pattern), key=lambda p: p.name)
    if not matches:
        raise FileNotFoundError(f"No evaluation_summary CSV under {eval_dir}")
    return matches[-1]


_EVAL_TS_RE = re.compile(r"_(\d{8}_\d{6})\.csv$")


def eval_ts_from_path(path: Optional[Path]) -> Optional[str]:
    """Extract the ``YYYYMMDD_HHMMSS`` timestamp embedded in an eval filename
    (``evaluation_summary_{league}_{ts}.csv``). Returns None when ``path`` is
    None or the name doesn't carry a timestamp. Used for depth-batch provenance
    so free_agent_market.py can tell a stale batch from a fresh one.
    """
    if path is None:
        return None
    m = _EVAL_TS_RE.search(path.name)
    return m.group(1) if m else None


def write_depth_meta(
    out_dir: Path,
    org_slug: str,
    ts: str,
    eval_path: Optional[Path],
    levels: List[str],
    args: argparse.Namespace,
) -> Path:
    """Write a per-batch provenance sidecar recording which eval the depth
    charts were built from, the levels covered, and the starter min-comp
    settings used.

    Always written — even without ``--min-comp`` — because freshness must be
    knowable regardless of whether a starter_gaps sidecar exists. Lets
    free_agent_market.py detect a depth batch built from an older eval than the
    current latest and regenerate before scanning, and replay the same
    thresholds when it does.

    Lands in ``{league}/depth/`` alongside the batch (per-league siloed) and
    carries the batch ``ts`` so it archives with the rest of the run.
    """
    payload = {
        "org_slug": org_slug,
        "batch_ts": ts,
        "source_eval": eval_path.name if eval_path else None,
        "source_eval_ts": eval_ts_from_path(eval_path),
        "levels": list(levels),
        "min_comp_global": getattr(args, "min_comp", None),
        "min_comp_per_pos": getattr(args, "min_comp_pos_map", {}) or {},
    }
    out_path = out_dir / f"{org_slug}_{ts}_depth_meta.json"
    out_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return out_path


# -----------------------------------------------------------------------------
# Eval CSV loading
# -----------------------------------------------------------------------------

def read_eval(path: Path) -> List[Dict[str, str]]:
    with path.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        return list(reader)


def to_float(v: Any, default: float = 0.0) -> float:
    try:
        s = str(v).strip()
        return float(s) if s else default
    except (TypeError, ValueError):
        return default


# -----------------------------------------------------------------------------
# /players-driven overrides (current team / level / status)
# -----------------------------------------------------------------------------
#
# The eval CSV's League_Level / Org / Team are a snapshot from the moment
# vos_v2 ran. Promotions, demotions, trades, DFAs, and DL placements that
# happen after that snapshot won't be reflected. The /players endpoint, on
# the other hand, is refreshed by StatsPlus daily and carries each player's
# current Level + Organization ID + status flags. We use it as the source
# of truth for roster membership before depth chart construction.
#
# Players in /players but not in eval: skipped (no VOS scores → can't be slotted).
# Players in eval but not in /players: kept with their eval values (best
#   effort fallback — usually means a brand-new player or an API hiccup).

DEFAULT_ID_MAPS = SCRIPT_DIR / "config" / "id_maps.json"
TEAMS_FILENAME_TEMPLATE = "teams-{league}.json"

# OOTP roster export uses a few labels that don't match depth_config's level
# taxonomy. These get translated when building the override patch.
#   MLB    -> ML
#   DSL    -> R   (Dominican Summer League is rookie ball)
#   ACL    -> R   (Arizona Complex League is rookie ball)
#   INT    -> skip (international roster slot, not an active level)
OOTP_LEVEL_TRANSLATIONS = {"MLB": "ML", "DSL": "R", "ACL": "R"}
OOTP_LEVEL_SKIP = {"INT"}


def build_players_lookup_from_csv(
    csv_paths: List[Path],
    team_name_to_id: Dict[str, int],
) -> Dict[str, Dict[str, str]]:
    """Build a /players-shaped lookup dict from one or more OOTP roster CSV exports.

    Expected columns (per the OOTP "Roster (Default)" export — extras are
    ignored, missing ones degrade gracefully):
        ID            — player ID, joins to eval CSV's ID column
        Lev           — level label (MLB/AAA/AA/A+/A/A-/R/INT/DSL/ACL)
        ORG           — full org display name, looked up via team_name_to_id
        TM            — affiliate team name (best-effort; we don't reverse-map)
        INJ           — "Yes" or "-"; sets is_on_dl when Yes
        Left          — injury duration text (informational only)

    Returned dict mirrors the shape ``sapi.build_players_lookup`` produces, so
    ``apply_players_override`` consumes it without modification.
    """
    if not csv_paths:
        return {}
    out: Dict[str, Dict[str, str]] = {}
    for csv_path in csv_paths:
        # Resolve against CWD first (default Path behavior), then fall back to
        # SCRIPT_DIR so relative paths work no matter where the user runs from.
        resolved = csv_path
        if not resolved.exists():
            alt = SCRIPT_DIR / csv_path
            if alt.exists():
                resolved = alt
                logger.info("Override CSV resolved relative to script dir: %s", resolved)
        if not resolved.exists():
            logger.warning(
                "Override CSV not found: %s (also tried %s) — skipping.",
                csv_path, SCRIPT_DIR / csv_path,
            )
            continue
        csv_path = resolved
        with csv_path.open("r", encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            row_count = 0
            skipped_int = 0
            unmapped_org = 0
            for row in reader:
                pid = (row.get("ID") or "").strip()
                if not pid:
                    continue
                lev_raw = (row.get("Lev") or "").strip().upper()
                if lev_raw in OOTP_LEVEL_SKIP:
                    skipped_int += 1
                    continue
                level_label = OOTP_LEVEL_TRANSLATIONS.get(lev_raw, lev_raw)
                entry: Dict[str, str] = {}
                if level_label:
                    entry["level"] = level_label  # passes through as-label in _resolve_player_level_label
                org_name = (row.get("ORG") or "").strip()
                if org_name:
                    org_id = team_name_to_id.get(org_name)
                    if org_id is not None:
                        entry["organization_id"] = str(org_id)
                    else:
                        unmapped_org += 1
                inj_raw = (row.get("INJ") or "").strip().lower()
                if inj_raw == "yes":
                    entry["is_on_dl"] = "1"
                if entry:
                    out[pid] = entry
                    row_count += 1
        logger.info(
            "Loaded override CSV %s: %d rows | %d skipped (INT) | %d org unmapped",
            csv_path.name, row_count, skipped_int, unmapped_org,
        )
    return out


def invert_team_id_to_name(team_id_to_name: Dict[int, str]) -> Dict[str, int]:
    """Reverse-map for CSV org name -> team id. Returns first id seen for any
    duplicated names (shouldn't happen in a well-formed teams config)."""
    out: Dict[str, int] = {}
    for tid, name in team_id_to_name.items():
        if name and name not in out:
            out[name] = tid
    return out


def load_level_id_to_label(path: Path = DEFAULT_ID_MAPS) -> Dict[int, str]:
    """Invert id_maps.json's league_level dict: {1: 'ML', 2: 'AAA', ...}."""
    if not path.exists():
        logger.warning("id_maps not found: %s", path)
        return {}
    with path.open("r", encoding="utf-8") as f:
        raw = json.load(f)
    level_map = (raw or {}).get("league_level") or {}
    out: Dict[int, str] = {}
    for label, value in level_map.items():
        if not isinstance(label, str) or label.startswith("_"):
            continue
        try:
            out[int(value)] = label
        except (TypeError, ValueError):
            continue
    return out


def load_team_id_to_name(league: str, config_dir: Path = SCRIPT_DIR / "config") -> Dict[int, str]:
    """Build {team_id: 'Name Nickname'} from teams-{league}.json."""
    path = config_dir / TEAMS_FILENAME_TEMPLATE.format(league=league.lower())
    if not path.exists():
        logger.warning("Teams config not found: %s", path)
        return {}
    with path.open("r", encoding="utf-8") as f:
        raw = json.load(f)
    if not isinstance(raw, dict):
        return {}
    out: Dict[int, str] = {}
    for tid_str, info in raw.items():
        if tid_str.startswith("_") or not isinstance(info, dict):
            continue
        try:
            tid = int(tid_str)
        except (TypeError, ValueError):
            continue
        name = (info.get("Name") or "").strip()
        nick = (info.get("Nickname") or "").strip()
        display = f"{name} {nick}".strip()
        if display:
            out[tid] = display
    return out


def _bool_from_value(value: object) -> bool:
    """Tolerant truthy check for /players boolean fields ('1', 'True', 'yes', etc.)."""
    text = str(value).strip().lower()
    return text in ("1", "true", "yes", "y", "t")


def _resolve_player_level_label(
    player_meta: Dict[str, str],
    level_id_to_label: Dict[int, str],
) -> Optional[str]:
    """Map /players 'level' field (numeric per api.txt) to a depth_config label.

    Returns None when the level is unrecognized or missing — caller should
    fall back to the eval value in that case.
    """
    raw = (player_meta.get("level") or "").strip()
    if not raw:
        return None
    # Handle either numeric ID or already-labeled string ('ML', 'AAA', ...).
    try:
        lvl_id = int(raw)
    except ValueError:
        # Already a label — pass through (e.g., 'ML').
        return raw.upper() if raw.isalpha() else raw
    return level_id_to_label.get(lvl_id)


def _is_inactive(player_meta: Dict[str, str]) -> Tuple[bool, str]:
    """Return (is_inactive, reason). Reason is a short tag for log output.

    NOTE: Deliberately does NOT check ``is_active``. In OOTP, ``is_active``
    means "on the major-league active roster" — minor leaguers all have
    ``is_active=0``, so checking it here would empty out every farm team.
    farm_value_old.py uses ``is_active`` correctly because it specifically
    wants ML market comparables.
    """
    if _bool_from_value(player_meta.get("retired")):
        return True, "retired"
    if _bool_from_value(player_meta.get("designated_for_assignment")):
        return True, "DFA"
    if _bool_from_value(player_meta.get("is_on_waivers")):
        return True, "waivers"
    if _bool_from_value(player_meta.get("is_on_dl60")):
        return True, "DL60"
    return False, ""


def apply_players_override(
    eval_rows: List[Dict[str, str]],
    players_lookup: Dict[str, Dict[str, str]],
    level_id_to_label: Dict[int, str],
    team_id_to_name: Dict[int, str],
    include_inactive: bool = False,
) -> Dict[str, int]:
    """Override League_Level / Team / Org on each eval row from the /players
    payload, and (unless ``include_inactive``) drop players that are retired,
    DFA'd, on waivers, or on the 60-day DL.

    Mutates ``eval_rows`` in place — drops filtered rows and edits surviving
    rows' League_Level / Team / Org / and tacks on _Status_Flags for any
    short-DL or secondary-roster signals the report can use.

    Returns a counts dict suitable for logging:
        {
            "total": int,
            "level_overrides": int,    # eval label != API label
            "org_overrides": int,
            "filtered_retired": int,
            "filtered_dfa": int,
            "filtered_waivers": int,
            "filtered_dl60": int,
            "missing_in_players": int,
            "unrecognized_level": int,
        }
    """
    counts = {
        "total": len(eval_rows),
        "level_overrides": 0,
        "org_overrides": 0,
        "filtered_retired": 0,
        "filtered_dfa": 0,
        "filtered_waivers": 0,
        "filtered_dl60": 0,
        "missing_in_players": 0,
        "unrecognized_level": 0,
    }
    if not players_lookup:
        return counts

    reason_to_count_key = {
        "retired": "filtered_retired",
        "DFA": "filtered_dfa",
        "waivers": "filtered_waivers",
        "DL60": "filtered_dl60",
    }

    surviving: List[Dict[str, str]] = []
    for row in eval_rows:
        pid = (row.get("ID") or "").strip()
        meta = players_lookup.get(pid) if pid else None
        if not meta:
            counts["missing_in_players"] += 1
            surviving.append(row)
            continue

        # Inactive filters first — no point overriding fields on a player
        # we're about to drop.
        inactive, reason = _is_inactive(meta)
        if inactive and not include_inactive:
            counts[reason_to_count_key.get(reason, "filtered_retired")] += 1
            logger.debug("Filtering %s (%s): %s", pid, row.get("Name", ""), reason)
            continue

        # Status flags — surface short DL / secondary / arb noise even when
        # the player isn't being filtered. Renderers can pick this up later.
        flags: List[str] = []
        if _bool_from_value(meta.get("is_on_dl")):
            flags.append("DL")
        if _bool_from_value(meta.get("is_on_dl60")):
            flags.append("DL60")
        if _bool_from_value(meta.get("is_on_secondary")):
            flags.append("Secondary")
        if _bool_from_value(meta.get("designated_for_assignment")):
            flags.append("DFA")
        if _bool_from_value(meta.get("is_on_waivers")):
            flags.append("Waivers")
        if flags:
            row["_Status_Flags"] = ",".join(flags)

        # Level override — only apply when the API has a mappable level.
        new_label = _resolve_player_level_label(meta, level_id_to_label)
        if new_label is None:
            counts["unrecognized_level"] += 1
        else:
            old_label = (row.get("League_Level") or "").strip()
            if old_label.upper() != new_label.upper():
                counts["level_overrides"] += 1
                row["League_Level"] = new_label

        # Org override — prefer Organization ID (set even when Parent Team ID
        # is blank for ML teams, per api.txt). Skip when no mapping or when
        # we'd be replacing a name with an empty string.
        org_id_raw = (meta.get("organization_id") or meta.get("parent_team_id") or "").strip()
        if org_id_raw:
            try:
                org_id = int(org_id_raw)
            except ValueError:
                org_id = None
            if org_id is not None:
                new_org = team_id_to_name.get(org_id)
                if new_org:
                    old_org = (row.get("Org") or "").strip()
                    if old_org != new_org:
                        counts["org_overrides"] += 1
                        row["Org"] = new_org

        # Team override — best-effort; only when team id resolves cleanly.
        team_id_raw = (meta.get("team_id") or "").strip()
        if team_id_raw:
            try:
                tid = int(team_id_raw)
            except ValueError:
                tid = None
            if tid is not None:
                new_team = team_id_to_name.get(tid)
                if new_team:
                    row["Team"] = new_team

        surviving.append(row)

    eval_rows[:] = surviving
    return counts


def is_pitcher(eval_row: Dict[str, str]) -> bool:
    pos = (eval_row.get("Pos") or "").strip().upper()
    return pos in PITCHER_POSITIONS


def projected_role_for_pitcher(eval_row: Dict[str, str]) -> str:
    """Return 'SP' or 'RP' as the depth-chart role bucket."""
    proj = (eval_row.get("Projected_Position") or "").strip().upper()
    if proj == "SP":
        return "SP"
    if proj in {"RP", "CL"}:
        return "RP"
    pos = (eval_row.get("Pos") or "").strip().upper()
    return "SP" if pos in {"SP", "P"} else "RP"


# -----------------------------------------------------------------------------
# Z-score helpers
# -----------------------------------------------------------------------------

def _mean_std(vals: List[float]) -> Tuple[float, float]:
    if not vals:
        return 0.0, 0.0
    n = len(vals)
    mean = sum(vals) / n
    if n < 2:
        return mean, 0.0
    var = sum((v - mean) ** 2 for v in vals) / (n - 1)
    return mean, math.sqrt(var) if var > 0 else 0.0


def _z(value: float, mean: float, std: float) -> float:
    if std <= 0:
        return 0.0
    return (value - mean) / std


def _to_2080(z: float) -> float:
    return max(20.0, min(80.0, 50.0 + 15.0 * z))


# -----------------------------------------------------------------------------
# Stats compositing
# -----------------------------------------------------------------------------

# Hitter component definitions: (key, sign, weight) — sign is +1 for "more is
# better" and -1 for "less is better". Weights normalize across present
# components.
HITTER_COMPONENTS = [
    ("wOBA", +1, 0.55),
    ("BB%", +1, 0.10),
    ("K%", -1, 0.10),
    ("ISO", +1, 0.15),
    ("SB%", +1, 0.05),
    ("OBP", +1, 0.05),
]

# Pitcher components for SP/RP — same shape.
PITCHER_COMPONENTS = [
    ("FIP", -1, 0.40),
    ("K-BB%", +1, 0.30),
    ("WHIP", -1, 0.15),
    ("GB%", +1, 0.05),
    ("HR/9", -1, 0.10),
]


def _norm_weights(present_weights: List[float]) -> List[float]:
    s = sum(present_weights)
    if s <= 0:
        return present_weights
    return [w / s for w in present_weights]


def _composite_from_components(
    player_stats: Dict[str, float],
    components: List[Tuple[str, int, float]],
    means: Dict[str, float],
    stds: Dict[str, float],
) -> float:
    """Weighted-average z-score across present components, mapped to 20-80."""
    pieces: List[Tuple[float, float]] = []  # (z, weight)
    for key, sign, weight in components:
        if key not in player_stats or key not in stds:
            continue
        val = player_stats[key]
        z = _z(val, means[key], stds[key]) * sign
        pieces.append((z, weight))
    if not pieces:
        return 50.0
    weights = _norm_weights([w for _, w in pieces])
    z_blend = sum(z * w for (z, _), w in zip(pieces, weights))
    return _to_2080(z_blend)


def hitter_stat_score(
    pid: str,
    hitters: Dict[str, Dict[str, Any]],
    means: Dict[str, float],
    stds: Dict[str, float],
    split: str = "overall",
    floors: Optional[Dict[str, float]] = None,
) -> Tuple[float, float]:
    """Returns (score_2080, sample_weight in [0,1])."""
    bundle = hitters.get(pid)
    if not bundle or split not in bundle:
        return 50.0, 0.0
    s = bundle[split]
    pa = float(s.get("PA", 0.0))
    score = _composite_from_components(s, HITTER_COMPONENTS, means, stds)
    sample_weight = _sample_weight(pa, floors or {}, "min_pa_full", "min_pa_partial")
    return score, sample_weight


def pitcher_stat_score(
    pid: str,
    pitchers: Dict[str, Dict[str, Any]],
    means: Dict[str, float],
    stds: Dict[str, float],
    floors: Optional[Dict[str, float]] = None,
) -> Tuple[float, float]:
    bundle = pitchers.get(pid)
    if not bundle:
        return 50.0, 0.0
    s = bundle["overall"]
    ip = float(s.get("IP", 0.0))
    outs = ip * 3.0
    score = _composite_from_components(s, PITCHER_COMPONENTS, means, stds)
    sample_weight = _sample_weight(outs, floors or {}, "min_outs_full", "min_outs_partial")
    return score, sample_weight


def _sample_weight(
    value: float,
    floors: Dict[str, float],
    full_key: str,
    partial_key: str,
) -> float:
    full = float(floors.get(full_key, 0.0))
    partial = float(floors.get(partial_key, 0.0))
    if value >= full:
        return 1.0
    if value <= partial:
        return 0.0
    if full <= partial:
        return 1.0
    return (value - partial) / (full - partial)


# In-process memo for compute_means_stds. Keyed on id(bundle) — the bundle
# dicts come from stats.build_player_stats, which is itself memoized, so the
# same dict object is reused across every (org, level) call. Identical id()
# guarantees identical contents in this codebase. components are hashable
# (immutable tuples), and split is a string.
_MEANS_STDS_CACHE: Dict[Tuple[int, Tuple[str, ...], Optional[str]], Tuple[Dict[str, float], Dict[str, float]]] = {}


def clear_means_stds_cache() -> None:
    """Drop the in-process compute_means_stds memo."""
    _MEANS_STDS_CACHE.clear()


def compute_means_stds(
    bundle: Dict[str, Dict[str, Any]],
    components: List[Tuple[str, int, float]],
    split: Optional[str] = None,
) -> Tuple[Dict[str, float], Dict[str, float]]:
    """Compute per-component (mean, std) across all players in `bundle`.

    Memoized in-process by ``id(bundle)``: the league-wide bundle is the same
    object across every per-org call (stats.build_player_stats is memoized
    upstream), so the second org onwards reuses the first org's z-score
    reference instead of re-iterating ~5,000 players' stat dicts.
    """
    cols = tuple(k for k, _, _ in components)
    cache_key = (id(bundle), cols, split)
    cached = _MEANS_STDS_CACHE.get(cache_key)
    if cached is not None:
        return cached

    by_col: Dict[str, List[float]] = {c: [] for c in cols}
    for v in bundle.values():
        s = v.get(split) if split else v.get("overall")
        if not isinstance(s, dict):
            continue
        for c in cols:
            if c in s:
                by_col[c].append(float(s[c]))
    means: Dict[str, float] = {}
    stds: Dict[str, float] = {}
    for c, vals in by_col.items():
        m, sd = _mean_std(vals)
        means[c] = m
        stds[c] = sd
    result = (means, stds)
    _MEANS_STDS_CACHE[cache_key] = result
    return result


# -----------------------------------------------------------------------------
# Depth chart construction
# -----------------------------------------------------------------------------

DEFAULT_AFFILIATE_PATTERN = r"\((ACL|DSL|FCL|VSL)\)"


def expand_levels_for_affiliates(
    levels_to_run: List[str],
    eval_rows: List[Dict[str, str]],
    org: str,
    cfg: Dict[str, Any],
) -> List[Tuple[str, str, Optional[str]]]:
    """For each level, if its config has ``split_by_affiliate`` set AND the
    org has more than one affiliate at that level, expand into one entry per
    affiliate. Otherwise the level passes through unchanged.

    Returns ``[(display_level, base_level, affiliate_or_None), ...]``.
        - ``display_level`` is what shows in filenames/headers (e.g. 'R-ACL')
        - ``base_level`` is the canonical config key (e.g. 'R')
        - ``affiliate`` is the suffix used to filter org_pool, or None when
          no split is in effect.
    """
    out: List[Tuple[str, str, Optional[str]]] = []
    org_l = (org or "").strip().lower()
    for lvl in levels_to_run:
        cfg_block = cfg.get("levels", {}).get(lvl, {}) or {}
        if not cfg_block.get("split_by_affiliate"):
            out.append((lvl, lvl, None))
            continue
        pattern = re.compile(cfg_block.get("affiliate_pattern") or DEFAULT_AFFILIATE_PATTERN)
        affiliates: set = set()
        lvl_u = lvl.upper()
        for row in eval_rows:
            if (row.get("Org") or "").strip().lower() != org_l:
                continue
            if (row.get("League_Level") or "").strip().upper() != lvl_u:
                continue
            m = pattern.search(row.get("Team") or "")
            if m:
                affiliates.add(m.group(1).upper())
        if len(affiliates) <= 1:
            # 0 = no players at this level for this org; 1 = single affiliate,
            # no split needed. Either way, fall back to the unsplit run.
            out.append((lvl, lvl, None))
            continue
        for aff in sorted(affiliates):
            out.append((f"{lvl}-{aff}", lvl, aff))
    return out


def org_pool(
    eval_rows: List[Dict[str, str]],
    org: str,
    level: str,
    affiliate: Optional[str] = None,
    affiliate_pattern: Optional[str] = None,
) -> List[Dict[str, str]]:
    """Filter eval rows for one (org, level) — optionally narrowing further to
    a single affiliate within a level (e.g. 'ACL' vs 'DSL' for R-ball).

    The affiliate is matched by regex against the player's Team field. The
    default pattern catches the OOTP convention of ``Arizona (ACL)`` etc.
    """
    org_l = org.strip().lower()
    lvl_l = level.strip().upper()
    rows = [
        r for r in eval_rows
        if (r.get("Org") or "").strip().lower() == org_l
        and (r.get("League_Level") or "").strip().upper() == lvl_l
    ]
    if not affiliate:
        return rows
    pattern = re.compile(affiliate_pattern or DEFAULT_AFFILIATE_PATTERN)
    aff_upper = affiliate.upper()
    return [
        r for r in rows
        if (m := pattern.search(r.get("Team") or "")) and m.group(1).upper() == aff_upper
    ]


def position_score(eval_row: Dict[str, str], pos: str) -> float:
    """Player's score at a specific position from eval CSV ({pos}_Score)."""
    return to_float(eval_row.get(f"{pos}_Score"), 0.0)


def assign_positions(
    pool: List[Dict[str, Any]],
    level_cfg: Dict[str, Any],
) -> Dict[str, List[Dict[str, Any]]]:
    """
    Greedy depth chart for hitter positions. Each iteration picks the (player, pos)
    pairing with the highest *blended* position score (ratings + stats) among
    un-slotted players. Falls back to the raw rating-only ``pos_scores`` map
    when the blended map is missing — keeps the function robust for callers
    that don't pre-compute blended scores.

    Returns: {pos: [tier1, tier2, ...]} ordered by tier.
    """
    hitter_count = int(level_cfg.get("hitter_count", 13))
    pos_min = level_cfg.get("hitter_position_min", {})
    positions = HITTER_POSITIONS
    desired_per_pos = {p: int(pos_min.get(p, 0)) for p in positions}

    # Build candidate scores: for each player, their score at each position.
    # Prefer the blended (ratings + stats) score; fall back to raw pos_scores
    # only when blending isn't available.
    candidates: List[Tuple[float, str, str]] = []  # (score, pos, pid)
    pid_to_player = {p["pid"]: p for p in pool}
    for p in pool:
        score_map = p.get("pos_scores_blended") or p.get("pos_scores") or {}
        for pos in positions:
            score = score_map.get(pos)
            if score is None or score <= 0:
                continue
            candidates.append((score, pos, p["pid"]))
    candidates.sort(key=lambda x: -x[0])

    placed: Dict[str, List[Dict[str, Any]]] = {p: [] for p in positions}
    used: set = set()
    pos_filled: Dict[str, int] = {p: 0 for p in positions}

    # Phase 1: satisfy per-position minimums.
    for score, pos, pid in candidates:
        if sum(pos_filled.values()) >= hitter_count:
            break
        if pid in used:
            continue
        if pos_filled[pos] >= desired_per_pos.get(pos, 0):
            continue
        placed[pos].append({**pid_to_player[pid], "_pos_score_here": score, "_assigned_pos": pos})
        used.add(pid)
        pos_filled[pos] += 1

    # Phase 2: fill remaining hitter slots with best-available player at any position.
    remaining = hitter_count - sum(pos_filled.values())
    if remaining > 0:
        for score, pos, pid in candidates:
            if pid in used:
                continue
            placed[pos].append({**pid_to_player[pid], "_pos_score_here": score, "_assigned_pos": pos})
            used.add(pid)
            remaining -= 1
            if remaining <= 0:
                break

    return placed


# -----------------------------------------------------------------------------
# OOTP-style position depth chart (Starter / Util1 / Util2 / Def Sub) +
# pinch hitter / pinch runner lists. This is a different view of the same
# hitter pool than ``assign_positions`` produces — that one allocates each
# player to exactly one position-tier slot for roster construction. This one
# lets a single player appear at multiple positions / slots, mirroring how
# OOTP's depth chart screen looks.
# -----------------------------------------------------------------------------

def _suggest_util_schedule(
    starter: Optional[Dict[str, Any]],
    util: Optional[Dict[str, Any]],
) -> str:
    """Pick an OOTP-style play schedule for a Util1 based on composite gap to
    the starter. Heuristic from `depth_chart --min-comp` design discussion:

        no starter (empty slot) → Ev. 2nd Game  (util is the would-be starter)
        gap <= 3                → Ev. 3rd Game
        gap <= 6                → Ev. 5th Game
        gap <= 10               → Ev. 8th Game
        gap > 10                → If Starter Tired

    Composite is on the 20-80 scale so absolute gaps are meaningful. Returns
    an empty string when there's no util — nothing to schedule.
    """
    if not util:
        return ""
    if not starter:
        return "Ev. 2nd Game"
    gap = float(starter.get("composite", 0.0)) - float(util.get("composite", 0.0))
    if gap <= 3:
        return "Ev. 3rd Game"
    if gap <= 6:
        return "Ev. 5th Game"
    if gap <= 10:
        return "Ev. 8th Game"
    return "If Starter Tired"


def build_position_depth_table(
    hitter_pool: List[Dict[str, Any]],
    starters_by_pos: Dict[str, Optional[Dict[str, Any]]],
    util_count: int = 2,
) -> Dict[str, Dict[str, Optional[Dict[str, Any]]]]:
    """For each position, return {starter, util1..utilN, def_sub, schedules}.

    Util slots: top-N players at this position by ``pos_score`` who are not
    the starter at this position. A starter at another position is allowed.

    Def Sub: best defender at this position (highest ``defense_score``) who
    isn't the starter and isn't already filling util1 (so the row reads as
    a meaningfully *different* fallback). Falls back to an existing util slot
    if no other defender qualifies. Skipped entirely for DH (no defense).

    ``schedules`` (str dict, keyed by slot name) carries the suggested OOTP
    play schedule for each util slot — see ``_suggest_util_schedule``.
    """
    util_count = max(1, int(util_count))
    out: Dict[str, Dict[str, Optional[Dict[str, Any]]]] = {}

    for pos in HITTER_POSITIONS:
        starter = starters_by_pos.get(pos)
        starter_pid = starter["pid"] if starter else None

        # Candidates: players viable at this position (raw pos_score > 0).
        # Sort by the *blended* score so util slots reflect both ratings and
        # current-year performance, matching the starter-slotting logic.
        # Viability still hinges on the raw rating score — a hot bat doesn't
        # make a player suddenly viable at a position the eval rejected.
        def _slot_score(player: Dict[str, Any]) -> float:
            blended = (player.get("pos_scores_blended") or {}).get(pos)
            if blended is not None:
                return blended
            return (player.get("pos_scores") or {}).get(pos, 0.0)

        candidates = [
            p for p in hitter_pool
            if (p.get("pos_scores") or {}).get(pos, 0.0) > 0
            and p["pid"] != starter_pid
        ]
        candidates.sort(key=lambda p: -_slot_score(p))

        slots: Dict[str, Optional[Dict[str, Any]]] = {"starter": starter}
        for i in range(util_count):
            slots[f"util{i + 1}"] = candidates[i] if i < len(candidates) else None

        # Def sub: skip entirely for DH (no fielding to back up). For everyone
        # else, highest defense_score among candidates who aren't util1. If
        # only util1 exists, def_sub draws from the broader pool (excluding
        # starter + util1).
        if pos == "DH":
            slots["def_sub"] = None
        else:
            util1 = slots.get("util1")
            util1_pid = util1["pid"] if util1 else None
            def_sub_pool = [
                p for p in hitter_pool
                if (p.get("pos_scores") or {}).get(pos, 0.0) > 0
                and p["pid"] not in {starter_pid, util1_pid}
            ]
            def_sub_pool.sort(key=lambda p: -p.get("defense_score", 0.0))
            slots["def_sub"] = def_sub_pool[0] if def_sub_pool else None

        # Suggested play schedule for each util slot — based on composite gap
        # to starter. Lets the renderer surface "this guy should play every
        # 3rd game, not just when the starter is tired" hints.
        schedules: Dict[str, str] = {}
        for i in range(util_count):
            key = f"util{i + 1}"
            schedules[key] = _suggest_util_schedule(starter, slots.get(key))
        slots["schedules"] = schedules  # type: ignore[assignment]

        out[pos] = slots

    return out


def _pinch_hitter_ranking_view(
    p: Dict[str, Any],
    pa_threshold: float,
) -> Tuple[float, float, float, str]:
    """Return (sort_key, current_pa, current_woba, basis) for a pinch hitter.

    Basis logic — per user spec: when in-season PA at this level is at or
    above ``pa_threshold``, rank by the in-season stat composite
    (``stat_score``); below the threshold, rank by the eval's
    ``Batting_Score``. Both metrics live on a 20-80 scale so they sort
    consistently in a single mixed list.
    """
    hb = p.get("hitter_bundle") or {}
    # Prefer the target-lid view (this level only) when present, else the
    # cross-level current view. _current_target is always stamped (zero-PA
    # placeholder for never-played-here players) by stats.build_player_stats.
    cur = hb.get("overall_current_target") or hb.get("overall_current") or {}
    pa_current = float(cur.get("PA", 0) or 0)
    woba_current = float(cur.get("wOBA", 0) or 0)

    if pa_current >= pa_threshold:
        # In-season composite (z-blend of wOBA / BB% / K% / ISO / SB% / OBP).
        # Already on the 20-80 scale via hitter_stat_score.
        return (float(p.get("stat_score", 50.0)), pa_current, woba_current, "Current")
    return (float(p.get("batting_score", 0.0)), pa_current, woba_current, "Eval")


def build_pinch_lists(
    hitter_pool: List[Dict[str, Any]],
    starters_by_pos: Dict[str, Optional[Dict[str, Any]]],
    pinch_hitter_count: int = 4,
    pinch_runner_count: int = 3,
    pinch_hitter_pa_threshold: float = 100.0,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Return (pinch_hitters, pinch_runners), top-N each.

    Pinch hitters: per-player, use in-season stat_score when current-year PA
    at this level is >= ``pinch_hitter_pa_threshold``; otherwise fall back to
    the eval's Batting_Score. Each surviving record is annotated with the
    ranking basis (``_pinch_basis`` = "Current" or "Eval"), the current PA,
    and the current wOBA so the renderer can show what drove the order.
    Pinch runners: ranked by ``baserunning_score``; players with a zero
    baserunning_score are excluded (catchers / station-to-station types).
    """
    starter_pids = {s["pid"] for s in starters_by_pos.values() if s}
    non_starters = [p for p in hitter_pool if p["pid"] not in starter_pids]

    # Pinch hitters — annotate, then sort by the basis-aware key.
    annotated: List[Tuple[float, Dict[str, Any]]] = []
    for p in non_starters:
        sort_key, pa_cur, woba_cur, basis = _pinch_hitter_ranking_view(
            p, pinch_hitter_pa_threshold,
        )
        # Tag the record (used by the renderer for the PA / wOBA / Basis cols).
        p["_pinch_basis"] = basis
        p["_pinch_pa_current"] = pa_cur
        p["_pinch_woba_current"] = woba_cur
        p["_pinch_sort_key"] = sort_key
        annotated.append((sort_key, p))
    annotated.sort(key=lambda kp: -kp[0])
    pinch_hitters = [p for _, p in annotated[:max(0, int(pinch_hitter_count))]]

    # Pinch runners — exclude zero-baserunning players.
    runners = [p for p in non_starters if (p.get("baserunning_score") or 0.0) > 0]
    runners.sort(key=lambda p: -p["baserunning_score"])
    pinch_runners = runners[:max(0, int(pinch_runner_count))]

    return pinch_hitters, pinch_runners


def assign_pitchers(
    pool: List[Dict[str, Any]],
    level_cfg: Dict[str, Any],
) -> Dict[str, List[Dict[str, Any]]]:
    """
    Slot pitchers by role. Respect Projected_Position (SP vs RP) — SPs become
    rotation, RPs become bullpen — then rank within each bucket by composite to
    fill SP1-N, CL, SU, MR, LR.
    """
    role_count = level_cfg.get("pitcher_role_count", {})
    sp_n = int(role_count.get("SP", 5))
    cl_n = int(role_count.get("CL", 1))
    su_n = int(role_count.get("SU", 2))
    mr_n = int(role_count.get("MR", 4))
    lr_n = int(role_count.get("LR", 1))

    sp_pool = [p for p in pool if p["proj_role"] == "SP"]
    rp_pool = [p for p in pool if p["proj_role"] == "RP"]
    sp_pool.sort(key=lambda p: -p["composite"])
    rp_pool.sort(key=lambda p: -p["composite"])

    out: Dict[str, List[Dict[str, Any]]] = {"SP": [], "CL": [], "SU": [], "MR": [], "LR": []}

    out["SP"] = sp_pool[:sp_n]
    leftover_sp = sp_pool[sp_n:]  # excess SPs slide to LR/MR if pen short

    rp_iter = list(rp_pool)
    out["CL"] = rp_iter[:cl_n]
    rp_iter = rp_iter[cl_n:]
    out["SU"] = rp_iter[:su_n]
    rp_iter = rp_iter[su_n:]
    out["MR"] = rp_iter[:mr_n]
    rp_iter = rp_iter[mr_n:]
    out["LR"] = rp_iter[:lr_n] or leftover_sp[:lr_n]

    return out


# -----------------------------------------------------------------------------
# Lineup construction
# -----------------------------------------------------------------------------

def _split_score(player: Dict[str, Any], split: str) -> float:
    """Hitter composite using vs-L or vs-R z-scores (falls back to overall)."""
    sk = f"split_score_{split}"
    return float(player.get(sk, player.get("composite", 50.0)))


def _make_lineup_gap(position: str) -> Dict[str, Any]:
    """Placeholder entry inserted into a lineup when a defensive position has
    no starter. Renderers should detect ``_lineup_gap`` and emit em-dashes
    for hitter stats. The minor-league manager fills these in-game; the
    purpose is just to make the gap explicit at the lineup level so the user
    sees "yes, I need someone at SS" instead of an 8-slot lineup that hides
    the issue."""
    return {
        "pid": f"_GAP_{position}",
        "name": "—",
        "primary_pos": position,
        "_assigned_pos": position,
        "_lineup_gap": True,
        "vos": 0.0,
        "composite": 0.0,
        "hitter_bundle": {},
        "is_pitcher": False,
    }


def build_lineup(
    starters: List[Dict[str, Any]],
    split: str,
    missing_positions: Optional[List[str]] = None,
    target_slots: int = 9,
) -> List[Tuple[int, Dict[str, Any]]]:
    """
    Sabermetric batting order per Tom Tango's *The Book* (pp. 130-137).

    - Your 3 best hitters bat at slots 1, 2, and 4 (not 3-4-5 — the 3-hole is
      overrated; it bats with two outs and runners on more than people think).
      Within the top 3:
        * highest SLG -> 4 (cleanup gets the most PAs with runners on)
        * highest OBP among the remaining two -> 1 (leadoff: get on base)
        * the third top-3 hitter -> 2
      If one player has both highest OBP and highest SLG, slot them to 4 since
      cleanup carries more leverage; the next-best OBP among the top 3 leads off.
    - 4th-best hitter -> 5
    - 5th-best hitter -> 3
    - 6th through 9th best -> slots 6, 7, 8, 9 in descending order.

    "Best hitter" is ranked by the split composite (vs-L or vs-R z-score blend).
    OBP/SLG for the top-3 distribution come from the same split.

    If ``missing_positions`` is provided, gap entries are appended after the
    real hitters so the output always reaches ``target_slots`` total. Gaps
    fall to the bottom of the order naturally — they have no hitting value
    so Tango's algorithm would never lift them. The user can shuffle them
    in-game; the purpose here is just to make the empty defensive slot
    explicit on the report.
    """
    missing_positions = list(missing_positions or [])
    if not starters and not missing_positions:
        return []
    pool = sorted(starters, key=lambda p: -_split_score(p, split))

    def split_stat(p: Dict[str, Any], stat: str) -> float:
        bundle = p.get("hitter_bundle") or {}
        s = bundle.get(split) or bundle.get("overall") or {}
        return float(s.get(stat, 0.0))

    one = two = three = four = five = None
    top3 = pool[:3]

    if len(top3) >= 3:
        top3_by_slg = sorted(top3, key=lambda p: -split_stat(p, "SLG"))
        four = top3_by_slg[0]
        # Among the remaining top-3 hitters, the one with the higher OBP leads off.
        rest_top3 = [p for p in top3 if p["pid"] != four["pid"]]
        rest_top3_by_obp = sorted(rest_top3, key=lambda p: -split_stat(p, "OBP"))
        one = rest_top3_by_obp[0] if rest_top3_by_obp else None
        two = rest_top3_by_obp[1] if len(rest_top3_by_obp) > 1 else None
    elif len(top3) == 2:
        # Two-hitter pool: best SLG cleans up, the other leads off.
        a, b = top3
        if split_stat(a, "SLG") >= split_stat(b, "SLG"):
            four, one = a, b
        else:
            four, one = b, a
    elif len(top3) == 1:
        one = top3[0]

    rest = pool[3:]
    five = rest[0] if len(rest) > 0 else None         # 4th best -> slot 5
    three = rest[1] if len(rest) > 1 else None        # 5th best -> slot 3
    six_through_nine = rest[2:6]                       # 6th-9th best -> 6-9

    order = [one, two, three, four, five] + six_through_nine
    filled = [(i + 1, p) for i, p in enumerate(order) if p]

    # Pad to ``target_slots`` with explicit position gaps. Without this, a
    # depth chart that's missing (say) a SS produces an 8-slot lineup that
    # hides the issue from the user. With this, the lineup ALWAYS shows 9
    # rows, with the missing position visible as an em-dash row at the
    # bottom of the order.
    if missing_positions and len(filled) < target_slots:
        next_slot = len(filled) + 1
        for miss_pos in missing_positions:
            if next_slot > target_slots:
                break
            filled.append((next_slot, _make_lineup_gap(miss_pos)))
            next_slot += 1

    return filled


# -----------------------------------------------------------------------------
# Promotion / replacement / demotion logic
# -----------------------------------------------------------------------------

def find_promotion_candidates(
    above_pool: List[Dict[str, Any]],
    below_pool: List[Dict[str, Any]],
    threshold: float,
    starters_by_pos: Optional[Dict[str, Optional[Dict[str, Any]]]] = None,
) -> List[Tuple[Dict[str, Any], Optional[Dict[str, Any]], Optional[Dict[str, Any]]]]:
    """For each player in below_pool whose composite beats either the starter at
    their position OR the weakest comparable above-level player by ``threshold``,
    flag as promotion candidate. Returns tuples of (cand, weakest, starter).

    For pitchers, ``starter`` is None (rotation has 5 equally-starting SPs; the
    "weakest" comparison is the more meaningful one for staff inclusion).
    Hitters use both: vs starter (replace the lineup spot) and vs weakest
    (just make the roster).
    """
    if not below_pool:
        return []
    above_sorted = sorted(above_pool, key=lambda p: p["composite"])
    flagged: List[Tuple[Dict[str, Any], Optional[Dict[str, Any]], Optional[Dict[str, Any]]]] = []
    for cand in sorted(below_pool, key=lambda p: -p["composite"]):
        weakest = _weakest_comparable(cand, above_sorted)
        starter = None
        if not cand.get("is_pitcher") and starters_by_pos:
            primary = (cand.get("primary_pos") or "").upper()
            starter = starters_by_pos.get(primary)
            # If the "starter" turns out to be the same player as the weakest
            # comparable, treat it as a single comparison (no separate starter row).
            if starter and weakest and starter["pid"] == weakest["pid"]:
                starter = None

        edges: List[float] = []
        if weakest:
            edges.append(cand["composite"] - weakest["composite"])
        if starter:
            edges.append(cand["composite"] - starter["composite"])
        if not edges or max(edges) < threshold:
            continue
        flagged.append((cand, weakest, starter))

    # Sort: hitters get prioritized by best edge (starter or weakest, whichever bigger);
    # pitchers fall back to vs-weakest. Ties broken by candidate composite.
    def _sort_key(entry):
        cand, weakest, starter = entry
        edges = []
        if weakest:
            edges.append(cand["composite"] - weakest["composite"])
        if starter:
            edges.append(cand["composite"] - starter["composite"])
        best_edge = max(edges) if edges else 0.0
        return (-best_edge, -cand["composite"])

    flagged.sort(key=_sort_key)
    return flagged


def _weakest_comparable(cand: Dict[str, Any], above_sorted: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """
    Return the lowest-composite above-level player who plays the same kind of
    role: pitchers compare to pitchers (and SP/RP buckets are matched), hitters
    compare to hitters at the same primary position when possible, else any
    hitter.
    """
    if cand["is_pitcher"]:
        pool = [p for p in above_sorted if p["is_pitcher"] and p.get("proj_role") == cand.get("proj_role")]
        if not pool:
            pool = [p for p in above_sorted if p["is_pitcher"]]
    else:
        primary = cand.get("primary_pos") or ""
        pool = [p for p in above_sorted if not p["is_pitcher"] and p.get("primary_pos") == primary]
        if not pool:
            pool = [p for p in above_sorted if not p["is_pitcher"]]
    return pool[0] if pool else None


def find_demotion_candidates(
    pool: List[Dict[str, Any]],
    threshold_z: float,
    floors: Dict[str, float],
    promotion_cfg: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """Underperformers: composite + meaningful sample below threshold."""
    if not pool:
        return []
    composites = [p["composite"] for p in pool]
    mean_comp, std_comp = _mean_std(composites)
    if std_comp <= 0:
        return []
    min_pa = float(promotion_cfg.get("min_pa_for_demote_call", 80))
    min_outs = float(promotion_cfg.get("min_outs_for_demote_call", 60))
    out: List[Dict[str, Any]] = []
    for p in pool:
        z = _z(p["composite"], mean_comp, std_comp)
        if z > threshold_z:
            continue
        if p["is_pitcher"]:
            outs = float(((p.get("pitcher_bundle") or {}).get("overall") or {}).get("IP", 0.0)) * 3.0
            if outs < min_outs:
                continue
        else:
            pa = float(((p.get("hitter_bundle") or {}).get("overall") or {}).get("PA", 0.0))
            if pa < min_pa:
                continue
        out.append({**p, "_demote_z": z})
    return sorted(out, key=lambda p: p["_demote_z"])


# -----------------------------------------------------------------------------
# Output rendering
# -----------------------------------------------------------------------------

def _fmt_score(x: float) -> str:
    return f"{x:.1f}" if isinstance(x, (int, float)) else str(x)


def _player_md_row(p: Dict[str, Any], extra_cols: List[Tuple[str, str]] = None) -> str:
    extras = []
    for label, key in (extra_cols or []):
        extras.append(str(p.get(key, "")))
    cells = [
        p["name"], p.get("primary_pos", ""), str(p.get("age", "")),
        f"{p['vos']:.1f}", f"{p.get('stat_score', 50.0):.1f}",
        f"{p['composite']:.1f}",
    ] + extras
    return "| " + " | ".join(cells) + " |"


def _name_with_flags(p: Optional[Dict[str, Any]]) -> str:
    """Render a player name with any /players status flags appended (e.g. ' (DL)')."""
    if not p:
        return ""
    flags = (p.get("status_flags") or "").strip()
    return f"{p['name']} ({flags})" if flags else p["name"]


def render_md(
    league: str, org: str, level: str, year: int,
    starters: Dict[str, List[Dict[str, Any]]],
    bench: List[Dict[str, Any]],
    lineup_l: List[Tuple[int, Dict[str, Any]]],
    lineup_r: List[Tuple[int, Dict[str, Any]]],
    pitcher_slots: Dict[str, List[Dict[str, Any]]],
    promotions: List[Tuple[Dict[str, Any], Optional[Dict[str, Any]], Optional[Dict[str, Any]]]],
    replacements: List[Dict[str, Any]],
    demotions: List[Dict[str, Any]],
    pitcher_mismatches: List[Dict[str, Any]],
    hitter_pool: Optional[List[Dict[str, Any]]] = None,
    depth_table: Optional[Dict[str, Dict[str, Optional[Dict[str, Any]]]]] = None,
    pinch_hitters: Optional[List[Dict[str, Any]]] = None,
    pinch_runners: Optional[List[Dict[str, Any]]] = None,
    off_chart_hitters: Optional[List[Dict[str, Any]]] = None,
    off_chart_pitchers: Optional[List[Dict[str, Any]]] = None,
    starter_gaps: Optional[List[Dict[str, Any]]] = None,
    min_comp_global: Optional[float] = None,
    min_comp_per_pos: Optional[Dict[str, float]] = None,
) -> str:
    out: List[str] = []
    out.append(f"# Depth Chart — {org} ({level})  ·  {league.upper()}  ·  {year}")
    out.append("")
    out.append("_Composite blends VOS rating with z-scored stats per `depth_config.json` weights._")
    out.append("")

    # Starter Gaps — positions where the best available didn't clear the
    # min-comp bar. Drives the FA-shopping workflow: scan this section to see
    # exactly where the roster needs a signing to push into contention.
    if starter_gaps:
        out.append("## Starter Gaps")
        out.append("")
        thr_note = ""
        if min_comp_global is not None and min_comp_per_pos:
            thr_note = (
                f"Global threshold {min_comp_global:g}; per-position overrides apply where set."
            )
        elif min_comp_global is not None:
            thr_note = f"Threshold {min_comp_global:g} (composite, 20-80 scale)."
        elif min_comp_per_pos:
            overrides = ", ".join(f"{k}:{v:g}" for k, v in sorted(min_comp_per_pos.items()))
            thr_note = f"Per-position thresholds: {overrides}."
        if thr_note:
            out.append(f"_{thr_note} Below-threshold players drop to Util 1; the starter slot stays empty._")
            out.append("")
        out.append("| Pos | Would-be Starter | Composite | Threshold | Gap |")
        out.append("| --- | --- | --- | --- | --- |")
        for g in starter_gaps:
            wb = g["would_be"]
            comp = float(wb.get("composite", 0.0))
            out.append(
                f"| {g['pos']} | {_name_with_flags(wb)} | "
                f"{comp:.1f} | {g['threshold']:g} | -{g['gap']:.1f} |"
            )
        out.append("")

    # Position depth chart — OOTP-style. Players can fill slots at multiple
    # positions; status flags (DL, DFA, etc.) appended in parens to names.
    if depth_table:
        # Determine util column count from any row. Filter to keys that match
        # util<N> exactly — `schedules` lives alongside util1/util2 but is a
        # str dict, not a player record, so it must not be iterated here.
        sample = next((v for v in depth_table.values() if v), {})
        util_keys = sorted(
            [k for k in sample.keys() if k.startswith("util") and k[4:].isdigit()],
            key=lambda k: int(k[4:]),
        )

        out.append("## Position Depth")
        out.append("")
        out.append("_Starter is the best player at the position (empty when no one clears "
                   "--min-comp). Util 1/2 are the next-best at the position; suggested play "
                   "schedule appears in parens. Def Sub is the best defender available (n/a for DH)._")
        out.append("")

        header_cols = ["Pos", "Starter"] + [f"Util {i + 1}" for i in range(len(util_keys))] + ["Def Sub"]
        out.append("| " + " | ".join(header_cols) + " |")
        out.append("| " + " | ".join(["---"] * len(header_cols)) + " |")
        for pos in HITTER_POSITIONS:
            row = depth_table.get(pos) or {}
            schedules = row.get("schedules") or {}
            cells = [pos, _name_with_flags(row.get("starter"))]
            for k in util_keys:
                util_player = row.get(k)
                name = _name_with_flags(util_player)
                # Append the schedule hint in parens — only when both player
                # and a non-empty schedule exist. Keeps empty cells clean.
                sched = schedules.get(k, "") if isinstance(schedules, dict) else ""
                if name and sched:
                    name = f"{name} ({sched})"
                cells.append(name)
            # DH never gets a def sub — render an em-dash so the column stays
            # aligned but the bug from the screenshot doesn't reappear.
            if pos == "DH":
                cells.append("—")
            else:
                cells.append(_name_with_flags(row.get("def_sub")))
            out.append("| " + " | ".join(cells) + " |")
        out.append("")
    else:
        # Legacy fallback (compatible with older callers that don't pass a depth_table).
        out.append("## Position Depth")
        out.append("")
        out.append("| Pos | 1st | 2nd | 3rd+ |")
        out.append("| --- | --- | --- | --- |")
        for pos in HITTER_POSITIONS:
            slots = starters.get(pos, [])
            first = slots[0]["name"] if len(slots) > 0 else ""
            second = slots[1]["name"] if len(slots) > 1 else ""
            rest = ", ".join(p["name"] for p in slots[2:]) if len(slots) > 2 else ""
            out.append(f"| {pos} | {first} | {second} | {rest} |")
        out.append("")

    # Pinch hitters / runners — OOTP-style side lists.
    if pinch_hitters:
        out.append("## Pinch Hitters")
        out.append("")
        out.append("_Ranked by in-season composite when current-year PA >= threshold "
                   "(``pinch_hitter_pa_threshold``, default 100); below that, by the "
                   "eval's Batting_Score. The Basis column shows which metric drove each row._")
        out.append("")
        out.append("| # | Name | Pos | PA | wOBA | Bat | Stat | Basis | Career |")
        out.append("| --- | --- | --- | --- | --- | --- | --- | --- | --- |")
        for i, p in enumerate(pinch_hitters, start=1):
            pa = float(p.get("_pinch_pa_current", 0) or 0)
            woba = float(p.get("_pinch_woba_current", 0) or 0)
            out.append(
                f"| {i} | {_name_with_flags(p)} | {p.get('primary_pos','')} | "
                f"{pa:.0f} | {woba:.3f} | {p.get('batting_score', 0):.1f} | "
                f"{p.get('stat_score', 50.0):.1f} | {p.get('_pinch_basis','')} | "
                f"{p['vos']:.1f} |"
            )
        out.append("")

    if pinch_runners:
        out.append("## Pinch Runners")
        out.append("")
        out.append("_Top non-starters by Baserunning Score. Use late-game / pinch-run / steal situations._")
        out.append("")
        out.append("| # | Name | Pos | BsR | Career |")
        out.append("| --- | --- | --- | --- | --- |")
        for i, p in enumerate(pinch_runners, start=1):
            out.append(
                f"| {i} | {_name_with_flags(p)} | {p.get('primary_pos','')} | "
                f"{p.get('baserunning_score', 0):.1f} | {p['vos']:.1f} |"
            )
        out.append("")

    if bench:
        out.append("## Bench / Flex")
        out.append("")
        out.append("| Name | Best Pos | Age | Career | Stat | Composite |")
        out.append("| --- | --- | --- | --- | --- | --- |")
        for p in bench:
            out.append(_player_md_row(p))
        out.append("")

    # Lineups
    out.append("## Lineup vs RHP")
    out.append("")
    out.append("| # | Name | Pos | Career | wOBA (vs R) | OBP (vs R) | SLG (vs R) |")
    out.append("| --- | --- | --- | --- | --- | --- | --- |")
    for slot, p in lineup_r:
        if p.get("_lineup_gap"):
            out.append(f"| {slot} | — | {p.get('_assigned_pos', '')} | — | — | — | — |")
            continue
        sb = (p.get("hitter_bundle") or {}).get("vs_r", {})
        out.append(
            f"| {slot} | {p['name']} | {p.get('_assigned_pos', p.get('primary_pos',''))} "
            f"| {p['vos']:.1f} | {sb.get('wOBA', 0):.3f} | {sb.get('OBP', 0):.3f} | {sb.get('SLG', 0):.3f} |"
        )
    out.append("")
    out.append("## Lineup vs LHP")
    out.append("")
    out.append("| # | Name | Pos | Career | wOBA (vs L) | OBP (vs L) | SLG (vs L) |")
    out.append("| --- | --- | --- | --- | --- | --- | --- |")
    for slot, p in lineup_l:
        if p.get("_lineup_gap"):
            out.append(f"| {slot} | — | {p.get('_assigned_pos', '')} | — | — | — | — |")
            continue
        sb = (p.get("hitter_bundle") or {}).get("vs_l", {})
        out.append(
            f"| {slot} | {p['name']} | {p.get('_assigned_pos', p.get('primary_pos',''))} "
            f"| {p['vos']:.1f} | {sb.get('wOBA', 0):.3f} | {sb.get('OBP', 0):.3f} | {sb.get('SLG', 0):.3f} |"
        )
    out.append("")

    # Pitching staff
    out.append("## Pitching Staff")
    out.append("")
    out.append("| Role | Name | Age | Career | Stat | Composite | IP | FIP | K-BB% |")
    out.append("| --- | --- | --- | --- | --- | --- | --- | --- | --- |")
    for i, p in enumerate(pitcher_slots.get("SP", []), start=1):
        b = (p.get("pitcher_bundle") or {}).get("overall", {})
        out.append(
            f"| SP{i} | {p['name']} | {p.get('age','')} | {p['vos']:.1f} | {p.get('stat_score', 50.0):.1f} | "
            f"{p['composite']:.1f} | {b.get('IP', 0):.1f} | {b.get('FIP', 0):.2f} | {b.get('K-BB%', 0)*100:.1f}% |"
        )
    for role in ("CL", "SU", "MR", "LR"):
        for p in pitcher_slots.get(role, []):
            b = (p.get("pitcher_bundle") or {}).get("overall", {})
            out.append(
                f"| {role} | {p['name']} | {p.get('age','')} | {p['vos']:.1f} | {p.get('stat_score', 50.0):.1f} | "
                f"{p['composite']:.1f} | {b.get('IP', 0):.1f} | {b.get('FIP', 0):.2f} | {b.get('K-BB%', 0)*100:.1f}% |"
            )
    out.append("")

    if pitcher_mismatches:
        out.append("## Pitcher Role Mismatches")
        out.append("")
        out.append("_Players whose stats look more like the opposite role than what their projection says._")
        out.append("")
        out.append("| Name | Projected | Suggested | Reason |")
        out.append("| --- | --- | --- | --- |")
        for m in pitcher_mismatches:
            out.append(f"| {m['name']} | {m['projected']} | {m['suggested']} | {m['reason']} |")
        out.append("")

    # Off the depth chart — players in the level eval pool who didn't get
    # slotted anywhere. Narrow cut-watch for DFA decisions. Sorted worst-first
    # by composite so the most obvious cuts are at the top.
    if off_chart_hitters:
        out.append("## Off the Depth Chart — Hitters (Cut Watch)")
        out.append("")
        out.append("_Hitters in the level pool who didn't make the depth chart. Sorted worst composite first. Cross-reference age and Reach before cutting — a young player with a high Reach is one the v10 reach model still expects to make the majors, so a low present-day composite alone isn't reason enough to drop them._")
        out.append("")
        out.append("| Name | Age | Pos | Career | Reach | Stat | Composite | PA | wOBA |")
        out.append("| --- | --- | --- | --- | --- | --- | --- | --- | --- |")
        for p in sorted(off_chart_hitters, key=lambda r: float(r.get("composite", 0))):
            hb = (p.get("hitter_bundle") or {}).get("overall") or {}
            pa = float(hb.get("PA", 0)) if hb else 0
            woba = float(hb.get("wOBA", 0)) if hb else 0
            out.append(
                f"| {p['name']} | {p.get('age','')} | {p.get('primary_pos','')} | "
                f"{p['vos']:.1f} | {p.get('vos_potential', 0.0):.1f} | "
                f"{p.get('stat_score', 50.0):.1f} | {p['composite']:.1f} | "
                f"{pa:.0f} | {woba:.3f} |"
            )
        out.append("")

    if off_chart_pitchers:
        out.append("## Off the Depth Chart — Pitchers (Cut Watch)")
        out.append("")
        out.append("_Pitchers in the level pool who didn't make the staff. Sorted worst composite first._")
        out.append("")
        out.append("| Name | Age | Role | Career | Reach | Stat | Composite | IP | FIP |")
        out.append("| --- | --- | --- | --- | --- | --- | --- | --- | --- |")
        for p in sorted(off_chart_pitchers, key=lambda r: float(r.get("composite", 0))):
            pb = (p.get("pitcher_bundle") or {}).get("overall") or {}
            ip = float(pb.get("IP", 0)) if pb else 0
            fip = float(pb.get("FIP", 0)) if pb else 0
            out.append(
                f"| {p['name']} | {p.get('age','')} | {p.get('proj_role','')} | "
                f"{p['vos']:.1f} | {p.get('vos_potential', 0.0):.1f} | "
                f"{p.get('stat_score', 50.0):.1f} | {p['composite']:.1f} | "
                f"{ip:.1f} | {fip:.2f} |"
            )
        out.append("")

    # All-hitter composite ranking (mirrors the pitching staff table for hitters).
    if hitter_pool:
        out.append("## Hitter Composites (level pool)")
        out.append("")
        out.append("_All hitters at this level, ranked by composite. Use this to spot ratings/stats divergence._")
        out.append("")
        out.append("| Name | Age | Pos | Career | Stat | Composite | PA | wOBA | OPS |")
        out.append("| --- | --- | --- | --- | --- | --- | --- | --- | --- |")
        for p in sorted(hitter_pool, key=lambda r: -float(r.get("composite", 0))):
            hb = (p.get("hitter_bundle") or {}).get("overall") or {}
            pa = float(hb.get("PA", 0)) if hb else 0
            woba = float(hb.get("wOBA", 0)) if hb else 0
            ops = float(hb.get("OPS", 0)) if hb else 0
            out.append(
                f"| {p['name']} | {p.get('age','')} | {p.get('primary_pos','')} | "
                f"{p['vos']:.1f} | {p.get('stat_score', 50.0):.1f} | {p['composite']:.1f} | "
                f"{pa:.0f} | {woba:.3f} | {ops:.3f} |"
            )
        out.append("")

    # Promotion candidates — split into hitters (vs starter + vs weakest) and
    # pitchers (vs weakest in role only). Sorted by best-edge in find_promotion_candidates.
    hitter_promos = [(c, w, s) for (c, w, s) in promotions if not c.get("is_pitcher")]
    pitcher_promos = [(c, w, s) for (c, w, s) in promotions if c.get("is_pitcher")]

    if hitter_promos:
        out.append(f"## Hitter Promotion Candidates")
        out.append("")
        out.append("_Sorted by largest possible upgrade. 'vs Starter' = displaces the lineup spot; 'vs Bench' = makes the roster._")
        out.append("")
        out.append("| Cand | Pos | Career | Comp | Starter | Starter Comp | vs Starter | Bench/Worst | Bench Comp | vs Bench |")
        out.append("| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |")
        for cand, weakest, starter in hitter_promos:
            cc = cand["composite"]
            s_name = starter["name"] if starter else "—"
            s_comp = f"{starter['composite']:.1f}" if starter else "—"
            s_edge = f"{cc - starter['composite']:+.1f}" if starter else "—"
            w_name = weakest["name"] if weakest else "—"
            w_comp = f"{weakest['composite']:.1f}" if weakest else "—"
            w_edge = f"{cc - weakest['composite']:+.1f}" if weakest else "—"
            out.append(
                f"| {cand['name']} | {cand.get('primary_pos','')} | {cand['vos']:.1f} | "
                f"{cc:.1f} | {s_name} | {s_comp} | {s_edge} | {w_name} | {w_comp} | {w_edge} |"
            )
        out.append("")

    if pitcher_promos:
        out.append(f"## Pitcher Promotion Candidates")
        out.append("")
        out.append("_'Replaces' = weakest pitcher in the same role bucket (rotation cutoff for SP, lowest RP for relievers)._")
        out.append("")
        out.append("| Cand | Role | Career | Comp | Replaces | Replaces Comp | Edge |")
        out.append("| --- | --- | --- | --- | --- | --- | --- |")
        for cand, weakest, _starter in pitcher_promos:
            cc = cand["composite"]
            w_name = weakest["name"] if weakest else "—"
            w_comp = f"{weakest['composite']:.1f}" if weakest else "—"
            w_edge = f"{cc - weakest['composite']:+.1f}" if weakest else "—"
            out.append(
                f"| {cand['name']} | {cand.get('proj_role','RP')} | {cand['vos']:.1f} | "
                f"{cc:.1f} | {w_name} | {w_comp} | {w_edge} |"
            )
        out.append("")

    if not promotions:
        out.append("## Promotion Candidates")
        out.append("")
        out.append("_None met the threshold._")
        out.append("")

    if replacements:
        out.append("## Replacement Candidates (likely displaced if promotion happens)")
        out.append("")
        out.append("_Doing well at this level but a better option is coming up._")
        out.append("")
        out.append("| Name | Pos | Career | Composite |")
        out.append("| --- | --- | --- | --- |")
        for p in replacements:
            out.append(f"| {p['name']} | {p.get('primary_pos','')} | {p['vos']:.1f} | {p['composite']:.1f} |")
        out.append("")

    if demotions:
        out.append("## Demotion Candidates")
        out.append("")
        out.append("_Underperforming for this level._")
        out.append("")
        out.append("| Name | Pos | Career | Composite | Z |")
        out.append("| --- | --- | --- | --- | --- |")
        for p in demotions:
            out.append(f"| {p['name']} | {p.get('primary_pos','')} | {p['vos']:.1f} | {p['composite']:.1f} | {p['_demote_z']:.2f} |")
        out.append("")

    return "\n".join(out)


# -----------------------------------------------------------------------------
# Player record assembly
# -----------------------------------------------------------------------------

def build_player_record(
    eval_row: Dict[str, str],
    pitchers: Dict[str, Dict[str, Any]],
    hitters: Dict[str, Dict[str, Any]],
    level_cfg: Dict[str, Any],
    floors: Dict[str, float],
    pitcher_means: Dict[str, float], pitcher_stds: Dict[str, float],
    hitter_means: Dict[str, float], hitter_stds: Dict[str, float],
    hitter_means_l: Dict[str, float], hitter_stds_l: Dict[str, float],
    hitter_means_r: Dict[str, float], hitter_stds_r: Dict[str, float],
) -> Dict[str, Any]:
    pid = (eval_row.get("ID") or "").strip()
    name = eval_row.get("Name", "")
    age = eval_row.get("Age", "")
    pitcher = is_pitcher(eval_row)
    primary_pos = (eval_row.get("Projected_Position") or eval_row.get("Pos") or "").strip().upper()

    # Per-position hitter scores for assignment
    pos_scores = {pos: position_score(eval_row, pos) for pos in HITTER_POSITIONS}

    rw = float(level_cfg.get("ratings_weight", 0.5))
    sw = float(level_cfg.get("stats_weight", 0.5))
    vos = to_float(eval_row.get("VOS_Score"), 0.0)

    if pitcher:
        stat_score, sample_w = pitcher_stat_score(pid, pitchers, pitcher_means, pitcher_stds, floors)
    else:
        stat_score, sample_w = hitter_stat_score(pid, hitters, hitter_means, hitter_stds, "overall", floors)

    # Renormalize weights when the sample is small.
    eff_sw = sw * sample_w
    total = rw + eff_sw
    if total > 0:
        rn_rw, rn_sw = rw / total, eff_sw / total
    else:
        rn_rw, rn_sw = 1.0, 0.0
    composite_score = rn_rw * vos + rn_sw * stat_score

    # Blend the per-position rating score with the player's overall stat z-score
    # using the same renormalized weights as the overall composite. Used by the
    # slotting algorithm so position assignments reflect both ratings AND
    # current-year performance, instead of ratings only.
    #
    # Critical: positions where the player is NOT viable (raw pos_score 0 or
    # missing) MUST stay 0 in the blended map — otherwise a hot bat would make
    # every player magically viable at every position.
    pos_scores_blended: Dict[str, float] = {}
    for _pos, _raw in pos_scores.items():
        if _raw and _raw > 0:
            pos_scores_blended[_pos] = rn_rw * _raw + rn_sw * stat_score
        else:
            pos_scores_blended[_pos] = 0.0

    rec: Dict[str, Any] = {
        "pid": pid,
        "name": name,
        "age": age,
        "is_pitcher": pitcher,
        "primary_pos": primary_pos,
        "proj_role": projected_role_for_pitcher(eval_row) if pitcher else "",
        "vos": vos,
        "vos_potential": to_float(eval_row.get("VOS_Potential"), 0.0),
        "batting_score": to_float(eval_row.get("Batting_Score"), 0.0),
        "defense_score": to_float(eval_row.get("Defense_Score"), 0.0),
        "baserunning_score": to_float(eval_row.get("Baserunning_Score"), 0.0),
        "stat_score": stat_score,
        "sample_weight": sample_w,
        "composite": composite_score,
        "pos_scores": pos_scores,
        "pos_scores_blended": pos_scores_blended,
        "pitcher_bundle": pitchers.get(pid) if pitcher else None,
        "hitter_bundle": hitters.get(pid) if not pitcher else None,
        # Status flag(s) from /players override (e.g., "DL", "Secondary"). Empty when none.
        "status_flags": (eval_row.get("_Status_Flags") or "").strip(),
    }

    if not pitcher and pid in hitters:
        # Cache per-split composites for lineup decisions.
        for split, m, s in (("vs_l", hitter_means_l, hitter_stds_l), ("vs_r", hitter_means_r, hitter_stds_r)):
            score, _ = hitter_stat_score(pid, hitters, m, s, split, floors)
            rec[f"split_score_{split}"] = score

    return rec


def detect_pitcher_mismatches(
    pitchers_pool: List[Dict[str, Any]],
    pitcher_slots: Dict[str, List[Dict[str, Any]]],
) -> List[Dict[str, Any]]:
    """Surface pitchers whose stats look out-of-bucket.

    Heuristic:
      - Projected SP with low IP per appearance (IP/G < 3) and high K%/9 — looks like an RP profile.
      - Projected RP with multi-inning track record (G>5 and IP/G > 2) and low K%/9 — looks like an SP profile.
    """
    out: List[Dict[str, Any]] = []
    for p in pitchers_pool:
        b = (p.get("pitcher_bundle") or {}).get("overall") or {}
        ip = float(b.get("IP", 0.0))
        g = float(b.get("G", 0.0))
        gs = float(b.get("GS", 0.0))
        if g < 5 or ip < 20:
            continue
        ip_per_g = ip / g if g else 0.0
        if p["proj_role"] == "SP" and gs > 0 and ip_per_g < 3.0:
            out.append({
                "name": p["name"],
                "projected": "SP",
                "suggested": "RP",
                "reason": f"IP/G {ip_per_g:.1f}, low for SP",
            })
        elif p["proj_role"] == "RP" and ip_per_g >= 2.0 and gs >= 1:
            out.append({
                "name": p["name"],
                "projected": "RP",
                "suggested": "SP",
                "reason": f"IP/G {ip_per_g:.1f} with {int(gs)} starts",
            })
    return out


# -----------------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------------

def _fetch_level_stats(
    level: str,
    args: argparse.Namespace,
    target_year: int,
    below_label: Optional[str],
    year_weights: List[float],
    woba_w: Dict[str, Any],
    multi_level_run: bool = False,
) -> Optional[Dict[str, Any]]:
    """Fetch the league/level-wide stat pool and compute the z-score means/stds.

    This is *organization-independent* — the returned bundle depends only on the
    league, level, and year, so it can be built once and reused to score several
    orgs against the same league reference (see ``build_team_pool``'s
    ``shared_stats`` argument, used by playoff_planner.py). Returns None when no
    base URL can be resolved for the league.
    """
    if args.no_stats:
        hitters, pitchers, _fielders = {}, {}, {}
        lg_constants = {"full": {}, "current": {}}
        target_lids: List[int] = []
        lids: List[int] = []
        logger.info("--no-stats set, skipping API fetch")
    else:
        base_url = sapi.resolve_base_url(args.league, args.base_url, args.league_url_config)
        if not base_url:
            logger.error("No base URL for league '%s'", args.league)
            return None

        league_ids_map = load_league_ids(args.league_ids_config)
        # In multi-level mode, force all-levels lid resolution so cache hits
        # for every subsequent level after the first.
        all_levels_flag = args.all_levels or multi_level_run
        lids = resolve_lids(args.league, level, below_label, league_ids_map, all_levels_flag, args.lids)
        if not lids:
            logger.warning(
                "No lids resolved for league=%s level=%s. API will default to top-level (ML only) — "
                "non-ML players will have no stats. Add the league to %s or pass --lids.",
                args.league, level, args.league_ids_config,
            )
        else:
            logger.info("Fetching stats for lids: %s", ", ".join(str(x) for x in lids))

        # Target lids = just the level being projected, so league averages
        # don't get diluted by the level-below promotion pool.
        target_lids = league_ids_map.get(args.league.lower(), {}).get(level.upper(), []) or lids

        # Disk cache for stat fetches (calendar-day TTL). One per league.
        cache_dir: Optional[Path] = None
        if not args.no_cache:
            cache_dir = args.cache_dir or (SCRIPT_DIR / args.league / "cache" / "stats")
            logger.info("Stats cache: %s", cache_dir)

        hitters, pitchers, _fielders, lg_constants = sapi.build_player_stats(
            base_url, target_year, year_weights, woba_w,
            lids=lids or None, target_lids=target_lids or None,
            cache_dir=cache_dir,
        )

    # League-level z-score reference (computed across the entire pool, not just the org).
    h_means, h_stds = compute_means_stds(hitters, HITTER_COMPONENTS, "overall") if hitters else ({}, {})
    h_means_l, h_stds_l = compute_means_stds(hitters, HITTER_COMPONENTS, "vs_l") if hitters else ({}, {})
    h_means_r, h_stds_r = compute_means_stds(hitters, HITTER_COMPONENTS, "vs_r") if hitters else ({}, {})
    p_means, p_stds = compute_means_stds(pitchers, PITCHER_COMPONENTS, "overall") if pitchers else ({}, {})

    return {
        "hitters": hitters, "pitchers": pitchers,
        "lg_constants": lg_constants, "target_lids": target_lids, "lids": lids,
        "h_means": h_means, "h_stds": h_stds,
        "h_means_l": h_means_l, "h_stds_l": h_stds_l,
        "h_means_r": h_means_r, "h_stds_r": h_stds_r,
        "p_means": p_means, "p_stds": p_stds,
    }


class CompositeContext:
    """Standalone depth-chart composite calculator.

    Wraps the organization-independent half of the depth-chart pipeline so other
    scripts can score a player's composite the same way ``run_depth_chart`` does,
    without building a whole org's depth chart. On construction it fetches the
    league/level-wide stat pool once and computes the z-score means/stds; after
    that, ``score``/``composite`` are cheap per-player calls against that shared
    reference.

    Because it reuses ``_fetch_level_stats`` + ``build_player_record``, a value
    from here matches the depth chart CSV for the same league/level/year (subject
    to the same stats-cache day). Stats are disk-cached under
    ``{league}/cache/stats`` with a calendar-day TTL, so a context built right
    after a depth-chart run is a cache hit (no network).

    Example (judging a trade target from player_card)::

        ctx = depth_chart.CompositeContext(league="tlg", level="ML", year=2061)
        comp = ctx.composite(eval_row)   # float on the 20-80 scale, or None

    ``eval_row`` must be a row from an ``evaluation_summary`` CSV — it needs at
    least ``ID``, ``VOS_Score``, and the per-position ``{pos}_Score`` columns.
    Raises ``ValueError`` for an unknown level and ``RuntimeError`` when no API
    base URL can be resolved (offline with a cold cache and no ``base_url``)."""

    def __init__(
        self,
        league: str,
        level: str = "ML",
        year: Optional[int] = None,
        *,
        config_path: Path = DEFAULT_CONFIG,
        base_url: Optional[str] = None,
        league_url_config: Path = DEFAULT_LEAGUE_URL,
        league_ids_config: Path = DEFAULT_LEAGUE_IDS,
        cache_dir: Optional[Path] = None,
        no_cache: bool = False,
        no_stats: bool = False,
        lids: Optional[str] = None,
    ) -> None:
        cfg = load_config(config_path)
        levels = cfg.get("levels", {})
        if level not in levels:
            raise ValueError(
                f"Level {level!r} not in {config_path.name}. "
                f"Available: {', '.join(levels) or '(none)'}"
            )
        self.league = league
        self.level = level
        self.year = year if year is not None else (
            league_default_year(league) or datetime.now().year
        )
        self.level_cfg = levels[level]
        self.floors = cfg.get("stat_floors", {})
        woba_w = cfg.get("woba_weights", {})
        year_weights = cfg.get("year_weights", [0.55, 0.35, 0.10])
        below_label = self.level_cfg.get("level_below")

        # _fetch_level_stats reads its inputs off an argparse.Namespace (it's the
        # CLI's stat-fetch helper). Build a minimal one rather than refactoring
        # its signature, so the CLI path stays byte-for-byte unchanged.
        ns = argparse.Namespace(
            league=league,
            base_url=base_url,
            league_url_config=league_url_config,
            league_ids_config=league_ids_config,
            all_levels=False,
            lids=lids,
            no_cache=no_cache,
            no_stats=no_stats,
            cache_dir=cache_dir,
        )
        stats = _fetch_level_stats(level, ns, self.year, below_label, year_weights, woba_w)
        if stats is None:
            raise RuntimeError(
                f"Could not build a stat pool for league={league!r} level={level!r}: "
                f"no API base URL resolved. Pass base_url=… or add the league to "
                f"{league_url_config.name}."
            )
        self.stats = stats

    def score(self, eval_row: Dict[str, str]) -> Dict[str, Any]:
        """Full depth-chart record for one player (keys include ``composite``,
        ``stat_score``, ``sample_weight``, ``vos``, position scores, …)."""
        s = self.stats
        return build_player_record(
            eval_row, s["pitchers"], s["hitters"], self.level_cfg, self.floors,
            s["p_means"], s["p_stds"], s["h_means"], s["h_stds"],
            s["h_means_l"], s["h_stds_l"], s["h_means_r"], s["h_stds_r"],
        )

    def composite(self, eval_row: Dict[str, str]) -> Optional[float]:
        """The composite score (20-80 scale) for one player, or None if it can't
        be coerced to a float."""
        try:
            return float(self.score(eval_row).get("composite"))
        except (TypeError, ValueError):
            return None


def build_team_pool(
    level: str,
    args: argparse.Namespace,
    cfg: Dict[str, Any],
    eval_rows: List[Dict[str, str]],
    target_year: int,
    multi_level_run: bool = False,
    affiliate: Optional[str] = None,
    base_level: Optional[str] = None,
    shared_stats: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """Resolve the level config, fetch (or reuse) the league stat pool, and build
    scored player records for ``args.org`` at ``level``.

    The stat fetch + league z-score reference is organization-independent, so a
    caller can pass ``shared_stats`` (the ``"stats"`` value returned by a previous
    call) to score a second org against the *same* league means/stds without
    re-fetching — this is what lets playoff_planner.py put two clubs on one common
    scale. Returns None when the level is missing from config or no base URL
    resolves.
    """
    config_key = base_level or level
    if config_key not in cfg["levels"]:
        logger.error("Level '%s' not in depth_config.json. Available: %s", config_key, ", ".join(cfg["levels"].keys()))
        return None
    level_cfg = cfg["levels"][config_key]
    floors = cfg.get("stat_floors", {})
    woba_w = cfg.get("woba_weights", {})
    year_weights = cfg.get("year_weights", [0.55, 0.35, 0.10])
    below_label = level_cfg.get("level_below")
    affiliate_pattern_cfg = level_cfg.get("affiliate_pattern")

    stats = shared_stats
    if stats is None:
        stats = _fetch_level_stats(
            level, args, target_year, below_label, year_weights, woba_w,
            multi_level_run=multi_level_run,
        )
        if stats is None:
            return None

    # Build records for level + level_below pools.
    # When ``affiliate`` is set, narrow the level pool to that team affiliate
    # (e.g. only 'Arizona (ACL)' rows). The level_below pool is NOT affiliate-
    # filtered — promotion candidates can come from any affiliate one level down.
    level_pool_eval = org_pool(
        eval_rows, args.org, config_key,
        affiliate=affiliate, affiliate_pattern=affiliate_pattern_cfg,
    )
    below_pool_eval = org_pool(eval_rows, args.org, below_label) if below_label else []

    def build_records(rows: List[Dict[str, str]]) -> List[Dict[str, Any]]:
        return [
            build_player_record(
                r, stats["pitchers"], stats["hitters"], level_cfg, floors,
                stats["p_means"], stats["p_stds"], stats["h_means"], stats["h_stds"],
                stats["h_means_l"], stats["h_stds_l"], stats["h_means_r"], stats["h_stds_r"],
            )
            for r in rows
        ]

    level_records = build_records(level_pool_eval)
    below_records = build_records(below_pool_eval)

    # Split records into hitter / pitcher pools for assignment.
    hitter_pool = [r for r in level_records if not r["is_pitcher"]]
    pitcher_pool = [r for r in level_records if r["is_pitcher"]]

    # Stat-join sanity check. If stats were fetched but ZERO of this org's
    # hitters matched a stat bundle, the stats window is almost certainly the
    # wrong in-game season (--year) for the league, or the eval/stats IDs don't
    # share an id space. Either way the composite silently degrades to ratings-
    # only (empty wOBA/FIP). A zero hit-rate is unambiguous, so warn loudly
    # rather than let the degradation pass unnoticed.
    if stats.get("hitters") and hitter_pool and not any(r.get("hitter_bundle") for r in hitter_pool):
        logger.warning(
            "Stats fetched but 0/%d hitters joined for %s at %s — likely the wrong --year "
            "for this league's in-game season, or an eval/stats ID-space mismatch. "
            "Composite will fall back to ratings-only (no wOBA/FIP).",
            len(hitter_pool), args.org, config_key,
        )

    return {
        "config_key": config_key,
        "level_cfg": level_cfg,
        "floors": floors,
        "below_label": below_label,
        "stats": stats,
        "level_records": level_records,
        "below_records": below_records,
        "hitter_pool": hitter_pool,
        "pitcher_pool": pitcher_pool,
    }


def run_one_level(
    level: str,
    args: argparse.Namespace,
    cfg: Dict[str, Any],
    eval_rows: List[Dict[str, str]],
    target_year: int,
    ts: str,
    multi_level_run: bool = False,
    affiliate: Optional[str] = None,
    base_level: Optional[str] = None,
    eval_path: Optional[Path] = None,
) -> Tuple[int, Optional[Dict[str, Any]]]:
    """Build, render, and write the depth chart for one level.

    Returns (return_code, level_data). ``level_data`` is None on error;
    otherwise a dict of structured artifacts (lineups, pitcher slots,
    placements, promotions, etc.) used by the org-wide summary writer.

    When called from a multi-level batch (``multi_level_run=True``), the stats
    fetch automatically uses --all-levels semantics so the cache is warm for
    every subsequent level after the first.
    """
    # When ``base_level`` is provided, the visible ``level`` may be a synthetic
    # affiliate-suffixed label (e.g., 'R-ACL') that doesn't exist in the config.
    # The base_level is the canonical config key (e.g., 'R'). The stats fetch,
    # league z-score reference, and per-org record build all live in
    # ``build_team_pool`` so playoff_planner.py can reuse the exact same pipeline.
    pool = build_team_pool(
        level, args, cfg, eval_rows, target_year,
        multi_level_run=multi_level_run, affiliate=affiliate, base_level=base_level,
    )
    if pool is None:
        return 2, None

    config_key = pool["config_key"]
    level_cfg = pool["level_cfg"]
    floors = pool["floors"]
    promotion_cfg = cfg.get("promotion", {})
    stats = pool["stats"]
    lg_constants = stats["lg_constants"]
    target_lids = stats["target_lids"]
    lids = stats["lids"]
    level_records = pool["level_records"]
    below_records = pool["below_records"]
    hitter_pool = pool["hitter_pool"]
    pitcher_pool = pool["pitcher_pool"]

    # Position depth — `placed` drives the bench / lineup / PDF code paths.
    placed = assign_positions(hitter_pool, level_cfg)

    # Determine starters (top-of-list at each position) for the lineup.
    # When --min-comp (or --min-comp-pos) is set, the best player at a position
    # only becomes the starter if their composite clears the threshold. Below-
    # threshold players cascade down: they stay in `placed` at index 0, but the
    # starter slot reads as empty so downstream lineups, depth tables, and the
    # FA-shopping "Starter Gaps" section all reflect the hole. Note that we
    # intentionally do NOT mutate `placed` — the player still occupies the
    # position depth-wise; they just lose the starter title.
    global_min = getattr(args, "min_comp", None)
    per_pos_min = getattr(args, "min_comp_pos_map", {}) or {}
    starter_gaps: List[Dict[str, Any]] = []  # {pos, would_be, threshold, gap}
    starters_by_pos: Dict[str, Optional[Dict[str, Any]]] = {}
    for pos in HITTER_POSITIONS:
        top = placed[pos][0] if placed[pos] else None
        threshold = resolve_min_comp(pos, global_min, per_pos_min)
        if top and threshold is not None and float(top.get("composite", 0.0)) < threshold:
            starter_gaps.append({
                "pos": pos,
                "would_be": top,
                "threshold": threshold,
                "gap": threshold - float(top.get("composite", 0.0)),
            })
            starters_by_pos[pos] = None
        else:
            starters_by_pos[pos] = top
    starter_set = [v for v in starters_by_pos.values() if v]

    # OOTP-style depth table — same hitter pool, but allows a player to fill
    # slots at multiple positions (Starter / Util1 / Util2 / Def Sub) and
    # produces side lists for pinch hitters and pinch runners.
    util_count = int(level_cfg.get("util_count_per_pos", 2))
    pinch_h_count = int(level_cfg.get("pinch_hitter_count", 4))
    pinch_r_count = int(level_cfg.get("pinch_runner_count", 3))
    pinch_h_pa_threshold = float(level_cfg.get("pinch_hitter_pa_threshold", 100.0))
    depth_table = build_position_depth_table(hitter_pool, starters_by_pos, util_count=util_count)
    pinch_hitters, pinch_runners = build_pinch_lists(
        hitter_pool, starters_by_pos,
        pinch_hitter_count=pinch_h_count,
        pinch_runner_count=pinch_r_count,
        pinch_hitter_pa_threshold=pinch_h_pa_threshold,
    )

    # Bench = hitters in the placed map but not in starter_set.
    starter_pids = {p["pid"] for p in starter_set}
    bench = []
    for slots in placed.values():
        for p in slots[1:]:
            if p["pid"] not in starter_pids:
                bench.append(p)
                starter_pids.add(p["pid"])

    # Pitcher staff
    pitcher_slots = assign_pitchers(pitcher_pool, level_cfg)

    # Lineups — pad to 9 slots even when the depth chart left positions empty.
    # The user wants the lineup to always show 9 entries so missing slots are
    # visible; the in-game manager fills the gaps as they see fit.
    missing_positions = [pos for pos in HITTER_POSITIONS if starters_by_pos.get(pos) is None]
    lineup_l = build_lineup(starter_set, "vs_l", missing_positions=missing_positions)
    lineup_r = build_lineup(starter_set, "vs_r", missing_positions=missing_positions)

    # Mismatches
    mismatches = detect_pitcher_mismatches(pitcher_pool, pitcher_slots)

    # Promotion / replacement / demotion
    threshold = float(promotion_cfg.get("min_advantage_for_promote", 2.5))
    promotions = find_promotion_candidates(
        level_records, below_records, threshold,
        starters_by_pos=starters_by_pos,
    )
    # Replacement = whoever the promotion would directly displace.
    # Prefer "starter" if there's a starter-level edge; else the weakest comp.
    replacements_raw: List[Dict[str, Any]] = []
    for cand, weakest, starter in promotions:
        cand_comp = cand["composite"]
        starter_edge = (cand_comp - starter["composite"]) if starter else float("-inf")
        weakest_edge = (cand_comp - weakest["composite"]) if weakest else float("-inf")
        if starter and starter_edge >= weakest_edge:
            replacements_raw.append(starter)
        elif weakest:
            replacements_raw.append(weakest)

    underperform_z = float(promotion_cfg.get("underperform_threshold", -1.5))
    demotions = find_demotion_candidates(level_records, underperform_z, floors, promotion_cfg)
    demote_pids = {p["pid"] for p in demotions}
    # Dedupe + drop demotion overlap.
    seen: set = set()
    replacements: List[Dict[str, Any]] = []
    for p in replacements_raw:
        if p["pid"] in seen or p["pid"] in demote_pids:
            continue
        seen.add(p["pid"])
        replacements.append(p)

    # Output
    out_dir = args.output_dir or (SCRIPT_DIR / args.league / "depth")
    out_dir.mkdir(parents=True, exist_ok=True)
    org_slug = filename_org_slug(args)
    out_md = out_dir / f"{org_slug}_{level}_{ts}.md"
    out_csv = out_dir / f"{org_slug}_{level}_{ts}.csv"

    # Cut-watch: players in the level pool who weren't slotted anywhere on the
    # depth chart (no starter, bench, or util slot for hitters; no role bucket
    # for pitchers). These are the most obvious DFA candidates — quick scan
    # for cleaning up roster bloat.
    slotted_hitter_pids = {p["pid"] for slots in placed.values() for p in slots}
    slotted_pitcher_pids = {
        p["pid"] for role_list in pitcher_slots.values() for p in role_list
    }
    off_chart_hitters = [p for p in hitter_pool if p["pid"] not in slotted_hitter_pids]
    off_chart_pitchers = [p for p in pitcher_pool if p["pid"] not in slotted_pitcher_pids]

    md = render_md(
        args.league, args.org, level, target_year,
        placed, bench, lineup_l, lineup_r, pitcher_slots,
        promotions, replacements, demotions, mismatches,
        hitter_pool=hitter_pool,
        depth_table=depth_table,
        pinch_hitters=pinch_hitters,
        pinch_runners=pinch_runners,
        off_chart_hitters=off_chart_hitters,
        off_chart_pitchers=off_chart_pitchers,
        starter_gaps=starter_gaps,
        min_comp_global=global_min,
        min_comp_per_pos=per_pos_min,
    )
    out_md.write_text(md, encoding="utf-8")
    logger.info("Wrote %s", out_md)

    # Build per-pid lineup slot lookups (for vs-L and vs-R)
    lineup_slot_l_map = {p["pid"]: slot for slot, p in lineup_l}
    lineup_slot_r_map = {p["pid"]: slot for slot, p in lineup_r}

    # Write CSV — enriched with stat columns + lineup slots so project_season.py
    # has everything it needs to run a Pythagorean projection without re-fetching.
    # ``min_comp_threshold`` / ``starter_gap`` carry the --min-comp diagnostics:
    # threshold is per-position, populated for the tier-1 row at each hitter
    # position whenever a threshold applies; starter_gap is the (threshold -
    # composite) delta and is only populated when the row failed to qualify
    # for the starter slot (negative when the player cleared the bar, positive
    # = how far short they fell). Both empty when --min-comp isn't set.
    fields = [
        "pid", "name", "age", "primary_pos", "is_pitcher", "proj_role",
        "vos", "vos_potential", "defense_score",
        "stat_score", "sample_weight", "composite", "tier",
        "min_comp_threshold", "starter_gap",
        "PA", "wOBA", "wOBA_current", "OBP", "SLG",
        "wOBA_vs_L", "wOBA_vs_L_current", "wOBA_vs_R", "wOBA_vs_R_current",
        "IP", "FIP", "FIP_current", "K_pct", "BB_pct",
        "lineup_slot_L", "lineup_slot_R",
    ]

    # Per-pid lookup for would-be-starters who failed the threshold.
    gap_by_pid = {g["would_be"]["pid"]: g for g in (starter_gaps or [])}

    def _enrich(
        p: Dict[str, Any],
        tier_label: str,
        min_comp_threshold: Any = "",
        starter_gap: Any = "",
    ) -> Dict[str, Any]:
        hb = (p.get("hitter_bundle") or {})
        # Prefer target-lid-scoped views (computed when build_player_stats was
        # called with target_lids) so a CSV produced for --level ML carries
        # ML-only PA / wOBA / etc. for promoted players. For 3-yr "career"
        # rates, fall back to cross-level when the target view has zero PA
        # (e.g. a brand-new ML callup with no prior ML history). Current-year
        # views always prefer target — zero target-PA is the truth.
        def _hview(label: str) -> Dict[str, Any]:
            tgt = hb.get(f"{label}_target") if hb else None
            if tgt and float(tgt.get("PA", 0.0) or 0.0) > 0:
                return tgt
            return hb.get(label, {}) if hb else {}

        def _hview_current(label: str) -> Dict[str, Any]:
            if hb and f"{label}_current_target" in hb:
                return hb.get(f"{label}_current_target") or {}
            return hb.get(f"{label}_current", {}) if hb else {}

        h_overall = _hview("overall")
        h_l = _hview("vs_l")
        h_r = _hview("vs_r")
        h_overall_cur = _hview_current("overall")
        h_l_cur = _hview_current("vs_l")
        h_r_cur = _hview_current("vs_r")
        pbb = p.get("pitcher_bundle") or {}
        pb_target = pbb.get("overall_target") if pbb else None
        if pb_target and float(pb_target.get("IP", 0.0) or 0.0) > 0:
            pb = pb_target
        else:
            pb = pbb.get("overall", {}) if pbb else {}
        if pbb and "current_target" in pbb:
            pb_current = pbb.get("current_target") or {}
        else:
            pb_current = pbb.get("current", {}) if pbb else {}
        return {
            "pid": p.get("pid", ""),
            "name": p.get("name", ""),
            "age": p.get("age", ""),
            "primary_pos": p.get("primary_pos", ""),
            "is_pitcher": p.get("is_pitcher", False),
            "proj_role": p.get("proj_role", ""),
            "vos": p.get("vos", 0.0),
            "vos_potential": p.get("vos_potential", 0.0),
            "defense_score": p.get("defense_score", ""),
            "stat_score": p.get("stat_score", 0.0),
            "sample_weight": p.get("sample_weight", 0.0),
            "composite": p.get("composite", 0.0),
            "tier": tier_label,
            "min_comp_threshold": min_comp_threshold,
            "starter_gap": starter_gap,
            "PA": h_overall.get("PA", "") if h_overall else "",
            "wOBA": h_overall.get("wOBA", "") if h_overall else "",
            "wOBA_current": h_overall_cur.get("wOBA", "") if h_overall_cur else "",
            "OBP": h_overall.get("OBP", "") if h_overall else "",
            "SLG": h_overall.get("SLG", "") if h_overall else "",
            "wOBA_vs_L": h_l.get("wOBA", "") if h_l else "",
            "wOBA_vs_L_current": h_l_cur.get("wOBA", "") if h_l_cur else "",
            "wOBA_vs_R": h_r.get("wOBA", "") if h_r else "",
            "wOBA_vs_R_current": h_r_cur.get("wOBA", "") if h_r_cur else "",
            "IP": pb.get("IP", "") if pb else "",
            "FIP": pb.get("FIP", "") if pb else "",
            "FIP_current": pb_current.get("FIP", "") if pb_current else "",
            "K_pct": pb.get("K%", "") if pb else "",
            "BB_pct": pb.get("BB%", "") if pb else "",
            "lineup_slot_L": lineup_slot_l_map.get(p["pid"], ""),
            "lineup_slot_R": lineup_slot_r_map.get(p["pid"], ""),
        }

    csv_rows: List[Dict[str, Any]] = []
    for pos, slots in placed.items():
        # Per-position threshold used for the gate (None when not set).
        pos_threshold = resolve_min_comp(pos, global_min, per_pos_min)
        for tier, p in enumerate(slots, start=1):
            mct: Any = ""
            sg: Any = ""
            # Only the tier-1 player at each hitter position is subject to the
            # starter gate; lower-tier rows leave both columns empty.
            # Sign convention: starter_gap = composite - threshold. Positive
            # means the player cleared the bar by that margin; negative means
            # they fell short (and the starter slot was left empty in MD).
            if tier == 1 and pos_threshold is not None:
                mct = pos_threshold
                sg = float(p.get("composite", 0.0)) - float(pos_threshold)
            csv_rows.append(_enrich(p, f"{pos}-{tier}", mct, sg))
    for role, slots in pitcher_slots.items():
        for tier, p in enumerate(slots, start=1):
            tag = f"{role}{tier}" if role == "SP" else f"{role}-{tier}"
            csv_rows.append(_enrich(p, tag))

    with out_csv.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for r in csv_rows:
            r2 = {k: (f"{r[k]:.4f}" if isinstance(r[k], float) else r[k]) for k in fields}
            writer.writerow(r2)
    logger.info("Wrote %s", out_csv)

    # League constants sidecar — used by project_season.py for runs conversion.
    # Now scoped to target_lids and includes both 3-yr-weighted ("full") and
    # current-year-only ("current") views for the projection blend.
    constants_payload = {
        "level": level,
        "year": target_year,
        "target_lids": target_lids,
        "fetched_lids": lids,
        "wOBA_scale": 1.20,
        "full": lg_constants.get("full", {}),
        "current": lg_constants.get("current", {}),
    }
    out_const = out_dir / f"{org_slug}_{level}_{ts}_constants.json"
    out_const.write_text(json.dumps(constants_payload, indent=2), encoding="utf-8")
    logger.info("Wrote %s", out_const)

    # Per-player rate sidecar — used by project_season.py to project counting
    # stat lines (HR, RBI, SB, W, SV, etc.) for the top hitters and pitchers
    # on the depth chart. Rates are derived from the same 3-yr-weighted
    # bundles + current-year bundles already computed for the depth chart, so
    # there's no extra fetch.
    def _safe_div(num: float, den: float) -> float:
        return float(num) / float(den) if den and float(den) > 0 else 0.0

    def _hitter_rates(p: Dict[str, Any]) -> Dict[str, Any]:
        hb = (p.get("hitter_bundle") or {})
        # Prefer target-lid-scoped 3-yr "career" view when the player has any
        # PA at the target level; otherwise fall back to the cross-level view
        # so brand-new callups still get a non-zero rate baseline. Current-year
        # totals always come from the target view (zero ML PA is the truth).
        ov_target = hb.get("overall_target") or {}
        if float(ov_target.get("PA", 0.0) or 0.0) > 0:
            ov = ov_target
        else:
            ov = hb.get("overall", {}) or {}
        if "overall_current_target" in hb:
            ov_cur = hb.get("overall_current_target") or {}
        else:
            ov_cur = hb.get("overall_current", {}) or {}
        pa = float(ov.get("PA", 0.0) or 0.0)
        pa_cur = float(ov_cur.get("PA", 0.0) or 0.0)
        return {
            "name": p.get("name", ""),
            "PA": pa,
            "PA_current": pa_cur,
            # Rate stats already in the bundle.
            "wOBA": float(ov.get("wOBA", 0.0) or 0.0),
            "wOBA_current": float(ov_cur.get("wOBA", 0.0) or 0.0),
            "AVG": float(ov.get("AVG", 0.0) or 0.0),
            "OBP": float(ov.get("OBP", 0.0) or 0.0),
            "SLG": float(ov.get("SLG", 0.0) or 0.0),
            "OPS": float(ov.get("OPS", 0.0) or 0.0),
            "ISO": float(ov.get("ISO", 0.0) or 0.0),
            "BB%": float(ov.get("BB%", 0.0) or 0.0),
            "K%": float(ov.get("K%", 0.0) or 0.0),
            "SB%": float(ov.get("SB%", 0.0) or 0.0),
            # Per-PA counting rates (computed off 3-yr weighted totals).
            "AB_per_PA": _safe_div(ov.get("AB", 0.0), pa),
            "HR_per_PA": _safe_div(ov.get("HR", 0.0), pa),
            "R_per_PA": _safe_div(ov.get("R", 0.0), pa),
            "RBI_per_PA": _safe_div(ov.get("RBI", 0.0), pa),
            "SB_per_PA": _safe_div(ov.get("SB", 0.0), pa),
            "CS_per_PA": _safe_div(ov.get("CS", 0.0), pa),
            "H_per_PA": _safe_div(ov.get("H", 0.0), pa),
            # Current-year actuals (totals + slash line). Used by
            # project_season.py's --use-current-standings overlay to blend
            # actual stats-to-date with career-rate projections of the rest
            # of the season instead of projecting full-season at career rate.
            "AB_current": float(ov_cur.get("AB", 0.0) or 0.0),
            "H_current": float(ov_cur.get("H", 0.0) or 0.0),
            "HR_current": float(ov_cur.get("HR", 0.0) or 0.0),
            "R_current": float(ov_cur.get("R", 0.0) or 0.0),
            "RBI_current": float(ov_cur.get("RBI", 0.0) or 0.0),
            "SB_current": float(ov_cur.get("SB", 0.0) or 0.0),
            "AVG_current": float(ov_cur.get("AVG", 0.0) or 0.0),
            "OBP_current": float(ov_cur.get("OBP", 0.0) or 0.0),
            "SLG_current": float(ov_cur.get("SLG", 0.0) or 0.0),
            "OPS_current": float(ov_cur.get("OPS", 0.0) or 0.0),
        }

    def _pitcher_rates(p: Dict[str, Any]) -> Dict[str, Any]:
        pb_root = p.get("pitcher_bundle") or {}
        # Prefer target-lid-scoped views, with the same career fallback rule:
        # use target when the pitcher has any IP at the level, else cross-level.
        # Current-year always prefers the target view.
        ov_target = pb_root.get("overall_target") or {}
        if float(ov_target.get("IP", 0.0) or 0.0) > 0:
            ov = ov_target
        else:
            ov = pb_root.get("overall", {}) or {}
        if "current_target" in pb_root:
            ov_cur = pb_root.get("current_target") or {}
        else:
            ov_cur = pb_root.get("current", {}) or {}
        ip_total = float(ov.get("IP", 0.0) or 0.0)
        ip_cur = float(ov_cur.get("IP", 0.0) or 0.0)
        gs_total = float(ov.get("GS", 0.0) or 0.0)
        g_total = float(ov.get("G", 0.0) or 0.0)
        return {
            "name": p.get("name", ""),
            "IP": ip_total,
            "IP_current": ip_cur,
            "G": g_total,
            "GS": gs_total,
            "ERA": float(ov.get("ERA", 0.0) or 0.0),
            "FIP": float(ov.get("FIP", 0.0) or 0.0),
            "FIP_current": float(ov_cur.get("FIP", 0.0) or 0.0),
            "K/9": float(ov.get("K/9", 0.0) or 0.0),
            "BB/9": float(ov.get("BB/9", 0.0) or 0.0),
            "HR/9": float(ov.get("HR/9", 0.0) or 0.0),
            "K%": float(ov.get("K%", 0.0) or 0.0),
            "BB%": float(ov.get("BB%", 0.0) or 0.0),
            "WHIP": float(ov.get("WHIP", 0.0) or 0.0),
            "GB%": float(ov.get("GB%", 0.0) or 0.0),
            # Role-utilization rates for projecting W/L/SV/HLD/QS counting lines.
            "IP_per_GS": _safe_div(ip_total, gs_total),
            "IP_per_G": _safe_div(ip_total, g_total),
            "W_per_GS": _safe_div(ov.get("W", 0.0), gs_total),
            "L_per_GS": _safe_div(ov.get("L", 0.0), gs_total),
            "QS_per_GS": _safe_div(ov.get("QS", 0.0), gs_total),
            "SV_per_G": _safe_div(ov.get("SV", 0.0), g_total),
            "HLD_per_G": _safe_div(ov.get("HLD", 0.0), g_total),
            # Current-year actuals — used by project_season's overlay.
            "G_current": float(ov_cur.get("G", 0.0) or 0.0),
            "GS_current": float(ov_cur.get("GS", 0.0) or 0.0),
            "W_current": float(ov_cur.get("W", 0.0) or 0.0),
            "L_current": float(ov_cur.get("L", 0.0) or 0.0),
            "SV_current": float(ov_cur.get("SV", 0.0) or 0.0),
            "HLD_current": float(ov_cur.get("HLD", 0.0) or 0.0),
            "QS_current": float(ov_cur.get("QS", 0.0) or 0.0),
            "ERA_current": float(ov_cur.get("ERA", 0.0) or 0.0),
            "WHIP_current": float(ov_cur.get("WHIP", 0.0) or 0.0),
            "K/9_current": float(ov_cur.get("K/9", 0.0) or 0.0),
            "BB/9_current": float(ov_cur.get("BB/9", 0.0) or 0.0),
            "HR/9_current": float(ov_cur.get("HR/9", 0.0) or 0.0),
        }

    player_stats_payload: Dict[str, Any] = {
        "level": level,
        "year": target_year,
        "hitters": {},
        "pitchers": {},
    }
    # Hitters in the placed map (starters + position depth).
    for slots in placed.values():
        for p in slots:
            pid = str(p.get("pid", ""))
            if pid and pid not in player_stats_payload["hitters"]:
                player_stats_payload["hitters"][pid] = _hitter_rates(p)
    # Pitchers in the role-tier slots.
    for slots in pitcher_slots.values():
        for p in slots:
            pid = str(p.get("pid", ""))
            if pid and pid not in player_stats_payload["pitchers"]:
                player_stats_payload["pitchers"][pid] = _pitcher_rates(p)

    out_player_stats = out_dir / f"{org_slug}_{level}_{ts}_player_stats.json"
    out_player_stats.write_text(json.dumps(player_stats_payload, indent=2), encoding="utf-8")
    logger.info("Wrote %s", out_player_stats)

    # Starter-gaps sidecar — only written when --min-comp (or per-pos) was set.
    # Picked up by free_agent_market.py to drive its "Empty Starter Slots — FA
    # Candidates" section without the user re-passing the threshold.
    if global_min is not None or per_pos_min:
        # Per-position threshold map for *every* hitter position, so the FA
        # tool can score candidates consistently even at positions that
        # happened to have a qualified starter today.
        thresholds_full = {
            pos: resolve_min_comp(pos, global_min, per_pos_min)
            for pos in HITTER_POSITIONS
        }
        gaps_payload = {
            "level": level,
            "source_eval": eval_path.name if eval_path else None,
            "source_eval_ts": eval_ts_from_path(eval_path),
            "global_min_comp": global_min,
            "min_comp_per_pos": per_pos_min,
            "thresholds": {k: v for k, v in thresholds_full.items() if v is not None},
            "empty_slots": [
                {
                    "pos": g["pos"],
                    "would_be_pid": g["would_be"].get("pid", ""),
                    "would_be_name": g["would_be"].get("name", ""),
                    "would_be_composite": float(g["would_be"].get("composite", 0.0)),
                    "threshold": float(g["threshold"]),
                    "gap": float(g["gap"]),
                }
                for g in starter_gaps
            ],
        }
        out_gaps = out_dir / f"{org_slug}_{level}_{ts}_starter_gaps.json"
        out_gaps.write_text(json.dumps(gaps_payload, indent=2), encoding="utf-8")
        logger.info("Wrote %s", out_gaps)

    level_data: Dict[str, Any] = {
        "level": level,
        "starters_by_pos": starters_by_pos,
        "lineup_l": lineup_l,
        "lineup_r": lineup_r,
        "pitcher_slots": pitcher_slots,
        "bench": bench,
        "placed": placed,
        "promotions": promotions,
        "replacements": replacements,
        "demotions": demotions,
        "mismatches": mismatches,
        "hitter_pool": hitter_pool,
        "pitcher_pool": pitcher_pool,
        "depth_table": depth_table,
        "starter_gaps": starter_gaps,
        "min_comp_global": global_min,
        "min_comp_per_pos": per_pos_min,
    }
    return 0, level_data


def render_org_summary(
    league: str,
    org: str,
    year: int,
    level_data_list: List[Dict[str, Any]],
) -> str:
    """One unified MD report rolling up every level's depth chart, lineups,
    pitching staff, bench, and promotion candidates."""
    out: List[str] = []
    out.append(f"# {org} — Org Depth Summary  ·  {league.upper()}  ·  {year}")
    out.append("")
    out.append(f"_Generated {datetime.now().strftime('%Y-%m-%d %H:%M')}._")
    out.append("")

    # Quick org snapshot — one row per level with headline players.
    out.append("## Org Snapshot")
    out.append("")
    out.append("| Level | Top Hitter | Top SP | Top RP | Promotion Cands | Demotions |")
    out.append("| --- | --- | --- | --- | --- | --- |")
    for d in level_data_list:
        # Top hitter = highest composite in the placed map
        all_hitters = [p for slots in d["placed"].values() for p in slots]
        top_h = max(all_hitters, key=lambda p: p["composite"], default=None)
        sp_list = d["pitcher_slots"].get("SP", [])
        rp_list = (
            d["pitcher_slots"].get("CL", [])
            + d["pitcher_slots"].get("SU", [])
            + d["pitcher_slots"].get("MR", [])
            + d["pitcher_slots"].get("LR", [])
        )
        top_sp = max(sp_list, key=lambda p: p["composite"], default=None)
        top_rp = max(rp_list, key=lambda p: p["composite"], default=None)
        n_promos = len(d["promotions"])
        n_dems = len(d["demotions"])

        def _fmt(p: Optional[Dict[str, Any]]) -> str:
            return f"{p['name']} ({p['composite']:.1f})" if p else "—"

        out.append(
            f"| {d['level']} | {_fmt(top_h)} | {_fmt(top_sp)} | {_fmt(top_rp)} | "
            f"{n_promos} | {n_dems} |"
        )
    out.append("")

    # Promotion ladder — cross-level flow showing who's pushing for each level.
    out.append("## Promotion Ladder")
    out.append("")
    out.append("_Promotion candidates flagged at each level. Read top-down: who's pushing into each tier?_")
    out.append("")
    for d in level_data_list:
        if not d["promotions"]:
            continue
        out.append(f"### Pushing for {d['level']}")
        out.append("")
        out.append("| Cand | Pos/Role | Career | Comp | Replaces | Their Comp | Edge |")
        out.append("| --- | --- | --- | --- | --- | --- | --- |")
        for cand, weakest, starter in d["promotions"][:8]:  # top 8 per level
            cc = cand["composite"]
            # Prefer starter comparison if it exists; otherwise weakest.
            comp = starter or weakest
            comp_name = comp["name"] if comp else "—"
            comp_score = f"{comp['composite']:.1f}" if comp else "—"
            edge = f"+{cc - comp['composite']:.1f}" if comp else "—"
            label = "starter" if starter else "bench"
            pos_label = cand.get("primary_pos") or cand.get("proj_role", "")
            out.append(
                f"| {cand['name']} | {pos_label} | {cand['vos']:.1f} | "
                f"{cc:.1f} | {comp_name} ({label}) | {comp_score} | {edge} |"
            )
        out.append("")

    # Per-level detail.
    for d in level_data_list:
        level = d["level"]
        out.append(f"## {level}")
        out.append("")

        # Lineups — condensed (no full stat columns).
        if d["lineup_r"]:
            out.append(f"### {level} — Lineup vs RHP")
            out.append("")
            out.append("| # | Name | Pos | wOBA (vs R) |")
            out.append("| --- | --- | --- | --- |")
            for slot, p in d["lineup_r"]:
                if p.get("_lineup_gap"):
                    out.append(f"| {slot} | — | {p.get('_assigned_pos', '')} | — |")
                    continue
                sb = (p.get("hitter_bundle") or {}).get("vs_r", {})
                out.append(
                    f"| {slot} | {p['name']} | "
                    f"{p.get('_assigned_pos', p.get('primary_pos',''))} | {sb.get('wOBA', 0):.3f} |"
                )
            out.append("")

        if d["lineup_l"]:
            out.append(f"### {level} — Lineup vs LHP")
            out.append("")
            out.append("| # | Name | Pos | wOBA (vs L) |")
            out.append("| --- | --- | --- | --- |")
            for slot, p in d["lineup_l"]:
                if p.get("_lineup_gap"):
                    out.append(f"| {slot} | — | {p.get('_assigned_pos', '')} | — |")
                    continue
                sb = (p.get("hitter_bundle") or {}).get("vs_l", {})
                out.append(
                    f"| {slot} | {p['name']} | "
                    f"{p.get('_assigned_pos', p.get('primary_pos',''))} | {sb.get('wOBA', 0):.3f} |"
                )
            out.append("")

        # Bench
        if d["bench"]:
            out.append(f"### {level} — Bench / Flex")
            out.append("")
            out.append("| Name | Pos | Career | Composite |")
            out.append("| --- | --- | --- | --- |")
            for p in d["bench"]:
                out.append(
                    f"| {p['name']} | {p.get('primary_pos','')} | "
                    f"{p['vos']:.1f} | {p['composite']:.1f} |"
                )
            out.append("")

        # Pitching staff — condensed.
        ps = d["pitcher_slots"]
        if any(ps.values()):
            out.append(f"### {level} — Pitching Staff")
            out.append("")
            out.append("| Role | Name | Composite | FIP |")
            out.append("| --- | --- | --- | --- |")
            for i, p in enumerate(ps.get("SP", []), start=1):
                b = (p.get("pitcher_bundle") or {}).get("overall", {})
                out.append(f"| SP{i} | {p['name']} | {p['composite']:.1f} | {b.get('FIP', 0):.2f} |")
            for role in ("CL", "SU", "MR", "LR"):
                for p in ps.get(role, []):
                    b = (p.get("pitcher_bundle") or {}).get("overall", {})
                    out.append(f"| {role} | {p['name']} | {p['composite']:.1f} | {b.get('FIP', 0):.2f} |")
            out.append("")

        # Replacements + demotions for this level.
        if d["replacements"]:
            out.append(f"### {level} — Replacement Candidates")
            out.append("")
            out.append("_On-roster players who'd be displaced by promotions._")
            out.append("")
            out.append("| Name | Pos | Career | Composite |")
            out.append("| --- | --- | --- | --- |")
            for p in d["replacements"]:
                out.append(
                    f"| {p['name']} | {p.get('primary_pos','')} | "
                    f"{p['vos']:.1f} | {p['composite']:.1f} |"
                )
            out.append("")

        if d["demotions"]:
            out.append(f"### {level} — Demotion Candidates")
            out.append("")
            out.append("_Underperforming for this level._")
            out.append("")
            out.append("| Name | Pos | Composite | Z |")
            out.append("| --- | --- | --- | --- |")
            for p in d["demotions"]:
                out.append(
                    f"| {p['name']} | {p.get('primary_pos','')} | "
                    f"{p['composite']:.1f} | {p['_demote_z']:.2f} |"
                )
            out.append("")

        if d["mismatches"]:
            out.append(f"### {level} — Pitcher Role Mismatches")
            out.append("")
            out.append("| Name | Projected | Suggested | Reason |")
            out.append("| --- | --- | --- | --- |")
            for m in d["mismatches"]:
                out.append(f"| {m['name']} | {m['projected']} | {m['suggested']} | {m['reason']} |")
            out.append("")

    return "\n".join(out)


def main() -> int:
    args = parse_args()
    logging.basicConfig(level=getattr(logging, args.log_level), format="%(levelname)s: %(message)s")

    # Parse --min-comp-pos up-front so a malformed value fails fast (before any
    # API fetches), and stash the parsed dict on args for run_one_level.
    try:
        args.min_comp_pos_map = parse_min_comp_pos(args.min_comp_pos)
    except ValueError as exc:
        logger.error("%s", exc)
        return 2
    if args.min_comp is not None or args.min_comp_pos_map:
        msg = []
        if args.min_comp is not None:
            msg.append(f"global={args.min_comp:g}")
        if args.min_comp_pos_map:
            overrides = ", ".join(f"{k}:{v:g}" for k, v in sorted(args.min_comp_pos_map.items()))
            msg.append(f"per-pos={{{overrides}}}")
        logger.info("Starter min-comp gate active — %s", "; ".join(msg))

    cfg = load_config(args.config)

    # Resolve which level(s) to run.
    if args.all_level_charts:
        levels_to_run = list(cfg["levels"].keys())
    else:
        if not args.level:
            logger.error("Either --level or --all-level-charts is required.")
            return 2
        lvl_upper = args.level.strip().upper()
        if lvl_upper not in cfg["levels"]:
            logger.error("Level '%s' not in depth_config.json. Available: %s",
                         lvl_upper, ", ".join(cfg["levels"].keys()))
            return 2
        levels_to_run = [lvl_upper]

    # Default to the league's in-game season (league_settings.json) rather than
    # the real-world calendar year — OOTP seasons rarely match, and a wrong year
    # fetches an empty stats window (silently dropping wOBA/FIP from the blend).
    settings_year = league_default_year(args.league)
    target_year = args.year or settings_year or datetime.now().year
    if not args.year and settings_year:
        logger.info("Using in-game season %d from league_settings.json (pass --year to override).", settings_year)

    # Resolve which org(s) to run.
    orgs_to_run: List[Tuple[str, Optional[str]]]
    if args.all_orgs:
        # Auto-resolve the league's default park-factors file if --park-factors
        # wasn't explicitly passed. Without this, resolve_all_orgs falls back to
        # either the {league}_orgs.json scoping file or (worst case) a Parent==0
        # scan of teams-{league}.json, which incorrectly catches college / HS /
        # indy / international teams as their own orgs.
        pf_path = args.park_factors or _default_park_factors_path(args.league)
        orgs_file = Path("config") / f"{args.league}_orgs.json"
        if not args.park_factors and pf_path.exists():
            logger.info("--all-orgs auto-resolved park-factors: %s", pf_path)
        try:
            orgs_to_run = resolve_all_orgs(args.league, Path("config"), pf_path)
        except FileNotFoundError as e:
            logger.error("%s", e)
            return 2
        if not orgs_to_run:
            logger.error("--all-orgs found no orgs for league %s", args.league)
            return 2
        # Only nag about missing team_code mapping when we have NO scoping
        # source at all (no park-factors AND no orgs file). Uniform-park-factors
        # leagues using {league}_orgs.json don't need this warning.
        if not pf_path.exists() and not orgs_file.exists():
            logger.warning(
                "--all-orgs without --park-factors and no default found at %s "
                "and no orgs file at %s: no team_code mapping available. "
                "Each org will use the top-level eval; per-org evals from "
                "%s/eval/<code>/ will be ignored.",
                pf_path, orgs_file, args.league,
            )
    else:
        if not args.org:
            logger.error("Either --org or --all-orgs is required.")
            return 2
        # Accept a team code in --org (e.g. "STL") by swapping it for the
        # canonical display name so downstream eval filtering still works.
        pf_path = args.park_factors or _default_park_factors_path(args.league)
        name_to_code = _name_to_code_map(pf_path)
        if args.org not in name_to_code:
            code_lookup = {c: n for n, c in name_to_code.items()}
            maybe_code = args.org.strip().lower()
            if maybe_code in code_lookup:
                logger.info("Resolved --org %r to %r (code=%s).",
                            args.org, code_lookup[maybe_code], maybe_code)
                args.org = code_lookup[maybe_code]
                if not args.org_code:
                    args.org_code = maybe_code
        orgs_to_run = [(args.org, args.org_code)]

    # Auto-archive any prior run's outputs so the top of depth/ only ever
    # holds the most recent run. Uses the same dir resolution as the writers
    # below — keeps `--output-dir` overrides honored. Skipped via --no-archive.
    if not args.no_archive:
        depth_dir_for_archive = args.output_dir or (SCRIPT_DIR / args.league / "depth")
        moved, archive_dir = archive_previous_runs(depth_dir_for_archive)
        if moved:
            logger.info("Archived %d file(s) from prior runs to %s", moved, archive_dir)

    # Single timestamp shared by every chart in this batch run.
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    multi_levels = len(levels_to_run) > 1
    multi_orgs = len(orgs_to_run) > 1
    overall_rc = 0

    # Fetch /players once for the whole batch — same daily disk cache used by
    # the stat endpoints, so this is free after the first run of the day.
    # Skipped only when --no-players-override or --no-stats is set.
    players_lookup: Dict[str, Dict[str, str]] = {}
    level_id_to_label: Dict[int, str] = {}
    team_id_to_name: Dict[int, str] = {}
    if not args.no_players_override and not args.no_stats:
        base_url = sapi.resolve_base_url(args.league, args.base_url, args.league_url_config)
        if base_url:
            cache_dir = None
            if not args.no_cache:
                cache_dir = args.cache_dir or (SCRIPT_DIR / args.league / "cache" / "stats")
            players_lookup = sapi.build_players_lookup(base_url, cache_dir=cache_dir)
            if players_lookup:
                level_id_to_label = load_level_id_to_label()
                team_id_to_name = load_team_id_to_name(args.league)
                logger.info(
                    "Loaded /players (%d players) — overriding stale eval Level/Org/Team and "
                    "filtering retired/DFA/waivers/DL60.",
                    len(players_lookup),
                )
            else:
                logger.warning(
                    "/players returned no rows — falling back to eval CSV for "
                    "Level/Org/Team. Promotions and demotions made after the eval "
                    "was generated will not be reflected."
                )
        else:
            logger.warning(
                "No base URL for league '%s' — skipping /players override.",
                args.league,
            )

    # CSV roster patch — applied on top of /players. Useful between sims when
    # the API hasn't refreshed yet but you've made manual roster moves in OOTP.
    # The eval CSV's id_maps and teams config are loaded lazily in case the
    # API path was skipped/empty above.
    if args.players_override_csv:
        if not level_id_to_label:
            level_id_to_label = load_level_id_to_label()
        if not team_id_to_name:
            team_id_to_name = load_team_id_to_name(args.league)
        team_name_to_id = invert_team_id_to_name(team_id_to_name)
        csv_patch = build_players_lookup_from_csv(args.players_override_csv, team_name_to_id)
        if csv_patch:
            collisions = sum(1 for pid in csv_patch if pid in players_lookup)
            # CSV wins where it overlaps with the API payload.
            players_lookup.update(csv_patch)
            logger.info(
                "Applied CSV override patch: %d players | %d overrode existing /players entries.",
                len(csv_patch), collisions,
            )

    for org_idx, (org_name, org_code) in enumerate(orgs_to_run, start=1):
        # Apply this org's identity to args so downstream helpers see it.
        args.org = org_name
        args.org_code = org_code

        if multi_orgs:
            logger.info("#" * 60)
            logger.info("Org %d/%d: %s (code=%s)", org_idx, len(orgs_to_run), org_name, org_code or "—")

        # Load eval per org (per-org subdir if org_code resolves, else top-level).
        try:
            eval_path = find_latest_eval(args.league, args.input, org_code)
        except FileNotFoundError as e:
            logger.error("%s — skipping %s", e, org_name)
            overall_rc = 2
            continue
        logger.info("Using eval file: %s", eval_path)
        eval_rows = read_eval(eval_path)

        # Reconcile eval against the live /players payload — promotions,
        # demotions, trades, DL60, DFA, and waivers all show up here.
        if players_lookup:
            counts = apply_players_override(
                eval_rows,
                players_lookup,
                level_id_to_label,
                team_id_to_name,
                include_inactive=args.include_inactive,
            )
            logger.info(
                "Players override: %d eval rows | %d level overrides | %d org overrides | "
                "filtered: %d retired, %d DFA, %d waivers, %d DL60 | "
                "%d missing in /players | %d unrecognized level",
                counts["total"],
                counts["level_overrides"],
                counts["org_overrides"],
                counts["filtered_retired"],
                counts["filtered_dfa"],
                counts["filtered_waivers"],
                counts["filtered_dl60"],
                counts["missing_in_players"],
                counts["unrecognized_level"],
            )

        collected: List[Dict[str, Any]] = []
        # Expand levels_to_run to include per-affiliate sub-levels when a level
        # has split_by_affiliate enabled AND this org actually has multiple
        # affiliates at that level (e.g., R-ball ACL + DSL).
        expanded_levels = expand_levels_for_affiliates(
            levels_to_run, eval_rows, args.org, cfg,
        )
        for display_lvl, base_lvl, aff in expanded_levels:
            if multi_levels:
                logger.info("=" * 60)
                logger.info("Building depth chart for level: %s", display_lvl)
            rc_lvl, level_data = run_one_level(
                display_lvl, args, cfg, eval_rows, target_year, ts,
                multi_level_run=multi_levels,
                affiliate=aff, base_level=base_lvl,
                eval_path=eval_path,
            )
            if rc_lvl != 0:
                overall_rc = rc_lvl
                continue
            if level_data is not None:
                collected.append(level_data)

        # Provenance sidecar — records which eval this batch was built from so
        # free_agent_market.py can detect a stale depth batch (built from an
        # older eval than the current latest) and regenerate before scanning.
        # Written for single- and multi-level runs alike, in {league}/depth/.
        if collected:
            meta_out_dir = args.output_dir or (SCRIPT_DIR / args.league / "depth")
            meta_out_dir.mkdir(parents=True, exist_ok=True)
            meta_path = write_depth_meta(
                meta_out_dir, filename_org_slug(args), ts, eval_path,
                [ld.get("level", "") for ld in collected], args,
            )
            logger.info("Wrote depth provenance: %s", meta_path)

        # Org-wide summary report — only when running multiple levels.
        if multi_levels and collected:
            out_dir = args.output_dir or (SCRIPT_DIR / args.league / "depth")
            out_dir.mkdir(parents=True, exist_ok=True)
            org_slug = filename_org_slug(args)

            summary_path = out_dir / f"{org_slug}_org_summary_{ts}.md"
            summary_md = render_org_summary(args.league, org_name, target_year, collected)
            summary_path.write_text(summary_md, encoding="utf-8")
            logger.info("Wrote org summary (MD): %s", summary_path)

            if not args.no_pdf:
                try:
                    import org_summary_pdf
                    pdf_path = out_dir / f"{org_slug}_org_summary_{ts}.pdf"
                    org_summary_pdf.render_pdf(pdf_path, args.league, org_name, target_year, collected)
                    logger.info("Wrote org summary (PDF): %s", pdf_path)
                except ImportError as e:
                    logger.warning("Skipping PDF (reportlab not available): %s", e)
                except Exception as e:
                    logger.warning("PDF render failed: %s", e)

    return overall_rc


if __name__ == "__main__":
    raise SystemExit(main())
