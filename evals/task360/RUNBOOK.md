# Task-360 eval — runbook

The 360 task test drives real interactive sessions through herdr against a throwaway
sandbox repo and a throwaway vault, then asserts the task trace from the throwaway index.
It is not a pytest suite; the tests gate never runs it. Only the oracle's own rules have
a unit check: `uv run --no-sync pytest evals/task360/test_oracle.py -q`.

## Run it

From any plain shell on a machine with herdr running and thinkweave dev-linked:

```bash
evals/task360/drive.sh setup        # optional: `run` sets up when no run exists
evals/task360/drive.sh run S2       # runs S0, S1, then S2; checks each as it goes
evals/task360/drive.sh check        # every checkpoint on the final state
evals/task360/drive.sh finish       # close the workspace once you have looked
```

- `setup` wipes and rebuilds the run root (`$TASK360_ROOT`, default
  `~/.local/state/task360`). It holds `sandbox/` (a fresh git repo with origin
  `github.com:task360/sandbox`, so `#n` resolves to `github:task360/sandbox#n`),
  `vault/` (an empty vault with the live ontology copied in) and `state.json`.
- `setup` creates one herdr workspace labelled `task360` and prints its id. Every
  session runs in a pane of it. Panes stay open after their session ends, until `finish`.
- Every pane starts with `THINKWEAVE_VAULT=<run root>/vault` and
  `THINKWEAVE_PROJECT=task360_sandbox`, so hooks, MCP and CLI write only the throwaway
  vault. The S0 checkpoint asserts the live vault has no folder for the sandbox project.
- Every pane also starts with `PYTHONPATH=<this checkout>/src`, so hooks and MCP run the
  checkout the driver lives in (a worktree included), not the dev-linked one.
- `run <label>` runs the scenario's prerequisites first, once per setup.
- Oracle rows print `PASS`, `FAIL` (a bug in a supported route), `KNOWN #n` (a gap that
  ticket #n owns: add evidence there, never a new issue) or `GAP` (a probe that found a
  missing capability). Each row carries the session id and session note id.

## Permissions: no bypass flags

No session or herdr worker starts with a permission-bypass flag.

- **Claude Code**: the sandbox's committed `.claude/settings.json` allows Bash, the
  file tools, the Agent tool and the thinkweave MCP tools. Worktree-isolated helpers
  (S4) see the same file. Its one-time folder-trust dialog is answered by the driver.
- **Codex**: a project-local `.codex/config.toml` cannot set `approval_policy` or
  `sandbox_mode` (Codex ignores those keys outside the user config). S12, S15 and the
  S13 worker can block on an approval. The driver then prints `HUMAN STEP: <label> is
  blocked …` with the pane id and waits. A human answers the prompt in that pane. Its
  folder-trust dialog is answered by the driver.
- **pi**: no permission gate, so S14's worker needs no step.

## Scenarios

Tickets exist only in the prompts (`prompts/`); nothing lives on GitHub.
#1 is the `dogfood greet` CLI, #2 `dogfood version`, #3 `dogfood version --json` (needs #2).

| Label | Needs | Harness | What it exercises |
|---|---|---|---|
| S0 | — | claude | Wiring smoke: the session note and its event register land in the throwaway vault. |
| S1 | S0 | claude | Create #1 with one foreground helper. |
| S2 | S1 | claude | Continue #1 in a new session, loose wording. |
| S3 | S2 | claude | Two background helpers that outlive the turn. |
| S4 | S3 | claude | A worktree-isolated helper. |
| S5 | S4 | claude → claude worker | Dispatch through herdr; the worker's warm-up prompt stays out of the digest. |
| S6 | S5 | claude | A mid-session correction, then late feedback as a `feedback_for` note. |
| S7 | S0 | claude | Two tickets (#2, #3) in one session, one helper each. |
| S8 | S1 | none | The devloop route: `s8-payload.json` lands as a devloop round on #1. |
| S9 | S1 | claude | Adds a round to #1; a second wrap changes nothing. |
| S10 | S9 | claude | #1 marked done: one close row. |
| S11 | S10 | none | Read-back: graph edges and the session-start listing. |
| S12 | S7 | codex | PROBE: a Codex native subagent while working #2. Human step possible. |
| S13 | S7 | claude → codex worker | PROBE: a Codex herdr worker on #3. Human step possible. |
| S14 | S7 | claude → pi worker | PROBE: a pi herdr worker on #3. |
| S15 | S7 | codex | PROBE: Codex continues #3 and wraps. Human step possible. |

Rows assert floors ("at least N rounds"), so a checkpoint still holds on the final state
of a longer run.

## What the driver handles

- **Folder trust**: a first start in a new sandbox path hits the harness's trust
  dialog. The driver moves the menu cursor to the "Yes" option and confirms.
- **Autocomplete**: a Codex `$skill` prompt can stay unsubmitted behind its popup.
  When herdr reports `agent_prompt_stalled`, the driver presses Enter again.
- **Background shells**: `settle` waits until the agent is idle and no shell runs
  under its process. `wrap` and `stop` both settle first, so a backgrounded
  `weave wrap-finalize` or a worker dispatch finishes before the next step.

## After a run

1. Run `drive.sh check` with no label, so every checkpoint runs on the final state.
2. File one issue per `FAIL` and per material `GAP` under the task-trace epic, with the
   oracle output as evidence. Add `KNOWN #n` evidence to ticket #n as a comment.
3. Run `drive.sh finish`. The run root stays on disk for inspection until the next setup.
