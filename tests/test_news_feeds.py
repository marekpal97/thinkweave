"""Tests for scripts/pull_news_feeds.py — RSS-pull → queue logic.

Mocks ``feedparser.parse`` so the suite is hermetic. Covers:

  1. ``_build_item`` happy path + skips for missing link.
  2. ``embedded_body`` is captured iff ``prefer_embedded`` is true and
     the feed entry actually carries ``content[0].value``.
  3. ``main()`` enqueues new entries.
  4. ``main()`` dedups against the active queue.
  5. ``main()`` dedups against the indexer (URL already a source note).
  6. Per-outlet daily cap stops further enqueues from that outlet.
  7. Bozo feed without entries is logged and skipped without crashing.
"""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

import pytest


@pytest.fixture
def pull_module(tmp_path, monkeypatch):
    """Load scripts/pull_news_feeds.py as a module rooted at tmp_path."""
    monkeypatch.setenv("PERSONAL_MEM_VAULT", str(tmp_path))
    repo_root = Path(__file__).resolve().parents[1]
    script_path = repo_root / "scripts" / "pull_news_feeds.py"
    # Force a fresh load each call (the script keeps no global state, but
    # the module cache could be stale between tests if reused).
    if "pull_news_feeds" in sys.modules:
        del sys.modules["pull_news_feeds"]
    spec = importlib.util.spec_from_file_location(
        "pull_news_feeds", script_path
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class FakeParsed:
    """Tiny stand-in for the FeedParserDict we don't want to construct."""

    def __init__(self, entries, bozo=False, bozo_exception=None):
        self.entries = entries
        self.bozo = bozo
        self.bozo_exception = bozo_exception


def _entry(
    link: str = "",
    title: str = "",
    summary: str = "",
    published: str = "",
    entry_id: str = "",
    content=None,
) -> dict:
    e: dict = {
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


def _seed_feeds_yaml(vault_root: Path, outlets: dict) -> None:
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


# ---------------------------------------------------------------------------
# _build_item
# ---------------------------------------------------------------------------


def test_build_item_basic(pull_module):
    entry = _entry(
        link="https://reuters.com/x",
        title="Title",
        summary="Summary",
        published="2026-05-09T13:00:00Z",
        entry_id="r-123",
    )
    conf = {"name": "Reuters", "tier": 1, "region": "global", "language": "en"}
    item = pull_module._build_item(entry, "reuters", conf, prefer_embedded=False)
    assert item["url"] == "https://reuters.com/x"
    assert item["outlet"] == "reuters"
    assert item["outlet_name"] == "Reuters"
    assert item["tier"] == 1
    assert item["entry_id"] == "r-123"
    assert item["embedded_body"] is None


def test_build_item_skips_missing_link(pull_module):
    entry = _entry(link="")
    assert pull_module._build_item(entry, "x", {}, prefer_embedded=False) is None


def test_build_item_falls_back_to_url_for_entry_id(pull_module):
    entry = _entry(link="https://x.com/a")
    item = pull_module._build_item(entry, "x", {}, prefer_embedded=False)
    assert item["entry_id"] == "https://x.com/a"


def test_build_item_captures_embedded_when_prefer_embedded(pull_module):
    entry = _entry(
        link="https://x.com/a",
        content=[{"type": "text/html", "value": "<p>FULL BODY</p>"}],
    )
    item = pull_module._build_item(entry, "x", {}, prefer_embedded=True)
    assert item["embedded_body"] == "<p>FULL BODY</p>"


def test_build_item_no_embedded_when_prefer_embedded_false(pull_module):
    entry = _entry(
        link="https://x.com/a",
        content=[{"type": "text/html", "value": "<p>FULL BODY</p>"}],
    )
    item = pull_module._build_item(entry, "x", {}, prefer_embedded=False)
    assert item["embedded_body"] is None


# ---------------------------------------------------------------------------
# main() integration
# ---------------------------------------------------------------------------


def test_main_enqueues_new_entries(pull_module, tmp_path, monkeypatch, capsys):
    _seed_feeds_yaml(tmp_path, {"reuters": _basic_outlet()})

    fake_entries = [
        _entry(link="https://reuters.com/a", title="A", entry_id="r-1"),
        _entry(link="https://reuters.com/b", title="B", entry_id="r-2"),
    ]
    import feedparser

    monkeypatch.setattr(feedparser, "parse", lambda url: FakeParsed(fake_entries))

    rc = pull_module.main()
    assert rc == 0
    stats = json.loads(capsys.readouterr().out)
    assert stats["enqueued"] == 2
    assert stats["entries_seen"] == 2
    assert stats["dup_queue"] == 0
    assert stats["dup_indexer"] == 0

    queue_path = tmp_path / ".mem" / "queues" / "news.jsonl"
    assert queue_path.exists()
    lines = queue_path.read_text().strip().splitlines()
    assert len(lines) == 2


def test_main_dedups_against_queue(pull_module, tmp_path, monkeypatch, capsys):
    _seed_feeds_yaml(tmp_path, {"reuters": _basic_outlet()})

    from personal_mem.sources.queue import Queue

    q = Queue.for_source_type("news", tmp_path)
    q.enqueue(
        {"url": "https://reuters.com/a", "entry_id": "r-1", "outlet": "reuters"}
    )

    fake_entries = [
        _entry(link="https://reuters.com/a", title="A", entry_id="r-1"),
        _entry(link="https://reuters.com/b", title="B", entry_id="r-2"),
    ]
    import feedparser

    monkeypatch.setattr(feedparser, "parse", lambda url: FakeParsed(fake_entries))

    pull_module.main()
    stats = json.loads(capsys.readouterr().out)
    assert stats["enqueued"] == 1
    assert stats["dup_queue"] == 1


def test_main_dedups_against_indexer(pull_module, tmp_path, monkeypatch, capsys):
    """A URL already a source note in the indexer should not re-enqueue."""
    _seed_feeds_yaml(tmp_path, {"x": _basic_outlet()})

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
    import feedparser

    monkeypatch.setattr(feedparser, "parse", lambda url: FakeParsed(fake_entries))

    pull_module.main()
    stats = json.loads(capsys.readouterr().out)
    assert stats["enqueued"] == 1
    assert stats["dup_indexer"] == 1


def test_main_respects_per_outlet_daily_cap(
    pull_module, tmp_path, monkeypatch, capsys
):
    _seed_feeds_yaml(tmp_path, {"x": _basic_outlet(daily_cap=2)})

    fake_entries = [
        _entry(link=f"https://x.com/{i}", title=f"E{i}", entry_id=f"x-{i}")
        for i in range(5)
    ]
    import feedparser

    monkeypatch.setattr(feedparser, "parse", lambda url: FakeParsed(fake_entries))

    pull_module.main()
    stats = json.loads(capsys.readouterr().out)
    assert stats["enqueued"] == 2
    assert stats["cap_hit"] == 3


def test_main_bozo_feed_logged_and_skipped(
    pull_module, tmp_path, monkeypatch, capsys
):
    _seed_feeds_yaml(tmp_path, {"broken": _basic_outlet()})

    import feedparser

    monkeypatch.setattr(
        feedparser,
        "parse",
        lambda url: FakeParsed([], bozo=True, bozo_exception="parse error"),
    )

    rc = pull_module.main()
    assert rc == 0
    stats = json.loads(capsys.readouterr().out)
    assert stats["enqueued"] == 0
    assert stats["feed_errors"] == 1


def test_main_no_outlets_returns_zero(pull_module, tmp_path, capsys):
    """Empty outlets section — main() must exit cleanly without crashing."""
    (tmp_path / ".mem").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".mem" / "news_feeds.yaml").write_text(
        "outlets:\n", encoding="utf-8"
    )
    rc = pull_module.main()
    assert rc == 0
