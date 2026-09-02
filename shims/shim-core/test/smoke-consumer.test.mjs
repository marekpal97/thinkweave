// Smoke consumer: import the built package by name, exactly as the Pi (#114)
// and OpenCode (#195) shims will. Plain .mjs on purpose — it runs against
// dist/ through the package "exports" map (node self-reference), so it fails
// if the published entry point is wrong even while relative imports work.
import { test } from "node:test";
import assert from "node:assert/strict";

import { CANONICAL_EVENTS, createFirstCallGuard, runHook } from "@thinkweave/shim-core";

test("the package entry point exposes the shim surface", () => {
  assert.equal(typeof runHook, "function");
  assert.equal(typeof createFirstCallGuard, "function");
  assert.ok(CANONICAL_EVENTS.includes("SessionStart"));
});
