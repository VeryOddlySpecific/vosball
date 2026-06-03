#!/usr/bin/env python3
"""
Draft Grades — Compares draft results to v10 VOS draft pool projections.

Reads 05_draft_pool.md (sorted by Outlook under v10) from a directory, fetches
current draft status from the league API, and awards "VOS Stamps" when a player
is drafted at or after their projection. The projection tiers, point values,
managed-risk parameters, delta bonus, and grade bands are all driven by
``config/draft_grades.json`` so the scoring can be tuned without code changes.

Default tier structure (per the v10 calibration in
`OOTP Study 27/draft_grades_calibration.md`):

  - Top 25:  rank 1-25,   7.0 base points
  - Top 100: rank 26-100, 3.5 base points
  - Later:   rank 101+,   1.5 base points

A log-scaled bonus (DELTA_LOG_SCALE * ln(1+delta)) is added when a player is
taken at or beyond projection. Reaches earn no points unless smaller than
num_teams (managed risk), which earns POINTS_MANAGED_RISK + log bonus. Grades
A–F are assigned by points range across the league (five equal bands).
"""

# --- tools/ -> repo-root bootstrap (added during tools/ move) ---
import os as _os, sys as _sys
_ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
if _ROOT not in _sys.path:
    _sys.path.insert(0, _ROOT)
# --- end bootstrap ---

import argparse
import csv
import json
import math
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.request import urlopen, Request

_SCRIPT_DIR = Path(__file__).resolve().parent.parent
DEFAULT_LEAGUE_URL_CONFIG = _SCRIPT_DIR / "config" / "league_url.json"
DEFAULT_GRADES_CONFIG = _SCRIPT_DIR / "config" / "draft_grades.json"


def load_api_base_url(league: Optional[str], config_path: Path) -> Optional[str]:
    """Look up the API base URL for a league from config/league_url.json."""
    if not league:
        return None
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return None
    return raw.get(league.strip().lower(), {}) if isinstance(raw, dict) else None


# ---------------------------------------------------------------------------
# Grading configuration — tier definitions, bonuses, and grade bands.
# Loaded from config/draft_grades.json; sensible v10-calibrated defaults
# kept as a fallback so the script still works if the config file is missing.
# ---------------------------------------------------------------------------

DEFAULT_GRADES_CONFIG_DATA: Dict[str, Any] = {
    "projection_tiers": [
        {"name": "Top 25",  "max_rank": 25,   "base_points": 7.0},
        {"name": "Top 100", "max_rank": 100,  "base_points": 3.5},
        {"name": "Later",   "max_rank": None, "base_points": 1.5},
    ],
    "managed_risk":  {"base_points": 0.75, "log_scale": 0.25},
    "delta_bonus":   {"log_scale": 0.5},
    "grade_bands": [
        {"position_max": 0.2, "grade": "F"},
        {"position_max": 0.4, "grade": "D"},
        {"position_max": 0.6, "grade": "C"},
        {"position_max": 0.8, "grade": "B"},
        {"position_max": 1.0, "grade": "A"},
    ],
}


def load_grades_config(config_path: Optional[Path] = None) -> Dict[str, Any]:
    """Load grading config from JSON. Falls back to defaults if file missing.

    Returns a dict with normalized shape (validated tier ordering, etc.) so
    callers don't need to defensively handle malformed config.
    """
    path = config_path or DEFAULT_GRADES_CONFIG
    cfg: Dict[str, Any]
    if path.exists():
        try:
            cfg = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            print(f"Warning: could not read {path}: {exc} — using built-in defaults.",
                  file=sys.stderr)
            cfg = dict(DEFAULT_GRADES_CONFIG_DATA)
    else:
        cfg = dict(DEFAULT_GRADES_CONFIG_DATA)

    # Validate + normalize. Failures fall back to defaults for that section
    # rather than crash the run.
    tiers = cfg.get("projection_tiers")
    if not isinstance(tiers, list) or not tiers:
        tiers = list(DEFAULT_GRADES_CONFIG_DATA["projection_tiers"])
    # Sort tiers by max_rank ascending; nulls (open-ended) sort last. This lets
    # config authors list them in any order without breaking the lookup.
    def _tier_sort_key(t: Dict[str, Any]) -> float:
        mr = t.get("max_rank")
        return float("inf") if mr is None else float(mr)
    tiers = sorted([t for t in tiers if isinstance(t, dict)], key=_tier_sort_key)
    # Ensure the last tier is open-ended; if not, append a default Later tier
    # so projections past the last cap still grade.
    if tiers and tiers[-1].get("max_rank") is not None:
        tiers.append({"name": "Later", "max_rank": None, "base_points": 1.5})
    cfg["projection_tiers"] = tiers

    mr = cfg.get("managed_risk")
    if not isinstance(mr, dict):
        mr = dict(DEFAULT_GRADES_CONFIG_DATA["managed_risk"])
    cfg["managed_risk"] = mr

    db = cfg.get("delta_bonus")
    if not isinstance(db, dict):
        db = dict(DEFAULT_GRADES_CONFIG_DATA["delta_bonus"])
    cfg["delta_bonus"] = db

    bands = cfg.get("grade_bands")
    if not isinstance(bands, list) or not bands:
        bands = list(DEFAULT_GRADES_CONFIG_DATA["grade_bands"])
    cfg["grade_bands"] = sorted(
        [b for b in bands if isinstance(b, dict)],
        key=lambda b: float(b.get("position_max", 1.0)),
    )

    return cfg


def _tier_for_rank(rank: Optional[int], tiers: List[Dict[str, Any]]
                   ) -> Optional[Dict[str, Any]]:
    """Return the tier dict whose max_rank covers ``rank``. Returns None when
    rank is missing/None (so callers can short-circuit). Always returns a
    tier for a valid integer rank — the last tier should be open-ended.
    """
    if rank is None:
        return None
    for tier in tiers:
        max_rank = tier.get("max_rank")
        if max_rank is None or rank <= int(max_rank):
            return tier
    return tiers[-1] if tiers else None


def _safe_int(value: Any) -> Optional[int]:
    """Convert to int or return None. Used for projection-rank parsing."""
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


# Module-level cache for the loaded config. Populated by ``main`` before any
# grading work; helpers below read from it. Tests can set it directly.
_GRADES_CFG: Dict[str, Any] = load_grades_config()


def _refresh_grades_cfg(config_path: Optional[Path]) -> None:
    """Reload the module-level config (used by main() after parsing CLI)."""
    global _GRADES_CFG
    _GRADES_CFG = load_grades_config(config_path)


# Back-compat shim — old code referenced TOP_PROJECTION_CAP for VOS Stamp
# semantics. Under v10's tiered system, "Top 100" is the second tier; the
# "VOS Stamp" still covers all projections within the broader top-100 cohort
# (any tier whose max_rank is <= 100). Helpers use the live config; this
# constant exists only so a downstream consumer that imports it doesn't break.
TOP_PROJECTION_CAP = 100


def _normalize_name(name: str) -> str:
    """Normalize name for matching: strip and collapse internal spaces."""
    if not name:
        return ""
    return " ".join(str(name).strip().split())


def find_draft_pool_md(directory: Path) -> Path:
    """Look for draft_pool.md or 05_draft_pool.md in directory."""
    for candidate in ("05_draft_pool.md", "draft_pool.md"):
        p = directory / candidate
        if p.exists():
            return p
    raise FileNotFoundError(
        f"No draft pool file found in {directory}. Expected 05_draft_pool.md or draft_pool.md"
    )


