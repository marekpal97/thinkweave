# The issue-to-PR loop

The first fully engineered loop in this repo: issues labeled
`ready-for-agent` flow through implement → gates → draft PR without
hand-prompting. Day shift plans the backlog (`/grill-with-docs` → `/to-spec`
→ `/to-tickets`, or `/wayfinder` for foggy multi-session efforts); this loop
is the night shift.

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

**The tracker is the DAG.** Since Pocock skills v1.1.0, `/to-tickets` and
`/wayfinder` publish blocking as **GitHub-native issue dependencies** — the
canonical, UI-visible representation — with body text as fallback: the
pipe-header form (`Track: … | Wave: 2 | Blocked-by: #16 | Parallel-safe:
yes | Epic: #11`) or the template's `## Blocked by` section. The rail gates
on the **union** of native and body edges, so old and new corpora both work.
Nothing else stores the graph; there is no plan file to rot. GitHub even
maintains the live gate for us: `issue_dependencies_summary.blocked_by`
counts open blockers natively.

**The script computes only the frontier.** `issue_loop.py plan` re-reads all
issues each run and partitions the `ready-for-agent` set into:

- **frontier** — open, unclaimed, every blocker CLOSED → runnable now;
- **blocked** — some blocker still open (listed);
- **claimed** — has an assignee (`claim_mode = assign`, the wayfinder
  convention: the assignee IS the claim) or the legacy claim label. Either
  way visible in the tracker UI, so trivially auditable.

No full topological sort is ever needed. Each PR body says `Closes #N`, so a
human merging a PR closes its issue, which moves the next rank of the DAG
into the frontier for the next run. GitHub's own issue state machine *is*
the DAG executor; the loop is stateless between runs (the Ralph principle:
static prompt, evolving environment — here the environment is the tracker).

**Two loops, one distinction: unrelated work vs one DAG.** `plan` computes
the **weakly-connected components** of the open-issue graph (component id =
smallest issue number in it). Two open issues in the same component belong
to one DAG — order matters, so they are chased *sequentially*; issues in
distinct components are unrelated by construction and safe to run in
*parallel* (each implementer in its own worktree, `Parallel-safe:` hint
still respected). `run_mode` names the two postures:

- **`pass`** — one pass over the frontier, breadth across components. The
  "tackle unrelated issues in parallel" loop.
- **`exhaust`** — re-plan after every shipped issue and keep chasing while
  the frontier is non-empty. The "keep working a whole DAG" loop. Honest
  physics under `pr-per-issue` delivery: blockers close on *merge*, so
  depth advances only as fast as a human merges — exhaust mode cooperates
  with concurrent merging rather than pretending the DAG can be finished
  unattended.

Scoping: `plan --dag <N>` (surfaced as `/issue-loop --dag N`) restricts a
run to the DAG component containing issue N — "work this epic, ignore the
rest of the backlog."

**Delivery is orthogonal to run_mode.** `delivery = pr-per-issue` (default)
ships every issue as its own branch + draft PR — small-PR review discipline,
merge-gated DAG advancement. `delivery = stacked` removes the mid-DAG
merge-waits for a `--dag`-scoped run: all slices land as stacked commits on
one branch, dependents unblock via `plan --assume-done <completed>` instead
of waiting for merges, and a single draft PR closes the whole set at the
end. The trade is explicit: one review of a bigger diff instead of many
small ones, and a review change to an early slice means reworking the stack
above it — pick it when the DAG is coherent enough that you'd review it as
one unit anyway. In-branch "done" is provisional (`assume-done`), never
written to the tracker; the tracker's truth still changes only when the PR
merges and closes the issues.

This is Pocock's Sandcastle planner made deterministic: he uses an LLM to
pick parallelizable issues, but since `/to-tickets` already encodes the
edges, scheduling is plain graph math in Python — LLM judgment is reserved
for implementation and evaluation, never for scheduling. A wide refactor
ticketed as expand–contract arrives here as an ordinary linear blocked
chain and needs nothing special from the loop.

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

- **Green baseline + `tdd.mode = auto`** → red→green TDD is *enforced*
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

## v1.1.0 alignment

Updated for Pocock skills v1.1.0 (2026-07-08): native blocking edges +
sub-issues read unioned with body text; claim by assignment; TDD narrowed to
red→green with refactoring owned by the review stage; seam-scoped tests and
the tautological-test anti-pattern in implementer standing orders; the
code-review Fowler smell baseline as a judgement-only reviewer axis.
Wayfinder needs no integration: its implementation tickets speak the same
wire protocol (native edges, assignee claims), so they land on the same
frontier unaided.

## Origin

Synthesized from three sources in the vault: Matt Pocock's AI Engineer
workshop (`src-2397c3aa` — DAG-of-issues Kanban, vertical slices,
Sandcastle, fresh-context review), Owain Lewis's agent-loops guide
(`src-82540a15` — control-plane visibility, risk labels, per-run caps), and
Austin Marchese's loop-engineering method (`src-e07b9ebd` — goal+verification
pairing, training mode, skills-before-loops).
