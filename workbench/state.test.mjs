import test from "node:test";
import assert from "node:assert/strict";
import {
  runGate,
  mergeEvents,
  recoveryLabel,
  patchGate,
  comparisonLabel,
} from "./state.mjs";
import { WorkbenchAPI } from "./api.mjs";
const ready = {
  blockers: [],
  execution_enabled: true,
  isolation_approved: true,
  baseline_healthy: true,
  watchdog_ready: true,
  live_capabilities: ["payment-unreachable"],
};
const cap = { id: "payment-unreachable" };
test("fixture and missing readiness cannot run even with an approved-looking plan", () => {
  assert.ok(runGate(ready, cap, "fixture", {}, false).length);
  assert.ok(runGate(null, cap, "controller", {}, false).length);
  assert.ok(runGate(ready, cap, "controller", {}, true).length);
  assert.deepEqual(runGate(ready, cap, "controller", {}, false), []);
});
test("event reconnect deduplicates, orders and refuses conflicting or missing events", () => {
  const a = { run_id: "run-01", seq: 1 };
  const b = { run_id: "run-01", seq: 2 };
  assert.deepEqual(mergeEvents([a], [b, a], "run-01"), [a, b]);
  assert.throws(() => mergeEvents([a], [{ ...a, state: "FAILED" }], "run-01"));
  assert.throws(() => mergeEvents([], [b], "run-01"));
  assert.throws(() => mergeEvents([], [{ ...a, run_id: "wrong" }], "run-01"));
});
test("recovered label needs cleanup evidence; fixture comparison never measured", () => {
  assert.equal(
    recoveryLabel({
      state: "RECOVERED",
      cleanup_verified: false,
      recovery_outcome: "verified",
    }),
    "Cleanup unverified",
  );
  assert.equal(
    comparisonLabel({ conclusive: true, outcome: "improved" }, true),
    "Illustrative comparison",
  );
  assert.equal(
    comparisonLabel({ conclusive: false, outcome: "improved" }),
    "Unverified comparison",
  );
});
test("patch review blocks changed approval, absent tests and pending recovery", () => {
  const review = {
    patch: { diff_sha256: "new" },
    diff: "exact",
    tests: { passed: true, checks: ["application-tests"] },
    approval: { diff_sha256: "old" },
    deployment_ready: true,
  };
  const run = {
    state: "RECOVERED",
    cleanup_verified: true,
    recovery_outcome: "verified",
  };
  assert.ok(patchGate(review, run, "controller", false).length);
  review.approval.diff_sha256 = "new";
  assert.deepEqual(patchGate(review, run, "controller", false), []);
  assert.ok(
    patchGate(review, { ...run, state: "NEEDS_ATTENTION" }, "controller", false)
      .length,
  );
  assert.ok(
    patchGate({ ...review, tests: { passed: false } }, run, "controller", false)
      .length,
  );
});
test("provider/API errors remain failures and mutation carries an idempotency key", async () => {
  const failing = new WorkbenchAPI(async () => ({
    ok: false,
    json: async () => ({ ok: false, message: "Provider unavailable" }),
  }));
  await assert.rejects(
    () => failing.call("/plans/validate", {}),
    /Provider unavailable/,
  );
  let options;
  const api = new WorkbenchAPI(async (_url, opts) => {
    options = opts;
    return { ok: true, json: async () => ({ ok: true, result: {} }) };
  });
  await api.call("/runs", {}, "req-01");
  assert.equal(options.headers["Idempotency-Key"], "req-01");
});
test("browser fetch is called without binding the API instance as its receiver", async () => {
  const api = new WorkbenchAPI(function () {
    assert.equal(this, undefined);
    return Promise.resolve({
      ok: true,
      json: async () => ({ ok: true, result: {} }),
    });
  });
  // Injected fetch functions also receive no accidental API-instance binding.
  await api.call("");
});
