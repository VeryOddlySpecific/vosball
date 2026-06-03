#!/usr/bin/env python3
"""
Farm system valuation from latest prospect_rankings_* files.

This script reuses VPC dollar calibration from farm_value_old.py (MLB salaries vs
projected VOS) and applies that VPC to the latest prospect rankings board for a
league, then aggregates org totals with the same top-12 + weighted tail method.
"""

from __future__ import annotations
# --- repo-root + core/ path bootstrap ---
import os as _os, sys as _sys
_ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
for _p in (_ROOT, _os.path.join(_ROOT, "core")):
    if _p not in _sys.path:
        _sys.path.insert(0, _p)
# --- end bootstrap ---


import argparse
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional
from urllib.error import URLError

from farm_value_old import (
    compute_vpc_base,
    default_org_output_path,
    infer_league_slug,
    read_csv_rows,
    resolve_base_url,
    resolve_input_path,
    summarize_org_values,
    to_float,
    validate_columns,
    write_csv,
    write_md_table,
)

logger = logging.getLogger(__name__)
SCRIPT_DIR = Path(__file__).resolve().parent.parent


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Value farm systems from latest prospect_rankings CSV using VPC from evaluation summary."
    )
    parser.add_argument(
        "--rankings-input",
        type=Path,
        default=None,
        help="Path to prospect_rankings CSV. If omitted, auto-picks latest for --league.",
    )
    parser.add_argument(
        "--league",
        type=str,
        default=None,
        help="League slug (used to auto-pick latest rankings/evaluation_summary files).",
    )
    parser.add_argument(
        "--evaluation-input",
        type=Path,
        default=None,
        help="Path to evaluation_summary CSV used only for VPC calibration.",
    )
    parser.add_argument("--output-org", type=Path, default=None, help="Output CSV for org farm values.")
    parser.add_argument("--output-players", type=Path, default=None, help="Optional output CSV for player values.")
    parser.add_argument("--salary-col", type=str, default="Contract_salary0", help="Salary column for VPC.")
    parser.add_argument("--pot-col", type=str, default="VOS_Potential",
                        help="Projected VOS column for VPC calibration. Default VOS_Potential (which "
                             "v6 aliases to VOS_Reach). Use --score-source to pick by intent.")
    parser.add_argument(
        "--score-source", choices=["reach", "career", "blended"], default=None,
        help="Which v6 score VPC calibration regresses MLB salaries against. 'reach' (default "
             "behavior via the VOS_Potential alias) anchors $/Reach-unit — historically used but "
             "semantically odd in v6 since Reach is P(reach MLB) and MLB players already reached. "
             "'career' anchors $/Career-projection-unit — most defensible for MLB salary VPC. "
             "'blended' anchors $/(Reach+Career)-unit. When set, overrides --pot-col."
    )
    parser.add_argument("--vos-floor", type=float, default=25.0, help="Minimum MLB VOS for calibration.")
    parser.add_argument("--winsor-lower", type=float, default=0.025, help="Lower winsorization quantile.")
    parser.add_argument("--winsor-upper", type=float, default=0.975, help="Upper winsorization quantile.")
    parser.add_argument("--non40-only", action="store_true", help="Org totals use only non-40-man players.")
    parser.add_argument(
        "--org-include-non-prospects",
        action="store_true",
        help="Include non-prospect rows in org totals if present in rankings file.",
    )
    parser.add_argument(
        "--base-url",
        type=str,
        default=None,
        help="Override league API base URL for /players market-comp VPC filter.",
    )
    parser.add_argument(
        "--league-url-config",
        type=Path,
        default=SCRIPT_DIR / "config" / "league_url.json",
        help="JSON file with league->base_url mappings.",
    )
    parser.add_argument(
        "--org-config",
        type=Path,
        default=None,
        help="Optional JSON file listing org names to include (e.g. config/tlg_orgs.json).",
    )
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    return parser.parse_args()


def resolve_rankings_input_path(rankings_input: Optional[Path], league: Optional[str], search_dir: Path) -> Path:
    if rankings_input is not None:
        return rankings_input
    league_slug = (league or "").strip()
    if not league_slug:
        raise ValueError("Provide either --rankings-input or --league.")

    full_pattern = f"prospect_rankings_{league_slug}_*.csv"
    full_matches = list(search_dir.glob(full_pattern))
    if not full_matches:
        raise FileNotFoundError(f"No files found matching {full_pattern} in {search_dir}")

    # Prefer full-board outputs over trimmed exports (e.g. *_top100.csv).
    full_board = [p for p in full_matches if "_top" not in p.stem.lower()]
    candidates = full_board if full_board else full_matches
    return sorted(candidates, key=lambda p: p.name)[-1]


