"""Back-compat shim for the legacy ``personal_mem.mcp`` import path.

The MCP surface lives at :mod:`personal_mem.surfaces.mcp` after the
Phase-4 surfaces split. External configs (``~/.claude.json`` and the
equivalent in other projects) historically launched the server via
``python -m personal_mem.mcp.server``; this shim keeps those configs
working. Update configs to ``personal_mem.surfaces.mcp.server`` (or the
``mem-mcp`` console script) when convenient.
"""

from __future__ import annotations
