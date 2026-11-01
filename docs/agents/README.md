# docs/agents/

What the devloop rail and the loop commands read from this repo, and where the
rest moved.

## Here

- `loop.toml`: this repo's gate pipeline and loop knobs. It stays here, the
  single copy: devloop finds it by the cwd-upward walk, CI and a machine
  without the rsi-rail repo need it, and the rail holds no second copy to
  drift (dec-dc79eeeb). `tests/devloop_golden_config.json` pins what it
  resolves to.
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
- `devloop/persona.md`, `standing-orders.md`, `judge-brief.md`,
  `shape-brief.md`, `constitution.md`: the pack texts. Without the rail,
  devloop serves its module constants and packaged files, which these copies
  equal as of the funloops pin they were written from.
