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

Division of labor, honoring "don't store what another surface records":

| Surface | Owns |
|---|---|
| tracker comments | run history, claims, gate evidence |
| PR body | diff summary, gate table, smell report |
| **trajectory note** | how the work went + lessons — the reusable part |
| session note (/wrap) | cross-issue synthesis, decisions, insights |

**Mechanics.** After §2 Report, for each processed issue:

1. `issue_loop.py trajectory <N> --cwd <worktree> --gates-json <file>
   [--skills-json <file>] [--skill-centric] --fix-rounds R --outcome shipped
   --pr-url <url> --run-id <id>` assembles the deterministic half: files
   touched, commit count, gate verdicts, skill invocations, refs — emitted
   as a `weave_create`-shaped payload.
2. The orchestrator fills the judgment half: a ≤1K-char body (What / How it
   went / Lessons — lessons omitted when there are none; most runs are
   uneventful and their note is 5 lines of frontmatter + 2 sentences), and
   **concepts chosen at creation** from the ontology (`weave_concepts`
   first; `concept_hints` in the payload carries the issue's labels). Then
   one `weave_create(type=note, tags=[loop-run], session_id=<this session>,
   frontmatter=<payload>)`. MCP down → `weave add -f …` CLI fallback.
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

**What the notes buy.** Trajectory notes carry concepts, so they flow into
concept hubs, digests, and retrieval like any note: "what did the loop learn
about the indexer" is `weave_search(concepts=[…], tags=[loop-run])`. Grouped
by `run_id`/`issue` frontmatter they are the raw material for the
task-trajectory primitive (grouping repeated task instances across sessions)
without committing to its schema now.

**Deliberately not built** (until felt): a deterministic outcome judge
(`did the PR merge without human rework commits?` is computable from the PR
timeline — a natural future /dream phase-2 worker that would append
`prediction_history`-style evidence to trajectory notes); serving trajectory
notes back into implementer prompts (retrieval-time concern; SessionStart
context + decisions-for-file already cover the load-bearing part).

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
