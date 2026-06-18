---
description: Inline session synthesis — walk imported-but-unsynthesised Claude Code sessions and compose a summary + insights + decisions via the running model, weave_extract one wrap-shaped session per transcript. Small backlogs run in-process; large ones deterministically fan out subagents. The keyless `--via inline` half of `weave import claude-code --enrich`; pairs with `--via batch` which fans out through the API wrapper.
allowed-tools: Task, Bash, mcp__thinkweave__weave_concepts, mcp__thinkweave__weave_read, mcp__thinkweave__weave_extract, mcp__thinkweave__weave_update
---

# /seed-enrich — Inline session synthesis

Turn imported Claude Code sessions into durable memory **without burning a
provider key**. An imported session is materialised as a verbatim transcript
dump (`enrichment` deferred); this skill synthesises each one — a summary plus
derived insight and decision notes — using the running session's model instead
of `agent_client.batch_completions_sync`.

Same spec, same writeback as the batch path: both reach `weave_extract`
(`extract_session`), so an inline-synthesised session is byte-for-byte
identical to a `--via batch` one and to a live `/wrap` session —
ontology-gated concepts, commit-evidence decision flips, `processed: true`.

The work is **the same for any backlog size; only the topology changes.** A
small backlog is synthesised in-process (no subagent spawn overhead — that is
dead weight for a handful of sessions). A large backlog deterministically fans
out `seed-enrich-worker` subagents, mirroring `/drain`'s writer fan-out, so a
full-history backfill parallelises and stays off the orchestrator's context.
The threshold and batch shape are config knobs (`[enrich]` in
`vault/.weave/config.toml`); you read the decision off the `--dry-run` line —
you never improvise it.

The user reaches this from `weave import claude-code --enrich --via inline`
(the CLI prints a hint pointing here) or directly as `/seed-enrich`.
Headless-safe: `claude -p "/seed-enrich"` works the same as in-session.

## Steps

1. **Load the ontology.** Call `mcp__thinkweave__weave_concepts(action='list', limit=1000)`. This is the canonical vocabulary; anything proposed outside it is routed to `proposed_concepts:` by the server-side gate (no need to pre-filter — proposing specific terms is encouraged). Hold the list; you pass it to each worker so they don't each re-fetch it.

2. **Read the plan + worklist.** Run:
   ```bash
   weave import claude-code --enrich --dry-run
   ```
   Parse two line shapes:
   - `FANOUT\t<mode>\t<threshold>\t<batch_size>\t<parallelism>` — one line. `<mode>` ∈ {`inline`, `fanout`}, decided by config against the pending count.
   - `PENDING\t<note_id>\t<project>\t<title>` — one per session awaiting synthesis.

   Honor any `--limit` the user passed by capping the PENDING list. If there are no PENDING lines, report "nothing to synthesise" and stop. Dispatch on `<mode>`:

### Step 3 — Synthesise

#### mode = `inline`  (pending ≤ threshold)

Process the worklist in-process. For each `PENDING` `<note_id>`:
   - `mcp__thinkweave__weave_read(id=<note_id>)` — the body is the verbatim `## Transcript` dump (you can fan the reads out in parallel).
   - Compose, from the transcript, the four artifacts (the §B spec below).
   - Write it back:
     ```
     mcp__thinkweave__weave_extract(
       session_id=<note_id>,
       summary=<summary>,
       insights=[{title, body, concepts}, ...],
       decisions=[{title, rationale, outcome, file_paths, concepts}, ...],
     )
     ```
   - Tag the session note's own concepts:
     ```
     mcp__thinkweave__weave_update(note_id=<note_id>, frontmatter_updates={"concepts": [<session concepts>]})
     ```

#### mode = `fanout`  (pending > threshold)

Deterministically fan out `seed-enrich-worker` subagents — **don't improvise the topology, read it off the FANOUT line:**

