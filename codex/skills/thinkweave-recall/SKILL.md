---
name: thinkweave-recall
description: Explicit, read-only ThinkWeave recall workflow. Use only when the user invokes `$thinkweave-recall` or explicitly asks to use the recall skill. For ordinary history or context questions, call the available `weave_*` tools directly without loading this skill.
---

# ThinkWeave Recall

This is an opt-in workflow, not a wrapper required for ordinary retrieval. The
MCP/CLI tool descriptions already provide the default routing contract.

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
