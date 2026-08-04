---
name: thinkweave-research-podcast
description: "Resolve a single pasted podcast RSS-feed URL into a queue item for its latest episode and drain one `research-podcast-worker`. Called from `/research` (router). The one-shot analog of `/podcast`'s discover+drain."
---

# Codex projection for `/research-podcast`

Read the [shared Codex adapter](../../docs/CODEX-SKILL-PROJECTION.md)
and the [canonical ThinkWeave command contract](../../commands/research/research-podcast.md) completely, then execute the canonical contract through
that adapter.

Use the adapter's native `spawn_agent` procedure for these shared
worker contracts:

- [`research-podcast-worker`](../../agents/research-podcast-worker.md)
