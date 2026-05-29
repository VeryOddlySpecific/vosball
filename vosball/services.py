"""vosball.services — UI-agnostic orchestration for VOS evaluation.

The seam between the engine/data layers and any front end. evaluate_players()
scores an already-loaded roster; evaluate_league() loads a league and scores it.
Both RETURN the evaluated rows (plain dicts) — no files written, no argv parsed,
no logging used for control flow — so a CLI, a web handler, or a notebook can
all drive the suite the same way. vosball.cli is now a thin wrapper over
evaluate_players; a future UI calls evaluate_league.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from vosball.engine import build_hitter_row, build_pitcher_row, is_pitcher
from vosball.data import (
    attach_contract_fields,
    get_league_base_url,
    load_contract_data,
    load_id_filter,
    load_id_maps,
    load_league_api_base_urls,
    load_park_factors,
    load_player_data,
    load_teams,
    load_weights,
    DEFAULT_RATING_SCALE,
    WEIGHTS_FILENAME,
)

logger = logging.getLogger(__name__)


def evaluate_players(
    players: List[Dict[str, str]],
    cfg: Dict[str, Any],
    league_lookup: Dict[int, str],
    teams: Dict[int, str],
    *,
    park_factors: Optional[Dict[str, Any]] = None,
    draft_mode: bool = False,
    contract_lookups: Optional[Tuple[Dict[str, Dict[str, str]], Dict[str, Dict[str, str]]]] = None,
) -> List[Dict[str, Any]]:
    """Score an already-loaded list of player rows; return the output rows.

    Pure: no file or network I/O. Pitchers route through build_pitcher_row,
    everyone else through build_hitter_row, against the given cfg / park context.
    If contract_lookups is provided — an (contract_lookup, extension_lookup) pair
    keyed by player_id — Contract_* / ContractExtension_* fields are attached to
    each row. This is the UI-agnostic core the CLI and any future UI share.
    """
    contract_lookup, extension_lookup = contract_lookups or ({}, {})
    include_contracts = contract_lookups is not None
    rows: List[Dict[str, Any]] = []
    for row in players:
        if is_pitcher(row):
            pos = (row.get("Pos") or "").strip().upper()
            role = "RP" if pos in ("RP", "CL") else "SP"
            out_row = build_pitcher_row(
                row, cfg, league_lookup, teams,
                role=role, park_factors=park_factors, draft_mode=draft_mode,
            )
        else:
            out_row = build_hitter_row(
                row, cfg, league_lookup, teams,
                park_factors=park_factors, draft_mode=draft_mode,
            )
        if out_row is None:
            logger.debug("Skipped row ID %s", row.get("ID"))
            continue
        rows.append(out_row)
        if include_contracts:
            pid = str(out_row.get("ID", "")).strip()
            attach_contract_fields(out_row, contract_lookup.get(pid),
                                   extension_lookup.get(pid))
    return rows


def evaluate_league(
    league: str,
    *,
    data_dir,
    config_dir,
    weights=None,
    ids_file=None,
    park_factors_path: Optional[str] = None,
    rating_scale: str = DEFAULT_RATING_SCALE,
    draft: bool = False,
    contracts: bool = False,
    base_url: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Load a league's data + config and return its scored rows.

    The UI-agnostic single-league entry point: a CLI, web handler, or notebook
    calls this and gets data back — no files written, no argv parsed. Raises
    ValueError / FileNotFoundError on fatal input problems (missing weights, no
    players, missing contract base URL) so the caller decides how to surface them.
    """
    data_dir = Path(data_dir)
    config_dir = Path(config_dir)
    id_filter = load_id_filter(Path(ids_file)) if ids_file else None
    cfg = load_weights(config_dir, Path(weights) if weights else None)
    if not cfg:
        raise ValueError(
            f"Weights config missing or invalid: {weights or (config_dir / WEIGHTS_FILENAME)}")
    league_lookup = load_id_maps(config_dir)
    teams = load_teams(config_dir, league)
    park_factors = load_park_factors(park_factors_path)
    players = load_player_data(data_dir, league, id_filter, rating_scale=rating_scale)
    if not players:
        raise ValueError(f"No players loaded for league {league!r}.")

    contract_lookups = None
    if contracts:
        resolved = get_league_base_url(league, base_url, load_league_api_base_urls(config_dir))
        if not resolved:
            raise ValueError(
                f"No base URL for league {league!r}; pass base_url or add it to league_url.json.")
        contract_lookups = load_contract_data(resolved, id_filter)

    return evaluate_players(
        players, cfg, league_lookup, teams,
        park_factors=park_factors, draft_mode=draft, contract_lookups=contract_lookups,
    )
