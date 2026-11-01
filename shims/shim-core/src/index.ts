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
 * `core.harness.CANONICAL_EVENTS` declares on the Python side: the phases
 * the Python hook handler actually implements (SubagentStart/SubagentStop
 * are the dispatch seam's task-boundary capture; PreCompact writes one
 * `compact` row, #353). This is deliberately narrower than what Claude Code
 * natively fires (it also emits PreToolUse, SessionEnd — those have no
 * thinkweave destination today, so shipping their names here would be a
 * capability silently faked). A shim translates PreCompact only where its
 * harness has a native pre-compaction event; Pi and OpenCode have none
 * observed, so neither shim sends it. Both
 * suites pin this module against the shared `canonical-events.json`
 * fixture, so the TS vocabulary and the Python normaliser cannot drift
 * apart silently.
 */
export const CANONICAL_EVENTS = [
  "SessionStart",
  "UserPromptSubmit",
  "PostToolUse",
  "SubagentStart",
  "SubagentStop",
  "Stop",
  "PreCompact",
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
  SubagentStart: "subagent_start",
  SubagentStop: "subagent_stop",
  Stop: "stop",
  PreCompact: "pre_compact",
};

/**
 * Hard per-event budgets (ms), after the agentmemory precedent: ~800 ms for
 * fire-and-forget telemetry, ~1500 ms where the caller waits on injected
 * context. Ceilings, not headroom — the measured happy path is 5–45× inside
 * them (spike on #194). Known carve-out: the launcher's one-time first-run
 * bootstrap (`uv sync`, minutes) blows any budget — every hook in that
 * window times out with the bootstrap notice on the failure's stderr.
 */
export const DEFAULT_TIMEOUT_MS: Record<CanonicalEvent, number> = {
  SessionStart: 1500,
  UserPromptSubmit: 800,
  PostToolUse: 800,
  SubagentStart: 800,
  SubagentStop: 800,
  Stop: 800,
  PreCompact: 800,
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

/** Env var carrying the hook's spawn time (unix epoch ms) to the handler. */
export const SPAWNED_AT_ENV = "THINKWEAVE_HOOK_SPAWNED_AT";

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
  return runProcess(opts.launcher, argv, payload, timeoutMs, opts.onFailure, true);
}

/**
 * The never-reject subprocess bridge under runHook: spawn `command argv`,
 * envelope on stdin, and resolve with the parsed JSON reply (`parse`) or `{}`
 * — on any failure, after the budget, or always when `parse` is false (a
 * foreign hook's stdout is not ours to read).
 */
function runProcess(
  command: string,
  argv: string[],
  payload: HookEnvelope,
  timeoutMs: number,
  onFailure: RunHookOptions["onFailure"],
  parse: boolean,
): Promise<HookResponse> {
  return new Promise((resolve) => {
    let settled = false;
    let stdout = "";
    let stderr = "";

    // The handler's hook budget runs from this spawn, not from its own first
    // line: launcher + uv + python start-up is spent inside the harness's
    // wait, so the stamp lets the handler count it (#329 follow-up).
    const child = spawn(command, argv, {
      stdio: ["pipe", "pipe", "pipe"],
      env: { ...process.env, [SPAWNED_AT_ENV]: String(Date.now()) },
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
      if (failure && onFailure) {
        try {
          onFailure({ ...failure, stderr });
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
      if (!parse) {
        finish({});
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
 * One foreign hook from the rsi-rail registration set: a script of the
 * user's own that runs on `event` (dec-4f158b58 layer 3). `matcher` is a
 * Claude Code style matcher (a regex over the tool name, empty = all),
 * applied only where the envelope carries a tool name.
 */
export interface ForeignHook {
  name: string;
  event: CanonicalEvent;
  matcher: string;
  /** Absolute path of an executable script; run with the envelope on stdin. */
  script: string;
}

/**
 * The rsi-rail registration set an installed stub hands its shim
 * (`weave hooks install` renders the rail's hooks.toml into it): the enabled
 * canonical events, rail matchers for tool events, and the foreign hooks.
 * Absent: every event enabled, no matcher filter, no foreign hook.
 */
export interface RailHooks {
  events?: CanonicalEvent[];
  matchers?: Partial<Record<CanonicalEvent, string[]>>;
  foreign?: ForeignHook[];
}

function matches(matcher: string, tool: string | undefined): boolean {
  if (!matcher || tool === undefined) return true;
  try {
    return new RegExp(`^(?:${matcher})$`).test(tool);
  } catch {
    return false;
  }
}

/**
 * runHook under the rsi-rail registration set: skip the launcher for a
 * disabled event (or a tool the rail's matchers exclude), then run each
 * foreign hook whose event and matcher fit, concurrently, inside what is left
 * of the same budget. Resolves with the launcher's reply (`{}` when skipped);
 * never rejects. A foreign failure reaches `onFailure` with its name.
 */
export async function dispatch(
  event: CanonicalEvent,
  payload: HookEnvelope,
  opts: RunHookOptions & { rail?: RailHooks },
): Promise<HookResponse> {
  const rail = opts.rail;
  const budget = opts.timeoutMs ?? DEFAULT_TIMEOUT_MS[event];
  const started = Date.now();
  const tool = typeof payload.tool_name === "string" ? payload.tool_name : undefined;
  const ruled = rail?.matchers?.[event];
  const ours =
    (!rail?.events || rail.events.includes(event)) &&
    (!ruled || ruled.some((m) => matches(m, tool)));
  const reply = ours ? await runHook(event, payload, { ...opts, timeoutMs: budget }) : {};
  const foreign = (rail?.foreign ?? []).filter(
    (f) => f.event === event && matches(f.matcher, tool),
  );
  if (foreign.length > 0) {
    const left = Math.max(0, budget - (Date.now() - started));
    const onFailure = opts.onFailure;
    await Promise.all(
      foreign.map((f) =>
        runProcess(
          f.script,
          [],
          payload,
          left,
          onFailure
            ? (failure) => onFailure({ ...failure, message: `foreign hook ${f.name}` })
            : undefined,
          false,
        ),
      ),
    );
  }
  return reply;
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
