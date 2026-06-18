"""Back-compat shim for the legacy ``thinkweave.mcp`` import path.

The MCP surface lives at :mod:`thinkweave.surfaces.mcp` after the
Phase-4 surfaces split. External configs (``~/.claude.json`` and the
equivalent in other projects) historically launched the server via
``python -m thinkweave.mcp.server``; this shim keeps those configs
working. Update configs to ``thinkweave.surfaces.mcp.server`` (or the
``weave-mcp`` console script) when convenient.
"""

from __future__ import annotations
