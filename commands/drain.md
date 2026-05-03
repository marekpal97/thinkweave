---
name: drain
owns_mechanic: queue_drain
capabilities: [acquire]
consumes: [mem_queue, mem_sources_config, mem_search, mem_concepts, mem_create, mem_read, mem_update, mem_link]
produces: [vault/sources/**]
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
description: Drain a per-source-type acquisition queue. One slug per invocation; per-item dispatch to the matching `research-<slug>` skill.
---

# /drain — Per-source-type queue drainer

`/drain --source-type <slug>` is the single-purpose queue worker. It walks
`vault/.mem/queues/<source_type>.jsonl` FIFO and dispatches each item to
the per-type research skill.

**Scope.** This skill *only* drains acquisition queues. Two former modes
have moved out:

- Concept-hub backfill (synthesis, vault → vault, no queue) → use
  **`/update-hubs --bulk`** (`inline` or `batch` sub-mode).
- One-shot retroactive Claude session import (migration, runs once per
  vault) → use **`/onboard`** (which wraps the underlying CLI). The CLI
  `mem drain --source claude-history` still exists for ad-hoc reruns.

---

## Per-item flow

Drain `vault/.mem/queues/<source_type>.jsonl`. Each entry is a dict with
at least `id`, `url`, optional `title`, `concepts`. Process FIFO,
one item per outer loop pass.

### 1. Load config + queue

```
mem_sources_config()
```
Use the returned dict to find `sources.<slug>.research_skill` and
`sources.<slug>.dedup_keys`. Fall back to `research-<slug>` skill name.

```
mem_queue(action="peek", source_type="<slug>", n=<batch>)
```
Defaults to 5 items; honour `--limit N`.

### 2. Per-item dispatch

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

### 3. Report

```
Drained N / M items from queue '<slug>'.
Created: <list of src-IDs>
Failed: <count>
Remaining: <queue size>
```

---

## When to use which route

| Path | Best for | Cost |
|---|---|---|
| `/drain --source-type paper` | Drain papers queue | Claude Code session |
| `/drain --source-type repo` | Drain repos queue | Claude Code session |
| `/drain --source-type article` | Drain articles queue | Claude Code session |
| `/update-hubs` (default) | 1–20 daily delta hub pairs | Claude Code session |
| `/update-hubs --bulk inline` | 100+ hub pairs, want oversight | Claude Code session |
| `/update-hubs --bulk batch` | 100+ hub pairs, no review | OpenAI Batches (50% off) |
| `/onboard` | First-time bootstrap incl. retroactive Claude session import | Claude Code session |
