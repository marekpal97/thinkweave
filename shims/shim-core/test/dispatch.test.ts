// dispatch under the rsi-rail registration set (dec-4f158b58 layers 2 and 3):
// a disabled event never reaches the launcher, a foreign hook runs after it
// with the same envelope, inside the same budget, and nothing ever rejects.
import { after, test } from "node:test";
import assert from "node:assert/strict";
import { chmodSync, existsSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { dispatch, type RunHookFailure } from "../src/index.js";

const dir = mkdtempSync(join(tmpdir(), "shim-dispatch-"));
after(() => rmSync(dir, { recursive: true, force: true }));

function script(name: string, body: string): string {
  const path = join(dir, name);
  writeFileSync(path, `#!/usr/bin/env node\n${body}`);
  chmodSync(path, 0o755);
  return path;
}

// Appends one line per call — its argv phase — to `<name>.log`, replies {}.
function recorder(name: string): { path: string; log: string } {
  const log = join(dir, `${name}.log`);
  const path = script(
    name,
    `const fs = require("node:fs");
let d = "";
process.stdin.on("data", (c) => (d += c));
process.stdin.on("end", () => {
  const p = JSON.parse(d || "{}");
  fs.appendFileSync(${JSON.stringify(log)}, (process.argv[2] ?? p.hook_event_name) + "\\n");
  process.stdout.write("{}");
});`,
  );
  return { path, log };
}

function lines(log: string): string[] {
  return existsSync(log) ? readFileSync(log, "utf-8").trim().split("\n").filter(Boolean) : [];
}

test("a disabled event is not dispatched; an enabled one is", async () => {
  const launcher = recorder("launcher-a");
  const rail = { events: ["Stop" as const], foreign: [] };
  await dispatch("UserPromptSubmit", { prompt: "hi" }, { launcher: launcher.path, rail, timeoutMs: 5000 });
  assert.deepEqual(lines(launcher.log), []);
  await dispatch("Stop", {}, { launcher: launcher.path, rail, timeoutMs: 5000 });
  assert.deepEqual(lines(launcher.log), ["stop"]);
});

test("a foreign hook on Stop runs once after the launcher, with the envelope", async () => {
  const launcher = recorder("launcher-b");
  const foreign = recorder("foreign-b");
  const rail = {
    foreign: [{ name: "note", event: "Stop" as const, matcher: "", script: foreign.path }],
  };
  await dispatch("Stop", { hook_event_name: "Stop" }, { launcher: launcher.path, rail, timeoutMs: 5000 });
  assert.deepEqual(lines(launcher.log), ["stop"]);
  assert.deepEqual(lines(foreign.log), ["Stop"]);
  await dispatch("SessionStart", { hook_event_name: "SessionStart" }, { launcher: launcher.path, rail, timeoutMs: 5000 });
  assert.deepEqual(lines(foreign.log), ["Stop"], "foreign hook is event-scoped");
});

test("a foreign hook runs even when the rail disables thinkweave's own dispatch", async () => {
  const launcher = recorder("launcher-c");
  const foreign = recorder("foreign-c");
  const rail = {
    events: [],
    foreign: [{ name: "note", event: "Stop" as const, matcher: "", script: foreign.path }],
  };
  await dispatch("Stop", { hook_event_name: "Stop" }, { launcher: launcher.path, rail, timeoutMs: 5000 });
  assert.deepEqual(lines(launcher.log), []);
  assert.deepEqual(lines(foreign.log), ["Stop"]);
});

test("matchers filter tool events for the launcher and for foreign hooks", async () => {
  const launcher = recorder("launcher-d");
  const foreign = recorder("foreign-d");
  const rail = {
    matchers: { PostToolUse: ["Write|Edit"] },
    foreign: [{ name: "bash", event: "PostToolUse" as const, matcher: "Bash", script: foreign.path }],
  };
  const opts = { launcher: launcher.path, rail, timeoutMs: 5000 };
  await dispatch("PostToolUse", { hook_event_name: "PostToolUse", tool_name: "Bash" }, opts);
  await dispatch("PostToolUse", { hook_event_name: "PostToolUse", tool_name: "Edit" }, opts);
  assert.deepEqual(lines(launcher.log), ["post_tool_use"]);
  assert.deepEqual(lines(foreign.log), ["PostToolUse"]);
});

test("a hanging foreign hook is reaped inside the budget and named", async () => {
  const launcher = recorder("launcher-e");
  const hang = script("hang", "setInterval(() => {}, 1000);");
  const failures: RunHookFailure[] = [];
  const started = Date.now();
  const reply = await dispatch("Stop", {}, {
    launcher: launcher.path,
    timeoutMs: 1500,
    rail: { foreign: [{ name: "slow", event: "Stop", matcher: "", script: hang }] },
    onFailure: (f) => failures.push(f),
  });
  assert.deepEqual(reply, {});
  assert.ok(Date.now() - started < 4000);
  assert.equal(failures.length, 1);
  assert.equal(failures[0].kind, "timeout");
  assert.equal(failures[0].message, "foreign hook slow");
});

test("no rail: dispatch is runHook", async () => {
  const launcher = recorder("launcher-f");
  await dispatch("Stop", {}, { launcher: launcher.path, timeoutMs: 5000 });
  assert.deepEqual(lines(launcher.log), ["stop"]);
});
