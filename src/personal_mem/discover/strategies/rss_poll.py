"""rss_poll discover strategy — generic RSS / Atom feed polling.

Replaces the standalone ``scripts/pull_news_feeds.py`` and ``/youtube``
step 1. The strategy discovers pollable source types from ``sources.yaml``:

- Types with ``feed_config: <path>`` AND slug starting with ``podcast-``
  use the **podcast flavor** (outlets-yaml-driven like news, but each
  entry carries an ``<enclosure>`` audio URL; items get ``audio_url``,
  ``duration_sec``, ``episode_number`` for the worker).
- Other types with ``feed_config: <path>`` use the **news flavor**
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
from personal_mem.sources.priorities import intake_for, load_priorities
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

        # Phase 3.1 — PRIORITIES.yaml intake reads.
        # When PRIORITIES.yaml::intake.<source_type> is present, its registry
        # (outlets/feeds/channels) supersedes the legacy reads from
        # news_feeds.yaml / podcast_events_feeds.yaml / sources.yaml inline.
        # Absent → legacy fallback. Fail-open (load_priorities returns {}).
        priorities = load_priorities(Path(vault_root))

        for slug, spec in sources.items():
            if filter_type and slug != filter_type:
                continue
            if not isinstance(spec, dict):
                continue
            intake_block = intake_for(priorities, slug)
            feeds = self._feed_urls_for(slug, spec, Path(vault_root), intake_block)
            if not feeds:
                continue
            # Merge intake-block-level overrides into the spec view the
            # per-source poller uses (lookback_days, drain_batch_max).
            # The intake block wins for fields it sets; spec fields fill
            # in everything else (queue path, dedup_keys, etc).
            effective_spec = dict(spec)
            for key in ("lookback_days", "drain_batch_max"):
                if key in intake_block:
                    effective_spec[key] = intake_block[key]
            descriptors.extend(
                self._poll_source(
                    slug=slug,
                    spec=effective_spec,
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
        self,
        slug: str,
        spec: dict[str, Any],
        vault_root: Path,
        intake_block: dict[str, Any],
    ) -> list[tuple[str, dict[str, Any]]]:
        """Return ``[(feed_url, meta), ...]`` for a source type.

        Resolution order:
        1. ``intake_block.outlets`` (PRIORITIES.yaml — news/podcast flavors)
        2. ``intake_block.channels`` (PRIORITIES.yaml — youtube flavor)
        3. ``spec.feed_config`` (legacy news_feeds.yaml / podcast_events_feeds.yaml)
        4. ``spec.channels`` (legacy inline list in sources.yaml)

        Meta carries per-feed context the item builder needs:
        - news/podcast flavor → ``{"outlet_slug": ..., "outlet_conf": ...}``
        - youtube flavor → ``{"channel_id": ...}``

        Source types without any recognised config return ``[]``.
        """
        # 1. PRIORITIES.yaml::intake.<slug>.outlets (news / podcast flavor)
        intake_outlets = intake_block.get("outlets") if intake_block else None
        if isinstance(intake_outlets, dict) and intake_outlets:
            return self._outlets_to_feeds(intake_outlets)

        # 2. PRIORITIES.yaml::intake.<slug>.channels (youtube flavor)
        intake_channels = intake_block.get("channels") if intake_block else None
        if isinstance(intake_channels, list) and intake_channels:
            return [
                (
                    f"https://www.youtube.com/feeds/videos.xml?channel_id={cid}",
                    {"channel_id": cid},
                )
                for cid in intake_channels
            ]

        # 3. Legacy feed_config pointer (news_feeds.yaml etc.)
        feed_config = spec.get("feed_config")
        if feed_config:
            # Strip leading ``vault/`` prefix that appears in DEFAULT_CONFIG
            # for visual clarity. The rest of the codebase treats these as
            # vault-rooted paths and ignores the prefix.
            cleaned = feed_config[len("vault/"):] if feed_config.startswith("vault/") else feed_config
            return self._load_news_feeds(vault_root / cleaned)

        # 4. Legacy inline channels list in sources.yaml
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
    def _outlets_to_feeds(
        outlets: dict[str, Any],
    ) -> list[tuple[str, dict[str, Any]]]:
        """Convert ``PRIORITIES.yaml::intake.<slug>.outlets`` into feed tuples.

        Same shape as legacy ``news_feeds.yaml::outlets`` — the migration
        is a YAML key relocation, not a content reshape.
        """
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
        # Flavor derives from slug shape so PRIORITIES.yaml (which doesn't
        # carry feed_config) still routes to the right item builder.
        # Legacy behaviour matched the same branches via feed_config
        # presence — slug-based is equivalent for the shipped source types.
        if slug.startswith("podcast-") or slug.startswith("podcast_"):
            flavor = "podcast"
        elif slug.startswith("youtube-") or slug.startswith("youtube_"):
            flavor = "youtube"
        else:
            flavor = "news"
        # Both news and podcasts honour per-outlet daily caps.
        enqueue_counts_today: dict[str, int] = (
            _count_today_per_outlet(queue, slug) if flavor in ("news", "podcast") else {}
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
                elif flavor == "podcast":
                    outlet_slug = meta["outlet_slug"]
                    outlet_conf = meta["outlet_conf"]
                    cap = int(outlet_conf.get("daily_cap", 5))
                    if enqueue_counts_today.get(outlet_slug, 0) >= cap:
                        stats["cap_hit"] += 1
                        continue
                    item = _build_podcast_item(
                        entry, outlet_slug, outlet_conf, cutoff_dt
                    )
                    if item is None and cutoff_dt is not None:
                        # _build_podcast_item returns None for both
                        # missing-enclosure and stale-pub-date; the
                        # latter is the more common reason when a
                        # cutoff is set, so account for it explicitly.
                        stats["stale_lookback"] += 1
                        continue
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
                if flavor in ("news", "podcast"):
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


def _build_podcast_item(
    entry: Any,
    outlet_slug: str,
    outlet_conf: dict[str, Any],
    cutoff_dt: datetime | None,
) -> dict[str, Any] | None:
    """Build a queue item from a single podcast RSS ``<item>`` entry.

    Pulls:
    - the canonical episode landing page (``link``) for ``url`` — used
      as the human-clickable URL on the source note and for the indexer
      dedup check;
    - the ``<enclosure>`` audio URL for ``audio_url`` — what the worker
      sends to Gemini;
    - ``<itunes:duration>`` and ``<itunes:episode>`` when present;
    - ``<guid>`` for ``entry_id`` (the most stable dedup key, since
      ``audio_url`` can change on CDN migrations).

    Returns ``None`` if there is no enclosure (a podcast feed item with
    no audio is malformed and not worth queuing) or if the published
    date is older than ``cutoff_dt``.
    """
    pub = entry.get("published_parsed")
    if pub:
        pub_dt = datetime(*pub[:6], tzinfo=timezone.utc)
        if cutoff_dt is not None and pub_dt < cutoff_dt:
            return None
        published_iso = pub_dt.isoformat()
    else:
        published_iso = entry.get("published", "") or ""

    audio_url = ""
    audio_type = ""
    audio_length = 0
    enclosures = entry.get("enclosures") or []
    # feedparser exposes enclosures as a list of dicts with keys
    # ``href``, ``type``, ``length``. Prefer the first audio/* entry.
    for enc in enclosures:
        if not isinstance(enc, dict):
            continue
        etype = (enc.get("type") or "").lower()
        href = (enc.get("href") or "").strip()
        if href and (etype.startswith("audio/") or not etype):
            audio_url = href
            audio_type = etype or "audio/mpeg"
            try:
                audio_length = int(enc.get("length") or 0)
            except (TypeError, ValueError):
                audio_length = 0
            break
    if not audio_url:
        return None

    link = (entry.get("link") or "").strip()
    title = (entry.get("title") or "").strip()
    summary = (entry.get("summary") or "").strip()
    entry_id = (entry.get("id") or link or audio_url).strip()
    duration_sec = _parse_itunes_duration(entry.get("itunes_duration"))
    episode_number = _coerce_int_or_none(entry.get("itunes_episode"))

    return {
        "url": link or audio_url,
        "audio_url": audio_url,
        "audio_type": audio_type,
        "audio_length_bytes": audio_length,
        "title": title,
        "summary": summary,
        "published": published_iso,
        "entry_id": entry_id,
        "duration_sec": duration_sec,
        "episode_number": episode_number,
        "outlet": outlet_slug,
        "outlet_name": outlet_conf.get("name", outlet_slug),
        "tier": int(outlet_conf.get("tier", 2)),
        "language": outlet_conf.get("language", "en"),
    }


def _parse_itunes_duration(value: Any) -> int:
    """Parse <itunes:duration> into seconds.

    The spec allows three forms: ``HH:MM:SS``, ``MM:SS``, or bare
    seconds. Returns 0 for unparseable input — duration is informational
    on the queue item, not load-bearing.
    """
    if value is None:
        return 0
    s = str(value).strip()
    if not s:
        return 0
    if ":" in s:
        parts = s.split(":")
        try:
            nums = [int(p) for p in parts]
        except ValueError:
            return 0
        if len(nums) == 3:
            return nums[0] * 3600 + nums[1] * 60 + nums[2]
        if len(nums) == 2:
            return nums[0] * 60 + nums[1]
        return 0
    try:
        return int(float(s))
    except ValueError:
        return 0


def _coerce_int_or_none(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


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
