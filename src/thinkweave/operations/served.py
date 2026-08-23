"""Serving-surface ``context_served`` logging — the generic half of #171.

A serving surface (``/learn`` today, ``/brief`` #170 later) hands the agent
a set of note ids outside the MCP retrieval tools, so the PostToolUse hook
never sees them. :func:`mark` records them the same way the hook would:
one ``retrieval`` event (``tool=<source>``) on the session's durable JSONL
— keyed by the harness UUID, never a ``ses-`` id — plus an immediate
``context_served`` upsert keyed by the ``ses-`` note id, so the rows exist
before the next index rebuild re-projects them from the log.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from thinkweave.core.config import Config
from thinkweave.core.indexer import Indexer
from thinkweave.operations.retrieval_log import append_event

@dataclass(frozen=True)
class SessionRef:
    ses_id: str
    source_session: str
    session_dir: Path


def resolve_session(cfg: Config, session: str) -> SessionRef | None:
    """Resolve a harness UUID or ``ses-`` id through the index. ``None``
    when unknown — callers write nothing rather than fabricate a key."""
    idx = Indexer(config=cfg)
    try:
        row = idx.db.execute(
            "SELECT id, path, json_extract(frontmatter, '$.source_session') AS src "
            "FROM notes WHERE type = 'session' AND (id = ? OR "
            "json_extract(frontmatter, '$.source_session') = ?) LIMIT 1",
            (session, session),
        ).fetchone()
    finally:
        idx.close()
    if not row or not row["src"]:
        return None
    return SessionRef(row["id"], str(row["src"]), (cfg.vault_root / row["path"]).parent)


def session_log(cfg: Config, ref: SessionRef, archived_name: str) -> Path:
    """Where a new event for this session belongs: the live buffer while
    the session runs; the archived per-session file once Stop moved it."""
    live = cfg.weave_dir / "buffer" / f"{ref.source_session}.jsonl"
    archived = ref.session_dir / archived_name
    return archived if archived.exists() and not live.exists() else live


def mark(cfg: Config, source: str, session: str, note_id: str, served: list[str]) -> int:
    """Log ``served`` ids as context served by ``source``. Returns rows upserted."""
    served = [s for s in dict.fromkeys(served) if s]
    ref = resolve_session(cfg, session)
    if ref is None or not served:
        return 0
    ts = datetime.now(timezone.utc).isoformat()
    append_event(
        session_log(cfg, ref, "retrieval_log.jsonl"),
        {"ts": ts, "type": "retrieval", "tool": source,
         "args": {"note": note_id}, "returned_ids": served},
    )
    idx = Indexer(config=cfg)
    try:
        idx.db.executemany(
            "INSERT OR REPLACE INTO context_served (session_id, note_id, source, ts) "
            "VALUES (?, ?, ?, ?)",
            [(ref.ses_id, nid, source, ts) for nid in served],
        )
        idx.db.commit()
    finally:
        idx.close()
    return len(served)
