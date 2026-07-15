---
name: issue-loop
description: "Drain the ready-for-agent frontier of the GitHub issue DAG: implement each unblocked issue in an isolated worktree, run the configured gate pipeline (tests/diff/acceptance/review), and open a draft PR per issue. Headless-safe."
argument-hint: "[issue-number] | --dag <issue> to work one DAG | --stacked | --max-issues <n> | --set key=value | nothing to drain the frontier"
disable-model-invocation: true
---

# Issue Loop — issue → gates → PR

Drain the runnable frontier of the issue DAG. Merged PRs close their issues
(`Closes #N`), which unblocks dependents; the tracker is the state machine.

`run_mode` picks between the two ways to run (see `loop.toml`):

- **pass** — one pass over the current frontier, up to `max_issues_per_run`.
  This is the *unrelated-work* mode: issues from **distinct DAG components**
  (see `component` in `plan` output) are independent by construction and may
  run in parallel. Stop when the pass is done.
- **exhaust** — *whole-DAG chasing*: after each shipped issue, re-run `plan`
  and keep going while the frontier is non-empty (still capped by
  `max_issues_per_run`). The frontier only widens as blockers close — i.e.
  as PRs get merged — so this mode is for working alongside a human who
  merges as you ship ("day shift merges, night shift chases"). When the
  frontier goes dry with blocked issues remaining, report what's awaiting
  merge and stop; never busy-wait.

Config: `docs/agents/loop.toml` (knobs + gate pipeline). Rail:
`scripts/issue_loop.py`. Semantics: `docs/agents/issue-loop.md`.
Issue-tracker conventions: `docs/agents/issue-tracker.md`; label vocabulary:
`docs/agents/triage-labels.md`.

## 0. Resolve config and plan

**Per-run overrides.** `loop.toml` holds the *defaults*; the arguments set
this run's *posture*. Translate sugar flags to rail overrides — `--stacked`
→ `--set delivery=stacked`, `--max-issues <n>` → `--set
max_issues_per_run=<n>` — and pass any explicit `--set [section.]key=value`
through verbatim. Collect the resulting `--set` flags once and append them
to **every** `issue_loop.py` invocation in this run (`config`, `plan`,
`claim`, `release`, `check`, `trajectory`), so the deterministic rail and
this orchestrator always see the same effective config. Never edit
`loop.toml` on the user's behalf to change one run. Gates are file-only by
design (the gate pipeline is a trust boundary, not a run-time posture) —
the rail rejects `--set` on unknown keys or gate config, and a nonsensical
combination (e.g. `--stacked` without `--dag`) is still an error per §1e.

```bash
python scripts/issue_loop.py config <set-flags>   # resolved knobs + gates
python scripts/issue_loop.py plan <set-flags>     # frontier / blocked / claimed
```

If the user passed an issue number as argument, the frontier is just that
issue (still verify via `plan` output that it is unblocked and unclaimed —
if not, say so and stop). If the user passed `--dag <N>`, scope every `plan`
call in this run with `--dag N` — the run works only that DAG component and
`run_mode` defaults to `exhaust` for it. If the frontier is empty, report
why (all blocked? all claimed? PRs awaiting human merge?) and stop.

Generate a run id: `loop-<YYYYMMDD>-<4 random hex>`.

## 0.5 Baseline probe (once per run)

Create the first implementer worktree, and **before any edits** run the
tests gate in it (pristine = origin/main state):

```bash
python scripts/issue_loop.py check --gate tests --cwd <worktree>
```

- **Green** → proceed. With `tdd.mode = auto` (or `always`), TDD is
  **enforced** in the implementer standing orders below.
- **Red** and `require_green_baseline = true` (default) → **stop before
  implementing anything.** Identify which open issue owns the failure
  (search the tracker for the failing test/subsystem) and report: "baseline
  red — fix #N first." In training mode, ask the user whether to proceed
  anyway; headless, refuse.
- **Red** and `require_green_baseline = false` → proceed degraded: the
  tests gate is scoped to the implementer's declared test targets instead
  of the whole suite, TDD downgrades from enforced to encouraged, and every
  PR body carries a `⚠ degraded-baseline` note naming the pre-existing
  failures.

## 1. Per issue — claim, implement, gate, ship

