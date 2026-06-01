"""``mem_prompts`` MCP tool.

Read-only listing of user prompts captured by the UserPromptSubmit hook
(Phase 4 E). Backed by :func:`personal_mem.operations.search.query_prompts`,
which walks per-session JSONL buffers — prompts live outside the SQLite
index because they're append-only event data, not knowledge.

Phase 4 H's ``/discover`` skill is the primary consumer: it cross-checks
under-covered concepts against the prompts the user has actually been
typing, so gap analysis is biased toward what the user already cares about.
"""

from __future__ import annotations

import json

from personal_mem.core.config import Config
from personal_mem.operations.search import query_prompts


TOOL_NAME = "mem_prompts"


def tool_schemas() -> list:
    """Return list of Tool schemas registered with the MCP server."""
    from mcp.types import Tool

    return [
        Tool(
            name=TOOL_NAME,
            description=(
                "Read-only listing of user prompts captured by the "
                "UserPromptSubmit hook (Phase 4 E).\n\n"
                "Returns prompts captured for a project, ordered by "
                "recency. Each entry has `ts`, `text`, `session_id`, "
                "`project`, `cwd`, and `classification` (``\"probe\"`` "
                "for exploratory user questions, ``null`` otherwise). "
                "Source data lives in per-session JSONL buffers, not "
                "the SQLite index — it's append-only event data.\n\n"
                "Use to ground gap analysis in what the user has "
                "actually been asking (e.g. /discover prioritises "
                "concepts mentioned in recent probe-classified prompts)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "project": {
                        "type": "string",
                        "description": "Project to scope to. Required.",
                    },
                    "since": {
                        "type": "string",
                        "description": (
                            "Earliest ISO date/datetime (YYYY-MM-DD or "
                            "full ISO timestamp). Inclusive. Optional."
                        ),
                    },
                    "limit": {
                        "type": "integer",
                        "default": 50,
                        "description": "Max prompts to return.",
                    },
                    "classified_as": {
                        "type": "string",
                        "description": (
                            "Optional classification filter (e.g. "
                            "``\"probe\"`` to keep only exploratory "
                            "questions). Skipped when omitted."
                        ),
                    },
                },
                "required": ["project"],
            },
        ),
    ]


def tool_schema() -> dict:
    """Legacy single-tool descriptor — retained for back-compat with code
    that imported the dict directly. Prefer ``tool_schemas()``."""
    schemas = tool_schemas()
    return {
        "name": schemas[0].name,
        "description": schemas[0].description,
        "inputSchema": schemas[0].inputSchema,
    }


def handle(cfg: Config, args: dict) -> str:
    """Run ``mem_prompts`` and return a JSON-serialisable payload as a string.

    Returned string is the JSON-encoded list of prompt dicts ready for
    ``TextContent.text`` on the MCP surface.
    """
    project = args.get("project", "") or cfg.default_project
    since = args.get("since") or None
    limit = int(args.get("limit", 50))
    classified_as = args.get("classified_as") or None

    if not project:
        return json.dumps({"error": "project required"})

    rows = query_prompts(
        cfg,
        project=project,
        since=since,
        limit=limit,
        classified_as=classified_as,
    )
    return json.dumps(rows, indent=2)


def handle_textcontent(cfg: Config, args: dict):
    """Wrap :func:`handle` output in ``[TextContent]`` for the dispatch table."""
    from mcp.types import TextContent

    return [TextContent(type="text", text=handle(cfg, args))]
