---
name: thinkweave-research-youtube
description: "Resolve a single pasted YouTube URL into a queue item and drain one `research-youtube-worker`. Called from `/research` (router). The one-shot analog of `/youtube`'s discover+drain — same two rails, one video."
---

# Codex projection for `/research-youtube`

Read the [shared Codex adapter](../../docs/CODEX-SKILL-PROJECTION.md)
and the [canonical ThinkWeave command contract](../../commands/research/research-youtube.md) completely, then execute the canonical contract through
that adapter.

Use the adapter's native `spawn_agent` procedure for these shared
worker contracts:

- [`research-youtube-worker`](../../agents/research-youtube-worker.md)
