# @thinkweave/shim-core

The shared kernel every thinkweave E3 harness shim (Pi #114, OpenCode #195)
builds on. One small TS package, four exports:

- **Canonical envelope types** — `CANONICAL_EVENTS`, `HookEnvelope`,
  `HookResponse`, mirroring the Python side's `core.harness.CANONICAL_EVENTS`
  and the handler's stdin/stdout protocol. `canonical-events.json` is the one
  shared fixture: this package's node:test suite pins the TS exports against
  it, and `tests/test_shim_core.py` pins it against the Python constants and
  the authored `hooks/hooks.json` argv — neither language can drift alone.
- **`runHook(event, payload, opts)`** — subprocess bridge to
  `bin/weave-hook-launch <phase>` (there is no `weave hook` subcommand; the
  launcher is the real, test-pinned entry). Envelope on stdin, parsed reply
  resolved back. Error policy: a dead, hanging, or garbage-spewing Python
  side never wedges the harness — every call resolves within its budget
  (`DEFAULT_TIMEOUT_MS`: 800 ms telemetry / 1500 ms injection) with `{}` as
  the fallback, never rejects, and abandons all handles on timeout.
- **`createFirstCallGuard()`** — the per-process dedup/synthesis primitive:
  synthesise SessionStart once on harnesses without one, drop double-fired
  deliveries.

Spike verdict (2026-08-29, recorded on #194): spawn-per-call is fine — the
real command measured 98 ms warm / 263 ms worst-case cold, 5–45× inside every
budget including Pi's ~4.5 s deadline. No resident daemon.

**Translator rule (dec-5a076384):** protocol adaptation only — event
synthesis, dedup, debounce, timeouts. Anything that understands what the
events mean lives on the Python side. A pytest deny-list grep over `shims/`
enforces the floor of that rule.

Standalone on purpose: not part of the Python package tree, not in the pytest
run. `npm install && npm test` here (tsc + node:test, no runtime deps).
