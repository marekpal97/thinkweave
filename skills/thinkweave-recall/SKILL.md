---
name: thinkweave-recall
description: Retrieve durable ThinkWeave memory for prior work, decisions, project history, known gotchas, or cross-session context. Use when the user asks what happened before, why something was chosen, what is known about a topic, or to ground substantial work in existing memory. Keep this workflow read-only unless the user separately asks to capture or update memory.
---

# ThinkWeave Recall

Use the `weave_*` MCP tools. If they are unavailable, tell the user to restart Codex and verify `codex mcp get thinkweave`; do not replace durable-memory retrieval with a broad filesystem crawl.

## Retrieve

1. Establish scope from the request. Use the current repository name as `project` only when the request is clearly project-specific; omit it for cross-project recall.
2. For a broad project briefing, call `weave_project_snapshot` first.
3. Call `weave_search`:
   - use `mode="fts"` for known words, identifiers, or filenames;
   - use `mode="hybrid"` for conceptual or uncertain wording;
   - filter by project, type, dates, tags, or concepts when that materially narrows the question.
4. Call `weave_read` for the few hits whose full reasoning or evidence matters.
5. Use `weave_context` for a compact multi-note briefing, or `weave_graph` when relationships, provenance, supersession, or dependencies are central.
6. If the first query misses, try one deliberate synonym or identifier variant. Do not spray many near-identical searches.

## Answer

- Lead with the recovered conclusion, then the supporting history.
- Cite relevant ThinkWeave IDs such as `dec-*`, `ses-*`, `n-*`, or `src-*` inline.
- Distinguish recorded memory from your inference.
- Surface conflicting or superseded decisions instead of silently selecting one.
- Do not create or modify vault entries during recall.