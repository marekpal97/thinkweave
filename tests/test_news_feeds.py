"""Tests for the ``rss_poll`` discover strategy — news flavor.

Replaces the old ``scripts/pull_news_feeds.py`` tests after the lift into
``discover/strategies/rss_poll.py``. The strategy is generic over
source_type; this file covers the **news flavor** (`feed_config:` →
``vault/.mem/news_feeds.yaml`` outlets). YouTube flavor coverage lives in
``test_rss_poll_youtube.py``.

Mocks ``feedparser.parse`` so the suite is hermetic. Covers:

  1. ``_build_news_item`` happy path + skips for missing link / id fallback.
  2. ``embedded_body`` is captured iff ``prefer_embedded`` is true and
     the feed entry actually carries ``content[0].value``.
  3. Strategy run enqueues new entries.
  4. Strategy run dedups against the active queue.
  5. Strategy run dedups against the indexer (URL already a source note).
  6. Per-outlet daily cap stops further enqueues from that outlet.
  7. Bozo feed without entries is logged and skipped without crashing.
"""

from __future__ import annotations

import json
import sqlite3
import sys
import types
from pathlib import Path
from typing import Any

import pytest

from personal_mem.discover.strategies import rss_poll
from personal_mem.discover.strategies.rss_poll import (
    RssPollStrategy,
    _build_news_item,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


class FakeParsed:
    """Tiny stand-in for the FeedParserDict we don't want to construct."""

    def __init__(self, entries: list[Any], bozo: bool = False, bozo_exception: Any = None):
        self.entries = entries
        self.bozo = bozo
        self.bozo_exception = bozo_exception
        self.feed = {}


def _entry(
    link: str = "",
    title: str = "",
    summary: str = "",
    published: str = "",
    entry_id: str = "",
    content: Any = None,
) -> dict:
    e: dict[str, Any] = {
        "link": link,
        "title": title,
        "summary": summary,
        "published": published,
    }
    if entry_id:
        e["id"] = entry_id
    if content is not None:
        e["content"] = content
    return e


def _seed_feeds_yaml(vault_root: Path, outlets: dict[str, dict]) -> None:
    """Write a minimal news_feeds.yaml the tiny YAML parser accepts."""
    (vault_root / ".mem").mkdir(parents=True, exist_ok=True)
    lines = ["outlets:"]
    for slug, conf in outlets.items():
        lines.append(f"  {slug}:")
        for k, v in conf.items():
            if isinstance(v, list):
                items = ", ".join(str(x) for x in v)
                lines.append(f"    {k}: [{items}]")
            elif isinstance(v, bool):
                lines.append(f"    {k}: {'true' if v else 'false'}")
            else:
                lines.append(f"    {k}: {v}")
    (vault_root / ".mem" / "news_feeds.yaml").write_text(
        "\n".join(lines), encoding="utf-8"
    )


def _basic_outlet(daily_cap: int = 5, prefer_embedded: bool = False) -> dict:
    return {
        "name": "Test Outlet",
        "feeds": ["https://feed.test/x"],
        "tier": 1,
        "region": "global",
        "prefer_embedded": prefer_embedded,
        "daily_cap": daily_cap,
    }


def _fake_vault(vault_root: Path, db_path: Path | None = None) -> Any:
    """Build a minimal vault-like object the strategy expects."""
    cfg = types.SimpleNamespace(vault_root=vault_root, index_db=db_path)
    return types.SimpleNamespace(config=cfg)


def _run_news(
    tmp_path: Path,
    db_path: Path | None = None,
    daily_cap: int = 5,
    prefer_embedded: bool = False,
    extra_outlets: dict | None = None,
) -> tuple[list[dict], dict]:
    """Run the strategy in news-flavor mode against tmp_path. Returns
    (descriptors, summary_stats). Assumes feedparser already patched.
    """
    outlets = {"reuters": _basic_outlet(daily_cap=daily_cap, prefer_embedded=prefer_embedded)}
    if extra_outlets:
        outlets.update(extra_outlets)
    _seed_feeds_yaml(tmp_path, outlets)
    config = {
        "sources": {
            "news": {
                "feed_config": ".mem/news_feeds.yaml",
                "dedup_keys": ["url", "entry_id"],
            }
        }
    }
    descriptors = RssPollStrategy().run(_fake_vault(tmp_path, db_path), None, config)
    summaries = [d for d in descriptors if d.get("kind") == "summary"]
    stats = summaries[0]["stats"] if summaries else {}
    return descriptors, stats


@pytest.fixture
def fake_feedparser(monkeypatch):
    """Ensure feedparser is importable and patchable in the strategy."""
    fp = types.ModuleType("feedparser")
    fp.parse = lambda url: FakeParsed([])
    monkeypatch.setitem(sys.modules, "feedparser", fp)
    return fp


# ---------------------------------------------------------------------------
# _build_news_item
# ---------------------------------------------------------------------------


def test_build_news_item_basic() -> None:
    entry = _entry(
        link="https://reuters.com/x",
        title="Title",
        summary="Summary",
        published="2026-05-09T13:00:00Z",
        entry_id="r-123",
    )
    conf = {
        "name": "Reuters",
        "tier": 1,
        "region": "global",
        "language": "en",
        "prefer_embedded": False,
    }
    item = _build_news_item(entry, "reuters", conf)
    assert item["url"] == "https://reuters.com/x"
    assert item["outlet"] == "reuters"
    assert item["outlet_name"] == "Reuters"
    assert item["tier"] == 1
    assert item["entry_id"] == "r-123"
    assert item["embedded_body"] is None


def test_build_news_item_skips_missing_link() -> None:
    assert _build_news_item(_entry(link=""), "x", {}) is None


def test_build_news_item_falls_back_to_url_for_entry_id() -> None:
    item = _build_news_item(_entry(link="https://x.com/a"), "x", {})
    assert item is not None
    assert item["entry_id"] == "https://x.com/a"


def test_build_news_item_captures_embedded_when_prefer_embedded() -> None:
    entry = _entry(
        link="https://x.com/a",
        content=[{"type": "text/html", "value": "<p>FULL BODY</p>"}],
    )
    item = _build_news_item(entry, "x", {"prefer_embedded": True})
    assert item["embedded_body"] == "<p>FULL BODY</p>"


def test_build_news_item_no_embedded_when_prefer_embedded_false() -> None:
    entry = _entry(
        link="https://x.com/a",
        content=[{"type": "text/html", "value": "<p>FULL BODY</p>"}],
    )
    item = _build_news_item(entry, "x", {"prefer_embedded": False})
    assert item["embedded_body"] is None


# ---------------------------------------------------------------------------
# Strategy.run() integration — news flavor
# ---------------------------------------------------------------------------


def test_strategy_enqueues_new_entries(tmp_path, fake_feedparser, monkeypatch) -> None:
    fake_entries = [
        _entry(link="https://reuters.com/a", title="A", entry_id="r-1"),
        _entry(link="https://reuters.com/b", title="B", entry_id="r-2"),
    ]
    monkeypatch.setattr(fake_feedparser, "parse", lambda url: FakeParsed(fake_entries))

    _, stats = _run_news(tmp_path)
    assert stats["enqueued"] == 2
    assert stats["entries_seen"] == 2
    assert stats["dup_queue"] == 0
    assert stats["dup_indexer"] == 0

    queue_path = tmp_path / ".mem" / "queues" / "news.jsonl"
    assert queue_path.exists()
    lines = queue_path.read_text().strip().splitlines()
    assert len(lines) == 2


def test_strategy_dedups_against_queue(tmp_path, fake_feedparser, monkeypatch) -> None:
    from personal_mem.sources.queue import Queue

    q = Queue.for_source_type("news", tmp_path)
    q.enqueue(
        {"url": "https://reuters.com/a", "entry_id": "r-1", "outlet": "reuters"}
    )

    fake_entries = [
        _entry(link="https://reuters.com/a", title="A", entry_id="r-1"),
        _entry(link="https://reuters.com/b", title="B", entry_id="r-2"),
    ]
    monkeypatch.setattr(fake_feedparser, "parse", lambda url: FakeParsed(fake_entries))

    _, stats = _run_news(tmp_path)
    assert stats["enqueued"] == 1
    assert stats["dup_queue"] == 1


def test_strategy_dedups_against_indexer(tmp_path, fake_feedparser, monkeypatch) -> None:
    """A URL already a source note in the indexer should not re-enqueue."""
    db_path = tmp_path / ".mem" / "index.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE notes (id TEXT, type TEXT, frontmatter TEXT)")
    conn.execute(
        "INSERT INTO notes VALUES (?, ?, ?)",
        ("src-old", "source", json.dumps({"url": "https://feed.test/already"})),
    )
    conn.commit()
    conn.close()

    fake_entries = [
        _entry(link="https://feed.test/already", title="Old", entry_id="x-1"),
        _entry(link="https://feed.test/new", title="New", entry_id="x-2"),
    ]
    monkeypatch.setattr(fake_feedparser, "parse", lambda url: FakeParsed(fake_entries))

    _, stats = _run_news(tmp_path, db_path=db_path)
    assert stats["enqueued"] == 1
    assert stats["dup_indexer"] == 1


