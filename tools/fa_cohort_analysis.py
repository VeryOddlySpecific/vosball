#!/usr/bin/env python3
"""
fa_cohort_analysis.py — Bucket the current FA pool by player cohort.

Cross-references the latest eval CSV (FAs identified via blank Org column)
against the StatsPlus /players endpoint (draft_year, service-time fields)
to classify each FA into a cohort relative to a given engine-launch year.

Use case: testing whether a "weak FA pitching class" hypothesis is driven by
extension-stripping of the year-1 draft cohort, draft-pool decline of pitching,
aging of the pre-engine cohort, or some combination.

Cohorts (parameterized by --engine-launch-year, default 2036 for OOTP 26):
  pre_engine      draft_year < launch_year (real-life seeded + OOTP 25 carryovers)
  year1_draft     draft_year == launch_year (first OOTP 26 draft class)
  post_engine_fa  draft_year > launch_year, mlb_service_years >= 6 (rare at 6 in)
  released        not in above; reached FA via cut/release rather than service
  unknown         missing draft_year

Output:
  {league}/fa_cohort/fa_cohort_{league}_{ts}.md

Usage:
  py fa_cohort_analysis.py --league uba
  py fa_cohort_analysis.py --league uba --engine-launch-year 2036 --top-n 15
"""

from __future__ import annotations
# --- tools/ -> repo-root bootstrap (added during tools/ move) ---
import os as _os, sys as _sys
_ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
if _ROOT not in _sys.path:
    _sys.path.insert(0, _ROOT)
# --- end bootstrap ---


import argparse
import csv
import logging
import re
import statistics
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.error import URLError

import stats as sapi

logger = logging.getLogger(__name__)
SCRIPT_DIR = Path(__file__).resolve().parent.parent

# Pitcher position labels as they appear in the eval CSV's Pos column.
PITCHER_POS = {"SP", "RP", "P", "CL", "SU", "MR", "LR"}

# Default engine launch year — OOTP 26 launched at game-year 2036 in the
# user's UBA league. Override with --engine-launch-year for other leagues.
DEFAULT_ENGINE_LAUNCH_YEAR = 2036


# -----------------------------------------------------------------------------
# CLI
# -----------------------------------------------------------------------------

