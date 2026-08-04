---
name: thinkweave-import-chatgpt
description: "Inline ChatGPT-export import — walk conversations.json, summarize each thread via the running model, and weave_create one note per conversation. The `weave import chatgpt --via inline` path; pairs with `--via batch` which fans out via the API wrapper instead."
---

# Codex projection for `/import-chatgpt`

Read the [canonical ThinkWeave command contract](../../commands/import-chatgpt.md) completely, then execute it. The linked file is the
semantic source of truth; this file only adapts harness vocabulary.

Use Codex-native equivalents for capabilities named in the canonical
contract: filesystem read/search for `Read`/`Grep`, the shell runner for
`Bash`, `apply_patch` for `Write`/`Edit`, the web tool for
`WebFetch`/`WebSearch`, and the user-input tool for `AskUserQuestion`.
When it names another `/skill`, read and follow the sibling
`../thinkweave-<skill>/SKILL.md` projection; `$thinkweave-<skill>` is
Codex's user-facing invocation spelling.
