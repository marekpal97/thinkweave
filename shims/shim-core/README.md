# @thinkweave/shim-core

The shared kernel every thinkweave E3 harness shim (Pi #114, OpenCode #195)
builds on. ESM-only (`type: module`, NodeNext), Node >= 20, zero runtime
dependencies. Five runtime exports plus the envelope types:

- **`CANONICAL_EVENTS` / `EVENT_PHASES`** + `HookEnvelope`/`HookResponse` —
  the canonical vocabulary and the handler's stdin/stdout protocol. This is
  **four events, not the nine issue #194 lists**: the vocabulary is whatever
  `core.harness.CANONICAL_EVENTS` declares (currently the four phases the
  Python hook handler implements). Claude Code natively fires more
  (PreToolUse, SessionEnd, PreCompact, Subagent lifecycle) — those have no
  thinkweave destination today, and shipping their names here would promise a
  capability nothing delivers. The issue's longer list is aspirational,
  pending Python-side support; when an event lands there, it lands in
  `canonical-events.json` and both suites force the two languages to move
  together. That fixture is the drift gate: this package's node:test suite
  pins the TS exports against it, and `tests/test_shim_core.py` pins it
  against the Python constants, the authored `hooks/hooks.json` argv, AND
  (statically) this package's source — so a stale side fails a suite instead
  of drifting silently.
- **`runHook(event, payload, opts)`** — subprocess bridge to
  `bin/weave-hook-launch <phase>` (there is no `weave hook` subcommand; the
  launcher is the real, test-pinned entry). Envelope on stdin, parsed reply
  resolved back. Error policy: a dead, hanging, or garbage-spewing Python
  side never wedges the harness — every call resolves within its budget
  (`DEFAULT_TIMEOUT_MS`: 800 ms telemetry / 1500 ms injection) with `{}` as
  the fallback, never rejects, and abandons all handles on timeout.
- **`onFailure` (option) / `RunHookFailure`** — the diagnostic channel: `{}`
  deliberately looks like a no-op, so failures are discriminated here instead
  (spawn-error / timeout / exit+code / bad-output, with a bounded head of the
  child's stderr). Shims should warn once per process (pair with the guard
  below) so a broken install is visible, never silently faked.
- **`createFirstCallGuard()`** — the per-process dedup/synthesis primitive:
  synthesise SessionStart once on harnesses without one, drop double-fired
  deliveries, or gate the warn-once diagnostics above.

Spike verdict (2026-08-29, methodology + full tables recorded on #194):
spawn-per-call is fine — the real command measured 98 ms warm p50 / 263 ms
cold, 5–45× inside every budget including Pi's ~4.5 s deadline. No resident
daemon. One carve-out: the launcher's one-time first-run bootstrap (`uv sync`
when the venv has no thinkweave yet) takes minutes and times out every hook
in that window — the bootstrap notice arrives on the failure's stderr via
`onFailure`, and the window ends at the first completed sync.

**Translator rule (dec-5a076384):** protocol adaptation only — event
synthesis, dedup, debounce, timeouts. Anything that understands what the
events mean lives on the Python side. A pytest deny-list grep over `shims/`
enforces the floor of that rule.

Standalone on purpose: not part of the Python package tree, not in the pytest
run. `npm install && npm test` here (tsc + node:test, no runtime deps).
