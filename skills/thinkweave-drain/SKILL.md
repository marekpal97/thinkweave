---
name: thinkweave-drain
description: "Drain a per-source-type acquisition queue. Source-type-agnostic — config in `vault/config/sources.yaml` decides whether items dispatch to a per-type research skill (sequential) or fan out to subagents (parallel)."
---

# Codex projection for `/drain`

Read the [shared Codex adapter](../../docs/CODEX-SKILL-PROJECTION.md)
and the [canonical ThinkWeave command contract](../../commands/drain.md) completely, then execute the canonical contract through
that adapter.

Use the adapter's native `spawn_agent` procedure for these shared
worker contracts:

- [`news-triage-worker`](../../agents/news-triage-worker.md)
- [`research-news-worker`](../../agents/research-news-worker.md)
- [`research-newsletter-worker`](../../agents/research-newsletter-worker.md)
- [`research-podcast-worker`](../../agents/research-podcast-worker.md)
- [`research-youtube-worker`](../../agents/research-youtube-worker.md)

> **Codex support status:** Interactive worker fan-out is supported
> through the native subagent projection above. Unattended/headless
> orchestration remains **degraded** until issue #110 supplies the
> dedicated executor; do not claim cron parity in the meantime.
