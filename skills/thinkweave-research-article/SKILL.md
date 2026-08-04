---
name: thinkweave-research-article
description: "Fetch a web article, extract argument + claims + evidence, write it as a `source_type: article` note. Called from `/research` (router) or `/drain --source-type article`."
---

# Codex projection for `/research-article`

Read the [canonical ThinkWeave command contract](../../commands/research/research-article.md) completely, then execute it. The linked file is the
semantic source of truth; this file only adapts harness vocabulary.

Use Codex-native equivalents for capabilities named in the canonical
contract: filesystem read/search for `Read`/`Grep`, the shell runner for
`Bash`, `apply_patch` for `Write`/`Edit`, the web tool for
`WebFetch`/`WebSearch`, and the user-input tool for `AskUserQuestion`.
When it names another `/skill`, read and follow the sibling
`../thinkweave-<skill>/SKILL.md` projection; `$thinkweave-<skill>` is
Codex's user-facing invocation spelling.
