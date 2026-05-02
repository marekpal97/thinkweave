"""Queue primitive — disk-backed JSONL queues for source-acquisition flows.

Each source type (paper, repo, article, …) gets its own queue at
``vault/.mem/queues/<source_type>.jsonl``. Items are appended one-per-line;
processed items move to ``vault/.mem/queues/_processed/YYYY-MM-DD/``.

Why JSONL over the previous ``todo+research`` tag-based queue:

- Acquisition state lives outside the knowledge graph. A queued URL isn't
  yet vault content; tagging a placeholder note pollutes search and
  conflates intake state with knowledge state.
- Per-type queues map cleanly onto the triad (research/drain/discover) and
  the registry's ``source_type`` axis.
- ``dedup_check`` honours per-type ``dedup_keys`` from ``sources.yaml``.

Single-user assumption: ``claim`` is implemented via a full-file rewrite
through a tempfile + atomic rename. Adequate for one human and the
occasional cron job; not safe for concurrent multi-process workers.
"""

from __future__ import annotations

import json
import os
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

# Where active queues live, relative to the vault root.
_QUEUES_DIR = ".mem/queues"
_PROCESSED_DIR = "_processed"
# Days of archive history scanned by ``dedup_check``.
_DEDUP_LOOKBACK_DAYS = 30


