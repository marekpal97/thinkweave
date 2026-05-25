"""Legacy entry point — re-exports :mod:`personal_mem.surfaces.mcp.server`.

Kept so ``python -m personal_mem.mcp.server`` continues to work for
external configs written before the Phase-4 surfaces split.
"""

from __future__ import annotations

from personal_mem.surfaces.mcp.server import *  # noqa: F401,F403
from personal_mem.surfaces.mcp.server import main


if __name__ == "__main__":
    main()
