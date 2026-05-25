#!/usr/bin/env python3
"""DEPRECATED — replaced by the ``rss_poll`` discover strategy.

Historically this script was the standalone cron entry point that polled
RSS feeds and enqueued items into ``vault/.mem/queues/news.jsonl``. It
has been folded into the discover-strategy registry:

    mem discover --strategy rss_poll --source-type news

The new path is generic over source_type (also handles ``youtube-events``
/ ``youtube-concepts`` whose ``channels:`` config replaces the
``feed_config:`` route), keeps the same queue-side + indexer-side dedup,
and lives next to the other strategies for visibility.

This shim preserves the old invocation for backwards compatibility: it
prints a deprecation warning to stderr, then runs the strategy and
prints its summary on stdout. Update your crontab to call ``mem
discover`` directly.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from personal_mem.core.config import load_config
from personal_mem.core.vault import VaultManager
from personal_mem.discover import get
from personal_mem.sources import load_user_config


def main() -> int:
    print(
        "DEPRECATED: scripts/pull_news_feeds.py — use "
        "`mem discover --strategy rss_poll --source-type news` instead.",
        file=sys.stderr,
    )
    cfg = load_config()
    user_cfg = dict(load_user_config(cfg.vault_root))
    user_cfg["_runtime"] = {"source_type": "news"}

    vm = VaultManager(config=cfg)
    strategy = get("rss_poll")
    descriptors = strategy.run(vm, None, user_cfg)

    # Preserve the script's historical stdout shape (a single stats blob)
    # by extracting the news summary row.
    for d in descriptors:
        if d.get("kind") == "summary" and d.get("source_type") == "news":
            print(json.dumps(d.get("stats", {}), indent=2))
            return 0

    # No summary row means no source-type was polled — surface why.
    errors = [d for d in descriptors if d.get("status") == "error"]
    if errors:
        for err in errors:
            print(
                f"error: {err.get('reason', 'unknown')} — {err.get('hint', '')}",
                file=sys.stderr,
            )
        return 1
    print("{}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
