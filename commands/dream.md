---
name: dream
owns_mechanic: vault_synthesis
consumes: [mem_concepts, mem_search, mem_read, mem_update, mem_link, mem_create, mem_extract]
produces: [ontology.yaml, vault/themes/*, vault/concepts/topics/*, vault/.mem/maintenance.jsonl, vault/digests/*, vault/reports/dream/*]
tools:
  - Read
  - Bash
  - Task
  - mem_concepts
  - mem_search
  - mem_read
  - mem_update
description: Periodic dream cycle — two-phase subagent orchestrator. Phase 1 fans out 5 synthesis workers (promotion/merge/theme/essence/priority), merges plan fragments, applies. Phase 2 fans out 3 composition workers (wrap catch-up, prediction judge, knowledge digest). One cron entry, eight workers, single maintenance.jsonl line per cycle. Self-deciding, headless-safe.
---

# /dream — Two-phase synthesis + composition cycle

The cron-friendly synthesis orchestrator. Phase 1 is **synthesis** (concept hygiene + theme mint/extend + essence rewrites + priority signals → plan → apply). Phase 2 is **composition + consumption** (catch-up unwrapped sessions, drain the rejudge queue, compose the day's knowledge digest).

**One cron entry replaces three** — what used to be three nightly jobs (`/dream`, `/mem-wrap` catch-up, `/judge-prediction --drain`) is now one orchestrator that fans out 8 workers across two phases.

Self-deciding. **Never prompts the user.** Designed for `claude -p "/dream"` cron use; works the same interactively.

The trust substrate is two artifacts per cycle:
- `vault/.mem/maintenance.jsonl` — one JSON line summarising what apply did (phase 1) plus phase-2 outcomes.
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
uv run mem dream scan --promotion-cap 20 --json > "$CYCLE_TMP/scan.json"
```

Returns a `DreamCycleScan` JSON payload with all six scan surfaces (`promotion_candidates`, `drift_pairs`, `theme_cluster_signals`, `active_themes`, `recent_probes`, plus phase-2 surfaces `unwrapped_sessions`, `rejudge_queue`, `knowledge_delta`). The `cycle_id` field is the cycle identity — carry it through to apply and into worker prompts.

### Step 1.2 — Load phase-1 tasks

```bash
uv run mem dream tasks --phase 1 --scan "$CYCLE_TMP/scan.json" --json > "$CYCLE_TMP/tasks-p1.json"
```

Emits a list of `{worker_name, surface_key, plan_keys, depends_on}` entries — only the phase-1 specs whose `has_signal(scan)` is True. Empty list means nothing to do this phase; skip to phase 2.

### Step 1.3 — Fan out phase-1 workers in parallel

**Spawn every enabled phase-1 task in a single message** as parallel `Task` calls. The orchestrator (you) does no judgment in this step — it routes the scan slice for each worker into a `Task` invocation.

For each task entry from `tasks-p1.json`:

```
Task({
  subagent_type: "<worker_name>",       // e.g. "dream-promotion-worker"
  model: "sonnet",
  description: "Dream phase-1 <surface_key>",
  prompt: <see per-worker prompt shape below>
})
```

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
uv run mem dream apply --plan "$CYCLE_TMP/plan.json" --json > "$CYCLE_TMP/apply-result.json"
```

The apply phase batches every structural change (merges → promotions → theme mints → theme extensions → essence rewrites → priority signals → one index rebuild → one maintenance.jsonl line). The essence-rewrite plan key with `new_essence` field actually mutates each theme's `## Essence` section. Returns a `DreamCycleResult` JSON.

If `apply-result.json` has non-empty `errors`, surface them in the wrap-up — the errors-don't-cascade contract guarantees the other steps still ran, so the cycle is partially successful, not failed.

## Phase 2 — Composition + Consumption

Phase 2 runs **after** phase 1's apply so its workers see the post-apply state (theme mints + extensions are already on disk; the index is rebuilt).

### Step 2.1 — Load phase-2 tasks

```bash
uv run mem dream tasks --phase 2 --scan "$CYCLE_TMP/scan.json" --apply-result "$CYCLE_TMP/apply-result.json" --json > "$CYCLE_TMP/tasks-p2.json"
```

Same shape as phase-1 tasks; each entry now potentially carries a `depends_on: [<worker_name>, ...]` field. The registry encodes `dream-digest-worker.depends_on = ["dream-judge-worker"]` because the digest reads the day's verdict flips (including any just-applied this cycle).

### Step 2.2 — Topologically fan out phase-2 workers

Walk the task list and group by dependency wave:

- **Wave A** — every task whose `depends_on` is empty (or all its named workers are not in the enabled list). For the v1 registry this is `dream-wrap-worker` and `dream-judge-worker`.
- **Wave B** — every task whose `depends_on` is non-empty (and at least one named worker is in the enabled list). For v1 this is `dream-digest-worker`.

**Spawn Wave A in parallel** (single message, one `Task` per enabled worker):

```
Task({
  subagent_type: "<worker_name>",       // dream-wrap-worker / dream-judge-worker
  model: "sonnet",
  description: "Dream phase-2 <surface_key>",
  prompt: <per-worker prompt — surface slice from scan.json + cycle_id>
})
```

**After Wave A returns, spawn Wave B**. Wave B workers receive their dependents' outcomes as additional context in the prompt:

```
Task({
  subagent_type: "dream-digest-worker",
  model: "sonnet",
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
  Phase 1: promoted N concepts. Minted T themes, extended X. M drift merges.
           E essence rewrites. P priority signals (Q enqueued).
  Phase 2: wrapped W sessions. Judged J verdicts (F flipped). Digests: <concept-id> (Sc sections), <event-id> (Se sections); skipped grains: <list>.
Logged to vault/.mem/maintenance.jsonl. Digests at vault/digests/YYYY-MM-DD-{concept,event}.md.
Cycle took <wall-time>s. Worker bugs: <worker_bug count or "none">.
```

Mirror the `/mem-wrap` wrap-up format. Keep it tight — the maintenance log + digest note carry the detail.

Clean up the cycle temp:

```bash
rm -rf "$CYCLE_TMP"
```

## Notes

- **First cycle on a vault with backlog will hit the 20-promotion cap.** This is fine — the cycle drains across multiple nightly runs. Steady state is ~0-5 surfaced items per cycle per worker.
- **The scan never crawls the filesystem from this skill.** All discovery is in the `mem dream scan` Bash call, which uses the SQLite index. Phase-2 surfaces follow the same rule: `unwrapped_sessions` / `rejudge_queue` / `knowledge_delta` are all index-driven.
- **No prompts.** If a worker's input is ambiguous, it skips (capability-named theme clusters age out cheaply; false promotion costs a `/themes-resolve` fix-up later).
- **Phase 2 is dependency-aware, not just sequential.** Wave A is parallel; Wave B waits for its `depends_on` workers. New workers added to the registry that declare additional dependency edges fit into the same topology — the orchestrator does not need a change.
- **Workers are pure-output (phase 1) or write-with-receipt (phase 2).** Phase-1 workers never Edit files; the apply step does all writes. Phase-2 workers DO write directly (e.g. `dream-wrap-worker` calls `mem wrap-finalize`; `dream-digest-worker` calls `mem_create`), but emit a `side_effects` list so the orchestrator can include the receipts in the report.
- **Hub fill is out of scope.** If a worker notices an empty concept hub during its judgment, it leaves it — `mem drain --target hubs` owns hub population, deliberately decoupled.
- **Cron consolidation.** This skill replaces three former cron entries (`/dream`, `/mem-wrap` catch-up, `/judge-prediction --drain`). The standalone `/mem-wrap` and `/judge-prediction` skills remain available for interactive use; only their cron entries collapse into this orchestrator.

## Post-install / restart caveats

- The subagent registry is captured at claude-process start. `.claude/agents/*.md` files added in-session (including the `dream-*-worker` files installed by `mem install`) won't be available until restart — `/clear` doesn't reload them. Exit claude and re-launch after install or after adding new subagents.
- The MCP server is process-bound too. After `mem install` upgrades or any change to MCP-exposed schemas/enums (new `NoteType` values, new tools, new enum members), restart claude to pick them up.
- The cron `claude -p '/dream'` path is fresh-process every run, so it's unaffected by either caveat. This applies only to interactive sessions.
