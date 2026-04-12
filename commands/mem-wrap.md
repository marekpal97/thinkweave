# /mem-wrap — Session-End Memory Extraction

You are performing end-of-session memory extraction for the personal_mem vault. This runs inside the existing conversation at zero extra API cost.

**Note on context**: The SessionStart hook already fed you ~7–10k tokens of structured project context at the start of this session (recent wrapped sessions, STATE.md, BACKLOG, decisions, concept histogram, MCP tool manifest). You don't need to re-fetch that context here — extraction builds *new* knowledge from the current conversation, not a re-render of existing state. If you need to look something up mid-wrap, use `mem_project_snapshot(project=<name>)` for an on-demand refresh.

## Steps

### 1. Find or Identify the Current Session
Look for the most recent session note in the vault for this project. Use the MCP tool `mem_search` or run:
```
mem search --type session --project <project> --limit 1
```

**If a session note exists**: Read it to see accumulated events and candidate insights.

**If the session was auto-extracted** (`auto_extracted: true` in frontmatter): The Stop hook already created a skeleton summary. You can enrich it — read the `events.jsonl` file in the session folder for context, then proceed to step 3 with `force=true`.

**If no session note exists** (non-code conversation): That's fine — `mem_extract` will auto-create one. Skip to step 3 using the current `CLAUDE_SESSION_ID` as the session_id.

### 2. Finalize the Session Note (if it exists)
Add a `## Summary` section with 2-3 sentences describing what was accomplished in this session. Update the session note using the vault file directly.

### 2.5. Build concept vocabulary

Before writing any insights or decisions, call `mem_concepts(min_count=2)` to load the existing concept vocabulary. You MUST reuse existing labels — do not invent new concepts when an existing one fits. Keep this list in working memory for steps 3-5.

### 3. Extract via mem_extract
Call `mem_extract` with:
- `session_id`: the session note ID (if found) or `CLAUDE_SESSION_ID` (if no session note)
- `summary`: 2-3 sentence summary of the session
- `insights`: key knowledge worth preserving (max 3, quality over quantity)
- `decisions`: architectural/design decisions (both committed and abandoned)
- `project`: the project name (required if no session note exists)
- `force`: set to `true` if re-extracting an auto-extracted session

For non-code conversations (discussions, brainstorming, design reviews), focus insights on ideas and conclusions that emerged from the discussion. `mem_extract` auto-creates a session note when one doesn't exist.

**CRITICAL — Concept assignment is mandatory on every insight and every decision.**
Every insight and decision MUST include a `concepts` array with **minimum 2 concepts**. Notes with <2 concepts cannot auto-link in the knowledge graph and will cluster as isolated islands in Obsidian. This is non-negotiable.

When assigning concepts:
- Draw from the vocabulary loaded in step 2.5 — reuse existing labels
- Pick concepts that connect this note to OTHER notes (thematic, not descriptive)
- Prefer specific domain terms (`fts5`, `write-ahead-log`) over generic ones (`architecture`, `testing`)
- Use domain-qualified paths when they exist (`ml/deep-learning` not `deep-learning`)
- A good concept test: "would another note about this topic share this concept?"

### Writing Good Insights

Each insight should capture **personal experience**, not textbook facts. Include:
- What problem or surprise led to this discovery?
- What did you try that didn't work, and why?
- What's the non-obvious implication or gotcha?

**BAD**: "SQLite WAL mode allows concurrent readers while one writer holds the lock."
**GOOD**: "WAL mode was the fix for index corruption when hooks and CLI ran simultaneously. The default rollback journal blocks concurrent readers, so the indexer failed silently when a hook was mid-write. Switching to WAL eliminated this entirely — but note that WAL doesn't help with concurrent writers, only concurrent reads during a write."

### Writing Good Decisions

Decisions need real Context/Decision/Consequences — not just the conclusion:
- **Context**: What problem forced this decision? What alternatives did you consider and reject?
- **Decision**: What did you choose and WHY (not just WHAT)?
- **Consequences**: What trade-offs did you accept? What became harder? What became easier?

