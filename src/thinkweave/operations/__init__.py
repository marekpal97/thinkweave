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
byte-equivalent (see tests/test_surface_contract.py). Door names resolve
lazily (PEP 562, same idiom as the core and surfaces doors): the hook
handler imports operations modules inside its per-event bodies on every
fire, and an eager door would pull all sibling modules — and through
them retrieval/synthesis/acquisition — into that budgeted path.

Storage: none of its own — writes vault notes via ``core`` and reads the
derived index via ``retrieval``; reports land under ``.weave/reports/``.

Extension points: one module per new concern; queue-shaped concerns
follow ``rejudge_queue``/``seam_link_queue`` (consumed as modules).
"""

# Lazy-door idiom — kept verbatim in core/operations/surfaces __init__.
_DOOR = {
    "append_maintenance_log": "thinkweave.operations.dream",
    "create_note": "thinkweave.operations.notes",
    "export_trajectory_rows": "thinkweave.operations.rlvr_export",
    "extract_session": "thinkweave.operations.extract",
    "finalize_wrap": "thinkweave.operations.wrap",
    "judge_and_writeback": "thinkweave.operations.decisions",
    "link_notes": "thinkweave.operations.notes",
    "maintenance_log_path": "thinkweave.operations.dream",
    "migrate_todo_research_to_queue": "thinkweave.operations.migrations",
    "query_context": "thinkweave.operations.search",
    "query_prompts": "thinkweave.operations.search",
    "recent_reports": "thinkweave.operations.reports",
    "run_hubs_batch": "thinkweave.operations.hubs_batch",
    "unlink_notes": "thinkweave.operations.notes",
    "update_note": "thinkweave.operations.notes",
}

__all__ = sorted(_DOOR)


def __getattr__(name: str):
    try:
        module = _DOOR[name]
    except KeyError:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from None
    import importlib

    obj = getattr(importlib.import_module(module), name)
    globals()[name] = obj  # cache: later accesses skip __getattr__
    return obj


def __dir__():
    return sorted(set(globals()) | set(__all__))
