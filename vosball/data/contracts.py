"""vosball.data.contracts — StatsPlus contract endpoint fetchers.

Resolves a league's API base URL and pulls /contract + /contractextension, keeping the latest season per player and attaching the fields to an output row. Lifted verbatim from loaders.py."""
from __future__ import annotations

import csv
import logging
from io import StringIO
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.request import urlopen

logger = logging.getLogger(__name__)


__all__ = [
    'DEFAULT_LEAGUE_API_BASE_URLS',
    'CONTRACT_FIELDS',
    'get_league_base_url',
    '_fetch_csv_endpoint',
    '_season_year_value',
    '_build_contract_lookup',
    'load_contract_data',
    'attach_contract_fields',
]


DEFAULT_LEAGUE_API_BASE_URLS: Dict[str, str] = {}


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