def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Bucket FA pool by cohort relative to engine launch.")
    p.add_argument("--league", required=True, help="League slug (e.g. uba).")
    p.add_argument("--engine-launch-year", type=int, default=DEFAULT_ENGINE_LAUNCH_YEAR,
                   help=f"In-game year of the OOTP version's first season. Default {DEFAULT_ENGINE_LAUNCH_YEAR}.")
    p.add_argument("--input", type=Path, default=None,
                   help="Eval CSV override. Default: latest under {league}/eval/.")
    p.add_argument("--output-dir", type=Path, default=None,
                   help="Output dir. Default: {league}/fa_cohort/.")
    p.add_argument("--base-url", type=str, default=None,
                   help="Override /players base URL.")
    p.add_argument("--top-n", type=int, default=10,
                   help="How many FAs per cohort/role to list in detail. Default 10.")
    p.add_argument("--no-players", action="store_true",
                   help="Skip /players fetch — every FA goes to 'unknown'. For dry runs.")
    p.add_argument("--log-level", default="INFO",
                   choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    return p.parse_args(argv)


# -----------------------------------------------------------------------------
# I/O
# -----------------------------------------------------------------------------

EVAL_FILE_RE = re.compile(r"^evaluation_summary_[a-z0-9]+_(\d{8}_\d{6})\.csv$")


def find_latest_eval(league: str) -> Path:
    eval_dir = SCRIPT_DIR / league / "eval"
    if not eval_dir.is_dir():
        raise FileNotFoundError(f"No eval directory: {eval_dir}")
    candidates: List[Tuple[str, Path]] = []
    for f in eval_dir.iterdir():
        if not f.is_file():
            continue
        m = EVAL_FILE_RE.match(f.name)
        if m:
            candidates.append((m.group(1), f))
    if not candidates:
        raise FileNotFoundError(f"No evaluation_summary_* files in {eval_dir}")
    candidates.sort(reverse=True)
    return candidates[0][1]


def read_eval(path: Path) -> Tuple[List[Dict[str, str]], List[str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        fieldnames = list(reader.fieldnames or [])
        rows = [dict(r) for r in reader]
    required = {"ID", "Name", "Pos", "Age", "Org", "VOS_Score", "VOS_Potential", "VOS_Tier"}
    missing = required - set(fieldnames)
    if missing:
        raise ValueError(f"Eval missing required columns: {sorted(missing)}")
    return rows, fieldnames


# -----------------------------------------------------------------------------
# Classification
# -----------------------------------------------------------------------------

def is_free_agent(row: Dict[str, str]) -> bool:
    """FA per the eval convention: blank Org column. Excludes retired (no Pos)."""
    org = (row.get("Org") or "").strip()
    pos = (row.get("Pos") or "").strip()
    return org == "" and pos != ""


def is_pitcher(pos: str) -> bool:
    return (pos or "").strip().upper() in PITCHER_POS


def to_float(v: Any) -> Optional[float]:
    if v is None:
        return None
    s = str(v).strip()
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def to_int(v: Any) -> Optional[int]:
    f = to_float(v)
    return int(f) if f is not None else None


def classify_cohort(
    pid: str,
    pmeta: Optional[Dict[str, str]],
    launch_year: int,
) -> str:
    """Bucket a player into one of five cohorts. See module docstring."""
    if pmeta is None:
        return "unknown"
    draft_year = to_int(pmeta.get("draft_year"))
    svc_years = to_float(pmeta.get("mlb_service_years"))
    pro_svc_years = to_float(pmeta.get("pro_service_years"))

    if draft_year is None or draft_year == 0:
        # Some int'l FA / undrafted players have draft_year=0. Fall back on
        # service time if available — long pro service ~= pre-engine vet.
        if pro_svc_years is not None and pro_svc_years >= (datetime.now().year - launch_year + 6):
            return "pre_engine"
        return "unknown"

    if draft_year < launch_year:
        return "pre_engine"
    if draft_year == launch_year:
        return "year1_draft"
    # draft_year > launch_year
    if svc_years is not None and svc_years >= 6.0:
        return "post_engine_fa"
    return "released"


# -----------------------------------------------------------------------------
# Aggregation
# -----------------------------------------------------------------------------

COHORT_ORDER = ["pre_engine", "year1_draft", "post_engine_fa", "released", "unknown"]
COHORT_LABELS = {
    "pre_engine":     "Pre-engine cohort (drafted before OOTP 26 launch)",
    "year1_draft":    "Year-1 draft cohort (first OOTP 26 draft class)",
    "post_engine_fa": "Post-engine FA (drafted after launch, 6+ MLB svc)",
    "released":       "Released / cut (not naturally FA)",
    "unknown":        "Unknown (missing /players match or draft_year)",
}
ROLE_ORDER = ["Hitter", "Pitcher"]


def summarize(values: List[float]) -> Dict[str, Optional[float]]:
    if not values:
        return {"n": 0, "mean": None, "median": None, "p75": None, "max": None}
    vs = sorted(values)
    n = len(vs)
    p75_idx = max(0, int(round(0.75 * (n - 1))))
    return {
        "n": n,
        "mean": round(statistics.fmean(vs), 2),
        "median": round(statistics.median(vs), 2),
        "p75": round(vs[p75_idx], 2),
        "max": round(vs[-1], 2),
    }


def bucket_fas(
    fa_rows: List[Dict[str, str]],
    players_lookup: Dict[str, Dict[str, str]],
    launch_year: int,
) -> Dict[Tuple[str, str], List[Dict[str, Any]]]:
    """Group FAs by (cohort, role) with attached metadata for reporting."""
    out: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
    for row in fa_rows:
        pid = (row.get("ID") or "").strip()
        pmeta = players_lookup.get(pid)
        cohort = classify_cohort(pid, pmeta, launch_year)
        role = "Pitcher" if is_pitcher(row.get("Pos", "")) else "Hitter"
        vos = to_float(row.get("VOS_Score"))
        pot = to_float(row.get("VOS_Potential"))
        age = to_float(row.get("Age"))
        draft_year = to_int(pmeta.get("draft_year")) if pmeta else None
        svc_years = to_float(pmeta.get("mlb_service_years")) if pmeta else None
        pro_svc = to_float(pmeta.get("pro_service_years")) if pmeta else None
        out.setdefault((cohort, role), []).append({
            "id": pid,
            "name": row.get("Name", ""),
            "pos": row.get("Pos", ""),
            "age": age,
            "vos": vos,
            "potential": pot,
            "tier": row.get("VOS_Tier", ""),
            "draft_year": draft_year,
            "mlb_service_years": svc_years,
            "pro_service_years": pro_svc,
        })
    return out


# -----------------------------------------------------------------------------
# Markdown rendering
# -----------------------------------------------------------------------------

def _fmt(x: Any) -> str:
    if x is None:
        return "-"
    if isinstance(x, float):
        return f"{x:g}"
    return str(x)


def render_markdown(
    league: str,
    eval_path: Path,
    launch_year: int,
    buckets: Dict[Tuple[str, str], List[Dict[str, Any]]],
    fa_total: int,
    matched: int,
    top_n: int,
) -> str:
    L: List[str] = []
    L.append(f"# FA Cohort Analysis — {league.upper()}")
    L.append("")
    L.append(f"- Eval: `{eval_path.name}`")
    L.append(f"- Engine launch year (cohort cutoff): **{launch_year}**")
    L.append(f"- Free agents in eval: **{fa_total}**")
    L.append(f"- Matched against /players: **{matched}** ({matched/fa_total*100:.1f}%)" if fa_total else "- (no FAs)")
    L.append("")
    L.append("**Cohort definitions** (vs --engine-launch-year):")
    for c in COHORT_ORDER:
        L.append(f"- `{c}` — {COHORT_LABELS[c]}")
    L.append("")

    # Summary matrix
    L.append("## Summary: VOS distribution by cohort × role")
    L.append("")
    L.append("| Cohort | Role | N | Mean | Median | P75 | Max |")
    L.append("|---|---|---:|---:|---:|---:|---:|")
    for cohort in COHORT_ORDER:
        for role in ROLE_ORDER:
            entries = buckets.get((cohort, role), [])
            vos_vals = [e["vos"] for e in entries if e["vos"] is not None]
            s = summarize(vos_vals)
            L.append(
                f"| {cohort} | {role} | {s['n']} | {_fmt(s['mean'])} | "
                f"{_fmt(s['median'])} | {_fmt(s['p75'])} | {_fmt(s['max'])} |"
            )
    L.append("")

    # Hitter-vs-pitcher gap by cohort (the headline test for the hypothesis)
    L.append("## Hitter vs Pitcher gap by cohort")
    L.append("")
    L.append("Larger gap (hitter median - pitcher median) within a cohort is the signal "
             "that role-specific extension-stripping or talent decline is at play.")
    L.append("")
    L.append("| Cohort | Hitter median | Pitcher median | Gap (H-P) | Hitter N | Pitcher N |")
    L.append("|---|---:|---:|---:|---:|---:|")
    for cohort in COHORT_ORDER:
        h = [e["vos"] for e in buckets.get((cohort, "Hitter"), []) if e["vos"] is not None]
        p = [e["vos"] for e in buckets.get((cohort, "Pitcher"), []) if e["vos"] is not None]
        h_med = round(statistics.median(h), 2) if h else None
        p_med = round(statistics.median(p), 2) if p else None
        gap = round(h_med - p_med, 2) if (h_med is not None and p_med is not None) else None
        L.append(
            f"| {cohort} | {_fmt(h_med)} | {_fmt(p_med)} | {_fmt(gap)} | {len(h)} | {len(p)} |"
        )
    L.append("")

    # Detailed top-N per cohort/role
    L.append(f"## Top {top_n} per cohort × role (by VOS)")
    L.append("")
    for cohort in COHORT_ORDER:
        L.append(f"### {COHORT_LABELS[cohort]}")
        L.append("")
        for role in ROLE_ORDER:
            entries = buckets.get((cohort, role), [])
            entries_sorted = sorted(
                entries,
                key=lambda e: (e["vos"] if e["vos"] is not None else -1),
                reverse=True,
            )[:top_n]
            L.append(f"**{role}s — {len(entries)} total**")
            L.append("")
            if not entries_sorted:
                L.append("_(none)_")
                L.append("")
                continue
            L.append("| Name | Pos | Age | VOS | Pot | Tier | Draft Yr | MLB Svc Yrs |")
            L.append("|---|---|---:|---:|---:|---|---:|---:|")
            for e in entries_sorted:
                L.append(
                    f"| {e['name']} | {e['pos']} | {_fmt(e['age'])} | {_fmt(e['vos'])} | "
                    f"{_fmt(e['potential'])} | {e['tier']} | {_fmt(e['draft_year'])} | "
                    f"{_fmt(e['mlb_service_years'])} |"
                )
            L.append("")
    return "\n".join(L) + "\n"


# -----------------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------------

def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=getattr(logging, args.log_level),
                        format="%(levelname)s: %(message)s")

    league = args.league.strip().lower()

    input_path = args.input or find_latest_eval(league)
    logger.info("Eval: %s", input_path)

    rows, _ = read_eval(input_path)
    fa_rows = [r for r in rows if is_free_agent(r)]
    logger.info("FAs in eval: %d (of %d total rows)", len(fa_rows), len(rows))

    players_lookup: Dict[str, Dict[str, str]] = {}
    if not args.no_players:
        base_url = sapi.resolve_base_url(league, args.base_url)
        if not base_url:
            logger.warning("No /players base URL for league '%s' — all FAs will be 'unknown'.",
                           league)
        else:
            cache_dir = SCRIPT_DIR / league / "cache" / "stats"
            try:
                players_lookup = sapi.build_players_lookup(base_url, cache_dir=cache_dir)
                logger.info("Loaded %d /players rows", len(players_lookup))
            except (URLError, TimeoutError, ValueError) as e:
                logger.warning("Failed to load /players (%s).", e)

    matched = sum(1 for r in fa_rows if (r.get("ID") or "").strip() in players_lookup)

    buckets = bucket_fas(fa_rows, players_lookup, args.engine_launch_year)

    md = render_markdown(
        league=league,
        eval_path=input_path,
        launch_year=args.engine_launch_year,
        buckets=buckets,
        fa_total=len(fa_rows),
        matched=matched,
        top_n=args.top_n,
    )

    out_dir = args.output_dir or (SCRIPT_DIR / league / "fa_cohort")
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = out_dir / f"fa_cohort_{league}_{ts}.md"
    out_path.write_text(md, encoding="utf-8")
    logger.info("Wrote %s", out_path)
    print(str(out_path))
    return 0


if __name__ == "__main__":
    sys.exit(main())
