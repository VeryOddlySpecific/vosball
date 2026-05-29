#!/usr/bin/env python3
"""
README: prospect_rankings.py
============================

Purpose
-------
Generate player-level prospect rankings from a `evaluation_summary_*.csv` file,
focused on upside/ceiling instead of present-day org asset value.

How this differs from farm_value_old.py
-----------------------------------
- `farm_value_old.py` asks: "What is each player worth to the org right now?"
- `prospect_rankings.py` asks: "Who are the best prospects by ceiling?"
- This script does NOT apply level/proximity discounts.
- This script DOES apply age-for-level adjustment (young-for-level bonus,
  old-for-level penalty), using the same curve/constants as `farm_value_old.py`.
- Optional position/role adjustment can be enabled/disabled with
  `--disable-position-adjust`.

Scoring
-------
`prospect_score = VOS_Potential * m_age * m_pos_role`

Where:
- `m_age` uses the same age-for-level function and defaults from `farm_value_old.py`.
- `m_pos_role` uses the same RP debuff and C/SS/CF premium boost logic.

Eligibility
-----------
- Always excludes `League_Level == ML`.
- If `/players` API data is available, it applies the same prospect service-time
  thresholds as `farm_value_old.py`:
  - `mlb_service_days <= --prospect-max-mlb-days` (default 90)
- `pro_service_years < --prospect-max-pro-years` (default 7.0)
- If API data is unavailable, all non-ML rows are included.

Output
------
Writes a player-level CSV with:
- `prospect_rank_overall` (across all orgs by `prospect_score`)
- `prospect_rank_org` (within each org by `prospect_score`)
- core player identifiers + scoring components

Default output filename:
- `prospect_rankings_{league}_{timestamp}.csv`

Quick CLI examples
------------------
- Auto-pick latest file for a league (full list):
  `python prospect_rankings.py --league woba`

- Use an explicit input file:
  `python prospect_rankings.py --input evaluation_summary_woba_20260410_093926.csv`

- Top 100 only:
  `python prospect_rankings.py --league woba --top-n 100`

- Single-org board:
  `python prospect_rankings.py --league woba --org ATL`

- Filter to orgs listed in a JSON file:
  `python prospect_rankings.py --league tlg --org-config config/tlg_orgs.json`

- Disable position/role adjustment:
  `python prospect_rankings.py --league woba --disable-position-adjust`

- Tune age curve:
  `python prospect_rankings.py --league woba --age-young-bonus-per-year 0.05 --age-old-penalty-per-year 0.07`

- Custom output path:
  `python prospect_rankings.py --league woba --output-players woba_prospect_board.csv`

- Write org count summary too:
  `python prospect_rankings.py --league woba --output-org-summary woba_prospect_org_counts.csv`
"""

from __future__ import annotations

import argparse
import json
import logging
import math
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.error import URLError

from farm_value_old import (
    BASELINE_AGE,
    age_for_level_multiplier,
    build_players_lookup,
    canonical_position_bucket,
    infer_league_slug,
    projected_role_field,
    read_csv_rows,
    resolve_base_url,
    resolve_input_path,
    role_static_multiplier,
    to_float,
    validate_columns,
    write_csv,
    write_md_table,
)

