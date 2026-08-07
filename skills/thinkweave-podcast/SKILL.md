---
name: thinkweave-podcast
description: "Thin orchestrator over the podcast intake rails. Calls the `rss_poll` discover strategy to enqueue from per-show RSS feeds, then `/drain --source-type podcast-*` to fan out writer subagents. Headless-safe."
---

# Codex projection for `/podcast`

Read the [shared Codex adapter](../../docs/CODEX-SKILL-PROJECTION.md)
and the [canonical ThinkWeave command contract](../../commands/podcast.md) completely, then execute the canonical contract through
that adapter.

Use the adapter's native `spawn_agent` procedure for these shared
worker contracts:

- [`research-podcast-worker`](../../agents/research-podcast-worker.md)
