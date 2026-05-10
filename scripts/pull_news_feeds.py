#!/usr/bin/env python3
"""Pull RSS feeds into the news acquisition queue.

Run from cron (hourly or every 4h). For each outlet declared in
``vault/.mem/news_feeds.yaml``:

  - parse every feed URL via feedparser (tolerates dead URLs — warns and skips),
  - dedup each entry against the active queue + recent archive
    (``Queue.dedup_check``),
  - dedup against the SQLite indexer (URL already a note? — covers the
    >30 day archive horizon),
  - respect per-outlet daily caps,
  - capture ``content:encoded`` as ``embedded_body`` when ``prefer_embedded=true``,
  - enqueue new items into ``vault/.mem/queues/news.jsonl``.

The fetch + concept extraction + mem_create happens later in
``/drain --source-type news``. This script only stages links.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from personal_mem.core.config import load_config
from personal_mem.sources.config import _parse_simple_yaml
from personal_mem.sources.queue import Queue

SOURCE_TYPE = "news"


def main() -> int:
    cfg = load_config()
    feed_config_path = cfg.vault_root / ".mem" / "news_feeds.yaml"
    if not feed_config_path.exists():
        print(f"news_feeds.yaml not found at {feed_config_path}", file=sys.stderr)
        return 1

    try:
        doc = _parse_simple_yaml(feed_config_path.read_text(encoding="utf-8"))
    except ValueError as exc:
        print(f"malformed {feed_config_path}: {exc}", file=sys.stderr)
        return 2

    outlets = doc.get("outlets") or {}
    if not isinstance(outlets, dict) or not outlets:
        print(f"no outlets declared in {feed_config_path}", file=sys.stderr)
        return 0

    try:
        import feedparser
    except ImportError:
        print(
            "feedparser is required. Install: uv add --optional news feedparser",
            file=sys.stderr,
        )
        return 3

    queue = Queue.for_source_type(SOURCE_TYPE, cfg.vault_root)
    indexer_urls = _load_indexer_urls(cfg.index_db)
    enqueue_counts_today = _count_today_per_outlet(queue)

    stats = {
        "outlets": 0,
        "feeds": 0,
        "entries_seen": 0,
        "enqueued": 0,
        "dup_queue": 0,
        "dup_indexer": 0,
        "cap_hit": 0,
        "feed_errors": 0,
    }

    for slug, conf in outlets.items():
        if not isinstance(conf, dict):
            continue
        stats["outlets"] += 1
        feeds = conf.get("feeds") or []
        if isinstance(feeds, str):
            feeds = [feeds]
        cap = int(conf.get("daily_cap", 10))
        prefer_embedded = bool(conf.get("prefer_embedded", False))
        already_today = enqueue_counts_today.get(slug, 0)

        for feed_url in feeds:
            stats["feeds"] += 1
            parsed = _safe_parse(feedparser, feed_url, slug)
            if parsed is None:
                stats["feed_errors"] += 1
                continue
            for entry in parsed.entries:
                stats["entries_seen"] += 1
                if already_today >= cap:
                    stats["cap_hit"] += 1
                    continue
                item = _build_item(entry, slug, conf, prefer_embedded)
                if item is None:
                    continue
                if item["url"] in indexer_urls:
                    stats["dup_indexer"] += 1
                    continue
                if queue.dedup_check(item, ["url", "entry_id"]):
                    stats["dup_queue"] += 1
                    continue
                queue.enqueue(item)
                stats["enqueued"] += 1
                already_today += 1

    print(json.dumps(stats, indent=2))
    return 0


def _safe_parse(feedparser_mod: Any, url: str, slug: str) -> Any | None:
    try:
        parsed = feedparser_mod.parse(url)
    except Exception as exc:  # noqa: BLE001 — third-party can raise anything
        print(f"  ! {slug}: feed {url} raised: {exc}", file=sys.stderr)
        return None
    if parsed.bozo and not parsed.entries:
        # bozo with entries usually means a benign XML quirk; bozo without
        # entries means the feed is unusable.
        reason = getattr(parsed, "bozo_exception", "unknown")
        print(f"  ! {slug}: feed {url} unusable (bozo={reason})", file=sys.stderr)
        return None
    return parsed


def _build_item(
    entry: Any,
    outlet_slug: str,
    outlet_conf: dict,
    prefer_embedded: bool,
) -> dict | None:
    url = (entry.get("link") or "").strip()
    if not url:
        return None
    title = (entry.get("title") or "").strip()
    summary = (entry.get("summary") or "").strip()
    published = entry.get("published") or entry.get("updated") or ""
    entry_id = entry.get("id") or url

    embedded_body: str | None = None
    if prefer_embedded:
        content = entry.get("content") or []
        if isinstance(content, list) and content:
            value = content[0].get("value") if isinstance(content[0], dict) else None
            if isinstance(value, str) and value.strip():
                embedded_body = value.strip()

    return {
        "url": url,
        "title": title,
        "summary": summary,
        "published": str(published),
        "entry_id": str(entry_id),
        "outlet": outlet_slug,
        "outlet_name": outlet_conf.get("name", outlet_slug),
        "tier": int(outlet_conf.get("tier", 2)),
        "region": outlet_conf.get("region", "global"),
        "language": outlet_conf.get("language", "en"),
        "prefer_embedded": prefer_embedded,
        "embedded_body": embedded_body,
    }


def _load_indexer_urls(db_path: Path) -> set[str]:
    """Return URLs already on file (type='source' notes).

    Covers the lookback gap: ``Queue.dedup_check`` only sees the last 30
    days of archive, but a news URL could have been ingested months ago
    and a feed re-emit would re-enqueue it.
    """
    if not db_path.exists():
        return set()
    try:
        conn = sqlite3.connect(str(db_path))
        rows = conn.execute(
            "SELECT json_extract(frontmatter, '$.url') FROM notes "
            "WHERE type = 'source' AND frontmatter IS NOT NULL"
        ).fetchall()
        conn.close()
    except sqlite3.Error:
        return set()
    return {r[0] for r in rows if r[0]}


def _count_today_per_outlet(queue: Queue) -> dict[str, int]:
    """{outlet_slug: count_seen_today} — active queue + today's archive.

    Daily cap counts items that have already been *seen today* across both
    states, so an item that was enqueued at 09:00 and drained by 13:00
    still counts toward the cap for the rest of the day.
    """
    today = datetime.now(timezone.utc).date()
    today_start = datetime(
        today.year, today.month, today.day, tzinfo=timezone.utc
    ).isoformat(timespec="seconds")

    out: dict[str, int] = {}
    for item in queue.peek(10_000):
        if (item.get("enqueued_at") or "") >= today_start:
            slug = item.get("outlet", "")
            if slug:
                out[slug] = out.get(slug, 0) + 1

    archive_file = queue.archive_root / today.isoformat() / f"{SOURCE_TYPE}.jsonl"
    if archive_file.exists():
        for line in archive_file.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            slug = row.get("outlet", "")
            if slug:
                out[slug] = out.get(slug, 0) + 1

    return out


if __name__ == "__main__":
    sys.exit(main())