1. Split the `PENDING` worklist into contiguous batches of `<batch_size>` sessions each.
2. Spawn the batches **`<parallelism>` workers at a time** (one parallel message per wave; the next wave starts only after the current wave's workers return). For each batch:
   ```
   Task({
     subagent_type: "seed-enrich-worker",
     description: "Synthesise <N> imported sessions",
     prompt: "{\n  \"batch\": [{\"note_id\": ..., \"project\": ..., \"title\": ...}, ...],\n  \"ontology\": [<the concept list from step 1>]\n}\n\nSynthesise each session in `batch` end-to-end per your spec. Return a single-line JSON outcome as the final non-empty line of your response."
   })
   ```
   **Never pass `model:`** — the worker's model is pinned in `agents/seed-enrich-worker.md` (that file is the single place to retune it).

   **Install-route namespacing.** Plugin installs (marketplace or `weave dev-link`) register the worker as `thinkweave:seed-enrich-worker`; project-scope clones use the bare name. Spawn with bare `seed-enrich-worker` first; if the agent type doesn't resolve, retry once as `thinkweave:seed-enrich-worker` (the failure message lists the available types).
3. Collect each worker's final JSON line. If a line is malformed, re-dispatch that one batch once with an anti-hallucination preamble ("return ONLY the JSON envelope as the last line"); if still malformed, log it as a worker error and continue. Tally `synthesized` / `decisions_created` / `insights_created` and the per-session errors across all workers.

### §B — Composition spec (both modes)

The four artifacts, identical to the batch backend and to live `/wrap` §C:
   - **summary** — 2–4 sentences of plain prose (becomes the session body), ≤400 chars.
   - **insights** — `[{title, body, concepts}]`. Gotchas, patterns, trade-offs the session surfaced; body ≤1000 chars each. Empty list if none stand out.
   - **decisions** — `[{title, rationale, outcome, file_paths, concepts}]` where `outcome ∈ {committed, abandoned, partial}`, rationale ≤1500 chars. Explicit choices made/accepted — skip exploratory chatter.
   - **concepts** — 2–6 specific session-level terms (`fts5`, `rrf-fusion`), not meta-terms; ≥2 on every insight and decision too.

   `weave_extract` archives the transcript to a `transcript.md` companion, mints the insight/decision notes, writes the `## Summary`, and stamps `processed: true` — all in one call. Backfill is conservative: don't invent insights the transcript doesn't support, and don't attach `predicted_outcome` (historical sessions have no checkable forward pointer).

### Step 4 — Batch finalize (both modes)

`weave_extract` writes the notes but does **not** run the deterministic tail. Run it **once** for the whole backlog (per-session finalize would be quadratic):
   ```bash
   weave index
   ```
   then regenerate landing docs for each distinct project in the worklist:
   ```bash
   weave landing --project <project> --doc all
   ```
   This is the batch-grain analogue of the per-session `weave wrap-finalize` that live `/wrap` and the `dream-wrap-worker` run — it indexes the new notes and refreshes DECISIONS/BACKLOG so the backfilled sessions are immediately retrievable and visible in landing docs. Skip the judge/drift steps: imported historical decisions carry no forward prediction to judge.

### Step 5 — Report

One line: `Synthesised N session(s) [inline | via M worker(s)]; created D decision(s), I insight(s); finalized P project(s).`

## Notes

- **Idempotent.** A session synthesised once is `processed: true` and drops out of the pending list — re-running is safe and skips it. A worker batch that partly failed can be re-run; the done sessions skip.
- **Don't re-embed the transcript.** Only the structured synthesis goes into `weave_extract`; the raw transcript is preserved as the companion, not duplicated into the summary.
- **Thin sessions are fine.** If a transcript is too sparse to extract anything, pass a one-sentence `summary` with empty `insights`/`decisions` — the session still becomes `processed`.
- **Tuning the fan-out.** `[enrich] fanout_threshold / batch_size / parallelism` in `vault/.weave/config.toml` govern when fan-out kicks in and its shape. Defaults: 12 / 6 / 3.
- **Large backlogs with an API key** are also available via `weave import claude-code --enrich --via batch` (provider fan-out instead of subagents). This skill is the no-key route and the small-batch default; the fan-out makes it scale to full-history backfills too.
