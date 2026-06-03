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

import os
from typing import Callable

from personal_mem.core.config import Config
from personal_mem.surfaces.mcp.tools import (
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
    "mem_create": notes.handle_create,
    "mem_read": notes.handle_read,
    "mem_link": notes.handle_link,
    "mem_update": notes.handle_update,
    "mem_unlink": notes.handle_unlink,
    # search / context / timeline / snapshot
    "mem_search": search.handle_search,
    "mem_context": search.handle_context,
    "mem_timeline": search.handle_timeline,
    "mem_project_snapshot": search.handle_project_snapshot,
    # graph (filter-dispatched)
    "mem_graph": graph.handle_dispatch,
    # concepts (action-dispatched)
    "mem_concepts": concepts.handle_dispatch,
    # extract / judge / landing / enrich
    "mem_extract": extract.handle_extract,
    "mem_judge": extract.handle_judge,
    "mem_landing": extract.handle_landing,
    "mem_enrich": extract.handle_enrich,
    # queue / config / prompts
    "mem_queue": queue.handle,
    "mem_sources_config": config.handle,
    "mem_prompts": prompts.handle_textcontent,
}


def dispatch(cfg: Config, name: str, arguments: dict) -> list:
    """Resolve ``name`` to its handler and run it, exporting the call's session
    id so nested Layer-B spend attributes to the right session.

    The long-lived MCP server process serves many Claude sessions and the MCP
    request envelope carries no session id of its own — the only session id
    available is the one a tool explicitly takes as an argument (``mem_create`` /
    ``mem_extract`` / ``mem_judge``). When present, we set
    ``PERSONAL_MEM_SESSION_ID`` for the duration of the handler so any
    ``record_spend`` fired deep inside (e.g. the OpenAI enrich call inside an
    extract path) lands in that session's ledger instead of the headless log.
    This is best-effort by design: tools that take no ``session_id`` still
    attribute their internal spend to the headless log — the one unavoidable
    limit of capturing spend in a session-agnostic server process.

    Returns the "Unknown tool" sentinel (matching the server's own contract)
    when ``name`` is not registered.
    """
    from mcp.types import TextContent

    handler = DISPATCH.get(name)
    if handler is None:
        return [TextContent(type="text", text=f"Unknown tool: {name}")]

    sid = (arguments or {}).get("session_id")
    if not sid:
        return handler(cfg, arguments)

    prev = os.environ.get("PERSONAL_MEM_SESSION_ID")
    os.environ["PERSONAL_MEM_SESSION_ID"] = str(sid)
    try:
        return handler(cfg, arguments)
    finally:
        if prev is None:
            os.environ.pop("PERSONAL_MEM_SESSION_ID", None)
        else:
            os.environ["PERSONAL_MEM_SESSION_ID"] = prev


__all__ = ["all_schemas", "DISPATCH", "dispatch"]
