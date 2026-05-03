---
name: drain
owns_mechanic: queue_drain
capabilities: [acquire]
consumes: [mem_queue, mem_sources_config, mem_search, mem_concepts, mem_create, mem_read, mem_update, mem_link]
produces: [vault/sources/**, vault/concepts/topics/*.md]
tools:
  - Read
  - Write
  - Edit
  - Bash
  - WebFetch
  - mem_search
  - mem_concepts
  - mem_create
  - mem_read
  - mem_update
  - mem_link
  - mem_queue
  - mem_sources_config
description: Drain pending acquisition work — concept-hub backfill, per-source-type queues, or one-shot retroactive importers. Inline default; opt into `--via batch` for large jobs.
---

# /drain — Drain Acquisition Work

`/drain` is the unified entry point for catching up on pending intake:

- `--target hubs` — backfill concept-hub learning logs from unprocessed notes
- `--source-type <slug>` — drain a per-type acquisition queue (paper, repo, article, …)
- `--source claude-history` — one-shot retroactive importer (always inline)

**Default route is inline** (Claude Code session, one item at a time).
`--via batch` opts into the OpenAI/Anthropic Batches API where supported
(currently `--target hubs --via batch` only — wired into `mem hubs run`'s
existing plumbing).

For small daily concept-hub deltas (1–20 notes), prefer `/update-hubs`.

---

## Mode A: `--target hubs` (concept hub backfill)

Bulk concept-hub backfill. Walks `.mem/hubs_plan.json` and processes every
unprocessed `(concept, note)` pair, appending learning artifacts.

### A1. Load or build the plan

If `.mem/hubs_plan.json` already exists, `Read` it. Otherwise run:
```
mem hubs plan [--concept X] [--project Y] [--limit-notes N] [--limit-concepts M]
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

Report the plan size before starting. If it exceeds ~200 pairs, suggest
`mem drain --target hubs --via batch` instead.

### A2. Cap and process

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
   Date = the source note's date (not today). Text ≤200 chars, distilled.
5. Track concepts that need essence revision in a running list (rare;
   most additions go to the log, not the essence).

### A3. Reindex and report

```
mem index
```

Report:
```
Processed N / M pairs (cap C).
Appended X learning-log entries across Y concepts.
Essence revision flagged for: [concepts, or "none"].
Pairs remaining: Z. Run /drain --target hubs again to continue.
```

### A4. Scope guardrails

- Never rewrite essence here — that's `/mem-resolve-concepts`.
- Never delete log entries — append-only.
- Never mutate source-note frontmatter to mark "processed" — the hub
  page IS the ledger.
- Never spawn Explore agents — the plan + hub already have everything.
- Stop at the cap. Hand back to the user.

For the `--via batch` route, exit early: `mem drain --target hubs --via=batch`
runs entirely in the CLI (OpenAI Batches API + gpt-5-mini). No Claude Code
work to do.

---

## Mode B: `--source-type <slug>` (queue drain)

Drain `vault/.mem/queues/<source_type>.jsonl`. Each entry is a dict with
at least `id`, `url`, optional `title`, `concepts`. Process FIFO,
one item per outer loop pass.

### B1. Load config + queue

```
mem_sources_config()
```
Use the returned dict to find `sources.<slug>.research_skill` and
`sources.<slug>.dedup_keys`. Fall back to `research-<slug>` skill name.

```
mem_queue(action="peek", source_type="<slug>", n=<batch>)
```
Defaults to 5 items; honour `--limit N`.

### B2. Per-item flow

For each item:

1. **Claim** — pre-emptive at the queue level isn't needed for single-user
   flow. If running multiple drainers, mark claimed via the queue
   primitive (call into `mem queue` CLI / future `mem_queue claim`).
2. **Dispatch** to the per-type research skill: `Skill(skill="research-<slug>", args="<url>")`.
   That skill handles fetch + summarize + concept mapping + `mem_create`.
3. **On success** — archive the queue item with status `done`:
   ```
   mem_queue(action="archive", source_type="<slug>", item_id="<item-id>", status="done")
   ```
4. **On failure** — leave the item in place; archive with status `failed`
   only if the failure is non-recoverable:
   ```
   mem_queue(action="archive", source_type="<slug>", item_id="<item-id>", status="failed")
   ```

### B3. Report

```
Drained N / M items from queue '<slug>'.
Created: <list of src-IDs>
Failed: <count>
Remaining: <queue size>
```

---

## Mode C: `--source claude-history` (retroactive import)

One-shot, always inline. The CLI does the heavy lifting; this skill exists
so users can invoke it from Claude Code.

```
Bash("uv run mem drain --source claude-history")
```

Report the imported counts (sessions / notes / decisions) verbatim.

This is intended to run once when adopting personal_mem — Phase 5 G will
fold this into `/onboard`.

---

## When to use which route

| Path | Best for | Cost |
|---|---|---|
| `/drain --target hubs` (inline) | 20–200 hub pairs, want oversight | Claude Code session |
| `mem drain --target hubs --via batch` | 200+ pairs, no review | OpenAI Batches (50% off) |
| `/update-hubs` | 1–20 daily delta pairs | Claude Code session |
| `/drain --source-type paper` | Drain papers queue | Claude Code session |
| `/drain --source claude-history` | One-shot bootstrap | Claude Code session |
