---
name: thinkweave-dream
description: "Periodic dream cycle — two-phase subagent orchestrator. Phase 1 fans out 5 synthesis workers (promotion/merge/theme/essence/priority), merges plan fragments, applies. Phase 2 fans out 5 composition workers (wrap catch-up, prediction judge, hub seam-link, memory seam, knowledge digest). One cron entry, ten workers, single maintenance.jsonl line per cycle. Owns routine ontology dedup for BOTH hub families (drift v2: cosine + verdict memory) AND CC-auto-memory↔vault reconciliation (the memory seam). Self-deciding, headless-safe."
---

# Codex projection for `/dream`

Read the [shared Codex adapter](../../docs/CODEX-SKILL-PROJECTION.md)
and the [canonical ThinkWeave command contract](../../commands/dream.md) completely, then execute the canonical contract through
that adapter.

Use the adapter's native `spawn_agent` procedure for these shared
worker contracts:

- [`dream-digest-worker`](../../agents/dream-digest-worker.md)
- [`dream-essence-worker`](../../agents/dream-essence-worker.md)
- [`dream-judge-worker`](../../agents/dream-judge-worker.md)
- [`dream-merge-worker`](../../agents/dream-merge-worker.md)
- [`dream-outcome-worker`](../../agents/dream-outcome-worker.md)
- [`dream-priority-worker`](../../agents/dream-priority-worker.md)
- [`dream-promotion-worker`](../../agents/dream-promotion-worker.md)
- [`dream-seam-link-worker`](../../agents/dream-seam-link-worker.md)
- [`dream-seam-worker`](../../agents/dream-seam-worker.md)
- [`dream-theme-worker`](../../agents/dream-theme-worker.md)
- [`dream-wrap-worker`](../../agents/dream-wrap-worker.md)

> **Codex support status:** Interactive worker fan-out is supported
> through the native subagent projection above. Unattended/headless
> orchestration remains **degraded** until issue #110 supplies the
> dedicated executor; do not claim cron parity in the meantime.
