---
name: thinkweave-onboard
description: "First-run onboarding — pre-flight checks, vault wiring, seed vault from prior Claude Code history, bootstrap ontology, configure focus + sources (validated against user-supplied sample files), install hooks (global by default), optionally install cron block, run end-to-end smoke test, emit landing docs."
---

# Codex projection for `/onboard`

Read the [shared Codex adapter](../../docs/CODEX-SKILL-PROJECTION.md)
and the [canonical ThinkWeave command contract](../../commands/onboard.md) completely, then execute the canonical contract through
that adapter.

Use the adapter's native `spawn_agent` procedure for these shared
worker contracts:

- [`research-news-worker`](../../agents/research-news-worker.md)
- [`research-newsletter-worker`](../../agents/research-newsletter-worker.md)
