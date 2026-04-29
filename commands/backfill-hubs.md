---
name: backfill-hubs
tools:
  - Read
  - Edit
  - Bash
  - mem_search
  - mem_read
  - mem_graph
description: Bulk concept-hub backfill inline in Claude Code. Iterates a hub plan file and appends learning artifacts for every unprocessed (concept, note) pair. For cost-efficient Batches-API backfill, use `mem hubs run` instead.
---

# /backfill-hubs — Bulk Concept Hub Backfill (Claude Code path)

Bulk backfill for concept hub pages. Walks `.mem/hubs_plan.json` and processes every unprocessed `(concept, note)` pair inline via Claude Code's own LLM capacity. Use this when you want to **watch a backfill happen** in an interactive session and intervene in hard calls.

For cost-optimised bulk backfill with no interactive review, use `mem hubs run` instead — same plan file, submits to the OpenAI Batches API with gpt-5-mini (50% discount, async throughput, automatic prompt caching for the shared hub state). Both paths share `hubs.py` for parse/diff/write, so results are equivalent.

## When to use which

| Path | Cost | Throughput | Review |
|---|---|---|---|
| `/backfill-hubs` (this skill) | Claude Code session cost | Sequential, one pair at a time | You see every entry as it's appended |
| `mem hubs run --plan <path>` | Batches API (50% off Messages) | Parallel, async | No interactive review; results applied on completion |
| `/update-hubs` | Claude Code session cost | Sequential | Best for small daily deltas (1–20 notes) |

Pick `/backfill-hubs` when plan size is 20–200 pairs and you want to stay in the loop. Switch to `mem hubs run` when plan size exceeds ~200 pairs or you don't need interactive oversight.

## What this is

Each concept in the ontology has a hub page at `vault/concepts/topics/{concept}.md` with two sections:

- **Essence** — ≤500w working mental model, slow-moving
- **Learning log** — append-only list of learning artifacts extracted from vault notes, each citing its source via `[[note-id]]`

The hub page *is* the processed ledger: notes already cited in the log are done, notes tagged with the concept but not yet cited are unprocessed. No frontmatter markers on source notes, so re-running this skill is safe — already-processed pairs are filtered out of the plan automatically.

Cross-type (sources, sessions, decisions, notes) and cross-project — any note with concepts feeds the synthesis layer.

## Steps

### 1. Load or build the plan

If `.mem/hubs_plan.json` already exists, read it with `Read`. Otherwise (or if the user wants a fresh scope), run `mem hubs plan` first — optionally scoped with `--concept`, `--project`, `--limit-notes`, or `--limit-concepts`.

The plan is a JSON object:
```
{
  "total_concepts": N,
  "total_notes": M,
  "est_input_tokens": ...,
  "concepts": [
    {"concept": "...", "domains": [...], "unprocessed_notes": [{"id": "n-...", "path": "...", "title": "...", "type": "...", "project": "...", "date": "..."}, ...]},
    ...
  ]
}
```

Report the plan size to the user before starting so they can confirm scope. If the total exceeds ~200 pairs, suggest `mem hubs run` as the Batches alternative.

### 2. Set an invocation cap

To keep a single Claude Code session tractable, process at most **100 `(concept, note)` pairs per invocation**. If the plan exceeds this, process the first 100 in this run and tell the user to re-run `/backfill-hubs` to continue. The next run's `mem hubs plan` call picks up only the still-unprocessed pairs — the hub-page ledger is the state.

If the user asks for a smaller cap (e.g. `/backfill-hubs --cap 20`), respect it.

### 3. Process each (concept, note) pair

For each pair in the plan, up to the cap:

1. **Read the concept hub**: `Read vault/concepts/topics/{concept}.md`. Note the current essence and the last ~10 learning-log entries. If the hub page doesn't exist yet, the plan builder should have surfaced this — skip and tell the user to run `/mem-resolve-concepts` to generate missing skeletons first.
2. **Read the originating note**: `Read <note_path>` from the plan entry.
3. **Extract 0–3 learning artifacts**. For each, decide its flag:
   - `new` — adds something not represented in the existing log
   - `agrees` — supports an existing entry (cite the entry's date in `ref`)
   - `contradicts` — conflicts with an existing entry (cite date in `ref`)
   - `extends` — elaborates on an existing entry (cite date in `ref`)
4. **Note whether this source would require an essence revision** (usually no — incremental additions go in the log, essence revisions are rare). Track flagged concepts in a running list.
5. **Append the entries** to the hub page using the exact entry format:
   ```
   - YYYY-MM-DD · *flag* — artifact text — [[note-id]]
   ```
   or with a ref:
   ```
   - YYYY-MM-DD · *contradicts 2026-01-15* — artifact text — [[note-id]]
   ```
   Date = today's date. Flag must be one of `new`/`agrees`/`contradicts`/`extends`. Text must be ≤200 chars, distilled not summarized.

### Extraction rules

- **Short**: 1–3 sentences per entry, max ~200 chars. Terse artifact statement, not paraphrase.
- **Discrete**: one artifact per entry. A single note usually yields 0–3 entries — often just 1, sometimes 0.
- **Honest flags**: `contradicts` only when there's an actual conflict with a prior entry. When in doubt, use `new`.
- **Not every note needs to contribute**: if a note is tagged with a concept but doesn't actually say anything worth adding to the synthesis, skip it. The note stays out of the hub's `[[note-id]]` citation list and will be picked up again next time — harmless.
- **Cite the originating note**: `[[note-id]]` using the note's vault ID.

### 4. Write back

For each hub page that gained entries, use `Edit` to insert the new log entries just before the next `## ` heading after `## Learning log`, or at the end of the log section if there's no following heading. Preserve:

- Frontmatter (all of it)
- `# {concept}` title line
- Domain link line (if present)
- `## Essence` section body (never rewrite here — `/mem-resolve-concepts` handles essence)
- Existing `## Learning log` entries

If the hub page has `*No entries yet.*` as its log content, replace that line with the first entry you're adding.

### 5. Reindex at the end

After processing, run `mem index` (incremental). Only touched hub pages will be re-indexed; SHA-256 hash dedup skips the rest.

### 6. Report

```
Processed N / M pairs (cap: C).
Appended X learning-log entries across Y concepts.
Essence revision flagged for: [list concepts, or "none"].
Pairs remaining: Z. Run `/backfill-hubs` again to continue, or switch to `mem hubs run` for Batches cost efficiency.
```

## Scope guardrails

- **Never rewrite the essence** in this skill. That's `/mem-resolve-concepts` territory.
- **Never delete log entries** — the log is append-only by design.
- **Never mutate source-note frontmatter** to mark it "processed." The hub page is the ledger.
- **Don't spawn Explore agents** for extra context — the plan file already has the list of notes, and the hub page has current state. Everything you need is in reads from step 3.
- **Stop at the cap.** Don't try to process the whole plan in one session; hand back control to the user instead.
