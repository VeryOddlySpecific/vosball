#!/usr/bin/env python3
"""
Draft Pool Analysis — v10 refactor.

Generates a comprehensive analysis package from a v10 eval CSV (preferring
``draft_evaluation_{league}_*.csv`` when present, falling back to
``evaluation_summary_{league}_*.csv``). Produces 6+ reports for podcast,
article, or post-draft analysis:

  - 00_summary.txt
  - 01_position_distribution.txt
  - 02_position_strength.txt
  - 03_ideal_value_distribution.txt  (renamed from 03_vos_potential_*)
  - 04_prospect_tiers.txt
  - 05_draft_pool.md                 (canonical downstream contract)
  - summary_data.csv / summary_data.md

v10 changes
-----------
- ``Ideal_Value`` is preserved as the primary sort key + the MD column name
  that ``draft_board`` and ``draft_grades`` parse downstream.
- New v10 columns surface in the MD: ``Outlook`` (Career-weights × Pot*
  composite via ``lib/draft_score.py``), ``Reach`` (VOS_Reach from eval),
  ``Career``, ``Blend``, ``Pers`` (Personality_Adj), ``Prone``, ``Ready``
  (Readiness_Adj), ``Outlook Pos`` / ``Outlook Reason`` (draft-strict DH
  policy diagnostics).
- ``Outlook`` requires PlayerData CSV alongside the eval (for Pot* tool
  inputs). If PlayerData isn't found, Outlook columns are left blank with
  a warning — the rest of the analysis still runs.
- Tier benchmarks (62/54/48) still apply to ``Ideal_Value``. Outlook is
  surfaced but not separately tiered until tier bands get recalibrated
  against the v10 Outlook distribution (open follow-up).
"""

# --- repo-root + core/ path bootstrap ---
import os as _os, sys as _sys
_ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
for _p in (_ROOT, _os.path.join(_ROOT, "core")):
    if _p not in _sys.path:
        _sys.path.insert(0, _p)
# --- end bootstrap ---

import argparse
import csv
import json
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from statistics import mean, median, stdev
from typing import Any, Dict, List, Optional, Tuple

SCRIPT_DIR = Path(__file__).resolve().parent.parent

# Make sibling lib/ importable. Mirrors run_vos.py's sys.path manipulation
# so lib.draft_score's run_vos imports resolve too.
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from lib.draft_score import compute_draft_outlook  # noqa: E402

# Tier benchmarks (fixed cutoffs, not percentiles). Originally calibrated
# 2026-05-21 against the v6 Ideal_Value distribution. As of the v10 refactor
# the tier function applies these to Outlook (when present) with Ideal_Value
# as a fallback. The Outlook distribution is close enough to Ideal_Value's
# that the existing thresholds still produce meaningful tier sizes; recalibrate
# later if the v10 Outlook distribution shifts the tier counts.
VOS_TIER_BENCHMARKS = {
    "elite_min": 62,   # Truly exceptional prospects (top ~2-5%)
    "plus_min": 54,    # Quality starters, above average
    "average_min": 48, # Solid contributors, near league average
}

# Standard position set
POSITION_GROUPS = {
    "Infield": ["C", "1B", "2B", "3B", "SS"],
    "Outfield": ["LF", "CF", "RF"],
    "Pitching": ["SP", "RP"],
}

DEFAULT_DATA_DIR = SCRIPT_DIR / "data"
DEFAULT_CONFIG_DIR = SCRIPT_DIR / "config"
DEFAULT_WEIGHTS_NAME = "weights_v10.json"

# Standardized location for the StatsPlus-exported draft pool ID list.
# One row per draft-eligible player. The eval CSV is the full league; the
# draft pool ID file scopes the analysis to just the draft-eligible cohort
# so Outlook's adjustment stack (readiness, personality, draft_age) doesn't
# float established MLBers to the top of the board.
DEFAULT_DRAFT_POOL_IDS_TEMPLATE = "draft_pool_{league}.csv"


# ---------------------------------------------------------------------------
# Input resolution — auto-find latest eval CSV per league (draft mode first)
# ---------------------------------------------------------------------------

def find_latest_eval_for_league(
    league: str,
    *,
    prefer_draft: bool = True,
    org_code: Optional[str] = None,
) -> Optional[Path]:
    """Locate the most recent eval CSV for ``league``.

    Strategy (compares timestamps across all locations):

    1. If ``org_code`` is provided, prefer files under
       ``{league}/eval/{org_code}/``. The org-specific file wins even if a
       newer top-level file exists, since the user explicitly scoped.
    2. Otherwise, search ALL of ``{league}/eval/**`` recursively and pick
       the file with the latest timestamp in the name. This is critical
       because per-org-evals runs (the common case for active leagues)
       leave newer files in ``{league}/eval/{org}/`` while the top-level
       dir may hold older runs.
    3. Within whichever scope is selected, prefer ``draft_evaluation_*``
       over ``evaluation_summary_*`` when ``prefer_draft=True`` — but only
       if the draft-mode file is at least as recent as the summary file.
       An older draft eval shouldn't beat a fresh summary eval just
       because of the prefix.
    """
    eval_dir = SCRIPT_DIR / league / "eval"
    if not eval_dir.exists():
        return None

    summary_pat = f"evaluation_summary_{league}_*.csv"
    draft_pat = f"draft_evaluation_{league}_*.csv"

    if org_code:
        # Org-specific scope wins. Search only that subdir.
        org_dir = eval_dir / org_code.strip().lower()
        if not org_dir.exists():
            return None
        search_glob = org_dir.glob
    else:
        # League-wide scope. Recursive across all subdirs so per-org runs
        # are visible alongside top-level runs.
        search_glob = eval_dir.rglob

    def _ts_key(path: Path) -> str:
        """Extract the trailing ``YYYYMMDD_HHMMSS`` timestamp from an eval
        filename for cross-prefix sorting. Files without a parseable
        timestamp sort first (oldest)."""
        stem = path.stem
        parts = stem.rsplit("_", 2)
        if len(parts) >= 2 and parts[-2].isdigit() and parts[-1].isdigit():
            return f"{parts[-2]}_{parts[-1]}"
        return ""

    draft_matches = sorted(search_glob(draft_pat), key=_ts_key) if prefer_draft else []
    summary_matches = sorted(search_glob(summary_pat), key=_ts_key)

    latest_draft = draft_matches[-1] if draft_matches else None
    latest_summary = summary_matches[-1] if summary_matches else None

    if latest_draft is None:
        return latest_summary
    if latest_summary is None:
        return latest_draft

    # Both exist — pick the more recent file by parsed timestamp. This
    # honors the user's preference for draft_evaluation only when the
    # draft eval is at least as recent as the summary; a stale draft eval
    # shouldn't shadow a fresh summary.
    return latest_draft if _ts_key(latest_draft) >= _ts_key(latest_summary) else latest_summary


