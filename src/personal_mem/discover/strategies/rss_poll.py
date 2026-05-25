"""rss_poll discover strategy — generic RSS / Atom feed polling.

Replaces the standalone ``scripts/pull_news_feeds.py`` and ``/youtube``
step 1. The strategy discovers pollable source types from ``sources.yaml``:

- Types with ``feed_config: <path>`` use the **news flavor**
  (outlets-yaml-driven, per-outlet daily caps, ``content:encoded``
  capture when ``prefer_embedded: true``).
- Types with ``channels: [...]`` use the **youtube flavor** (one feed
  per channel ID, items carry ``video_id`` / ``channel`` / ``channel_id``,
  ``lookback_days`` clips stale entries).

Enqueue + dedup happen inside the strategy. Two dedup layers stack: the
queue's own ``dedup_check`` (active + recently-archived items) and the
SQLite indexer (already-noted ``url``). This matches the original script's
behaviour — moving them to ``mem_queue(action="enqueue")`` would require
adding indexer-side dedup there, which is out of scope.

Output is a list of descriptors:

- ``{kind: "enqueued", ...}`` — one per item that got into the queue.
- ``{kind: "summary", ...}`` — one per polled source type, with stats.
- ``{kind: "external", status: "error", ...}`` — fatal config errors
  (e.g. ``feedparser`` not installed).

Optional ``_runtime.source_type`` (set by ``mem discover --source-type``)
limits polling to one source type. Without it, every source type with a
recognised feed configuration is polled.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from personal_mem.sources.config import _parse_simple_yaml
from personal_mem.sources.queue import Queue


class RssPollStrategy:
    name = "rss_poll"

    def run(
        self,
        vault: Any,
        project: str | None,
        config: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        config = config or {}
        runtime = config.get("_runtime") or {}
        filter_type = runtime.get("source_type") or None

        sources = config.get("sources") or {}
        cfg = getattr(vault, "config", None) or vault
        vault_root = getattr(cfg, "vault_root", None)
        if vault_root is None:
            return [
                {
                    "strategy": self.name,
                    "kind": "external",
                    "status": "error",
                    "reason": "vault_root_unknown",
                }
            ]
        db_path = getattr(cfg, "index_db", None)

        try:
            import feedparser
        except ImportError:
            return [
                {
                    "strategy": self.name,
                    "kind": "external",
                    "status": "error",
                    "reason": "feedparser_missing",
                    "hint": "uv add --optional news feedparser",
                }
            ]

        indexer_urls = _load_indexer_urls(db_path)
        descriptors: list[dict[str, Any]] = []

        for slug, spec in sources.items():
            if filter_type and slug != filter_type:
                continue
            if not isinstance(spec, dict):
                continue
            feeds = self._feed_urls_for(slug, spec, Path(vault_root))
            if not feeds:
                continue
            descriptors.extend(
                self._poll_source(
                    slug=slug,
                    spec=spec,
                    feed_urls=feeds,
                    vault_root=Path(vault_root),
                    feedparser_mod=feedparser,
                    indexer_urls=indexer_urls,
                )
            )

        return descriptors

    # ------------------------------------------------------------------
    # Feed-URL discovery — per flavor
    # ------------------------------------------------------------------

    def _feed_urls_for(
        self, slug: str, spec: dict[str, Any], vault_root: Path
    ) -> list[tuple[str, dict[str, Any]]]:
        """Return ``[(feed_url, meta), ...]`` for a source type.

        Meta carries per-feed context the item builder needs:
        - news flavor → ``{"outlet_slug": ..., "outlet_conf": ...}``
        - youtube flavor → ``{"channel_id": ...}``

        Source types without a recognised config key return ``[]``.
        """
        feed_config = spec.get("feed_config")
        if feed_config:
            return self._load_news_feeds(vault_root / feed_config)
        channels = spec.get("channels") or []
        if channels:
            return [
                (
                    f"https://www.youtube.com/feeds/videos.xml?channel_id={cid}",
                    {"channel_id": cid},
                )
                for cid in channels
            ]
        return []

    @staticmethod
    def _load_news_feeds(path: Path) -> list[tuple[str, dict[str, Any]]]:
        if not path.exists():
            return []
        try:
            doc = _parse_simple_yaml(path.read_text(encoding="utf-8"))
        except ValueError:
            return []
        outlets = doc.get("outlets") or {}
        if not isinstance(outlets, dict):
            return []
        out: list[tuple[str, dict[str, Any]]] = []
        for outlet_slug, conf in outlets.items():
            if not isinstance(conf, dict):
                continue
            feeds = conf.get("feeds") or []
            if isinstance(feeds, str):
                feeds = [feeds]
            for feed_url in feeds:
                out.append(
                    (
                        str(feed_url),
                        {"outlet_slug": str(outlet_slug), "outlet_conf": conf},
                    )
                )
        return out

    # ------------------------------------------------------------------
    # Poll one source type
    # ------------------------------------------------------------------

    def _poll_source(
        self,
        slug: str,
        spec: dict[str, Any],
        feed_urls: list[tuple[str, dict[str, Any]]],
        vault_root: Path,
        feedparser_mod: Any,
        indexer_urls: set[str],
    ) -> list[dict[str, Any]]:
        queue = Queue.for_source_type(slug, vault_root)
        dedup_keys = list(spec.get("dedup_keys") or ["url"])
        flavor = "news" if spec.get("feed_config") else "youtube"
        enqueue_counts_today: dict[str, int] = (
            _count_today_per_outlet(queue, slug) if flavor == "news" else {}
        )
        lookback_days = int(spec.get("lookback_days") or 0)
        cutoff_dt: datetime | None = None
        if lookback_days > 0:
            cutoff_dt = datetime.now(timezone.utc) - timedelta(days=lookback_days)

        stats = {
            "feeds": 0,
            "entries_seen": 0,
            "enqueued": 0,
            "dup_queue": 0,
            "dup_indexer": 0,
            "cap_hit": 0,
            "stale_lookback": 0,
            "feed_errors": 0,
        }
        out: list[dict[str, Any]] = []

        for feed_url, meta in feed_urls:
            stats["feeds"] += 1
            parsed = _safe_parse(feedparser_mod, feed_url)
            if parsed is None:
                stats["feed_errors"] += 1
                continue
            for entry in parsed.entries:
                stats["entries_seen"] += 1
                if flavor == "news":
                    outlet_slug = meta["outlet_slug"]
                    outlet_conf = meta["outlet_conf"]
                    cap = int(outlet_conf.get("daily_cap", 10))
                    if enqueue_counts_today.get(outlet_slug, 0) >= cap:
                        stats["cap_hit"] += 1
                        continue
                    item = _build_news_item(entry, outlet_slug, outlet_conf)
                else:
                    item = _build_youtube_item(
                        entry,
                        getattr(parsed, "feed", None),
                        meta["channel_id"],
                        cutoff_dt,
                    )
                    if item is None and cutoff_dt is not None:
                        stats["stale_lookback"] += 1
                        continue
                if item is None:
                    continue
                if item.get("url") and item["url"] in indexer_urls:
                    stats["dup_indexer"] += 1
                    continue
                if queue.dedup_check(item, dedup_keys):
                    stats["dup_queue"] += 1
                    continue
                item_id = queue.enqueue(item)
                stats["enqueued"] += 1
                if flavor == "news":
                    enqueue_counts_today[outlet_slug] = (
                        enqueue_counts_today.get(outlet_slug, 0) + 1
                    )
                out.append(
                    {
                        "strategy": self.name,
                        "kind": "enqueued",
                        "source_type": slug,
                        "queue_item_id": item_id,
                        "url": item.get("url", ""),
                        "title": item.get("title", ""),
                        "outlet": item.get("outlet") or item.get("channel", ""),
                    }
                )
        out.append(
            {
                "strategy": self.name,
                "kind": "summary",
                "source_type": slug,
                "stats": stats,
            }
        )
        return out


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _safe_parse(feedparser_mod: Any, url: str) -> Any | None:
    try:
        parsed = feedparser_mod.parse(url)
    except Exception:  # noqa: BLE001 — third-party can raise anything
        return None
    if parsed.bozo and not parsed.entries:
        return None
    return parsed


def _build_news_item(
    entry: Any, outlet_slug: str, outlet_conf: dict[str, Any]
) -> dict[str, Any] | None:
    url = (entry.get("link") or "").strip()
    if not url:
        return None
    title = (entry.get("title") or "").strip()
    summary = (entry.get("summary") or "").strip()
    published = entry.get("published") or entry.get("updated") or ""
    entry_id = entry.get("id") or url

    embedded_body: str | None = None
    prefer_embedded = bool(outlet_conf.get("prefer_embedded", False))
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


def _build_youtube_item(
    entry: Any,
    feed_meta: Any,
    channel_id: str,
    cutoff_dt: datetime | None,
) -> dict[str, Any] | None:
    vid = getattr(entry, "yt_videoid", None)
    if not vid:
        raw_id = entry.get("id") or ""
        vid = raw_id.split(":")[-1] if raw_id else ""
    if not vid:
        return None
    pub = entry.get("published_parsed")
    if pub:
        pub_dt = datetime(*pub[:6], tzinfo=timezone.utc)
        if cutoff_dt is not None and pub_dt < cutoff_dt:
            return None
        published_iso = pub_dt.isoformat()
    else:
        published_iso = entry.get("published", "")
    channel_name = (
        feed_meta.get("title", channel_id) if isinstance(feed_meta, dict) else channel_id
    )
    if not channel_name and hasattr(feed_meta, "get"):
        channel_name = feed_meta.get("title") or channel_id
    return {
        "video_id": vid,
        "url": entry.get("link", f"https://www.youtube.com/watch?v={vid}"),
        "title": (entry.get("title") or "").strip(),
        "channel": str(channel_name or channel_id),
        "channel_id": channel_id,
        "published": str(published_iso),
        "description": ((entry.get("summary") or "")[:2000]).strip(),
    }


def _load_indexer_urls(db_path: Path | None) -> set[str]:
    """Return the URLs already on file as ``type='source'`` notes.

    Covers the gap beyond ``Queue.dedup_check``'s 30-day archive horizon —
    a URL ingested months ago that re-emits via RSS won't get re-enqueued.
    """
    if db_path is None or not db_path.exists():
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


def _count_today_per_outlet(queue: Queue, source_type: str) -> dict[str, int]:
    """Per-outlet count of items seen today (active queue + today's archive)."""
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

    archive_file = queue.archive_root / today.isoformat() / f"{source_type}.jsonl"
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


STRATEGY = RssPollStrategy()