Process frontier issues **sequentially** by default. If `max_parallel > 1`
AND every picked issue has `parallel_safe: true`, you may dispatch
implementer subagents concurrently — each in its own worktree — but run at
most `max_parallel` at once and **never two issues from the same DAG
`component`** (plan output computes components deterministically; two open
issues sharing a component are one DAG and must be chased sequentially,
whatever their labels say).

For each issue:

### 1a. Claim (control-plane visibility)

```bash
python scripts/issue_loop.py claim <N> --run-id <run-id>
```

### 1b. Implement

Read the issue: `gh issue view <N> --comments`. Then dispatch an
**implementer subagent** with worktree isolation (Agent tool,
`isolation: "worktree"`). Its prompt must contain, verbatim: the issue body,
the acceptance criteria, the branch name (`<branch_prefix><N>`), and these
standing orders:

- Read `ARCHITECTURE.md` §-relevant parts and check prior decisions for every
  file you touch (`weave_graph(file_path=…, filter='decisions_for_file')`;
  fall back to `weave decisions-for-file` CLI if MCP is absent). Do not
  re-litigate a settled decision — surface conflicts instead.
- TDD per the probe (§0.5): when enforced, for each acceptance criterion
  write the failing test FIRST, watch it fail, then implement to green.
  The cycle is **red → green only** — refactoring belongs to the review
  stage's fix rounds, not the TDD cycle. The issue's "Slices" checklist is
  your plan. When degraded, still add tests for your own slice, but the
  whole-suite guarantee is off.
- **Test at seams.** Test only at the seams the issue names (its acceptance
  criteria / named interfaces). If the issue names none, choose the seams
  yourself and declare the choice in your return payload so it lands in the
  PR body — never scatter tests across internals.
- **No tautological tests.** Expected values must come from an independent
  source of truth (the issue's criteria, a hand-computed value, a fixture) —
  never recomputed the same way the code under test computes them.
- Commit in slice-sized increments on branch `<branch_prefix><N>`.
- Do NOT push, do NOT open a PR, do NOT close or label anything — the
  orchestrator owns the control plane.
- Return: worktree path, branch, files touched, test commands run, and any
  acceptance criterion you believe is NOT yet met (honesty over green-washing).

### 1c. Gate pipeline

Run the configured gates **in order**, inside the implementer's worktree.

- `kind: command` / `kind: diff` — deterministic, via the rail:
  ```bash
  python scripts/issue_loop.py check --gate <id> --cwd <worktree> --base-ref origin/main
  ```
- `kind: acceptance` — dispatch a **fresh judge subagent** (no implementation
  context). Give it: the issue's acceptance criteria, `git diff
  origin/main...HEAD` from the worktree, and the test output. It returns one
  verdict per criterion (`met` / `not-met` + one-line evidence). The gate
  passes per its `threshold` (`all` or `majority`).
- `kind: review` — dispatch a **fresh reviewer subagent** (code-reviewer
  type) on the diff. It returns findings with severities
  (critical/major/minor/nit). The gate fails if any finding's severity is in
  `block_on`. With `smells_baseline = true`, the reviewer also checks the
  Fowler smell baseline (mysterious name, duplicated code, feature envy,
  data clumps, primitive obsession, repeated switches, shotgun surgery,
  divergent change, speculative generality, message chains, middle man,
  refused bequest) — smells are **judgement calls reported in the PR body,
  never gate-failing**, and a documented repo standard overrides the
  baseline.

**On a required-gate failure:** feed the evidence (gate id, summary, detail,
per-criterion verdicts, review findings) back to the implementer subagent
(SendMessage to the same agent — it keeps its context) for a fix round.
Re-run the pipeline **from the first failed gate**. After `max_fix_rounds`
exhausted:

```bash
python scripts/issue_loop.py release <N>
gh issue edit <N> --remove-label ready-for-agent --add-label <on_gate_failure>
gh issue comment <N> --body "<gate evidence table + what was attempted>"
```

Then continue with the next frontier issue — one stuck issue must not stall
the loop.

### 1d. Ship

All required gates green. If `training_mode = true`, STOP here for this
issue and present the gate evidence table to the user; only push/PR after
approval (headless runs with training_mode on: leave the branch committed in
the worktree, comment the evidence + worktree path on the issue, and report —
do not push). Otherwise (or after approval):

```bash
git push -u origin <branch_prefix><N>
gh pr create --draft --title "<issue title> (#<N>)" --body "<body>"
gh issue comment <N> --body "🤖 issue-loop run <run-id>: PR <url> opened. <gate table>"
```

PR body must contain: `Closes #<N>`, a summary of the change, the gate
evidence table (gate | verdict | summary), and end with the standard
Claude Code attribution line. Do not remove the `ready-for-agent` label —
the issue closes on merge. Release is implicit: the claim (the assignee in
`claim_mode = assign`, the label otherwise) stays until merge closes the
issue — a claimed+closed issue is inert; if the PR is rejected, a human
unassigns / unlabels to re-queue.

In `run_mode = exhaust`: after shipping, re-run `plan`. If new frontier
issues appeared (a blocker got merged meanwhile), continue with them until
the per-run cap; otherwise report and stop.

### 1e. Stacked delivery (`delivery = stacked`)

One larger piece of work, no intermittent PRs. Requires a `--dag <N>` scope
and is sequential by definition (`max_parallel` is ignored). Differences
from the flow above — everything else (claim, implementer standing orders,
gate pipeline, fix rounds, failure routing) is identical:

- **One branch, one worktree.** `loop/dag-<N>`, created once from
  origin/main. Each issue's implementer subagent is FRESH (new context per
  issue) but works in this same worktree, stacking commits on the previous
  slices. Record the branch tip sha before each issue starts.