def resolve_directory(dir_arg: Optional[str], league: Optional[str], script_dir: Path) -> Path:
    """
    Resolve the input directory across the project's `{league}/drafts/{folder}/` layout.

    Accepts (in order of preference):
      - An absolute path
      - A path relative to cwd that exists
      - A path relative to script_dir (e.g. 'woba/drafts/2041_woba_draft')
      - A draft folder name resolved under '{league}/drafts/' when --league is set
        (e.g. dir='2041_woba_draft' + league='woba' -> '{script_dir}/woba/drafts/2041_woba_draft')
    """
    if not dir_arg:
        raise FileNotFoundError(
            "No directory provided. Pass a path, or pass --league plus a draft folder name."
        )
    p = Path(dir_arg)
    if p.is_absolute() and p.is_dir():
        return p
    # Try as-is (relative to cwd)
    if p.is_dir():
        return p.resolve()
    # Try relative to script directory (e.g. 'woba/drafts/2041_woba_draft')
    candidate = script_dir / p
    if candidate.is_dir():
        return candidate
    # Try as a draft folder name under '{league}/drafts/'
    if league:
        candidate = script_dir / league.strip().lower() / "drafts" / p
        if candidate.is_dir():
            return candidate
    raise FileNotFoundError(f"Could not resolve directory: {dir_arg}")


def infer_league_from_path(directory: Path, script_dir: Path) -> Optional[str]:
    """
    Infer the league slug from a path like '{script_dir}/{league}/drafts/{folder}/'.
    Returns None if path doesn't match that pattern.
    """
    try:
        rel = directory.resolve().relative_to(script_dir.resolve())
    except ValueError:
        return None
    parts = rel.parts
    if len(parts) >= 2 and parts[1] == "drafts":
        return parts[0].lower()
    return None


def load_projections_from_md(
    md_path: Path,
) -> Tuple[Dict[str, int], Dict[str, str], Dict[str, int], Dict[str, Dict[str, str]]]:
    """
    Parse draft pool markdown table.

    Handles both legacy and v10 column layouts. Under v10, draft_pool_analysis.py
    sorts the MD by **Outlook** (not Ideal Value), so the Rank column reflects
    Outlook ranking. The v10 column block (Outlook / Reach / Career / Pers /
    Prone) is captured into a parallel dict and surfaced in the per-pick output.

    Returns (name_to_rank, name_to_pos, name_to_id, name_to_v10):
      - name_to_rank: normalized player name -> 1-based projection rank
      - name_to_pos: normalized player name -> position (e.g. "CF")
      - name_to_id:  normalized player name -> player ID (int), if ID column present
      - name_to_v10: normalized player name -> {"Outlook", "Reach", "Career",
                     "Pers", "Prone"} of MD cell strings. Missing columns are
                     stored as empty strings so callers can render blank cells
                     for pre-v10 MDs without conditional checks.
    """
    text = md_path.read_text(encoding="utf-8")
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    name_to_rank: Dict[str, int] = {}
    name_to_pos: Dict[str, str] = {}
    name_to_id: Dict[str, int] = {}
    name_to_v10: Dict[str, Dict[str, str]] = {}
    rank = 0
    in_table = False
    name_idx: Optional[int] = None
    pos_idx: Optional[int] = None
    id_idx: Optional[int] = None
    # v10 column indices — populated when the header row contains them.
    v10_idx: Dict[str, Optional[int]] = {
        "Outlook": None, "Reach": None, "Career": None, "Pers": None, "Prone": None,
    }
    for line in lines:
        if not line.startswith("|"):
            continue
        raw = [p.strip() for p in line.split("|")]
        if raw and raw[0] == "":
            raw = raw[1:]
        if raw and raw[-1] == "":
            raw = raw[:-1]
        parts = raw
        if not parts:
            continue
        lower_parts = [p.lower() for p in parts]
        if not in_table and "name" in lower_parts:
            name_idx = lower_parts.index("name")
            if "pos" in lower_parts:
                pos_idx = lower_parts.index("pos")
            else:
                pos_idx = next(
                    (i for i, p in enumerate(lower_parts) if "pos" in p and i != name_idx),
                    None,
                )
            id_idx = lower_parts.index("id") if "id" in lower_parts else None
            # v10 column lookups — case-insensitive on the actual header label.
            for key in v10_idx:
                v10_idx[key] = (
                    lower_parts.index(key.lower())
                    if key.lower() in lower_parts else None
                )
            in_table = True
            continue
        if in_table and "---" in line:
            continue
        if not in_table or name_idx is None:
            continue
        if name_idx >= len(parts):
            continue
        name = _normalize_name(parts[name_idx])
        if not name:
            continue
        pos = parts[pos_idx].strip() if (pos_idx is not None and pos_idx < len(parts)) else ""
        rank += 1
        name_to_pos[name] = pos
        name_to_rank[name] = rank
        if id_idx is not None and id_idx < len(parts):
            try:
                name_to_id[name] = int(parts[id_idx].strip())
            except (ValueError, TypeError):
                pass
        # Capture v10 columns — empty string when absent so the output stays
        # clean for pre-v10 MDs.
        name_to_v10[name] = {
            key: (parts[idx].strip() if (idx is not None and idx < len(parts)) else "")
            for key, idx in v10_idx.items()
        }
    return name_to_rank, name_to_pos, name_to_id, name_to_v10


