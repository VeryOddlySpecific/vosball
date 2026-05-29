#!/usr/bin/env python3
"""
run_vos.py — VOS v5/v6 scoring engine and standalone CLI.

The refactored successor to scripts/vos_v2.py. v2 stays in place for active
deployments running v2/v3 weights; this file is the home for the v5/v6
schemas and any future iterations.

Produces three normalized 20-80 scores per player:
    VOS_Reach    Predicts P(reach MLB). Two implementations:
                  - v5 heuristic (Pot*-weighted composite) — used when the
                    weights JSON has scoring_modes.vos_reach
                  - v6 logistic (trained model with current+Pot* features
                    + age) — used when scoring_modes.vos_reach_v6 is
                    present; takes precedence over v5
    VOS_Career   Current-rating score with age-decay predicting WAR | MLB.
    VOS_Blended  alpha * Reach + (1 - alpha) * Career.

Reads PlayerData CSV + a weights JSON. Writes
evaluation_summary_{league}_{timestamp}.csv plus a companion Markdown.

Schema sanity is checked at load time — feeding this engine a v2 or v3
weights file will fail loudly rather than silently producing nonsense.
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import math
import sys
from io import StringIO
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple
from urllib.error import URLError
from urllib.request import urlopen

SCRIPT_DIR = Path(__file__).resolve().parent

# Locate the directory containing lib/. Source layout has lib/ as a sibling
# of scripts/ (so SCRIPT_DIR.parent); deployed-alongside layout has lib/ as
# a sibling of run_vos.py itself (so SCRIPT_DIR).
for _lib_root in (SCRIPT_DIR, SCRIPT_DIR.parent):
    if (_lib_root / "lib").is_dir():
        if str(_lib_root) not in sys.path:
            sys.path.insert(0, str(_lib_root))
        break

from lib.vos_decay import apply_age_decay  # noqa: E402

# --- Phase 1 refactor: the VOS engine is being extracted into the vosball
# package. Moved names are re-imported here so existing importers
# (`import run_vos as v2`, lib/draft_score.py) and the code still living in this
# module keep resolving them unchanged. Output is unchanged — guarded by
# tests/test_golden.py.
from vosball.engine import (  # noqa: E402
    normalize_to_20_80,
    _normalization_params,
    classify_vos_tier,
    tier_for_player_role,
    _resolve_tier_bands,
    resolve_float,
    resolve_int,
)
# The remaining migrated engine surface (the ~40 scoring/assembly functions and
# the engine constant tables) is re-exported so `import run_vos as v2` and
# `import run_vos` callers resolve every name they used pre-refactor.
from vosball.engine.core import *        # noqa: E402,F401,F403
from vosball.engine.constants import *   # noqa: E402,F401,F403

# -----------------------------------------------------------------------------
# Paths and constants
# -----------------------------------------------------------------------------

DEFAULT_DATA_DIR = SCRIPT_DIR / "data"
DEFAULT_CONFIG_DIR = SCRIPT_DIR / "config"
WEIGHTS_FILENAME = "weights_v10.json"
ID_MAPS_FILENAME = "id_maps.json"
LEAGUE_URLS_FILENAME = "league_url.json"
TEAMS_FILENAME_TEMPLATE = "teams-{league}.json"
PLAYER_DATA_FILENAME_TEMPLATE = "PlayerData-{league}.csv"

DEFAULT_LEAGUE_API_BASE_URLS: Dict[str, str] = {}

# Engine constant tables (pitch-type maps, personality/injury encodings,
# position lists, CSV column alternatives) -> vosball/engine/constants.py.

# Rating-scale conversion (ported from vos_v2.py) ----------------------------
# weights_v6.json (cutoffs, hard floors/ceilings, age-decay slopes, learned
# logistic coefficients) all assume the 20-80 scale. Some leagues export
# component ratings on a 1-100 scale instead. When --rating-scale 1-100 is
# set we convert each rating cell down to 20-80 at load time so the rest of
# the pipeline (and the output) stays on the familiar 20-80 scale. OVR/POT,
# ages, IDs and personality cells are NOT converted.
RATING_SCALES = ("20-80", "1-100")
DEFAULT_RATING_SCALE = "20-80"

# Component-rating column names (CSV). These are the only cells that get
# rescaled when --rating-scale 1-100 is active.
RATING_COLUMNS: Set[str] = {
    # Hitter batting (current + potential)
    "Cntct", "Gap", "Pow", "Eye", "Ks",
    "PotCntct", "PotGap", "PotPow", "PotEye", "PotKs",
    # Hitter defense
    "CArm", "CFrm", "CBlk",
    "IFR", "IFA", "IFE", "TDP",
    "OFR", "OFA", "OFE",
    # Hitter baserunning
    "Speed", "Run", "StealAbi", "Steal", "StlRt",
    # Pitcher ability (current + potential)
    "Stf", "Mov", "Ctrl", "Ctrl_R", "Ctrl_L", "HRA",
    "PotStf", "PotMov", "PotCtrl", "PotHRA",
    # Pitcher pitch ratings (current)
    "Fst", "Snk", "Cutt", "Crv", "Sld", "Chg",
    "Splt", "Frk", "CirChg", "Scr", "Kncrv", "Knbl",
    # Pitcher pitch ratings (potential)
    "PotFst", "PotSnk", "PotCutt", "PotCrv", "PotSld", "PotChg",
    "PotSplt", "PotFrk", "PotCirChg", "PotScr", "PotKncrv", "PotKnbl",
    # Stamina (compared against 20-80 cutoffs in weights)
    "Stm",
}


def convert_1_100_to_20_80(value: float) -> float:
    """Linear remap: 1->20, 50->50.30, 100->80. Inverse of standard scout-grade scaling."""
    return (value - 1.0) * (60.0 / 99.0) + 20.0


def apply_rating_scale_to_row(row: Dict[str, str], scale: str) -> None:
    """In-place: convert component-rating cells from 1-100 to 20-80. No-op if scale='20-80'."""
    if scale == DEFAULT_RATING_SCALE:
        return
    if scale != "1-100":
        raise ValueError(f"Unknown rating scale: {scale!r} (expected one of {RATING_SCALES})")
    for col in RATING_COLUMNS:
        raw = row.get(col)
        if raw is None:
            continue
        s = raw.strip()
        if s == "" or s.upper() in ("NA", "N/A", "."):
            continue
        try:
            v = float(s)
        except (TypeError, ValueError):
            continue
        row[col] = f"{convert_1_100_to_20_80(v):.4f}"

CONTRACT_FIELDS = [
    "player_id", "team_id", "league_id", "is_major", "no_trade",
    "last_year_team_option", "last_year_player_option", "last_year_vesting_option",
    "next_last_year_team_option", "next_last_year_player_option", "next_last_year_vesting_option",
    "contract_team_id", "contract_league_id", "season_year",
    "salary0", "salary1", "salary2", "salary3", "salary4", "salary5", "salary6", "salary7",
    "salary8", "salary9", "salary10", "salary11", "salary12", "salary13", "salary14",
    "years", "current_year", "minimum_pa", "minimum_pa_bonus", "minimum_ip", "minimum_ip_bonus",
    "mvp_bonus", "cyyoung_bonus", "allstar_bonus", "next_last_year_option_buyout", "last_year_option_buyout",
]

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)


# -----------------------------------------------------------------------------
# Data loading
# -----------------------------------------------------------------------------

def load_json(path: Path) -> Dict[str, Any]:
    if not path.exists():
        logger.warning("Config not found: %s", path)
        return {}
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def _validate_v5_schema(cfg: Dict[str, Any]) -> None:
    """Fail loudly if cfg isn't a v5/v6 weights file.

    v5/v6 is identified by the top-level scoring_modes block. Career mode
    must be present. For Reach, either the v5 heuristic block (vos_reach)
    or the v6 logistic model block (vos_reach_v6) must be present — v6
    takes precedence when both exist.
    """
    if not isinstance(cfg.get("scoring_modes"), dict):
        raise ValueError(
            "Weights file is not v5/v6: missing top-level 'scoring_modes' "
            "block. run_vos.py only accepts v5/v6 weights. Use "
            "scripts/vos_v2.py for v2/v3 weights."
        )
    modes = cfg["scoring_modes"]
    if not isinstance(modes.get("vos_career"), dict):
        raise ValueError("Weights file missing scoring_modes.vos_career")
    has_v5_reach = isinstance(modes.get("vos_reach"), dict)
    has_v6_reach = isinstance(modes.get("vos_reach_v6"), dict)
    if not (has_v5_reach or has_v6_reach):
        raise ValueError(
            "Weights file must have either scoring_modes.vos_reach "
            "(v5 heuristic) or scoring_modes.vos_reach_v6 (v6 logistic)."
        )
    if not isinstance(cfg.get("age_decay"), dict):
        raise ValueError("Weights file missing top-level 'age_decay' block.")


def load_weights(config_dir: Path,
                 weights_path: Optional[Path] = None) -> Dict[str, Any]:
    """Load a v5 weights JSON. Defaults to config_dir/weights_v5_draft.json.
    Pass weights_path (absolute, or relative to cwd or config_dir) to load
    any other v5 file (e.g. a tuned weights_v5.json post-validation)."""
    if weights_path is not None:
        p = Path(weights_path)
        if not p.is_absolute() and not p.exists():
            p = config_dir / weights_path
        cfg = load_json(p)
    else:
        cfg = load_json(config_dir / WEIGHTS_FILENAME)
    if cfg:
        _validate_v5_schema(cfg)
    return cfg


def load_id_maps(config_dir: Path) -> Dict[int, str]:
    raw = load_json(config_dir / ID_MAPS_FILENAME)
    level_map = raw.get("league_level") or raw.get("league_levels")
    if not isinstance(level_map, dict):
        return {}
    lookup: Dict[int, str] = {}
    for label, value in level_map.items():
        if label.startswith("_"):
            continue
        try:
            lookup[int(value)] = str(label)
        except (TypeError, ValueError):
            continue
    return lookup


def load_teams(config_dir: Path, league: str) -> Dict[int, str]:
    path = config_dir / TEAMS_FILENAME_TEMPLATE.format(league=league)
    raw = load_json(path)
    if not isinstance(raw, dict):
        return {}
    result: Dict[int, str] = {}
    for tid_str, info in raw.items():
        if tid_str.startswith("_") or not isinstance(info, dict):
            continue
        try:
            tid = int(tid_str)
        except (TypeError, ValueError):
            continue
        name = info.get("Name") or ""
        nick = info.get("Nickname") or ""
        result[tid] = f"{name} {nick}".strip() or f"Team {tid}"
    return result


def load_league_api_base_urls(config_dir: Path) -> Dict[str, str]:
    path = config_dir / LEAGUE_URLS_FILENAME
    raw = load_json(path)
    if not isinstance(raw, dict):
        logger.warning("league_url.json missing or invalid at %s", path)
        return {}
    return {str(k).strip().lower(): str(v).strip().rstrip("/")
            for k, v in raw.items() if k and v}


# resolve_float, resolve_int -> vosball/engine/rows.py (imported at module top).


def load_player_data(data_dir: Path, league: str,
                     id_filter: Optional[Set[str]] = None,
                     rating_scale: str = DEFAULT_RATING_SCALE) -> List[Dict[str, str]]:
    """When rating_scale='1-100', component-rating cells are converted down to
    the 20-80 scale at load time so all downstream cutoffs/weights apply
    unchanged."""
    if rating_scale not in RATING_SCALES:
        raise ValueError(f"rating_scale must be one of {RATING_SCALES}, got {rating_scale!r}")
    path = data_dir / PLAYER_DATA_FILENAME_TEMPLATE.format(league=league)
    if not path.exists():
        logger.error("Player data not found: %s", path)
        return []
    rows: List[Dict[str, str]] = []
    with path.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames or "ID" not in reader.fieldnames:
            logger.error("CSV missing ID column")
            return []
        for row in reader:
            pid = (row.get("ID") or "").strip()
            if not pid:
                continue
            if id_filter is not None and pid not in id_filter:
                continue
            apply_rating_scale_to_row(row, rating_scale)
            rows.append(row)
    if rating_scale != DEFAULT_RATING_SCALE:
        logger.info("Converted component ratings from %s -> 20-80 for %d players",
                    rating_scale, len(rows))
    logger.info("Loaded %d players from %s", len(rows), path.name)
    return rows


def load_id_filter(file_path: Optional[Path]) -> Optional[Set[str]]:
    if file_path is None:
        return None
    if not file_path.exists():
        raise FileNotFoundError(f"--ids-file not found: {file_path}")
    ids: Set[str] = set()
    with file_path.open("r", encoding="utf-8") as f:
        for line in f:
            for sep in (",", ";", "\t", " "):
                line = line.replace(sep, " ")
            for token in line.split():
                t = token.strip()
                if t:
                    ids.add(t)
    if not ids:
        raise ValueError(f"--ids-file contained no IDs: {file_path}")
    return ids


def get_league_base_url(
    league: str,
    base_url_override: Optional[str] = None,
    league_api_base_urls: Optional[Dict[str, str]] = None,
) -> Optional[str]:
    if base_url_override:
        return base_url_override.rstrip("/")
    lookup = league_api_base_urls if league_api_base_urls is not None else DEFAULT_LEAGUE_API_BASE_URLS
    return lookup.get((league or "").strip().lower())


def _fetch_csv_endpoint(url: str) -> List[Dict[str, str]]:
    with urlopen(url, timeout=30) as resp:
        content_type = (resp.headers.get("Content-Type") or "").lower()
        payload = resp.read().decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(StringIO(payload))
    if not reader.fieldnames:
        if "json" in content_type:
            logger.warning("Endpoint returned JSON instead of CSV: %s", url)
        raise ValueError(f"No CSV header found at endpoint: {url}")
    return [r for r in reader if isinstance(r, dict)]


def _season_year_value(row: Dict[str, str]) -> int:
    try:
        return int(float((row.get("season_year") or "").strip()))
    except (TypeError, ValueError):
        return -1


def _build_contract_lookup(rows: List[Dict[str, str]],
                           id_filter: Optional[Set[str]] = None) -> Dict[str, Dict[str, str]]:
    out: Dict[str, Dict[str, str]] = {}
    for r in rows:
        pid = (r.get("player_id") or "").strip()
        if not pid:
            continue
        if id_filter is not None and pid not in id_filter:
            continue
        prev = out.get(pid)
        if prev is None or _season_year_value(r) >= _season_year_value(prev):
            out[pid] = r
    return out


def load_contract_data(
    base_url: str,
    id_filter: Optional[Set[str]] = None,
) -> Tuple[Dict[str, Dict[str, str]], Dict[str, Dict[str, str]]]:
    contract_url = f"{base_url.rstrip('/')}/contract"
    extension_url = f"{base_url.rstrip('/')}/contractextension"
    contract_rows = _fetch_csv_endpoint(contract_url)
    extension_rows = _fetch_csv_endpoint(extension_url)
    contract_lookup = _build_contract_lookup(contract_rows, id_filter)
    extension_lookup = _build_contract_lookup(extension_rows, id_filter)
    logger.info("Loaded %d /contract rows, %d /contractextension rows",
                len(contract_rows), len(extension_rows))
    logger.info("Built %d contract entries, %d extension entries",
                len(contract_lookup), len(extension_lookup))
    return contract_lookup, extension_lookup


def attach_contract_fields(
    out_row: Dict[str, Any],
    contract_row: Optional[Dict[str, str]],
    extension_row: Optional[Dict[str, str]],
) -> None:
    for field in CONTRACT_FIELDS:
        out_row[f"Contract_{field}"] = (contract_row or {}).get(field, "")
        out_row[f"ContractExtension_{field}"] = (extension_row or {}).get(field, "")


# -----------------------------------------------------------------------------
# Park factors (optional)
# -----------------------------------------------------------------------------

def load_park_factors(path: Optional[str]) -> Optional[Dict[str, Any]]:
    if not path:
        return None
    path_obj = Path(path)
    if not path_obj.exists():
        logger.warning("Park factors file not found: %s", path)
        return None
    try:
        with path_obj.open("r", encoding="utf-8") as f:
            data = json.load(f)
        logger.info("Loaded park factors from %s", path)
        return data
    except json.JSONDecodeError as e:
        logger.error("Invalid JSON in park factors file: %s", e)
        return None










# -----------------------------------------------------------------------------
# Normalization (20-80 sigmoid)
#   normalize_to_20_80, _normalization_params -> vosball/engine/normalization.py
#   (imported at module top).
# -----------------------------------------------------------------------------


# -----------------------------------------------------------------------------
# VOS tier classification
#   classify_vos_tier, tier_for_player_role, _resolve_tier_bands and the default
#   tier bands -> vosball/engine/tiers.py (imported at module top).
# -----------------------------------------------------------------------------


# -----------------------------------------------------------------------------
# League / team labels
# -----------------------------------------------------------------------------







# -----------------------------------------------------------------------------
# Mode helpers
# -----------------------------------------------------------------------------











# -----------------------------------------------------------------------------
# v6 Reach — trained logistic regression model
# -----------------------------------------------------------------------------
# The v6 Reach block lives at scoring_modes.vos_reach_v6 in the weights JSON.
# Three models — hitter_model, sp_model, rp_model — each with:
#   features:  list of feature names in fixed order
#   means:     per-feature mean (for standardization)
#   stds:      per-feature std (for standardization)
#   medians:   per-feature median (used to impute missing values)
#   coefs:     per-feature logistic coefficient
#   intercept: model intercept
#
# Scoring math: z_i = (x_i - mean_i) / std_i;
#               logit = intercept + sum(coefs_i * z_i);
#               p = 1 / (1 + exp(-logit));
#               VOS_Reach = 20 + 60 * p.
# No further adjustments (age_adj, dev_adj, etc.) — the model already
# incorporates age, current ratings, defense, and position. Applying the
# v5-style adjustment stack on top would double-count.







# -----------------------------------------------------------------------------
# Hitter scoring
# -----------------------------------------------------------------------------











# -----------------------------------------------------------------------------
# Pitcher scoring
# -----------------------------------------------------------------------------







# -----------------------------------------------------------------------------
# Adjustments (carried forward from v2/v3, all read from cfg.adjustments.*)
# -----------------------------------------------------------------------------



















# -----------------------------------------------------------------------------
# Output row building
# -----------------------------------------------------------------------------

















# -----------------------------------------------------------------------------
# Output writers
# -----------------------------------------------------------------------------

def write_output_csv(
    rows: List[Dict[str, Any]],
    path: Path,
    draft_mode: bool = False,
    include_contracts: bool = False,
) -> None:
    if not rows:
        logger.warning("No rows to write")
        return
    cols = [
        "ID", "Name", "Pos", "Age", "Team", "Org", "League_Level",
        "VOS_Reach", "VOS_Career", "VOS_Blended", "VOS_Ceiling", "Ceiling_Tier",
        "Arch_Career_WAR", "Arch_Career_WAR_Hi",
        "Remaining_WAR", "Remaining_WAR_Hi", "Proj_Debut_Age",
        "VOS_Score", "VOS_Potential", "VOS_Tier", "VOS_Potential_Tier",
        "Batting_Score", "Batting_Potential", "Defense_Score", "Baserunning_Score",
        "Pitching_Ability_Score", "Pitching_Ability_Potential", "Pitching_Arsenal_Score",
        "Development_Adj", "Age_Adj", "Personality_Adj",
        "Park_Name", "Park_Applied", "Prone",
        "BABIP", "PotBABIP", "PBABIP", "PotPBABIP",
    ]
    if draft_mode:
        cols.insert(cols.index("Development_Adj") + 1, "Readiness_Adj")
        cols.insert(cols.index("Personality_Adj") + 1, "Draft_Age_Adj")
        cols.insert(cols.index("Draft_Age_Adj") + 1, "Draft_RP_Penalty")
    pos_cols = [f"{p}_Score" for p in HITTER_POSITIONS]
    pos_pot_cols = [f"{p}_Potential" for p in HITTER_POSITIONS]
    cols += pos_cols + pos_pot_cols
    cols += [
        "Projected_Top_Score", "Projected_Second_Score", "Projected_Margin",
        "Projected_Margin_Tier", "Projected_Viable_Positions", "Projected_Viable_Pos_List",
    ]
    cols += ["Current_Position", "Projected_Position", "Ideal_Value"]
    if include_contracts:
        cols += [f"Contract_{f}" for f in CONTRACT_FIELDS]
        cols += [f"ContractExtension_{f}" for f in CONTRACT_FIELDS]
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def _write_eval_summary_md(rows: List[Dict[str, Any]], path: Path, league: str) -> None:
    """Markdown summary for Obsidian. Shows all three v5 scores side by side."""
    md_cols = ["Name", "Pos", "Age", "Team", "Org", "League_Level",
               "VOS_Reach", "VOS_Career", "VOS_Blended"]

    def _row(r: Dict[str, Any]) -> str:
        cells = [str(r.get(c, "")) for c in md_cols]
        return "| " + " | ".join(cells) + " |"

    header = "| " + " | ".join(md_cols) + " |"
    sep = "| " + " | ".join("---" for _ in md_cols) + " |"

    mlb_rows = sorted(
        [r for r in rows if str(r.get("League_Level", "")).strip().upper() in ("MLB", "AAA")],
        key=lambda r: float(r.get("VOS_Career") or 0),
        reverse=True,
    )[:50]
    prospect_rows = sorted(
        [r for r in rows if str(r.get("League_Level", "")).strip().upper() not in ("MLB",)],
        key=lambda r: float(r.get("VOS_Reach") or 0),
        reverse=True,
    )[:75]

    lines: List[str] = [
        f"# Evaluation Summary — {league.upper()}  (v5)",
        "",
        f"_Generated from `{path.name.replace('.md', '.csv')}`._",
        "",
        "## Top MLB/AAA Players by VOS Career",
        "",
        header, sep,
    ]
    lines += [_row(r) for r in mlb_rows]
    lines += [
        "",
        "## Top Prospects by VOS Reach",
        "",
        header, sep,
    ]
    lines += [_row(r) for r in prospect_rows]
    lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")
    logger.info("Wrote evaluation summary MD: %s", path)


# -----------------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="VOS v5: two-track (Reach + Career + Blended) player evaluation.")
    parser.add_argument("--league", required=True, help="League slug (e.g. woba, sky)")
    parser.add_argument("--output", default=None, help="Output CSV path (default: evaluation_summary_{league}_{timestamp}.csv)")
    parser.add_argument("--ids-file", default=None, type=Path, help="Optional file of player IDs to include")
    parser.add_argument("--park-factors", default=None, type=str, help="Optional path to park-factors.json")
    parser.add_argument("--draft", action="store_true", help="Enable draft-specific adjustments (readiness, draft_age, draft_role)")
    parser.add_argument("--contracts", action="store_true", help="Include contract and contractextension API data in output")
    parser.add_argument("--base-url", default=None, type=str, help="Override league API base URL")
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR, help="Data directory")
    parser.add_argument("--config-dir", type=Path, default=DEFAULT_CONFIG_DIR, help="Config directory")
    parser.add_argument("--weights", type=Path, default=None,
                        help=f"Path to v5 weights JSON. Defaults to {{config-dir}}/{WEIGHTS_FILENAME}.")
    parser.add_argument(
        "--per-org-evals",
        action="store_true",
        help=(
            "When the park-factors file is in combined teams[] format, write one eval per team "
            "into {league}/eval/{team_code}/. Each per-team eval grades the WHOLE league through "
            "that team's park context (single-park mode). Useful for sharing team-specific evals."
        ),
    )
    parser.add_argument(
        "--rating-scale",
        choices=list(RATING_SCALES),
        default=DEFAULT_RATING_SCALE,
        help=(
            "Scale of the component ratings in PlayerData-{league}.csv. Default '20-80' "
            "matches weights_v6.json. Use '1-100' for leagues that export component ratings "
            "(Cntct/Gap/Pow/.../Stf/Mov/.../pitch ratings/Stm) on a 1-100 scale; values are "
            "linearly remapped to 20-80 at load time so cutoffs, hard floors and the output "
            "scale stay unchanged. OVR/POT, ages, IDs and personality cells are not converted."
        ),
    )
    args = parser.parse_args()

    config_dir = args.config_dir
    data_dir = args.data_dir
    league = args.league.strip()
    try:
        id_filter = load_id_filter(args.ids_file)
    except (FileNotFoundError, ValueError) as e:
        logger.error("%s", e)
        return 1

    try:
        cfg = load_weights(config_dir, args.weights)
    except ValueError as e:
        logger.error("%s", e)
        return 1
    if not cfg:
        weights_label = args.weights or (config_dir / WEIGHTS_FILENAME)
        logger.error("Weights config missing or invalid: %s", weights_label)
        return 1
    weights_used = args.weights if args.weights else (config_dir / WEIGHTS_FILENAME)
    logger.info("Using v5 weights file: %s", weights_used)

    league_lookup = load_id_maps(config_dir)
    teams = load_teams(config_dir, league)
    league_api_base_urls = load_league_api_base_urls(config_dir)
    park_factors = load_park_factors(args.park_factors)
    players = load_player_data(data_dir, league, id_filter,
                               rating_scale=args.rating_scale)
    if not players:
        logger.error("No players loaded.")
        return 1

    contract_lookup: Dict[str, Dict[str, str]] = {}
    extension_lookup: Dict[str, Dict[str, str]] = {}
    include_contracts = bool(args.contracts)
    if include_contracts:
        base_url = get_league_base_url(league, args.base_url, league_api_base_urls)
        if not base_url:
            logger.error(
                "No base URL found for league '%s'. Add it to %s or pass --base-url.",
                league, config_dir / LEAGUE_URLS_FILENAME,
            )
            return 1
        try:
            contract_lookup, extension_lookup = load_contract_data(base_url, id_filter)
        except (URLError, TimeoutError, ValueError) as e:
            logger.error("Failed to load contract endpoints from %s: %s", base_url, e)
            return 1

    from datetime import datetime
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    draft_mode = args.draft
    # Match vos_v2 naming: draft runs get a different filename prefix so
    # consumers can tell at a glance which CSV they're looking at.
    out_prefix = "draft_evaluation" if draft_mode else "evaluation_summary"

    def _run_eval_pass(pass_park_factors: Optional[Dict[str, Any]],
                       out_path: Path) -> None:
        """Score every player against pass_park_factors and write the CSV/MD.
        Factored out so --per-org-evals can call it once per team-park."""
        out_path.parent.mkdir(parents=True, exist_ok=True)
        rows: List[Dict[str, Any]] = []
        for row in players:
            if is_pitcher(row):
                pos = (row.get("Pos") or "").strip().upper()
                role = "RP" if pos in ("RP", "CL") else "SP"
                out_row = build_pitcher_row(
                    row, cfg, league_lookup, teams,
                    role=role, park_factors=pass_park_factors, draft_mode=draft_mode,
                )
            else:
                out_row = build_hitter_row(
                    row, cfg, league_lookup, teams,
                    park_factors=pass_park_factors, draft_mode=draft_mode,
                )
            if out_row is not None:
                rows.append(out_row)
            else:
                logger.debug("Skipped row ID %s", row.get("ID"))
            if out_row is not None and include_contracts:
                pid = str(out_row.get("ID", "")).strip()
                attach_contract_fields(out_row, contract_lookup.get(pid),
                                       extension_lookup.get(pid))

        write_output_csv(rows, out_path, draft_mode=draft_mode,
                         include_contracts=include_contracts)
        logger.info("Wrote %d rows to %s", len(rows), out_path)
        md_path = out_path.with_suffix(".md")
        _write_eval_summary_md(rows, md_path, league)

        # Sanity range checks on all three scores.
        for col in ("VOS_Reach", "VOS_Career", "VOS_Blended"):
            vals = [r[col] for r in rows if isinstance(r.get(col), (int, float))]
            if not vals:
                continue
            lo, hi = min(vals), max(vals)
            if lo < 20 or hi > 80:
                logger.warning("%s range [%.2f, %.2f] outside 20-80", col, lo, hi)
            else:
                logger.info("%s range [%.2f, %.2f] (within 20-80)", col, lo, hi)

    # --per-org-evals: one eval per team in the combined teams[] block, each
    # treating the whole league as if every player batted in that team's park.
    if args.per_org_evals:
        teams_block = (park_factors or {}).get("teams") if isinstance(park_factors, dict) else None
        if not isinstance(teams_block, dict) or not teams_block:
            logger.error(
                "--per-org-evals requires a park-factors file in combined teams[] format. "
                "Either omit the flag or point --park-factors at a file with a top-level 'teams' object."
            )
            return 1

        app_rules = (park_factors or {}).get("application_rules") or {}
        for team_name, team_block in teams_block.items():
            if team_name.startswith("_") or not isinstance(team_block, dict):
                continue
            info = team_block.get("team_info") or {}
            team_code = (info.get("team_code") or "").strip().lower()
            if not team_code:
                # Fall back to slugified team_name if no team_code present.
                team_code = team_name.strip().lower().replace(" ", "_")
            # Synthesize a single-park view: tool_adjustments at root triggers
            # _is_single_park_format -> applies this team's park to everyone.
            synth_pf: Dict[str, Any] = {
                "tool_adjustments": team_block.get("tool_adjustments") or {},
                "handedness_splits": team_block.get("handedness_splits") or {},
                "team_info": info,
                "application_rules": app_rules,
            }
            out_path = (
                SCRIPT_DIR / league / "eval" / team_code
                / f"{out_prefix}_{league}_{ts}.csv"
            )
            logger.info("=" * 60)
            logger.info("Per-org eval: %s (%s)", team_name, team_code)
            _run_eval_pass(synth_pf, out_path)
        return 0

    # Default: single eval pass with the park-factors as loaded.
    out_path = args.output
    if out_path is None:
        out_path = SCRIPT_DIR / league / "eval" / f"{out_prefix}_{league}_{ts}.csv"
    else:
        out_path = Path(out_path)
    _run_eval_pass(park_factors, out_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
