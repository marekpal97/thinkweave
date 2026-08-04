---
name: thinkweave-newsletter
description: "Orchestrator over the email-newsletter intake rails. Probes the Gmail connector, reads the per-type `mail_poll` discover-strategy plan (effective_query + processed_label), fetches threads via Gmail MCP, enqueues, drains, then applies the processed_label. Headless-safe."
---

# Codex projection for `/newsletter`

Read the [canonical ThinkWeave command contract](../../commands/newsletter.md) completely, then execute it. The linked file is the
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

- [`research-newsletter-worker`](../../agents/research-newsletter-worker.md)
