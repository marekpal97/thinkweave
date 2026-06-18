"""Per-tool MCP modules.

Each tool file exposes:

- ``tool_schemas() -> list[Tool]`` — schema descriptors registered with
  the MCP server.
- one or more ``handle_*`` functions taking ``(cfg, args)`` and returning
  ``list[TextContent]``.

This module aggregates them into ``ALL_SCHEMAS`` (for ``list_tools``) and
``DISPATCH`` (for ``call_tool``). The server file is a thin shell over
those two structures.
"""

from __future__ import annotations

from typing import Callable

from thinkweave.core.config import Config
from thinkweave.surfaces.mcp.tools import (
    concepts,
    config,
    extract,
    graph,
    notes,
    prompts,
    queue,
    search,
)


def all_schemas() -> list:
    """Return the full list of Tool schemas to register with the server."""
    schemas: list = []
    schemas.extend(notes.tool_schemas())
    schemas.extend(search.tool_schemas())
    schemas.extend(graph.tool_schemas())
    schemas.extend(concepts.tool_schemas())
    schemas.extend(extract.tool_schemas())
    schemas.extend(queue.tool_schemas())
    schemas.extend(config.tool_schemas())
    schemas.extend(prompts.tool_schemas())
    return schemas


# name → handler(cfg, args) -> list[TextContent]
DISPATCH: dict[str, Callable[[Config, dict], list]] = {
    # notes
    "weave_create": notes.handle_create,
    "weave_read": notes.handle_read,
    "weave_link": notes.handle_link,
    "weave_update": notes.handle_update,
    "weave_unlink": notes.handle_unlink,
    # search / context / timeline / snapshot
    "weave_search": search.handle_search,
    "weave_context": search.handle_context,
    "weave_timeline": search.handle_timeline,
    "weave_project_snapshot": search.handle_project_snapshot,
    # graph (filter-dispatched)
    "weave_graph": graph.handle_dispatch,
    # concepts (action-dispatched)
    "weave_concepts": concepts.handle_dispatch,
    # extract / judge / landing
    "weave_extract": extract.handle_extract,
    "weave_judge": extract.handle_judge,
    "weave_landing": extract.handle_landing,
    # queue / config / prompts
    "weave_queue": queue.handle,
    "weave_sources_config": config.handle,
    "weave_prompts": prompts.handle_textcontent,
}


def dispatch(cfg: Config, name: str, arguments: dict) -> list:
    """Resolve ``name`` to its handler and run it.

    Returns the "Unknown tool" sentinel (matching the server's own contract)
    when ``name`` is not registered.
    """
    from mcp.types import TextContent

    handler = DISPATCH.get(name)
    if handler is None:
        return [TextContent(type="text", text=f"Unknown tool: {name}")]
    return handler(cfg, arguments)


__all__ = ["all_schemas", "DISPATCH", "dispatch"]
