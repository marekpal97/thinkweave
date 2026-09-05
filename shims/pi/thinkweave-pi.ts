// The Pi E3 shim (#114) — a translator from Pi's extension events onto the
// canonical hook vocabulary, on the shim-core kernel (#194).
//
// Loaded through a one-line stub `weave hooks install --harness pi` writes
// into Pi's extensions dir; the stub re-exports this file from the repo
// checkout, so a reinstall only ever rewrites the stub (live-verified on Pi
// 0.84.4, 2026-09-05: the loader follows an absolute-path re-export and this
// file's relative .ts import of shim-core).
//
// Translator rule (#194, grep-enforced): protocol adaptation only — event
// mapping, tool-name casing, payload flattening, dedup, injection plumbing.
// What any event *means* stays on the Python side of runHook.
//
// Event map (profile `hook_events`, every native event observed firing live
// in the 2026-09-05 probe):
//   session_start      → SessionStart   (awaited: the `context` event fires
//                        milliseconds later, so fire-and-forget would always
//                        miss the injection window)
//   before_agent_start → UserPromptSubmit (awaited: its reply may carry the
//                        prompt-time enrichment block to inject)
//   tool_result        → PostToolUse    (fire-and-forget)
//   agent_end          → Stop           (awaited within budget; a Stop that
//                        outlives it keeps running as runHook's documented
//                        orphan and finishes the buffer materialisation)
//
// The SessionStart payload cannot ride a hook reply on Pi — there is no
// additionalContext channel. It is queued here and prepended as a synthetic
// user message by the `context` handler (superpowers.ts's proven pattern):
// armed on session_start, marker-deduped against the live message array,
// disarmed on agent_end. Serve-once semantics stay on the Python side —
// resume/compact lifecycles return no payload, so this shim re-asks and
// injects whatever the handler decides (today: nothing).

import { spawn } from "node:child_process";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import {
  createFirstCallGuard,
  runHook,
  type CanonicalEvent,
  type HookEnvelope,
} from "../shim-core/src/index.ts";

const HARNESS = "pi";
// This file lives at <repo>/shims/pi/, the launcher at <repo>/bin/ — derived
// relative to the module itself so the installed stub is the only artifact
// carrying machine state.
const REPO_ROOT = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const LAUNCHER = join(REPO_ROOT, "bin", "weave-hook-launch");

// First line of every injected context message; the dedup key here and the
// harness-plumbing filter in the Python importer (importers/pi.py keeps the
// same literal).
const CONTEXT_MARKER = "[thinkweave session context]";

// The launcher's warm path is ~0.4s, but a cold spawn (fresh uv/python
// caches) measured 12.96s on 2026-09-05 — past every budget. This no-op
// phase (the handler's fall-through) pays the warm-up concurrently with
// Pi's own startup, so the awaited SessionStart call that follows usually
// finds a warm cache. Detached and unref'd: it must never hold Pi's exit.
function warmLauncher(): void {
  try {
    const child = spawn(LAUNCHER, ["warmup"], {
      stdio: ["pipe", "ignore", "ignore"],
      detached: true,
    });
    child.on("error", () => {});
    child.stdin?.on("error", () => {});
    try {
      child.stdin?.write("{}");
      child.stdin?.end();
    } catch {
      /* already gone */
    }
    child.unref();
  } catch {
    /* warm-up is best-effort by definition */
  }
}

// Pi mints its tool names in lowercase; the buffer vocabulary is Claude
// Code's. Unlisted names pass through unchanged — the handler's own gates
// decide what counts, never this shim.
const TOOL_NAMES: Record<string, string> = {
  bash: "Bash",
  edit: "Edit",
  write: "Write",
};

// Pi's session_start reasons vs the handler's lifecycle vocabulary
// (startup|clear|resume|compact — serve-once serves the first two only).
// Pi has no compact reason here; compaction is its own event below.
const LIFECYCLES: Record<string, string> = {
  startup: "startup",
  new: "clear",
  resume: "resume",
  reload: "resume",
  fork: "resume",
};

function flattenContent(content: unknown): string {
  if (typeof content === "string") return content;
  if (!Array.isArray(content)) return "";
  return content
    .map((block) =>
      block && typeof block === "object" && typeof (block as any).text === "string"
        ? (block as any).text
        : "",
    )
    .filter(Boolean)
    .join("\n");
}

