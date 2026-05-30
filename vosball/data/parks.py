"""vosball.data.parks — park-factors file loader.

Reads a park-factors JSON file (or returns None). Lifted verbatim from loaders.py."""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


__all__ = [
    'load_park_factors',
]


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
