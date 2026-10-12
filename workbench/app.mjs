import { WorkbenchAPI } from "./api.mjs";
import {
  ACTIVE,
  runGate,
  mergeEvents,
  recoveryLabel,
  patchGate,
  comparisonLabel,
} from "./state.mjs";
const api = new WorkbenchAPI();
const content = document.querySelector("#content");
const savedRun = sessionStorage.getItem("workbench-run");
const state = {
  mode: "unavailable",
  readiness: null,
  capabilities: [],
  history: [],
  run: null,
  events: [],
  observations: [],
  diagnosis: null,
  review: null,
  comparison: null,
  plan: null,
  stale: true,
  busy: false,
  selected: savedRun,
};
/** Escape a value for safe insertion into HTML. */
const esc = (v) =>
  String(v ?? "").replace(
    /[&<>"']/g,
    (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[
        c
      ],
  );
/** Pretty-printed, HTML-escaped JSON. */
const json = (value) => esc(JSON.stringify(value, null, 2));
/** Show a notice message. */
const note = (text) => {
  document.querySelector("#notice").textContent = text;
};
/** A stable idempotency key per action for this browser session. */
const actionKey = (action) => {
  const key = "workbench-key-" + action;
  if (!sessionStorage.getItem(key))
    sessionStorage.setItem(key, crypto.randomUUID());
  return sessionStorage.getItem(key);
};
/** HTML for a button. */
function button(id, label, disabled = false, klass = "") {
  return `<button type="${id === "validate" ? "submit" : "button"}" id="${id}" class="${klass}" ${disabled ? "disabled" : ""}>${label}</button>`;
}
/** HTML listing the reasons an action is blocked (empty when none). */
function blockers(reasons) {
  return reasons.length
    ? `<div class="blocked"><strong>Before continuing</strong><ul>${reasons.map((r) => `<li>${esc(r)}</li>`).join("")}</ul></div>`
    : "";
}
/** HTML definition list of label/value pairs. */
function facts(rows) {
  return `<dl class="facts">${rows.map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join("")}</dl>`;
}
/** Page heading, marked as illustrative in fixture mode. */
function heading(title, description) {
  return `<p class="eyebrow">${state.mode === "fixture" ? "ILLUSTRATIVE DATA · NO REAL INJECTION" : "PAYMENT EXPERIMENTS"}</p><h1>${title}</h1><p class="lead">${description}</p>`;
}
/** The capability selected in the form. */
function capability() {
  return (
    state.capabilities.find(
      (c) => c.id === document.querySelector("#capability")?.value,
    ) ||
    state.capabilities.find((c) => c.id === state.plan?.capability_id) ||
    state.capabilities[0]
  );
}
/** Render the plan page: capability, parameters, scope, gates and the validate / run controls. */
function planView() {
  const cap =
    state.capabilities.find((c) => c.id === state.plan?.capability_id) ||
    state.capabilities[0];
  content.innerHTML =
    heading(
      "Plan an experiment",
      "Select one payment failure, inspect its scope and check the execution prerequisites.",
    ) +
    `<div class="columns"><section><form id="plan-form"><label for="capability">Failure scenario</label><select id="capability">${state.capabilities.map((c) => `<option value="${esc(c.id)}" ${c.id === cap?.id ? "selected" : ""}>${esc(c.title)} (${esc(c.execution)})</option>`).join("")}</select><div id="parameters"></div><label for="request">What do you want to learn?</label><textarea id="request" maxlength="500" required placeholder="Check whether checkout explains a payment service failure and retains the cart.">${esc(state.plan?.expected_impact || "Check payment failure handling while catalog and cart remain healthy.")}</textarea><label for="duration">Duration in seconds</label><input id="duration" type="number" min="0" max="30" value="${state.plan?.duration_seconds || 15}" required><div class="actions">${button("validate", "Validate plan", state.busy, "primary")}${button("execute", "Run experiment", true)}</div></form><div id="run-blockers"></div><h2>Validated plan</h2><pre id="plan-record">${state.plan ? json(state.plan) : "No validated plan yet."}</pre></section><aside class="inspector"><h2>Execution readiness</h2>${facts(
      [
        [
          "Environment",
          state.readiness?.sandbox?.compose_project || "Unavailable",
        ],
        [
          "Isolation",
          state.readiness?.isolation_approved ? "Approved" : "Unapproved",
        ],
        [
          "Baseline",
          state.readiness?.baseline_healthy ? "Healthy and fresh" : "Not ready",
        ],
        ["Watchdog", state.readiness?.watchdog_ready ? "Ready" : "Not ready"],
        ["Source", state.mode],
      ],
    )}<div class="actions">${button("baseline", "Capture healthy baseline", state.mode !== "controller" || state.busy || state.history.some(r => !["RECOVERED", "ABORTED", "FAILED"].includes(r.state)))}</div><p class="muted">${esc(state.readiness?.baseline_job?.state || "No baseline capture running")}</p><h2>Scope and recovery</h2><div id="scope"></div></aside></div>`;
  /** Render the selected capability's parameters and scope. */
  function scope() {
    const c = capability();
    document.querySelector("#parameters").innerHTML = (c?.parameters || [])
      .map(
        (p) =>
          `<label for="param-${esc(p.name)}">${esc(p.description)}</label>${p.type === "enum" ? `<select data-param="${esc(p.name)}" id="param-${esc(p.name)}">${p.choices.map((v) => `<option ${state.plan?.parameters?.[p.name] === v ? "selected" : ""}>${esc(v)}</option>`).join("")}</select>` : `<input data-param="${esc(p.name)}" data-type="${esc(p.type)}" id="param-${esc(p.name)}" type="${p.type === "int" ? "number" : "checkbox"}" ${p.type === "int" ? `min="${p.minimum}" max="${p.maximum}" value="${p.default}"` : p.default ? "checked" : ""}>`}`,
      )
      .join("");
    document.querySelector("#scope").innerHTML = c
      ? facts([
          ["Target", c.target_service],
          ["Path", c.affected_dependency || "Payment handler"],
          ["Restore", c.restoration],
          ["Availability", c.disabled_reason || c.execution],
        ])
      : "No capabilities available.";
    gates();
  }
  /** Render why a live run is blocked and enable Run only when no gate blocks. */
  function gates() {
    const reasons = runGate(
      state.readiness,
      capability(),
      state.mode,
      state.plan,
      state.stale,
    );
    document.querySelector("#run-blockers").innerHTML = blockers(reasons);
    document.querySelector("#execute").disabled =
      state.busy || reasons.length > 0;
  }
  document.querySelector("#capability").onchange = () => {
    state.plan = null;
    document.querySelector("#plan-record").textContent =
      "Validate this new selection.";
    scope();
  };
  document.querySelector("#plan-form").oninput = () => {
    state.plan = null;
    gates();
    document.querySelector("#plan-record").textContent =
      "Inputs changed. Validate again.";
  };
  document.querySelector("#plan-form").onsubmit = async (e) => {
    e.preventDefault();
    await perform(async () => {
      const parameters = {};
      document.querySelectorAll("[data-param]").forEach((el) => {
        parameters[el.dataset.param] =
          el.dataset.type === "int"
            ? Number(el.value)
            : el.dataset.type === "bool"
              ? el.checked
              : el.value;
      });
      const result = await api.call("/plans/validate", {
        capability_id: capability()?.id,
        parameters,
        duration_seconds: Number(document.querySelector("#duration").value),
        expected_impact: document.querySelector("#request").value,
      });
      state.plan = result.plan;
      document.querySelector("#plan-record").textContent = JSON.stringify(
        state.plan,
        null,
        2,
      );
    });
    gates();
  };
  document.querySelector("#execute").onclick = async () => {
    if (
      runGate(
        state.readiness,
        capability(),
        state.mode,
        state.plan,
        state.stale,
      ).length
    )
      return;
    const result = await mutate("run-" + state.plan.plan_id, "/runs", { plan: state.plan });
    if (!result?.run_id) return;
    state.selected = result.run_id;
    state.events = [];
    sessionStorage.setItem("workbench-run", result.run_id);
    await refresh();
    location.hash = "run";
  };
  document.querySelector("#baseline").onclick = async () => {
    await mutate("baseline", "/baselines", {});
    await refresh();
  };
  scope();
}
/** Render the selected run: state, recovery, timeline, observations and diagnosis. */
function runView() {
  const run = state.run;
  content.innerHTML =
    heading(
      "Run & evidence",
      "Follow the saved timeline. Ending a fault and verifying recovery are separate steps.",
    ) +
    (!run
      ? '<p class="empty">Select a saved run from History.</p>'
      : `<div class="split"><div><h2>${esc(run.run_id)}</h2><p>${esc(run.state)} · ${esc(recoveryLabel(run))}</p></div>${button("abort", "Request emergency restore", !ACTIVE.has(run.state) || state.mode !== "controller" || state.busy, "danger")}</div>${state.stale ? blockers(["Connection lost. State may be stale; backend recovery supervision must continue."]) : ""}${run.state === "NEEDS_ATTENTION" ? blockers(["Cleanup could not be verified. New runs and code deployment must remain blocked."]) : ""}<div class="columns"><section><h2>Ordered timeline</h2><ol class="timeline">${state.events.map((e) => `<li><span>${e.seq}</span><span>${esc(e.state)}</span><span>${esc(e.type)}<br><span class="muted">${esc(e.at)}</span><br>${esc(e.data?.message || "")}</span></li>`).join("")}</ol><h2>Observations</h2><div class="scroll"><table><thead><tr><th>Probe / phase</th><th>Outcome</th><th>Latency</th><th>Evidence quality</th></tr></thead><tbody>${state.observations.map((o) => `<tr><td>${esc(o.probe)}<br><span class="muted">${esc(o.phase)} · ${esc(o.observation_id)}</span></td><td>${esc(o.outcome)}${o.http_status ? " / " + o.http_status : ""}</td><td>${o.latency_ms == null ? "Not measured" : esc(o.latency_ms) + " ms"}</td><td>${o.fresh ? "Fresh" : "Stale"} · ${o.complete ? "Complete" : "Incomplete"}<br>${o.trace_id ? "Trace " + esc(o.trace_id) : "No trace reference"}</td></tr>`).join("")}</tbody></table></div></section><aside class="inspector"><h2>Recovery</h2>${facts(
          [
            ["Cleanup", run.cleanup_verified ? "Verified" : "Unverified"],
            ["Recovery", run.recovery_outcome || "Not observed"],
            ["Stop reason", run.stop_reason || "None"],
            [
              "Protected paths",
              state.readiness?.protected_path_status || "Not observed",
            ],
          ],
        )}<h2>Diagnosis</h2>${
          state.diagnosis
            ? facts([
                ["Category", state.diagnosis.category],
                ["Service", state.diagnosis.service || "Unknown"],
                ["Uncertainty", state.diagnosis.uncertainty],
                [
                  "Citations",
                  state.diagnosis.cited_observations.join(", ") || "None",
                ],
                ["Next action", state.diagnosis.next_action],
              ])
            : "<p>No diagnosis available.</p>"
        }<div class="actions">${button("diagnose", "Diagnose with rules", state.mode !== "controller" || state.busy || !run.terminal)}${button("prepare-patch", "Prepare candidate fix", state.mode !== "controller" || state.busy || !run.terminal)}</div><p class="muted">Candidate fixes require review and application tests. Diagnosis uses observed evidence and can report uncertainty.</p><h2>Saved references</h2><ul>${run.evidence_refs.map((r) => `<li>${r.startsWith("/api/v1/") ? `<a href="${esc(r)}" target="_blank" rel="noopener">${esc(r)}</a>` : esc(r)}</li>`).join("") || "<li>No saved evidence</li>"}</ul></aside></div>`);
  for (const [id, action] of [["diagnose", "diagnosis"], ["prepare-patch", "patch"]]) {
    document.querySelector("#" + id)?.addEventListener("click", async () => {
      await mutate(action + "-" + run.run_id, `/runs/${encodeURIComponent(run.run_id)}/${action}`, {});
      await refresh();
      if (action === "patch" && state.review) location.hash = "patch";
    });
  }
  document.querySelector("#abort")?.addEventListener("click", async () => {
    await mutate(
      "abort-" + run.run_id,
      `/runs/${encodeURIComponent(run.run_id)}/abort`,
      { reason: "manual_abort" },
    );
    await refresh();
  });
}
/** Render the list of saved runs. */
function historyView() {
  content.innerHTML =
    heading(
      "Saved history",
      "Reload authoritative run state and resume events from the last recorded sequence.",
    ) +
    `<div class="scroll"><table><thead><tr><th>Run</th><th>State</th><th>Recovery</th><th></th></tr></thead><tbody>${state.history.map((r) => `<tr><td>${esc(r.run_id)}</td><td>${esc(r.state)}</td><td>${esc(recoveryLabel(r))}</td><td><button data-run="${esc(r.run_id)}">Inspect run</button></td></tr>`).join("") || '<tr><td colspan="4">No saved runs.</td></tr>'}</tbody></table></div>`;
  document.querySelectorAll("[data-run]").forEach(
    (b) =>
      (b.onclick = async () => {
        state.selected = b.dataset.run;
        state.events = [];
        sessionStorage.setItem("workbench-run", state.selected);
        await refresh();
        location.hash = "run";
      }),
  );
}
/** Render the patch review page and its approval gates. */
function patchView() {
  const reviewerName = document.querySelector("#reviewer")?.value || "";
  const review = state.review;
  const reasons = patchGate(review, state.run, state.mode, state.stale);
  content.innerHTML =
    heading(
      "Review a fix",
      "Inspect the exact change and its tests. Approval is tied to one revision and one sandbox.",
    ) +
    (!review
      ? '<p class="empty">No patch proposed for this run.</p>'
      : `<div class="columns"><section>${facts([
          ["Patch", review.patch.patch_id],
          ["Base revision", review.patch.base_revision],
          ["Diff SHA-256", review.patch.diff_sha256],
          ["Affected files", review.patch.affected_files.join(", ")],
          ["Rationale", review.patch.rationale],
        ])}<h2>Exact proposed diff</h2><pre>${esc(review.diff)}</pre><label for="reviewer">Reviewer name</label><input id="reviewer" maxlength="100" value="${esc(reviewerName)}" placeholder="Your name"><div class="actions">${button("approve", "Approve this revision", state.mode !== "controller" || state.stale || review.tests?.passed !== true || !review.tests?.checks?.includes("application-tests"))}${button("deploy", "Apply to sandbox", reasons.length > 0 || state.busy, "primary")}</div>${blockers(reasons)}</section><aside class="inspector"><h2>Verification</h2>${facts(
          [
            ["Tests", review.tests?.passed ? "Passed" : "Not passed / not run"],
            ["Approval", review.approval ? "Recorded" : "Not approved"],
            ["Deployment", review.deployment?.outcome || "Not deployed"],
            [
              "Rollback",
              review.deployment?.rollback_verified
                ? "Verified"
                : "Not observed",
            ],
          ],
        )}<h2>Test details</h2><pre>${json(review.tests)}</pre><p class="lead">Restoring a feature flag ends the experiment. Applying this patch changes application code and requires a separate approval.</p></aside></div>`);
  document.querySelector("#approve")?.addEventListener("click", async () => {
    const reviewer = document.querySelector("#reviewer").value.trim();
    if (!reviewer) {
      note("Enter a reviewer name.");
      return;
    }
    await mutate(
      "approve-" + review.patch.diff_sha256,
      `/patches/${review.patch.patch_id}/approve`,
      {
        diff_sha256: review.patch.diff_sha256,
        base_revision: review.patch.base_revision,
        tests_digest: review.tests.digest,
        sandbox: state.readiness.sandbox,
        reviewer,
      },
    );
    await refresh();
  });
  document.querySelector("#deploy")?.addEventListener("click", async () => {
    if (patchGate(review, state.run, state.mode, state.stale).length) return;
    await mutate(
      "deploy-" + review.patch.diff_sha256,
      `/patches/${review.patch.patch_id}/deploy`,
      { diff_sha256: review.patch.diff_sha256, approval: review.approval },
    );
    await refresh();
  });
}
/** Render the comparison between the original run and its replay. */
function compareView() {
  const c = state.comparison;
  content.innerHTML =
    heading(
      "Compare a replay",
      "A persistent payment outage must still fail payment. Improvement means accurate errors, bounded responses and preserved state.",
    ) +
    (!c
      ? '<p class="empty">No matched replay available.</p>'
      : `<h2>${esc(comparisonLabel(c, state.mode === "fixture"))}</h2>${facts([
          ["Original run", c.original_run_id],
          ["Replay run", c.replay_run_id || "Not recorded"],
          ["Outcome", c.outcome || "Unverified"],
        ])}<div class="scroll"><table><thead><tr><th>Measure</th><th>Before</th><th>After</th></tr></thead><tbody>${Object.entries(
          c.differences || {},
        )
          .map(
            ([k, v]) =>
              `<tr><td>${esc(k)}</td><td>${esc(v.before)}</td><td>${esc(v.after)}</td></tr>`,
          )
          .join("")}</tbody></table></div>${blockers(c.limitations || [])}`);
}
/** Render the page for the current URL hash and mark the active nav link. */
function render() {
  const page = (location.hash || "#plan").slice(1);
  document
    .querySelectorAll("nav a")
    .forEach((a) =>
      a.setAttribute("aria-current", a.hash === "#" + page ? "page" : "false"),
    );
  (
    ({
      plan: planView,
      run: runView,
      history: historyView,
      patch: patchView,
      compare: compareView,
    })[page] || planView
  )();
}
/** Run an action once at a time, showing any error as a notice. */
async function perform(fn) {
  if (state.busy) return;
  state.busy = true;
  try {
    return await fn();
    note("");
  } catch (e) {
    note(e.message);
    state.stale = true;
  } finally {
    state.busy = false;
  }
}
/** Send a mutating request with an idempotency key (refused in fixture mode). */
async function mutate(key, path, body) {
  if (state.mode !== "controller") {
    note("Fixture actions cannot mutate the application.");
    return;
  }
  return await perform(async () => {
    const result = await api.call(path, body, actionKey(key));
    sessionStorage.removeItem("workbench-key-" + key);
    return result;
  });
}
/** Reload the controller snapshot and the selected run, then re-render. */
async function refresh() {
  try {
    const snapshot = await api.call("");
    state.mode = snapshot.mode;
    state.readiness = snapshot.readiness;
    state.capabilities = snapshot.capabilities;
    state.history = snapshot.history;
    state.stale = false;
    const id = state.history.some(r => r.run_id === state.selected) ? state.selected : state.history[0]?.run_id;
    if (id !== state.selected) state.events = [];
    if (!id) { state.selected = null; state.run = null; state.events = []; state.observations = []; state.diagnosis = null; state.review = null; state.comparison = null; }
    if (id) {
      state.selected = id;
      sessionStorage.setItem("workbench-run", id);
      const detail = await api.call("/runs/" + encodeURIComponent(id));
      state.run = detail.run;
      state.observations = detail.observations || [];
      state.diagnosis = detail.diagnosis;
      state.review = detail.patch_review;
      state.comparison = detail.comparison;
      const incoming = await api.call(
        `/runs/${encodeURIComponent(id)}/events?after_seq=${state.events.at(-1)?.seq || 0}`,
      );
      try {
        state.events = mergeEvents(state.events, incoming.events, id);
      } catch {
        const all = await api.call(
          `/runs/${encodeURIComponent(id)}/events?after_seq=0`,
        );
        state.events = mergeEvents([], all.events, id);
      }
    }
    document.querySelector("#mode").textContent =
      state.mode === "fixture"
        ? "Fixture mode · no live actions"
        : "Controller connected";
    document.querySelector("#updated").textContent =
      "State loaded " + new Date().toLocaleTimeString();
    note(
      state.mode === "fixture"
        ? "Sample records for UI development. These are not measured experiment results."
        : "",
    );
  } catch (e) {
    state.stale = true;
    document.querySelector("#mode").textContent = "Connection unavailable";
    note(e.message + " Saved state may be stale.");
  }
  render();
}
document.querySelector("#refresh").onclick = refresh;
window.addEventListener("hashchange", render);
await refresh();
// Poll authoritative state on operational views only, preserving unsent planning input.
setInterval(() => {
  if (
    ["#run", "#history", "#patch", "#compare"].includes(location.hash) &&
    !state.busy
  )
    refresh();
}, 5000);
