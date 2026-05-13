---
name: mem-wrap
owns_mechanic: session_extraction
consumes: [mem_extract, mem_concepts, mem_project_snapshot, mem_wrap_finalize]
produces: [session.md, DECISIONS.md, BACKLOG.md]
tools:
  - Read
  - Bash
  - mem_project_snapshot
  - mem_extract
  - mem_concepts
description: End-of-session memory extraction. Compose insights/decisions inline, call `mem_extract` once, then `mem wrap-finalize` (deterministic tail). Self-contained; never prompts the user.
---

# /mem-wrap — Session-End Memory Extraction

End-of-session memory extraction for the personal_mem vault. **Self-contained and headless-safe**: never prompt the user. You decide what's worth recording (which insights, which decisions, which todos); if the user gave direction earlier in *this* session about what to capture, honor it — but do not ask.

**One inline pass.** Compose the session's insights and decisions yourself, call `mem_extract` once, then run `mem wrap-finalize` (one Bash call — prune → index → judge → landing → drift, zero model turns). For ≤5 notes the overhead of spawning a subagent exceeds the per-turn savings; do the writing inline. (An older revision of this skill spawned a Sonnet extraction subagent — that was reversed after measurement: 25 tool uses and ~8 min on a small wrap, dominated by spawn + over-verification.)

Two minor variants:
- **Live wrap** — running in-session before `/clear`. You have the conversation; that's the source.
- **Catch-up wrap** — headless (e.g. `claude -p "/mem-wrap"`) over a session that already ended. There is no live conversation; you work from `events.jsonl` + the session note's auto-extract skeleton + `git log/diff`.

The steps below cover both. Step 1 + 2 differ in source material; everything from step 3 onward is identical.

---

## 1. Find the session note (or note its absence)

```
mem search --type session --project <project> --limit 1
```

- **Session note exists** → read it. Frontmatter has `commits`, `files_touched`, sometimes `## Candidate Insights`. If `processed: true` and `auto_extracted: true` you're in catch-up mode by definition; pass `force=true` to `mem_extract` at step 3.
- **No session note** → fine. Mint a session id (e.g. `<slug>-<date>`) or use `CLAUDE_SESSION_ID`; `mem_extract` auto-creates the note.

Optionally add a `## Summary` section to the session note (2–3 sentences on what was accomplished) by editing the markdown directly. Skip this for tiny non-code conversations — `mem_extract` will set the summary from its `summary=` argument.

## 2. Gather your source material

**Live mode** — the full conversation in this turn. That's the *narrative*; `events.jsonl` is only the skeleton (raw tool events). The narrative is what makes insights non-textbook and decisions have real Context/Decision/Consequences.

**Catch-up mode** — read the session folder's `events.jsonl` (raw tool events: files edited, bash commands, commit hashes, test results), the session note's auto-extracted `## Summary` skeleton, its `commits` and `files_touched` frontmatter, and `git log`/`git diff` for the window if a commit range is obvious. Accept the quality floor of working from events + git alone — this is the headless reality.

## 3. Call `mem_extract` once

Apply the §C content rules below: load the concept vocabulary (`mem_concepts(min_count=2)`), then compose ≤3 insights + the decisions worth formalizing + the user's explicitly-stated future plans as `todo`-tagged insights. Then one call:

```
mem_extract(
  session_id   = <ses-id or minted id>,
  project      = <project>,                  # required if no session note exists
  summary      = "<2–3 sentence summary>",
  insights     = [ {title, body, tags, concepts}, ... ],   # max 3 total (todos count)
  decisions    = [ {title, rationale, outcome, file_paths, concepts, summary?, predicted_outcome?, supersedes?, cites?}, ... ],
  force        = <true if the session is already processed/auto-extracted>,
)
```

`mem_extract` is pure Python — zero API cost, one tool round-trip. It writes the notes/decisions to the session folder, indexes them, and auto-extracts any `todo` items from the body.