def infer_league_from_path(csv_path: Path) -> Optional[str]:
    """Infer league slug from an eval CSV filename. Supports both
    ``evaluation_summary_{league}_*.csv`` and
    ``draft_evaluation_{league}_*.csv`` shapes."""
    stem = csv_path.stem
    for prefix in ("draft_evaluation_", "evaluation_summary_"):
        if stem.startswith(prefix):
            tail = stem[len(prefix):].split("_", 1)
            if tail and tail[0]:
                return tail[0].lower()
    return None


# ---------------------------------------------------------------------------
# PlayerData loading — required for Outlook computation
# ---------------------------------------------------------------------------

def load_player_data(league: str,
                     data_dir: Path = DEFAULT_DATA_DIR
                     ) -> Dict[str, Dict[str, str]]:
    """Load PlayerData-{league}.csv into a {player_id: row} lookup.

    Returns {} (with a warning) when the file is missing — Outlook
    computation will then degrade gracefully.
    """
    path = data_dir / f"PlayerData-{league}.csv"
    if not path.exists():
        print(f"WARN: PlayerData not found at {path} — Outlook column will be blank.",
              file=sys.stderr)
        return {}
    out: Dict[str, Dict[str, str]] = {}
    with path.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            pid = (row.get("ID") or "").strip()
            if pid:
                out[pid] = row
    print(f"Loaded {len(out)} PlayerData rows from {path.name}", file=sys.stderr)
    return out


def load_weights_cfg(config_dir: Path = DEFAULT_CONFIG_DIR,
                     weights_name: str = DEFAULT_WEIGHTS_NAME
                     ) -> Optional[Dict[str, Any]]:
    """Load v10 weights JSON (required for Outlook computation)."""
    path = config_dir / weights_name
    if not path.exists():
        print(f"WARN: weights config not found at {path} — Outlook will be blank.",
              file=sys.stderr)
        return None
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def load_draft_pool_ids(path: Path) -> Optional[set]:
    """Load a draft-eligible player ID list from a StatsPlus-exported CSV.

    Accepts two shapes:
        1. CSV with header — first column named `ID` (case-insensitive). Any
           other columns are ignored. This matches StatsPlus's typical export
           format ("ID, Name, ...").
        2. Plain text — one player ID per line, no header. Comments (#) and
           blank lines are skipped.

    Returns the set of player IDs (as strings, since the eval CSV's ID
    column is string-typed). Returns None when the file doesn't exist —
    the caller treats that as "no filter, process the whole pool".
    """
    if not path.exists():
        return None

    ids: set = set()
    with path.open("r", encoding="utf-8", newline="") as f:
        # Peek at the first non-empty line to decide if it's a header or
        # a plain ID list. Robust to leading whitespace and BOM.
        first_line = ""
        peeked: List[str] = []
        for raw in f:
            line = raw.lstrip("﻿").rstrip("\n\r")
            if line.strip() and not line.lstrip().startswith("#"):
                first_line = line
                break
            peeked.append(raw)
        f.seek(0)

        looks_like_csv = "," in first_line or first_line.lower().lstrip().startswith("id")
        if looks_like_csv:
            reader = csv.DictReader(f)
            id_field = None
            if reader.fieldnames:
                # Case-insensitive ID column lookup
                for fn in reader.fieldnames:
                    if (fn or "").strip().lower() == "id":
                        id_field = fn
                        break
            if id_field is None:
                # Headered but no ID column — fall back to first column
                # (matches "headerless single-column" use case dressed as CSV)
                f.seek(0)
                for row in csv.reader(f):
                    if not row:
                        continue
                    val = (row[0] or "").strip()
                    if val and not val.startswith("#") and val.lower() != "id":
                        ids.add(val)
            else:
                for row in reader:
                    val = (row.get(id_field) or "").strip()
                    if val:
                        ids.add(val)
        else:
            # Plain text — one ID per line
            for raw in f:
                line = raw.strip().lstrip("﻿")
                if not line or line.startswith("#"):
                    continue
                ids.add(line)

    return ids


def resolve_draft_pool_ids_path(
    explicit_path: Optional[Path],
    league: Optional[str],
    data_dir: Path,
    input_csv_path: Path,
) -> Optional[Path]:
    """Decide which draft-pool-ids file (if any) to use.

    Resolution order:
        1. Explicit ``--draft-pool-ids PATH`` — always wins, even when the
           file doesn't exist (the loader will warn).
        2. Auto-detect when the input filename starts with
           ``draft_evaluation_`` — the --draft prefix signals draft intent,
           so look for the standard ``data/draft_pool_{league}.csv``.
        3. Otherwise: no filter (return None).

    The auto-detect path is the recommended workflow: export the draft
    pool from StatsPlus to ``data/draft_pool_{league}.csv``, run with
    ``--draft`` to get a ``draft_evaluation_*.csv`` eval, and the
    filter activates automatically.
    """
    if explicit_path is not None:
        return explicit_path
    if league is None:
        return None
    # Auto-detect from draft_evaluation_* input
    if input_csv_path.stem.startswith("draft_evaluation_"):
        candidate = data_dir / DEFAULT_DRAFT_POOL_IDS_TEMPLATE.format(
            league=league.strip().lower(),
        )
        if candidate.exists():
            return candidate
    return None


# ---------------------------------------------------------------------------
# Column reading + Outlook augmentation
# ---------------------------------------------------------------------------

def get_column_value(row: Dict[str, Any], *possible_names: str) -> Optional[Any]:
    """Try multiple column names in order, return first found non-empty value."""
    for name in possible_names:
        if name in row:
            val = row[name]
            if val is not None and str(val).strip() != "":
                return val
    return None


def _safe_float(v: Any) -> Optional[float]:
    if v is None or str(v).strip() == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def augment_with_outlook(
    players: List[Dict[str, Any]],
    player_data_lookup: Dict[str, Dict[str, str]],
    cfg: Optional[Dict[str, Any]],
) -> int:
    """Compute Draft_Outlook for every row that has a matching PlayerData
    entry. Annotates each row in place with:

        _outlook              — Draft_Outlook score (20-80)
        _outlook_composite    — pre-adjustment composite
        _outlook_pos          — best position under draft_strict DH policy
        _outlook_reason       — short routing-reason tag

    Skips rows where PlayerData is unavailable or cfg wasn't loaded — those
    rows keep blank Outlook columns. Returns the count of successfully
    annotated rows.
    """
    if not cfg or not player_data_lookup:
        return 0

    annotated = 0
    for row in players:
        pid = str(get_column_value(row, "ID", "Player_ID", "PlayerID", "player_id") or "").strip()
        if not pid:
            continue
        pd_row = player_data_lookup.get(pid)
        if not pd_row:
            continue
        try:
            result = compute_draft_outlook(pd_row, cfg)
        except Exception as exc:
            # Defensive: never let an Outlook failure kill the whole run.
            # Print the first occurrence for debugging; downstream rows
            # silently skip.
            if annotated == 0:
                print(f"WARN: compute_draft_outlook failed for ID={pid}: {exc}",
                      file=sys.stderr)
            continue
        row["_outlook"] = result["draft_outlook"]
        row["_outlook_composite"] = result["composite"]
        row["_outlook_pos"] = result["ideal_pos"]
        row["_outlook_reason"] = (result.get("breakdown") or {}).get("ideal_reason", "")
        annotated += 1
    return annotated


