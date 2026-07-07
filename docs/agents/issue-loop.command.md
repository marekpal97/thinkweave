---
name: issue-loop
description: "Drain the ready-for-agent frontier of the GitHub issue DAG: implement each unblocked issue in an isolated worktree, run the configured gate pipeline (tests/diff/acceptance/review), and open a draft PR per issue. Headless-safe."
argument-hint: "[issue-number] | nothing to drain the frontier"
disable-model-invocation: true
---

# Issue Loop — issue → gates → PR

Drain the runnable frontier of the issue DAG. Each run processes up to
`max_issues_per_run` unblocked issues; merged PRs close their issues
(`Closes #N`), which unblocks dependents for the *next* run. You never walk
the whole DAG — the tracker is the state machine.

Config: `docs/agents/loop.toml` (knobs + gate pipeline). Rail:
`scripts/issue_loop.py`. Semantics: `docs/agents/issue-loop.md`.
Issue-tracker conventions: `docs/agents/issue-tracker.md`; label vocabulary:
`docs/agents/triage-labels.md`.

## 0. Resolve config and plan

```bash
python scripts/issue_loop.py config          # resolved knobs + gates
python scripts/issue_loop.py plan            # frontier / blocked / claimed
```

If the user passed an issue number as argument, the frontier is just that
issue (still verify via `plan` output that it is unblocked and unclaimed —
if not, say so and stop). If the frontier is empty, report why (all blocked?
all claimed? PRs awaiting human merge?) and stop.

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
most `max_parallel` at once and never two issues that share a `track:` label.

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
  write the failing test FIRST, watch it fail, then implement to green
  (red-green-refactor; the issue's "Slices" checklist is your plan). When
  degraded, still add tests for your own slice, but the whole-suite
  guarantee is off.
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
  `block_on`.

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
the issue closes on merge. Release is implicit: the claim label stays until
merge closes the issue (a claimed+closed issue is inert; if the PR is
rejected, a human removes the label to re-queue).

## 2. Report

Per issue: number, outcome (PR opened / awaiting approval / routed-to-human),
gate results, fix rounds used. Plus: frontier remaining, issues newly
blocked-on-human. If nothing was shippable, say what the human must do to
unblock the DAG (usually: merge open loop PRs).
