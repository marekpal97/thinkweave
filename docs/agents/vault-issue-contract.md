# Vault ↔ issue contract — division of labor for loop runs

This is the **capture-parity contract** (n-04674047): a headless loop run —
the fast loop, a Routine, `/loop` — must land in the vault *identically* to
interactive work, and the boundary between the surfaces a run writes to must
be a written, tested contract rather than tribal knowledge. The contract test
lives at `tests/test_vault_issue_contract.py`; this doc is its prose half.

A finished issue produces outputs on four surfaces. Each owns a disjoint slice
of the record — **no field is written by two owners.**

## Ownership

| Surface | Owns |
|---|---|
| tracker comments | run history, claims, gate evidence |
| PR body | diff summary, gate table, smell report |
| trajectory note | how it went, lessons |
| session note (`/wrap`) | cross-issue synthesis, decisions, insights |

- **Tracker comments** own the *run history*: which run claimed the issue,
  when, and the gate evidence (pass/fail detail) — GitHub owns their state.
- **PR body** owns the *diff summary*, the gate table, and the smell report —
  the code-review view of the change.
- **Trajectory note** (`type: note`, tag `loop-run`; assembled by
  `scripts/issue_loop.py trajectory`, design in
  [`issue-loop-memory.md`](issue-loop-memory.md)) owns **how the work went and
  the lessons** — the reusable half, written by the orchestrator at run end
  while the fix-round/seam detail is still in context. It is a plain `note`: its
  frontmatter carries observable facts only (`outcome`, `gates`, `files_touched`,
  `fix_rounds`, `primed`/`served`) and **never a decision field**
  (`status`, `predicted_outcome`, `prediction_history`, `supersedes`,
  `superseded_by`, `file_paths`). The loop never mints a `type: decision`.
- **Session note** (`/wrap`) owns **cross-issue synthesis, decisions, and
  insights**. Where an issue's resolution embodied a real architectural choice,
  `/wrap` promotes it to a `decision` exactly as in a hand-driven session —
  concepts and judgment belong to wrap's LLM pass, not to a per-PR template.
  This is the sole owner of decisions: **a decision is never minted by both the
  loop and `/wrap`.** (Scope: this partition is loop-vs-`/wrap` *within a work
  session*. Plan-time distillation — `/plan-distill`
  ([`plan-distill.command.md`](plan-distill.command.md)), human-invoked at
  grill/plan time, outside the loop — mints its own *forecast* decisions on a
  separate surface at a separate moment; it is not a second writer racing the
  loop for this record.)

The boundary that keeps this a partition: the loop writes the *deterministic*
per-issue record (trajectory), and `/wrap` writes the *synthesised* cross-issue
record (session). `outcome` (an observable fact: shipped / routed-to-human /
awaiting-approval) is not the decision-lifecycle `status`; the two are never
conflated.

## Wrap coverage for headless runs

The AC — "after a headless loop run, `weave_search(type=session, since=<run>)`
returns a session note and `weave_timeline` shows it" — is met by the existing
**hook + dream-wrap-worker catch-up rail**, not by new machinery:

1. The `SessionStart` hook (`hooks/handler.py::_ensure_session`) mints the
   run's session note on the first buffered event, stamped with the Claude Code
   session UUID (`source_session`) and **no** `processed` flag. This fires for a
   headless `claude -p` / worktree run exactly as for an interactive one — the
   note is loop-shaped but otherwise ordinary.
2. Hook events buffer into the session folder's `events.jsonl`.
3. The nightly `/dream` phase-2 `dream-wrap-worker` scans for **unwrapped but
   wrap-eligible** sessions (`operations/dream.py::_collect_unwrapped_sessions`):
   `type: session`, not `processed: true`, recorded within `recent_days` (30),
   with a non-empty `events.jsonl`. A loop-shaped session meets all four —
   proven by `tests/test_dream.py::TestScan::test_unwrapped_sessions_surfaces_headless_loop_session`.
   The worker runs the extract + `weave wrap-finalize` tail and stamps
   `processed: true`.

So the loop does **not** run `/wrap` itself. Doing so would either duplicate the
LLM synthesis the nightly dream already owns or (worse) let the loop mint
decisions — violating the single-owner rule above. The deterministic content
that would otherwise be lost by wrap time — how the work went, the lessons — is
already captured in the per-issue trajectory note; the session note only adds
the cross-issue synthesis, which wrap composes whenever it runs.

**Subagents.** Loop implementer subagents do not emit their own wrap-eligible
sessions; their work is captured deterministically by the orchestrator's
trajectory note (filed under the run's session via `session_id`). The result is
exactly **one wrapped session note plus one trajectory note per issue** per run.

## Verifying a real run

The structural guarantees above are covered by tests against fixture artifacts
(no real vault is read). End-to-end verification of an actual run — that this
run's session note (`weave_search type=session`) and its per-issue trajectory
notes exist and carry the right owners — is an MCP-side check for whoever has
vault access, not something the contract test asserts.