def load_draft_pool(csv_path: Path) -> List[Dict[str, Any]]:
    """Load eval CSV with flexible column mapping. Filters rows missing
    Projected_Position or Ideal_Value (need both for ranking/tiering)."""
    if not csv_path.exists():
        raise FileNotFoundError(f"Draft pool file not found: {csv_path}")

    players: List[Dict[str, Any]] = []
    with open(csv_path, newline="", encoding="utf-8") as f:
        try:
            reader = csv.DictReader(f)
            if not reader.fieldnames:
                raise ValueError("CSV has no header row")
            for row in reader:
                projected_pos = get_column_value(row, "Projected_Position",
                                                 "Ideal_Position", "Ideal Pos")
                raw_value = get_column_value(row, "Ideal_Value", "VOS_Potential",
                                             "VOS Potential")
                if projected_pos is None or raw_value is None:
                    continue
                try:
                    value = float(raw_value)
                except (TypeError, ValueError):
                    continue
                row["_ideal_position"] = str(projected_pos).strip()
                row["_ideal_value"] = value  # used for ranking/tiers (Ideal_Value)
                players.append(row)
        except csv.Error as e:
            raise ValueError(f"Invalid CSV format: {e}") from e

    if not players:
        raise ValueError("No valid players found after filtering "
                         "(need Projected_Position and Ideal_Value)")
    return players


# ---------------------------------------------------------------------------
# Statistics
# ---------------------------------------------------------------------------

def calculate_statistics(values: List[float]) -> Dict[str, float]:
    """Return count/mean/median/min/max/std_dev + p5/p10/p25/p75/p90/p95."""
    result: Dict[str, float] = {
        "count": 0.0, "mean": 0.0, "median": 0.0, "min": 0.0, "max": 0.0,
        "std_dev": 0.0, "p5": 0.0, "p10": 0.0, "p25": 0.0, "p75": 0.0,
        "p90": 0.0, "p95": 0.0,
    }
    if not values:
        return result

    sorted_vals = sorted(values)
    n = len(sorted_vals)
    result["count"] = float(n)
    result["mean"] = mean(sorted_vals)
    result["median"] = median(sorted_vals)
    result["min"] = min(sorted_vals)
    result["max"] = max(sorted_vals)

    if n >= 2:
        result["std_dev"] = stdev(sorted_vals)

    def percentile_index(p: float) -> int:
        idx = max(0, min(n - 1, int(round(p / 100.0 * n)) - 1))
        return max(0, idx)

    result["p5"] = sorted_vals[percentile_index(5)]
    result["p10"] = sorted_vals[percentile_index(10)]
    result["p25"] = sorted_vals[percentile_index(25)]
    result["p75"] = sorted_vals[percentile_index(75)]
    result["p90"] = sorted_vals[percentile_index(90)]
    result["p95"] = sorted_vals[percentile_index(95)]

    return result


def analyze_position_distribution(players: List[Dict[str, Any]]) -> Dict[str, int]:
    """Count players by ideal position (Projected_Position from eval)."""
    counts: Dict[str, int] = defaultdict(int)
    for p in players:
        pos = p.get("_ideal_position", "")
        if pos:
            counts[pos] += 1
    return dict(counts)


def analyze_position_strength(
    players: List[Dict[str, Any]],
) -> Tuple[Dict[str, Dict[str, Any]], str]:
    """Calculate position-level statistics on the primary axis (Outlook with
    Ideal_Value fallback). Returns (per-position stats, axis_label) so the
    report can label its columns correctly.
    """
    by_pos: Dict[str, List[float]] = defaultdict(list)
    using_outlook = any(p.get("_outlook") is not None for p in players)
    for p in players:
        pos = p.get("_ideal_position", "")
        val = _primary_value(p)
        if pos and val is not None:
            by_pos[pos].append(val)

    result: Dict[str, Dict[str, Any]] = {}
    for pos, vals in by_pos.items():
        stats = calculate_statistics(vals)
        result[pos] = {
            "count": int(stats["count"]),
            "mean": stats["mean"],
            "median": stats["median"],
            "min": stats["min"],
            "max": stats["max"],
            "p5": stats["p5"],
            "p25": stats["p25"],
            "p75": stats["p75"],
            "p95": stats["p95"],
        }
    axis_label = "Outlook" if using_outlook else "Ideal Value"
    return result, axis_label


def _primary_value(player: Dict[str, Any]) -> Optional[float]:
    """Return Outlook when populated, falling back to Ideal_Value. This is
    the single source of truth for sort + tier under v10. When a player's
    Outlook couldn't be computed (PlayerData missing, pre-v10 eval CSV),
    Ideal_Value preserves the v6-era behavior so the script never has to
    fail on a row.
    """
    outlook = player.get("_outlook")
    if outlook is not None:
        try:
            return float(outlook)
        except (TypeError, ValueError):
            pass
    ideal = player.get("_ideal_value")
    if ideal is None:
        return None
    try:
        return float(ideal)
    except (TypeError, ValueError):
        return None


def categorize_prospects(
    players: List[Dict[str, Any]],
) -> Tuple[Dict[str, List[Dict[str, Any]]], Dict[str, Any]]:
    """Categorize into Elite/Plus/Average/Org Depth on Outlook (with
    Ideal_Value fallback when Outlook isn't available). Returns
    (tier -> list of players, tier -> aggregate stats).
    """
    elite_min = VOS_TIER_BENCHMARKS["elite_min"]
    plus_min = VOS_TIER_BENCHMARKS["plus_min"]
    average_min = VOS_TIER_BENCHMARKS["average_min"]

    categories: Dict[str, List[Dict[str, Any]]] = {
        "Elite": [],
        "Plus": [],
        "Average": [],
        "Org Depth": [],
    }

    for p in players:
        v = _primary_value(p)
        if v is None:
            continue
        if v >= elite_min:
            categories["Elite"].append(p)
        elif v >= plus_min:
            categories["Plus"].append(p)
        elif v >= average_min:
            categories["Average"].append(p)
        else:
            categories["Org Depth"].append(p)

    tier_stats: Dict[str, Any] = {}
    for tier, tier_players in categories.items():
        vals = [_primary_value(p) for p in tier_players]
        vals = [v for v in vals if v is not None]
        tier_stats[tier] = calculate_statistics(vals) if vals else calculate_statistics([])

    return categories, tier_stats


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------

