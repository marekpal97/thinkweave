"""Tests for the Queue primitive (sources/queue.py).

Covers enqueue/dequeue ordering, dedup against active + archive, the
archive lifecycle (move + day-bucket layout), and claim semantics.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path


from thinkweave.acquisition.sources.queue import Queue


def _make_queue(tmp_path: Path, source_type: str = "paper") -> Queue:
    return Queue.for_source_type(source_type, tmp_path)


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# enqueue / dequeue
# ---------------------------------------------------------------------------


def test_enqueue_assigns_id_and_timestamp(tmp_path: Path) -> None:
    q = _make_queue(tmp_path)
    item_id = q.enqueue({"url": "https://arxiv.org/abs/2305.10403"})
    assert item_id.startswith("q-")
    items = q.peek(10)
    assert len(items) == 1
    assert items[0]["id"] == item_id
    assert items[0]["url"] == "https://arxiv.org/abs/2305.10403"
    assert "enqueued_at" in items[0]


def test_enqueue_preserves_caller_id(tmp_path: Path) -> None:
    q = _make_queue(tmp_path)
    given = q.enqueue({"id": "custom-1", "url": "https://x.test/a"})
    assert given == "custom-1"


def test_dequeue_returns_oldest_first(tmp_path: Path) -> None:
    q = _make_queue(tmp_path)
    a = q.enqueue({"url": "https://x.test/a"})
    b = q.enqueue({"url": "https://x.test/b"})
    first = q.dequeue()
    assert first is not None
    assert first["id"] == a
    assert q.peek(10)[0]["id"] == b


def test_dequeue_on_empty_queue_returns_none(tmp_path: Path) -> None:
    q = _make_queue(tmp_path)
    assert q.dequeue() is None


def test_dequeue_skips_already_claimed(tmp_path: Path) -> None:
    q = _make_queue(tmp_path)
    a = q.enqueue({"url": "https://x.test/a"})
    b = q.enqueue({"url": "https://x.test/b"})
    assert q.claim(a) is True
    nxt = q.dequeue()
    assert nxt is not None
    assert nxt["id"] == b


# ---------------------------------------------------------------------------
# claim
# ---------------------------------------------------------------------------


def test_claim_marks_item(tmp_path: Path) -> None:
    q = _make_queue(tmp_path)
    a = q.enqueue({"url": "https://x.test/a"})
    assert q.claim(a) is True
    items = q.peek(10)
    assert items[0]["claimed"] is True
    assert "claimed_at" in items[0]


def test_claim_unknown_id_returns_false(tmp_path: Path) -> None:
    q = _make_queue(tmp_path)
    assert q.claim("nonexistent") is False


def test_claim_idempotent(tmp_path: Path) -> None:
    q = _make_queue(tmp_path)
    a = q.enqueue({"url": "https://x.test/a"})
    assert q.claim(a) is True
    assert q.claim(a) is True


# ---------------------------------------------------------------------------
# archive lifecycle
# ---------------------------------------------------------------------------


def test_archive_moves_item_to_dated_folder(tmp_path: Path) -> None:
    q = _make_queue(tmp_path)
    a = q.enqueue({"url": "https://x.test/a"})
    q.archive(a, status="done")

    # Item gone from active queue
    assert q.peek(10) == []
    # Active jsonl exists but is empty
    assert q.path.exists()

    # Archive dir exists with a single jsonl file under today's date
    archive_dirs = list(q.archive_root.iterdir())
    assert len(archive_dirs) == 1
    archive_file = archive_dirs[0] / "paper.jsonl"
    assert archive_file.exists()

    rows = [json.loads(line) for line in archive_file.read_text().splitlines() if line.strip()]
    assert len(rows) == 1
    assert rows[0]["id"] == a
    assert rows[0]["status"] == "done"
    assert "archived_at" in rows[0]


def test_archive_unknown_id_is_noop(tmp_path: Path) -> None:
    q = _make_queue(tmp_path)
    q.enqueue({"url": "https://x.test/a"})
    q.archive("nope", status="done")
    assert len(q.peek(10)) == 1
    assert not q.archive_root.exists()


def test_archive_appends_within_same_day(tmp_path: Path) -> None:
    q = _make_queue(tmp_path)
    a = q.enqueue({"url": "https://x.test/a"})
    b = q.enqueue({"url": "https://x.test/b"})
    q.archive(a, status="done")
    q.archive(b, status="failed")

    archive_dirs = list(q.archive_root.iterdir())
    assert len(archive_dirs) == 1
    rows = [
        json.loads(line)
        for line in (archive_dirs[0] / "paper.jsonl").read_text().splitlines()
        if line.strip()
    ]
    assert len(rows) == 2


# ---------------------------------------------------------------------------
# dedup
# ---------------------------------------------------------------------------


def test_dedup_check_active(tmp_path: Path) -> None:
    q = _make_queue(tmp_path)
    a = q.enqueue({"url": "https://arxiv.org/abs/1", "title": "Foo"})
    conflict = q.dedup_check(
        {"url": "https://arxiv.org/abs/1", "title": "Bar"},
        keys=["url", "title"],
    )
    assert conflict == a


def test_dedup_check_case_insensitive(tmp_path: Path) -> None:
    q = _make_queue(tmp_path)
    a = q.enqueue({"title": "Hello World"})
    conflict = q.dedup_check({"title": "  hello world  "}, keys=["title"])
    assert conflict == a


def test_dedup_check_archive(tmp_path: Path) -> None:
    q = _make_queue(tmp_path)
    a = q.enqueue({"url": "https://x.test/y"})
    q.archive(a, status="done")
    conflict = q.dedup_check({"url": "https://x.test/y"}, keys=["url"])
    assert conflict == a


def test_dedup_check_no_match_returns_none(tmp_path: Path) -> None:
    q = _make_queue(tmp_path)
    q.enqueue({"url": "https://x.test/a"})
    assert q.dedup_check({"url": "https://x.test/different"}, keys=["url"]) is None


def test_dedup_check_ignores_empty_values(tmp_path: Path) -> None:
    q = _make_queue(tmp_path)
    q.enqueue({"url": "https://x.test/a", "doi": ""})
    # Both have empty doi → not a collision.
    assert q.dedup_check({"url": "https://x.test/b", "doi": ""}, keys=["doi"]) is None


def test_dedup_check_excludes_self(tmp_path: Path) -> None:
    q = _make_queue(tmp_path)
    a = q.enqueue({"url": "https://x.test/a"})
    q.peek(10)
    # Re-checking the item that's already in the queue should return None.
    assert q.dedup_check({"id": a, "url": "https://x.test/a"}, keys=["url"]) is None


# ---------------------------------------------------------------------------
# items_since (recent across active + archive)
# ---------------------------------------------------------------------------


def test_items_since_active_only(tmp_path: Path) -> None:
    q = _make_queue(tmp_path)
    now = datetime.now(timezone.utc)
    q.enqueue({"url": "https://x.test/old", "enqueued_at": _iso(now - timedelta(days=3))})
    new = q.enqueue(
        {"url": "https://x.test/new", "enqueued_at": _iso(now - timedelta(hours=1))}
    )
    cutoff = _iso(now - timedelta(days=1))
    ids = {it["id"] for it in q.items_since(cutoff)}
    assert ids == {new}


def test_items_since_archive_only(tmp_path: Path) -> None:
    q = _make_queue(tmp_path)
    now = datetime.now(timezone.utc)
    a = q.enqueue(
        {"url": "https://x.test/a", "enqueued_at": _iso(now - timedelta(hours=2))}
    )
    q.archive(a, status="done")
    assert q.peek(10) == []  # active queue drained into archive

    items = q.items_since(_iso(now - timedelta(days=1)))
    assert [it["id"] for it in items] == [a]
    assert items[0]["status"] == "done"


def test_items_since_mixed_active_and_archive(tmp_path: Path) -> None:
    q = _make_queue(tmp_path)
    now = datetime.now(timezone.utc)
    arch = q.enqueue(
        {"url": "https://x.test/arch", "enqueued_at": _iso(now - timedelta(hours=5))}
    )
    q.archive(arch, status="done")
    active = q.enqueue(
        {"url": "https://x.test/active", "enqueued_at": _iso(now - timedelta(hours=1))}
    )
    ids = {it["id"] for it in q.items_since(_iso(now - timedelta(days=1)))}
    assert ids == {arch, active}


def test_items_since_cutoff_boundary_inclusive(tmp_path: Path) -> None:
    q = _make_queue(tmp_path)
    now = datetime.now(timezone.utc)
    ts = _iso(now - timedelta(hours=2))
    at_cutoff = q.enqueue({"url": "https://x.test/at", "enqueued_at": ts})
    q.enqueue(
        {"url": "https://x.test/before", "enqueued_at": _iso(now - timedelta(hours=3))}
    )
    ids = {it["id"] for it in q.items_since(ts)}
    # Item exactly at the cutoff is kept; the earlier one is excluded.
    assert ids == {at_cutoff}


def test_items_since_excludes_archived_before_cutoff(tmp_path: Path) -> None:
    """An item archived today but enqueued before the cutoff is filtered out."""
    q = _make_queue(tmp_path)
    now = datetime.now(timezone.utc)
    old = q.enqueue(
        {"url": "https://x.test/old", "enqueued_at": _iso(now - timedelta(days=3))}
    )
    q.archive(old, status="done")  # lands in today's day-bucket
    assert q.items_since(_iso(now - timedelta(days=1))) == []


def test_items_since_empty_queue(tmp_path: Path) -> None:
    q = _make_queue(tmp_path)
    cutoff = _iso(datetime.now(timezone.utc) - timedelta(days=1))
    assert q.items_since(cutoff) == []


# ---------------------------------------------------------------------------
# MCP archive action
# ---------------------------------------------------------------------------


def test_mcp_archive_action_moves_item_to_dated_folder(tmp_path: Path) -> None:
    """End-to-end: enqueue via Queue, archive via the weave_queue MCP handler."""
    from datetime import datetime, timezone

    from thinkweave.core.config import Config
    from thinkweave.surfaces.mcp.tools.queue import handle as weave_queue_handle

    cfg = Config(vault_root=tmp_path)
    q = Queue.for_source_type("paper", tmp_path)
    item_id = q.enqueue({"url": "https://arxiv.org/abs/2401.00001"})

    result = weave_queue_handle(
        cfg,
        {
            "action": "archive",
            "source_type": "paper",
            "item_id": item_id,
            "status": "done",
        },
    )

    # Handler returns a single TextContent confirming the archive.
    assert len(result) == 1
    assert item_id in result[0].text
    assert "done" in result[0].text

    # Active queue is empty.
    assert q.peek(10) == []

    # Archive file exists at .weave/queues/_processed/<today>/paper.jsonl.
    today = datetime.now(timezone.utc).date().isoformat()
    archive_file = tmp_path / ".weave" / "queues" / "_processed" / today / "paper.jsonl"
    assert archive_file.exists()

    rows = [
        json.loads(line)
        for line in archive_file.read_text().splitlines()
        if line.strip()
    ]
    assert len(rows) == 1
    assert rows[0]["id"] == item_id
    assert rows[0]["status"] == "done"
    assert "archived_at" in rows[0]


def test_mcp_archive_action_requires_source_type(tmp_path: Path) -> None:
    from thinkweave.core.config import Config
    from thinkweave.surfaces.mcp.tools.queue import handle as weave_queue_handle

    cfg = Config(vault_root=tmp_path)
    result = weave_queue_handle(
        cfg, {"action": "archive", "item_id": "q-abc", "status": "done"}
    )
    assert len(result) == 1
    assert "source_type" in result[0].text


def test_mcp_archive_action_requires_item_id(tmp_path: Path) -> None:
    from thinkweave.core.config import Config
    from thinkweave.surfaces.mcp.tools.queue import handle as weave_queue_handle

    cfg = Config(vault_root=tmp_path)
    result = weave_queue_handle(
        cfg, {"action": "archive", "source_type": "paper", "status": "done"}
    )
    assert len(result) == 1
    assert "item_id" in result[0].text


# ---------------------------------------------------------------------------
# claim contention (single-user simulation)
# ---------------------------------------------------------------------------


def test_two_workers_cannot_both_dequeue_same_item(tmp_path: Path) -> None:
    """Two sequential dequeue() calls must yield different items."""
    q = _make_queue(tmp_path)
    a = q.enqueue({"url": "https://x.test/a"})
    b = q.enqueue({"url": "https://x.test/b"})
    one = q.dequeue()
    two = q.dequeue()
    assert one is not None and two is not None
    assert {one["id"], two["id"]} == {a, b}
