// The OpenCode E3 shim (#195) — a translator from OpenCode's plugin hooks and
// bus events onto the canonical hook vocabulary, on the shim-core kernel (#194).
//
// Loaded through the one-line stub `weave hooks install --harness opencode`
// writes into ~/.config/opencode/plugins/; the stub re-exports this file's
// default export from the repo checkout, so a reinstall only rewrites the stub.
//
// Translator rule (#194, grep-enforced): protocol adaptation only — event
// mapping, session identity, tool-name casing, payload flattening, dedup,
// injection plumbing. What any event *means* stays on the Python side.
//
// Event map (every native hook below observed firing live, OpenCode 1.19.0,
// 2026-10-12 probe — headless `opencode run` and the interactive TUI):
//   first sighting of a     → SessionStart (synthesised: OpenCode has no
//   root session id           session-start hook; `session.created` marks a
//                             fresh session, a first sighting without it is a
//                             resumed one)
//   chat.message            → UserPromptSubmit (awaited: its reply may carry
//                             the prompt-time enrichment block)
//   tool.execute.after      → PostToolUse (fire-and-forget)
//   event session.idle      → Stop (once per finished turn; the only end-of-
//                             turn signal that fires — claude-mem#2462)
//   event session.compacted → SessionStart(compact), re-asked like Pi does
//
// Injection channel: `experimental.chat.system.transform` runs before EVERY
// model call with the session id. The SessionStart payload is fetched once
// per session, cached here, and re-appended on each call — so it lives for
// the whole session, as Claude Code's additionalContext does. The prompt-time
// block rides the same channel for the rest of its turn and is dropped at
// session.idle. Serve-once semantics stay on the Python side.
//
// Session identity: `shell.env` exports OPENCODE_SESSION_ID (the root
// session's id) and THINKWEAVE_HARNESS into every shell call, so
// `weave session-id` resolves inside OpenCode exactly as on Claude Code.
//
// Subagent child sessions (the `task` tool) are folded onto their root: their
// tool events count toward the parent session, their prompts and idles do not
// (those are the parent agent talking to itself, not the user).

import { spawn } from "node:child_process";
import { fileURLToPath } from "node:url";
import { homedir } from "node:os";
import { dirname, join } from "node:path";
import {
  createFirstCallGuard,
  runHook,
  type CanonicalEvent,
  type HookEnvelope,
} from "../shim-core/src/index.ts";

const HARNESS = "opencode";
const REPO_ROOT = join(dirname(fileURLToPath(import.meta.url)), "..", "..");

// OpenCode keeps every session in one store file, not one file per session,
// so the prompt envelope addresses the session inside it as `<store>#<id>`
// (the digest reader splits on `#`) — what lets a dispatched worker bind.
const STORE = join(
  process.env.XDG_DATA_HOME || join(homedir(), ".local", "share"),
  "opencode",
  "opencode.db",
);
const LAUNCHER = join(REPO_ROOT, "bin", "weave-hook-launch");

// First line of every injected block; the importer filters on the same
// literal (importers/opencode.py), as the Pi pair does.
const CONTEXT_MARKER = "[thinkweave session context]";

// Measured launcher floor on the dev machine (WSL2, store on /mnt/c): the
// shim-core telemetry defaults sit below it — same budgets as the Pi shim.
// PostToolUse is fire-and-forget, so its budget only bounds how long a
// launcher may live; at 800 ms a launcher started under load (the last tool
// call races Stop and the final reply) was reaped before uv had forked
// python, and the event — 2026-10-12: the session's commit — never landed.
const BUDGET_MS = {
  SessionStart: 5000,
  UserPromptSubmit: 2500,
  PostToolUse: 10000,
  Stop: 6000,
} as const;

// OpenCode mints lowercase tool names; the buffer vocabulary is Claude
// Code's. Unlisted names pass through — the handler's gates decide.
const TOOL_NAMES: Record<string, string> = {
  bash: "Bash",
  edit: "Edit",
  write: "Write",
  read: "Read",
};

// OpenCode names MCP tools `<server>_<tool>`; the handler keys on Claude
// Code's `mcp__<server>__<tool>` namespacing.
const MCP_SERVER = "thinkweave";
function canonicalToolName(native: string): string {
  if (native.startsWith(`${MCP_SERVER}_`)) {
    return `mcp__${MCP_SERVER}__${native.slice(MCP_SERVER.length + 1)}`;
  }
  return TOOL_NAMES[native] ?? native;
}