logger = logging.getLogger(__name__)
SCRIPT_DIR = Path(__file__).resolve().parent


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Rank prospects by ceiling using VOS potential with age-for-level adjustments."
    )
    parser.add_argument("--input", type=Path, default=None, help="Path to evaluation_summary CSV.")
    parser.add_argument("--league", type=str, default=None, help="League slug (auto-picks latest summary file).")
    parser.add_argument(
        "--base-url",
        type=str,
        default=None,
        help="Override league API base URL (e.g. https://host/league/api).",
    )
    parser.add_argument(
        "--league-url-config",
        type=Path,
        default=SCRIPT_DIR / "config" / "league_url.json",
        help="JSON file with league->base_url mappings.",
    )
    parser.add_argument("--pot-col", type=str, default="VOS_Potential",
                        help="VOS column used as the prospect-score base (default VOS_Potential, "
                             "which v6 aliases to VOS_Reach). Use --score-source instead to pick "
                             "between Reach / Career / Blended by intent.")
    parser.add_argument("--vos-col", type=str, default="VOS_Score",
                        help="VOS current-value column (informational; not used in scoring).")
    parser.add_argument(
        "--score-source", choices=["reach", "career", "blended"], default=None,
        help="Which v6 score drives the prospect ranking. 'reach' (default behavior via the "
             "VOS_Potential alias) ranks by P(reach MLB) — the trained logistic model, best for "
             "raw upside. 'career' ranks by age-decayed Career projection — better for ranking "
             "near-MLB-ready prospects on contribution. 'blended' uses alpha*Reach + "
             "(1-alpha)*Career — combines ceiling and projection. When set, overrides --pot-col."
    )
    parser.add_argument(
        "--top-n",
        type=int,
        default=0,
        help="How many prospects to include (default: all, 0 = all).",
    )
    parser.add_argument("--org", type=str, default=None, help="Filter output to a single org.")
    parser.add_argument(
        "--org-config",
        type=Path,
        default=None,
        help="Optional JSON file listing org names to include (e.g. config/tlg_orgs.json).",
    )
    parser.add_argument(
        "--pool",
        type=str,
        choices=["prospects", "free_agents", "non_org", "all"],
        default="prospects",
        help=(
            "Player pool to rank: prospects (default, org-affiliated only), "
            "free_agents/non_org (blank Org rows), or all."
        ),
    )
    parser.add_argument(
        "--age-plateau-half-width",
        type=float,
        default=2.0,
        help="Years around typical age for level where m_age stays at 1.0 (each side).",
    )
    parser.add_argument(
        "--age-young-bonus-per-year",
        type=float,
        default=0.04,
        help="m_age bonus per year younger beyond the young threshold (capped by --age-m-max).",
    )
    parser.add_argument(
        "--age-old-penalty-per-year",
        type=float,
        default=0.06,
        help="m_age penalty per year older beyond the old threshold (floored by --age-m-min).",
    )
    parser.add_argument(
        "--age-m-min",
        type=float,
        default=0.70,
        help="Minimum m_age multiplier from age-for-level curve.",
    )
    parser.add_argument(
        "--age-m-max",
        type=float,
        default=1.15,
        help="Maximum m_age multiplier from age-for-level curve.",
    )
    parser.add_argument(
        "--disable-position-adjust",
        action="store_true",
        help="Skip RP/premium role multipliers.",
    )
    parser.add_argument(
        "--rp-debuff",
        type=float,
        default=0.93,
        help="Multiplier for projected RP/CL (and similar relief roles).",
    )
    parser.add_argument(
        "--premium-pos-boost",
        type=float,
        default=1.04,
        help="Multiplier for projected C, SS, CF.",
    )
    parser.add_argument(
        "--prospect-max-mlb-days",
        type=float,
        default=90.0,
        help="Prospect eligibility: /players mlb_service_days <= this.",
    )
    parser.add_argument(
        "--prospect-max-pro-years",
        type=float,
        default=7.0,
        help="Prospect eligibility: /players pro_service_years < this.",
    )
    parser.add_argument(
        "--output-players",
        type=Path,
        default=None,
        help="Output CSV path (default: prospect_rankings_{league}_{timestamp}.csv).",
    )
    parser.add_argument(
        "--output-org-summary",
        type=Path,
        default=None,
        help="Output CSV path for org prospect counts (default: prospect_rankings_org_summary_{league}_{timestamp}.csv).",
    )
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    return parser.parse_args()


def is_prospect_eligible(
    row: Dict[str, str],
    players_lookup: Optional[Dict[str, Dict[str, str]]],
    max_mlb_days: float,
    max_pro_years: float,
) -> bool:
    league_level = (row.get("League_Level") or "").strip()
    if league_level == "ML":
        return False
    if players_lookup is None:
        return True

    pid = (row.get("ID") or "").strip()
    if not pid:
        return True
    pmeta = players_lookup.get(pid)
    if pmeta is None:
        return True

    mlb_days = to_float(pmeta.get("mlb_service_days"), -1.0)
    pro_years = to_float(pmeta.get("pro_service_years"), -1.0)
    if mlb_days < 0 or pro_years < 0:
        return True
    if mlb_days > max_mlb_days:
        return False
    if pro_years >= max_pro_years:
        return False
    return True


