"""MCP server for personal_mem.

Run: ``python -m personal_mem.surfaces.mcp.server``
Transport: stdio
Requires: ``pip install personal-mem[mcp]``

This module is a thin shell. Tool schemas + handlers live under
``surfaces/mcp/tools/`` (one module per area); the dispatch table is
assembled in ``tools/__init__.py``. The Phase-4-C deprecation aliases
(``mem_concept_search``, ``mem_source_lens``, ``mem_decisions_for_file``,
``mem_concepts_tighten``, ``mem_concepts_merge``,
``mem_concept_source_counts``, ``mem_concepts_drift``) were deleted
2026-05-21; calls to those names now return "Unknown tool".

Back-compat: a handful of helpers (``_parse_candidate_insights``,
``_flush_insight``, ``_build_decision_body``) historically lived here
and are re-exported below — the test suite reaches for them.
"""

from __future__ import annotations

import sys

# Back-compat re-exports — tests import these names from this module.
from personal_mem.surfaces.mcp.tools.extract import (
    _build_decision_body,
    _flush_insight,
    _parse_candidate_insights,
)


def main() -> None:
    try:
        import mcp.server.stdio
        from mcp.server import Server
        from mcp.types import TextContent, Tool  # noqa: F401
    except ImportError:
        print("MCP server requires: pip install personal-mem[mcp]", file=sys.stderr)
        sys.exit(1)

    import asyncio

    from personal_mem.core.config import load_config
    from personal_mem.surfaces.mcp.tools import all_schemas, dispatch

    server = Server("personal-mem")
    cfg = load_config()

    @server.list_tools()
    async def list_tools() -> list[Tool]:
        return all_schemas()

    @server.call_tool()
    async def call_tool(name: str, arguments: dict) -> list[TextContent]:
        return dispatch(cfg, name, arguments)

    async def run():
        async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
            await server.run(
                read_stream,
                write_stream,
                server.create_initialization_options(),
            )

    asyncio.run(run())


def build_server():
    """Construct + return the MCP Server (no transport). Smoke-test entry point."""
    try:
        from mcp.server import Server
        from mcp.types import TextContent, Tool  # noqa: F401
    except ImportError as exc:
        raise RuntimeError("MCP server requires: pip install personal-mem[mcp]") from exc

    from personal_mem.core.config import load_config
    from personal_mem.surfaces.mcp.tools import all_schemas, dispatch

    server = Server("personal-mem")
    cfg = load_config()

    @server.list_tools()
    async def list_tools() -> list[Tool]:
        return all_schemas()

    @server.call_tool()
    async def call_tool(name: str, arguments: dict) -> list[TextContent]:
        return dispatch(cfg, name, arguments)

    return server


__all__ = [
    "main",
    "build_server",
    "_parse_candidate_insights",
    "_flush_insight",
    "_build_decision_body",
]


if __name__ == "__main__":
    main()
