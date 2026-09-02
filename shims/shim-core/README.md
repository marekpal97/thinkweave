# @thinkweave/shim-core

The shared kernel every thinkweave E3 harness shim (Pi #114, OpenCode #195)
builds on. ESM-only (`type: module`, NodeNext), Node >= 20, zero runtime
dependencies. Five runtime exports plus the envelope types:

- **`CANONICAL_EVENTS` / `EVENT_PHASES`** + `HookEnvelope`/`HookResponse` —
  the canonical vocabulary and the handler's stdin/stdout protocol: exactly
  what `core.harness.CANONICAL_EVENTS` declares, four events today — the
  why is in the `CANONICAL_EVENTS` docblock in `src/index.ts`.
  `canonical-events.json` is the drift gate: this package's node:test suite
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

Spike verdict (2026-08-29, tables on #194): spawn-per-call fine, 5–45×
inside every budget; first-run uv-sync carve-out documented at
`DEFAULT_TIMEOUT_MS`.

**Translator rule (dec-5a076384):** protocol adaptation only — enforced by
the pytest deny-list grep.

Standalone on purpose: not part of the Python package tree, not in the pytest
run. `npm install && npm test` here (tsc + node:test, no runtime deps).