// OpenCode's tool args are camelCase (`filePath`, `oldString`); the handler
// reads Claude Code's snake_case. apply_patch carries its envelope as
// `patchText` where the handler (Codex's shape) reads `command`.
function canonicalToolInput(native: string, args: unknown): Record<string, unknown> {
  if (!args || typeof args !== "object") return {};
  const out: Record<string, unknown> = {};
  for (const [key, value] of Object.entries(args as Record<string, unknown>)) {
    out[key.replace(/[A-Z]/g, (c) => `_${c.toLowerCase()}`)] = value;
  }
  if (native === "apply_patch" && typeof out.patch_text === "string") {
    out.command = out.patch_text;
  }
  return out;
}

function warmLauncher(): void {
  // The no-op phase pays a cold uv/python start concurrently with OpenCode's
  // own boot, so the first awaited call usually finds a warm cache.
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

interface SessionState {
  /** Resolves once SessionStart has answered; memoised so concurrent model
   * calls (title generation runs beside the main turn) ask only once. */
  started: Promise<void> | null;
  context: string;
  enrichment: string[];
  model?: string;
  /** `/name args` of a command expansion awaiting its chat.message. */
  command?: string;
  /** PostToolUse calls still running; Stop drains them first so the turn's
   * last tool event is buffered before the session note is rebuilt. */
  inflight: Set<Promise<unknown>>;
}

export default async function thinkweaveOpencode(input: any) {
  warmLauncher();

  const cwd: string = typeof input?.directory === "string" ? input.directory : process.cwd();
  const warnOnce = createFirstCallGuard();
  const sessions = new Map<string, SessionState>();
  const parents = new Map<string, string>();
  const created = new Set<string>();
  const stops = new Set<Promise<void>>();
  let anyStarted = false;

  function rootOf(id: string): string {
    let cur = id;
    for (let hops = 0; hops < 16 && parents.has(cur); hops += 1) {
      cur = parents.get(cur)!;
    }
    return cur;
  }

  function state(id: string): SessionState {
    let s = sessions.get(id);
    if (!s) {
      s = { started: null, context: "", enrichment: [], inflight: new Set() };
      sessions.set(id, s);
    }
    return s;
  }

  function call(event: CanonicalEvent, sid: string, extra: HookEnvelope, timeoutMs?: number) {
    const envelope: HookEnvelope = {
      hook_event_name: event,
      session_id: sid,
      cwd,
      model: sessions.get(sid)?.model,
      ...extra,
    };
    return runHook(event, envelope, {
      launcher: LAUNCHER,
      harness: HARNESS,
      timeoutMs,
      onFailure: (failure) => {
        // Visible once per (event, kind), never wedging the harness.
        if (!warnOnce(`${event}:${failure.kind}`)) return;
        try {
          void input?.client?.tui?.showToast?.({
            body: {
              message:
                `thinkweave ${event} hook ${failure.kind}` +
                (failure.stderr ? ` — ${failure.stderr.slice(0, 200)}` : ""),
              variant: "warning",
            },
          });
        } catch {
          /* diagnostics must not break the harness */
        }
      },
    });
  }

  function marked(text: string | undefined): string {
    return text && text.trim() ? `${CONTEXT_MARKER}\n${text}` : "";
  }

  function ensureStarted(sid: string, lifecycle?: string): Promise<void> {
    const s = state(sid);
    if (s.started && !lifecycle) return s.started;
    // startup for the process's first session, clear for a later fresh one
    // (the TUI's /new), resume for a session this process did not create.
    const source =
      lifecycle ?? (created.has(sid) ? (anyStarted ? "clear" : "startup") : "resume");
    anyStarted = true;
    s.started = call("SessionStart", sid, { source }, BUDGET_MS.SessionStart).then(
      (reply) => {
        const text = marked(reply.hookSpecificOutput?.additionalContext);
        if (text) s.context = text;
      },
    );
    return s.started;
  }

  return {
    event: async ({ event }: any) => {
      const type = event?.type;
      const props = event?.properties ?? {};
      if (type === "session.created") {
        const info = props.info ?? {};
        if (typeof info.id === "string") {
          created.add(info.id);
          if (typeof info.parentID === "string" && info.parentID) {
            parents.set(info.id, info.parentID);
          }
        }
        return;
      }
      const sid = typeof props.sessionID === "string" ? props.sessionID : "";
      if (!sid || parents.has(sid)) return;
      if (type === "session.compacted") {
        await ensureStarted(sid, "compact");
      } else if (type === "session.idle") {
        const s = state(sid);
        s.enrichment.length = 0;
        const stop = (async () => {
          // Bounded: each in-flight call resolves within its own budget.
          await Promise.all([...s.inflight]);
          // Past the budget the launcher is reaped and the python grandchild
          // finishes the materialisation as runHook's documented orphan.
          await call("Stop", sid, {}, BUDGET_MS.Stop);
        })();
        stops.add(stop);
        await stop.finally(() => stops.delete(stop));
      }
    },

    // `opencode run` disposes the instance ~10 ms after the final idle
    // without awaiting the event handler above (live 2026-10-12), which
    // killed the last turn's capture with the process. dispose IS awaited:
    // drain every outstanding call, each still bounded by its own budget.
    dispose: async () => {
      await Promise.all([
        ...stops,
        ...[...sessions.values()].flatMap((s) => [...s.inflight]),
      ]);
    },

    "experimental.chat.system.transform": async (i: any, o: any) => {
      const sid = typeof i?.sessionID === "string" ? rootOf(i.sessionID) : "";
      if (!sid || !Array.isArray(o?.system)) return;
      await ensureStarted(sid);
      const s = state(sid);
      if (s.context) o.system.push(s.context);
      for (const block of s.enrichment) o.system.push(block);
    },

    "command.execute.before": async (i: any) => {
      if (typeof i?.sessionID !== "string" || typeof i?.command !== "string") return;
      const args = typeof i.arguments === "string" ? i.arguments.trim() : "";
      state(rootOf(i.sessionID)).command = `/${i.command}${args ? ` ${args}` : ""}`;
    },

    "chat.message": async (i: any, o: any) => {
      const sid = typeof i?.sessionID === "string" ? i.sessionID : "";
      if (!sid || parents.has(sid)) return;
      const s = state(sid);
      if (i.model?.modelID) s.model = String(i.model.modelID);
      await ensureStarted(sid);
      // A command expansion arrives as its whole template; record what the
      // user typed instead, as Claude Code's UserPromptSubmit does.
      const typed = s.command;
      s.command = undefined;
      const prompt =
        typed ??
        (Array.isArray(o?.parts) ? o.parts : [])
          .filter((p: any) => p?.type === "text" && !p.synthetic && typeof p.text === "string")
          .map((p: any) => p.text)
          .join("\n");
      if (!prompt.trim()) return;
      const reply = await call(
        "UserPromptSubmit",
        sid,
        {
          prompt,
          transcript_path: `${STORE}#${sid}`,
          turn_id: typeof i.messageID === "string" ? i.messageID : undefined,
        },
        BUDGET_MS.UserPromptSubmit,
      );
      const block = marked(reply.hookSpecificOutput?.additionalContext);
      if (block) s.enrichment.push(block);
    },

    "tool.execute.after": async (i: any, o: any) => {
      const native = typeof i?.tool === "string" ? i.tool : "";
      const sid = typeof i?.sessionID === "string" ? rootOf(i.sessionID) : "";
      if (!native || !sid) return;
      // Fire-and-forget telemetry — the tool loop never waits on capture;
      // only the turn's Stop does.
      const inflight = state(sid).inflight;
      const pending = call(
        "PostToolUse",
        sid,
        {
          tool_name: canonicalToolName(native),
          tool_input: canonicalToolInput(native, i.args),
          tool_response: { stdout: typeof o?.output === "string" ? o.output : "", stderr: "" },
          tool_use_id: typeof i.callID === "string" ? i.callID : undefined,
        },
        BUDGET_MS.PostToolUse,
      ).finally(() => inflight.delete(pending));
      inflight.add(pending);
    },

    "shell.env": async (i: any, o: any) => {
      if (!o?.env) return;
      o.env.THINKWEAVE_HARNESS = HARNESS;
      if (typeof i?.sessionID === "string" && i.sessionID) {
        o.env.OPENCODE_SESSION_ID = rootOf(i.sessionID);
      }
    },
  };
}
