"""Rejudge queue — a per-vault JSONL queue of decisions awaiting re-judgment.

Unlike the per-source-type acquisition queues under
``vault/.weave/queues/<source_type>.jsonl``, the rejudge queue is *one*
queue per vault, rooted at ``vault/.weave/rejudge_queue.jsonl``. It feeds
the ``/judge-prediction`` skill's worklist: items land here either when a
supersession trigger fires (a new decision declares ``supersedes: [dec-X]``),
or when cron / a user explicitly asks for a re-judge, or when the periodic
``pending_due`` sweep discovers a stale ``pending`` verdict.

Item shape (matches the wire schema documented in
``commands/judge-prediction.md``)::

    {
      "decision_id": "dec-...",
      "reason": "superseded by dec-XYZ" | "manual rejudge" | "stale verdict",
      "source": "supersession" | "manual" | "cron",
      "enqueued_at": "<iso-utc>"
    }

Dedupe semantics: :func:`enqueue` is idempotent on ``decision_id`` —
calling it twice for the same id is a no-op. The first reason/source
wins; subsequent enqueues don't refresh the timestamp either. Rationale:
once a decision is queued, the next drain will pick it up; queuing it
again before the drain runs is just noise.

Atomic writes follow the same pattern as ``sources/queue.py`` —
tempfile + rename in the same directory.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from thinkweave.core.config import Config
from thinkweave.core.indexer import Indexer

# Per-vault storage location. NOT under .weave/queues/ — that namespace is
# reserved for per-source-type acquisition queues; this queue is a
# different beast (verdict pipeline, not intake pipeline).
_QUEUE_PATH = ".weave/rejudge_queue.jsonl"


def _queue_path(config: Config) -> Path:
    return Path(config.vault_root) / _QUEUE_PATH


def _read_all(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            # Corrupt rows skipped, not fatal — drain proceeds with what's valid.
            continue
        if isinstance(row, dict):
            out.append(row)
    return out


def _write_all(path: Path, items: list[dict[str, Any]]) -> None:
    """Atomic rewrite via tempfile + rename in the same directory.

    Empty ``items`` truncates to a zero-byte file (preserving existence).
    Failed rename leaves the tempfile cleaned up; the original is intact.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    if not items:
        with path.open("w", encoding="utf-8"):
            return
    fd, tmp_name = tempfile.mkstemp(
        prefix=".rejudge_queue.", suffix=".jsonl", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            for item in items:
                fh.write(json.dumps(item, ensure_ascii=False) + "\n")
        os.replace(tmp_name, path)
    except Exception:
        if os.path.exists(tmp_name):
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
        raise


def enqueue(
    config: Config,
    *,
    decision_id: str,
    reason: str,
    source: str,
) -> None:
    """Append one item to the rejudge queue. Idempotent on ``decision_id``.

    If ``decision_id`` already appears in the queue, this is a no-op (the
    first enqueue wins on reason/source/timestamp). This mirrors decision
    call #3 from the Phase-3 design: queuing the same decision twice
    before the next drain shouldn't double-process it; the drain will
    pick it up exactly once.
    """
    if not decision_id:
        return
    path = _queue_path(config)
    items = _read_all(path)
    for it in items:
        if it.get("decision_id") == decision_id:
            return
    items.append(
        {
            "decision_id": decision_id,
            "reason": reason,
            "source": source,
            "enqueued_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
    )
    _write_all(path, items)


def drain_all(config: Config) -> list[dict[str, Any]]:
    """Return every queued item and clear the queue. Atomic.

    Used by ``weave judge --drain``. After this returns, the queue file is
    truncated to zero bytes (it stays existing so subsequent enqueues
    don't race against parent-dir creation).
    """
    path = _queue_path(config)
    items = _read_all(path)
    if items:
        _write_all(path, [])
    return items


def remove(config: Config, decision_ids: list[str] | set[str]) -> int:
    """Remove the entries whose ``decision_id`` is in ``decision_ids``. Atomic.

    The selective counterpart of :func:`drain_all` — used by
    ``weave dream apply`` to consume exactly the entries the dream scan
    handed off to the phase-2 ``dream-judge-worker``, leaving anything
    beyond the scan cap (or enqueued since) untouched, fields intact.
    Returns the number of entries removed.
    """
    ids = {i for i in decision_ids if i}
    if not ids:
        return 0
    path = _queue_path(config)
    items = _read_all(path)
    kept = [it for it in items if it.get("decision_id") not in ids]
    removed = len(items) - len(kept)
    if removed:
        _write_all(path, kept)
    return removed


def peek(config: Config) -> list[dict[str, Any]]:
    """Return every queued item without clearing. Read-only."""
    return _read_all(_queue_path(config))


def pending_due(
    config: Config,
    *,
    age_days: int = 1,
) -> list[str]:
    """Decision ids with ``prediction_match == 'pending'`` and stale judging.

    "Stale" = either ``judged_at`` missing entirely, or ``judged_at`` older
    than ``age_days`` from now (UTC). Used by ``weave judge --drain`` to merge
    cron-style re-judgment work into the supersession-triggered worklist.

    Implemented via SQLite ``json_extract`` on the indexed frontmatter blob
    so we never filesystem-walk the vault. The cutoff comparison uses ISO
    timestamps as plain string comparison — works because
    :func:`synthesis.prediction.append_verdict` always writes timezone-aware
    ISO strings (``YYYY-MM-DDTHH:MM:SS...``) which sort lexicographically.
    """
    cutoff = (
        datetime.now(timezone.utc) - timedelta(days=age_days)
    ).isoformat()

    idx = Indexer(config=config)
    try:
        rows = idx.db.execute(
            """
            SELECT id, json_extract(frontmatter, '$.judged_at') AS judged_at
              FROM notes
             WHERE type = 'decision'
               AND json_extract(frontmatter, '$.prediction_match') = 'pending'
            """,
        ).fetchall()
    finally:
        idx.close()

    out: list[str] = []
    for row in rows:
        # sqlite3.Row supports both index and key access.
        try:
            note_id = row["id"]
            judged = row["judged_at"]
        except (KeyError, IndexError):
            note_id = row[0]
            judged = row[1]
        if not judged or str(judged) < cutoff:
            out.append(note_id)
    return out
