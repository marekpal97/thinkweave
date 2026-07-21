"""Tests for the ``rss_poll`` discover strategy — podcast flavor.

The podcast flavor reads outlets from
``PRIORITIES.yaml::intake.podcast_events.outlets`` (like news) and is
dispatched by source-type slug prefix (``podcast-*``). Per entry the
strategy extracts:
- the <enclosure> audio URL into ``audio_url``,
- <itunes:duration> parsed to seconds,
- <itunes:episode> as episode_number,
- <guid> as entry_id (most stable dedup key).

Mocks ``feedparser.parse`` so the suite is hermetic.
"""

from __future__ import annotations

import sys
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from thinkweave.acquisition.discover.strategies.rss_poll import (
    RssPollStrategy,
    _build_podcast_item,
    _parse_itunes_duration,
)
from thinkweave.acquisition.sources.queue import Queue


class FakeParsed:
    def __init__(self, entries: list[Any], bozo: bool = False, feed_title: str = "TestShow"):
        self.entries = entries
        self.bozo = bozo
        self.bozo_exception = None
        self.feed = {"title": feed_title}


def _pod_entry(
    *,
    title: str,
    link: str,
    audio_url: str,
    audio_type: str = "audio/mpeg",
    audio_length: int = 28_000_000,
    guid: str | None = None,
    published_parsed: Any = None,
    duration: str | None = None,
    episode: int | None = None,
    summary: str = "",
) -> Any:
    """Build a feedparser-shaped podcast entry.

    Real feedparser exposes <enclosure> as ``entry.enclosures`` (list of
    dicts with ``href``, ``type``, ``length``), and <itunes:duration> /
    <itunes:episode> as attributes (``itunes_duration``, ``itunes_episode``).
    """
    base = {
        "link": link,
        "title": title,
        "summary": summary,
        "id": guid or link,
        "published_parsed": published_parsed,
        "published": "",
        "enclosures": [
            {"href": audio_url, "type": audio_type, "length": str(audio_length)}
        ],
        "itunes_duration": duration,
        "itunes_episode": episode,
    }

    class Entry:
        def __init__(self, payload: dict):
            self._p = payload

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
# _parse_itunes_duration
# ---------------------------------------------------------------------------


def test_parse_itunes_duration_hms() -> None:
    assert _parse_itunes_duration("01:23:45") == 3600 + 23 * 60 + 45


def test_parse_itunes_duration_ms() -> None:
    assert _parse_itunes_duration("45:30") == 45 * 60 + 30


def test_parse_itunes_duration_seconds() -> None:
    assert _parse_itunes_duration("3540") == 3540


def test_parse_itunes_duration_missing_or_bad() -> None:
    assert _parse_itunes_duration(None) == 0
    assert _parse_itunes_duration("") == 0
    assert _parse_itunes_duration("not-a-duration") == 0


# ---------------------------------------------------------------------------
# _build_podcast_item
# ---------------------------------------------------------------------------


def test_build_podcast_item_basic() -> None:
    pub = (datetime.now(timezone.utc) - timedelta(days=1)).timetuple()
    entry = _pod_entry(
        title="Episode 42 — The Dollar's Last Stand",
        link="https://show.example.com/ep/42",
        audio_url="https://chrt.fm/track/abc/show.com/ep42.mp3",
        guid="show.com/guid/42",
        published_parsed=pub,
        duration="01:05:30",
        episode=42,
        summary="Alf and Brent on the dollar.",
    )
    outlet_conf = {"name": "The Macro Trading Floor", "tier": 1, "language": "en"}
    item = _build_podcast_item(
        entry, "macro-trading-floor", outlet_conf, cutoff_dt=None
    )
    assert item is not None
    assert item["url"] == "https://show.example.com/ep/42"
    assert item["audio_url"] == "https://chrt.fm/track/abc/show.com/ep42.mp3"
    assert item["audio_type"] == "audio/mpeg"
    assert item["audio_length_bytes"] == 28_000_000
    assert item["title"] == "Episode 42 — The Dollar's Last Stand"
    assert item["entry_id"] == "show.com/guid/42"
    assert item["duration_sec"] == 3600 + 5 * 60 + 30
    assert item["episode_number"] == 42
    assert item["outlet"] == "macro-trading-floor"
    assert item["outlet_name"] == "The Macro Trading Floor"
    assert item["tier"] == 1
    assert item["language"] == "en"


