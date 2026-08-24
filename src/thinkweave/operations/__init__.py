"""Operations layer — the seam between surfaces (CLI, MCP) and the knowledge layer.

Both the `weave` CLI and the `weave_*` MCP tools call into these functions.
Each module owns one cross-cutting concern; surface handlers should be
5-10 line wrappers that translate input shape (argparse / JSON) into a call
into here, and translate the result into the surface's output (text or JSON).

The dependency rule: operations may import from `core/`, `retrieval/`,
`synthesis/`, `acquisition/`, but NEVER from `surfaces/`.

Operations: note CRUD (``create_note``/``update_note``/``link_notes``/
``unlink_notes``), session extraction + wrap tail (``extract_session``,
``finalize_wrap``), retrieval-shaped queries (``query_context``,
``query_prompts``), decision writeback (``judge_and_writeback`` — the
write-side half of the decision primitive; read-side judgment is
``synthesis.judge``), hub backfill (``run_hubs_batch``), dream cycle
bookkeeping (``maintenance_log_path``/``append_maintenance_log``,
``recent_reports``), RLVR export (``export_trajectory_rows``).

Invariants: every mutation goes through here so both surfaces stay
byte-equivalent (see tests/test_surface_contract.py).

Storage: none of its own — writes vault notes via ``core`` and reads the
derived index via ``retrieval``; reports land under ``.weave/reports/``.

Extension points: one module per new concern; queue-shaped concerns
follow ``rejudge_queue``/``seam_link_queue`` (consumed as modules).
"""

from thinkweave.operations.decisions import judge_and_writeback
from thinkweave.operations.dream import append_maintenance_log, maintenance_log_path
from thinkweave.operations.extract import extract_session
from thinkweave.operations.hubs_batch import run_hubs_batch
from thinkweave.operations.migrations import migrate_todo_research_to_queue
from thinkweave.operations.notes import (
    create_note,
    link_notes,
    unlink_notes,
    update_note,
)
from thinkweave.operations.reports import recent_reports
from thinkweave.operations.rlvr_export import export_trajectory_rows
from thinkweave.operations.search import query_context, query_prompts
from thinkweave.operations.wrap import finalize_wrap

__all__ = [
    "append_maintenance_log",
    "create_note",
    "export_trajectory_rows",
    "extract_session",
    "finalize_wrap",
    "judge_and_writeback",
    "link_notes",
    "maintenance_log_path",
    "migrate_todo_research_to_queue",
    "query_context",
    "query_prompts",
    "recent_reports",
    "run_hubs_batch",
    "unlink_notes",
    "update_note",
]
