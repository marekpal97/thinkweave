"""Tests for the ``rss_poll`` discover strategy — youtube flavor.

The youtube flavor uses ``channels: [...]`` (inline list of channel IDs)
rather than a ``feed_config:`` file. Each channel ID maps to one
YouTube RSS feed URL.

Mocks ``feedparser.parse`` so the suite is hermetic.
"""

from __future__ import annotations

import sys
import types
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

import pytest

from thinkweave.acquisition.discover.strategies.rss_poll import (
    RssPollStrategy,
    _build_youtube_item,
)


class FakeParsed:
    def __init__(self, entries: list[Any], bozo: bool = False, feed_title: str = "TestChannel"):
        self.entries = entries
        self.bozo = bozo
        self.bozo_exception = None
        self.feed = {"title": feed_title}


def _yt_entry(
    *,
    video_id: str,
    title: str,
    link: str | None = None,
    published_parsed: Any = None,
) -> Any:
    """Build a feedparser-shaped entry.

    feedparser exposes ``yt:videoId`` via ``yt_videoid`` attribute, not
    via dict lookup. So the entry object needs both __getitem__-style
    `.get` access AND the `yt_videoid` attribute.
    """
    base = {
        "link": link or f"https://www.youtube.com/watch?v={video_id}",
        "title": title,
        "summary": f"description of {video_id}",
        "id": f"yt:video:{video_id}",
        "published_parsed": published_parsed,
        "published": "",
    }

    class Entry:
        def __init__(self, payload: dict):
            self._p = payload
            self.yt_videoid = video_id

        def get(self, key: str, default: Any = None) -> Any:
            return self._p.get(key, default)

    return Entry(base)


def _fake_vault(vault_root: Path) -> Any:
    cfg = types.SimpleNamespace(vault_root=vault_root, index_db=None)
    return types.SimpleNamespace(config=cfg)


@pytest.fixture
def fake_feedparser(monkeypatch):
    fp = types.ModuleType("feedparser")
    fp.parse = lambda url: FakeParsed([])
    monkeypatch.setitem(sys.modules, "feedparser", fp)
    return fp


# ---------------------------------------------------------------------------
# _build_youtube_item
# ---------------------------------------------------------------------------


def test_build_youtube_item_basic() -> None:
    pub = (datetime.now(timezone.utc) - timedelta(days=1)).timetuple()
    entry = _yt_entry(video_id="abc123XYZ", title="Test", published_parsed=pub)
    item = _build_youtube_item(entry, {"title": "MyChannel"}, "UCxxx", cutoff_dt=None)
    assert item is not None
    assert item["video_id"] == "abc123XYZ"
    assert item["channel"] == "MyChannel"
    assert item["channel_id"] == "UCxxx"
    assert item["title"] == "Test"
    assert item["url"] == "https://www.youtube.com/watch?v=abc123XYZ"


def test_build_youtube_item_cutoff_drops_stale() -> None:
    """Videos published before cutoff_dt return None."""
    old = (datetime.now(timezone.utc) - timedelta(days=30)).timetuple()
    entry = _yt_entry(video_id="old1", title="Stale", published_parsed=old)
    cutoff = datetime.now(timezone.utc) - timedelta(days=7)
    assert _build_youtube_item(entry, None, "UCxxx", cutoff_dt=cutoff) is None


def test_build_youtube_item_cutoff_keeps_fresh() -> None:
    """Videos published after cutoff_dt are kept."""
    fresh = (datetime.now(timezone.utc) - timedelta(days=1)).timetuple()
    entry = _yt_entry(video_id="new1", title="Fresh", published_parsed=fresh)
    cutoff = datetime.now(timezone.utc) - timedelta(days=7)
    item = _build_youtube_item(entry, None, "UCxxx", cutoff_dt=cutoff)
    assert item is not None
    assert item["video_id"] == "new1"


# ---------------------------------------------------------------------------
# Strategy run — youtube flavor
# ---------------------------------------------------------------------------


def test_strategy_polls_channels_and_enqueues(tmp_path, fake_feedparser, monkeypatch) -> None:
    fresh = (datetime.now(timezone.utc) - timedelta(hours=2)).timetuple()
    entries = [
        _yt_entry(video_id="aaa111BBB22", title="Video A", published_parsed=fresh),
        _yt_entry(video_id="ccc333DDD44", title="Video B", published_parsed=fresh),
    ]
    monkeypatch.setattr(fake_feedparser, "parse", lambda url: FakeParsed(entries))

    config = {
        "sources": {
            "youtube-events": {
                "channels": ["UCBJycsmduvYEL83R_U4JriQ"],
                "lookback_days": 7,
                "dedup_keys": ["video_id", "url"],
            }
        }
    }
    descriptors = RssPollStrategy().run(_fake_vault(tmp_path), None, config)
    enqueued = [d for d in descriptors if d.get("kind") == "enqueued"]
    summaries = [d for d in descriptors if d.get("kind") == "summary"]

    assert len(enqueued) == 2
    assert summaries[0]["stats"]["enqueued"] == 2
    assert all(d["source_type"] == "youtube-events" for d in enqueued)

    queue_path = tmp_path / ".weave" / "queues" / "youtube-events.jsonl"
    assert queue_path.exists()


