---
name: thinkweave-research-repo
description: "Fetch a GitHub / GitLab repo, extract architectural summary + key files, write it as a `source_type: repo` note. Called from `/research` (router) or `/drain --source-type repo`."
---

# Codex projection for `/research-repo`

Read the [canonical ThinkWeave command contract](../../commands/research/research-repo.md) completely, then execute it. The linked file is the
semantic source of truth; this file only adapts harness vocabulary.

Use Codex-native equivalents for capabilities named in the canonical
contract: filesystem read/search for `Read`/`Grep`, the shell runner for
`Bash`, `apply_patch` for `Write`/`Edit`, the web tool for
`WebFetch`/`WebSearch`, and the user-input tool for `AskUserQuestion`.
When it names another `/skill`, read and follow the sibling
`../thinkweave-<skill>/SKILL.md` projection; `$thinkweave-<skill>` is
Codex's user-facing invocation spelling.
