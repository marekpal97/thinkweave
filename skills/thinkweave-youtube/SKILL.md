---
name: thinkweave-youtube
description: "Thin orchestrator over the YouTube intake rails. Calls the `rss_poll` discover strategy to enqueue from channel RSS feeds, then `/drain --source-type youtube-*` to fan out writer subagents. Headless-safe."
---

# Codex projection for `/youtube`

Read the [shared Codex adapter](../../docs/CODEX-SKILL-PROJECTION.md)
and the [canonical ThinkWeave command contract](../../commands/youtube.md) completely, then execute the canonical contract through
that adapter.

Use the adapter's native `spawn_agent` procedure for these shared
worker contracts:

- [`research-youtube-worker`](../../agents/research-youtube-worker.md)
