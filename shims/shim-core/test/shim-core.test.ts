// Conformance suite for shim-core (#194). The load-bearing contract is the
// error policy: a dead, hanging, or garbage-spewing Python side must NEVER
// wedge the harness — every runHook call resolves within its budget with a
// usable fallback, never throws, never leaves handles holding the event loop.
import { test } from "node:test";
import assert from "node:assert/strict";
import { chmodSync, mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import {
  CANONICAL_EVENTS,
  DEFAULT_TIMEOUT_MS,
  EVENT_PHASES,
  createFirstCallGuard,
  runHook,
} from "../src/index.js";

// --- cross-language parity ---------------------------------------------------
// canonical-events.json is the one fixture both suites pin against; the
// pytest side checks it equals core.harness.CANONICAL_EVENTS and the argv
// phases authored in hooks/hooks.json. Together the two suites make it
// impossible for the TS vocabulary and the Python normaliser to drift apart
// without a test going red somewhere.

const fixture: Record<string, string> = JSON.parse(
  readFileSync(new URL("../../canonical-events.json", import.meta.url), "utf-8"),
);

test("CANONICAL_EVENTS matches the shared fixture (names and order)", () => {
  assert.deepEqual([...CANONICAL_EVENTS], Object.keys(fixture));
});

test("EVENT_PHASES matches the shared fixture exactly", () => {
  assert.deepEqual(EVENT_PHASES, fixture);
});

test("every canonical event has a default timeout budget", () => {
  assert.deepEqual(Object.keys(DEFAULT_TIMEOUT_MS).sort(), [...CANONICAL_EVENTS].sort());
  for (const ms of Object.values(DEFAULT_TIMEOUT_MS)) {
    assert.ok(ms > 0);
  }
});

// --- runHook subprocess bridge ----------------------------------------------

const dir = mkdtempSync(join(tmpdir(), "shim-core-"));

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

// --- first-call guard --------------------------------------------------------

test("createFirstCallGuard: true once per key, independent across guards", () => {
  const guard = createFirstCallGuard();
  assert.equal(guard("ses-1"), true);
  assert.equal(guard("ses-1"), false);
  assert.equal(guard("ses-2"), true);
  assert.equal(createFirstCallGuard()("ses-1"), true);
});