def generate_summary_report(
    players: List[Dict[str, Any]],
    position_counts: Dict[str, int],
    position_strength: Dict[str, Dict[str, Any]],
    categories: Dict[str, List[Dict[str, Any]]],
    output_path: Path,
    outlook_annotated: int = 0,
    axis_label: str = "Outlook",
) -> None:
    """Generate 00_summary.txt. Surfaces both Ideal_Value pool stats and
    (when populated) Outlook pool stats so the user can spot drift between
    the heuristic ranking key and the new Outlook composite."""
    total = len(players)
    all_ideal = [p["_ideal_value"] for p in players]
    ideal_stats = calculate_statistics(all_ideal)

    outlook_vals = [p["_outlook"] for p in players if p.get("_outlook") is not None]
    outlook_stats = calculate_statistics(outlook_vals) if outlook_vals else None

    # Pool stats reported on the v10 primary axis (Outlook) when available,
    # falling back to Ideal Value. Both are shown so the user can spot drift.
    primary_stats = outlook_stats or ideal_stats

    pos_means = [(pos, data["mean"], data["count"])
                 for pos, data in position_strength.items()]
    pos_means.sort(key=lambda x: -x[1])
    strongest = pos_means[:5]
    weakest = pos_means[-5:] if len(pos_means) >= 5 else pos_means
    weakest.reverse()

    lines = [
        "=" * 80,
        "DRAFT POOL ANALYSIS SUMMARY",
        "=" * 80,
        "",
        "OVERVIEW",
        "-" * 80,
        f"Total Players: {total}",
    ]
    if outlook_stats:
        lines.extend([
            f"Mean Outlook (primary axis, Career×Pot*): {outlook_stats['mean']:.2f}",
            f"Median Outlook: {outlook_stats['median']:.2f}",
            f"Outlook coverage: {outlook_annotated}/{total} "
            f"({100*outlook_annotated/total:.1f}%) — rest missing PlayerData",
            f"Mean Ideal Value (cross-reference): {ideal_stats['mean']:.2f}",
            f"Median Ideal Value: {ideal_stats['median']:.2f}",
        ])
    else:
        lines.extend([
            f"Mean Ideal Value: {ideal_stats['mean']:.2f}",
            f"Median Ideal Value: {ideal_stats['median']:.2f}",
            "Outlook: not computed (PlayerData or weights config missing) — "
            "tiers and ranking fall back to Ideal Value",
        ])

    lines.extend([
        "",
        "POSITION DISTRIBUTION",
        "-" * 80,
    ])
    for pos in sorted(position_counts.keys()):
        c = position_counts[pos]
        pct = 100.0 * c / total if total else 0
        lines.append(f"{pos}: {c} ({pct:.1f}%)")

    lines.extend([
        "",
        f"STRONGEST POSITIONS (by average {axis_label})",
        "-" * 80,
    ])
    for i, (pos, m, c) in enumerate(strongest, 1):
        lines.append(f"{i}. {pos}: {m:.2f} (n={c})")

    lines.extend([
        "",
        f"WEAKEST POSITIONS (by average {axis_label})",
        "-" * 80,
    ])
    for i, (pos, m, c) in enumerate(weakest, 1):
        lines.append(f"{i}. {pos}: {m:.2f} (n={c})")

    primary_label = "Outlook" if outlook_stats else "Ideal Value"
    lines.extend([
        "",
        f"PROSPECT TIER BREAKDOWN (fixed {primary_label} benchmarks)",
        "-" * 80,
    ])
    for tier in ["Elite", "Plus", "Average", "Org Depth"]:
        c = len(categories[tier])
        pct = 100.0 * c / total if total else 0
        lines.append(f"{tier}: {c} ({pct:.1f}%)")

    lines.extend([
        "",
        "KEY METRICS FOR REFERENCE (tier benchmarks)",
        "-" * 80,
        f"Elite: {primary_label} >= 62 (Truly exceptional prospects)",
        f"Plus: {primary_label} 54-61 (Quality starters, above average)",
        f"Average: {primary_label} 48-53 (Solid contributors, league average)",
        f"Org Depth: {primary_label} < 48 (Below average, organizational depth)",
        f"Average Draft Pool Quality ({primary_label}): {primary_stats['mean']:.2f}",
        f"Median Draft Pool Quality ({primary_label}): {primary_stats['median']:.2f}",
    ])

    output_path.write_text("\n".join(lines), encoding="utf-8")


def generate_position_distribution_report(
    position_counts: Dict[str, int],
    total: int,
    output_path: Path,
) -> None:
    """Generate 01_position_distribution.txt."""
    lines = [
        "=" * 80,
        "POSITION DISTRIBUTION",
        "=" * 80,
        "",
        f"Total Players: {total}",
        "",
        "Position         Count      Percentage     ",
        "-" * 80,
    ]
    for pos in sorted(position_counts.keys()):
        c = position_counts[pos]
        pct = 100.0 * c / total if total else 0
        lines.append(f"{pos:<16} {c:>6}    {pct:>6.1f}%")

    group_counts: Dict[str, int] = defaultdict(int)
    for pos_name, pos_list in POSITION_GROUPS.items():
        for p in pos_list:
            group_counts[pos_name] += position_counts.get(p, 0)
    if "DH" in position_counts:
        group_counts["DH"] = position_counts["DH"]

    lines.extend([
        "",
        "=" * 80,
        "POSITION GROUPING SUMMARY",
        "=" * 80,
        "",
        "Category             Count      Percentage     ",
        "-" * 80,
    ])
    for cat in ["Infield", "Outfield", "Pitching"]:
        c = group_counts.get(cat, 0)
        pct = 100.0 * c / total if total else 0
        lines.append(f"{cat:<20} {c:>6}    {pct:>6.1f}%")
    if group_counts.get("DH", 0):
        c = group_counts["DH"]
        pct = 100.0 * c / total if total else 0
        lines.append(f"{'DH':<20} {c:>6}    {pct:>6.1f}%")

    output_path.write_text("\n".join(lines), encoding="utf-8")


