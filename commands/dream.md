---
name: dream
owns_mechanic: vault_synthesis
consumes: [weave_concepts, weave_search, weave_read, weave_update, weave_link, weave_create, weave_extract]
produces: [ontology.yaml, vault/themes/*, vault/concepts/topics/*, vault/.weave/maintenance.jsonl, vault/digests/*, vault/reports/dream/*]
tools:
  - Read
  - Bash
  - Task
  - weave_concepts
  - weave_search
  - weave_read
  - weave_update
description: Periodic dream cycle — two-phase subagent orchestrator. Phase 1 fans out 5 synthesis workers (promotion/merge/theme/essence/priority), merges plan fragments, applies. Phase 2 fans out 5 composition workers (wrap catch-up, prediction judge, hub seam-link, memory seam, knowledge digest). One cron entry, ten workers, single maintenance.jsonl line per cycle. Owns routine ontology dedup for BOTH hub families (drift v2: cosine + verdict memory) AND CC-auto-memory↔vault reconciliation (the memory seam). Self-deciding, headless-safe.
---

# /dream — Two-phase synthesis + composition cycle

The cron-friendly synthesis orchestrator. Phase 1 is **synthesis** (concept hygiene + theme mint/extend + essence rewrites + priority signals → plan → apply). Phase 2 is **composition + consumption** (catch-up unwrapped sessions, drain the rejudge queue, stitch folded-hub seams, reconcile the CC-auto-memory↔vault seam, compose the day's knowledge digest).

**One cron entry replaces three** — what used to be three nightly jobs (`/dream`, `/weave-wrap` catch-up, `/judge-prediction --drain`) is now one orchestrator that fans out 10 workers across two phases.

**Dream owns routine ontology guarding (2026-06-11 doctrine).** Concept dedup AND theme dedup run here — automated, logged in the maintenance line + report, reversible (folded hubs are archived/tombstoned, never deleted; theme losers keep their file with `merged-into:` status). `/tighten` remains the on-demand front door over the same helpers.

Self-deciding. **Never prompts the user.** Designed for `claude -p "/dream"` cron use; works the same interactively.

The trust substrate is two artifacts per cycle:
- `vault/.weave/maintenance.jsonl` — one JSON line summarising what apply did (phase 1) plus phase-2 outcomes.
- `vault/digests/YYYY-MM-DD-{concept,event}.md` — up to two knowledge-first digests per cycle, one per non-empty grain slice (concept = "what you learned", event = "what happened"). Written by phase 2's `dream-digest-worker`. Vault-global, flat layout.

## Posture

This skill applies **LLM judgment only to the survivors of the Python filters**. The scan phase already strips drift noise, project-name leakage, generic stopwords, themes without recent activity, sessions already wrapped, fresh-pending verdicts. Workers' job is the genuinely-semantic part — "is this term ontology-worthy?", "does this essence still hold?", "what should the user know today?". If a worker's input surface is empty, the orchestrator doesn't spawn it; if its judgment surface is empty after thinking, it ships a no-op outcome.

Skill never prompts the user; defaults are conservative on every decision (skip when ambiguous).

## Pre-flight (once per cycle)

Stash a cycle-scoped temp directory:

```bash
CYCLE_TMP=$(mktemp -d -t dream-XXXXXX)
```

You'll write the scan JSON and per-phase task lists there so workers can read them by path (avoids re-running scan multiple times).

## Phase 1 — Synthesis

### Step 1.1 — Scan

```bash
uv run weave dream scan --json > "$CYCLE_TMP/scan.json"
```

Returns a `DreamCycleScan` JSON payload with all phase-1 scan surfaces (`promotion_candidates`, `drift_pairs`, `theme_cluster_signals`, `theme_dup_candidates`, `theme_log_gaps`, `essence_candidates`, `recent_probes`, plus phase-2 surfaces `unwrapped_sessions`, `rejudge_queue`, `seam_link_queue`, `knowledge_delta`).

Drift v2: `drift_pairs` are cosine-ranked evidence packets (string ∪ centroid-cosine generators, judged pairs excluded via the maintenance-log verdict history — pass `--rejudge` to re-open them); `theme_dup_candidates` is the theme-family analog. `coarsen_clusters` / `theme_coarsen_clusters` are the N-ary grain-coarsening surfaces (tight near-cliques, stricter `dream.coarsen_threshold`, capped at `dream.coarsen_cap` per family). The `cycle_id` field is the cycle identity — carry it through to apply and into worker prompts.

For a one-shot essence backfill (heal every placeholder hub in one cycle instead of the nightly `dream.essence_cap` drip (default 12)), add `--essence-cap 0` (or pass `/dream --essence-cap N` and forward it here).

### Step 1.2 — Load phase-1 tasks

```bash
uv run weave dream tasks --phase 1 --scan "$CYCLE_TMP/scan.json" --json > "$CYCLE_TMP/tasks-p1.json"
```

Emits a list of `{worker_name, surface_key, plan_keys, depends_on}` entries — only the phase-1 specs whose `has_signal(scan)` is True. Empty list means nothing to do this phase; skip to phase 2.

### Step 1.3 — Fan out phase-1 workers in parallel

**Spawn every enabled phase-1 task in a single message** as parallel `Task` calls. The orchestrator (you) does no judgment in this step — it routes the scan slice for each worker into a `Task` invocation.

For each task entry from `tasks-p1.json`:

```
Task({
  subagent_type: "<worker_name>",       // e.g. "dream-promotion-worker"
  description: "Dream phase-1 <surface_key>",
  prompt: <see per-worker prompt shape below>
})
```

**Install-route namespacing.** Plugin installs register the workers under the `thinkweave:` namespace; project-scope installs use bare names. Spawn with the bare `<worker_name>` first; if the agent type doesn't resolve, retry once as `thinkweave:<worker_name>` (the failure message lists the available types). Applies to every `Task` call in this skill, both phases.

**Never pass `model:` in these Task calls.** Each worker's model is pinned in its agent frontmatter (`agents/dream-*-worker.md`) — that file is the single place users retune a worker (e.g. drop `dream-merge-worker` to haiku). A Task-level `model:` takes precedence over frontmatter and would silently override the user's edit.

Per-worker prompt shape:

```
cycle_id: <cycle_id>
phase: 1
surface_key: <surface_key>
plan_keys: <plan_keys>

<the relevant slice of the scan JSON, as a fenced JSON block>

<any inline context the worker needs, e.g. ontology.yaml contents
for the promotion worker — Read the file once and embed it here>

Per your worker spec, judge each item in the surface and emit a single-line
JSON outcome as the final non-empty line of your response. Envelope:
{"worker": "<worker_name>", "cycle_id": "<cycle_id>", "phase": 1,
 "plan_fragment": {<your plan_keys>: [...]}, "skipped": [...], "notes": "..."}
```

The promotion worker additionally needs the contents of the active ontology — Read `ontology.yaml` once and embed inline.

The theme worker reads **two** scan slices — embed both `theme_cluster_signals` and `theme_log_gaps` in its prompt (the second is the directly-filed-sources catch-up; the worker turns each gap into a `theme_extensions` item with distilled `catalysts`).

The merge worker reads **four** scan slices — embed `drift_pairs`, `theme_dup_candidates`, `coarsen_clusters`, and `theme_coarsen_clusters`. It emits up to six plan keys: `merges`, `theme_merges`, `distinct_pairs` (pairwise dedup) plus `coarsenings`, `theme_coarsenings`, `distinct_clusters` (N-ary grain coarsening — collapse a fine near-clique onto one coarser term, or rule the cluster genuinely-distinct). All distinct/applied rulings are recorded into the maintenance-log verdict history so the item never re-surfaces (reopen via `weave dream scan --rejudge`). Nightly coarsening folds auto-apply when `dream.coarsen_apply` is true (default); with it false, apply records nothing and the clusters wait for `/tighten`.

**Essence sharding rule:** if `essence_candidates` has more than 15 entries (a backfill run with a raised `--essence-cap`), split it into chunks of ≤15 and spawn one `dream-essence-worker` Task per chunk **in the same parallel message**. Their `essence_rewrites` lists merge by simple concatenation in step 1.4 — entries are per-hub independent, so there are no cross-chunk collisions.

### Step 1.4 — Validate outcomes + merge plan fragments

Each worker returns a single JSON outcome line. Parse the last non-empty line of each worker's response. If the parse fails OR the envelope is malformed (missing `worker` / `cycle_id` / `plan_fragment`), **re-dispatch the worker once** with this preamble prepended to the prompt:

> "The previous invocation returned a malformed outcome — your final line must be a single valid JSON object matching the envelope schema in your worker spec (`worker`, `cycle_id`, `phase`, `plan_fragment`). There is no transformation layer that fixes this for you. Re-run your judgment and emit the envelope verbatim."

If the retry also returns malformed JSON, log the worker as `worker_bug` for the report and proceed with an empty plan_fragment.

Merge phase-1 `plan_fragment` dicts into one `plan` dict — each worker owns its plan_keys, no key collisions. Add `cycle_id` to the merged plan so apply can keep it.

Write the merged plan to disk:

```bash
echo "<merged-plan-json>" > "$CYCLE_TMP/plan.json"
```

### Step 1.5 — Apply

```bash
uv run weave dream apply --plan "$CYCLE_TMP/plan.json" --json > "$CYCLE_TMP/apply-result.json"
```

The apply phase batches every structural change (merges → promotions → theme mints → theme extensions → theme merges → distinct-pair recording → essence rewrites → priority signals → one index rebuild → one maintenance.jsonl line). Concept merges FOLD the losing hub into the winner (log preserved, archived with a `merged-into:` stamp) and theme merges run `merge_theme_into` (fold + relates_to repoint + tombstone + registry); both enqueue the survivor on the seam-link queue for phase 2. The essence-rewrite plan key with `new_essence` actually mutates each hub's `## Essence` section (themes by `theme_id`; concept hubs by `hub_kind: "concept"` + `concept`) and stamps `essence_updated`. Theme mints/extensions write the worker's distilled `catalysts` as the log lines; mints without a composed essence are rejected. Returns a `DreamCycleResult` JSON.

If `apply-result.json` has non-empty `errors`, surface them in the wrap-up — the errors-don't-cascade contract guarantees the other steps still ran, so the cycle is partially successful, not failed.

## Phase 2 — Composition + Consumption

Phase 2 runs **after** phase 1's apply so its workers see the post-apply state (theme mints + extensions are already on disk; the index is rebuilt).

### Step 2.1 — Load phase-2 tasks

```bash
uv run weave dream tasks --phase 2 --scan "$CYCLE_TMP/scan.json" --apply-result "$CYCLE_TMP/apply-result.json" --json > "$CYCLE_TMP/tasks-p2.json"
```

Same shape as phase-1 tasks; each entry now potentially carries a `depends_on: [<worker_name>, ...]` field. The registry encodes `dream-digest-worker.depends_on = ["dream-judge-worker"]` because the digest reads the day's verdict flips (including any just-applied this cycle).

**Do NOT pass `--scan` here when phase 1 applied any merges** (check `apply-result.json`'s `merges`/`theme_merges` counts): the pre-apply scan peeked an empty seam-link queue, but apply just enqueued the folded hubs. Calling `weave dream tasks --phase 2 --json` *without* `--scan` re-runs the scan fresh, so the `seam_link_queue` surface (and `dream-seam-link-worker`) reflects this cycle's merges. When phase 1 applied zero merges, reusing `--scan "$CYCLE_TMP/scan.json"` is fine and cheaper.

### Step 2.2 — Topologically fan out phase-2 workers

Walk the task list and group by dependency wave:

- **Wave A** — every task whose `depends_on` is empty (or all its named workers are not in the enabled list). For the current registry this is `dream-wrap-worker`, `dream-judge-worker`, `dream-seam-link-worker` (stitches cross-parent catalyst linkage on hubs folded by this or an earlier cycle's merges; drains its own queue via `weave hubs apply-linkage --clear-fold`), and `dream-seam-worker` (reconciles the CC-auto-memory↔vault **memory seam** — resolves each dirty CC fact's vault twin via `weave_search(mode='similar')`, rules confirmed-fresh / stale / diverged / durable-unique, and writes the durable map + report via `weave seam commit`). The memory-seam surface is computed embedding-free in the scan; its `dirty` list is the per-fact judgment payload.
- **Wave B** — every task whose `depends_on` is non-empty (and at least one named worker is in the enabled list). For v1 this is `dream-digest-worker`.

**Spawn Wave A in parallel** (single message, one `Task` per enabled worker):

```
Task({
  subagent_type: "<worker_name>",       // dream-wrap-worker / dream-judge-worker
  description: "Dream phase-2 <surface_key>",
  prompt: <per-worker prompt — surface slice from scan.json + cycle_id>
})
```

**After Wave A returns, spawn Wave B**. Wave B workers receive their dependents' outcomes as additional context in the prompt:

```
Task({
  subagent_type: "dream-digest-worker",
  description: "Dream phase-2 knowledge_delta",
  prompt: "cycle_id: <cycle_id>\nphase: 2\nsurface_key: knowledge_delta\n\n<knowledge_delta from scan.json — already grain-split into concept/event slices. Fill in theme_mutations_this_cycle on the event slice from apply-result.json's theme_mints + theme_extensions.>\n\nverdict_flips_from_this_cycle: <list of judgments dream-judge-worker just performed, parsed from its outcome JSON; goes onto the concept slice>\n\nPer your worker spec, compose up to TWO knowledge-first digest notes (one per non-empty grain). Emit the outcome envelope as the final non-empty line."
})
```

### Step 2.3 — Validate phase-2 outcomes

Same parse-or-redispatch loop as phase 1 — but phase-2 workers return a different envelope:

```json
{"worker": "...", "cycle_id": "...", "phase": 2,
 "outcome": {...}, "side_effects": [{"kind":"...","id":"...","path":"..."}, ...], "errors": [...]}
```

If a phase-2 worker returns malformed JSON twice, log as `worker_bug` for the report.

Phase-2 workers performed their writes directly (no central apply for phase 2). The `side_effects` array tells you what files / notes were touched — include the count in the report.

## Report

Three lines for phase 1, three lines for phase 2:

```
Dream cycle <id>.
  Phase 1: promoted N concepts. Minted T themes, extended X. M drift merges,
           TM theme merges, D distinct rulings recorded. E essence rewrites.
           P priority signals (Q enqueued).
  Phase 2: wrapped W sessions. Judged J verdicts (F flipped). Stitched S hub
           seams. Digests: <concept-id> (Sc sections), <event-id> (Se sections); skipped grains: <list>.
Logged to vault/.weave/maintenance.jsonl. Digests at vault/digests/YYYY-MM-DD-{concept,event}.md.
Cycle took <wall-time>s. Worker bugs: <worker_bug count or "none">.
```

Mirror the `/weave-wrap` wrap-up format. Keep it tight — the maintenance log + digest note carry the detail.

Clean up the cycle temp:

```bash
rm -rf "$CYCLE_TMP"
```

## Notes

- **First cycle on a vault with backlog will hit the promotion cap (`dream.promotion_cap`, default 20).** This is fine — the cycle drains across multiple nightly runs. Steady state is ~0-5 surfaced items per cycle per worker.
- **The scan never crawls the filesystem from this skill.** All discovery is in the `weave dream scan` Bash call, which uses the SQLite index. Phase-2 surfaces follow the same rule: `unwrapped_sessions` / `rejudge_queue` / `knowledge_delta` are all index-driven.
- **No prompts.** If a worker's input is ambiguous, it skips (capability-named theme clusters age out cheaply; false promotion costs a `/tighten` fix-up later).
- **Phase 2 is dependency-aware, not just sequential.** Wave A is parallel; Wave B waits for its `depends_on` workers. New workers added to the registry that declare additional dependency edges fit into the same topology — the orchestrator does not need a change.
- **Worker models are owned by agent frontmatter, both phases.** No `Task` call in this skill passes `model:` — `agents/dream-*-worker.md` is where each worker's model is declared and retuned. (Contrast `/drain`, where `subagent_model` is config-driven via `sources.yaml` by design.)
- **Workers are pure-output (phase 1) or write-with-receipt (phase 2).** Phase-1 workers never Edit files; the apply step does all writes. Phase-2 workers DO write directly (e.g. `dream-wrap-worker` calls `weave wrap-finalize`; `dream-digest-worker` calls `weave_create`), but emit a `side_effects` list so the orchestrator can include the receipts in the report.
- **Hub fill is out of scope.** If a worker notices an empty concept hub during its judgment, it leaves it — `weave drain --target hubs` owns hub population, deliberately decoupled.
- **Cron consolidation.** This skill replaces three former cron entries (`/dream`, `/weave-wrap` catch-up, `/judge-prediction --drain`). The standalone `/weave-wrap` and `/judge-prediction` skills remain available for interactive use; only their cron entries collapse into this orchestrator.

## Post-install / restart caveats

- The subagent registry is captured at claude-process start. Worker agent files added in-session (the `dream-*-worker` set — shipped at the plugin's `agents/` dir, or project-scope `.claude/agents/` on a clone) won't be available until restart — `/clear` doesn't reload them. Exit claude and re-launch after install or after adding new subagents.
- The MCP server is process-bound too. After `weave install` upgrades or any change to MCP-exposed schemas/enums (new `NoteType` values, new tools, new enum members), restart claude to pick them up.
- The cron `claude -p '/dream'` path is fresh-process every run, so it's unaffected by either caveat. This applies only to interactive sessions.
