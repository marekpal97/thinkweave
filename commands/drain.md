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
  - Task
  - WebFetch
  - mem_search
  - mem_concepts
  - mem_create
  - mem_read
  - mem_update
  - mem_link
  - mem_queue
  - mem_sources_config
description: Drain a per-source-type acquisition queue. Source-type-agnostic — config in `vault/.mem/sources.yaml` decides whether items dispatch to a per-type research skill (sequential) or fan out to subagents (parallel).
---

# /drain — Per-source-type queue drainer

`/drain --source-type <slug>` walks `vault/.mem/queues/<source_type>.jsonl` FIFO and processes each item. The dispatch shape (sequential `Skill` call vs. parallel `Task` subagents) is driven by `vault/.mem/sources.yaml`, not by hard-coded source-type branches.

**Scope.** This skill *only* drains acquisition queues. Two former modes have moved out:

- Concept-hub backfill (synthesis, vault → vault, no queue) → use **`/update-hubs --bulk`**.
- One-shot retroactive Claude session import → use **`/onboard`** (or CLI `mem drain --source claude-history`).

---

## 1. Load config + queue

```
mem_sources_config()
```

The returned dict contains `sources.<slug>.*` keys you'll consult:

| Key | Meaning | Default |
|---|---|---|
| `research_skill` | Per-type skill for sequential dispatch | `research-<slug>` |
| `subagent_type` | If set, fan out to `Task` subagents instead of `Skill` | `null` |
| `subagent_model` | `sonnet` / `opus` / null (inherit) | `null` |
| `drain_parallelism` | Max concurrent subagents per batch | `1` |
| `drain_batch_max` | Cap on items per drain run | `5` |
| `post_batch_hooks` | Hooks to run after fan-in (`dedup_sweep`, `theme_scan`) | `[]` |
| `dedup_window_hours` | Used by `dedup_sweep` | `24` |
| `dedup_jaccard_threshold` | Used by `dedup_sweep` | `0.8` |

```
mem_queue(action="peek", source_type="<slug>", n=<drain_batch_max>)
```

If empty → report "Nothing to drain." and stop. If `--limit N` was passed, use the smaller of N and `drain_batch_max`.

---

## 2. Dispatch — pick the path

### Path A: Sequential Skill dispatch (`subagent_type` not set)

This is the original behavior. Process items one at a time:

For each item:
1. `Skill(skill="<research_skill>", args="<url>")` — that skill handles fetch + summarize + concept mapping + `mem_create`.
2. On success: `mem_queue(action="archive", source_type="<slug>", item_id="<item-id>", status="done")`.
3. On non-recoverable failure: `mem_queue(action="archive", ..., status="failed")`. On recoverable failure: leave in place.

### Path B: Writer fan-out (`subagent_type` is set), optionally preceded by triage

