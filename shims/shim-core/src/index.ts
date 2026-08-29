// shim-core — the shared kernel of every thinkweave E3 harness shim (#194).
//
// Shims are *translators* onto the canonical hook vocabulary (dec-5a076384):
// protocol adaptation — event synthesis, dedup, debounce, timeouts — is
// allowed here; anything that understands what the events *mean* stays on
// the Python side, behind the subprocess boundary below. A pytest deny-list
// grep over shims/ enforces that rule mechanically.
//
// The error policy is the load-bearing contract: a dead, hanging, or
// misbehaving Python side must NEVER wedge the harness. runHook always
// resolves (never rejects) within its budget, with `{}` as the documented
// fallback, and abandons every handle on timeout so the harness's event
// loop is free to move on. Failures are not silent, though: the optional
// onFailure callback is the discriminated diagnostic channel.

import { spawn } from "node:child_process";

/**
 * The canonical lifecycle vocabulary — exactly the tuple
 * `core.harness.CANONICAL_EVENTS` declares on the Python side: the four
 * phases the Python hook handler actually implements. This is deliberately
 * narrower than both issue #194's event list (SubagentStart/Stop,
 * SessionEnd, PreCompact — aspirational until the Python side handles them)
 * and what Claude Code natively fires (it also emits PreToolUse, SessionEnd,
 * PreCompact — those have no thinkweave destination today, so shipping their
 * names here would be a capability silently faked). Both suites pin this
 * module against the shared `canonical-events.json` fixture, so the TS
 * vocabulary and the Python normaliser cannot drift apart silently.
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
 * context. The 2026-08-29 spike (methodology recorded on #194) measured the
 * real command at 98 ms warm / 141 ms cold (telemetry) and 188 ms warm /
 * 263 ms cold (injection), so these are error-policy ceilings, not headroom
 * the happy path spends. Known carve-out: the launcher's one-time first-run
 * bootstrap (`uv sync`, minutes) blows any budget — every hook in that
 * window times out with the bootstrap notice on the failure's stderr.
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

/** Why a runHook call fell back to `{}`, for the onFailure channel. */
export interface RunHookFailure {
  kind: "spawn-error" | "timeout" | "exit" | "bad-output";
  /** The child's exit code, when kind is "exit". */
  code?: number;
  /** The spawn error, when kind is "spawn-error". */
  message?: string;
  /**
   * Bounded head of the child's stderr — where the launcher's own
   * diagnostics land (uv resolution errors, the first-run bootstrap notice).
   */
  stderr: string;
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
  /**
   * Called at most once per runHook call when it falls back to `{}`, never
   * on success. This is how a shim tells one failure from another (and from
   * a genuine no-op) without breaking the never-reject contract — the
   * intended use is a warn-once-per-process notice (pair with
   * `createFirstCallGuard`) so a dead Python side is visible instead of
   * silently faked. A throwing callback is swallowed.
   */
  onFailure?: (failure: RunHookFailure) => void;
}

const STDERR_CAP = 4096;

/**
 * Spawn the hook handler for one canonical event, envelope on stdin, and
 * resolve with its parsed reply — or `{}` (the protocol's no-op response) on
 * ANY failure: missing binary, nonzero exit, garbage stdout, or the budget
 * expiring. Never rejects; failures also surface through `onFailure`.
 *
 * On timeout the spawned launcher is SIGKILLed and every pipe destroyed, so
 * nothing keeps holding the harness's event loop — that is the no-wedge
 * guarantee, and it is conformance-tested. Honestly stated ceiling: `uv run`
 * forks rather than execs, so the SIGKILL lands on the launcher/uv and the
 * python grandchild is orphaned; it owns no handle of ours and cannot wedge
 * the harness, but it may keep running against its own files briefly.
 * ponytail: a detached process-group kill would reap it, at the cost of
 * removing the child from the harness's group (Ctrl-C no longer propagates)
 * — not obviously a better trade, so the orphan stays documented instead.
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
    let stderr = "";

    const child = spawn(opts.launcher, argv, {
      stdio: ["pipe", "pipe", "pipe"],
    });
    // utf8 via StringDecoder: a multi-byte character straddling a 64KiB pipe
    // chunk must not decode as U+FFFD — JSON.parse would still succeed and
    // silently corrupt the one payload this bridge exists to carry.
    child.stdout.setEncoding("utf8");
    child.stderr?.setEncoding("utf8");

    const finish = (value: HookResponse, failure?: Omit<RunHookFailure, "stderr">) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      if (failure && opts.onFailure) {
        try {
          opts.onFailure({ ...failure, stderr });
        } catch {
          /* a shim's diagnostic bug must not break the harness */
        }
      }
      resolve(value);
    };

    const abandon = (failure: Omit<RunHookFailure, "stderr">) => {
      try {
        child.kill("SIGKILL");
      } catch {
        /* already gone */
      }
      child.stdout?.destroy();
      child.stderr?.destroy();
      child.stdin?.destroy();
      child.unref();
      finish({}, failure);
    };

    // unref: the timer must not keep an otherwise-done harness alive; the
    // child's pipes hold the loop until close or abandon, so it still fires.
    const timer = setTimeout(() => abandon({ kind: "timeout" }), timeoutMs);
    timer.unref();

    child.on("error", (err) =>
      abandon({ kind: "spawn-error", message: String(err) }),
    );
    child.stdin.on("error", () => {
      /* EPIPE from an already-dead child — the close handler settles it */
    });
    child.stdout.on("data", (chunk) => {
      stdout += chunk;
    });
    child.stderr?.on("data", (chunk: string) => {
      if (stderr.length < STDERR_CAP) stderr += chunk.slice(0, STDERR_CAP - stderr.length);
    });
    child.on("close", (code) => {
      if (code !== 0) {
        finish({}, { kind: "exit", code: code ?? undefined });
        return;
      }
      try {
        const parsed: unknown = JSON.parse(stdout);
        if (typeof parsed === "object" && parsed !== null) {
          finish(parsed as HookResponse);
        } else {
          finish({}, { kind: "bad-output" });
        }
      } catch {
        finish({}, { kind: "bad-output" });
      }
    });

    try {
      child.stdin.write(JSON.stringify(payload));
      child.stdin.end();
    } catch {
      abandon({ kind: "spawn-error", message: "stdin write failed" });
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