def load_players_lookup_for_vpc(
    league_slug: Optional[str],
    base_url_override: Optional[str],
    league_url_config: Path,
) -> Optional[Dict[str, Dict[str, str]]]:
    from farm_value_old import build_players_lookup

    base_url = resolve_base_url(league_slug, base_url_override, league_url_config)
    if not base_url:
        logger.warning("No league/base-url provided for /players VPC market-comp filter; using legacy VPC filter.")
        return None
    try:
        players = build_players_lookup(base_url)
        logger.info("Loaded %d /players rows from %s", len(players), base_url)
        return players
    except (URLError, TimeoutError, ValueError) as e:
        logger.warning("Failed to load /players data from %s: %s", base_url, e)
        logger.warning("Falling back to VPC calibration without /players market-comp filter.")
        return None


def build_player_rows(rank_rows: List[Dict[str, str]], vpc_base: float) -> List[Dict[str, object]]:
    out: List[Dict[str, object]] = []
    for r in rank_rows:
        org = (r.get("Org") or "").strip()
        if not org:
            continue

        prospect_score = to_float(r.get("prospect_score"), 0.0)
        farm_value = prospect_score * vpc_base

        out.append(
            {
                "ID": r.get("ID", ""),
                "prospect_rank_overall": r.get("prospect_rank_overall", ""),
                "prospect_rank_org": r.get("prospect_rank_org", ""),
                "Name": r.get("Name", ""),
                "Org": org,
                "Team": r.get("Team", ""),
                "League_Level": r.get("League_Level", ""),
                "Age": r.get("Age", ""),
                "Pos": r.get("Pos", ""),
                "projected_role": r.get("projected_role", ""),
                "pos_bucket": r.get("pos_bucket", ""),
                "vos": round(to_float(r.get("vos"), 0.0), 4),
                "vos_pot": round(to_float(r.get("vos_pot"), 0.0), 4),
                "vos_gap": round(to_float(r.get("vos_gap"), 0.0), 4),
                "age_baseline_level": r.get("age_baseline_level", ""),
                "age_dev_vs_level": r.get("age_dev_vs_level", ""),
                "m_age": round(to_float(r.get("m_age"), 1.0), 4),
                "m_pos_role": round(to_float(r.get("m_pos_role"), 1.0), 4),
                "prospect_score": round(prospect_score, 4),
                "vpc_base": round(vpc_base, 4),
                "farm_value": round(farm_value, 2),
                # Keep org aggregation contract-compatible with summarize_org_values.
                "is_non40": 1,
                "is_prospect_org": 1,
                "is_major": 0,
            }
        )
    return out


def rank_org_values(org_rows: List[Dict[str, object]],
                    key: str = "farm_value_total") -> List[Dict[str, object]]:
    """Stamp a 1-based ``rank`` and ``num_orgs`` ("4 of 30") onto each org row,
    ordered by ``key`` descending, in place. Returns the re-sorted list. Ties
    take distinct ranks in sorted order (stable)."""
    org_rows.sort(key=lambda r: float(r.get(key, 0.0) or 0.0), reverse=True)
    n = len(org_rows)
    for i, r in enumerate(org_rows, start=1):
        r["rank"] = i
        r["num_orgs"] = n
    return org_rows


