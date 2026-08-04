---
name: thinkweave-onboard
description: "First-run onboarding — pre-flight checks, vault wiring, seed vault from prior Claude Code history, bootstrap ontology, configure focus + sources (validated against user-supplied sample files), install hooks (global by default), optionally install cron block, run end-to-end smoke test, emit landing docs."
---

# Codex projection for `/onboard`

Read the [canonical ThinkWeave command contract](../../commands/onboard.md) completely, then execute it. The linked file is the
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

- [`research-news-worker`](../../agents/research-news-worker.md)
- [`research-newsletter-worker`](../../agents/research-newsletter-worker.md)