def test_build_podcast_item_no_enclosure_returns_none() -> None:
    """A podcast feed item with no <enclosure> isn't worth queuing."""

    class NoEnclosureEntry:
        def get(self, key: str, default: Any = None) -> Any:
            base = {
                "link": "https://show.example.com/ep/0",
                "title": "Bad entry",
                "id": "guid/0",
                "enclosures": [],
                "published_parsed": None,
                "published": "",
            }
            return base.get(key, default)

    assert (
        _build_podcast_item(
            NoEnclosureEntry(), "show", {"name": "Show"}, cutoff_dt=None
        )
        is None
    )


def test_build_podcast_item_cutoff_drops_stale() -> None:
    """Episodes older than cutoff_dt return None."""
    old = (datetime.now(timezone.utc) - timedelta(days=30)).timetuple()
    entry = _pod_entry(
        title="Old episode",
        link="https://show.example.com/ep/1",
        audio_url="https://cdn.example.com/old.mp3",
        guid="guid/1",
        published_parsed=old,
    )
    cutoff = datetime.now(timezone.utc) - timedelta(days=7)
    assert _build_podcast_item(entry, "show", {"name": "Show"}, cutoff_dt=cutoff) is None


def test_build_podcast_item_cutoff_keeps_fresh() -> None:
    """Episodes published after cutoff_dt are kept."""
    fresh = (datetime.now(timezone.utc) - timedelta(days=1)).timetuple()
    entry = _pod_entry(
        title="Fresh episode",
        link="https://show.example.com/ep/2",
        audio_url="https://cdn.example.com/fresh.mp3",
        guid="guid/2",
        published_parsed=fresh,
    )
    cutoff = datetime.now(timezone.utc) - timedelta(days=7)
    item = _build_podcast_item(entry, "show", {"name": "Show"}, cutoff_dt=cutoff)
    assert item is not None
    assert item["entry_id"] == "guid/2"


def test_build_podcast_item_fallback_id_uses_link() -> None:
    """Entries missing <guid> fall back to <link> as entry_id."""
    fresh = (datetime.now(timezone.utc) - timedelta(hours=1)).timetuple()
    entry = _pod_entry(
        title="No-guid episode",
        link="https://show.example.com/ep/no-guid",
        audio_url="https://cdn.example.com/no-guid.mp3",
        guid=None,  # _pod_entry defaults guid to link
        published_parsed=fresh,
    )
    item = _build_podcast_item(entry, "show", {"name": "Show"}, cutoff_dt=None)
    assert item is not None
    assert item["entry_id"] == "https://show.example.com/ep/no-guid"


# ---------------------------------------------------------------------------
# Strategy run — podcast flavor
# ---------------------------------------------------------------------------


def _write_priorities_podcast(
    vault_root: Path, outlet_slug: str, feeds: list[str], **outlet_extra: Any
) -> None:
    """Seed ``config/PRIORITIES.yaml::intake.podcast_events.outlets`` with one show."""
    (vault_root / "config").mkdir(parents=True, exist_ok=True)
    lines = ["intake:", "  podcast_events:", "    outlets:", f"      {outlet_slug}:"]
    for k, v in {"name": outlet_slug, **outlet_extra}.items():
        lines.append(f"        {k}: {v}")
    feeds_inline = ", ".join(feeds)
    lines.append(f"        feeds: [{feeds_inline}]")
    (vault_root / "config" / "PRIORITIES.yaml").write_text(
        "\n".join(lines) + "\n", encoding="utf-8"
    )