def build_farm_values(
    rank_rows: List[Dict[str, str]],
    eval_rows: List[Dict[str, str]],
    *,
    salary_col: str = "Contract_salary0",
    pot_col: str = "VOS_Potential",
    vos_floor: float = 25.0,
    winsor_lower: float = 0.025,
    winsor_upper: float = 0.975,
    players_lookup: Optional[Dict[str, Dict[str, str]]] = None,
    non40_only: bool = False,
    org_include_non_prospects: bool = False,
    top_n: int = 12,
    tail_weight: float = 0.25,
) -> Dict[str, object]:
    """Calibrate VPC, value every prospect, and roll up *ranked* org totals.

    Pure (no I/O) — the shared seam for the CLI and the web UI, mirroring
    trade_targets.build_trade_targets / free_agent_market.compute_fa_fit.

    ``rank_rows`` are prospect-board rows (``prospect_score``, ``Org``, … — as
    produced by prospect_rankings.compute_prospect_rows or a
    prospect_rankings_*.csv board). ``eval_rows`` are evaluation-summary rows
    used *only* for VPC calibration; they need contract columns
    (``Contract_is_major`` / ``salary_col``) for a dollar VPC.

    When the eval has no MLB contract rows to calibrate against, VPC falls back
    to 1.0 and ``vpc_ok`` is False: farm_value then equals prospect_score, so the
    *ranking* is still correct (VPC is a global scalar) while the magnitudes are
    model points, not dollars.

    Returns ``{vpc_base, mlb_count, vpc_ok, player_rows, org_rows}`` with
    ``org_rows`` already ranked (``rank`` / ``num_orgs`` stamped) by
    farm_value_total.
    """
    try:
        vpc_base, mlb_count = compute_vpc_base(
            rows=eval_rows, salary_col=salary_col, calib_col=pot_col,
            vos_floor=vos_floor, winsor_lower=winsor_lower, winsor_upper=winsor_upper,
            players_lookup=players_lookup,
        )
        vpc_ok = True
    except ValueError as e:
        logger.warning(
            "VPC calibration failed (%s); falling back to VPC=1.0 -- farm values "
            "are model points, not dollars (org ranking is unaffected).", e)
        vpc_base, mlb_count, vpc_ok = 1.0, 0, False

    player_rows = build_player_rows(rank_rows, vpc_base)
    org_rows = summarize_org_values(
        player_rows, non40_only=non40_only, top_n=top_n, tail_weight=tail_weight,
        prospect_only_org=not org_include_non_prospects,
    )
    rank_org_values(org_rows)
    return {
        "vpc_base": vpc_base,
        "mlb_count": mlb_count,
        "vpc_ok": vpc_ok,
        "player_rows": player_rows,
        "org_rows": org_rows,
    }


def load_orgs_config(path: Optional[Path]) -> Optional[List[str]]:
    if path is None:
        return None
    if not path.exists():
        raise FileNotFoundError(f"Org config not found: {path}")
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, list):
        return [str(v).strip() for v in data if str(v).strip()]
    if isinstance(data, dict):
        orgs = data.get("orgs")
        if isinstance(orgs, list):
            return [str(v).strip() for v in orgs if str(v).strip()]
    raise ValueError("Org config must be a JSON array of org names or an object with an 'orgs' array.")


def maybe_filter_org_config_rows(rank_rows: List[Dict[str, str]], orgs: Optional[List[str]]) -> List[Dict[str, str]]:
    if not orgs:
        return rank_rows
    allow = {o.strip().lower() for o in orgs if o and o.strip()}
    if not allow:
        return rank_rows
    return [r for r in rank_rows if (r.get("Org") or "").strip().lower() in allow]


