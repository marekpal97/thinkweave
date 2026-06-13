"""Tests for PRIORITIES.yaml intake reads in the rss_poll strategy.

Verifies that ``intake.<slug>.outlets`` (news/podcast) and
``intake.<slug>.channels`` (youtube) drive feed discovery, and that
inline ``channels:`` in ``sources.yaml`` survives as the youtube
fallback when PRIORITIES is unset. The standalone ``*_feeds.yaml``
``feed_config`` pointer was retired 2026-06-13.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from thinkweave.acquisition.discover.strategies.rss_poll import RssPollStrategy


class _FakeVault:
    def __init__(self, vault_root: Path):
        self.vault_root = vault_root
        self.index_db = vault_root / ".weave" / "index.db"
        self.config = self


def _write_priorities(vault_root: Path, body: str) -> None:
    cfg = vault_root / "config"
    cfg.mkdir(parents=True, exist_ok=True)
    (cfg / "PRIORITIES.yaml").write_text(body, encoding="utf-8")


def test_news_outlets_from_priorities(tmp_path: Path):
    """When intake.news.outlets is set, the strategy uses it for feed discovery."""
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
    strategy = RssPollStrategy()

    feeds = strategy._feed_urls_for(
        "news",
        {"dedup_keys": ["url"]},
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


def test_news_without_priorities_returns_empty(tmp_path: Path):
    """News flavor has no fallback once feed_config is retired — empty intake
    means no feeds (the standalone *_feeds.yaml path is gone)."""
    strategy = RssPollStrategy()
    # A stray legacy file at the old location must NOT be consulted.
    weave_dir = tmp_path / ".weave"
    weave_dir.mkdir(parents=True, exist_ok=True)
    (weave_dir / "news_feeds.yaml").write_text(
        "outlets:\n  legacy-outlet:\n    feeds: [https://legacy.example.com/rss]\n",
        encoding="utf-8",
    )

    feeds = strategy._feed_urls_for("news", {"dedup_keys": ["url"]}, tmp_path, intake_block={})
    assert feeds == []


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
