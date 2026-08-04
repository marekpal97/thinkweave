---
name: thinkweave-ingest
description: "Universal input router — classifies input shape (URL / file / text / structured-id) and dispatches to the appropriate ingestion skill. The single user-facing front door for getting external content into the vault."
---

# Codex projection for `/ingest`

Read the [canonical ThinkWeave command contract](../../commands/ingest.md) completely, then execute it. The linked file is the
semantic source of truth; this file only adapts harness vocabulary.

Use Codex-native equivalents for capabilities named in the canonical
contract: filesystem read/search for `Read`/`Grep`, the shell runner for
`Bash`, `apply_patch` for `Write`/`Edit`, the web tool for
`WebFetch`/`WebSearch`, and the user-input tool for `AskUserQuestion`.
When it names another `/skill`, read and follow the sibling
`../thinkweave-<skill>/SKILL.md` projection; `$thinkweave-<skill>` is
Codex's user-facing invocation spelling.
