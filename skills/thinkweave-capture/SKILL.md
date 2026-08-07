---
name: thinkweave-capture
description: Save a user-supplied insight, quote, snippet, brief, decision, or reusable lesson into ThinkWeave. Use when the user says to remember, capture, preserve, record, or add something to durable memory. Do not trigger for ordinary conversation where persistence was not requested.
---

# ThinkWeave Capture

Use the `weave_*` MCP tools. Preserve the user's meaning and avoid inflating a small observation into a large note.

## Capture

1. Reject empty input. Classify it as a reusable `note`, constraining `decision`, or external `source`. Default to `note`.
2. Call `weave_search` with distinctive title or body terms before writing. If an equivalent note exists, report it; update only when the user asked to amend it.
3. Call `weave_concepts(action="list", min_count=2)`. Reuse at least two fitting concepts when possible. Put genuinely new vocabulary in `frontmatter.proposed_concepts` rather than pretending it is canonical.
4. Derive a concise title. Preserve quotes and snippets closely; lightly format prose without changing its claim.
5. Call `weave_create`:
   - `type="note"` for reusable knowledge;
   - `type="decision"` only for a choice that constrains later work, with Context, Decision, and Consequences in the body;
   - `type="source"` for attributed external material, with `source_type`, URL, and authors in frontmatter.
6. Put concepts and provenance edges in `frontmatter`. Set `project` only for project-bound material; external sources are global unless the user requests project scope.

Use sparse tags. Reserve `todo` for an explicit future action and `probe` for a substantive user question that produced a reusable lesson.

## Report

Return the created or reused ID, title, type, and concepts in one compact result. Do not claim persistence unless the MCP write succeeded.