def generate_position_strength_report(
    position_strength: Dict[str, Dict[str, Any]],
    output_path: Path,
    axis_label: str = "Outlook",
) -> None:
    """Generate 02_position_strength.txt. ``axis_label`` is "Outlook" when
    Outlook was successfully computed for the pool, "Ideal Value" otherwise."""
    rows = [(pos, data) for pos, data in position_strength.items()]
    rows.sort(key=lambda x: -x[1]["mean"])

    lines = [
        "=" * 80,
        "POSITION STRENGTH ANALYSIS",
        "=" * 80,
        "",
        f"Average {axis_label} by Position",
        "",
        "Position         Count      Mean       Median     Min        Max       ",
        "-" * 80,
    ]
    for pos, data in rows:
        lines.append(
            f"{pos:<16} {data['count']:>6}    {data['mean']:>6.2f}     "
            f"{data['median']:>6.2f}   {data['min']:>6.2f}    {data['max']:>6.2f}"
        )

    lines.extend([
        "",
        "=" * 80,
        "POSITION STRENGTH RANKINGS",
        "=" * 80,
        "",
        f"Strongest positions (by average {axis_label}):",
    ])
    for i, (pos, data) in enumerate(rows[:10], 1):
        lines.append(f"  {i}. {pos}: {data['mean']:.2f} (n={data['count']})")

    lines.extend([
        "",
        f"Weakest positions (by average {axis_label}):",
    ])
    for i, (pos, data) in enumerate(reversed(rows[-10:]), 1):
        lines.append(f"  {i}. {pos}: {data['mean']:.2f} (n={data['count']})")

    lines.extend([
        "",
        "=" * 80,
        "POSITION PERCENTILES",
        "=" * 80,
        "",
        "Position         P25        P50        P75        P95       ",
        "-" * 80,
    ])
    for pos, data in rows:
        lines.append(
            f"{pos:<16} {data['p25']:>6.2f}     {data['median']:>6.2f}     "
            f"{data['p75']:>6.2f}     {data['p95']:>6.2f}"
        )

    output_path.write_text("\n".join(lines), encoding="utf-8")


def generate_ideal_value_distribution_report(
    players: List[Dict[str, Any]],
    output_path: Path,
) -> None:
    """Generate 03_ideal_value_distribution.txt."""
    vals = [p["_ideal_value"] for p in players]
    stats = calculate_statistics(vals)
    total = len(vals)

    ranges = [
        (90, 100), (80, 89), (70, 79), (60, 69), (50, 59),
        (40, 49), (30, 39), (20, 29),
    ]

    range_counts: List[Tuple[str, int]] = []
    for low, high in ranges:
        c = sum(1 for v in vals if low <= v <= high)
        range_counts.append((f"{low}-{high}", c))
    c_under = sum(1 for v in vals if v < 20)
    range_counts.append(("<20", c_under))

    lines = [
        "=" * 80,
        "IDEAL VALUE DISTRIBUTION",
        "=" * 80,
        "",
        "SUMMARY STATISTICS",
        "-" * 80,
        f"Count: {int(stats['count'])}",
        f"Mean: {stats['mean']:.2f}",
        f"Median: {stats['median']:.2f}",
        f"Std Dev: {stats['std_dev']:.2f}",
        f"Min: {stats['min']:.2f}",
        f"Max: {stats['max']:.2f}",
        "",
        "PERCENTILES",
        "-" * 80,
        f"5th:  {stats['p5']:.2f}",
        f"10th: {stats['p10']:.2f}",
        f"25th: {stats['p25']:.2f}",
        f"50th (Median): {stats['median']:.2f}",
        f"75th: {stats['p75']:.2f}",
        f"90th: {stats['p90']:.2f}",
        f"95th: {stats['p95']:.2f}",
        "",
        "DISTRIBUTION BY RANGE",
        "-" * 80,
        "Range            Count      Percentage",
    ]
    for label, c in range_counts:
        pct = 100.0 * c / total if total else 0
        lines.append(f"{label:<16} {c:>6}    {pct:>6.1f}%")

    output_path.write_text("\n".join(lines), encoding="utf-8")


def generate_prospect_tier_report(
    categories: Dict[str, List[Dict[str, Any]]],
    tier_stats: Dict[str, Any],
    total: int,
    output_path: Path,
    axis_label: str = "Outlook",
) -> None:
    """Generate 04_prospect_tiers.txt. ``axis_label`` is "Outlook" when
    tiering ran on Outlook (the v10 default), "Ideal Value" when the
    pool fell back to Ideal Value (no PlayerData / pre-v10 eval)."""
    lines = [
        "=" * 80,
        "PROSPECT TIER BREAKDOWN",
        "=" * 80,
        "",
        f"Fixed {axis_label} Benchmarks (62/54/48 — originally calibrated on",
        "Ideal Value, carried forward to Outlook for v10):",
        f"- Elite: {axis_label} >= 62 (Truly exceptional prospects)",
        f"- Plus: {axis_label} 54-61 (Quality starters, above average)",
        f"- Average: {axis_label} 48-53 (Solid contributors, league average)",
        f"- Org Depth: {axis_label} < 48 (Below average, organizational depth)",
        "",
        "TIER SUMMARY",
        "-" * 80,
        "Tier             Count      Percentage  Mean       Median     Range",
    ]

    for tier in ["Elite", "Plus", "Average", "Org Depth"]:
        pl = categories[tier]
        st = tier_stats[tier]
        c = len(pl)
        pct = 100.0 * c / total if total else 0
        mn = st["mean"]
        med = st["median"]
        rmin = st["min"]
        rmax = st["max"]
        lines.append(f"{tier:<16} {c:>6}    {pct:>6.1f}%      {mn:>6.2f}     "
                     f"{med:>6.2f}   {rmin:.2f}-{rmax:.2f}")

    lines.append("")
    lines.append("DETAILED BREAKDOWN")
    lines.append("-" * 80)

    for tier in ["Elite", "Plus", "Average", "Org Depth"]:
        pl = categories[tier]
        st = tier_stats[tier]
        c = len(pl)
        pct = 100.0 * c / total if total else 0
        lines.append("")
        if tier == "Elite":
            lines.append(f"ELITE TIER ({axis_label} >= 62)")
        elif tier == "Plus":
            lines.append(f"PLUS TIER ({axis_label} 54-61)")
        elif tier == "Average":
            lines.append(f"AVERAGE TIER ({axis_label} 48-53)")
        else:
            lines.append(f"ORG DEPTH TIER ({axis_label} < 48)")
        lines.append(f"Total: {c} players ({pct:.1f}% of pool)")
        lines.append(f"Average {axis_label}: {st['mean']:.2f}")
        lines.append(f"Median {axis_label}: {st['median']:.2f}")
        lines.append(f"Range: {st['min']:.2f} - {st['max']:.2f}")

    output_path.write_text("\n".join(lines), encoding="utf-8")


