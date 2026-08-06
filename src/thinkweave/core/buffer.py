"""Session event buffer I/O — append-only JSONL helpers.

The hook handler buffers Claude Code events to ``<weave_dir>/buffer/<session_id>.jsonl``
during a session; the Stop hook (and ``weave_extract``) then drain that buffer
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


def session_state_dir(weave_dir: Path, session_id: str) -> Path:
    """Per-session scratch beside the buffer, for markers with no place in it.

    Holds the hook handler's delivery receipts (one empty file per persisted
    ``delivery_id``) and its once-per-session failure markers. Lives here, not
    in ``surfaces/hooks``, because its lifetime is the buffer's: whoever
    retires the buffer retires the scratch with it.
    """
    return weave_dir / "buffer" / ".state" / session_id


def clear_session_state(weave_dir: Path, session_id: str) -> None:
    """Drop a session's scratch dir. Best-effort — never blocks the caller.

    Receipts are only meaningful while the buffer they guard is live, so
    leaving them behind would grow ``.weave/buffer`` without bound, one
    directory per session, forever.
    """
    shutil.rmtree(session_state_dir(weave_dir, session_id), ignore_errors=True)


def _append_unique_lines(path: Path, lines: list[str]) -> None:
    """Append archive rows idempotently after a partial prior attempt.

    Reads the destination in full to do it. Session archives are bounded by
    one session's events, so the set costs about what the append does.
    """
    if not lines:
        return
    seen = (
        set(path.read_text(encoding="utf-8").splitlines())
        if path.exists()
        else set()
    )
    pending = [line for line in lines if line not in seen]
    if pending:
        with open(path, "a", encoding="utf-8") as f:
            f.write("\n".join(pending) + "\n")


def cleanup_buffer(weave_dir: Path, session_id: str) -> None:
    """Delete the buffer file, and its scratch dir, after extraction."""
    buf_file = weave_dir / "buffer" / f"{session_id}.jsonl"
    buf_file.unlink(missing_ok=True)
    clear_session_state(weave_dir, session_id)


def archive_buffer(weave_dir: Path, session_id: str, session_dir: Path) -> None:
    """Move the buffer file into the session folder, partitioning by type.

    Action/prompt events → ``events.jsonl``.
    Retrieval + startup events → ``retrieval_log.jsonl``.

    If no retrieval/startup events are present, the function degenerates to
    the pre-RLVR behaviour: a single ``events.jsonl`` is written and the
    sibling retrieval log file is never created.
    """
    buf_file = weave_dir / "buffer" / f"{session_id}.jsonl"
    if not buf_file.exists():
        clear_session_state(weave_dir, session_id)
        return

    events_dest = session_dir / "events.jsonl"
    retrieval_dest = session_dir / "retrieval_log.jsonl"

    # Any failure propagates and leaves the live buffer in place. The next
    # Stop/wrap can retry; silently deleting lifecycle evidence cannot.
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
                pass  # malformed lines stay in the action catch-all
            if etype in _RETRIEVAL_LOG_TYPES:
                retrieval_lines.append(line)
            else:
                action_lines.append(line)

    session_dir.mkdir(parents=True, exist_ok=True)
    if action_lines:
        _append_unique_lines(events_dest, action_lines)
    elif not events_dest.exists():
        events_dest.touch()
    if retrieval_lines:
        _append_unique_lines(retrieval_dest, retrieval_lines)

    buf_file.unlink()
    clear_session_state(weave_dir, session_id)
