---
name: issue-loop
description: "Drain the ready-for-agent frontier of the GitHub issue DAG: implement each unblocked issue in an isolated worktree, run the configured gate pipeline (diff/tests/acceptance/review/simplify), and open a draft PR per issue. Headless-safe."
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

**Prime from prior trajectories (claim-time).** Before spawning the
implementer, fetch the reusable half of prior similar runs — this is the native
`bd prime`: the insight notes prior trajectories link via `builds_on`, for work
similar to this issue.

```bash
python scripts/issue_loop.py prime <N> --run-id <run-id> \
  --concepts "<2-3 ontology terms>" --query "<the issue's title (+ body)>" \
  [--decisions "<comma-separated note ids>"] --vault <vault-root> \
  [--buffer <weave_dir>/buffer/<this-session-id>.jsonl] <set-flags>
```

**You resolve the three signals; the rail fuses them.**

1. **`--concepts` — ontology terms, never GitHub labels.** The write side tags
   trajectories with concepts from `ontology.yaml` (the strict gate shunts
   everything else to `proposed_concepts`), so labels like `enhancement` or
   `track:E-devloop` match zero notes. You hold `weave_concepts` — map the
   issue to 2–3 ontology terms at claim time. `--labels` still works and still
   defaults `--concepts` to the label set, but on its own that join is dead —
   so a labels-only call with no `--query` comes back with a warning stamped in
   the payload's `note` instead of a benign-looking empty match. If you see
   that note, the run was effectively unprimed: fix the call, don't shrug.
2. **`--query` — the issue's own text.** Title, or title + body. This is the
   full-text leg; it is what makes priming land when your concept guess misses.
3. **`--decisions` — file-anchored ids, resolved by you at claim time.** Walk a
   granularity ladder and stop at the first rung that returns anything:
   files named in the issue body → `weave_graph(file_path=…,
   filter='decisions_for_file')`; nothing named or nothing found → the same
   walk for the module/dir those files live in; still nothing → let the
   concept+`--query` fusion above carry the retrieval alone.

