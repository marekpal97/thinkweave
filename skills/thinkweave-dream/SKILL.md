---
name: thinkweave-dream
description: "Periodic dream cycle — two-phase subagent orchestrator. Phase 1 fans out 5 synthesis workers (promotion/merge/theme/essence/priority), merges plan fragments, applies. Phase 2 fans out 5 composition workers (wrap catch-up, prediction judge, hub seam-link, memory seam, knowledge digest). One cron entry, ten workers, single maintenance.jsonl line per cycle. Owns routine ontology dedup for BOTH hub families (drift v2: cosine + verdict memory) AND CC-auto-memory↔vault reconciliation (the memory seam). Self-deciding, headless-safe."
---

# Codex projection for `/dream`

Read the [canonical ThinkWeave command contract](../../commands/dream.md) completely, then execute it. The linked file is the
semantic source of truth; this file only adapts harness vocabulary.

Use Codex-native equivalents for capabilities named in the canonical
contract: filesystem read/search for `Read`/`Grep`, the shell runner for
`Bash`, `apply_patch` for `Write`/`Edit`, the web tool for
`WebFetch`/`WebSearch`, and the user-input tool for `AskUserQuestion`.
When it names another `/skill`, read and follow the sibling
`../thinkweave-<skill>/SKILL.md` projection; `$thinkweave-<skill>` is
Codex's user-facing invocation spelling.

## Native subagent projection

Translate every canonical `Task` dispatch to Codex's native
`spawn_agent` tool. Read the relevant worker contract below in full
before spawning, and include its complete instructions plus the
task-specific prompt in `message`; a generic subagent does not inherit
the contract by name. Normalize the worker name to a valid `task_name`
by replacing hyphens with underscores. Use `followup_task` for the
contract's retry path and `wait_agent` for fan-in/dependency waves.
Do not use or emit Claude Code Task-call syntax.

Shared worker contracts:

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