def row_pool_type(row: Dict[str, str]) -> str:
    org = (row.get("Org") or "").strip()
    if org:
        return "org_affiliated"
    return "free_agent"


def compute_prospect_rows(
    rows: List[Dict[str, str]],
    vos_col: str,
    pot_col: str,
    players_lookup: Optional[Dict[str, Dict[str, str]]],
    prospect_max_mlb_days: float,
    prospect_max_pro_years: float,
    age_plateau_half_width: float,
    age_young_bonus_per_year: float,
    age_old_penalty_per_year: float,
    age_m_min: float,
    age_m_max: float,
    position_adjust: bool,
    rp_debuff: float,
    premium_pos_boost: float,
    pool: str,
) -> List[Dict[str, object]]:
    out: List[Dict[str, object]] = []
    for r in rows:
        pool_type = row_pool_type(r)
        is_org_affiliated = pool_type == "org_affiliated"
        is_free_agent = pool_type == "free_agent"

        if pool == "prospects":
            if not is_org_affiliated:
                continue
            if not is_prospect_eligible(
                r,
                players_lookup,
                prospect_max_mlb_days,
                prospect_max_pro_years,
            ):
                continue
        elif pool in ("free_agents", "non_org"):
            if not is_free_agent:
                continue
            league_level = (r.get("League_Level") or "").strip()
            if league_level == "ML":
                continue
        elif pool == "all":
            if is_org_affiliated and not is_prospect_eligible(
                r,
                players_lookup,
                prospect_max_mlb_days,
                prospect_max_pro_years,
            ):
                continue
            league_level = (r.get("League_Level") or "").strip()
            if is_free_agent and league_level == "ML":
                continue

        org = (r.get("Org") or "").strip()
        team = (r.get("Team") or "").strip()
        org_board = org if org else "Free Agents"
        league_level = (r.get("League_Level") or "").strip()
        vos = to_float(r.get(vos_col), 0.0)
        vos_pot_raw = to_float(r.get(pot_col), float("nan"))
        vos_pot = vos if math.isnan(vos_pot_raw) else vos_pot_raw
        vos_gap = max(0.0, vos_pot - vos)
        age = to_float(r.get("Age"), float("nan"))

        base_age = BASELINE_AGE.get(league_level)
        age_dev = float("nan")
        if base_age is None or math.isnan(age):
            m_age = 1.0
        else:
            m_age, age_dev = age_for_level_multiplier(
                age,
                float(base_age),
                age_plateau_half_width,
                age_young_bonus_per_year,
                age_old_penalty_per_year,
                age_m_min,
                age_m_max,
            )

        pos_bucket = canonical_position_bucket(r)
        proj_role = projected_role_field(r)
        m_pos_role = (
            role_static_multiplier(pos_bucket, rp_debuff, premium_pos_boost) if position_adjust else 1.0
        )
        prospect_score = vos_pot * m_age * m_pos_role

        out.append(
            {
                "ID": r.get("ID", ""),
                "Name": r.get("Name", ""),
                "Org": org,
                "Team": team,
                "pool_type": pool_type,
                "org_board": org_board,
                "League_Level": league_level,
                "Age": r.get("Age", ""),
                "Pos": r.get("Pos", ""),
                "projected_role": proj_role,
                "pos_bucket": pos_bucket,
                "vos": round(vos, 4),
                "vos_pot": round(vos_pot, 4),
                "vos_gap": round(vos_gap, 4),
                "age_baseline_level": round(base_age, 2) if base_age is not None else "",
                "age_dev_vs_level": round(age_dev, 2) if not math.isnan(age_dev) else "",
                "m_age": round(m_age, 4),
                "m_pos_role": round(m_pos_role, 4),
                "prospect_score": round(prospect_score, 4),
            }
        )
    return out


