#!/usr/bin/env python3
"""run_vos.py — entry point + back-compat shim for the VOS evaluator.

The VOS engine, data loaders, output writers, and CLI now live in the `vosball`
package (vosball.engine / vosball.data / vosball.reporting / vosball.cli). This
file remains for two reasons:

  1. Entry point — `python run_vos.py --league <slug> ...` still runs the full
     evaluation. It delegates to vosball.cli.main, anchored at this directory so
     default data/config dirs and the <root>/<league>/eval/ output location are
     unchanged.
  2. Back-compat — it re-exports the engine + data + reporting surface, so the
     existing importers (`import run_vos as v2` in player_card.py / what_if.py,
     `import run_vos` in lib/draft_score.py) resolve every name they used when
     this file was a single 2,100-line module.

Output is byte-identical to the pre-refactor engine (guarded by
tests/test_golden.py).
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent

# Ensure lib/ (a sibling of this file) is importable before the vosball package
# pulls in lib.vos_decay.
for _lib_root in (SCRIPT_DIR, SCRIPT_DIR.parent):
    if (_lib_root / "lib").is_dir():
        if str(_lib_root) not in sys.path:
            sys.path.insert(0, str(_lib_root))
        break

# --- Back-compat re-exports -------------------------------------------------
# The wildcard imports pull the full engine, data, and reporting surfaces
# (functions + constant tables) into this module, so every `run_vos.<name>` /
# `v2.<name>` reference that worked pre-refactor still resolves.
from vosball.engine import *      # noqa: E402,F401,F403
from vosball.data import *        # noqa: E402,F401,F403
from vosball.reporting import write_output_csv, _write_eval_summary_md  # noqa: E402,F401
from vosball.cli import main      # noqa: E402,F401

# Application paths: the suite's data/ and config/ live beside run_vos.py. The
# engine and data layers are path-agnostic, so these defaults stay with the app.
# Kept as module attributes for back-compat (player_card.py reads
# run_vos.DEFAULT_CONFIG_DIR).
DEFAULT_DATA_DIR = SCRIPT_DIR / "data"
DEFAULT_CONFIG_DIR = SCRIPT_DIR / "config"

# Configure root logging at import (preserves the prior side effect for callers
# that `import run_vos`); the CLI relies on it to surface INFO progress lines.
logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

if __name__ == "__main__":
    sys.exit(main(app_root=SCRIPT_DIR))
