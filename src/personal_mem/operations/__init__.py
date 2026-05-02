"""Cross-cutting operations: migrations, drain handlers, queue helpers.

Phase 4 C2 will move more of the existing CLI/MCP plumbing in here. For
now this package is the seam where Phase 3 D's data migrations live.
"""

from personal_mem.operations.migrations import migrate_todo_research_to_queue

__all__ = ["migrate_todo_research_to_queue"]