def test_strategy_respects_per_outlet_daily_cap(tmp_path, fake_feedparser, monkeypatch) -> None:
    fake_entries = [
        _entry(link=f"https://reuters.com/{i}", title=f"E{i}", entry_id=f"r-{i}")
        for i in range(5)
    ]
    monkeypatch.setattr(fake_feedparser, "parse", lambda url: FakeParsed(fake_entries))

    _, stats = _run_news(tmp_path, daily_cap=2)
    assert stats["enqueued"] == 2
    assert stats["cap_hit"] == 3


def test_strategy_bozo_feed_logged_and_skipped(tmp_path, fake_feedparser, monkeypatch) -> None:
    monkeypatch.setattr(
        fake_feedparser,
        "parse",
        lambda url: FakeParsed([], bozo=True, bozo_exception="parse error"),
    )

    _, stats = _run_news(tmp_path)
    assert stats["enqueued"] == 0
    assert stats["feed_errors"] == 1


def test_strategy_no_outlets_returns_empty(tmp_path, fake_feedparser) -> None:
    """Empty outlets section — strategy should emit no descriptors for news."""
    (tmp_path / ".mem").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".mem" / "news_feeds.yaml").write_text("outlets:\n", encoding="utf-8")
    config = {
        "sources": {
            "news": {"feed_config": ".mem/news_feeds.yaml", "dedup_keys": ["url"]}
        }
    }
    descriptors = RssPollStrategy().run(_fake_vault(tmp_path), None, config)
    # No outlets → no feeds → no source row processed → no descriptors.
    assert descriptors == []


def test_strategy_runtime_source_type_filters(tmp_path, fake_feedparser, monkeypatch) -> None:
    """`_runtime.source_type` limits polling to one source type."""
    _seed_feeds_yaml(tmp_path, {"reuters": _basic_outlet()})
    fake_entries = [_entry(link="https://reuters.com/a", title="A", entry_id="r-1")]
    monkeypatch.setattr(fake_feedparser, "parse", lambda url: FakeParsed(fake_entries))
    config = {
        "_runtime": {"source_type": "paper"},  # not news → news skipped
        "sources": {
            "news": {"feed_config": ".mem/news_feeds.yaml", "dedup_keys": ["url"]},
            "paper": {"queue": "ignored"},  # no feed config, no channels
        },
    }
    descriptors = RssPollStrategy().run(_fake_vault(tmp_path), None, config)
    assert descriptors == []