**BAD**: "Use FTS5 for search index. Better performance, prefix queries, column filters."
**GOOD**: Context explains that search needed to work across 4 note types with different vocabularies, that alternatives included external search (too heavy), FTS4 (no prefix queries needed for autocomplete), and raw LIKE queries (too slow at scale). FTS5 won because prefix queries enable autocomplete in the CLI and column filters let us scope by type without post-filtering.

### 4. Extract Probes (Learning Artifacts)
Review the conversation for **substantive questions the user asked** — "how does X work?", "why was Y done this way?", "what happens if Z?". These are signals of active learning.

For each substantive question (skip clarifications like "which file?" or "can you repeat that?"):
- Include it as an insight in the `mem_extract` call
- Tag it with `probe` (plus any relevant domain tags)
- Title = the question itself
- Body = the key insight or answer discovered — what the user learned, not a textbook restatement

**Probes vs regular insights**: If something would be a good insight AND was prompted by a user question, make it a probe (use the `probe` tag). Don't create both a probe and a separate insight for the same thing.

### 5. Prompt for Decisions and Future Plans
Review the session for any architectural or design decisions that were made, and any future plans or ideas that were discussed but not acted on. Ask the user:

"Were any decisions made in this session worth formalizing as decision records? Were any future plans or ideas discussed worth tracking?"

If yes, include decisions in the `mem_extract` call as usual. When creating decisions, include a one-sentence `summary` field for each — this powers the DECISIONS.md landing page. Future plans become insights tagged `todo` — they land in the session folder and surface via `mem backlog`. Never auto-add `todo`; only include plans the user explicitly confirms.

### 6. Re-index
```
mem index
```

### 6.5. Judge Extracted Decisions
If any decisions were extracted in step 3, evaluate them against git reality:
```
mem_judge(session_id=<session_id>)
```

This reconciles each decision with git evidence — catching commits made during or after the session. The judge writes `commit_refs` (list of git hashes) and `verdict` (kept/superseded/reverted/unknown) onto each decision. Even if commits happen after the session, re-running `mem_judge` later will discover and link them.

If no decisions were extracted, skip this step.

### 7. Refresh Landing Documents
After extraction, refresh the project's landing documents:
```
mem_landing(project=<project>, doc="decisions")
mem_landing(project=<project>, doc="backlog")
```

DECISIONS.md and BACKLOG.md are cheap to regenerate — always refresh them.

**STATE.md**: Only update if this session genuinely changed the project's big picture — new major decisions, architectural shifts, new areas opened up. Routine work in existing areas doesn't warrant an update. If updating, use `mem_landing(project=<project>, doc="state", state_context=true)` to get raw data, then write a narrative STATE.md that tells the human what matters most.

### 7.5. Ontology drift check (advisory)
Call `mem_concepts_drift(project=<project>)` (or shell out to
`mem concepts drift --project <project>`). This is **read-only and advisory**
— it surfaces three kinds of drift:

1. **Near-duplicate concepts** (e.g. `neural-network` ≈ `neural-networks`) —
   suggests a merge command.
2. **New concept candidates** — concepts that crossed count ≥ 5 but are
   NOT listed in `ontology.yaml`. Worth considering as new domain members.
3. **Ontology staleness** — `ontology.yaml` was edited after the last
   `mem concepts hubs` run. Hub pages may be stale.

**Do NOT auto-merge or auto-regenerate.** Surface the findings in the final
report. If the user wants to act, they'll ask. This step should take under a
second — it's a small read-only query.

### 8. Prune orphan session folders
Run `mem prune-orphans --project <project> --yes`. This deletes stub session
folders that accumulated no derived content — no notes/decisions, tiny
`events.jsonl` (< 500 bytes), no `files_touched`, no `commits`, older than
1 hour, and NOT the session currently being wrapped. The 7-condition orphan
definition is intentionally conservative; cleanup is safe to auto-run.

After a real delete (not dry-run), the index is cleaned in-place so searches
and landing docs stop surfacing the deleted sessions.

Include the pruned count in the step 9 report.

### 9. Report
Print a summary of what was extracted:
- Session note path and summary
- Notes created (with IDs), including probes
- Decisions created (if any)
- Landing documents refreshed
- Orphan folders pruned (count + freed bytes)
- Total vault stats via `mem stats`