def test_strategy_youtube_lookback_filters_stale(tmp_path, fake_feedparser, monkeypatch) -> None:
    """Videos older than `lookback_days` are skipped, counted in stale_lookback."""
    old = (datetime.now(timezone.utc) - timedelta(days=30)).timetuple()
    fresh = (datetime.now(timezone.utc) - timedelta(hours=12)).timetuple()
    entries = [
        _yt_entry(video_id="oldXYZold12", title="Old", published_parsed=old),
        _yt_entry(video_id="freshABC456", title="Fresh", published_parsed=fresh),
    ]
    monkeypatch.setattr(fake_feedparser, "parse", lambda url: FakeParsed(entries))

    config = {
        "sources": {
            "youtube-events": {
                "channels": ["UCxxx"],
                "lookback_days": 7,
                "dedup_keys": ["video_id"],
            }
        }
    }
    descriptors = RssPollStrategy().run(_fake_vault(tmp_path), None, config)
    summary = next(d for d in descriptors if d.get("kind") == "summary")
    assert summary["stats"]["enqueued"] == 1
    assert summary["stats"]["stale_lookback"] == 1


def test_strategy_empty_channels_skips_source(tmp_path, fake_feedparser) -> None:
    """A youtube-* source with no channels: produces no descriptors."""
    config = {
        "sources": {
            "youtube-concepts": {
                "channels": [],
                "lookback_days": 30,
                "dedup_keys": ["video_id"],
            }
        }
    }
    descriptors = RssPollStrategy().run(_fake_vault(tmp_path), None, config)
    assert descriptors == []


def test_strategy_dedups_against_indexer_by_video_id(
    tmp_path, fake_feedparser, monkeypatch
) -> None:
    """A video already on file as a source note dedups by video_id even
    when its note URL differs from the feed URL (e.g. youtu.be short form)."""
    import json as _json
    import sqlite3 as _sqlite3

    db_path = tmp_path / ".weave" / "index.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = _sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE notes (id TEXT, type TEXT, frontmatter TEXT)")
    conn.execute(
        "INSERT INTO notes VALUES (?, ?, ?)",
        (
            "src-old",
            "source",
            _json.dumps(
                {"url": "https://youtu.be/aaa111BBB22", "video_id": "aaa111BBB22"}
            ),
        ),
    )
    conn.commit()
    conn.close()

    fresh = (datetime.now(timezone.utc) - timedelta(hours=2)).timetuple()
    entries = [
        _yt_entry(video_id="aaa111BBB22", title="Already filed", published_parsed=fresh),
        _yt_entry(video_id="ccc333DDD44", title="New", published_parsed=fresh),
    ]
    monkeypatch.setattr(fake_feedparser, "parse", lambda url: FakeParsed(entries))

    cfg = types.SimpleNamespace(vault_root=tmp_path, index_db=db_path)
    vault = types.SimpleNamespace(config=cfg)
    config = {
        "sources": {
            "youtube-events": {
                "channels": ["UCBJycsmduvYEL83R_U4JriQ"],
                "lookback_days": 7,
                "dedup_keys": ["video_id", "url"],
            }
        }
    }
    descriptors = RssPollStrategy().run(vault, None, config)
    summary = next(d for d in descriptors if d.get("kind") == "summary")
    assert summary["stats"]["enqueued"] == 1
    assert summary["stats"]["dup_indexer"] == 1


def test_indexer_dedup_honours_per_type_keys_only(
    tmp_path, fake_feedparser, monkeypatch
) -> None:
    """The indexer guard matches only the source type's own ``dedup_keys``
    (f3ef59f) — ``url`` is not a forced universal backstop. A type keyed on
    ``video_id`` alone must not drop a new video whose feed URL happens to
    collide with an already-filed note's url."""
    import json as _json
    import sqlite3 as _sqlite3

    db_path = tmp_path / ".weave" / "index.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = _sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE notes (id TEXT, type TEXT, frontmatter TEXT)")
    conn.execute(
        "INSERT INTO notes VALUES (?, ?, ?)",
        (
            "src-old",
            "source",
            _json.dumps(
                {"url": "https://example.com/shared", "video_id": "oldVID11111"}
            ),
        ),
    )
    conn.commit()
    conn.close()

    fresh = (datetime.now(timezone.utc) - timedelta(hours=2)).timetuple()
    entries = [
        _yt_entry(
            video_id="newVID22222",
            title="New video, colliding url",
            link="https://example.com/shared",
            published_parsed=fresh,
        ),
    ]
    monkeypatch.setattr(fake_feedparser, "parse", lambda url: FakeParsed(entries))

    cfg = types.SimpleNamespace(vault_root=tmp_path, index_db=db_path)
    vault = types.SimpleNamespace(config=cfg)
    config = {
        "sources": {
            "youtube-events": {
                "channels": ["UCxxx"],
                "lookback_days": 7,
                "dedup_keys": ["video_id"],
            }
        }
    }
    descriptors = RssPollStrategy().run(vault, None, config)
    summary = next(d for d in descriptors if d.get("kind") == "summary")
    assert summary["stats"]["dup_indexer"] == 0
    assert summary["stats"]["enqueued"] == 1
