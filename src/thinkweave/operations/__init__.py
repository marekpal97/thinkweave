"""Operations layer — the seam between surfaces (CLI, MCP) and the knowledge layer.

Both the `weave` CLI and the `weave_*` MCP tools call into these functions.
Each module owns one cross-cutting concern; surface handlers should be
5-10 line wrappers that translate input shape (argparse / JSON) into a call
into here, and translate the result into the surface's output (text or JSON).

The dependency rule: operations may import from `core/`, `retrieval/`,
`synthesis/`, `sources/`, but NEVER from `surfaces/`.
"""

from thinkweave.operations.migrations import migrate_todo_research_to_queue

__all__ = ["migrate_todo_research_to_queue"]
