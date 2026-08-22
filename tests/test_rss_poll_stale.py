"""``rss_poll`` queue-side freshness: ``stale_after_days`` archives
already-queued event-grain items so the drain head is always fresh.

Before 2026-08-23 the news queue was a FIFO that never shrank (613 deep,
inflow ≈ drain cap) and every drain briefed week-old news first.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from thinkweave.acquisition.discover.strategies.rss_poll import (
    _archive_stale,
    _item_age_anchor,
)
from thinkweave.acquisition.sources.queue import Queue


def _iso(days_ago: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()


def test_age_anchor_prefers_published_and_parses_rfc2822():
    assert _item_age_anchor({"published": "Mon, 11 Aug 2026 08:00:00 +0000"}) == (
        datetime(2026, 8, 11, 8, tzinfo=timezone.utc)
    )
    # falls back to enqueued_at when published is junk
    anchor = _item_age_anchor({"published": "garbage", "enqueued_at": _iso(3)})
    assert anchor is not None
    assert _item_age_anchor({"title": "no dates at all"}) is None


def test_archive_stale_moves_old_unclaimed_items_only(tmp_path: Path):
    q = Queue.for_source_type("news", tmp_path)
    old = q.enqueue({"url": "https://x/old", "title": "old", "enqueued_at": _iso(10)})
    fresh = q.enqueue({"url": "https://x/new", "title": "new", "enqueued_at": _iso(1)})
    claimed = q.enqueue(
        {"url": "https://x/claimed", "title": "c", "enqueued_at": _iso(10)}
    )
    q.claim(claimed)
    undated = q.enqueue({"url": "https://x/undated", "title": "u", "enqueued_at": ""})

    assert _archive_stale(q, 7) == 1

    remaining = {i["id"] for i in q.peek(100)}
    assert old not in remaining
    assert {fresh, claimed, undated} <= remaining
    archived = list(q._archive_items_since(_iso(1)[:10]))
    assert archived and archived[0]["id"] == old
    assert archived[0]["status"] == "stale"


def test_archive_stale_disabled_at_zero(tmp_path: Path):
    q = Queue.for_source_type("news", tmp_path)
    q.enqueue({"url": "https://x/old", "enqueued_at": _iso(30)})
    assert _archive_stale(q, 0) == 0
    assert len(q.peek(10)) == 1
