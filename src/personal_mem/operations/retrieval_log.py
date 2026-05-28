"""Retrieval-event capture for the RLVR decision-context substrate.

The ``PostToolUse`` hook calls into this module whenever the agent invokes
one of personal_mem's MCP retrieval tools. The output is an event of the
shape::

    {"ts": ..., "type": "retrieval", "tool": "mcp__personal-mem__mem_search",
     "args": {"query": "...", "mode": "fts", ...},
     "returned_ids": ["n-abc123", "ses-def456", ...]}

It is appended to the same per-session ``buffer/<session_id>.jsonl`` that
already holds Write/Edit/Bash + prompt events. The Stop-time finalizer
partitions retrieval/startup lines into a sibling ``retrieval_log.jsonl``
next to ``events.jsonl`` (slice 2 — not done yet); a later SQLite
projection ``context_served(session_id, note_id, source)`` derives from
that (slice 3).

Why no MCP-side capture? The MCP server runs in its own process and
doesn't see the Claude Code session_id. The PostToolUse hook does — and
also already owns the per-session buffer. One capture point, no new
plumbing.

The closed ``RETRIEVAL_TOOLS`` set is intentional: a future retrieval tool
must opt into capture explicitly. Auto-matching ``mcp__personal-mem__mem_*``
would pull in mutation tools (``mem_create``, ``mem_link``, ``mem_extract``)
and pollute the log.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

# The closed set of MCP tool names whose calls produce a retrieval event.
# Names match what Claude Code sends to PostToolUse: dash-form server name
# (see install.py:SERVER_NAME = "personal-mem"), underscore tool name.
RETRIEVAL_TOOLS: frozenset[str] = frozenset({
    "mcp__personal-mem__mem_search",
    "mcp__personal-mem__mem_context",
    "mcp__personal-mem__mem_graph",
    "mcp__personal-mem__mem_read",
    "mcp__personal-mem__mem_timeline",
    "mcp__personal-mem__mem_project_snapshot",
})

# Note-ID regex. Prefix list is the canonical set from
# core/schemas.NOTE_ID_PREFIXES plus theme-candidates (`cand-`) and
# concept-hub IDs (`cncpt-`). All prefixes are reserved tokens — no prose
# false-positives.
_ID_RE = re.compile(
    r"\b((?:n|ses|dec|thm|src|cand|cncpt)-[a-z0-9]{6,})\b"
)

# Per-tool whitelist of args worth keeping. Keeps the buffer small and
# guards against accidentally logging large payloads (e.g. raw embeddings).
_KEEP_ARGS: dict[str, tuple[str, ...]] = {
    "mcp__personal-mem__mem_search": (
        "query", "mode", "type", "project", "tags", "concepts",
        "since", "until", "limit",
    ),
    "mcp__personal-mem__mem_context": (
        "query", "project", "tags", "concepts", "type", "since", "until", "limit",
    ),
    "mcp__personal-mem__mem_graph": (
        "id", "depth", "filter", "edge_types", "note_type", "project",
        "source_id", "file_path", "status", "concepts", "match_mode",
        "min_matches", "type", "limit",
    ),
    "mcp__personal-mem__mem_read": ("id",),
    "mcp__personal-mem__mem_timeline": ("project", "days"),
    "mcp__personal-mem__mem_project_snapshot": (
        "project", "sections", "budget_tokens",
    ),
}


def parse_returned_ids(tool_output: str) -> list[str]:
    """Extract note IDs from a rendered MCP tool_output text.

    Three stamping styles are used across the retrieval surface — ``(id)``
    in mem_search/mem_context/filtered mem_graph, ``[id]`` in source_lens
    and decisions_for_file, and ``\\`id\\``` in project_snapshot. One regex
    catches all three.

    Order: first appearance in the text. Duplicates removed.
    """
    seen: set[str] = set()
    out: list[str] = []
    for m in _ID_RE.finditer(tool_output):
        nid = m.group(1)
        if nid not in seen:
            seen.add(nid)
            out.append(nid)
    return out


def summarize_args(tool: str, args: dict | None) -> dict:
    """Project the MCP arguments to the small subset worth recording.

    Drops anything not whitelisted in ``_KEEP_ARGS[tool]`` — including
    accidentally-large fields. Returns a shallow copy; values are not
    further sanitized (the caller is the hook handler, not user code).
    """
    if not args:
        return {}
    keep = _KEEP_ARGS.get(tool, ())
    return {k: args[k] for k in keep if k in args and args[k] not in (None, "", [])}


def build_retrieval_event(
    tool_name: str,
    tool_input: dict,
    tool_output: Any,
    ts: str,
) -> dict | None:
    """Build the per-call retrieval event, or None to skip.

    Special-case ``mem_read``: the answer is exactly the ``id`` argument
    (the rendered note body may or may not mention it). Other tools rely
    on regex extraction from ``tool_output`` text.

    Returns None when ``tool_name`` isn't in the closed retrieval set —
    cheap gate so the hook handler can call this unconditionally.
    """
    if tool_name not in RETRIEVAL_TOOLS:
        return None

    args = summarize_args(tool_name, tool_input)

    if tool_name == "mcp__personal-mem__mem_read":
        # The id IS the answer — bypass regex parse.
        rid = (tool_input or {}).get("id", "")
        returned_ids = [rid] if rid else []
    else:
        # Defensive normalisation: hook handlers normalise via
        # ``_extract_tool_output_text`` before calling, but headless and
        # test callers sometimes pass the raw Claude Code ``tool_response``
        # object ({stdout, stderr, ...}). Concatenate both stream fields
        # so the regex parser sees the rendered text in either shape.
        if isinstance(tool_output, str):
            text = tool_output
        elif isinstance(tool_output, dict):
            text = (tool_output.get("stdout") or "") + (tool_output.get("stderr") or "")
        else:
            text = ""
        returned_ids = parse_returned_ids(text)

    return {
        "ts": ts,
        "type": "retrieval",
        "tool": tool_name,
        "args": args,
        "returned_ids": returned_ids,
    }


def append_event(buffer_path: Path, event: dict) -> None:
    """Append a single event to the per-session JSONL buffer.

    Thin wrapper kept here (rather than reaching into the hook handler's
    ``_buffer_event``) so non-hook callers — tests, headless catch-up
    flows — can write events without importing from ``surfaces/``.
    """
    buffer_path.parent.mkdir(parents=True, exist_ok=True)
    with open(buffer_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(event) + "\n")
