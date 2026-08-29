// shim-core — the shared kernel of every thinkweave E3 harness shim (#194).
//
// Shims are *translators* onto Claude Code's canonical hook vocabulary
// (dec-5a076384): protocol adaptation — event synthesis, dedup, debounce,
// timeouts — is allowed here; anything that understands what the events
// *mean* stays on the Python side, behind the subprocess boundary below.
// A pytest deny-list grep over shims/ enforces that rule mechanically.
//
// The error policy is the load-bearing contract: a dead, hanging, or
// misbehaving Python side must NEVER wedge the harness. runHook always
// resolves (never rejects) within its budget, with `{}` as the documented
// fallback, and abandons every handle on timeout so the harness's event
// loop is free to move on.

import { spawn } from "node:child_process";

/**
 * The canonical lifecycle vocabulary — Claude Code's, exactly the tuple
 * `core.harness.CANONICAL_EVENTS` declares on the Python side. Both suites
 * pin against the shared `canonical-events.json` fixture, so this list and
 * the Python normaliser cannot drift apart silently.
 */
export const CANONICAL_EVENTS = [
  "SessionStart",
  "UserPromptSubmit",
  "PostToolUse",
  "Stop",
] as const;

export type CanonicalEvent = (typeof CANONICAL_EVENTS)[number];

/**
 * Canonical event → the handler's argv phase token, exactly what the
 * authored hooks/hooks.json commands pass to
 * `python -m thinkweave.surfaces.hooks.handler <phase>`.
 */
export const EVENT_PHASES: Record<CanonicalEvent, string> = {
  SessionStart: "session_start",
  UserPromptSubmit: "user_prompt_submit",
  PostToolUse: "post_tool_use",
  Stop: "stop",
};

/**
 * Hard per-event budgets (ms), after the agentmemory precedent: ~800 ms for
 * fire-and-forget telemetry, ~1500 ms where the caller waits on injected
 * context. The 2026-08-29 spike measured the real command at 98 ms warm /
 * 141 ms cold (telemetry) and 188 ms warm / 263 ms cold (injection), so
 * these are error-policy ceilings, not headroom the happy path spends.
 */
export const DEFAULT_TIMEOUT_MS: Record<CanonicalEvent, number> = {
  SessionStart: 1500,
  UserPromptSubmit: 800,
  PostToolUse: 800,
  Stop: 800,
};

/**
 * The canonical envelope, mirroring what the Python handler reads from
 * stdin. Open on purpose (index signature): the handler ignores unknown
 * keys, and a shim forwarding extra native fields is harmless — renaming
 * or dropping the known ones is what breaks capture.
 */
export interface HookEnvelope {
  session_id?: string;
  cwd?: string;
  hook_event_name?: string;
  /** PostToolUse */
  tool_name?: string;
  tool_input?: Record<string, unknown>;
  tool_response?: unknown;
  /** Harness-minted per-delivery ids the handler's dedup receipts key on. */
  tool_use_id?: string;
  turn_id?: string;
  /** UserPromptSubmit */
  prompt?: string;
  [key: string]: unknown;
}

/** The handler's reply on stdout (Claude Code hook protocol). */
export interface HookResponse {
  systemMessage?: string;
  hookSpecificOutput?: {
    hookEventName: string;
    additionalContext: string;
  };
  [key: string]: unknown;
}

export interface RunHookOptions {
  /** Path to the repo's `bin/weave-hook-launch` (or a byte-compatible launcher). */
  launcher: string;
  /**
   * Harness id appended as `--harness <id>` — the handler reads its own argv
   * rather than the environment (docs/HARNESSES.md). Omit only for Claude
   * Code, whose commands stay byte-identical to hooks/hooks.json.
   */
  harness?: string;
  /** Override the event's DEFAULT_TIMEOUT_MS budget. */
  timeoutMs?: number;
}

/**
 * Spawn the hook handler for one canonical event, envelope on stdin, and
 * resolve with its parsed reply — or `{}` (the protocol's no-op response) on
 * ANY failure: missing binary, nonzero exit, garbage stdout, or the budget
 * expiring. Never rejects. On timeout the child is killed and every pipe
 * destroyed so nothing keeps holding the harness's event loop; the launcher
 * chain execs down to one python process, and an orphan that somehow
 * survives owns no handle of ours.
 */
export function runHook(
  event: CanonicalEvent,
  payload: HookEnvelope,
  opts: RunHookOptions,
): Promise<HookResponse> {
  const timeoutMs = opts.timeoutMs ?? DEFAULT_TIMEOUT_MS[event];
  const argv = [EVENT_PHASES[event]];
  if (opts.harness) {
    argv.push("--harness", opts.harness);
  }

  return new Promise((resolve) => {
    let settled = false;
    let stdout = "";

    const child = spawn(opts.launcher, argv, {
      // stderr ignored: the launcher is chatty on its diagnostic channel
      // (bootstrap notices, uv resolution errors) and buffering it here
      // could only ever block; the Python side owns its own logging.
      stdio: ["pipe", "pipe", "ignore"],
    });

    const finish = (value: HookResponse) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      resolve(value);
    };

    const abandon = () => {
      try {
        child.kill("SIGKILL");
      } catch {
        /* already gone */
      }
      child.stdout?.destroy();
      child.stdin?.destroy();
      child.unref();
      finish({});
    };

    // unref: the timer must not keep an otherwise-done harness alive; the
    // child's pipes hold the loop until close or abandon, so it still fires.
    const timer = setTimeout(abandon, timeoutMs);
    timer.unref();

    child.on("error", abandon); // ENOENT, EACCES, spawn failures
    child.stdin.on("error", () => {
      /* EPIPE from an already-dead child — the close handler settles it */
    });
    child.stdout.on("data", (chunk: Buffer) => {
      stdout += chunk;
    });
    child.on("close", (code) => {
      if (code !== 0) {
        finish({});
        return;
      }
      try {
        const parsed: unknown = JSON.parse(stdout);
        finish(
          typeof parsed === "object" && parsed !== null
            ? (parsed as HookResponse)
            : {},
        );
      } catch {
        finish({});
      }
    });

    try {
      child.stdin.write(JSON.stringify(payload));
      child.stdin.end();
    } catch {
      abandon();
    }
  });
}

/**
 * Per-process first-call guard: returns a predicate that is true exactly
 * once per key. This is the dedup/synthesis primitive the shims share — a
 * harness with no native SessionStart synthesises one before the first real
 * event of a session (`guard(sessionId)`), and a harness that double-fires
 * an event drops the replay (`guard(deliveryId)`). Per-process on purpose:
 * cross-process dedup belongs to the Python handler's delivery receipts.
 */
export function createFirstCallGuard(): (key: string) => boolean {
  const seen = new Set<string>();
  return (key) => {
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  };
}