def assign_rankings(rows: List[Dict[str, object]]) -> None:
    for r in rows:
        r["prospect_rank_overall"] = ""
        r["prospect_rank_org"] = ""

    sort_key = lambda r: (
        -float(r["prospect_score"]),
        -float(r["vos_pot"]),
        str(r.get("Name", "")),
        str(r.get("ID", "")),
    )

    for i, r in enumerate(sorted(rows, key=sort_key), start=1):
        r["prospect_rank_overall"] = i

    by_org: Dict[str, List[Dict[str, object]]] = {}
    for r in rows:
        by_org.setdefault(str(r.get("org_board", "")), []).append(r)
    for org_rows in by_org.values():
        for i, r in enumerate(sorted(org_rows, key=sort_key), start=1):
            r["prospect_rank_org"] = i


def rankings_prefix_for_pool(pool: str) -> str:
    if pool == "free_agents":
        return "free_agent_rankings"
    if pool == "non_org":
        return "non_org_rankings"
    if pool == "all":
        return "all_player_rankings"
    return "prospect_rankings"


def default_players_output_path(input_path: Path, league_slug: Optional[str], run_ts: str, pool: str) -> Path:
    prefix = rankings_prefix_for_pool(pool)
    if league_slug:
        name = f"{prefix}_{league_slug}_{run_ts}.csv"
        out = SCRIPT_DIR / league_slug / "prospects" / name
    else:
        name = f"{prefix}_{run_ts}.csv"
        out = input_path.with_name(name)
    out.parent.mkdir(parents=True, exist_ok=True)
    return out


def default_org_summary_output_path(input_path: Path, league_slug: Optional[str], run_ts: str, pool: str) -> Path:
    prefix = rankings_prefix_for_pool(pool)
    if league_slug:
        name = f"{prefix}_org_summary_{league_slug}_{run_ts}.csv"
        out = SCRIPT_DIR / league_slug / "prospects" / name
    else:
        name = f"{prefix}_org_summary_{run_ts}.csv"
        out = input_path.with_name(name)
    out.parent.mkdir(parents=True, exist_ok=True)
    return out


def maybe_filter_org(rows: List[Dict[str, object]], org: Optional[str]) -> List[Dict[str, object]]:
    if not org or not org.strip():
        return rows
    target = org.strip().lower()
    return [r for r in rows if str(r.get("Org", "")).strip().lower() == target]


def maybe_filter_org_config(rows: List[Dict[str, object]], orgs: Optional[List[str]]) -> List[Dict[str, object]]:
    if not orgs:
        return rows
    allow = {o.strip().lower() for o in orgs if o and o.strip()}
    if not allow:
        return rows
    return [r for r in rows if str(r.get("Org", "")).strip().lower() in allow]


def maybe_limit_top_n(rows: List[Dict[str, object]], top_n: int) -> List[Dict[str, object]]:
    if top_n <= 0:
        return rows
    return rows[:top_n]


def load_players_lookup(
    league: Optional[str],
    base_url_override: Optional[str],
    league_url_config: Path,
) -> Tuple[Optional[Dict[str, Dict[str, str]]], Optional[str]]:
    base_url = resolve_base_url(league, base_url_override, league_url_config)
    if not base_url:
        logger.warning("No league/base-url provided for /players filter; including all non-ML players.")
        return None, None
    try:
        players = build_players_lookup(base_url)
        logger.info("Loaded %d /players rows from %s", len(players), base_url)
        return players, base_url
    except (URLError, TimeoutError, ValueError) as e:
        logger.warning("Failed to load /players data from %s: %s", base_url, e)
        logger.warning("Falling back to non-ML inclusion without service-time filtering.")
        return None, base_url


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


