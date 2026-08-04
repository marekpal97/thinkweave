---
name: thinkweave-hubs-link
description: "Inline temporal-DAG linkage for concept hubs — walk hubs lacking agrees/contradicts/extends flags and rewrite via the running model. The `weave hubs link --via inline` path; pairs with `--via batch` which fans out via the API wrapper."
---

# Codex projection for `/hubs-link`

Read the [canonical ThinkWeave command contract](../../commands/hubs-link.md) completely, then execute it. The linked file is the
semantic source of truth; this file only adapts harness vocabulary.

Use Codex-native equivalents for capabilities named in the canonical
contract: filesystem read/search for `Read`/`Grep`, the shell runner for
`Bash`, `apply_patch` for `Write`/`Edit`, the web tool for
`WebFetch`/`WebSearch`, and the user-input tool for `AskUserQuestion`.
When it names another `/skill`, read and follow the sibling
`../thinkweave-<skill>/SKILL.md` projection; `$thinkweave-<skill>` is
Codex's user-facing invocation spelling.
