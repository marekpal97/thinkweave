---
name: update-hubs
tools:
  - Read
  - Edit
  - Bash
  - mem_search
  - mem_read
  - mem_graph
description: Daily incremental sync for concept hub pages. Appends learning artifacts from unprocessed notes to their concept hubs. For bulk backfill use `mem hubs run`.
---

# /update-hubs — Daily Concept Hub Sync

Daily incremental sync for concept hub pages. Walks unprocessed vault notes and appends learning artifacts to their concept hubs. Designed for small daily deltas (0–10 new notes × a few concepts each). For bulk backfill on a fresh vault, use `mem hubs run` instead (OpenAI SDK + Batches API with gpt-5-mini, see CLAUDE.md).

## What this is

Each concept in the ontology has a hub page at `vault/concepts/topics/{concept}.md` with two sections:

- **Essence** — ≤500w working mental model, slow-moving
- **Learning log** — append-only list of learning artifacts extracted from vault notes, each citing its source via `[[note-id]]`

The hub page *is* the processed ledger: notes already cited in the log are done, notes tagged with the concept but not yet cited are unprocessed. No frontmatter markers on source notes.

This skill processes the unprocessed. Cross-type (sources, sessions, decisions, notes) and cross-project — any note with concepts feeds the synthesis layer.

## Steps

### 1. Survey scope

Run `mem hubs status` to see per-concept processed state. Look at the `todo` column.

If `todo` is small (roughly 1–20 notes across a handful of concepts, a normal daily delta), continue with this skill.

If `todo` is large (>50 notes total), this is a backfill-scale job. Tell the user they have two alternatives and let them pick:

- **`/drain --target hubs --via inline`** — same inline extraction as this skill, but iterates a full plan file with a per-invocation cap. Good when you want to watch it happen and intervene on hard calls.
- **`mem drain --target hubs --via batch`** — submits the plan to the OpenAI Batches API with gpt-5-mini (50% discount, async, automatic prompt caching for the shared hub state). Good when cost efficiency matters more than interactive review.

Don't force the choice — offer both and stop here unless the user confirms continuing with the incremental path.

### 2. Pick concepts to process

Default: process all concepts with any `todo`. If the user scopes to a specific concept (`/update-hubs agentic-harness`), only process that one.

For each concept, run `mem hubs plan --concept <concept>` to get the list of unprocessed notes. This writes a plan file to `.mem/hubs_plan.json` — read it.

### 3. Process notes inline

For each `(concept, note)` pair in the plan, do the extraction inline via your own LLM capacity (not via the OpenAI SDK — that's only for the bulk backfill path):

1. Read the concept hub page: `Read vault/concepts/topics/{concept}.md`. Note the current essence and the last ~10 learning-log entries.
2. Read the originating note: `Read <note_path>` where `note_path` comes from the plan.
3. Extract 0–3 learning artifacts. For each, decide its flag:
   - `new` — adds something not represented in the existing log
   - `agrees` — supports an existing entry (cite the entry's date in `ref`)
   - `contradicts` — conflicts with an existing entry (cite date in `ref`)
   - `extends` — elaborates on an existing entry (cite date in `ref`)
4. Note whether this source would require an essence revision (usually: no — incremental additions go in the log, essence revisions are rare).
5. Append the entries to the hub page using the exact entry format:
   ```
   - YYYY-MM-DD · *flag* — artifact text — [[note-id]]
   ```
   or with a ref:
   ```
   - YYYY-MM-DD · *contradicts 2026-01-15* — artifact text — [[note-id]]
   ```
   Date = today's date in the user's timezone (hub-update run date, not the source date). Flag must be one of `new`/`agrees`/`contradicts`/`extends`. Text must be ≤200 chars, distilled not summarized.

### Extraction rules

- **Short**: 1–3 sentences per entry, max ~200 chars. Terse artifact statement, not paraphrase.
- **Discrete**: one artifact per entry. A single note usually yields 0–3 entries — often just 1, sometimes 0 if the note doesn't actually teach anything new about this concept.
- **Honest flags**: `contradicts` only when there's an actual conflict with a prior entry — not when the new source covers different aspects. When in doubt, use `new`.
- **Not every note needs to contribute**: if a note is tagged with a concept but doesn't actually say anything worth adding to the synthesis, skip it (no entries for that `(concept, note)` pair). The note will be picked up again next run — harmless.
- **Cite the originating note**: `[[note-id]]` using the note's vault ID (e.g. `[[src-a94ed140]]`, `[[ses-2026-04-05]]`, `[[dec-abcd]]`).

### 4. Write back

For each hub page that gained entries, use `Edit` to insert the new log entries just before the next `## ` heading after `## Learning log`, or at the end of the log section if there's no following heading. Preserve:

- Frontmatter (all of it)
- `# {concept}` title line
- Domain link line (if present)
- `## Essence` section body (never rewrite during daily sync — flag to user if you think it needs rewriting, and let them run `/mem-resolve-concepts` to handle it)
- Existing `## Learning log` entries

If the hub page has `*No entries yet.*` as its log content, replace that line with the first entry you're adding.

### 5. Mark essences flagged for revision (if any)

If any concept's essence should be revised, don't rewrite it here. Note the flagged concepts in your final report and suggest `/mem-resolve-concepts` to handle the revisions.

### 6. Reindex

Run `mem index` (incremental — don't pass `--full`). Only the touched hub pages will be re-indexed; SHA-256 hash dedup skips the rest.

### 7. Report

One short paragraph:

```
Processed N notes across M concepts.
Appended X learning-log entries.
Essence revision flagged for: [list concepts, or "none"].
Run `/mem-resolve-concepts` to handle essence revisions.
```

That's it. No lists of individual entries, no diffs — the user can read the hub pages in Obsidian.

## Scope guardrails

- **Never rewrite the essence** in this skill. That's `/mem-resolve-concepts` territory.
- **Never delete log entries** — the log is append-only by design. If an entry is wrong, the user can hand-edit.
- **Never mutate source-note frontmatter** to mark it "processed." The hub page is the ledger.
- **Don't spawn Explore agents** to crawl the vault for related context — the plan file already has the list of notes to process, and the hub page already has its current state. Everything you need is in step 1–3 reads.
- **If the plan shows more than ~20 unprocessed notes total**, stop and let the user pick between `/drain --target hubs --via inline` (interactive bulk) and `mem drain --target hubs --via batch` (Batches API bulk). Both are bulk paths; don't force either.
