---
name: thinkweave-podcast
description: "Thin orchestrator over the podcast intake rails. Calls the `rss_poll` discover strategy to enqueue from per-show RSS feeds, then `/drain --source-type podcast-*` to fan out writer subagents. Headless-safe."
---

# Codex projection for `/podcast`

Read the [canonical ThinkWeave command contract](../../commands/podcast.md) completely, then execute it. The linked file is the
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

- [`research-podcast-worker`](../../agents/research-podcast-worker.md)