def test_strategy_polls_podcast_feeds_and_enqueues(
    tmp_path, fake_feedparser, monkeypatch
) -> None:
    fresh = (datetime.now(timezone.utc) - timedelta(hours=2)).timetuple()
    entries = [
        _pod_entry(
            title="Episode A",
            link="https://show.example.com/ep/a",
            audio_url="https://cdn.example.com/a.mp3",
            guid="guid/a",
            published_parsed=fresh,
            duration="45:00",
            episode=10,
        ),
        _pod_entry(
            title="Episode B",
            link="https://show.example.com/ep/b",
            audio_url="https://cdn.example.com/b.mp3",
            guid="guid/b",
            published_parsed=fresh,
            duration="01:02:00",
            episode=11,
        ),
    ]
    monkeypatch.setattr(fake_feedparser, "parse", lambda url: FakeParsed(entries))

    _write_priorities_podcast(
        tmp_path,
        outlet_slug="macro-trading-floor",
        feeds=["https://feeds.example.com/macro-trading-floor"],
        tier=1,
        daily_cap=5,
        language="en",
    )

    config = {
        "sources": {
            "podcast-events": {
                "lookback_days": 7,
                "dedup_keys": ["entry_id", "audio_url", "url"],
            }
        }
    }
    descriptors = RssPollStrategy().run(_fake_vault(tmp_path), None, config)
    enqueued = [d for d in descriptors if d.get("kind") == "enqueued"]
    summaries = [d for d in descriptors if d.get("kind") == "summary"]

    assert len(enqueued) == 2
    assert summaries[0]["stats"]["enqueued"] == 2
    assert all(d["source_type"] == "podcast-events" for d in enqueued)

    items = Queue.for_source_type("podcast-events", tmp_path).peek(10)
    audio_urls = {it.get("audio_url") for it in items}
    entry_ids = {it.get("entry_id") for it in items}
    assert audio_urls == {
        "https://cdn.example.com/a.mp3",
        "https://cdn.example.com/b.mp3",
    }
    assert entry_ids == {"guid/a", "guid/b"}


def test_strategy_podcast_lookback_filters_stale(
    tmp_path, fake_feedparser, monkeypatch
) -> None:
    """Episodes older than lookback_days are skipped, counted in stale_lookback."""
    old = (datetime.now(timezone.utc) - timedelta(days=30)).timetuple()
    fresh = (datetime.now(timezone.utc) - timedelta(hours=12)).timetuple()
    entries = [
        _pod_entry(
            title="Old",
            link="https://show.example.com/old",
            audio_url="https://cdn.example.com/old.mp3",
            guid="guid/old",
            published_parsed=old,
        ),
        _pod_entry(
            title="Fresh",
            link="https://show.example.com/fresh",
            audio_url="https://cdn.example.com/fresh.mp3",
            guid="guid/fresh",
            published_parsed=fresh,
        ),
    ]
    monkeypatch.setattr(fake_feedparser, "parse", lambda url: FakeParsed(entries))

    _write_priorities_podcast(
        tmp_path, outlet_slug="show", feeds=["https://feeds.example.com/show"]
    )

    config = {
        "sources": {
            "podcast-events": {
                "lookback_days": 7,
                "dedup_keys": ["entry_id"],
            }
        }
    }
    descriptors = RssPollStrategy().run(_fake_vault(tmp_path), None, config)
    summary = next(d for d in descriptors if d.get("kind") == "summary")
    assert summary["stats"]["enqueued"] == 1
    assert summary["stats"]["stale_lookback"] == 1


def test_strategy_podcast_daily_cap_enforced(
    tmp_path, fake_feedparser, monkeypatch
) -> None:
    """Outlet's daily_cap caps per-show enqueues per UTC day."""
    fresh = (datetime.now(timezone.utc) - timedelta(hours=1)).timetuple()
    entries = [
        _pod_entry(
            title=f"Ep {i}",
            link=f"https://show.example.com/ep/{i}",
            audio_url=f"https://cdn.example.com/{i}.mp3",
            guid=f"guid/{i}",
            published_parsed=fresh,
        )
        for i in range(5)
    ]
    monkeypatch.setattr(fake_feedparser, "parse", lambda url: FakeParsed(entries))

    _write_priorities_podcast(
        tmp_path,
        outlet_slug="show",
        feeds=["https://feeds.example.com/show"],
        daily_cap=2,  # cap at 2 even though 5 are fresh
    )

    config = {
        "sources": {
            "podcast-events": {
                "lookback_days": 7,
                "dedup_keys": ["entry_id"],
            }
        }
    }
    descriptors = RssPollStrategy().run(_fake_vault(tmp_path), None, config)
    summary = next(d for d in descriptors if d.get("kind") == "summary")
    assert summary["stats"]["enqueued"] == 2
    assert summary["stats"]["cap_hit"] == 3
