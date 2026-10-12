// Pure presentation boundaries shared by the browser and deterministic tests.
export const ACTIVE = new Set([
  "ARMED",
  "RUNNING",
  "RESTORING",
  "VERIFYING_RECOVERY",
  "NEEDS_ATTENTION",
]);
/**
 * Reasons a live run is blocked (fixture mode, stale state, no plan, capability not live, execution
 * off, no isolation, baseline or watchdog).
 */
export function runGate(readiness, capability, mode, plan, stale) {
  const reasons = [...(readiness?.blockers || [])];
  if (mode !== "controller")
    reasons.push("Fixture mode cannot execute a real fault.");
  if (stale) reasons.push("Controller state is unavailable or stale.");
  if (!plan) reasons.push("Validate a plan first.");
  if (!capability || !readiness?.live_capabilities?.includes(capability.id))
    reasons.push("Capability is not live-enabled by the controller.");
  if (readiness?.execution_enabled !== true)
    reasons.push("Execution is disabled.");
  if (readiness?.isolation_approved !== true)
    reasons.push("Dedicated sandbox is not approved.");
  if (readiness?.baseline_healthy !== true)
    reasons.push("A fresh healthy baseline is required.");
  if (readiness?.watchdog_ready !== true)
    reasons.push("Recovery watchdog is not ready.");
  return [...new Set(reasons)];
}
/**
 * Merge newly received events into the saved ones: dedupe by sequence, reject conflicts or gaps,
 * return them in order.
 */
export function mergeEvents(existing, incoming, runId) {
  const map = new Map(existing.map((e) => [e.seq, e]));
  for (const event of incoming) {
    if (event.run_id !== runId || !Number.isInteger(event.seq) || event.seq < 1)
      throw Error("Invalid event identity or sequence.");
    if (
      map.has(event.seq) &&
      JSON.stringify(map.get(event.seq)) !== JSON.stringify(event)
    )
      throw Error("Conflicting saved event.");
    map.set(event.seq, event);
  }
  const events = [...map.values()].sort((a, b) => a.seq - b.seq);
  if (events.some((e, i) => e.seq !== i + 1))
    throw Error("Event gap; reload the saved timeline.");
  return events;
}
/** Human label for a run's cleanup and recovery status. */
export function recoveryLabel(run) {
  if (!run) return "No run selected";
  if (run.cleanup_verified !== true) return "Cleanup unverified";
  return run.recovery_outcome === "verified"
    ? "Recovery verified"
    : "Recovery unverified";
}
/**
 * Reasons a patch cannot be deployed yet (state, diff, tests, approval, deployment gates,
 * recovery).
 */
export function patchGate(review, run, mode, stale) {
  const reasons = [];
  if (mode !== "controller" || stale)
    reasons.push("Connect to current controller state.");
  if (!review?.patch || !review?.diff)
    reasons.push("Exact patch and diff required.");
  if (
    review?.tests?.passed !== true ||
    !review?.tests?.checks?.includes("application-tests")
  )
    reasons.push("Application tests have not passed.");
  if (
    review?.approval?.diff_sha256 !== review?.patch?.diff_sha256 ||
    !review?.approval
  )
    reasons.push("This patch revision is not approved.");
  if (review?.deployment_ready !== true)
    reasons.push("Controller deployment gates are blocked.");
  if (
    !run ||
    ACTIVE.has(run.state) ||
    run.cleanup_verified !== true ||
    run.recovery_outcome !== "verified"
  )
    reasons.push("Wait for verified cleanup and recovery.");
  return reasons;
}
/** Human label for a replay comparison result. */
export function comparisonLabel(comparison, fixture = false) {
  if (fixture) return "Illustrative comparison";
  if (comparison?.conclusive !== true) return "Unverified comparison";
  return comparison.outcome === "improved"
    ? "Measured improvement"
    : "No verified improvement";
}