- **Blockers advance in-branch, not by merge.** After an issue's slices
  pass all gates, add it to the done-list and re-plan with
  `plan --dag <N> --assume-done <done-list>` — its dependents become
  workable immediately. No merge-waits mid-DAG.
- **Per-issue gates, scoped diffs.** Run `check --gate diff-guard
  --base-ref <tip-before-this-issue>` so diff limits apply per slice, not
  cumulatively; the tests gate always runs on the whole branch (earlier
  slices must stay green — that IS the stacking guarantee). The acceptance
  judge sees the per-issue diff (`git diff <tip-before>...HEAD`).
- **Tracker visibility without PRs.** After each issue passes:
  `gh issue comment <N> --body "🤖 issue-loop run <id>: slice landed on
  loop/dag-<root> at <sha> — PR at end of run. <gate table>"`. Do NOT
  close the issue; do NOT open a PR yet.
- **One PR at the end** (DAG exhausted, cap hit, or an issue routed to
  human): push the branch and open a single draft PR whose body carries
  `Closes #A` lines for every completed issue, the per-issue gate tables,
  and — if some of the DAG remains — which issues are NOT included and
  why. `training_mode` pauses once, here, instead of per issue.
- **A failed issue doesn't poison the stack.** If an issue exhausts its fix
  rounds, reset the branch to the last good tip (`git reset --hard
  <tip-before-this-issue>`), route the issue to human as usual, and stop
  extending this DAG (dependents of the failed issue are blocked anyway;
  independent siblings within the DAG may continue). Ship the PR with what
  completed.

## 2. Report

Per issue: number, outcome (PR opened / awaiting approval / routed-to-human),
gate results, fix rounds used. Plus: frontier remaining, issues newly
blocked-on-human. If nothing was shippable, say what the human must do to
unblock the DAG (usually: merge open loop PRs).

## 3. Feed the vault — PROPOSAL, do not execute until accepted

Design: `docs/agents/issue-loop-memory.md`. Once accepted: for each
processed issue, assemble the deterministic half —

```bash
python scripts/issue_loop.py trajectory <N> --cwd <worktree> \
  --gates-json <results-file> --fix-rounds <R> --outcome <o> \
  --pr-url <url> --run-id <run-id>
```

— then fill the judgment half and write ONE note: body ≤1K chars
(What / How it went / Lessons; omit Lessons when there are none), concepts
chosen from the ontology (`weave_concepts` first; the payload's
`concept_hints` are raw material, not concepts), and
`weave_create(type=note, tags=[loop-run], session_id=<this session>,
frontmatter=<payload frontmatter>)`. If MCP is down, fall back to
`weave add -f …`. Do not duplicate gate evidence or run history — the
tracker and PR own those.
