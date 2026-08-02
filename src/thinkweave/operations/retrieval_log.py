"""Retrieval-event capture for the RLVR decision-context substrate.

The ``PostToolUse`` hook calls into this module whenever the agent invokes
one of thinkweave's MCP retrieval tools. The output is an event of the
shape::

    {"ts": ..., "type": "retrieval", "tool": "mcp__thinkweave__weave_search",
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
must opt into capture explicitly. Auto-matching ``mcp__thinkweave__weave_*``
would pull in mutation tools (``weave_create``, ``weave_link``, ``weave_extract``)
and pollute the log.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

# The closed set of MCP tool names whose calls produce a retrieval event.
# Names match what Claude Code sends to PostToolUse: dash-form server name
# (see install.py:SERVER_NAME = "thinkweave"), underscore tool name.
RETRIEVAL_TOOLS: frozenset[str] = frozenset({
    "mcp__thinkweave__weave_search",
    "mcp__thinkweave__weave_context",
    "mcp__thinkweave__weave_graph",
    "mcp__thinkweave__weave_read",
    "mcp__thinkweave__weave_timeline",
    "mcp__thinkweave__weave_project_snapshot",
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
    "mcp__thinkweave__weave_search": (
        "query", "mode", "type", "project", "tags", "concepts",
        "since", "until", "limit",
    ),
    "mcp__thinkweave__weave_context": (
        "query", "project", "tags", "concepts", "type", "since", "until", "limit",
    ),
    "mcp__thinkweave__weave_graph": (
        "id", "depth", "filter", "edge_types", "note_type", "project",
        "source_id", "file_path", "status", "concepts", "match_mode",
        "min_matches", "type", "limit",
    ),
    "mcp__thinkweave__weave_read": ("id",),
    "mcp__thinkweave__weave_timeline": ("project", "days"),
    "mcp__thinkweave__weave_project_snapshot": (
        "project", "sections", "budget_tokens",
    ),
}


def parse_returned_ids(tool_output: str) -> list[str]:
    """Extract note IDs from a rendered MCP tool_output text.

    Three stamping styles are used across the retrieval surface — ``(id)``
    in weave_search/weave_context/filtered weave_graph, ``[id]`` in source_lens
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


def response_text(tool_output: Any) -> str:
    """Every string anywhere in a tool response, newline-joined.

    A retrieval response *is* the rendered answer, so there is no shape here
    that isn't worth scanning for note ids — and no shape to hardcode either:
    Claude Code sends ``{stdout, stderr, …}``, while the schema in the codex-cli
    0.146.0 binary types ``tool_response`` as "any JSON" and no MCP call was
    observed in a credential-less session, so Codex's real wrapper (likely the
    nested MCP ``{"content": [{"text": …}]}``) is unmeasured (#107).

    Recursive rather than a JSON dump: dumping escapes newlines, and a
    ``\\nses-abc123`` in the escaped form glues the ``n`` onto the id and
    defeats the ``\\b``-anchored prefix match in :data:`_ID_RE`.

    Deliberately *not* shared with the action path — Write/Edit responses echo
    the file that was just written, and mining those re-captures the source's
    own ★ Insight blocks on every touch (``handler._extract_tool_output_text``).
    """
    if isinstance(tool_output, str):
        return tool_output
    if isinstance(tool_output, dict):
        parts = [response_text(v) for v in tool_output.values()]
    elif isinstance(tool_output, (list, tuple)):
        parts = [response_text(v) for v in tool_output]
    else:
        return ""
    return "\n".join(p for p in parts if p)


def build_retrieval_event(
    tool_name: str,
    tool_input: dict,
    tool_output: Any,
    ts: str,
) -> dict | None:
    """Build the per-call retrieval event, or None to skip.

    Special-case ``weave_read``: the answer is exactly the ``id`` argument
    (the rendered note body may or may not mention it). Other tools rely
    on regex extraction from ``tool_output`` text.

    Returns None when ``tool_name`` isn't in the closed retrieval set —
    cheap gate so the hook handler can call this unconditionally.
    """
    if tool_name not in RETRIEVAL_TOOLS:
        return None

    args = summarize_args(tool_name, tool_input)

    if tool_name == "mcp__thinkweave__weave_read":
        # The id IS the answer — bypass regex parse.
        rid = (tool_input or {}).get("id", "")
        returned_ids = [rid] if rid else []
    else:
        returned_ids = parse_returned_ids(response_text(tool_output))

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
