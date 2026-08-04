---
name: thinkweave-news
description: "One-off URL ingest for news articles, mid-conversation. Dispatches a Sonnet writer directly; no triage gate (you've already decided this is worth briefing)."
---

# Codex projection for `/news`

Read the [shared Codex adapter](../../docs/CODEX-SKILL-PROJECTION.md)
and the [canonical ThinkWeave command contract](../../commands/news.md) completely, then execute the canonical contract through
that adapter.

Use the adapter's native `spawn_agent` procedure for these shared
worker contracts:

- [`research-news-worker`](../../agents/research-news-worker.md)
