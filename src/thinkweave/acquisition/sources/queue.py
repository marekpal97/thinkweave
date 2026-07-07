"""Queue primitive — disk-backed JSONL queues for source-acquisition flows.

Each source type (paper, repo, article, …) gets its own queue at
``vault/.weave/queues/<source_type>.jsonl``. Items are appended one-per-line;
processed items move to ``vault/.weave/queues/_processed/YYYY-MM-DD/``.

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
from itertools import chain
from pathlib import Path
from typing import Any, Iterable, TypedDict

# Where active queues live, relative to the vault root.
_QUEUES_DIR = ".weave/queues"
_PROCESSED_DIR = "_processed"
# Days of archive history scanned by ``dedup_check``. Tuned for fast-moving
# event-grain types (news/youtube/podcast) where items >7 days old are
# already stale; slower types (paper/article/repo) still get URL/title
# dedup on the active queue + the worker's ``weave_search`` backstop.
_DEDUP_LOOKBACK_DAYS = 7


class QueueItem(TypedDict, total=False):
    """Schema of a single queue record (per-source-type subsets, total=False).

    A queue file is JSONL where each line is one of these dicts. Because the
    union spans every source type, every field is optional at the type
    level — readers should branch on ``source_type`` and check field presence.

    **Lifecycle spine** (always populated after enqueue):

    - ``id`` — auto-assigned ``q-<8hex>``; the record's stable handle.
    - ``enqueued_at`` — ISO-8601 UTC timestamp written by :meth:`Queue.enqueue`.
    - ``source_type`` — routing slug; matches a key in ``vault/.weave/sources.yaml``.

    **Universal content pointers** (most strategies populate; dedup keys for
    most types read from these):

    - ``url`` — canonical human URL (item landing page).
    - ``title`` — display title for the triage UI / archive log.
    - ``entry_id`` — RSS ``<guid>`` or feed-stable id; the most reliable
      dedup key when present (survives URL canonicalization changes).
    - ``message_id`` — RFC 5322 Message-ID for email-grain types; the
      primary dedup key for ``newsletter-*``.

    **Per-source-type extensions** (set by the producing strategy; read by
    the worker skill — kept as ``str`` / ``int`` / ``bool`` so JSON
    round-trips cleanly):

    - News / podcast / youtube common: ``summary``, ``published``,
      ``outlet``, ``outlet_name``, ``tier``, ``language``, ``region``.
    - Podcast only: ``audio_url``, ``audio_type``, ``audio_length_bytes``,
      ``duration_sec``, ``episode_number``.
    - News-prefer-embedded: ``prefer_embedded``, ``embedded_body``.
    - Newsletter (per-thread): ``thread_id``, ``sender``.

    **Lifecycle state** (mutated in place by :meth:`Queue.claim` /
    :meth:`Queue.archive`):

    - ``claimed``, ``claimed_at`` — set by :meth:`Queue.claim`.
    - ``status`` — set by :meth:`Queue.archive` (``done`` / ``failed`` /
      ``rejected`` / ``duplicate`` / …).
    - ``reason`` — optional human/worker explanation paired with ``status``.
    - ``archived_at`` — ISO-8601 set by :meth:`Queue.archive`.

    Per-type strategies are free to add additional keys beyond this set
    (the worker reads what it knows). The TypedDict documents the *common*
    surface; it does not restrict it. ``total=False`` reflects that
    every field is optional at the schema level — presence is gated by
    source type, not by the type system.
    """

    # Lifecycle spine
    id: str
    enqueued_at: str
    source_type: str

    # Universal content pointers
    url: str
    title: str
    entry_id: str
    message_id: str

    # News / podcast / youtube common
    summary: str
    published: str
    outlet: str
    outlet_name: str
    tier: int
    language: str
    region: str

    # Podcast only
    audio_url: str
    audio_type: str
    audio_length_bytes: int
    duration_sec: int
    episode_number: int

    # News prefer-embedded
    prefer_embedded: bool
    embedded_body: str | None

    # Newsletter (per-thread)
    thread_id: str
    sender: str

    # Lifecycle state
    claimed: bool
    claimed_at: str
    status: str
    reason: str
    archived_at: str


@dataclass
class Queue:
    """Per-source-type JSONL queue with claim/archive/dedup primitives.

    Attributes:
        source_type: canonical source-type slug (``paper``, ``repo``, …).
        path: absolute path to the active queue file
            (``<vault>/.weave/queues/<source_type>.jsonl``).
        archive_root: root of the archive tree
            (``<vault>/.weave/queues/_processed/``).
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

    def enqueue(self, item: QueueItem | dict[str, Any]) -> str:
        """Append an item to the queue. Returns its ``id``.

        The item gets a UUID-based ``id`` and an ``enqueued_at`` ISO-8601
        timestamp if those keys are absent. The original ``item`` dict is
        not mutated.
        """
        record: QueueItem = dict(item)  # type: ignore[assignment]
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

    def dequeue(self) -> QueueItem | None:
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

    def peek(self, n: int) -> list[QueueItem]:
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

    def archive(self, item_id: str, status: str, reason: str | None = None) -> None:
        """Move ``item_id`` from the active queue into the dated archive.

        ``status`` (``done`` / ``failed`` / ``rejected`` / ``duplicate`` / …)
        is stamped onto the archived record, along with an optional
        ``reason`` string when the worker emits one (e.g. rejection
        explanations). The active queue is rewritten without the item;
        the archive jsonl is appended to.

        No-op if the id isn't present (so callers can call ``archive``
        defensively without a pre-check).
        """
        items = self._read_all()
        target: QueueItem | None = None
        remaining: list[QueueItem] = []
        for item in items:
            if target is None and item.get("id") == item_id:
                target = item
                continue
            remaining.append(item)
        if target is None:
            return
        target["status"] = status
        if reason:
            target["reason"] = reason
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

    def items_since(self, cutoff_iso: str) -> list[QueueItem]:
        """Return every item ``enqueued_at`` at or after ``cutoff_iso``.

        Spans both halves of the queue's lifecycle — the live active file
        and the dated archive — so callers get "recent items" without
        knowing the archive exists or how it's laid out. The archive's
        ``_processed/YYYY-MM-DD/`` day-bucketing is an implementation
        detail bounded internally from the cutoff date; callers pass a
        plain ISO-8601 timestamp and read back a flat list.

        The comparison is inclusive (``>=``) and lexical over ISO-8601 UTC
        strings — the format :meth:`enqueue` writes — so an item stamped
        exactly at the cutoff is kept.
        """
        return [
            item
            for item in chain(self._read_all(), self._archive_items_since(cutoff_iso))
            if (item.get("enqueued_at") or "") >= cutoff_iso
        ]

    # ------------------------------------------------------------------
    # dedup
    # ------------------------------------------------------------------

    def dedup_check(
        self, item: QueueItem | dict[str, Any], keys: list[str]
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
        today = datetime.now(timezone.utc).date()
        since = (today - timedelta(days=_DEDUP_LOOKBACK_DAYS)).isoformat()
        candidates: list[QueueItem] = list(self._read_all())
        candidates.extend(self._archive_items_since(since))
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

    def _read_all(self) -> list[QueueItem]:
        if not self.path.exists():
            return []
        out: list[QueueItem] = []
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

    def _write_all(self, items: list[QueueItem]) -> None:
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

    def _archive_items_since(self, start_iso: str) -> Iterable[QueueItem]:
        """Yield archived items from every day-bucket on or after ``start_iso``.

        ``start_iso`` may be a bare ISO date (``YYYY-MM-DD``) or a full
        timestamp; only its date component selects the day-buckets to scan
        (from that date through today, inclusive). This is the sole reader
        of the ``_processed/YYYY-MM-DD/`` layout — the day-bucketing stays
        contained here and never leaks to callers.
        """
        if not self.archive_root.exists():
            return []
        start_date = datetime.fromisoformat(start_iso).date()
        today = datetime.now(timezone.utc).date()
        out: list[QueueItem] = []
        for delta in range((today - start_date).days + 1):
            day = start_date + timedelta(days=delta)
            archive_file = self.archive_root / day.isoformat() / f"{self.source_type}.jsonl"
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
