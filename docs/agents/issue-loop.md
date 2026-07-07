# The issue-to-PR loop

The first fully engineered loop in this repo: issues labeled
`ready-for-agent` flow through implement → gates → draft PR without
hand-prompting. Day shift plans the backlog (`/grill-with-docs` → `/to-prd` →
`/to-issues`); this loop is the night shift.

Surfaces:

- **`scripts/issue_loop.py`** — deterministic rail (stdlib-only). DAG
  snapshot, frontier computation, claim/release, command+diff gates.
- **`docs/agents/loop.toml`** — every tunable: run caps, fix rounds,
  training mode, label names, and the gate pipeline.
- **`/issue-loop`** (`docs/agents/issue-loop.command.md`) — the orchestrator:
  dispatches implementer/judge/reviewer subagents, owns all control-plane
  writes. Dev tooling, not a product skill — it deliberately does NOT live in
  the root `commands/` dir (which ships with the plugin). Install on a dev
  machine with the repo's standard untracked-symlink pattern:

  ```bash
  ln -s ../../docs/agents/issue-loop.command.md .claude/commands/issue-loop.md
  ```

## How the issue DAG becomes a script

**The tracker is the DAG.** `/to-issues` serializes blocking edges into
issue bodies — either the pipe-header form (`Track: … | Wave: 2 |
Blocked-by: #16 | Parallel-safe: yes | Epic: #11`) or the template's
`## Blocked by` section. Nothing else stores the graph; there is no plan
file to rot.

**The script computes only the frontier.** `issue_loop.py plan` re-reads all
issues each run and partitions the `ready-for-agent` set into:

- **frontier** — open, unclaimed, every blocker CLOSED → runnable now;
- **blocked** — some blocker still open (listed);
- **claimed** — carries the claim label (another run, or a stale crash —
  visible, so trivially auditable).

No full topological sort is ever needed. Each PR body says `Closes #N`, so a
human merging a PR closes its issue, which moves the next rank of the DAG
into the frontier for the next run. GitHub's own issue state machine *is*
the DAG executor; the loop is stateless between runs (the Ralph principle:
static prompt, evolving environment — here the environment is the tracker).

**Parallelism = frontier width.** `Wave:` orders the frontier;
`Parallel-safe:` and disjoint `track:` labels gate concurrent dispatch
(each implementer gets its own git worktree). This is Pocock's Sandcastle
planner made deterministic: he uses an LLM to pick parallelizable issues,
but since `/to-issues` already encodes the edges, scheduling is plain graph
math in Python — LLM judgment is reserved for implementation and evaluation,
never for scheduling.

## How success is defined: the gate pipeline

Evaluation is an **ordered list of typed gates** in `loop.toml`. The closed
set of *kinds* lives in code; the open set of *instances* is pure config —
adding another `command` gate (a linter, a contract test, a benchmark
threshold) touches no code.

| kind | judged by | what it checks |
|---|---|---|
| `diff` | script (deterministic) | forbidden paths, max changed lines |
| `command` | script (deterministic) | any shell command; pass = exit 0 |
| `acceptance` | fresh LLM judge | the issue's own acceptance criteria, per-criterion, `threshold = all\|majority` |
| `review` | fresh LLM reviewer | code-review findings vs `block_on` severities |

Design rules baked in:

- **Goal and verification are inseparable** — an issue is only
  `ready-for-agent` if its acceptance criteria are checkable, and the
  acceptance gate scores exactly those criteria, not a generic "looks good".
- **Fresh context for judgment.** Reviewing in the implementer's session
  happens in the dumb zone; the acceptance judge and reviewer see only the
  issue, the diff, and the evidence.
- **Fix loop with a floor.** implement → gates → feed failures back →
  fix, up to `max_fix_rounds`; exhaustion routes the issue to
  `ready-for-human` with the full evidence trail instead of stalling the
  loop or green-washing.
- **Everything visible.** Claim, evidence, PR link — all issue comments.
  The tracker comment stream is the loop's memory and audit log.
- **Training mode.** `training_mode = true` stops before push/PR and
  presents the gate table for approval. Flip it off once a few runs have
  earned trust; the guardrail gates (`diff`, caps) stay.

## The TDD contingency

TDD in the loop is **contingent on the baseline, not assumed**. Once per
run, before any edits, the orchestrator runs the tests gate in the pristine
implementer worktree — a deterministic baseline probe of origin/main.

- **Green baseline + `tdd.mode = auto`** → red-green-refactor is *enforced*
  in the implementer's standing orders: each acceptance criterion gets its
  failing test before its implementation. TDD only disciplines an agent when
  a green suite makes "new red" attributable to the new work.
- **Red baseline** → the whole-suite tests gate is unattributable, so by
  default (`require_green_baseline = true`) the loop refuses to implement
  and names the issue that owns the failure — fixing the baseline *is* the
  frontier. Opting out (`require_green_baseline = false`) degrades the tests
  gate to the issue's own declared test targets, downgrades TDD from
  enforced to encouraged, and stamps PRs with a `degraded-baseline` note.
- `tdd.mode = always | never` overrides the probe for repos where the
  answer is known.

## Extension points

- **New deterministic check** → add a `[[gates]]` entry (config-only).
- **New gate kind** (e.g. a benchmark-vs-baseline judge, a docs-drift
  checker) → one dispatch branch in `issue_loop.py` (deterministic) or one
  subsection in the `/issue-loop` command (LLM-judged).
- **Per-track policy** (e.g. stricter review on `track:B-core`) → gates grow
  an optional `only_tracks` / `skip_tracks` filter; deliberately not built
  until a real need shows up.
- **Scheduling** — the command is headless-safe; wire it to `/loop`, a cron,
  or a Routine once training mode has been retired. Per-run caps
  (`max_issues_per_run`) bound token burn regardless of trigger.

## Origin

Synthesized from three sources in the vault: Matt Pocock's AI Engineer
workshop (`src-2397c3aa` — DAG-of-issues Kanban, vertical slices,
Sandcastle, fresh-context review), Owain Lewis's agent-loops guide
(`src-82540a15` — control-plane visibility, risk labels, per-run caps), and
Austin Marchese's loop-engineering method (`src-e07b9ebd` — goal+verification
pairing, training mode, skills-before-loops).