def generate_csv_summary(
    position_counts: Dict[str, int],
    position_strength: Dict[str, Dict[str, Any]],
    categories: Dict[str, List[Dict[str, Any]]],
    total: int,
    output_path: Path,
) -> None:
    """Generate summary_data.csv."""
    lines = [
        "Position Distribution",
        "Position,Count,Percentage",
    ]
    for pos in sorted(position_counts.keys()):
        c = position_counts[pos]
        pct = f"{100.0 * c / total:.2f}%" if total else "0%"
        lines.append(f"{pos},{c},{pct}")

    lines.extend([
        "",
        "Position Strength",
        "Position,Count,Mean,Median,Min,Max,P25,P75",
    ])
    for pos in sorted(position_strength.keys()):
        d = position_strength[pos]
        lines.append(f"{pos},{d['count']},{d['mean']:.2f},{d['median']:.2f},"
                     f"{d['min']:.2f},{d['max']:.2f},{d['p25']:.2f},{d['p75']:.2f}")

    def tier_pct(count: int) -> str:
        return f"{100.0 * count / total:.2f}%" if total else "0.00%"

    lines.extend([
        "",
        "Prospect Tiers",
        "Tier,Count,Percentage,Threshold",
        f"Elite,{len(categories['Elite'])},{tier_pct(len(categories['Elite']))},>= 62",
        f"Plus,{len(categories['Plus'])},{tier_pct(len(categories['Plus']))},54-61",
        f"Average,{len(categories['Average'])},{tier_pct(len(categories['Average']))},48-53",
        f"Org Depth,{len(categories['Org Depth'])},{tier_pct(len(categories['Org Depth']))},< 48",
    ])

    output_path.write_text("\n".join(lines), encoding="utf-8")


def generate_summary_md(
    position_counts: Dict[str, int],
    position_strength: Dict[str, Dict],
    categories: Dict[str, List],
    total: int,
    output_path: Path,
) -> None:
    """Markdown companion to summary_data.csv (Obsidian quick-reference)."""
    lines = ["## Position Distribution", ""]
    lines += ["| Position | Count | Percentage |", "| --- | --- | --- |"]
    for pos in sorted(position_counts.keys()):
        c = position_counts[pos]
        pct = f"{100.0 * c / total:.1f}%" if total else "0%"
        lines.append(f"| {pos} | {c} | {pct} |")

    lines += ["", "## Position Strength", ""]
    lines += ["| Position | Count | Mean | Median | Min | Max |",
              "| --- | --- | --- | --- | --- | --- |"]
    for pos in sorted(position_strength.keys()):
        d = position_strength[pos]
        lines.append(f"| {pos} | {d['count']} | {d['mean']:.1f} | "
                     f"{d['median']:.1f} | {d['min']:.1f} | {d['max']:.1f} |")

    def tier_pct(count: int) -> str:
        return f"{100.0 * count / total:.1f}%" if total else "0%"

    lines += ["", "## Prospect Tiers", ""]
    lines += ["| Tier | Count | Percentage | Threshold |", "| --- | --- | --- | --- |"]
    lines += [
        f"| Elite | {len(categories['Elite'])} | {tier_pct(len(categories['Elite']))} | >= 62 |",
        f"| Plus | {len(categories['Plus'])} | {tier_pct(len(categories['Plus']))} | 54-61 |",
        f"| Average | {len(categories['Average'])} | {tier_pct(len(categories['Average']))} | 48-53 |",
        f"| Org Depth | {len(categories['Org Depth'])} | {tier_pct(len(categories['Org Depth']))} | < 48 |",
    ]
    lines.append("")
    output_path.write_text("\n".join(lines), encoding="utf-8")


def _tier_for_value(value: float) -> str:
    """Return prospect tier label. Caller is responsible for supplying the
    primary axis value (Outlook with Ideal_Value fallback under v10)."""
    if value >= VOS_TIER_BENCHMARKS["elite_min"]:
        return "Elite"
    if value >= VOS_TIER_BENCHMARKS["plus_min"]:
        return "Plus"
    if value >= VOS_TIER_BENCHMARKS["average_min"]:
        return "Average"
    return "Org Depth"


def _md_escape(s: str) -> str:
    """Escape pipe and newline for markdown table cells."""
    if not s:
        return ""
    return str(s).replace("|", "\\|").replace("\n", " ").replace("\r", "").strip()


def _parse_viable_positions(raw_list: str) -> List[str]:
    """Parse projected viable position list into normalized position codes."""
    if not raw_list:
        return []
    normalized = str(raw_list).replace("/", ",").replace(";", ",")
    return [p.strip() for p in normalized.split(",") if p.strip()]


def _build_viable_potential_cell(player: Dict[str, Any]) -> str:
    """Build markdown cell showing potential score per viable projected position."""
    viable_raw = get_column_value(player, "Projected_Viable_Pos_List")
    viable_positions = _parse_viable_positions(str(viable_raw or ""))
    if not viable_positions:
        return ""

    pairs: List[str] = []
    for pos in viable_positions:
        pos_potential = get_column_value(player, f"{pos}_Potential")
        if pos_potential is None:
            continue
        try:
            pos_potential_str = f"{float(pos_potential):.2f}"
        except (TypeError, ValueError):
            pos_potential_str = str(pos_potential).strip()
        pairs.append(f"{pos}:{pos_potential_str}")
    return ", ".join(pairs)


def _fmt_float(value: Any, digits: int = 2) -> str:
    """Format a numeric cell, falling back to empty string when blank/NaN."""
    v = _safe_float(value)
    return f"{v:.{digits}f}" if v is not None else ""


