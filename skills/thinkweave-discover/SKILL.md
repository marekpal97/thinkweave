---
name: thinkweave-discover
description: "Strategy-driven discovery. Runs the configured strategy list; strategies emit gap descriptors and (for external-trigger ones) directly enqueue. The single producer rail in the discover→drain spine."
---

# Codex projection for `/discover`

Read the [canonical ThinkWeave command contract](../../commands/discover.md) completely, then execute it. The linked file is the
semantic source of truth; this file only adapts harness vocabulary.

Use Codex-native equivalents for capabilities named in the canonical
contract: filesystem read/search for `Read`/`Grep`, the shell runner for
`Bash`, `apply_patch` for `Write`/`Edit`, the web tool for
`WebFetch`/`WebSearch`, and the user-input tool for `AskUserQuestion`.
When it names another `/skill`, read and follow the sibling
`../thinkweave-<skill>/SKILL.md` projection; `$thinkweave-<skill>` is
Codex's user-facing invocation spelling.
