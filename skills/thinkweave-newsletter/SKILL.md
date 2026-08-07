---
name: thinkweave-newsletter
description: "Orchestrator over the email-newsletter intake rails. Probes the Gmail connector, reads the per-type `mail_poll` discover-strategy plan (effective_query + processed_label), fetches threads via Gmail MCP, enqueues, drains, then applies the processed_label. Headless-safe."
---

# Codex projection for `/newsletter`

Read the [shared Codex adapter](../../docs/CODEX-SKILL-PROJECTION.md)
and the [canonical ThinkWeave command contract](../../commands/newsletter.md) completely, then execute the canonical contract through
that adapter.

Use the adapter's native `spawn_agent` procedure for these shared
worker contracts:

- [`research-newsletter-worker`](../../agents/research-newsletter-worker.md)