def generate_draft_pool_markdown(
    players: List[Dict[str, Any]],
    output_path: Path,
) -> None:
    """Generate 05_draft_pool.md — the canonical contract downstream tools
    (draft_board, draft_grades) parse.

    Column ordering preserves all v2/v6-era columns at their established
    positions; v10 additions (Outlook, Reach, Career, Blend, Pers, Prone,
    Ready, Outlook Pos, Outlook Reason) are appended after Ideal Value and
    before Tier. Tier still floats at the end so it's easy to filter on.

    Sort key under v10: Outlook (with Ideal_Value as fallback). Ideal_Value
    is preserved as a column for cross-reference but no longer drives the
    ranking. See ``_primary_value`` for the fallback semantics.
    """
    # Sort by Outlook descending (Ideal_Value fallback). The Rank column
    # accordingly reflects Outlook ranking under v10.
    sorted_players = sorted(
        players, key=lambda p: _primary_value(p) or 0, reverse=True,
    )

    headers = [
        # v2/v6-era core (do not reorder — downstream parsers rely on these)
        "Rank", "ID", "Name", "Pos", "Age", "Org",
        "Projected Position", "Projected Margin Tier",
        "Projected Viable Pos List", "Viable Pos Potentials",
        "Ideal Value",
        # v10 additions — appended in priority order
        "Outlook", "Outlook Pos", "Outlook Reason",
        "Reach", "Career", "Blend",
        "Pers", "Prone", "Ready",
        # Tier last (computed from Ideal Value)
        "Tier",
    ]

    rows = []
    for idx, p in enumerate(sorted_players, start=1):
        pid_raw = get_column_value(p, "ID", "Player_ID", "PlayerID", "player_id")
        pid = ""
        if pid_raw is not None and str(pid_raw).strip():
            try:
                pid = _md_escape(str(int(float(pid_raw))))
            except (TypeError, ValueError):
                pid = _md_escape(str(pid_raw))
        name = _md_escape(get_column_value(p, "Name") or "")
        pos = _md_escape(get_column_value(p, "Pos") or "")
        age_raw = get_column_value(p, "Age")
        age = ""
        if age_raw is not None and str(age_raw).strip():
            try:
                age = _md_escape(str(int(float(age_raw))))
            except (TypeError, ValueError):
                age = _md_escape(str(age_raw))
        org = _md_escape(get_column_value(p, "Org") or "")
        projected_pos = _md_escape(
            get_column_value(p, "Projected_Position")
            or p.get("_ideal_position")
            or ""
        )
        projected_margin_tier = _md_escape(
            get_column_value(p, "Projected_Margin_Tier") or ""
        )
        viable_pos_list = _md_escape(
            str(get_column_value(p, "Projected_Viable_Pos_List") or "")
        )
        viable_pos_potentials = _md_escape(_build_viable_potential_cell(p))
        ideal_value = p.get("_ideal_value")
        ideal_value_str = f"{ideal_value:.2f}" if ideal_value is not None else ""

        # v10 columns — all gracefully empty if the source column is blank.
        outlook = _fmt_float(p.get("_outlook"), 2)
        outlook_pos = _md_escape(p.get("_outlook_pos") or "")
        outlook_reason = _md_escape(p.get("_outlook_reason") or "")
        reach = _fmt_float(get_column_value(p, "VOS_Reach"), 2)
        career = _fmt_float(get_column_value(p, "VOS_Career"), 2)
        blend = _fmt_float(get_column_value(p, "VOS_Blended"), 2)
        pers = _fmt_float(get_column_value(p, "Personality_Adj"), 2)
        prone = _md_escape(str(get_column_value(p, "Prone") or ""))
        ready = _fmt_float(get_column_value(p, "Readiness_Adj"), 2)

        # Tier on Outlook (Ideal_Value fallback) — matches the new sort key
        # so the Rank and Tier columns reference the same axis.
        primary = _primary_value(p)
        tier = _tier_for_value(primary) if primary is not None else ""

        rows.append([
            str(idx), pid, name, pos, age, org,
            projected_pos, projected_margin_tier,
            viable_pos_list, viable_pos_potentials,
            ideal_value_str,
            outlook, outlook_pos, outlook_reason,
            reach, career, blend,
            pers, prone, ready,
            tier,
        ])

    # Header blurb explaining the new columns so consumers (and future you)
    # don't have to dig back into draft_score.py to remember what Outlook is.
    blurb_lines = [
        "**Sorted by Outlook (best first)** — the v10 Career-weights × Pot* composite,",
        "which answers \"if this prospect realizes their ceiling, how good will they",
        "be as an MLB player?\" Tier categorization runs on the same axis.",
        "",
        "Ideal Value is preserved as a cross-reference column. When Outlook is missing",
        "(no PlayerData / pre-v10 eval), the row falls back to Ideal Value for both",
        "sort and tier.",
        "",
        "**v10 column semantics:**",
        "- **Outlook** *(primary sort)*: Career-weights applied to Pot* ratings,",
        "  normalized 20-80. See `lib/draft_score.py`. Reflects ceiling-as-MLB-WAR.",
        "- **Outlook Pos / Outlook Reason**: best-position pick under the draft-strict",
        "  DH-routing policy and a short reason tag (field_max, field_routed,",
        "  dh_bat_dominates, dh_no_viable_field, dh_unrescuable_with_elite_bat).",
        "- **Ideal Value**: heuristic Reach composite at the player's best position",
        "  (Pot*-weighted, pre-adjustment). The v3-v10 legacy ranking key. Still",
        "  written so draft_board / draft_grades can fall back to it.",
        "- **Reach / Career / Blend**: v10's three normalized scores from the eval CSV.",
        "  Reach is the logistic-model P(reach MLB) score; Career uses current",
        "  ratings + age decay; Blend = 0.4 × Reach + 0.6 × Career.",
        "- **Pers / Ready**: Personality_Adj and Readiness_Adj from the eval (latter",
        "  populated only when --draft was passed to run_vos.py).",
        "- **Prone**: categorical injury proneness (Iron Man / Durable / Normal /",
        "  Fragile / Wrecked) passed through from PlayerData.",
    ]

    lines = [
        "# Draft Pool (Evaluation Summary)",
        "",
        f"Total players: {len(players)}.",
        "",
        *blurb_lines,
        "",
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    for row in rows:
        lines.append("| " + " | ".join(row) + " |")

    output_path.write_text("\n".join(lines), encoding="utf-8")


# ---------------------------------------------------------------------------
# Input resolution
# ---------------------------------------------------------------------------

def resolve_input_path(path_arg: str, script_dir: Path) -> Path:
    """Resolve input CSV path; if relative and not found, try parent directory."""
    p = Path(path_arg)
    if not p.is_absolute():
        if (script_dir / p).exists():
            return script_dir / p
        if (script_dir.parent / p).exists():
            return script_dir.parent / p
        return script_dir / p  # Return as-is so load_draft_pool can raise
    return p


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate draft pool analysis package from a v10 eval CSV.",
    )
    src_group = parser.add_mutually_exclusive_group(required=True)
    src_group.add_argument(
        "draft_pool",
        type=str,
        nargs="?",
        default=None,
        help="Path to draft pool CSV (typically draft_evaluation_{league}_*.csv "
             "or evaluation_summary_{league}_*.csv). Mutually exclusive with --league.",
    )
    src_group.add_argument(
        "--league",
        type=str,
        default=None,
        help="League slug — auto-resolve to the latest draft_evaluation_{league}_*.csv "
             "(falling back to evaluation_summary_{league}_*.csv) under {league}/eval/.",
    )
    parser.add_argument(
        "--org-code", type=str, default=None,
        help="When --league is used, look in {league}/eval/{org_code}/ first.",
    )
    parser.add_argument(
        "--no-prefer-draft", action="store_true",
        help="When --league is used, skip draft_evaluation_*.csv and go straight to "
             "evaluation_summary_*.csv. Useful mid-season when --draft hasn't been re-run.",
    )
    parser.add_argument(
        "--output-dir", type=str, default=None,
        help="Custom output directory path.",
    )
    parser.add_argument(
        "--name", type=str, default=None,
        help="Custom name for folder (draft_pool_analysis_{name}).",
    )
    parser.add_argument(
        "--data-dir", type=Path, default=DEFAULT_DATA_DIR,
        help="Directory containing PlayerData-{league}.csv (for Outlook computation).",
    )
    parser.add_argument(
        "--config-dir", type=Path, default=DEFAULT_CONFIG_DIR,
        help="Directory containing weights_v10.json.",
    )
    parser.add_argument(
        "--weights-name", type=str, default=DEFAULT_WEIGHTS_NAME,
        help=f"Weights file name within --config-dir (default {DEFAULT_WEIGHTS_NAME}).",
    )
    parser.add_argument(
        "--skip-outlook", action="store_true",
        help="Don't compute the Outlook column (use when PlayerData isn't available).",
    )
    parser.add_argument(
        "--draft-pool-ids", type=Path, default=None,
        help="Path to a CSV/text file of draft-eligible player IDs. Filters the "
             "eval rows before analysis so non-amateurs (vets, MLB players) "
             "don't dominate the Outlook-sorted ranking. Format: either a CSV "
             "with an `ID` column (StatsPlus export format) or one ID per line. "
             "Auto-detected at data/draft_pool_{league}.csv when --league is used "
             "AND input is a draft_evaluation_*.csv file.",
    )
    parser.add_argument(
        "--no-draft-pool-filter", action="store_true",
        help="Disable the draft-pool-ids auto-detect. Use when you specifically "
             "want to analyze the entire eval (not just draft-eligible players).",
    )
    args = parser.parse_args()

    # Resolve the input CSV.
    if args.league:
        csv_path = find_latest_eval_for_league(
            args.league.strip().lower(),
            prefer_draft=not args.no_prefer_draft,
            org_code=args.org_code,
        )
        if csv_path is None:
            print(f"Error: no eval CSV found for league '{args.league}' under "
                  f"{SCRIPT_DIR / args.league / 'eval'}", file=sys.stderr)
            sys.exit(1)
    else:
        csv_path = resolve_input_path(args.draft_pool, SCRIPT_DIR)

    print(f"Loading draft pool from {csv_path}...")

    try:
        players = load_draft_pool(csv_path)
    except FileNotFoundError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    print(f"Loaded {len(players)} players from eval")

    # Apply the draft-pool ID filter when one is configured. This is what
    # gives draft analyses an actual draft cohort to rank, rather than
    # surfacing established MLBers at the top of an Outlook-sorted list.
    league_for_resolve = (args.league.strip().lower()
                          if args.league else infer_league_from_path(csv_path))
    pool_ids_path: Optional[Path] = None
    if not args.no_draft_pool_filter:
        pool_ids_path = resolve_draft_pool_ids_path(
            args.draft_pool_ids, league_for_resolve, args.data_dir, csv_path,
        )
    if pool_ids_path is not None:
        if pool_ids_path.exists():
            id_filter = load_draft_pool_ids(pool_ids_path)
            if id_filter is None:
                print(f"WARN: draft pool ID file {pool_ids_path} unreadable — "
                      "no filter applied.", file=sys.stderr)
            else:
                pre = len(players)
                players = [
                    p for p in players
                    if str(get_column_value(p, "ID", "Player_ID", "PlayerID", "player_id") or "").strip()
                    in id_filter
                ]
                post = len(players)
                # ASCII arrow — Windows default cp1252 stdout can't encode '→'
                print(f"Applied draft pool filter from {pool_ids_path.name}: "
                      f"{pre} -> {post} players ({len(id_filter)} IDs in pool file, "
                      f"{post} matched).")
                if post == 0:
                    print("Error: filter eliminated every player. Check that the "
                          "ID file matches the eval's ID column type.", file=sys.stderr)
                    sys.exit(1)
        else:
            print(f"NOTE: draft pool ID file expected at {pool_ids_path} but not found — "
                  "no filter applied.", file=sys.stderr)

    # Outlook augmentation — requires PlayerData and the weights config.
    outlook_annotated = 0
    if not args.skip_outlook:
        league = args.league.strip().lower() if args.league else infer_league_from_path(csv_path)
        if league:
            player_data_lookup = load_player_data(league, data_dir=args.data_dir)
            cfg = load_weights_cfg(args.config_dir, args.weights_name)
            outlook_annotated = augment_with_outlook(players, player_data_lookup, cfg)
            if outlook_annotated:
                print(f"Computed Outlook for {outlook_annotated}/{len(players)} players")
        else:
            print("WARN: could not infer league from CSV path — skipping Outlook.",
                  file=sys.stderr)

    # Resolve output directory.
    if args.output_dir:
        output_dir = Path(args.output_dir)
    else:
        if args.name:
            folder_name = f"draft_pool_analysis_{args.name}"
        else:
            folder_name = f"draft_pool_analysis_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        league_slug = (args.league.strip().lower() if args.league
                       else infer_league_from_path(csv_path))
        if league_slug:
            output_dir = SCRIPT_DIR / league_slug / "drafts" / folder_name
        else:
            output_dir = SCRIPT_DIR / folder_name

    output_dir.mkdir(parents=True, exist_ok=True)

    print("\nAnalyzing position distribution...")
    position_counts = analyze_position_distribution(players)
    print("Analyzing position strength...")
    position_strength, position_strength_axis = analyze_position_strength(players)
    print("Categorizing prospects...")
    categories, tier_stats = categorize_prospects(players)

    total = len(players)

    print("\nGenerating reports...")
    generate_summary_report(
        players, position_counts, position_strength, categories,
        output_dir / "00_summary.txt",
        outlook_annotated=outlook_annotated,
        axis_label=position_strength_axis,
    )
    print("  [ok] Summary report")
    generate_position_distribution_report(
        position_counts, total, output_dir / "01_position_distribution.txt",
    )
    print("  [ok] Position distribution report")
    generate_position_strength_report(
        position_strength, output_dir / "02_position_strength.txt",
        axis_label=position_strength_axis,
    )
    print("  [ok] Position strength report")
    generate_ideal_value_distribution_report(
        players, output_dir / "03_ideal_value_distribution.txt",
    )
    print("  [ok] Ideal Value distribution report")
    generate_prospect_tier_report(
        categories, tier_stats, total, output_dir / "04_prospect_tiers.txt",
        axis_label=position_strength_axis,
    )
    print("  [ok] Prospect tier report")
    generate_csv_summary(
        position_counts, position_strength, categories, total,
        output_dir / "summary_data.csv",
    )
    print("  [ok] CSV summary")
    generate_summary_md(
        position_counts, position_strength, categories, total,
        output_dir / "summary_data.md",
    )
    print("  [ok] Summary data MD")
    generate_draft_pool_markdown(players, output_dir / "05_draft_pool.md")
    print("  [ok] Draft pool Markdown")

    print("\n" + "=" * 80)
    print("DRAFT POOL ANALYSIS COMPLETE")
    print("=" * 80)
    print(f"\nAnalysis package saved to: {output_dir}")
    print("\nGenerated files:")
    print("  - 00_summary.txt (Quick reference summary)")
    print("  - 01_position_distribution.txt")
    print("  - 02_position_strength.txt")
    print("  - 03_ideal_value_distribution.txt")
    print("  - 04_prospect_tiers.txt")
    print("  - summary_data.csv (Data for further analysis)")
    print("  - summary_data.md (Obsidian quick-reference)")
    print("  - 05_draft_pool.md (Draft pool table w/ v10 columns)")


if __name__ == "__main__":
    main()
