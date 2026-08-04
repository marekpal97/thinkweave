---
name: thinkweave-seed-enrich
description: "Inline session synthesis — walk imported-but-unsynthesised coding-agent sessions (Claude Code and Codex, as separate per-source lanes) and compose a summary + insights + decisions via the running model, weave_extract one wrap-shaped session per transcript. Small backlogs run in-process; large ones deterministically fan out subagents. The keyless `--via inline` half of `weave import {claude-code|codex} --enrich`; pairs with `--via batch` which fans out through the API wrapper."
---

# Codex projection for `/seed-enrich`

Read the [canonical ThinkWeave command contract](../../commands/seed-enrich.md) completely, then execute it. The linked file is the
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

- [`dream-wrap-worker`](../../agents/dream-wrap-worker.md)
- [`seed-enrich-worker`](../../agents/seed-enrich-worker.md)
