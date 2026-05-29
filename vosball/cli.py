"""vosball.cli — command-line entrypoint for VOS evaluation.

main() parses args, loads data/config/players via vosball.data, scores them
via vosball.engine, and writes output via vosball.reporting. It takes an
optional app_root (default: cwd) that anchors the default data/config dirs
and the default <root>/<league>/eval/ output location; run_vos.py passes its
own directory so behavior is identical to the pre-refactor CLI.
"""
from __future__ import annotations

import argparse
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.error import URLError

from vosball.engine import build_hitter_row, build_pitcher_row, is_pitcher
from vosball.data import (
    load_id_filter, load_weights, load_id_maps, load_teams,
    load_league_api_base_urls, load_park_factors, load_player_data,
    get_league_base_url, load_contract_data, attach_contract_fields,
    WEIGHTS_FILENAME, LEAGUE_URLS_FILENAME, RATING_SCALES, DEFAULT_RATING_SCALE,
)
from vosball.reporting import write_output_csv, _write_eval_summary_md

logger = logging.getLogger(__name__)


def main(argv: Optional[List[str]] = None, app_root: Optional[Path] = None) -> int:
    # app_root anchors the default data/config dirs and the default output
    # location (<root>/<league>/eval/...). run_vos.py passes its own directory;
    # `python -m vosball.cli` falls back to the current working directory.
    app_root = Path(app_root) if app_root is not None else Path.cwd()
    default_data_dir = app_root / "data"
    default_config_dir = app_root / "config"
    parser = argparse.ArgumentParser(
        description="VOS v5: two-track (Reach + Career + Blended) player evaluation.")
    parser.add_argument("--league", required=True, help="League slug (e.g. woba, sky)")
    parser.add_argument("--output", default=None, help="Output CSV path (default: evaluation_summary_{league}_{timestamp}.csv)")
    parser.add_argument("--ids-file", default=None, type=Path, help="Optional file of player IDs to include")
    parser.add_argument("--park-factors", default=None, type=str, help="Optional path to park-factors.json")
    parser.add_argument("--draft", action="store_true", help="Enable draft-specific adjustments (readiness, draft_age, draft_role)")
    parser.add_argument("--contracts", action="store_true", help="Include contract and contractextension API data in output")
    parser.add_argument("--base-url", default=None, type=str, help="Override league API base URL")
    parser.add_argument("--data-dir", type=Path, default=default_data_dir, help="Data directory")
    parser.add_argument("--config-dir", type=Path, default=default_config_dir, help="Config directory")
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
    args = parser.parse_args(argv)

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
                app_root / league / "eval" / team_code
                / f"{out_prefix}_{league}_{ts}.csv"
            )
            logger.info("=" * 60)
            logger.info("Per-org eval: %s (%s)", team_name, team_code)
            _run_eval_pass(synth_pf, out_path)
        return 0

    # Default: single eval pass with the park-factors as loaded.
    out_path = args.output
    if out_path is None:
        out_path = app_root / league / "eval" / f"{out_prefix}_{league}_{ts}.csv"
    else:
        out_path = Path(out_path)
    _run_eval_pass(park_factors, out_path)
    return 0