@dataclass
class Queue:
    """Per-source-type JSONL queue with claim/archive/dedup primitives.

    Attributes:
        source_type: canonical source-type slug (``paper``, ``repo``, …).
        path: absolute path to the active queue file
            (``<vault>/.mem/queues/<source_type>.jsonl``).
        archive_root: root of the archive tree
            (``<vault>/.mem/queues/_processed/``).
    """

    source_type: str
    path: Path
    archive_root: Path

    # ------------------------------------------------------------------
    # construction
    # ------------------------------------------------------------------

    @classmethod
    def for_source_type(cls, source_type: str, vault_root: Path) -> "Queue":
        """Return the queue for ``source_type`` rooted at ``vault_root``.

        The queue file is created lazily — no file is touched until the
        first :meth:`enqueue`. The archive root and the queues dir are
        created on demand by the writers.
        """
        if not source_type:
            raise ValueError("source_type must be a non-empty string")
        queues_dir = Path(vault_root) / _QUEUES_DIR
        path = queues_dir / f"{source_type}.jsonl"
        archive_root = queues_dir / _PROCESSED_DIR
        return cls(source_type=source_type, path=path, archive_root=archive_root)

    # ------------------------------------------------------------------
    # core ops
    # ------------------------------------------------------------------

    def enqueue(self, item: dict[str, Any]) -> str:
        """Append an item to the queue. Returns its ``id``.

        The item gets a UUID-based ``id`` and an ``enqueued_at`` ISO-8601
        timestamp if those keys are absent. The original ``item`` dict is
        not mutated.
        """
        record = dict(item)
        if not record.get("id"):
            record["id"] = f"q-{uuid.uuid4().hex[:8]}"
        if not record.get("enqueued_at"):
            record["enqueued_at"] = datetime.now(timezone.utc).isoformat(
                timespec="seconds"
            )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        return record["id"]

    def dequeue(self) -> dict[str, Any] | None:
        """Pop and return the oldest unclaimed item, or ``None`` if empty.

        ``dequeue`` is implemented as a one-shot peek-then-rewrite: we
        read every line, return the first item that isn't already
        ``claimed``, and rewrite the file with that item removed.
        """
        items = self._read_all()
        if not items:
            return None
        for i, item in enumerate(items):
            if item.get("claimed"):
                continue
            remaining = items[:i] + items[i + 1 :]
            self._write_all(remaining)
            return item
        return None

    def peek(self, n: int) -> list[dict[str, Any]]:
        """Return up to ``n`` items from the head of the queue (no mutation)."""
        if n <= 0:
            return []
        return self._read_all()[:n]

    def claim(self, item_id: str) -> bool:
        """Mark ``item_id`` as claimed in place. Returns ``True`` on success.

        Idempotent: claiming an already-claimed item returns ``True``
        (the caller's intent — "this id should be marked claimed" — is
        already satisfied). Returns ``False`` if the id isn't present.
        """
        items = self._read_all()
        for item in items:
            if item.get("id") == item_id:
                item["claimed"] = True
                item["claimed_at"] = datetime.now(timezone.utc).isoformat(
                    timespec="seconds"
                )
                self._write_all(items)
                return True
        return False

    def archive(self, item_id: str, status: str) -> None:
        """Move ``item_id`` from the active queue into the dated archive.

        ``status`` (``done`` / ``failed`` / ``duplicate`` / …) is stamped
        onto the archived record. The active queue is rewritten without
        the item; the archive jsonl is appended to.

        No-op if the id isn't present (so callers can call ``archive``
        defensively without a pre-check).
        """
        items = self._read_all()
        target: dict[str, Any] | None = None
        remaining: list[dict[str, Any]] = []
        for item in items:
            if target is None and item.get("id") == item_id:
                target = item
                continue
            remaining.append(item)
        if target is None:
            return
        target["status"] = status
        target["archived_at"] = datetime.now(timezone.utc).isoformat(
            timespec="seconds"
        )
        # Active queue rewrite first; if it raises mid-flight we'd rather
        # leave the item in place than orphan it in the archive.
        self._write_all(remaining)

        archive_dir = self.archive_root / _today_iso()
        archive_dir.mkdir(parents=True, exist_ok=True)
        archive_file = archive_dir / f"{self.source_type}.jsonl"
        with archive_file.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(target, ensure_ascii=False) + "\n")

    # ------------------------------------------------------------------
    # dedup
    # ------------------------------------------------------------------

    def dedup_check(
        self, item: dict[str, Any], keys: list[str]
    ) -> str | None:
        """Return the conflicting item id if ``item`` collides on any of
        ``keys`` with an active or recently-archived item. ``None`` otherwise.

        A collision means: there exists another item where, for at least
        one ``k`` in ``keys``, both records have a non-empty value for ``k``
        and those values are equal (case-folded for strings).

        Recent archive lookback: :data:`_DEDUP_LOOKBACK_DAYS` days back
        from today, inclusive.
        """
        if not keys:
            return None
        candidates: list[dict[str, Any]] = list(self._read_all())
        candidates.extend(self._recent_archive_items(_DEDUP_LOOKBACK_DAYS))
        for other in candidates:
            if other.get("id") == item.get("id"):
                continue
            for k in keys:
                lhs = item.get(k)
                rhs = other.get(k)
                if not lhs or not rhs:
                    continue
                if _values_equal(lhs, rhs):
                    return str(other.get("id") or "")
        return None

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    def _read_all(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        out: list[dict[str, Any]] = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                # Skip corrupt rows rather than blowing up the queue.
                continue
            if isinstance(row, dict):
                out.append(row)
        return out

    def _write_all(self, items: list[dict[str, Any]]) -> None:
        """Atomic rewrite via tempfile + rename in the same directory."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not items:
            # Truncate to empty file (preserves the queue's existence).
            with self.path.open("w", encoding="utf-8"):
                return
        fd, tmp_name = tempfile.mkstemp(
            prefix=f".{self.source_type}.", suffix=".jsonl", dir=str(self.path.parent)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                for item in items:
                    fh.write(json.dumps(item, ensure_ascii=False) + "\n")
            os.replace(tmp_name, self.path)
        except Exception:
            # Best-effort tempfile cleanup if the rename never happened.
            if os.path.exists(tmp_name):
                try:
                    os.unlink(tmp_name)
                except OSError:
                    pass
            raise

    def _recent_archive_items(self, days: int) -> Iterable[dict[str, Any]]:
        """Yield archived items from the last ``days`` (inclusive of today)."""
        if not self.archive_root.exists():
            return []
        today = datetime.now(timezone.utc).date()
        out: list[dict[str, Any]] = []
        for delta in range(days + 1):
            day = (today - timedelta(days=delta)).isoformat()
            archive_file = self.archive_root / day / f"{self.source_type}.jsonl"
            if not archive_file.exists():
                continue
            for line in archive_file.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(row, dict):
                    out.append(row)
        return out


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _today_iso() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def _values_equal(a: Any, b: Any) -> bool:
    """Case-insensitive equality for strings; strict equality otherwise."""
    if isinstance(a, str) and isinstance(b, str):
        return a.strip().lower() == b.strip().lower()
    return a == b
