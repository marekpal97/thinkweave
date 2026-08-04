---
name: thinkweave-drain
description: "Drain a per-source-type acquisition queue. Source-type-agnostic — config in `vault/config/sources.yaml` decides whether items dispatch to a per-type research skill (sequential) or fan out to subagents (parallel)."
---

# Codex projection for `/drain`

Read the [canonical ThinkWeave command contract](../../commands/drain.md) completely, then execute it. The linked file is the
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

- [`news-triage-worker`](../../agents/news-triage-worker.md)
- [`research-news-worker`](../../agents/research-news-worker.md)

> **Codex support status:** Interactive worker fan-out is supported
> through the native subagent projection above. Unattended/headless
> orchestration remains **degraded** until issue #110 supplies the
> dedicated executor; do not claim cron parity in the meantime.
