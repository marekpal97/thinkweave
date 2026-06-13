"""Tests for PRIORITIES.yaml intake reads in the rss_poll strategy.

Verifies that ``intake.<slug>.outlets`` (news/podcast) and
``intake.<slug>.channels`` (youtube) supersede the legacy reads from
``news_feeds.yaml``, ``podcast_events_feeds.yaml``, and inline
``channels:`` in ``sources.yaml``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from personal_mem.acquisition.discover.strategies.rss_poll import RssPollStrategy


class _FakeVault:
    def __init__(self, vault_root: Path):
        self.vault_root = vault_root
        self.index_db = vault_root / ".mem" / "index.db"
        self.config = self


def _write_priorities(vault_root: Path, body: str) -> None:
    cfg = vault_root / "config"
    cfg.mkdir(parents=True, exist_ok=True)
    (cfg / "PRIORITIES.yaml").write_text(body, encoding="utf-8")


def test_news_outlets_from_priorities(tmp_path: Path):
    """When intake.news.outlets is set, the strategy uses it (not feed_config)."""
    _write_priorities(
        tmp_path,
        "intake:\n"
        "  news:\n"
        "    outlets:\n"
        "      reuters:\n"
        "        name: Reuters\n"
        "        feeds: [https://example.com/rss]\n"
        "        tier: 1\n"
        "        daily_cap: 5\n",
    )
    vault = _FakeVault(tmp_path)
    strategy = RssPollStrategy()
    # The legacy news_feeds.yaml does NOT exist — old route would return []
    sources = {"news": {"feed_config": "vault/.mem/news_feeds.yaml"}}

    feeds = strategy._feed_urls_for(
        "news",
        sources["news"],
        tmp_path,
        intake_block=(
            {"outlets": {"reuters": {"name": "Reuters", "feeds": ["https://example.com/rss"], "tier": 1, "daily_cap": 5}}}
        ),
    )

    urls = [f[0] for f in feeds]
    assert "https://example.com/rss" in urls
    # outlet meta carried through for the news item builder
    assert feeds[0][1]["outlet_slug"] == "reuters"
    assert feeds[0][1]["outlet_conf"]["tier"] == 1


def test_youtube_channels_from_priorities(tmp_path: Path):
    """intake.youtube_concepts.channels supersedes spec.channels (inline)."""
    strategy = RssPollStrategy()
    spec = {"channels": ["UCold-legacy-id"]}
    intake_block = {"channels": ["UCnew-priorities-id"]}

    feeds = strategy._feed_urls_for(
        "youtube-concepts", spec, tmp_path, intake_block
    )

    urls = [f[0] for f in feeds]
    # Priorities-listed channel wins; legacy spec.channels is ignored
    assert "https://www.youtube.com/feeds/videos.xml?channel_id=UCnew-priorities-id" in urls
    assert "https://www.youtube.com/feeds/videos.xml?channel_id=UCold-legacy-id" not in urls


def test_legacy_feed_config_fallback(tmp_path: Path):
    """When PRIORITIES.yaml intake block is empty, fall back to feed_config."""
    # Place a legacy news_feeds.yaml at the legacy location
    mem_dir = tmp_path / ".mem"
    mem_dir.mkdir(parents=True, exist_ok=True)
    (mem_dir / "news_feeds.yaml").write_text(
        "outlets:\n"
        "  legacy-outlet:\n"
        "    name: Legacy\n"
        "    feeds: [https://legacy.example.com/rss]\n"
        "    tier: 2\n",
        encoding="utf-8",
    )

    strategy = RssPollStrategy()
    spec = {"feed_config": "vault/.mem/news_feeds.yaml"}
    feeds = strategy._feed_urls_for("news", spec, tmp_path, intake_block={})

    urls = [f[0] for f in feeds]
    assert "https://legacy.example.com/rss" in urls
    assert feeds[0][1]["outlet_slug"] == "legacy-outlet"


def test_legacy_channels_inline_fallback(tmp_path: Path):
    """When neither PRIORITIES nor feed_config: fall back to spec.channels."""
    strategy = RssPollStrategy()
    spec = {"channels": ["UCinline-id"]}
    feeds = strategy._feed_urls_for("youtube-events", spec, tmp_path, intake_block={})

    urls = [f[0] for f in feeds]
    assert "https://www.youtube.com/feeds/videos.xml?channel_id=UCinline-id" in urls


def test_no_config_returns_empty(tmp_path: Path):
    """Source type with no intake, no feed_config, no channels → no feeds."""
    strategy = RssPollStrategy()
    feeds = strategy._feed_urls_for("news", {}, tmp_path, intake_block={})
    assert feeds == []


def test_outlets_to_feeds_handles_string_feed(tmp_path: Path):
    """A single feed string (not a list) coerces to a one-element list."""
    feeds = RssPollStrategy._outlets_to_feeds(
        {"single-feed-outlet": {"name": "S", "feeds": "https://single.example/rss"}}
    )
    assert len(feeds) == 1
    assert feeds[0][0] == "https://single.example/rss"
