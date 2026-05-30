"""vosball.data.loaders — back-compat re-export shim.

The loaders were split into config / players / contracts / parks (Phase-5
polish). This module re-exports all of them so the legacy import path
`from vosball.data.loaders import X` keeps working unchanged — notably
reporting.py imports CONTRACT_FIELDS straight from here, and vosball.data's
package __init__ does `from vosball.data.loaders import *`.
"""
from __future__ import annotations

from vosball.data import config as _config
from vosball.data import players as _players
from vosball.data import contracts as _contracts
from vosball.data import parks as _parks
from vosball.data.config import *  # noqa: F401,F403
from vosball.data.players import *  # noqa: F401,F403
from vosball.data.contracts import *  # noqa: F401,F403
from vosball.data.parks import *  # noqa: F401,F403

__all__ = (list(_config.__all__) + list(_players.__all__)
           + list(_contracts.__all__) + list(_parks.__all__))
