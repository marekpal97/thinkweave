"""Session event buffer I/O — append-only JSONL helpers.

The hook handler buffers Claude Code events to ``<mem_dir>/buffer/<session_id>.jsonl``
during a session; the Stop hook (and ``mem_extract``) then drain that buffer
into the session note's folder. These helpers own the file-system shape of
that buffer.

Lives in ``core/`` because both the hook surface (``surfaces/hooks``) and
the MCP surface (``surfaces/mcp/tools/extract``) need to call them, and a
``surfaces → surfaces`` import is forbidden by the layer rule.

The archive step also *partitions* the buffer into two siblings:

- ``events.jsonl`` — Write/Edit/Bash + prompt events (the action stream)
- ``retrieval_log.jsonl`` — ``type: retrieval`` and ``type: startup`` events
  (the context-served stream feeding the RLVR substrate)

Events with no ``type`` field, or any unrecognised type, land in
``events.jsonl`` so legacy buffers and the test fixtures roundtrip
unchanged. ``retrieval_log.jsonl`` is created only when at least one
retrieval/startup event exists — keeps session folders tidy for
retrieval-free sessions.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

# Event types that get routed to retrieval_log.jsonl rather than events.jsonl.
_RETRIEVAL_LOG_TYPES = frozenset({"retrieval", "startup"})


def cleanup_buffer(mem_dir: Path, session_id: str) -> None:
    """Delete the buffer file after successful extraction."""
    buf_file = mem_dir / "buffer" / f"{session_id}.jsonl"
    buf_file.unlink(missing_ok=True)


def archive_buffer(mem_dir: Path, session_id: str, session_dir: Path) -> None:
    """Move the buffer file into the session folder, partitioning by type.

    Action/prompt events → ``events.jsonl``.
    Retrieval + startup events → ``retrieval_log.jsonl``.

    If no retrieval/startup events are present, the function degenerates to
    the pre-RLVR behaviour: a single ``events.jsonl`` is written and the
    sibling retrieval log file is never created.
    """
    buf_file = mem_dir / "buffer" / f"{session_id}.jsonl"
    if not buf_file.exists():
        return

    events_dest = session_dir / "events.jsonl"
    retrieval_dest = session_dir / "retrieval_log.jsonl"

    try:
        # Stream-partition — keep memory bounded for large buffers.
        action_lines: list[str] = []
        retrieval_lines: list[str] = []
        with open(buf_file, encoding="utf-8") as f:
            for line in f:
                line = line.rstrip("\n")
                if not line:
                    continue
                etype = ""
                try:
                    etype = (json.loads(line) or {}).get("type", "")
                except json.JSONDecodeError:
                    # Malformed lines stay with action stream — preserves the
                    # principle "events.jsonl is the catch-all".
                    pass
                if etype in _RETRIEVAL_LOG_TYPES:
                    retrieval_lines.append(line)
                else:
                    action_lines.append(line)

        session_dir.mkdir(parents=True, exist_ok=True)

        # Append rather than overwrite — supports rerun in catch-up wraps
        # where the buffer has already been archived once and a second
        # finalize pass should be a no-op (buffer file is already gone).
        if action_lines:
            with open(events_dest, "a", encoding="utf-8") as f:
                f.write("\n".join(action_lines) + "\n")
        elif not events_dest.exists():
            # No action events at all — touch an empty file so prune.py's
            # "events.jsonl missing" orphan rule still applies sensibly.
            events_dest.touch()
        if retrieval_lines:
            with open(retrieval_dest, "a", encoding="utf-8") as f:
                f.write("\n".join(retrieval_lines) + "\n")

        buf_file.unlink(missing_ok=True)
    except Exception:
        # Fallback: at minimum drop the buffer so it doesn't accumulate.
        # Better to lose a session's events than to leak buffer files.
        try:
            shutil.move(str(buf_file), str(events_dest))
        except Exception:
            buf_file.unlink(missing_ok=True)