def main() -> int:
    args = parse_args()
    logging.basicConfig(level=getattr(logging, args.log_level), format="%(levelname)s: %(message)s")
    run_ts = datetime.now().strftime("%Y%m%d_%H%M%S")

    # --score-source resolves to the matching v6 column and overrides --pot-col.
    # Default (None) preserves the legacy --pot-col=VOS_Potential behavior.
    if args.score_source is not None:
        args.pot_col = {
            "reach":   "VOS_Reach",
            "career":  "VOS_Career",
            "blended": "VOS_Blended",
        }[args.score_source]
        logger.info("Score source = %s, VPC calibration column = %s",
                    args.score_source, args.pot_col)

    _ranks_search_dir = SCRIPT_DIR / args.league / "prospects" if args.league else Path.cwd()
    rankings_input_path = resolve_rankings_input_path(args.rankings_input, args.league, _ranks_search_dir)
    if not rankings_input_path.exists():
        raise FileNotFoundError(f"Rankings input not found: {rankings_input_path}")
    logger.info("Using rankings input file: %s", rankings_input_path)

    rank_rows, rank_fieldnames = read_csv_rows(rankings_input_path)
    validate_columns(
        rank_fieldnames,
        ["ID", "Name", "Org", "Team", "League_Level", "Age", "Pos", "prospect_score"],
    )
    orgs_from_config = load_orgs_config(args.org_config)
    if orgs_from_config is not None:
        logger.info("Loaded %d org filters from %s", len(orgs_from_config), args.org_config)
        rank_rows = maybe_filter_org_config_rows(rank_rows, orgs_from_config)
        logger.info("Rankings rows after --org-config filter: %d", len(rank_rows))

    league_slug = infer_league_slug(rankings_input_path, args.league)
    _eval_search_dir = SCRIPT_DIR / league_slug / "eval" if league_slug else Path.cwd()
    evaluation_input_path = resolve_input_path(args.evaluation_input, league_slug, _eval_search_dir)
    if not evaluation_input_path.exists():
        raise FileNotFoundError(f"Evaluation input not found: {evaluation_input_path}")
    logger.info("Using evaluation input file for VPC calibration: %s", evaluation_input_path)

    eval_rows, eval_fieldnames = read_csv_rows(evaluation_input_path)
    validate_columns(
        eval_fieldnames,
        ["ID", "Org", "League_Level", "Contract_is_major", args.salary_col, args.pot_col],
    )

    players_lookup = load_players_lookup_for_vpc(league_slug or args.league, args.base_url, args.league_url_config)
    fv_res = build_farm_values(
        rank_rows, eval_rows,
        salary_col=args.salary_col, pot_col=args.pot_col,
        vos_floor=args.vos_floor, winsor_lower=args.winsor_lower,
        winsor_upper=args.winsor_upper, players_lookup=players_lookup,
        non40_only=args.non40_only,
        org_include_non_prospects=args.org_include_non_prospects,
    )
    vpc_base = fv_res["vpc_base"]
    logger.info("Calibrated VPC (dollars per projected VOS): %.2f%s", vpc_base,
                "" if fv_res["vpc_ok"] else "  [fallback — model points, not dollars]")
    logger.info("MLB calibration sample size: %d", fv_res["mlb_count"])

    farm_rows = fv_res["player_rows"]
    logger.info("Prospect rows valued: %d", len(farm_rows))

    org_rows = fv_res["org_rows"]  # already ranked by build_farm_values

    output_org = args.output_org or default_org_output_path(rankings_input_path, league_slug, run_ts)
    org_fields = [
        "rank",
        "Org",
        "farm_value_total",
        "farm_value_top12",
        "farm_value_tail_weighted",
        "num_farm_players",
        "avg_value_per_player",
        "farm_value_non40",
        "num_non40",
        "num_orgs",
    ]
    write_csv(output_org, org_rows, org_fields)
    logger.info("Wrote organization farm values: %s", output_org)
    org_md = output_org.with_suffix(".md")
    write_md_table(
        org_md, org_rows, org_fields,
        title="Farm System Values by Org",
        dollar_cols=["farm_value_total", "farm_value_top12", "farm_value_tail_weighted", "farm_value_non40", "avg_value_per_player"],
    )
    logger.info("Wrote organization farm values MD: %s", org_md)

    if args.output_players:
        player_fields = [
            "prospect_rank_overall",
            "prospect_rank_org",
            "ID",
            "Name",
            "Org",
            "Team",
            "League_Level",
            "Age",
            "Pos",
            "projected_role",
            "pos_bucket",
            "vos",
            "vos_pot",
            "vos_gap",
            "age_baseline_level",
            "age_dev_vs_level",
            "m_age",
            "m_pos_role",
            "prospect_score",
            "vpc_base",
            "farm_value",
            "is_major",
            "is_non40",
            "is_prospect_org",
        ]
        sorted_players = sorted(farm_rows, key=lambda r: float(r["farm_value"]), reverse=True)
        write_csv(args.output_players, sorted_players, player_fields)
        logger.info("Wrote player farm value details: %s", args.output_players)
        players_md = Path(args.output_players).with_suffix(".md")
        md_player_fields = ["prospect_rank_overall", "prospect_rank_org", "Name", "Org", "Pos", "League_Level", "Age", "vos", "vos_pot", "prospect_score", "farm_value"]
        write_md_table(
            players_md, sorted_players, md_player_fields,
            title="Farm Player Values (Prospect Rankings)",
            max_rows=100,
            dollar_cols=["farm_value"],
        )
        logger.info("Wrote player farm value MD: %s", players_md)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
