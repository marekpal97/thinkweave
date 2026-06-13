"""Legacy entry point — re-exports :mod:`thinkweave.surfaces.mcp.server`.

Kept so ``python -m thinkweave.mcp.server`` continues to work for
external configs written before the Phase-4 surfaces split.
"""

from __future__ import annotations

from thinkweave.surfaces.mcp.server import *  # noqa: F401,F403
from thinkweave.surfaces.mcp.server import main


if __name__ == "__main__":
    main()
