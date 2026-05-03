"""Session event buffer I/O — append-only JSONL helpers.

The hook handler buffers Claude Code events to ``<mem_dir>/buffer/<session_id>.jsonl``
during a session; the Stop hook (and ``mem_extract``) then drain that buffer
into the session note's folder. These helpers own the file-system shape of
that buffer.

Lives in ``core/`` because both the hook surface (``surfaces/hooks``) and
the MCP surface (``surfaces/mcp/tools/extract``) need to call them, and a
``surfaces → surfaces`` import is forbidden by the layer rule.
"""

from __future__ import annotations

import shutil
from pathlib import Path


def cleanup_buffer(mem_dir: Path, session_id: str) -> None:
    """Delete the buffer file after successful extraction."""
    buf_file = mem_dir / "buffer" / f"{session_id}.jsonl"
    buf_file.unlink(missing_ok=True)


def archive_buffer(mem_dir: Path, session_id: str, session_dir: Path) -> None:
    """Move the buffer file to ``events.jsonl`` inside the session folder."""
    buf_file = mem_dir / "buffer" / f"{session_id}.jsonl"
    if not buf_file.exists():
        return
    dest = session_dir / "events.jsonl"
    try:
        shutil.move(str(buf_file), str(dest))
    except Exception:
        # Fallback: just delete
        buf_file.unlink(missing_ok=True)
