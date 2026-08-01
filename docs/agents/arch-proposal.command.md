---
name: arch-proposal
description: "The slow self-improvement loop: run the deepening axis (/improve-codebase-architecture) and the simplification axis (vendored ponytail-audit), gate the candidates against the #62 evidence substrate, and file ranked draft arch-proposal issues — never open PRs. Read-only + issue-filing only. Headless-safe."
argument-hint: "nothing — reads the whole repo, files gated arch-proposal issues (no PRs, no code changes)"
disable-model-invocation: true
---

# Arch-Proposal — the slow loop (propose, never apply)

The **slow** half of the self-improvement machine. Where the fast loop
(`/issue-loop`, #44) drains `ready-for-agent` issues into PRs, this loop does
the judgment-heavy work: it reads the whole codebase along two axes, and emits
**ranked draft GitHub issues** for a human to greenlight. It **files issues, it
never opens PRs and never modifies code.** Accepted proposals become the fast
loop's fuel after a human relabels them `ready-for-agent`.

This is deliberately read-only + issue-filing only: architectural change is
judgment-heavy, so the loop proposes and a human disposes. Every filed issue
must cite evidence from the self-improvement substrate (the #62 gate) — the loop
never invents work.

Config: `[steering]` in the vault `config.toml` (budget + signal weights).
Gate: `weave steering gate` (rail: `operations/steering.py`; contract:
`docs/agents/steering.md`). Fast-loop counterpart: `docs/agents/issue-loop.md`.

## Contract in one line

**Load the project snapshot + prior decisions → run both axes → convert
findings to gate candidates → `weave steering gate` → file ONLY the gate's
`filed` list as `arch-proposal` issues.** No PR is ever opened; no file is
ever edited.

## 0. Ensure the label exists (idempotent)

The output label must exist on a fresh tracker. Create it idempotently at the
top of every run (safe to re-run — `--force` updates in place instead of
erroring on a second run):

```bash
gh label create arch-proposal --force \
  --color 5319e7 --description "Draft architectural proposal from the slow loop; human greenlights → ready-for-agent"
```

`arch-proposal` is documented in `docs/agents/triage-labels.md` alongside the
canonical triage roles.

## 1. Load the ground truth first (anti-re-proposal)

Before proposing anything, load what is already true so the loop does not
re-propose against settled work.

**The input-context surface is `weave_project_snapshot`** — one
thinkweave-native call (its `state` section is the project's `STATE.md`
verbatim), not a hand-curated doc list.

1. **Project snapshot.** One call, at the top of the run:
   - MCP available: `weave_project_snapshot(project="thinkweave",
     sections=["state", "backlog", "decisions", "probes"])`.
   - MCP absent (headless degrade): the CLI parity command —
     `weave project-snapshot thinkweave --sections state,backlog,decisions,probes`.
2. **Prior decisions, per candidate area.** The snapshot's `decisions` section
   is the *recent* window; a settled decision can be older than it. So for each
   area you are about to consider, query prior decisions directly and build a
   **skip-list of already-decided-against directions**:
   - MCP available: `weave_graph(id=<file>, filter='decisions_for_file')` per
     candidate file, and `weave_search(query=<area>, type=[decision])` for the
     area's decisions. (weave_graph read-only is allowed.)
   - MCP absent (headless degrade): the CLI fallbacks —
     `weave decisions --file <path>` and `weave search --type decision "<area>"`.
   Vault decisions ARE code-facing ADRs (`file_path` + `predicted_outcome` +
   supersession). If a direction is already covered by a live decision, **skip
   it** and note the skip — do not re-litigate a settled decision. Only surface
   a conflict if you believe the decision itself should be revisited (and even
   then, as a proposal, not an edit).

Anything on the skip-list is dropped before the gate ever sees it.

**Architectural invariants.** `ARCHITECTURE.md` remains the authority on
*invariants*, consulted per-area by the axes (§2) rather than read end-to-end
here. A proposal contradicting a stated invariant is out of scope.

## 2. Run the two axes (read-only)

Two complementary lenses. Run each in a **fresh subagent** so their contexts
don't cross-contaminate; neither may edit code.

- **Deepening axis — `/improve-codebase-architecture`.** The installed Matt
  Pocock skill (machine-global at `~/.claude/skills/improve-codebase-architecture/`).
  It finds where modules are too shallow and proposes deeper interfaces /
  better seams. Feed it ARCHITECTURE.md context; collect its proposals. **If the
  skill is not installed on this machine** (the directory is absent), skip the
  deepening axis, note `deepening axis skipped: improve-codebase-architecture
  not installed` in the run output (§6), and proceed with the simplification
  axis alone — a missing global skill degrades the run, it never fails it.
- **Simplification axis — vendored `ponytail-audit`.** Dispatch a fresh
  subagent with the text of the **vendored**
  `docs/agents/ponytail-audit.command.md` skill (whole-repo over-engineering
  audit). It returns a ranked delete/stdlib/native/yagni/shrink list, one line
  per finding with a `[path]` and a `net: -<N> lines, -<M> deps possible` tally.
  (Interactive `/ponytail-audit` is the same skill once symlinked — see §5.)

Both axes are **read-only reports**. Nothing is applied.

## 3. Convert findings to gate candidates

Each surviving finding (skip-list already removed) becomes one **candidate** in
the #62 gate's schema — a `{module | paths, rationale, title?}` dict:

| Candidate field | Filled from |
| --------------- | ----------- |
| `paths` (or `module`) | the repo path(s) the finding touches — ponytail-audit's `[path]` annotation, or the module improve-arch is deepening. `paths` for a multi-file proposal, `module` for a single path. |
| `rationale`     | the proposal in prose: what to change and why (the axis's finding text). Becomes the issue body's lead paragraph. |
| `title`         | a short issue title (`<verb> <what> in <area>`). |

Write the candidate list to a JSON file (a bare list, or `{"candidates": [...]}`):

```json
[
  {"module": "src/thinkweave/operations/dream.py",
   "rationale": "The phase-1 fan-out re-reads the ontology per worker; hoist the read to the orchestrator and pass it down. Deepens the worker seam and drops N redundant reads.",
   "title": "Hoist ontology read out of dream phase-1 workers"}
]
```

## 4. Gate the candidates — file ONLY what the gate returns

Route every candidate through the #62 evidence gate. It drops candidates with
**no cited evidence** (the anti-invention rule), ranks the survivors by evidence
weight, and caps the run at `[steering] weekly_budget` (default 3):

```bash
weave steering gate --proposals-json <candidates.json> --json
```

The gate returns `{filed: [...], dropped: [...]}`. **File ONLY the `filed`
list.** Each `filed[i]` already carries:

- `body` — the rationale plus a machine-readable ` ```json ` **evidence block**
  (real counts from the index: rework, fix_rounds, superseded_decisions,
  gate_failures, weight). Never invent these.
- `evidence` / `weight` — the raw counts and the ranking weight.
- `module` / `paths` / `rationale` / `title` — echoed from the candidate.

`dropped` entries carry a `reason` (`no cited evidence` or `exceeded weekly
budget`) — report them in the run summary but do NOT file them. The
`weekly_budget` cap is the anti-invention ceiling: at most that many issues per
run, no matter how many candidates the axes produced. (A configured
`weekly_budget = 0` files nothing — a valid pause.)

## 5. File each gated proposal as a draft issue (no PRs)

For each `filed` entry, file one issue. The issue body carries the gate's
`body` (rationale + evidence block) plus a **Definition of Done** checklist and
a **blast-radius** note:

```bash
gh issue create --label arch-proposal \
  --title "<filed.title>" \
  --body "$(cat <<'EOF'
<filed.body>          # rationale + the ```json evidence block, verbatim

## Definition of Done
- [ ] <concrete, verifiable outcome 1>
- [ ] <concrete, verifiable outcome 2>
- [ ] tests cover the new/changed seam
- [ ] ARCHITECTURE.md updated if an invariant moved

## Blast radius
- **Modules touched:** <filed.paths>
- **Risk:** <low | medium | high> — <one line: what could break, what's downstream>
EOF
)"
```

**Never** `gh pr create`. **Never** edit, stage, or commit code. This command's
entire write surface is `gh issue create` (+ the idempotent `gh label create` in
§0). If you ever find yourself about to open a PR or modify a file, stop — that
is the fast loop's job, reached only after a human relabels this issue
`ready-for-agent`.

**Human triage (downstream, not this command's job):** accept a proposal →
relabel `arch-proposal` → `ready-for-agent` (it enters the fast loop's
frontier); reject → close the issue.

## 6. Report

Print: how many candidates each axis produced, how many the skip-list removed,
the gate's `filed` count and each filed issue URL, and the `dropped` list with
reasons. If the gate filed nothing (no evidence, or `weekly_budget = 0`), say so
plainly — a quiet week is a valid outcome, not a failure.

## Headless posture & the skill-resolution gotcha

This command is headless-safe: it never prompts, and it degrades gracefully when
MCP is unavailable (the §1 CLI fallbacks). But there is a resolution gotcha —
**headless `claude -p "/arch-proposal"` only resolves slash commands from
`.claude/commands/` symlinks, not from the skills catalog.** So either:

- the Routine's prompt inline-references this file path
  (`docs/agents/arch-proposal.command.md`), **or**
- the machine-local symlink is created once as part of Routine setup (mirrors
  how `issue-loop` and the vendored ponytail skills are wired — the symlink is
  NOT committed; `git ls-files .claude/commands/` is empty):

  ```bash
  # from the repo root, once per machine
  ln -s ../../docs/agents/arch-proposal.command.md .claude/commands/arch-proposal.md
  ```

## Routine spec (the weekly schedule)

Run this as a **weekly Routine** (prefer CronCreate / Routines over OS crontab).
Exact parameters:

| Parameter | Value |
| --------- | ----- |
| Cadence   | **weekly**, Sunday 03:00 (off the nightly `/dream` at 00:30) |
| Invocation | `claude -p "/arch-proposal" --dangerously-skip-permissions` |
| cwd       | the repo root (so `gh`, `weave`, and the vendored skill resolve) |
| Prompt note | if the `.claude/commands/` symlink isn't installed, reference `docs/agents/arch-proposal.command.md` inline instead of `/arch-proposal` |

`--dangerously-skip-permissions` matches this repo's established headless posture
(the same as the nightly `/dream` cron and the Windows Task Scheduler runs) — the
command's only writes are `gh issue create` + `gh label create`, so a headless
run files gated proposals and opens **zero** PRs without any permission prompt.

**Who creates the Routine:** the human (or the orchestrator wiring this repo)
creates the actual Routine/cron entry via CronCreate — this command file does
not create cron entries or touch crontab. This section is the spec they follow.
