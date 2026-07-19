# How finished issues feed thinkweave

Status: **accepted** — owner sign-off 2026-07-15. The vault-write step
(`issue_loop.py trajectory`, command §3) is enabled and runs unattended:
after each processed issue the orchestrator writes one trajectory note. What
follows documents that live behavior.

## The gap

The loop's outputs live in three places today: the tracker (comments, PRs —
the run history), git (the code), and the orchestrator session (which /wrap
or the dream wrap-catch-up worker will synthesize *if* someone wraps it).
What's lost is the per-issue **trajectory** — how the work actually went:
which seams were tested, why fix rounds happened, what a future run should
do differently. That is precisely the "task-trajectory capture primitive"
the month plan names as the precondition for self-evolution, and the loop is
its ideal first emitter: every issue is a task instance with a naturally
structured trajectory (issue → branch → gates → fix rounds → PR).

## Design

**One `note` per processed issue, created at run end by the orchestrator.**
No new note type, no lifecycle fields — the only status-like field
(`outcome`) is an observable fact (shipped / routed-to-human /
awaiting-approval), and every ref (issue, PR) is a URL whose state GitHub
owns.

Division of labor, honoring "don't store what another surface records": four
surfaces — tracker, PR, trajectory note, session note (`/wrap`) — each own a
disjoint slice, and decisions are minted by the session/`/wrap` owner alone.
The full four-owner table is the **capture-parity contract** in
[`vault-issue-contract.md`](vault-issue-contract.md) — its single source of
truth, codified and tested (`tests/test_vault_issue_contract.py`) with the
wrap-coverage guarantee for headless runs. It is not duplicated here.

**Mechanics.** After §2 Report, for each processed issue:

1. `issue_loop.py trajectory <N> --cwd <worktree> --gates-json <file>
   [--skills-json <file>] [--skill-centric] --fix-rounds R --outcome shipped
   --pr-url <url> --run-id <id>` assembles the deterministic half: files
   touched, commit count, gate verdicts, skill invocations, refs — emitted
   as a `weave_create`-shaped payload.