def load_team_codes(league: str, script_dir: Path) -> Dict[str, str]:
    """Read team_name -> team_code (lowercased) from config/<league>-park-factors.json."""
    if not league:
        return {}
    pf_path = script_dir / "config" / f"{league.strip().lower()}-park-factors.json"
    if not pf_path.exists():
        return {}
    try:
        data = json.loads(pf_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    teams = data.get("teams", {}) if isinstance(data, dict) else {}
    out: Dict[str, str] = {}
    for name, block in teams.items():
        if not isinstance(block, dict) or name.startswith("_"):
            continue
        info = block.get("team_info") or {}
        code = (info.get("team_code") or "").strip().lower()
        if code:
            out[name] = code
    return out


def find_latest_org_eval_csv(league_dir: Path, team_code: str) -> Optional[Path]:
    """Latest draft_evaluation_*.csv under <league_dir>/eval/<team_code>/."""
    eval_dir = league_dir / "eval" / team_code
    if not eval_dir.is_dir():
        return None
    candidates = sorted(eval_dir.glob("draft_evaluation_*.csv"))
    return candidates[-1] if candidates else None


def load_per_team_board(
    eval_csv: Path, draft_pool_ids: set,
) -> Tuple[Dict[str, int], Dict[str, Dict[str, str]]]:
    """
    Read team's park-adjusted eval CSV, filter to draft-pool IDs, sort by Ideal_Value desc.

    Returns (name_to_rank, name_to_v10_eval_cols):
      - name_to_rank: 1-based per-team board rank
      - name_to_v10_eval_cols: per-player v10 cell strings sourced from the
        eval CSV. Eval column names differ from the master MD's short labels:
            VOS_Reach        -> Reach
            VOS_Career       -> Career
            Personality_Adj  -> Pers
            Prone            -> Prone
        Outlook is NOT in the eval CSV (only the master MD has it); it's
        omitted from the org variant of the v10 columns. Missing columns
        render as empty strings.

    NOTE: per-team boards are still ranked by Ideal_Value (the heuristic
    Reach composite). Outlook isn't park-adjusted in run_vos.py — only the
    heuristic batting/defense composites are — so a per-team Outlook ranking
    wouldn't differ from the master Outlook ranking. Keeping Ideal_Value as
    the per-team sort key preserves the park-adjusted board's distinct
    information value.
    """
    entries: List[Tuple[float, str]] = []
    v10_by_name: Dict[str, Dict[str, str]] = {}
    with open(eval_csv, "r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                pid = int((row.get("ID") or "").strip() or 0)
            except ValueError:
                continue
            if pid not in draft_pool_ids:
                continue
            try:
                iv = float((row.get("Ideal_Value") or "").strip() or 0)
            except ValueError:
                iv = 0.0
            name = _normalize_name(row.get("Name", ""))
            if not name:
                continue
            entries.append((iv, name))
            v10_by_name[name] = {
                "Reach": (row.get("VOS_Reach") or "").strip(),
                "Career": (row.get("VOS_Career") or "").strip(),
                "Pers": (row.get("Personality_Adj") or "").strip(),
                "Prone": (row.get("Prone") or "").strip(),
            }
    entries.sort(key=lambda x: (-x[0], x[1]))
    name_to_rank = {name: i + 1 for i, (_, name) in enumerate(entries)}
    return name_to_rank, v10_by_name


def build_team_boards(
    league: Optional[str],
    league_dir: Path,
    script_dir: Path,
    draft_pool_ids: set,
) -> Tuple[Dict[str, Dict[str, int]], Dict[str, Dict[str, Dict[str, str]]], List[str]]:
    """
    Build {team_name -> name_to_rank} and {team_name -> {name -> v10 cols}}
    for all teams with a park-factors entry.

    Returns (team_boards, team_v10, missing_teams). Teams without an eval
    CSV are listed in missing_teams; caller can warn and fall back to the
    master board.
    """
    team_boards: Dict[str, Dict[str, int]] = {}
    team_v10: Dict[str, Dict[str, Dict[str, str]]] = {}
    missing: List[str] = []
    codes = load_team_codes(league or "", script_dir)
    if not codes:
        return team_boards, team_v10, missing
    for team_name, code in codes.items():
        csv_path = find_latest_org_eval_csv(league_dir, code)
        if csv_path is None:
            missing.append(team_name)
            continue
        board, v10 = load_per_team_board(csv_path, draft_pool_ids)
        if not board:
            missing.append(team_name)
            continue
        team_boards[team_name] = board
        team_v10[team_name] = v10
    return team_boards, team_v10, missing


def fetch_draft_csv(api_url: str) -> List[Dict[str, str]]:
    """Fetch draft status CSV from league API. Returns list of row dicts."""
    req = Request(api_url, headers={"User-Agent": "DraftGrades/1.0"})
    with urlopen(req, timeout=30) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
    reader = csv.DictReader(
        (line for line in raw.splitlines() if line.strip()),
        quotechar='"',
        skipinitialspace=True,
    )
    rows = []
    for row in reader:
        # Normalize keys (API may use "Player Name" / "Overall" / "Team")
        rows.append({k.strip(): v for k, v in row.items()})
    return rows


def get_draft_value(row: Dict[str, str], *keys: str) -> Optional[str]:
    """Get first non-empty value from row for given keys."""
    for k in keys:
        v = row.get(k)
        if v is not None and str(v).strip():
            return str(v).strip()
    return None


def _grade_pick(projection: Optional[int], overall: int, num_teams: int) -> Tuple[str, str, float]:
    """Per-pick: returns (pick_grade, stamp_type, points) given a projection rank.

    Same algo regardless of whether the projection is from the master board or
    the drafting team's park-adjusted board.

    Stamp_type comes from the tier the player's projection falls into (per
    ``config/draft_grades.json``), with "Managed Risk" as the special stamp for
    small reaches. Pick_grade letter (S/A/B/C/D/F) is unchanged from the v3
    behavior — it's a function of |delta|, not which tier the player was in.
    """
    cfg = _GRADES_CFG
    tiers: List[Dict[str, Any]] = cfg["projection_tiers"]
    delta_log_scale = float(cfg["delta_bonus"].get("log_scale", 0.5))
    mr_cfg = cfg["managed_risk"]
    mr_base = float(mr_cfg.get("base_points", 0.75))
    mr_log_scale = float(mr_cfg.get("log_scale", 0.25))

    pick_grade = ""
    stamp_type = ""
    points = 0.0
    if projection is None:
        return pick_grade, stamp_type, points
    delta = overall - projection
    if delta > num_teams:
        pick_grade = "S"
    elif delta >= 0:
        pick_grade = "A"
    else:
        reach = abs(delta)
        if reach <= 5:
            pick_grade = "B"
        elif reach <= 15:
            pick_grade = "C"
        elif reach <= 30:
            pick_grade = "D"
        else:
            pick_grade = "F"
    if overall >= projection:
        safe_delta = max(0, delta)
        log_bonus = delta_log_scale * math.log(1 + safe_delta)
        tier = _tier_for_rank(projection, tiers)
        if tier is not None:
            points = float(tier.get("base_points", 0.0)) + log_bonus
            stamp_type = str(tier.get("name", ""))
    elif delta < 0 and abs(delta) < num_teams:
        reach = abs(delta)
        log_bonus = mr_log_scale * math.log(1 + (num_teams - reach))
        points = mr_base + log_bonus
        stamp_type = "Managed Risk"
    return pick_grade, stamp_type, points


def _lookup_rank(board: Dict[str, int], norm_name: str) -> Optional[int]:
    """Exact, then case-insensitive fallback."""
    r = board.get(norm_name)
    if r is not None:
        return r
    low = norm_name.lower()
    for k, v in board.items():
        if k.lower() == low:
            return v
    return None


def compare_draft_to_projections(
    draft_rows: List[Dict[str, str]],
    name_to_rank: Dict[str, int],
    num_teams: int,
    team_boards: Optional[Dict[str, Dict[str, int]]] = None,
    name_to_v10: Optional[Dict[str, Dict[str, str]]] = None,
    team_v10: Optional[Dict[str, Dict[str, Dict[str, str]]]] = None,
) -> List[Dict]:
    """
    For each pick:
      - Market: rank from master board (name_to_rank), delta, stamp, points.
      - Org (if team_boards): rank from drafting team's park-adjusted board.
        Falls back to master board for teams missing from team_boards.

    v10 columns (Outlook / Reach / Career / Pers / Prone) flow through onto
    each result row when ``name_to_v10`` is provided. The same columns
    sourced from the drafting team's park-adjusted eval (Org Reach / Org
    Career / Org Pers / Org Prone) flow through when ``team_v10`` is
    provided. Outlook is master-MD-only — it isn't surfaced in the per-team
    eval CSV, so there's no Org Outlook.
    """
    results = []
    for row in draft_rows:
        name = get_draft_value(row, "Player Name", "Player name", "Name")
        team = get_draft_value(row, "Team")
        overall_raw = get_draft_value(row, "Overall")
        if not name or overall_raw is None:
            continue
        try:
            overall = int(overall_raw)
        except ValueError:
            continue
        norm_name = _normalize_name(name)

        market_proj = _lookup_rank(name_to_rank, norm_name)
        market_delta = (overall - market_proj) if market_proj is not None else None
        mkt_grade, mkt_stamp, mkt_pts = _grade_pick(market_proj, overall, num_teams)

        org_proj: Optional[int] = None
        org_v10_cells: Dict[str, str] = {}
        if team_boards is not None and team:
            board = team_boards.get(team)
            org_v10_src: Optional[Dict[str, Dict[str, str]]] = (team_v10 or {}).get(team) if team_v10 else None
            if board is None:
                for tn, b in team_boards.items():
                    if tn.lower() == team.lower():
                        board = b
                        if team_v10 is not None:
                            org_v10_src = team_v10.get(tn)
                        break
            if board is None:
                # Fallback: use master board so the org column still has a value.
                # No per-team v10 cells in this case — they'd duplicate the
                # market columns, which isn't informative.
                board = name_to_rank
                org_v10_src = None
            org_proj = _lookup_rank(board, norm_name)
            if org_v10_src is not None:
                org_v10_cells = org_v10_src.get(norm_name, {}) or {}
        org_delta = (overall - org_proj) if org_proj is not None else None
        org_grade, org_stamp, org_pts = _grade_pick(org_proj, overall, num_teams)

        # Master-MD v10 cells (Outlook is here; Org Outlook does not exist).
        v10_cells = (name_to_v10 or {}).get(norm_name, {}) or {}

        result = {
            "Player Name": name,
            "Team": team or "",
            "Overall Pick": overall,
            "Projection Rank": market_proj if market_proj is not None else "",
            "Delta": market_delta if market_delta is not None else "",
            "Pick Grade": mkt_grade,
            "Stamp Type": mkt_stamp,
            "Points": mkt_pts,
            "VOS Stamp": "Y" if mkt_pts > 0 else "N",
            # v10 columns (master MD)
            "Outlook": v10_cells.get("Outlook", ""),
            "Reach": v10_cells.get("Reach", ""),
            "Career": v10_cells.get("Career", ""),
            "Pers": v10_cells.get("Pers", ""),
            "Prone": v10_cells.get("Prone", ""),
            # Park-adjusted (org) variants
            "Org Projection": org_proj if org_proj is not None else "",
            "Org Delta": org_delta if org_delta is not None else "",
            "Org Pick Grade": org_grade,
            "Org Stamp Type": org_stamp,
            "Org Points": org_pts,
            "Org VOS Stamp": "Y" if org_pts > 0 else "N",
            "Org Reach": org_v10_cells.get("Reach", ""),
            "Org Career": org_v10_cells.get("Career", ""),
            "Org Pers": org_v10_cells.get("Pers", ""),
            "Org Prone": org_v10_cells.get("Prone", ""),
        }
        results.append(result)
    return results


def _base_for_projection(projection) -> float:
    """Base stamp value for a drafted player with a projection (delta=0, no bonus).
    Lookup is tier-driven via the loaded grading config. Empty/None projection
    returns 0.0.
    """
    rank = _safe_int(projection)
    tier = _tier_for_rank(rank, _GRADES_CFG["projection_tiers"])
    if tier is None:
        return 0.0
    return float(tier.get("base_points", 0.0))


def _all_stamp_names() -> List[str]:
    """Active stamp type names from the loaded config: every projection tier
    name plus "Managed Risk" at the end. Used to size per-tier count dicts
    and drive column generation in the summary writers."""
    tier_names = [str(t.get("name", "")).strip()
                  for t in _GRADES_CFG.get("projection_tiers", [])]
    tier_names = [n for n in tier_names if n]
    if "Managed Risk" not in tier_names:
        tier_names.append("Managed Risk")
    return tier_names


def aggregate_by_team(rows: List[Dict]) -> Dict[str, Dict]:
    """Per team: points, base, and stamp counts (one entry per stamp name in
    the active config + Managed Risk). Both market and org grading variants.
    Org fields are only meaningful if compare_draft_to_projections was called
    with team_boards; otherwise org counts will be zero.

    The ``counts`` and ``org_counts`` sub-dicts are keyed by stamp name from
    the loaded config — so under the v10 default config, you'll see "Top 25",
    "Top 100", "Later", "Managed Risk". This keeps the data structure
    flexible across tier-schema changes.
    """
    stamp_names = _all_stamp_names()
    by_team: Dict[str, Dict] = {}
    for r in rows:
        team = (r.get("Team") or "").strip()
        if not team:
            continue
        if team not in by_team:
            by_team[team] = {
                "points": 0.0, "base": 0.0,
                "counts": {name: 0 for name in stamp_names},
                "org_points": 0.0, "org_base": 0.0,
                "org_counts": {name: 0 for name in stamp_names},
            }
        pt = float(r.get("Points") or 0)
        by_team[team]["points"] += pt
        by_team[team]["base"] += _base_for_projection(r.get("Projection Rank"))
        stamp = r.get("Stamp Type", "")
        if stamp in by_team[team]["counts"]:
            by_team[team]["counts"][stamp] += 1

        org_pt = float(r.get("Org Points") or 0)
        by_team[team]["org_points"] += org_pt
        by_team[team]["org_base"] += _base_for_projection(r.get("Org Projection"))
        org_stamp = r.get("Org Stamp Type", "")
        if org_stamp in by_team[team]["org_counts"]:
            by_team[team]["org_counts"][org_stamp] += 1
    return by_team


def compute_grades_by_range(team_data: Dict[str, Dict]) -> Dict[str, Dict]:
    """
    Assign grades based on points range: range = max(points) - min(points);
    bands from min to max map to letter grades per ``config/draft_grades.json``'s
    ``grade_bands`` block. Default config = five equal bands → F, D, C, B, A.
    Returns team -> {grade, rank}. Rank is still by points desc (best = 1).
    """
    if not team_data:
        return {}
    points_list = [data["points"] for data in team_data.values()]
    min_pts = min(points_list)
    max_pts = max(points_list)
    span = max_pts - min_pts

    # Sort by points desc for rank; ties get best rank in group
    sorted_teams = sorted(
        team_data.items(),
        key=lambda x: (-x[1]["points"], x[0]),
    )

    # Config-driven grade bands: list of {"position_max", "grade"}; sorted
    # ascending by position_max (load_grades_config normalizes this).
    bands_cfg = _GRADES_CFG.get("grade_bands") or []
    bands: List[Tuple[float, str]] = [
        (float(b.get("position_max", 1.0)), str(b.get("grade", "C")))
        for b in bands_cfg
    ]
    if not bands:
        bands = [(0.2, "F"), (0.4, "D"), (0.6, "C"), (0.8, "B"), (1.0, "A")]

    result: Dict[str, Dict] = {}
    prev_pts = None
    for i, (team, data) in enumerate(sorted_teams):
        pts = data["points"]
        if pts != prev_pts:
            rank = i + 1
        prev_pts = pts
        if span == 0:
            grade = bands[len(bands) // 2][1]  # middle band when no spread
        else:
            # Position within range: 0 = min, 1 = max.
            pos = (pts - min_pts) / span
            grade = bands[-1][1]  # default to top grade if pos == 1.0
            for cutoff, g in bands:
                grade = g
                if pos < cutoff:
                    break
        result[team] = {"grade": grade, "rank": rank}
    return result


def write_raw_csv(rows: List[Dict], path: Path, include_org: bool = False) -> None:
    """Write raw comparison data to CSV. Includes Org-* columns if include_org=True.

    v10 columns (Outlook / Reach / Career / Pers / Prone) ride with the
    market grading columns. Their park-adjusted org variants (Org Reach /
    Org Career / Org Pers / Org Prone) ride with the org grading columns.
    Note: Outlook has no Org variant because run_vos.py doesn't park-adjust
    Pot* ratings — only the heuristic batting/defense composites get the
    park multiplier.
    """
    if not rows:
        return
    fieldnames = [
        "Player Name", "Team", "Overall Pick",
        "Projection Rank", "Delta",
        "Outlook", "Reach", "Career", "Pers", "Prone",
        "Pick Grade", "Stamp Type", "Points", "VOS Stamp",
    ]
    if include_org:
        fieldnames += [
            "Org Projection", "Org Delta",
            "Org Reach", "Org Career", "Org Pers", "Org Prone",
            "Org Pick Grade", "Org Stamp Type", "Org Points", "Org VOS Stamp",
        ]
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def write_raw_md(rows: List[Dict], path: Path, include_org: bool = False) -> None:
    """Write stamped picks as a Markdown table. If include_org, append a second
    table of org-stamped picks (park-adjusted).

    v10 columns (Outlook / Reach / Career / Pers / Prone) are surfaced
    alongside Projection Rank + Delta so each pick has its full v10 context
    visible. For the park-adjusted org table, Org Reach / Career / Pers /
    Prone come from the per-team eval CSV (no Outlook — not park-adjusted).
    """

    def _stamped_table(
        title: str, stamped: List[Dict], proj_key: str,
        delta_key: str, stamp_key: str, pts_key: str,
        extra_cols: List[Tuple[str, str]],
    ) -> List[str]:
        """extra_cols: list of (display_label, row_key) appended between
        Delta and Stamp Type. Used to inject v10 columns per table variant."""
        fieldnames = (
            ["Player Name", "Team", "Overall Pick", "Projection Rank", "Delta"]
            + [label for label, _ in extra_cols]
            + ["Stamp Type", "Points"]
        )
        out = [f"## {title}", "",
               "| " + " | ".join(fieldnames) + " |",
               "| " + " | ".join("---" for _ in fieldnames) + " |"]
        for r in stamped:
            pts = r.get(pts_key, 0)
            try:
                pts_str = f"{float(pts):.2f}"
            except (TypeError, ValueError):
                pts_str = str(pts)
            cells = [
                str(r.get("Player Name", "")),
                str(r.get("Team", "")),
                str(r.get("Overall Pick", "")),
                str(r.get(proj_key, "")),
                str(r.get(delta_key, "")),
            ]
            cells.extend(str(r.get(key, "")) for _, key in extra_cols)
            cells.extend([str(r.get(stamp_key, "")), pts_str])
            out.append("| " + " | ".join(cells) + " |")
        out.append("")
        return out

    if not rows and not include_org:
        path.write_text("_No picks._\n", encoding="utf-8")
        return

    # Master MD has all 5 v10 cols (Outlook is master-only).
    market_extras: List[Tuple[str, str]] = [
        ("Outlook", "Outlook"),
        ("Reach", "Reach"),
        ("Career", "Career"),
        ("Pers", "Pers"),
        ("Prone", "Prone"),
    ]
    # Org variant: no Outlook (not park-adjusted in run_vos).
    org_extras: List[Tuple[str, str]] = [
        ("Reach", "Org Reach"),
        ("Career", "Org Career"),
        ("Pers", "Org Pers"),
        ("Prone", "Org Prone"),
    ]

    lines: List[str] = []
    if rows:
        lines.extend(_stamped_table(
            "Draft Grades — Raw (All Picks)",
            rows, "Projection Rank", "Delta", "Stamp Type", "Points",
            extra_cols=market_extras,
        ))
    else:
        lines.extend(["## Draft Grades — Raw (All Picks)", "", "_No picks._", ""])

    if include_org:
        if rows:
            lines.extend(_stamped_table(
                "Draft Grades — Raw (All Picks, Park-Adjusted)",
                rows, "Org Projection", "Org Delta", "Org Stamp Type", "Org Points",
                extra_cols=org_extras,
            ))
        else:
            lines.extend(["## Draft Grades — Raw (All Picks, Park-Adjusted)", "", "_No picks._", ""])

    path.write_text("\n".join(lines), encoding="utf-8")


def write_summary_md(team_data: Dict[str, Dict], path: Path, include_org: bool = False) -> None:
    """Write team summary as Markdown. With include_org=True, columns pair Market + Org metrics.

    Stamp-count columns are generated from the active config's projection
    tiers (plus Managed Risk). Under the v10 default config, that produces
    Top 25 / Top 100 / Later / Managed Risk columns. If the user trims the
    tier list (e.g., reverts to 2-tier), the columns shrink accordingly.
    """
    mkt_grades = compute_grades_by_range(team_data)
    org_grades = compute_grades_by_range({t: {"points": d["org_points"]} for t, d in team_data.items()}) if include_org else {}

    league_pts = sum(d["points"] for d in team_data.values())
    league_base = sum(d.get("base", 0.0) for d in team_data.values())
    league_ratio = (league_pts / league_base) if league_base > 0 else 0.0

    org_league_pts = sum(d.get("org_points", 0.0) for d in team_data.values()) if include_org else 0.0
    org_league_base = sum(d.get("org_base", 0.0) for d in team_data.values()) if include_org else 0.0
    org_league_ratio = (org_league_pts / org_league_base) if (include_org and org_league_base > 0) else 0.0

    def _vd(pts: float, base: float, ratio: float) -> str:
        if base <= 0 or ratio <= 0:
            return "—"
        return f"{((pts / base) / ratio * 100):.0f}"

    def _vsbase(pts: float, base: float) -> str:
        if base <= 0:
            return "—"
        return f"{(pts / base * 100):.0f}%"

    # Column labels for stamp counts come from the active stamp names.
    # Market columns are "<name> Stamps" (e.g., "Top 25 Stamps") except
    # for "Managed Risk" which keeps its short label. Org columns prefix
    # with "Org ".
    stamp_names = _all_stamp_names()

    def _mkt_label(name: str) -> str:
        return name if name == "Managed Risk" else f"{name} Stamps"

    def _org_label(name: str) -> str:
        return f"Org {name}" if name == "Managed Risk" else f"Org {name}"

    rows = []
    for team, data in team_data.items():
        mkt_info = mkt_grades.get(team, {})
        counts = data.get("counts") or {}
        row = {"Team": team}
        for name in stamp_names:
            row[_mkt_label(name)] = counts.get(name, 0)
        row.update({
            "Total Points": round(data["points"], 1),
            "Base": round(data["base"], 1),
            "vs Base": _vsbase(data["points"], data["base"]),
            "vDraft+": _vd(data["points"], data["base"], league_ratio),
            "Rank": mkt_info.get("rank", ""),
            "Grade": mkt_info.get("grade", "F"),
        })
        if include_org:
            org_info = org_grades.get(team, {})
            org_counts = data.get("org_counts") or {}
            for name in stamp_names:
                row[_org_label(name)] = org_counts.get(name, 0)
            row.update({
                "Org Points": round(data["org_points"], 1),
                "Org Base": round(data["org_base"], 1),
                "Org vs Base": _vsbase(data["org_points"], data["org_base"]),
                "Org vDraft+": _vd(data["org_points"], data["org_base"], org_league_ratio),
                "Org Rank": org_info.get("rank", ""),
                "Org Grade": org_info.get("grade", "F"),
            })
        rows.append(row)

    mkt_fields = (
        ["Team"]
        + [_mkt_label(n) for n in stamp_names]
        + ["Total Points", "Base", "vs Base", "vDraft+", "Rank", "Grade"]
    )
    org_fields = (
        ["Team"]
        + [_org_label(n) for n in stamp_names]
        + ["Org Points", "Org Base", "Org vs Base", "Org vDraft+", "Org Rank", "Org Grade"]
    )

    def _vd_sort_key(r, key):
        try:
            return (-int(r[key]), r["Team"])
        except (ValueError, TypeError):
            return (1, r["Team"])

    def _render_table(title: str, table_rows: List[Dict], fields: List[str]) -> List[str]:
        out = [f"## {title}", "",
               "| " + " | ".join(fields) + " |",
               "| " + " | ".join("---" for _ in fields) + " |"]
        for r in table_rows:
            out.append("| " + " | ".join(str(r.get(f, "")) for f in fields) + " |")
        out.append("")
        return out

    lines: List[str] = []
    rows_mkt_by_pts = sorted(rows, key=lambda r: (r["Rank"] or 999, r["Team"]))
    rows_mkt_by_vd = sorted(rows, key=lambda r: _vd_sort_key(r, "vDraft+"))
    lines.extend(_render_table("Draft Grades — Market (by Total Points)", rows_mkt_by_pts, mkt_fields))
    lines.extend(_render_table("Draft Grades — Market (by vDraft+)", rows_mkt_by_vd, mkt_fields))

    if include_org:
        rows_org_by_pts = sorted(rows, key=lambda r: (r["Org Rank"] or 999, r["Team"]))
        rows_org_by_vd = sorted(rows, key=lambda r: _vd_sort_key(r, "Org vDraft+"))
        lines.extend(_render_table("Draft Grades — Park-Adjusted (by Total Points)", rows_org_by_pts, org_fields))
        lines.extend(_render_table("Draft Grades — Park-Adjusted (by vDraft+)", rows_org_by_vd, org_fields))

    path.write_text("\n".join(lines), encoding="utf-8")


def load_slack_config(config_path: Optional[Path] = None, league: Optional[str] = None) -> Optional[Dict[str, str]]:
    """
    Load team -> Slack handle mapping from config JSON.
    If config_path is None, derive from league: config/{league}-gm-slack.json.
    Returns None if file is missing or invalid; caller can fall back to no Slack substitution.
    """
    if config_path is None:
        if not league:
            return None
        config_path = Path(__file__).resolve().parent / "config" / f"{league.strip().lower()}-gm-slack.json"
    if not config_path.exists():
        return None
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (json.JSONDecodeError, OSError):
        return None


def write_headlines_md(
    rows: List[Dict],
    name_to_pos: Dict[str, str],
    path: Path,
    team_to_slack: Optional[Dict[str, str]] = None,
    points_key: str = "Points",
    projection_key: str = "Projection Rank",
    title: str = "Draft Grades — Headlines",
    projection_label: str = "projected at",
) -> None:
    """
    Write a Markdown file with one headline per pick that earned points.
    Defaults to market (Points / Projection Rank). Pass points_key="Org Points"
    and projection_key="Org Projection" for the park-adjusted variant.
    """
    lines = [f"## {title}", ""]
    for r in rows:
        pts = float(r.get(points_key) or 0)
        if pts <= 0:
            continue
        name = (r.get("Player Name") or "").strip()
        team = (r.get("Team") or "").strip()
        overall = r.get("Overall Pick", "")
        projection = r.get(projection_key, "")
        norm_name = _normalize_name(name)
        pos = (name_to_pos.get(norm_name) or "").strip()
        lead = f"{pos} **{name}**" if pos else f"**{name}**"
        pts_str = f"{pts:.2f}"
        if team_to_slack and team and team in team_to_slack:
            slack_handle = team_to_slack[team]
            line = f"- {lead} — {projection_label} #{projection} overall — was drafted by the {team} at #{overall}. This adds **{pts_str}** to the final draft score of @{slack_handle}."
        else:
            line = f"- {lead} — {projection_label} #{projection} overall — was drafted at #{overall}. This adds **{pts_str}** to the final draft score of the {team}."
        lines.append(line)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def build_summary_rows(team_data: Dict[str, Dict], include_org: bool = False) -> List[Dict]:
    """Build per-team summary row dicts (Total Points, Base, vs Base, vDraft+,
    Rank, Grade — plus Org-* columns if include_org=True). Sorted by Rank asc.
    Used by both write_summary (CSV) and the PDF summary page.
    """
    mkt_grades = compute_grades_by_range(team_data)
    org_grades = compute_grades_by_range({t: {"points": d["org_points"]} for t, d in team_data.items()}) if include_org else {}

    league_pts = sum(d["points"] for d in team_data.values())
    league_base = sum(d.get("base", 0.0) for d in team_data.values())
    league_ratio = (league_pts / league_base) if league_base > 0 else 0.0

    org_league_pts = sum(d.get("org_points", 0.0) for d in team_data.values()) if include_org else 0.0
    org_league_base = sum(d.get("org_base", 0.0) for d in team_data.values()) if include_org else 0.0
    org_league_ratio = (org_league_pts / org_league_base) if (include_org and org_league_base > 0) else 0.0

    stamp_names = _all_stamp_names()

    def _mkt_label(name: str) -> str:
        return name if name == "Managed Risk" else f"{name} Stamps"

    def _org_label(name: str) -> str:
        return f"Org {name}"

    rows = []
    for team, data in team_data.items():
        mkt_info = mkt_grades.get(team, {})
        base = data["base"]; pts = data["points"]
        vs_base = f"{(pts / base * 100):.0f}%" if base > 0 else ""
        vdraft = f"{((pts / base) / league_ratio * 100):.0f}" if (base > 0 and league_ratio > 0) else ""
        counts = data.get("counts") or {}
        r = {"Team": team}
        for name in stamp_names:
            r[_mkt_label(name)] = counts.get(name, 0)
        r.update({
            "Total Points": round(pts, 1),
            "Base": round(base, 1),
            "vs Base": vs_base,
            "vDraft+": vdraft,
            "Rank": mkt_info.get("rank", ""),
            "Grade": mkt_info.get("grade", "F"),
        })
        if include_org:
            org_info = org_grades.get(team, {})
            obase = data["org_base"]; opts = data["org_points"]
            o_vs_base = f"{(opts / obase * 100):.0f}%" if obase > 0 else ""
            o_vdraft = f"{((opts / obase) / org_league_ratio * 100):.0f}" if (obase > 0 and org_league_ratio > 0) else ""
            org_counts = data.get("org_counts") or {}
            for name in stamp_names:
                r[_org_label(name)] = org_counts.get(name, 0)
            r.update({
                "Org Points": round(opts, 1),
                "Org Base": round(obase, 1),
                "Org vs Base": o_vs_base,
                "Org vDraft+": o_vdraft,
                "Org Rank": org_info.get("rank", ""),
                "Org Grade": org_info.get("grade", "F"),
            })
        rows.append(r)
    rows.sort(key=lambda r: (r["Rank"] or 999, r["Team"]))
    return rows


def write_summary(team_data: Dict[str, Dict], path: Path, include_org: bool = False) -> None:
    """Write team summary CSV. Adds Org-* columns when include_org=True."""
    rows = build_summary_rows(team_data, include_org=include_org)
    stamp_names = _all_stamp_names()

    def _mkt_label(name: str) -> str:
        return name if name == "Managed Risk" else f"{name} Stamps"

    def _org_label(name: str) -> str:
        return f"Org {name}"

    fieldnames = (
        ["Team"]
        + [_mkt_label(n) for n in stamp_names]
        + ["Total Points", "Base", "vs Base", "vDraft+", "Rank", "Grade"]
    )
    if include_org:
        fieldnames += (
            [_org_label(n) for n in stamp_names]
            + ["Org Points", "Org Base", "Org vs Base", "Org vDraft+", "Org Rank", "Org Grade"]
        )
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Grade draft results vs VOS draft pool; output raw CSV and team summary."
    )
    parser.add_argument(
        "directory",
        type=str,
        help=(
            "Directory containing draft analysis output (with 05_draft_pool.md). "
            "Accepts an absolute path, a path relative to cwd or to the script dir, "
            "or a draft folder name resolved under '{league}/drafts/' when --league is set."
        ),
    )
    parser.add_argument(
        "--num-teams",
        type=int,
        required=True,
        metavar="N",
        help="Number of teams in the draft (used for managed-risk tier: reach < N spots earns 0.75 pts)",
    )
    parser.add_argument(
        "--through-pick",
        type=int,
        default=None,
        metavar="N",
        help=(
            "Grade only through overall pick number N (inclusive). "
            "Example: --through-pick 50 grades picks with Overall <= 50."
        ),
    )
    parser.add_argument(
        "--league",
        type=str,
        default=None,
        help="League slug (e.g. woba, sahl) — used to look up draft API URL from config.",
    )
    parser.add_argument(
        "--league-url-config",
        type=Path,
        default=DEFAULT_LEAGUE_URL_CONFIG,
        help="JSON file with league->api_url mappings (default: config/league_url.json)",
    )
    parser.add_argument(
        "--grades-config",
        type=Path,
        default=DEFAULT_GRADES_CONFIG,
        help="Path to the grading config JSON (projection tiers, managed-risk + delta "
             "bonus parameters, grade bands). Default: config/draft_grades.json. "
             "Edit the JSON to retune POINTS_TOP_*/POINTS_LATER without code changes.",
    )
    parser.add_argument(
        "--api-url",
        type=str,
        default=None,
        help="Draft API URL override (e.g. https://host/league/api/draft/). Overrides --league config lookup.",
    )
    parser.add_argument(
        "--output-dir",
        type=str,
        default=None,
        help="Directory for output files (default: same as input directory)",
    )
    parser.add_argument(
        "--raw-name",
        type=str,
        default="draft_grades_raw.csv",
        help="Filename for raw comparison CSV",
    )
    parser.add_argument(
        "--summary-name",
        type=str,
        default="draft_grades_summary.csv",
        help="Filename for team summary CSV",
    )
    parser.add_argument(
        "--headlines-name",
        type=str,
        default="draft_grades_headlines.md",
        help="Filename for one-line headlines (picks that earned points)",
    )
    parser.add_argument(
        "--headlines-park-adj-name",
        type=str,
        default="draft_grades_headlines_park_adj.md",
        help="Filename for park-adjusted headlines (only written when --park-adjusted is set)",
    )
    parser.add_argument(
        "--exclude-team",
        type=str,
        default=None,
        metavar="NAME",
        help="Exclude this team from all calculations and output (as if it did not exist)",
    )
    parser.add_argument(
        "--slack-headlines",
        action="store_true",
        help="In headlines, use 'drafted by the {Team} at #N' and replace team at end with @Slack handle (from config/sahl-gm-slack.json)",
    )
    parser.add_argument(
        "--park-adjusted",
        action="store_true",
        help="Also grade picks against each team's park-adjusted board (from <league>/eval/<team_code>/draft_evaluation_*.csv). Adds Org-* columns and a second summary table.",
    )
    parser.add_argument(
        "--pdf",
        action="store_true",
        help="Also emit a styled per-pick PDF (requires reportlab). Saved alongside other outputs in the output directory.",
    )
    parser.add_argument(
        "--pdf-name",
        type=str,
        default="draft_grades.pdf",
        help="Filename for the market PDF (default: draft_grades.pdf)",
    )
    parser.add_argument(
        "--pdf-park-adj-name",
        type=str,
        default="draft_grades_park_adj.pdf",
        help="Filename for the park-adjusted PDF (only written when --pdf and --park-adjusted are both set)",
    )
    parser.add_argument(
        "--pdf-title",
        type=str,
        default=None,
        help="PDF title (default: '{LEAGUE} Draft Grades' or 'Draft Grades')",
    )
    parser.add_argument(
        "--pdf-subtitle",
        type=str,
        default=None,
        help="PDF subtitle (default: derived from --through-pick if set, else empty)",
    )
    parser.add_argument(
        "--pdf-max-picks",
        type=int,
        default=None,
        help="Limit PDF to picks with Overall <= this number (default: all picks)",
    )
    args = parser.parse_args()

    # Refresh module-level grading config from --grades-config (or default).
    # Done before any grade computation so _grade_pick / _base_for_projection
    # / aggregate_by_team all read the right tier definitions.
    _refresh_grades_cfg(args.grades_config)
    _active_tier_names = ", ".join(
        f"{t['name']} (rank ≤ {t['max_rank']})" if t.get("max_rank") is not None
        else f"{t['name']} (open)"
        for t in _GRADES_CFG["projection_tiers"]
    )
    print(f"Grading config: {args.grades_config}")
    print(f"  Active tiers: {_active_tier_names}")

    script_dir = Path(__file__).resolve().parent.parent
    try:
        directory = resolve_directory(args.directory, args.league, script_dir)
    except FileNotFoundError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    # Infer league from path if not explicitly provided (e.g. woba/drafts/...)
    inferred_league = args.league or infer_league_from_path(directory, script_dir)

    output_dir = Path(args.output_dir) if args.output_dir else directory

    try:
        pool_path = find_draft_pool_md(directory)
    except FileNotFoundError as e:
        print(str(e), file=sys.stderr)
        sys.exit(1)

    print(f"Loading projections from {pool_path}...")
    name_to_rank, name_to_pos, name_to_id, name_to_v10 = load_projections_from_md(pool_path)
    print(f"  Loaded {len(name_to_rank)} players (top {TOP_PROJECTION_CAP} eligible for VOS Stamp).")
    v10_count = sum(1 for v in name_to_v10.values() if v.get("Outlook"))
    if v10_count:
        print(f"  v10 columns detected (Outlook present for {v10_count}/{len(name_to_v10)} players).")

    api_url = args.api_url
    if not api_url:
        base = load_api_base_url(inferred_league, args.league_url_config)
        if base:
            api_url = base.rstrip("/") + "/draft/"
        else:
            print(
                "[ERROR] No --api-url provided and could not resolve league API URL. "
                "Provide --league (matching an entry in config/league_url.json) or --api-url.",
                file=sys.stderr,
            )
            sys.exit(1)

    print(f"Fetching draft status from {api_url}...")
    try:
        draft_rows = fetch_draft_csv(api_url)
    except Exception as e:
        print(f"Error fetching draft API: {e}", file=sys.stderr)
        sys.exit(1)
    print(f"  Fetched {len(draft_rows)} draft picks.")

    if args.through_pick is not None:
        if args.through_pick < 1:
            print("--through-pick must be >= 1 (1-based overall pick number).", file=sys.stderr)
            sys.exit(1)

        filtered = []
        for row in draft_rows:
            overall_raw = get_draft_value(row, "Overall")
            if overall_raw is None:
                continue
            try:
                overall = int(overall_raw)
            except ValueError:
                continue
            if overall <= args.through_pick:
                filtered.append(row)

        removed = len(draft_rows) - len(filtered)
        draft_rows = filtered
        print(f"  Filtering: kept {len(draft_rows)} picks through Overall #{args.through_pick} (removed {removed}).")

    team_boards = None
    team_v10: Optional[Dict[str, Dict[str, Dict[str, str]]]] = None
    if args.park_adjusted:
        if not inferred_league:
            print("Warning: --park-adjusted requires a league (pass --league or use a path under '{league}/drafts/'); skipping park-adjusted grading.", file=sys.stderr)
        else:
            draft_pool_ids = set(name_to_id.values())
            if not draft_pool_ids:
                print("Warning: --park-adjusted requires player IDs in the draft pool (none found); skipping park-adjusted grading.", file=sys.stderr)
            else:
                league_dir = script_dir / inferred_league
                boards, t_v10, missing = build_team_boards(inferred_league, league_dir, script_dir, draft_pool_ids)
                if not boards:
                    print(f"Warning: --park-adjusted requested but no draft_evaluation_*.csv files found under {league_dir / 'eval'}/<team_code>/; falling back to master board for all teams.", file=sys.stderr)
                    team_boards = {}
                    team_v10 = {}
                else:
                    team_boards = boards
                    team_v10 = t_v10
                    print(f"  Park-adjusted boards loaded for {len(boards)} team(s).")
                    if missing:
                        print(f"  Missing eval CSV for {len(missing)} team(s); will fall back to master board: {', '.join(missing)}", file=sys.stderr)
    rows = compare_draft_to_projections(
        draft_rows, name_to_rank, args.num_teams,
        team_boards=team_boards,
        name_to_v10=name_to_v10,
        team_v10=team_v10,
    )

    if args.exclude_team:
        exclude_name = args.exclude_team.strip()
        orig_len = len(rows)
        rows = [r for r in rows if (r.get("Team") or "").strip().lower() != exclude_name.lower()]
        n_removed = orig_len - len(rows)
        print(f"  Excluding team {exclude_name!r}: removed {n_removed} picks from consideration.")

    # Console stamp summary — read tier names from active config so a
    # different tier schema (e.g. Top 25/Top 100/Later under v10) prints
    # the right counts.
    stamp_names = _all_stamp_names()
    stamp_counts = {name: 0 for name in stamp_names}
    for r in rows:
        stamp = r.get("Stamp Type", "")
        if stamp in stamp_counts:
            stamp_counts[stamp] += 1
    total_pts = sum(float(r.get("Points") or 0) for r in rows)
    total_base = sum(_base_for_projection(r.get("Projection Rank")) for r in rows)
    pct_of_base = f"{(total_pts / total_base * 100):.0f}%" if total_base > 0 else "—"

    # Build a stamp-line that includes the base_points for each tier so the
    # console output stays informative across config changes.
    tier_by_name = {str(t.get("name", "")): t for t in _GRADES_CFG["projection_tiers"]}
    tier_by_name["Managed Risk"] = _GRADES_CFG["managed_risk"]
    stamp_parts = []
    for name in stamp_names:
        bp = float(tier_by_name.get(name, {}).get("base_points", 0.0))
        stamp_parts.append(f"{stamp_counts[name]} {name} ({bp:g} pts + log bonus)")
    print(f"  Stamps: {', '.join(stamp_parts)}. Total points: {total_pts:.1f}.")
    print(f"  Base (projection-meeting): {total_base:.1f}. Total vs Base: {pct_of_base}.")
    if team_boards is not None:
        org_total_pts = sum(float(r.get("Org Points") or 0) for r in rows)
        org_total_base = sum(_base_for_projection(r.get("Org Projection")) for r in rows)
        org_pct = f"{(org_total_pts / org_total_base * 100):.0f}%" if org_total_base > 0 else "—"
        print(f"  Park-adjusted total: {org_total_pts:.1f}. Base: {org_total_base:.1f}. Org vs Base: {org_pct}.")

    team_data = aggregate_by_team(rows)
    output_dir.mkdir(parents=True, exist_ok=True)
    raw_path = output_dir / args.raw_name
    summary_path = output_dir / args.summary_name
    headlines_path = output_dir / args.headlines_name

    team_to_slack = None
    if args.slack_headlines:
        if not inferred_league:
            print(
                "Warning: --slack-headlines requested but no league could be inferred "
                "(pass --league or use a path under '{league}/drafts/'); using plain team names.",
                file=sys.stderr,
            )
        else:
            slack_path = script_dir / "config" / f"{inferred_league}-gm-slack.json"
            team_to_slack = load_slack_config(slack_path)
            if not team_to_slack:
                msg = f"Warning: --slack-headlines requested but {slack_path.name} not found or invalid; using plain team names."
                print(msg, file=sys.stderr)
            else:
                print(f"  Using Slack handles from {slack_path.name} for headlines.")

    include_org = team_boards is not None
    write_raw_csv(rows, raw_path, include_org=include_org)
    write_summary(team_data, summary_path, include_org=include_org)
    write_headlines_md(rows, name_to_pos, headlines_path, team_to_slack=team_to_slack)
    headlines_park_adj_path: Optional[Path] = None
    if include_org:
        headlines_park_adj_path = output_dir / args.headlines_park_adj_name
        write_headlines_md(
            rows, name_to_pos, headlines_park_adj_path,
            team_to_slack=team_to_slack,
            points_key="Org Points",
            projection_key="Org Projection",
            title="Draft Grades — Headlines (Park-Adjusted)",
            projection_label="team-board projected at",
        )

    raw_md_path = raw_path.with_suffix(".md")
    write_raw_md(rows, raw_md_path, include_org=include_org)
    summary_md_path = summary_path.with_suffix(".md")
    write_summary_md(team_data, summary_md_path, include_org=include_org)

    pdf_path: Optional[Path] = None
    pdf_org_path: Optional[Path] = None
    if args.pdf:
        try:
            from draft_grades_pdf import (
                write_pdf,
                DEFAULT_COLUMNS,
                PARK_ADJ_COLUMNS,
                SUMMARY_COLUMNS,
                PARK_ADJ_SUMMARY_COLUMNS,
            )
        except ImportError as e:
            print(f"Warning: --pdf requested but import failed ({e}); skipping PDF. "
                  "Install with: pip install reportlab", file=sys.stderr)
        else:
            pdf_title = args.pdf_title or (
                f"{inferred_league.upper()} Draft Grades" if inferred_league else "Draft Grades"
            )
            if args.pdf_subtitle is not None:
                pdf_subtitle = args.pdf_subtitle
            elif args.through_pick:
                pdf_subtitle = f"Picks 1–{args.through_pick}"
            else:
                pdf_subtitle = ""

            summary_rows = build_summary_rows(team_data, include_org=include_org)
            league_label = inferred_league.upper() if inferred_league else "Draft"
            summary_title_mkt = f"{league_label} Team Grades"

            pdf_path = output_dir / args.pdf_name
            try:
                write_pdf(
                    rows, pdf_path,
                    title=pdf_title, subtitle=pdf_subtitle,
                    max_picks=args.pdf_max_picks, columns=DEFAULT_COLUMNS,
                    summary_rows=summary_rows,
                    summary_columns=SUMMARY_COLUMNS,
                    summary_title=summary_title_mkt,
                    summary_subtitle=pdf_subtitle,
                )
            except Exception as e:
                print(f"Warning: failed to write PDF: {e}", file=sys.stderr)
                pdf_path = None

            if include_org:
                pdf_org_path = output_dir / args.pdf_park_adj_name
                org_title = f"{pdf_title} (Park-Adjusted)"
                # Sort summary rows by Org Rank for the park-adjusted PDF.
                summary_rows_org = sorted(
                    summary_rows,
                    key=lambda r: (r.get("Org Rank") or 999, r.get("Team", "")),
                )
                try:
                    write_pdf(
                        rows, pdf_org_path,
                        title=org_title, subtitle=pdf_subtitle,
                        max_picks=args.pdf_max_picks, columns=PARK_ADJ_COLUMNS,
                        summary_rows=summary_rows_org,
                        summary_columns=PARK_ADJ_SUMMARY_COLUMNS,
                        summary_title=f"{summary_title_mkt} (Park-Adjusted)",
                        summary_subtitle=pdf_subtitle,
                    )
                except Exception as e:
                    print(f"Warning: failed to write park-adjusted PDF: {e}", file=sys.stderr)
                    pdf_org_path = None

    print(f"\nOutput written to {output_dir}:")
    print(f"  - {raw_path.name} (raw: delta, stamp type, points per pick)")
    print(f"  - {raw_md_path.name} (MD: stamped picks for Obsidian)")
    _stamp_summary_desc = " / ".join(_all_stamp_names()).lower()
    print(f"  - {summary_path.name} (teams, {_stamp_summary_desc}, total points, grade)")
    print(f"  - {summary_md_path.name} (MD: team summary for Obsidian)")
    print(f"  - {headlines_path.name} (one-line headlines for picks that earned points)")
    if headlines_park_adj_path is not None:
        print(f"  - {headlines_park_adj_path.name} (park-adjusted headlines)")
    if pdf_path is not None:
        print(f"  - {pdf_path.name} (styled per-pick PDF)")
    if pdf_org_path is not None:
        print(f"  - {pdf_org_path.name} (styled per-pick PDF, park-adjusted)")


if __name__ == "__main__":
    main()
