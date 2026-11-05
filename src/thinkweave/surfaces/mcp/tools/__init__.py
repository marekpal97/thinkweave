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

import json
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
    schema = _input_schemas().get(name)
    if schema is not None and isinstance(arguments, dict):
        unknown = _unknown_arguments(arguments, schema)
        if unknown:
            return [
                TextContent(
                    type="text",
                    text=_unknown_arguments_error(name, unknown),
                )
            ]
        arguments = _coerce(arguments, schema)
    return handler(cfg, arguments)


def _input_schemas() -> dict[str, dict]:
    global _SCHEMAS
    if _SCHEMAS is None:
        _SCHEMAS = {t.name: t.inputSchema for t in all_schemas()}
    return _SCHEMAS


_SCHEMAS: dict[str, dict] | None = None


def _unknown_arguments(arguments: dict, schema: dict) -> list[str]:
    """Return top-level argument keys not advertised by a tool schema."""
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        return []
    return sorted((str(key) for key in arguments if key not in properties), key=str)


def _unknown_arguments_error(name: str, unknown: list[str]) -> str:
    return (
        f"{name} rejected unknown argument(s): {', '.join(unknown)}. "
        "No changes were made."
    )


def _allowed_types(schema: dict) -> set[str]:
    kinds = schema.get("type")
    allowed = set(kinds) if isinstance(kinds, list) else {kinds} if kinds else set()
    for alt in schema.get("anyOf") or ():
        allowed |= _allowed_types(alt)
    return allowed


def _coerce(value, schema: dict):
    """Undo JSON-encoded strings where the schema wants an array or object.

    Some models (seen live: Qwen via OpenCode) serialise list/object
    arguments as JSON text — ``concepts="[\\"a\\", \\"b\\"]"``. Handlers
    then iterate the characters of a string, or drop the value. A string
    is replaced only when it parses as a JSON array/object and the schema
    accepts that shape at that position; anything else passes through
    untouched.
    """
    allowed = _allowed_types(schema)
    if isinstance(value, str) and allowed & {"array", "object"}:
        text = value.strip()
        if text[:1] in "[{":
            try:
                parsed = json.loads(text)
            except ValueError:
                parsed = None
            if (isinstance(parsed, list) and "array" in allowed) or (
                isinstance(parsed, dict) and "object" in allowed
            ):
                value = parsed
    if isinstance(value, dict):
        props = schema.get("properties") or {}
        return {k: _coerce(v, props[k]) if k in props else v for k, v in value.items()}
    if isinstance(value, list) and isinstance(schema.get("items"), dict):
        return [_coerce(v, schema["items"]) for v in value]
    return value


__all__ = ["all_schemas", "DISPATCH", "dispatch"]
