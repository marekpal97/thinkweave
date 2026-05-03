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


def tool_schema() -> dict:
    """Return the JSONSchema descriptor registered with the MCP server."""
    return {
        "name": TOOL_NAME,
        "description": (
            "Read-only listing of user prompts captured by the "
            "UserPromptSubmit hook.\n\n"
            "Returns prompts captured for a project, ordered by recency. "
            "Each entry has `ts`, `text`, `session_id`, `project`, and "
            "`cwd`. Source data lives in per-session JSONL buffers, not "
            "the SQLite index — it's append-only event data.\n\n"
            "Use to ground gap analysis in what the user has actually "
            "been asking (e.g. /discover prioritises concepts mentioned "
            "in recent prompts)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {
                    "type": "string",
                    "description": "Project to scope to. Required.",
                },
                "since": {
                    "type": "string",
                    "description": (
                        "Earliest ISO date/datetime (YYYY-MM-DD or full "
                        "ISO timestamp). Inclusive. Optional."
                    ),
                },
                "limit": {
                    "type": "integer",
                    "default": 50,
                    "description": "Max prompts to return.",
                },
            },
            "required": ["project"],
        },
    }


def handle(cfg: Config, args: dict) -> str:
    """Run ``mem_prompts`` and return a JSON-serialisable payload as a string.

    Returned string is the JSON-encoded list of prompt dicts ready for
    ``TextContent.text`` on the MCP surface.
    """
    project = args.get("project", "") or cfg.default_project
    since = args.get("since") or None
    limit = int(args.get("limit", 50))

    if not project:
        return json.dumps({"error": "project required"})

    rows = query_prompts(cfg, project=project, since=since, limit=limit)
    return json.dumps(rows, indent=2)
