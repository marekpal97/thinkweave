---
name: thinkweave-ingest-paper-file
description: "Local PDF paper ingestion — extract text, derive title/authors, detect arxiv ID, write a `source_type: paper` note with the PDF and extracted text staged as companions. Called from `/ingest` for the local-PDF file shape."
---

# Codex projection for `/ingest-paper-file`

Read the [canonical ThinkWeave command contract](../../commands/ingest-paper-file.md) completely, then execute it. The linked file is the
semantic source of truth; this file only adapts harness vocabulary.

Use Codex-native equivalents for capabilities named in the canonical
contract: filesystem read/search for `Read`/`Grep`, the shell runner for
`Bash`, `apply_patch` for `Write`/`Edit`, the web tool for
`WebFetch`/`WebSearch`, and the user-input tool for `AskUserQuestion`.
When it names another `/skill`, read and follow the sibling
`../thinkweave-<skill>/SKILL.md` projection; `$thinkweave-<skill>` is
Codex's user-facing invocation spelling.