The rail reads the derived index read-only, retrieves `[loop-run]` notes by
concept match and full-text match fused with RRF, weights them by outcome, and
emits JSON: `block` (markdown to splice), `primed`, `holdout`, `served` (the
note ids surfaced — insight bodies plus the decision ids you passed). **Splice
`block` verbatim into the implementer prompt, adjacent to the
`decisions_for_file` standing order below** — it is the same class of context
(prior decisions for touched files + prior lessons for similar work). When
`block` is empty (a deliberate holdout — `prime_holdout` samples one run in N
in expectation to run unprimed for the served-context regression — or simply no
matching trajectories), splice nothing and dispatch unchanged; the loop runs
identically. Record `primed` and `served` for §3. When you pass `--buffer` (the
loop session's buffer JSONL), the rail also logs the served ids as a
`loop_prime` event that the indexer projects to
`context_served(source='loop-prime')`, making served context recoverable per
run from the index. Add `--dry-run` to see what a call would serve without
logging it as served (payload prints, buffer write suppressed).

**Dispatch blocks (issue #89) — write-time simplification pressure.** When
`dispatch.persona` is on (loop.toml `[dispatch] persona = true`, the default),
splice two blocks into the implementer prompt, adjacent to the prime block:

1. **The ponytail persona** — the body of the **vendored**
   `docs/agents/ponytail-persona.md` (everything below its provenance header).
   Read that file and splice its text; never duplicate it here — the
   vendored file is the single source.
2. **The epic's north-star block, verbatim.** When the issue belongs to an
   epic that carries a north-star block, splice that epic's block; the current
   one (epic #88) is:

   <!-- verbatim from epic #88 — three lines, kept unwrapped on purpose -->
   > **Goal:** fewer POCs; deep but interpretable modules with boundaries at likely redesign points; conceptual fidelity — retrieval, triaging, and trajectory composition are different concerns and never share a bucket; generic utils (parsing, coercion, path matching) are never defined alongside key logic.
   > **Anti-goals:** no new half-mechanisms (a capability ships with its consumer or not at all); no contract asserted in prose without an enforcing seam; no behavior change during the mechanical package split.
   > **Provenance:** distilled from the owner's 18 review comments on PR #86 and the loop-v3 plan (session 2026-07-31).

When `dispatch.persona` is off (`--set dispatch.persona=false`), splice
neither block anywhere — every dispatch prompt (implementer, fix round,
reviewer, acceptance judge) is **byte-identical** to the pre-#89 loop.

Read the issue: `gh issue view <N> --comments`. Then dispatch an
**implementer subagent** with worktree isolation (Agent tool,
`isolation: "worktree"`). Its prompt must contain, verbatim: the issue body,
the acceptance criteria, the branch name (`<branch_prefix><N>`), the spliced
prime block (when non-empty), the two dispatch blocks above (when
`dispatch.persona` is on), and these standing orders:

- Read `ARCHITECTURE.md` §-relevant parts and check prior decisions for every
  file you touch (`weave_graph(file_path=…, filter='decisions_for_file')`;
  fall back to `weave decisions --file <path>` CLI if MCP is absent). Do not
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

**The gate split — which plane runs which kind.** `command` and `diff` gates
**execute in the rail**: Python runs the shell command / the diff arithmetic
and returns the verdict. `acceptance`, `review`, and `simplify` are **never
executed by the rail** — *this* orchestrator dispatches a fresh subagent. The
rail's `check` runs those two kinds and refuses every other kind — judgment
kind or typo alike — with `gate kind '<k>' is LLM-judged — run it from the
/issue-loop command, not the script`. Protocol detail — the two registries, the
shared `GateResult` shape, execute-vs-validate:
`docs/agents/devloop-boundaries.md` §3.

**A judgment gate's return is schema-checked before it becomes a verdict.**
Each judgment kind has a JSON schema (in its bullet below). For `acceptance`
and `review`, ask the subagent for exactly that object as its return. For
`simplify` the **vendored** skill owns its output format (a prose delete-list),
so you condense its delete-list + tally into the envelope yourself — the same
one §3's `trace` stores. Either way, write the JSON to a file and hand it to
the rail before acting on it:

```bash
python scripts/issue_loop.py validate --gate <id> --return-json <return-file>
```

The rail emits the same `GateResult` the deterministic gates emit, plus
`reasons`, and exits:

- `0` — schema-valid and the gate **passed**.
- `1` — schema-valid and the gate **failed**: a real verdict, so run a fix
  round per the failure flow below.
- `2` — **schema-rejected**: `reasons` names each offending field path and
  value (`criteria[1].verdict: 'probably' is not one of met | not-met`).
  SendMessage those reasons back to the same subagent and **re-ask** — never
  hand-fix its return, never read a verdict out of a rejected one, and never
  pass it on to `--gates-json` / `--trace-json`. If the re-ask still comes back
  rejected, treat the gate as failed and route to human with the reasons as
  the evidence.

- `kind: command` / `kind: diff` — deterministic, via the rail:
  ```bash
  python scripts/issue_loop.py check --gate <id> --cwd <worktree> --base-ref origin/main
  ```
- `kind: acceptance` — dispatch a **fresh judge subagent** (no implementation
  context). Give it: the issue's acceptance criteria, `git diff
  origin/main...HEAD` from the worktree, the test output, and — when
  `dispatch.persona` is on — the **north-star block only** (§1b; it judges
  against the goal, not the persona). It returns one verdict per criterion,
  as `{"criteria": [{"id": "AC1", "verdict": "met", "evidence": "<one line>"}]}`
  — `verdict` is `"met"` or `"not-met"`, `evidence` is never blank. `validate`
  applies the gate's `threshold` (`all` or `majority`) to compute the verdict.
- `kind: review` — dispatch a **fresh reviewer subagent** (code-reviewer
  type) on the diff, with — when `dispatch.persona` is on — the
  **north-star block only** (§1b). It returns
  `{"findings": [{"severity": "minor", "finding": "<prose>"}]}` — `severity` is
  one of `"critical"` / `"major"` / `"minor"` / `"nit"`, and an empty list is a
  clean review. The gate fails if any finding's severity is in
  `block_on`. With `smells_baseline = true`, the reviewer also checks the
  Fowler smell baseline (mysterious name, duplicated code, feature envy,
  data clumps, primitive obsession, repeated switches, shotgun surgery,
  divergent change, speculative generality, message chains, middle man,
  refused bequest) — smells are **judgement calls reported in the PR body,
  never gate-failing**, and a documented repo standard overrides the
  baseline.
- `kind: simplify` — the over-engineering trim. Runs **last, only after every
  required gate is green**, and is safe by construction: it can only *shrink*
  the verified diff and must preserve verified behavior. See the dedicated
  flow below. `required = false` — it can never fail the pipeline; its
  "failure" mode is a revert, not a block.

**The simplify stage (`kind: simplify`, after review).** Once review passes:

1. **Snapshot the tip.** `pre=$(git -C <worktree> rev-parse HEAD)`. In stacked
   mode this is per-slice — the snapshot is the tip *before* this slice's
   simplify, so a revert only unwinds the trim, never prior slices.
2. **Get the delete-list.** Dispatch a **fresh subagent** with the text of the
   **vendored** `docs/agents/ponytail-review.command.md` skill (host
   `/simplify` is the fallback if unavailable) and the slice diff —
   `git diff origin/main...HEAD`; in stacked mode
   `git diff <tip-before-this-issue>...HEAD`, per §1e's scoped-diffs bullet.
   It returns a delete-list (one line per cut) and a
   `net: -<N> lines possible` tally, in the vendored skill's own format.
   Condense that into the gate's envelope — `{"outcome": "applied",
   "lines_delta": -<N>, "cuts": [{"what": …, "why": …}], "kept": [{…}]}`, the
   same one §3's `trace` stores — and `validate` it. `outcome` is `"lean"` when
   the subagent said `Lean already. Ship.` (skip the rest — note "simplify:
   lean already" in the PR body and move on); `"applied"` when you apply the
   delete-list; `"reverted"` is step 5's terminal value after a red re-verify.
3. **Apply.** Apply the delete-list as a single commit on the branch.
4. **Re-verify.** Re-run the gates named in the gate's `rerun` key (`tests`,
   then `acceptance`) on the shrunk diff, in order — `tests` via the rail
   (`check --gate tests`), `acceptance` via a fresh judge subagent, same as §1c.
5. **Keep or revert.**
   - **Both green** → keep the shrink. Note the win in the PR body
     (`simplify: -<N> lines, tests+acceptance green`).
   - **Either red** → `git -C <worktree> reset --hard $pre` to discard the
     trim and ship the **pre-simplify** diff. Add the gate's `revert_note`
     (`⚠ simplify-reverted`) to the PR body with the failing gate named.

Because a red re-verify resets to `$pre`, simplify never blocks shipping and
never regresses behavior — worst case the shipped diff is exactly the
post-review diff. That is the whole point of running it last and non-required.

**On a required-gate failure:** feed the evidence (gate id, summary, detail,
per-criterion verdicts, review findings) back to the implementer subagent
(SendMessage to the same agent — it keeps its context) for a fix round.
When `dispatch.persona` is on, re-splice **both dispatch blocks** (§1b —
persona from the vendored file + north-star verbatim) into the fix-round
message.
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

**No stack-tip simplify here — a documented no-op.** Each pr-per-issue
branch holds one slice, so the per-slice simplify gate (§1c) already ran at
what IS the stack tip; the whole-branch pass exists only in stacked delivery
(§1e).

**Risk-lane triage — label what a human should look at.** Daily runs
outpace review, so after the PR is opened, classify it so a human reviews
only what matters. Assemble the shipped PR's signal set — you already hold
all of it — into a JSON file and run:

```bash
python scripts/issue_loop.py triage <N> --signals-json <signals-file>
```

Signals schema (you compute them per shipped PR). The three **safety-critical**
keys are REQUIRED and fail closed — an absent key or an unrecognized enum value
classifies **red** (naming the offending key/value), never green-eligible,
because you assemble these signals and enum drift (`high` / `partial`) is
realistic. The rest are optional and default benignly:

| key               | type      | required | meaning                                             |
| ----------------- | --------- | -------- | --------------------------------------------------- |
| `review_severity` | str       | **yes**  | worst review finding: `none`/`minor`/`major`/`critical` |
| `baseline_green`  | bool      | **yes**  | the tests gate was green on the pristine worktree   |
| `acceptance`      | str       | **yes**  | acceptance verdict: `met`/`uncertain`/`not-met`     |
| `fix_rounds`      | int       | no (→0)  | implement→gate→fix iterations (0 = first try)       |
| `diff_lines`      | int       | no (→0)  | total changed lines (the diff-guard gate's count)   |
| `files_touched`   | list[str] | no (→[]) | repo-relative paths the PR changed                  |
| `tests_touched`   | bool      | no (→F)  | the change carries test coverage                    |

The rail returns `{issue, lane, label, reasons}` — precedence red > yellow >
green, `reasons` lists every triggered rule. **You** apply the label via gh
(the rail only classifies):

```bash
gh issue edit <N> --add-label <label>   # or: gh pr edit <pr-url> --add-label
```

- **green** (`auto-merge-ok`) — first-try, small, test-covered, `<= minor`
  review, green baseline, no sensitive path. Only emitted when
  `triage.green_enabled = true` (ship default: **false** → a would-be-green
  PR is labeled `review-light` instead). Green is safe ONLY where GitHub
  branch protection + required CI actually guard the merge; enable it per the
  training-mode graduation, not before.
- **yellow** (`review-light`) — passed, but with fix rounds, a medium diff, a
  watched path, or no coverage signal: a human skims the trajectory note's
  "How it went".
- **red** (`ready-for-human`) — sensitive path (always, regardless of size),
  big diff, degraded baseline, `major`/`critical` review, or uncertain/not-met
  acceptance. This reuses the `on_gate_failure` label `ready-for-human`
  deliberately — same "human, please look" rung as a gate failure.

Thresholds and the sensitive-path list are `[triage]` knobs in `loop.toml`
(override per run with `--set triage.green_enabled=true` etc.), never
hardcoded.

**Teardown.** Once the PR is open the worktree has served its purpose —
the branch lives on origin and review happens from there. From the main
checkout: `git worktree remove <worktree>` (add `--force` when the only
dirt is regenerated lockfiles — that's churn, not work). A lingering
worktree pins its branch, and git then refuses every human attempt to
check the PR out (the VS Code PR extension fails with "error switching
to pull request"). The only flows that deliberately keep a worktree are
the evidence paths — `training_mode` headless holds and gate-failure
routing — and those must be listed in the run report (§2) so a human
knows to remove them after acting on the evidence.

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
  judge sees the per-issue diff (`git diff <tip-before>...HEAD`), and so does
  the per-slice simplify subagent (§1c step 2) — cross-slice trimming belongs
  to the stack-tip pass below, never to a slice's own gate.
- **Tracker visibility without PRs.** After each issue passes:
  `gh issue comment <N> --body "🤖 issue-loop run <id>: slice landed on
  loop/dag-<root> at <sha> — PR at end of run. <gate table>"`. Do NOT
  close the issue; do NOT open a PR yet.
- **Stack-tip simplify — whole-branch ponytail review before PR-open.** The
  per-slice simplify gate (§1c) sees one slice at a time and cannot see
  cross-slice redundancy (a later slice re-rolling an earlier slice's
  helper, hand-rolled retrieval a sibling already ships). So once the stack
  is final — DAG exhausted, cap hit, or an issue routed to human — run the
  §1c simplify flow ONCE more over the whole branch, before pushing
  anything. Same gate entry, same five steps, differing in exactly two
  inputs: the diff is the **cumulative merge-base diff**
  `git diff origin/main...HEAD` (the whole ponytail, not one slice), and the
  subagent also receives the **whole-file contents** of every touched file
  (`git diff --name-only origin/main...HEAD`, then read each) so it can
  judge duplication across slices. Keep-or-revert is unchanged and reuses
  the existing gate config: snapshot `pre=$(git rev-parse HEAD)`, apply the
  delete-list as one commit, re-run the gate's `rerun` list — `tests` on the
  whole branch via the rail, `acceptance` as one fresh judge over EVERY
  completed issue's criteria against the cumulative diff, judged per the
  gate's `threshold` same as §1c — and on any red `git reset --hard $pre` and
  add the gate's `revert_note` (`⚠ simplify-reverted`, suffixed
  `(stack-tip)`) to the PR body. A run whose slices **individually passed**
  simplify can still receive cuts here — that is the point of the pass.
  Record the result in the final completed issue's §3 trace under
  `stack_simplify` (same outcome/lines_delta/cuts/kept envelope as the
  per-slice `simplify` key), and note the win
  (`stack-tip simplify: -<N> lines, tests+acceptance green`) or the revert
  in the PR body.
- **One PR at the end** (DAG exhausted, cap hit, or an issue routed to
  human): push the branch and open a single draft PR whose body carries
  `Closes #A` lines for every completed issue, the per-issue gate tables,
  and — if some of the DAG remains — which issues are NOT included and
  why. `training_mode` pauses once, here, instead of per issue. After the
  PR is open, remove the `loop/dag-<N>` worktree (same teardown rule as
  §1d — the stacked worktrees were exactly the ones found pinning PR
  branches on 2026-07-21).
- **A failed issue doesn't poison the stack.** If an issue exhausts its fix
  rounds, reset the branch to the last good tip (`git reset --hard
  <tip-before-this-issue>`), route the issue to human as usual, and stop
  extending this DAG (dependents of the failed issue are blocked anyway;
  independent siblings within the DAG may continue). Ship the PR with what
  completed.

## 2. Report

Per issue: number, outcome (PR opened / awaiting approval / routed-to-human),
gate results, fix rounds used. Plus: frontier remaining, issues newly
blocked-on-human, and **every worktree left behind** (evidence paths only —
path + branch + why), so a human can `git worktree remove` them after
acting on the evidence. If nothing was shippable, say what the human must do to
unblock the DAG (usually: merge open loop PRs).

## 3. Feed the vault — write one trajectory note per processed issue

Owner-approved 2026-07-15 and enabled (design: `docs/agents/issue-loop-memory.md`).
Run unattended — do not gate on user approval. For each processed issue,
assemble the deterministic half —

```bash
python scripts/issue_loop.py trajectory <N> --cwd <worktree> \
  --gates-json <results-file> --skills-json <dispatch-log> [--skill-centric] \
  [--primed | --no-primed] [--served-json <served-ids-file>] \
  [--trace-json <trace-file>] \
  --fix-rounds <R> --outcome <o> --pr-url <url> --run-id <run-id>
```

Mirror the §1b prime verdict: pass `--primed` with `--served-json` (a JSON list
of the `served` ids the prime emitted) when this issue's implementer received
prime context, or `--no-primed` when it was a holdout (`primed: false`, no
served ids). Omitting both keeps the pre-#57 shape. This frontmatter mirror is
markdown-truth for the served-context regression — the trajectory's `outcome`
(and #60's outcome judge) regressed against `primed`/`served` separates
"context helped" from "easy issue".

`--skills-json` points at a JSON file you write from what the loop dispatched
for this issue: a list of `{id, role, outcome, fix_rounds_attributed}`, one
per stage skill you ran — the implementer subagent, the acceptance judge
(`kind: acceptance` gate), the reviewer (`kind: review` gate), and any future
stage (ponytail, tdd). `role` is the stage role, `outcome` is how that
invocation resolved (e.g. `shipped` / `met` / `not-met` / `passed`), and
`fix_rounds_attributed` is how many fix rounds that gate/skill caused (attribute
each round in §1c to the gate that triggered it; the total is `--fix-rounds`).
Omit `--skills-json` and the payload carries `skills: []`. Add `--skill-centric`
when the record is primarily about a skill invocation (SkillOpt raw material) —
it adds the `skill-invocation` tag so `weave_search(tags=[skill-invocation])`
returns skill-attributed records.

`--trace-json` (issue #85) points at a JSON file **you compose from the gate
agents' own reports** — no new model call, you already have these in context:
the reviewer's findings + reasoning, the simplify gate's cut/keep rationale (the
over-engineering description), the acceptance judge's per-criterion evidence and
any verdict flips, and the TDD red-confirmation. Condense them into the envelope

```json
{
  "rounds":     [{"gate": "review", "finding": "<prose>", "severity": "minor",
                  "disposition": "accepted", "fixed_by": "<prose>"}],
  "criteria":   [{"id": "AC1", "verdict": "met", "flipped_by_round": 1}],
  "simplify":   {"outcome": "applied", "lines_delta": -12,
                 "cuts": [{"what": "<prose>", "why": "<prose>"}],
                 "kept": [{"what": "<prose>", "why": "<prose>"}]},
  "stack_simplify": {"outcome": "applied", "lines_delta": -31,
                     "cuts": [{"what": "<prose>", "why": "<prose>"}],
                     "kept": [{"what": "<prose>", "why": "<prose>"}]},
  "edge_cases": ["<prose>"],
  "tdd":        {"red_confirmed": true}
}
```

The rail only accepts and shapes it (unknown keys dropped; a non-dict trace is
rejected). `stack_simplify` (issue #90) shares the `simplify` envelope and
records the §1e stack-tip pass: at most once per stacked run, on the **final
completed issue's** trajectory. The placement rule is deliberately unenforced
by the rail — which issue is final is orchestrator knowledge the rail never
holds. It lands under the single `trace` frontmatter key — the
machine-readable half of the tracker's gate evidence, not a second prose owner.
Counts (`lines_delta`, `flipped_by_round`) are filter/join keys, not signal.
Omit `--trace-json` for the pre-#85 shape.

**Mint portable lessons as insight notes, then link them (issue #85).** The
trajectory body is the run-causal register only (What / How it went) — there is
`no Lessons section`. The reusable wisdom a *future* run would apply is minted as
one or more separate **insight notes** at ship time (concepts at creation, from
the ontology — `weave_concepts` first), then linked from the trajectory via
`builds_on`. The register test that sorts every artifact:
`run-bound semantic trace` → the trajectory's `trace`; a `portable lesson` → an
`insight note`, linked; an enumerable fact → a `frontmatter key`. Prime v2 serves
those insight bodies by following the `builds_on` links, so a lesson written once
is reused verbatim.

Compose, per issue:

1. **Insight notes** for the portable lessons (skip when the run taught nothing
   reusable). The MCP `weave_create` schema accepts only
   `type/title/body/project/tags/frontmatter/session_id` — extra top-level
   kwargs are **silently dropped**, so `concepts` MUST be nested under
   `frontmatter=`:

   ```python
   weave_create(type=note, title="<portable lesson title>",
                body="<the reusable wisdom, prose>",
                session_id="<this run's session>",
                frontmatter={"concepts": ["<ontology-term>", "<ontology-term>"]})
   ```

   Capture each returned insight id.

2. **The trajectory note** — body ≤1K chars (What / How it went only), then one
   `weave_create` with the payload's frontmatter **plus** a `builds_on` list of
   the insight ids from step 1 (again nested under `frontmatter=`, same
   dropped-kwarg trap):

   ```python
   weave_create(type=note, tags=<payload tags>,
                session_id="<this run's session>",
                frontmatter={**<payload frontmatter>, "builds_on": [<insight ids>]})
   ```

   The payload's `tags` already carry `loop-run` (plus `skill-invocation` when
   `--skill-centric`). If MCP is down, fall back to `weave add -f …`.

Do not duplicate gate evidence or run history — the tracker and PR own those.
Optionally print the first run's composed notes as a sanity check; non-blocking.

## 4. Wrap coverage — do NOT run `/wrap` here

Headless loop runs are wrap-covered without an explicit run-end `/wrap`. The
`SessionStart` hook mints this run's session note (with a `source_session` UUID,
no `processed` flag); the nightly `/dream` phase-2 `dream-wrap-worker` catch-up
picks it up (`type: session`, not processed, recent, non-empty `events.jsonl`)
and synthesises + `weave wrap-finalize`s it. The deterministic per-issue content
is already in the §3 trajectory notes, so nothing is lost by wrap time.

Do not run `/wrap` or `weave wrap-finalize` from the loop: session synthesis and
**decision promotion** belong to the session-note owner, and letting the loop
mint decisions would break the single-owner rule. See
[`vault-issue-contract.md`](vault-issue-contract.md) for the full division of
labor and its contract test.