def summarize_org_counts(rows: List[Dict[str, object]]) -> List[Dict[str, object]]:
    counts: Dict[str, int] = {}
    for r in rows:
        org = str(r.get("org_board", "")).strip()
        if not org:
            continue
        counts[org] = counts.get(org, 0) + 1
    out: List[Dict[str, object]] = []
    for org, count in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])):
        out.append({"Org": org, "prospects_ranked": count})
    return out


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
        logger.info("Score source = %s, prospect-score base column = %s",
                    args.score_source, args.pot_col)

    _eval_search_dir = SCRIPT_DIR / args.league / "eval" if args.league else Path.cwd()
    input_path = resolve_input_path(args.input, args.league, _eval_search_dir)
    if not input_path.exists():
        raise FileNotFoundError(f"Input not found: {input_path}")
    logger.info("Using input file: %s", input_path)

    rows, fieldnames = read_csv_rows(input_path)
    validate_columns(fieldnames, ["ID", "Name", "Org", "Team", "League_Level", "Age", "Pos", args.vos_col, args.pot_col])

    league_slug = infer_league_slug(input_path, args.league)
    players_lookup, _ = load_players_lookup(league_slug or args.league, args.base_url, args.league_url_config)
    orgs_from_config = load_orgs_config(args.org_config)
    if orgs_from_config is not None:
        logger.info("Loaded %d org filters from %s", len(orgs_from_config), args.org_config)

    prospect_rows = compute_prospect_rows(
        rows=rows,
        vos_col=args.vos_col,
        pot_col=args.pot_col,
        players_lookup=players_lookup,
        prospect_max_mlb_days=args.prospect_max_mlb_days,
        prospect_max_pro_years=args.prospect_max_pro_years,
        age_plateau_half_width=args.age_plateau_half_width,
        age_young_bonus_per_year=args.age_young_bonus_per_year,
        age_old_penalty_per_year=args.age_old_penalty_per_year,
        age_m_min=args.age_m_min,
        age_m_max=args.age_m_max,
        position_adjust=not args.disable_position_adjust,
        rp_debuff=args.rp_debuff,
        premium_pos_boost=args.premium_pos_boost,
        pool=args.pool,
    )
    logger.info("Rows included for pool '%s': %d", args.pool, len(prospect_rows))

    assign_rankings(prospect_rows)
    sorted_rows = sorted(
        prospect_rows,
        key=lambda r: (
            -float(r["prospect_score"]),
            -float(r["vos_pot"]),
            str(r.get("Name", "")),
            str(r.get("ID", "")),
        ),
    )
    filtered_rows = maybe_filter_org_config(sorted_rows, orgs_from_config)
    filtered_rows = maybe_filter_org(filtered_rows, args.org)
    output_rows = maybe_limit_top_n(filtered_rows, args.top_n)

    output_players = args.output_players or default_players_output_path(
        input_path, league_slug, run_ts, args.pool
    )
    output_fields = [
        "prospect_rank_overall",
        "prospect_rank_org",
        "ID",
        "Name",
        "Org",
        "Team",
        "pool_type",
        "org_board",
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
    ]
    write_csv(output_players, output_rows, output_fields)
    logger.info("Wrote prospect rankings: %s", output_players)
    players_md = output_players.with_suffix(".md")
    md_fields = [
        "prospect_rank_overall",
        "prospect_rank_org",
        "Name",
        "Org",
        "org_board",
        "pool_type",
        "Pos",
        "League_Level",
        "Age",
        "vos",
        "vos_pot",
        "vos_gap",
        "projected_role",
        "prospect_score",
    ]
    write_md_table(players_md, output_rows, md_fields, title="Prospect Rankings", max_rows=150)
    logger.info("Wrote prospect rankings MD: %s", players_md)

    output_org_summary = args.output_org_summary or default_org_summary_output_path(
        input_path, league_slug, run_ts, args.pool
    )
    org_summary_rows = summarize_org_counts(output_rows)
    org_summary_fields = ["Org", "prospects_ranked"]
    write_csv(output_org_summary, org_summary_rows, org_summary_fields)
    logger.info("Wrote org prospect count summary: %s", output_org_summary)
    org_summary_md = output_org_summary.with_suffix(".md")
    write_md_table(org_summary_md, org_summary_rows, org_summary_fields, title="Prospect Count by Org")
    logger.info("Wrote org prospect count summary MD: %s", org_summary_md)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
