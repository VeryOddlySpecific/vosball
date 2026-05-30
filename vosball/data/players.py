"""vosball.data.players — PlayerData CSV loading + rating-scale conversion.

Loads the per-league PlayerData export and the optional ID filter, converting component ratings from 1-100 to 20-80 at load when asked. Path-agnostic. Lifted verbatim from loaders.py."""
from __future__ import annotations

import csv
import logging
from pathlib import Path
from typing import Dict, List, Optional, Set

logger = logging.getLogger(__name__)


__all__ = [
    'PLAYER_DATA_FILENAME_TEMPLATE',
    'RATING_SCALES',
    'DEFAULT_RATING_SCALE',
    'RATING_COLUMNS',
    'convert_1_100_to_20_80',
    'apply_rating_scale_to_row',
    'load_player_data',
    'load_id_filter',
]


PLAYER_DATA_FILENAME_TEMPLATE = "PlayerData-{league}.csv"


RATING_SCALES = ("20-80", "1-100")


DEFAULT_RATING_SCALE = "20-80"


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
