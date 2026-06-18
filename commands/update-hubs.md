---
name: update-hubs
owns_mechanic: concept_hubs
consumes: [weave_search, weave_read, weave_graph]
produces: [vault/concepts/topics/*.md]
tools:
  - Read
  - Edit
  - Bash
  - weave_search
  - weave_read
  - weave_graph
description: Concept-hub sync — incremental (default) for daily deltas, or `--bulk [inline|batch]` for backfill on fresh / long-untended vaults.
---

# /update-hubs — Concept Hub Sync

Owns the synthesis-side update of `vault/concepts/topics/*.md`. Two modes:

- **Default (no flag) — incremental.** Walks unprocessed vault notes and
  appends learning artifacts to their concept hubs. Designed for small daily
  deltas (0–20 new notes × a few concepts each).
- **`--bulk` — backfill.** Walks the full hub plan with a per-invocation cap.
  Two sub-modes:
  - `--bulk` or `--bulk inline` — runs `weave drain --target hubs --via inline`
    (Claude Code session, interactive review, current LLM does the extraction).
  - `--bulk batch` — runs `weave drain --target hubs --via batch` (OpenAI
    Batches API + gpt-5-mini, 50% discount, async, no interactive review).

**Cap policy.** Bulk mode is the right tool when the plan has 100+ unprocessed
`(concept, note)` pairs. For small daily deltas (1–20 pairs) stay on the
default incremental mode — bulk is overkill and the Batches API turnaround
isn't worth it for a handful of items.

## What this is

Each concept in the ontology has a hub page at `vault/concepts/topics/{concept}.md` with two sections:

- **Essence** — ≤500w working mental model, slow-moving
- **Catalyst log** — append-only list of learning artifacts extracted from vault notes, each citing its source via `[[note-id]]` (was `## Learning log` pre-rename; `migrate_hub_log_heading` rewrites on `weave index --full`)

The hub page *is* the processed ledger: notes already cited in the log are done, notes tagged with the concept but not yet cited are unprocessed. No frontmatter markers on source notes.

This skill processes the unprocessed. Cross-type (sources, sessions, decisions, notes) and cross-project — any note with concepts feeds the synthesis layer.

---

## Default mode (incremental)

### 1. Survey scope

Run `weave hubs status` to see per-concept processed state. Look at the `todo` column.

If `todo` is small (roughly 1–20 notes across a handful of concepts, a normal daily delta), continue here.

If `todo` is large (>50 notes total), this is a backfill-scale job — stop and
suggest `/update-hubs --bulk` (with `inline` or `batch` sub-mode). Don't try
to process a backfill in incremental mode; the per-invocation cap and the
"watch every entry" posture both stop making sense.

### 2. Pick concepts to process

Default: process all concepts with any `todo`. If the user scopes to a specific concept (`/update-hubs agentic-harness`), only process that one.

For each concept, run `weave hubs plan --concept <concept>` to get the list of unprocessed notes. This writes a plan file to `.weave/hubs_plan.json` — read it.

### 3. Process notes inline

For each `(concept, note)` pair in the plan, do the extraction inline via your own LLM capacity (not via the OpenAI SDK — that's only for the bulk batch path):

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

For each hub page that gained entries, use `Edit` to insert the new log entries just before the next `## ` heading after `## Catalyst log` (or the legacy `## Learning log` on unmigrated hubs), or at the end of the log section if there's no following heading. Preserve:

- Frontmatter (all of it)
- `# {concept}` title line
- Domain link line (if present)
- `## Essence` section body (never rewrite during daily sync — flag to user if you think it needs rewriting, and let them run `/tighten` to handle it)
- Existing log entries

If the hub page has `*No entries yet.*` as its log content, replace that line with the first entry you're adding.

### 5. Mark essences flagged for revision (if any)

If any concept's essence should be revised, don't rewrite it here. Note the flagged concepts in your final report and suggest `/tighten` to handle the revisions.

### 6. Reindex

Run `weave index` (incremental — don't pass `--full`). Only the touched hub pages will be re-indexed; SHA-256 hash dedup skips the rest.

### 7. Report

One short paragraph:

```
Processed N notes across M concepts.
Appended X learning-log entries.
Essence revision flagged for: [list concepts, or "none"].
Run `/tighten` to handle essence revisions.
```

That's it. No lists of individual entries, no diffs — the user can read the hub pages in Obsidian.

---

## `--bulk` mode (backfill)

Bulk concept-hub backfill. Walks `.weave/hubs_plan.json` and processes every
unprocessed `(concept, note)` pair, appending learning artifacts. Use when
the plan has 100+ pairs (fresh vault, long-untended vault, or a re-onboarded
project) — incremental mode is the wrong shape for that.

### Sub-mode dispatch

- **`--bulk` or `--bulk inline`** — interactive bulk path. The CLI prints a
  hint then this skill body takes over and processes pairs in-session
  with full Claude oversight.
  ```
  Bash("weave drain --target hubs --via inline")
  ```
  Then proceed with the per-pair flow below.
- **`--bulk batch`** — non-interactive bulk path. The CLI runs entirely in
  Python: it reads the plan, submits all `(concept, note)` pairs to the
  OpenAI Batches API with gpt-5-mini, polls for completion, and applies the
  appended log entries. No Claude Code work to do beyond launching it.
  ```
  Bash("weave drain --target hubs --via batch")
  ```
  Report the CLI's stdout verbatim and stop.

### B1. Load or build the plan (inline sub-mode only)

If `.weave/hubs_plan.json` already exists, `Read` it. Otherwise run:
```
weave hubs plan [--concept X] [--project Y] [--limit-notes N] [--limit-concepts M]
```

The plan is a JSON object:
```
{
  "total_concepts": N,
  "total_notes": M,
  "est_input_tokens": …,
  "concepts": [
    {"concept": "…", "domains": […], "unprocessed_notes": [{"id": "n-…", "path": "…", "title": "…", "type": "…", "project": "…", "date": "…"}, …]},
    …
  ]
}
```

Report the plan size before starting. If it exceeds ~200 pairs, suggest the
user switch to `--bulk batch` for cost efficiency.

### B2. Cap and process (inline sub-mode only)

Process at most **100 (concept, note) pairs per invocation**. Honour
`--cap N` if the user passed one.

For each pair:

1. `Read vault/concepts/topics/{concept}.md` — note current essence and
   recent log entries.
2. `Read <note_path>` — the originating note from the plan entry.
3. Extract 0–3 learning artifacts. Pick a flag for each:
   - `new` — adds something not represented in the existing log
   - `agrees` — supports an existing entry (cite the entry's date in `ref`)
   - `contradicts` — conflicts with an existing entry (cite date in `ref`)
   - `extends` — elaborates on an existing entry (cite date in `ref`)
4. Append entries to the hub's `## Catalyst log` (or `## Learning log` —
   whichever the hub uses) just before the next `## ` heading. Format:
   ```
   - YYYY-MM-DD · *flag* — artifact text — [[note-id]]
   ```
   Date = the source note's date (not today, unlike incremental mode — bulk
   is reconstructing history, so honour the source's timestamp). Text ≤200
   chars, distilled.
5. Track concepts that need essence revision in a running list (rare;
   most additions go to the log, not the essence).

### B3. Reindex and report

```
weave index
```

Report:
```
Processed N / M pairs (cap C).
Appended X learning-log entries across Y concepts.
Essence revision flagged for: [concepts, or "none"].
Pairs remaining: Z. Run /update-hubs --bulk again to continue.
```

---

## Scope guardrails (both modes)

- **Never rewrite the essence** in this skill. That's `/tighten` territory.
- **Never delete log entries** — the log is append-only by design. If an entry is wrong, the user can hand-edit.
- **Never mutate source-note frontmatter** to mark it "processed." The hub page is the ledger.
- **Don't spawn Explore agents** to crawl the vault for related context — the plan file already has the list of notes to process, and the hub page already has its current state. Everything you need is in the per-pair reads.
- **Stop at the cap** in `--bulk inline`. Hand back to the user; the next invocation picks up where you left off.
