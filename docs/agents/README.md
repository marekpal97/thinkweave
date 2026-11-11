# docs/agents/

What the devloop rail and the loop commands read from this repo, and where the
rest moved.

## Here

- `loop.toml`: this repo's facts for the loop: the gate pipeline and test
  command, labels, triage paths, branch prefix, baseline rule and dispatch
  role shapes. devloop finds it by the cwd-upward walk, so CI and a machine
  without the rsi-rail repo need nothing else. It carries none of the
  person's settings (dec-6c3539b2): those resolve to devloop's defaults, or
  to the rail's `devloop/loop.toml` below. `tests/devloop_golden_config.json`
  pins what it resolves to without the rail, and
  `tests/test_devloop_boundaries.py` reds if a person's setting is added
  here.
- `vault-issue-contract.md`: the capture-parity contract (which surface owns
  which field), tested by `tests/test_vault_issue_contract.py`.
- `domain.md`, `arch-proposal.command.md`, `plan-distill.command.md`: native
  command docs and fixtures.

## Moved to the rsi-rail repo (thinkweave#346)

The texts that steer the loop live in the rsi-rail repo's `devloop/` folder,
read live when `rsi_rail` is set (`THINKWEAVE_RSI_RAIL` or the user-scope
`rsi_rail` key): `scripts/issue_loop.py` exports
`DEVLOOP_POLICY_DIR=<rsi_rail>/devloop`, and devloop serves each file present
there in place of its packaged default.

- `devloop/issue-loop-memory.md`: how finished issues feed thinkweave (the
  trajectory note and its round). Its division of labor defers to
  `vault-issue-contract.md` here.
- `devloop/steering.md`: the evidence-gated steering contract behind
  `weave steering gate`.
- `devloop/loop.toml`: the person's loop settings: `[loop]`
  `max_issues_per_run`, `max_fix_rounds`, `training_mode`, `run_mode`,
  `delivery`; `[tdd] mode`; `[prime] enabled, limit, budget_chars`;
  `[dispatch.<role>]` `transport`, `harness`, `model`, `effort`, `args`.
  devloop layers it over this repo's `loop.toml` and refuses any other key
  there by name, so the two files share no key. It ships with every line
  commented at devloop's default. `tests/test_issue_loop_seam.py` runs the
  layering through the shim (thinkweave#334 round 2).
- `devloop/persona.md`, `standing-orders.md`, `judge-brief.md`,
  `shape-brief.md`, `constitution.md`: the pack texts. Without the rail,
  devloop serves its module constants and packaged files, which these copies
  equal as of the funloops pin they were written from.