export default function thinkweavePi(pi: any) {
  warmLauncher();

  // One stable session identity per process: Pi's own id when the manager
  // exposes it, else minted — a capture with no session key is dropped by
  // the handler, so headless print-mode runs need the fallback.
  let sessionId = "";
  const warnOnce = createFirstCallGuard();
  let lastCtx: any = null;
  const pendingInjections: string[] = [];

  function resolveSessionId(ctx: any): string {
    try {
      const id = ctx?.sessionManager?.getSessionId?.();
      if (typeof id === "string" && id) sessionId = id;
    } catch {
      /* keep whatever we have */
    }
    if (!sessionId) {
      sessionId = `pi-${Date.now()}-${Math.random().toString(36).slice(2, 10)}`;
    }
    return sessionId;
  }

  function envelope(event: CanonicalEvent, extra: HookEnvelope): HookEnvelope {
    return {
      hook_event_name: event,
      session_id: sessionId,
      cwd: process.cwd(),
      ...extra,
    };
  }

  function call(
    event: CanonicalEvent,
    extra: HookEnvelope,
    timeoutMs?: number,
  ) {
    return runHook(event, envelope(event, extra), {
      launcher: LAUNCHER,
      harness: HARNESS,
      timeoutMs,
      onFailure: (failure) => {
        // Visible once per (event, kind), never wedging: a dead Python side
        // is a notice in the TUI, not a silent capture gap.
        if (!warnOnce(`${event}:${failure.kind}`)) return;
        try {
          lastCtx?.ui?.notify?.(
            `thinkweave ${event} hook ${failure.kind}` +
              (failure.stderr ? ` — ${failure.stderr.slice(0, 200)}` : ""),
            "warning",
          );
        } catch {
          /* diagnostics must not break the harness */
        }
      },
    });
  }

  function queueInjection(text: string): void {
    if (text && text.trim()) {
      pendingInjections.push(`${CONTEXT_MARKER}\n${text}`);
    }
  }

  // The SessionStart payload is fetched and injected INSIDE the `context`
  // handler, not here: `context` fires ~2ms after `session_start` (measured
  // 2026-09-05), far sooner than an awaited runHook resolves, so building
  // the payload in this handler would always miss the injection window.
  // Instead this arms a flag the first `context` call consumes — the exact
  // superpowers.ts arm/inject/disarm split. Armed again on compaction.
  let bootstrapLifecycle: string | null = null;
  pi.on("session_start", (event: any, ctx: any) => {
    lastCtx = ctx;
    resolveSessionId(ctx);
    const reason = typeof event?.reason === "string" ? event.reason : "startup";
    bootstrapLifecycle = LIFECYCLES[reason] ?? "startup";
  });

  // Compaction is its own event on Pi; re-arm so the `context` call after it
  // re-runs SessionStart (serve-once on the Python side decides `compact`
  // injects nothing, but the call keeps the exposure ledger honest).
  pi.on("session_compact", (_event: any, ctx: any) => {
    lastCtx = ctx;
    resolveSessionId(ctx);
    bootstrapLifecycle = "compact";
  });

  pi.on("context", async (event: any, ctx: any) => {
    lastCtx = ctx;
    // First context call after (re-)arming: fetch and queue the SessionStart
    // payload. Awaited here because Pi awaits an async context handler's
    // promise before the LLM call (verified on 0.84.4), so this is the one
    // place the injection window stays open for a subprocess round-trip.
    if (bootstrapLifecycle !== null) {
      const lifecycle = bootstrapLifecycle;
      bootstrapLifecycle = null;
      resolveSessionId(ctx);
      const reply = await call("SessionStart", { source: lifecycle }, 5000);
      queueInjection(reply.hookSpecificOutput?.additionalContext ?? "");
    }
    if (pendingInjections.length === 0) return undefined;
    const messages = Array.isArray(event?.messages) ? event.messages : [];
    // Marker dedup: repeated `context` calls within one turn must not
    // double-inject (superpowers' guard, kept exactly).
    const already = new Set<string>();
    for (const m of messages) {
      const text = flattenContent(m?.content);
      if (text.startsWith(CONTEXT_MARKER)) already.add(text);
    }
    const fresh = pendingInjections.filter((t) => !already.has(t));
    pendingInjections.length = 0;
    if (fresh.length === 0) return undefined;
    // Insert after any leading compaction summaries so the payload neither
    // precedes the retained summary nor gets compacted away with it.
    let at = 0;
    while (at < messages.length && messages[at]?.role === "compactionSummary") {
      at += 1;
    }
    const injected = fresh.map((text) => ({
      role: "user",
      content: [{ type: "text", text }],
      timestamp: Date.now(),
    }));
    return { messages: [...messages.slice(0, at), ...injected, ...messages.slice(at)] };
  });

  pi.on("before_agent_start", async (event: any, ctx: any) => {
    lastCtx = ctx;
    resolveSessionId(ctx);
    const prompt = typeof event?.prompt === "string" ? event.prompt : "";
    if (!prompt) return undefined;
    // Awaited: the reply can carry the prompt-time enrichment block, and the
    // `context` event that could deliver it fires immediately after this
    // handler. Capture itself also lands before a print-mode exit this way.
    const reply = await call("UserPromptSubmit", { prompt });
    queueInjection(reply.hookSpecificOutput?.additionalContext ?? "");
  });

  pi.on("tool_result", (event: any, ctx: any) => {
    lastCtx = ctx;
    const native = typeof event?.toolName === "string" ? event.toolName : "";
    if (!native) return undefined;
    // Fire-and-forget telemetry — the tool loop never waits on capture.
    void call("PostToolUse", {
      tool_name: TOOL_NAMES[native] ?? native,
      tool_input:
        event?.input && typeof event.input === "object" ? event.input : {},
      tool_response: {
        stdout: flattenContent(event?.content),
        stderr: "",
      },
      tool_use_id:
        typeof event?.toolCallId === "string" ? event.toolCallId : undefined,
    });
    return undefined;
  });

  pi.on("agent_end", async (event: any, ctx: any) => {
    lastCtx = ctx;
    pendingInjections.length = 0;
    // Stop reconstructs the session note and indexes — usually slower than
    // the 800ms telemetry budget. The await keeps the happy path ordered;
    // past the budget the launcher is reaped and the python grandchild
    // finishes the materialisation as runHook's documented orphan.
    await call("Stop", {});
  });
}
