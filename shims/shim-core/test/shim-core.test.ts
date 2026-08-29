// Conformance suite for shim-core (#194). The load-bearing contract is the
// error policy: a dead, hanging, or garbage-spewing Python side must NEVER
// wedge the harness — every runHook call resolves within its budget with a
// usable fallback, never throws, never leaves handles holding the event loop.
import { after, test } from "node:test";
import assert from "node:assert/strict";
import { chmodSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import {
  CANONICAL_EVENTS,
  EVENT_PHASES,
  createFirstCallGuard,
  runHook,
} from "../src/index.js";

// --- cross-language parity ---------------------------------------------------
// canonical-events.json is the shared fixture; the pytest side pins the
// Python constants + hooks.json argv (see tests/test_shim_core.py).

const fixture: Record<string, string> = JSON.parse(
  readFileSync(new URL("../../canonical-events.json", import.meta.url), "utf-8"),
);

test("CANONICAL_EVENTS matches the shared fixture (names and order)", () => {
  assert.deepEqual([...CANONICAL_EVENTS], Object.keys(fixture));
});

test("EVENT_PHASES matches the shared fixture exactly", () => {
  assert.deepEqual(EVENT_PHASES, fixture);
});

// --- runHook subprocess bridge ----------------------------------------------

const dir = mkdtempSync(join(tmpdir(), "shim-core-"));
after(() => rmSync(dir, { recursive: true, force: true }));

function fakeLauncher(name: string, body: string): string {
  const path = join(dir, name);
  writeFileSync(path, `#!/usr/bin/env node\n${body}`);
  chmodSync(path, 0o755);
  return path;
}

const echoBack = fakeLauncher(
  "echo-back",
  `let d = "";
process.stdin.on("data", (c) => (d += c));
process.stdin.on("end", () => {
  process.stdout.write(JSON.stringify({ argv: process.argv.slice(2), payload: JSON.parse(d) }));
});`,
);

test("runHook spawns the launcher with the phase argv and payload on stdin", async () => {
  const res = await runHook(
    "SessionStart",
    { session_id: "s-1", cwd: "/tmp" },
    { launcher: echoBack },
  );
  assert.deepEqual(res, {
    argv: ["session_start"],
    payload: { session_id: "s-1", cwd: "/tmp" },
  });
});

test("runHook appends --harness <id> when given (the handler reads its own argv)", async () => {
  const res = (await runHook("PostToolUse", {}, { launcher: echoBack, harness: "pi" })) as {
    argv?: string[];
  };
  assert.deepEqual(res.argv, ["post_tool_use", "--harness", "pi"]);
});

test("multi-byte characters survive chunked stdout (>64KiB pipe boundary)", async () => {
  // A ~90KB response of 3-byte characters guarantees one straddles the 64KiB
  // pipe chunk boundary; per-chunk decoding would corrupt it to U+FFFD while
  // JSON.parse still succeeds — silent corruption of a SessionStart payload.
  const bigUnicode = fakeLauncher(
    "big-unicode",
    `process.stdin.resume();
process.stdin.on("end", () =>
  process.stdout.write(JSON.stringify({ pad: "\\u2192".repeat(30000) })),
);`,
  );
  const res = (await runHook("SessionStart", {}, { launcher: bigUnicode, timeoutMs: 5000 })) as {
    pad?: string;
  };
  assert.equal(res.pad, "→".repeat(30000));
});

test("a hanging launcher resolves with the fallback within the budget", async () => {
  const hang = fakeLauncher("hang", "setInterval(() => {}, 1000);");
  const started = Date.now();
  const res = await runHook("Stop", {}, { launcher: hang, timeoutMs: 250 });
  assert.deepEqual(res, {});
  assert.ok(Date.now() - started < 1250, "did not resolve within budget");
});

test("a missing launcher binary resolves with the fallback, never throws", async () => {
  const res = await runHook("Stop", {}, { launcher: join(dir, "no-such-binary") });
  assert.deepEqual(res, {});
});

test("garbage stdout resolves with the fallback", async () => {
  const garbage = fakeLauncher("garbage", 'process.stdout.write("not json at all");');
  assert.deepEqual(await runHook("Stop", {}, { launcher: garbage }), {});
});

test("a nonzero exit resolves with the fallback even if stdout parsed", async () => {
  const failing = fakeLauncher("failing", 'process.stdout.write("{}"); process.exit(3);');
  assert.deepEqual(await runHook("Stop", {}, { launcher: failing }), {});
});

test("non-object JSON on stdout resolves with the fallback", async () => {
  const scalar = fakeLauncher("scalar", 'process.stdout.write("42");');
  assert.deepEqual(await runHook("Stop", {}, { launcher: scalar }), {});
});

// --- diagnostic channel ------------------------------------------------------
// {} still conflates every failure with a genuine no-op on the value side —
// that is the never-wedge contract. onFailure is the discriminated side
// channel: invoked at most once per call with the kind (and bounded stderr,
// where the launcher's own diagnostics land — e.g. the first-run bootstrap
// notice), silent on success, and never able to break the caller.

import type { RunHookFailure } from "../src/index.js";

async function failureOf(
  launcher: string,
  timeoutMs = 800,
): Promise<{ failures: RunHookFailure[]; result: unknown }> {
  const failures: RunHookFailure[] = [];
  const result = await runHook("Stop", {}, {
    launcher,
    timeoutMs,
    onFailure: (f) => failures.push(f),
  });
  return { failures, result };
}

test("onFailure reports a timeout", async () => {
  const hang = fakeLauncher("hang-2", "setInterval(() => {}, 1000);");
  const { failures } = await failureOf(hang, 250);
  assert.equal(failures.length, 1);
  assert.equal(failures[0].kind, "timeout");
});

test("onFailure reports a missing binary as spawn-error", async () => {
  const { failures } = await failureOf(join(dir, "no-such-binary"));
  assert.equal(failures.length, 1);
  assert.equal(failures[0].kind, "spawn-error");
});

test("onFailure reports a nonzero exit with its code and bounded stderr", async () => {
  const failing = fakeLauncher(
    "failing-stderr",
    'process.stderr.write("first-run bootstrap: something slow\\n"); process.exit(127);',
  );
  const { failures } = await failureOf(failing);
  assert.equal(failures.length, 1);
  assert.equal(failures[0].kind, "exit");
  assert.equal(failures[0].code, 127);
  assert.match(failures[0].stderr, /first-run bootstrap/);
});

test("onFailure reports unparseable stdout as bad-output", async () => {
  const garbage = fakeLauncher("garbage-2", 'process.stdout.write("not json");');
  const { failures } = await failureOf(garbage);
  assert.equal(failures.length, 1);
  assert.equal(failures[0].kind, "bad-output");
});

test("onFailure stays silent on success", async () => {
  const failures: RunHookFailure[] = [];
  const res = await runHook("Stop", { session_id: "s" }, {
    launcher: echoBack,
    onFailure: (f) => failures.push(f),
  });
  assert.deepEqual(failures, []);
  assert.ok((res as { argv?: unknown }).argv);
});

test("a throwing onFailure never reaches the caller", async () => {
  const res = await runHook("Stop", {}, {
    launcher: join(dir, "no-such-binary"),
    onFailure: () => {
      throw new Error("shim bug");
    },
  });
  assert.deepEqual(res, {});
});

test("the timeout path actually kills the child", async () => {
  const pidFile = join(dir, "hang-pid.txt");
  const hangPid = fakeLauncher(
    "hang-pid",
    `require("node:fs").writeFileSync(${JSON.stringify(pidFile)}, String(process.pid));
setInterval(() => {}, 1000);`,
  );
  await runHook("Stop", {}, { launcher: hangPid, timeoutMs: 250 });
  const pid = Number(readFileSync(pidFile, "utf-8"));
  // SIGKILL delivery is asynchronous; give it a moment, then require ESRCH.
  await new Promise((r) => setTimeout(r, 200));
  assert.throws(() => process.kill(pid, 0), /ESRCH/);
});

// --- first-call guard --------------------------------------------------------

test("createFirstCallGuard: true once per key, independent across guards", () => {
  const guard = createFirstCallGuard();
  assert.equal(guard("ses-1"), true);
  assert.equal(guard("ses-1"), false);
  assert.equal(guard("ses-2"), true);
  assert.equal(createFirstCallGuard()("ses-1"), true);
});