2. The orchestrator fills the judgment half: a ≤1K-char body (What / How it
   went — the run-causal register only; **Lessons are retired**, see below),
   and **concepts chosen at creation** from the ontology (`weave_concepts`
   first; `concept_hints` in the payload carries the issue's labels). Then
   one `weave_create(type=note, tags=[loop-run], session_id=<this session>,
   frontmatter=<payload>)`, adding a `builds_on` list (under `frontmatter=`) of
   the ship-time insight note ids. MCP down → `weave add -f …` CLI fallback.
3. `/wrap` (interactive) or the dream wrap-catch-up worker (headless) later
   synthesizes the *session* as usual — trajectory notes already sit in the
   session folder via `session_id`, so the wrap references them instead of
   re-describing per-issue detail. Where an issue's resolution embodied a
   real architectural choice, wrap promotes it to a `decision` exactly as it
   would in a hand-driven session — the loop never auto-mints decisions
   (decision-per-PR would be noise, and concepts/judgment belong to wrap's
   LLM pass, not a template).

**Invocation-trajectory extension (epic #54 / #56).** The frontmatter also
carries `skills[]` — the loop's stage-dispatch log, one entry per dispatched
stage skill as `{id, role, outcome, fix_rounds_attributed}`. A stage skill is
effectively a gate/subagent the loop already dispatches (implementer,
acceptance judge, reviewer, and future ponytail/tdd), so this is a
frontmatter extension, not new hooks — it keeps the lightweight /
no-pre-action-injection doctrine. `fix_rounds_attributed` makes fix-round
attribution explicit: which gate/skill caused each round (the total stays in
`fix_rounds`). The orchestrator passes the log via `--skills-json`; existing
callers pass nothing and get `skills: []`. When a record is primarily about a
skill invocation (SkillOpt raw material, #64), `--skill-centric` adds the
`skill-invocation` tag alongside `loop-run`, so
`weave_search(tags=[skill-invocation], concepts=[…])` returns
skill-attributed records.

**Semantic execution trace + Lessons retirement (issue #85).** Two register
collisions closed at once. (1) The trajectory's `## Lessons` section duplicated
the *insight* lane — same portable-wisdom register — so it is **retired**: the
body is the run-causal register only (What / How it went), and portable lessons
are minted as separate **insight notes** at ship time (concepts at creation) and
linked from the trajectory via `builds_on`. The register test that sorts every
artifact: **run-bound semantic trace → the trajectory's `trace`; portable lesson
→ an insight note, linked; enumerable fact → a frontmatter key.** (2) The signal
that *was* being discarded — the semantic execution trace the gate agents
already compose (reviewer findings + reasoning, simplify cut/keep rationale,
judge criterion evidence + verdict flips, TDD red-confirmation) — is now captured
under a single `trace` frontmatter key:

```
trace:
  rounds:     [{gate, finding, severity, disposition, fixed_by}]   # prose-valued
  criteria:   [{id, verdict, flipped_by_round}]                    # flip = int|null
  simplify:   {outcome, cuts:[{what,why}], kept:[{what,why}], lines_delta}
  edge_cases: [<prose>]
  tdd:        {red_confirmed}
```

The orchestrator condenses these envelopes **from the gate agents' own reports —
no new model call** (`--trace-json`, §3); the rail (`_normalize_trace`) only
accepts and shapes them (strict on type — a non-dict trace is rejected; lenient
on keys — unknowns dropped, each item projected). Counts (`lines_delta`,
`flipped_by_round`) are filter/join keys, not signal. The `trace` is the
**machine-readable half of the tracker's gate evidence, not a second prose
owner** — it duplicates neither the tracker's prose nor the trajectory body. It
is a top-level frontmatter key no existing consumer reads, so #60's outcome
judge, `weave rlvr export` (row envelope locked), and #62's steering evidence are
untouched; absent (`--trace-json` omitted), the pre-#85 payload is byte-stable.

**What the notes buy.** Trajectory notes carry concepts, so they flow into
concept hubs, digests, and retrieval like any note: "what did the loop learn
about the indexer" is `weave_search(concepts=[…], tags=[loop-run])`. Grouped
by `run_id`/`issue` frontmatter they are the raw material for the
task-trajectory primitive (grouping repeated task instances across sessions)
without committing to its schema now.

**Serving trajectories back into the implementer (epic #54 / #57).** Capture
without serving is a dead end. Claim-time priming is thinkweave's native
`bd prime`: before dispatching issue N's implementer, `issue_loop.py prime <N>
--run-id <id> --labels …` reads the derived index read-only, matches
`[loop-run]` trajectory notes by the issue's concepts, and emits a
budget-capped block of their **reusable color** that the orchestrator splices
into the implementer prompt, adjacent to the standing `decisions_for_file`
context (§1b). Empty match or holdout → nothing spliced, loop unchanged.

**Prime v2 (issue #85): serve insight bodies via links, weighted by outcome.**
For each concept-matched trajectory, prime follows its `builds_on` links to the
linked **insight notes** and serves *their bodies* (the portable lesson's new
home); a v1 trajectory with no links falls back to its inline `## Lessons`
section, so the 13 pre-#85 notes still serve. When the matched set carries
`outcome_label`s (from #60's judge), prime stably orders merged-clean/stable
trajectories ahead of reworked/closed ones before the budget cap — a
deterministic sort tweak, not a scoring framework; an all-unlabeled set keeps
pure recency. Served ids are the *insight* ids for a v2 trajectory (that is what
the run received) and the *trajectory* id for a v1 fallback.

- **Served-context logging.** The prime emits the `served` note ids (insight
  notes / trajectory Lessons + decisions_for_file, capped top-`limit` per kind).
  The orchestrator
  mirrors `primed` + `served` into the trajectory note frontmatter (§3), and —
  when passed the session buffer via `--buffer` — the rail writes a `loop_prime`
  retrieval event that the indexer projects to
  `context_served(source='loop-prime')` (the same sentinel-tool mechanism
  prompt-time retrieval uses; context_served stays a pure projection of
  `retrieval_log.jsonl`). Served ids are recoverable per run from the index by
  both routes.
- **Deliberate holdout.** Every `prime_holdout`th run (default 5th; `loop.toml`
  knob, `--set`-overridable) dispatches **unprimed**, marked `primed: false`
  with no served ids. The holdout is deterministic per run-id
  (`sha1(run_id) mod N == 0`, not random). Loop runs are numerous, comparable,
  and gate-scored, so regressing #60's `outcome` against `primed`/served
  context separates "context helped" from "easy issue".

**Deliberately not built** (until felt): serving is retrieval-only today —
no learned ranking of which prior lessons help most (the holdout regression is
the measurement substrate that would inform it later).

**Closing the loop back into *proposals* (epic #54 / #62).** The trajectory
substrate also feeds the *slow* loop — the weekly Routine (#61, not yet built)
that runs improve-arch / ponytail-audit and files self-improvement issues. To
stop it inventing work, `weave steering gate` reads the same index read-only and
computes per-module evidence — rework/churn (#60 `outcome_label` +
`fix_rounds`), superseded-decision density, gate-failure hotspots, concept-hub
pressure — then drops any candidate with **no cited evidence** and caps the rest
at a weekly budget ranked by evidence weight. #61 files ONLY what the gate
returns; each filed proposal embeds a machine-readable evidence block. See
[steering.md](steering.md).

## Why not…

- **…let /wrap do everything?** Concepts-at-creation is house doctrine —
  deferred enrichment is the anti-pattern — and by wrap time the per-issue
  gate/fix-round detail has left the context window. The loop writes the
  trajectory while it still knows it; wrap keeps the synthesis job.
- **…a JSONL run log in the repo?** Parallel PRs would merge-conflict on
  it, and it duplicates the tracker. The vault IS the loop's memory spine.
- **…auto-decisions per shipped PR?** Decisions are choices that constrain
  future work, with predicted outcomes worth judging. "Implemented #26 as
  specified" constrains nothing; forcing it into a decision devalues the
  RLVR substrate.