## 4. Run `mem wrap-finalize` (one Bash call)

```
mem wrap-finalize <session_id> --project <project>
```

Does in one process, zero model turns:
- prune orphan session folders (conservative GC; this session is protected)
- incremental reindex (picks up freshly written notes, drops pruned rows)
- `judge_and_writeback` on the new decisions (verdict + status from git evidence)
- regenerate DECISIONS.md + BACKLOG.md
- concept-drift advisory (read-only — proposes nothing, just reports)

Add `--json` for headless flows. The CLI exits non-zero if any step errored.

**Does NOT** touch STATE.md (see step 5) and does NOT run `/mem-resolve-concepts`. If drift surfaces a proposed concept at threshold the report mentions it; promotion is `/mem-resolve-concepts`'s job, run separately.

## 5. STATE.md — only if the big picture changed (live mode only)

If this session opened a new area, made a major architectural shift, or otherwise changed what someone needs to know first about the project:
```
mem landing --project <project> --doc state
```
Or use `mem_landing(project=..., doc="state", state_context=true)` to get raw data and write a narrative STATE.md yourself. Routine work in existing areas — skip. Catch-up mode — always skip (a headless pass doesn't have the context to judge a big-picture change).

## 6. Report

- session note path + summary
- notes created (IDs, including probes); decisions created (IDs)
- `mem wrap-finalize` summary (decisions judged + verdicts, landing docs, orphans pruned, drift advisory)
- whether STATE.md was updated
- `mem stats` line

---

## §C. Content rules

### C1. Load the concept vocabulary
`mem_concepts(min_count=2)` first. Reuse existing labels — don't invent a new concept when one fits. Keep the list in working memory.

### C2. Write insights — `mem_extract` `insights=[...]`
Max 3. Quality over quantity. Each insight captures **personal experience**, not textbook facts:
- what problem or surprise led to it; what was tried that didn't work, and why; the non-obvious implication or gotcha.

**BAD**: "SQLite WAL mode allows concurrent readers while one writer holds the lock."
**GOOD**: "WAL mode was the fix for index corruption when hooks and CLI ran simultaneously. The default rollback journal blocks concurrent readers, so the indexer failed silently when a hook was mid-write. Switching to WAL eliminated this — but WAL doesn't help with concurrent *writers*, only concurrent reads during a write."

**Probes**: if an insight was prompted by a substantive user question, tag it `probe` (plus domain tags), title = the question, body = what was learned (not a textbook restatement). One probe per question — don't also make a separate insight for the same thing.

**Future plans**: things the user explicitly wants tracked → insights tagged `todo`. Never add `todo` otherwise. Todos count toward the max-3 cap.

### C3. Write decisions — `mem_extract` `decisions=[...]`
Real Context / Decision / Consequences, not just the conclusion:
- **Context**: what problem forced this; alternatives considered and rejected.
- **Decision**: what was chosen and WHY (not just WHAT).
- **Consequences**: trade-offs accepted; what got harder, what got easier.

Per decision dict: `title`, `rationale` (the C/D/C prose), `outcome` (`committed`/`abandoned`/`partial`), `file_paths` (relevant paths), `concepts` (≥2), optional `summary` (one sentence — powers DECISIONS.md), optional `supersedes`/`cites`, and **optional `predicted_outcome`** — a concrete expected result, if you can state one; omit it rather than fabricate. (`predicted_outcome` is a forward-looking hook for decision-outcome judging; fine to leave off most decisions today.)

### C4. Concepts are mandatory
Every insight and every decision: a `concepts` array, **≥2**, from the vocabulary loaded in C1. Pick concepts that connect this note to *other* notes (thematic, not descriptive). Prefer specific domain terms (`fts5`, `write-ahead-log`) over generic ones (`architecture`, `testing`). Test: "would another note about this topic share this concept?" Terms not in the ontology are accepted automatically into `proposed_concepts:` by the server — you don't pre-canonicalise.
