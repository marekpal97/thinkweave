---
name: thinkweave-seed-enrich
description: "Inline session synthesis — walk imported-but-unsynthesised coding-agent sessions (Claude Code and Codex, as separate per-source lanes) and compose a summary + insights + decisions via the running model, weave_extract one wrap-shaped session per transcript. Small backlogs run in-process; large ones deterministically fan out subagents. The keyless `--via inline` half of `weave import {claude-code|codex} --enrich`; pairs with `--via batch` which fans out through the API wrapper."
---

# Codex projection for `/seed-enrich`

Read the [shared Codex adapter](../../docs/CODEX-SKILL-PROJECTION.md)
and the [canonical ThinkWeave command contract](../../commands/seed-enrich.md) completely, then execute the canonical contract through
that adapter.

Use the adapter's native `spawn_agent` procedure for these shared
worker contracts:

- [`seed-enrich-worker`](../../agents/seed-enrich-worker.md)