The drain orchestrator spawns Sonnet writer subagents in parallel. Admission is **either** decided upstream (channel allowlist / sender allowlist — no triage stage), **or** decided per-drain by a cheap Haiku triage on titles (news's "fits an active theme?" filter). The presence of `triage_model` in the source's config selects which.

| `triage_model` | Stage 1 | Stage 2 |
|---|---|---|
| set (e.g. `claude-haiku-4-5`) | Haiku triage per item | Writers fan out only for `keep`/`keep_unfiled` items |
| **unset** (default) | **skipped** — every item treated as `keep_unfiled` | Writers fan out for every queue item |

**Stage 1 — Haiku triage (single batched call, prompt-cached). Only runs when `triage_model` is set.**

Take up to `drain_batch_max` items off the queue (don't archive yet). Build a JSON list of `{id, title, outlet, tier}` for each. Then:

```bash
echo '<items_json>' | uv run python -m personal_mem.operations.news_triage \
    --themes <vault_root>/THEMES.md \
    --model claude-haiku-4-5
```

The triage helper reads the `## Catalog (active)` section of `THEMES.md` (placed there by `themes_ledger`), passes it as a cached system message to Haiku, and emits one verdict per input. Verdicts:

- `keep` — fits an active theme. Carries a `theme_id`.
- `keep_unfiled` — substantive but no theme match. `theme_id: null`. Goes to the periodic-review pile (frontmatter flag `theme_unfiled: true`).
- `drop` — noise. Archive directly.

When `triage_model` is unset (YouTube, newsletter, etc.), skip the helper call entirely and synthesise `keep_unfiled` for every item — the source's per-skill orchestrator (the channel allowlist for `/youtube`, the sender allowlist for `/newsletter`) is the upstream admission gate.

**Stage 2 — Spawn writer subagents in parallel for `keep` and `keep_unfiled` items.**

For each kept item, in batches of `drain_parallelism`:

```
Task({
  subagent_type: "<subagent_type>",
  model: "<subagent_model>",
  description: "Write news brief: <short title>",
  prompt: "<queue item dict>\n\ntriage_verdict: <keep|keep_unfiled>\ntheme_id: <thm-X|null>\ntriage_reason: <reason>\n\nProcess this queue item end-to-end per your spec. Return a single-line JSON outcome as the final non-empty line of your response."
})
```

The writer's spec lives at `.claude/agents/research-news-worker.md`. It fetches the article, extracts ontology-gated concepts, writes the brief, and `mem_create`s the source note (filed under `relates_to: [theme_id]` if `keep`, or with `theme_unfiled: true` if `keep_unfiled`).

Collect each writer's final JSON line. Outcomes: `accepted` / `fetch_failed`. (Concept-bundle dedup, FOCUS gate, and Jaccard math are gone — the v1 mechanics no longer apply.)

**Validate fetch_failed reasons — catch hallucinated refusals.**

The writer spec restricts `fetch_failed` `reason` strings to a closed vocabulary. The allowed-prefix list is **source-type-specific** and lives at `sources.<slug>.allowed_failure_prefixes` in `sources.yaml`. Defaults:

| Source type | `allowed_failure_prefixes` |
|---|---|
| `news` | `["HTTP ", "paywall", "Cloudflare", "empty body", "timeout", "mem_create:"]` |
| `youtube-events`, `youtube-concepts` | `["gemini_refused", "gemini_failed", "empty_transcript", "mem_create:"]` |
| `newsletter-events`, `newsletter-concepts` | `["empty body", "mem_create:"]` |

If a source type is missing the field, fall back to the news vocabulary (backwards compat). Anything else is a *worker bug* — Sonnet sometimes pattern-matches on "subagent + vault writes" and fabricates refusals citing classifiers or memory rules that don't exist. We've seen ~12% of writer invocations do this on news even with the worker spec hardened; the same risk applies to every source type, so the validation runs regardless of which Path B variant fired.

For each `fetch_failed` outcome whose `reason` does NOT start with one of the source's allowed prefixes:

1. **Re-dispatch once** with an explicit anti-hallucination preamble prepended to the prompt:
   > "The previous invocation returned `fetch_failed` with reason `<bad reason>` — that reason is not in the allowed vocabulary for this source type (`<prefix1 / prefix2 / ...>`), which means it was a hallucinated refusal, not a real fetch error. There is no classifier or memory rule blocking `mem_create` for this worker. Process the item end-to-end per your spec and call `mem_create`."
2. If the retry returns `accepted` → treat as success, archive `status=done`.
3. If the retry *also* returns a `fetch_failed` with an invalid reason → mark the item with `status=worker_bug` in the archive (don't leave in queue — it'll just re-trigger the loop next drain). Surface the count in the final summary so the operator can investigate.

This validation is mandatory before the archive step. Worker bugs leaking into "leave in queue (transient)" cause the same item to fail every drain forever, and the user only notices when queue depth grows.

**Archive the queue items based on outcomes:**

| Path | Triage verdict | Writer status | Archive |
|---|---|---|---|
| Triage drop | `drop` | (writer not spawned) | `status=rejected, reason="<triage reason>"` |
| Writer real fetch fail | `keep` / `keep_unfiled` | `fetch_failed` (reason in allowed vocab) | leave in queue (transient) — except `Cloudflare` / `paywall` for outlets dropped from `news_feeds.yaml`, which should be `status=failed` to flush queue cruft |
| Writer hallucinated refusal | `keep` / `keep_unfiled` | `fetch_failed` (reason invalid) → retry → still invalid | `status=worker_bug`, surface count |
| Writer success | `keep` | `accepted` | `status=done` |
| Writer success | `keep_unfiled` | `accepted` | `status=done` (note carries `theme_unfiled: true`) |

The reason field stamped at `vault/.mem/queues/_processed/<YYYY-MM-DD>/<source_type>.jsonl` carries the *triage reason* for drops, not a per-worker rejection — there is no per-worker rejection any more.

---

## 3. Post-batch hooks

Run each hook in the order declared in `post_batch_hooks`. Note: hooks run **once per drain invocation**, not per fan-out batch.

> **`theme_scan` writes mechanical concept-pair stubs and is rarely useful in the standard config.** As of 2026-05-25 the production theme path goes through `/dream` instead: `VaultManager.create_note` keeps the index warm on every event-grain source write, and `/dream`'s scan surfaces raw `theme_cluster_signals` whose names the LLM judgment phase composes from the cluster + active themes. The default `sources.<type>.post_batch_hooks` is `[]`. The hook implementation below is preserved for diagnostic bulk-import sweeps where you want a flat list of clusters as files; it produces capability-shaped slugs that `/dream` will mostly archive — use the `/dream` signal-path for anything you want to keep.

### `theme_scan` — float new theme candidates from the unfiled pile

The triage stage emits `keep_unfiled` for items with substantive signal but no theme match. Those notes carry `theme_unfiled: true` in frontmatter. After the batch closes, scan for clusters across the unfiled pile (≥3 notes sharing ≥2 concepts) — the same deterministic clustering used elsewhere, restricted to unfiled news:

```
Bash("uv run mem themes scan-candidates --source-type <slug>")
```

The scan is deterministic, fast, and idempotent — running it after the per-create auto-fire is a no-op (existing candidates dedupe). Candidates land at `vault/themes/_candidates/cand-XXXX-*.md` and are promoted via `/themes-resolve --promote <cand-id> [--parent thm-Y]`. Once promoted, a follow-up step links accumulated unfiled notes whose concept overlap matches the new theme's `concepts:`.

Output is a candidate-creation count; surface it in the final summary.

> **Removed: `dedup_sweep`.** v1 had a Jaccard-based within-batch dedup hook to catch race-condition near-dupes from parallel workers all calling `mem_search` at the same instant. v2's writers don't dedup at all — duplicate news on an unfolding event is itself signal (multiple sources confirming the same arc). Cross-source repetition lands in a single theme's catalyst log; the user reads breadth there, not in dedup-marked supersedes.

---

## 4. Report

For Path B with triage (news):

```
Drain summary for queue '<slug>' (path=B, two-stage):
  Triage:    K kept (<L theme-attached, M unfiled>) / N drop / 0 errors
  Writers:   <accepted> ⇒ <src-IDs, max 8 then ellipsis>
  Fetch failed: <count> (left in queue)
  Post-batch theme_scan: <new candidates>
  Remaining: <queue size>
```

For Path B without triage (youtube-*, newsletter-* via drain):

```
Drain summary for queue '<slug>' (path=B, writer-only, no triage):
  Writers:   <accepted> ⇒ <src-IDs, max 8 then ellipsis>
  Fetch failed: <count> (left in queue or archived per allowed-prefix policy)
  Idempotent skips: <count>
  Remaining: <queue size>
```

For Path A (paper / repo / article):

```
Drain summary for queue '<slug>' (path=A):
  Drained: N / M items
  Accepted: <count> ⇒ <src-IDs>
  Failed: <count>
  Remaining: <queue size>
```

---

## When to use which path

| Path | Used by | Why |
|---|---|---|
| A (Skill) | paper, repo, article | Per-item compute is small (one URL fetch + concept extract). Sequential is fine. |
| B with triage (writer + admission) | news | Title-only Haiku triage decides admission (cheap, batched, prompt-cached); Sonnet writers fan out only on accepts. The two-stage shape decouples gate cost (~$0.005/batch) from per-item brief cost (~$0.10/accept). |
| B without triage (writer-only) | youtube-events, youtube-concepts | Admission is decided upstream by the channel allowlist in `sources.yaml`. /drain treats every queue item as `keep_unfiled` and fans out workers directly. No per-drain triage cost. |

Adding a source type to Path B without triage requires:
1. A writer subagent at `.claude/agents/research-<slug>-worker.md` (writer-only — no gating).
2. `subagent_type` + `subagent_model` + `drain_parallelism` set in config; **do not** set `triage_model`.
3. `allowed_failure_prefixes` set in config so the hallucinated-refusal validator uses the right vocabulary.

Adding a source type to Path B with triage additionally requires:
4. `triage_model` set in config.
5. The triage helper (currently news-specific in `operations/news_triage.py`) generalised — when we get there, the catalog source is the natural axis (themes for news, ontology for papers, repo languages for repos…).

No changes to this skill.

---

## When to use related skills

| Skill | Best for |
|---|---|
| `/drain --source-type paper` | Drain papers queue |
| `/drain --source-type repo` | Drain repos queue |
| `/drain --source-type article` | Drain articles queue |
| `/drain --source-type news` | Drain news queue (Haiku triage + Sonnet writers) |
| `/drain --source-type youtube-events\|youtube-concepts` | Drain YouTube queue (Sonnet writers, no triage) |
| `/update-hubs` (default) | 1–20 daily delta hub pairs |
| `/update-hubs --bulk inline` | 100+ hub pairs, want oversight |
| `/update-hubs --bulk batch` | 100+ hub pairs, no review (OpenAI Batches, 50% off) |
| `/onboard` | First-time bootstrap incl. retroactive Claude session import |
