/* Offline client regressions. No browser claim: a minimal DOM and deferred fetch are injected. */
"use strict";
const assert = require("node:assert/strict");
const crypto = require("node:crypto");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const root = path.resolve(__dirname, "../../..");
class Element {
  constructor(id = "", type = "") {
    this.id = id; this.type = type; this.value = ""; this.defaultValue = "";
    this.children = []; this.listeners = {}; this.dataset = {}; this.fields = {}; this.queries = {};
    this.hidden = false; this.disabled = false; this.checked = false; this._text = "";
  }
  set textContent(text) { this._text = text; this.children = []; }
  get textContent() { return this._text || this.children.map(e => e.textContent).join(""); }
  setAttribute(name, value) { this[name] = value; }
  removeAttribute(name) { delete this[name]; }
  addEventListener(name, fn) { this.listeners[name] = fn; }
  fire(name) { if (this.listeners[name]) this.listeners[name]({preventDefault() {}}); }
  appendChild(node) { this.children.push(node); return node; }
  removeChild(node) { this.children.splice(this.children.indexOf(node), 1); }
  get firstChild() { return this.children[0]; }
  get selectedOptions() { return this.children.filter(node => node.selected); }
  contains() { return false; }
  showModal() { this.open = true; }
  close() { this.open = false; }
  focus() {}
  reset() { for (const field of Object.values(this.fields)) { field.value = field.defaultValue; field.checked = false; } }
  querySelector(selector) {
    const match = selector.match(/^\[name="([^"]+)"\](:checked)?$/);
    if (match) { const field = this.fields[match[1]]; return field && (!match[2] || field.checked) ? field : null; }
    return this.queries[selector] ||= new Element();
  }
  querySelectorAll(selector) {
    if (selector === "input, select, textarea, button") return [...Object.values(this.fields), this.querySelector('button[type="submit"]')];
    if (selector === '[name="system_ids"]:checked') return this.systems || [];
    return [];
  }
}
function field(form, name, value = "", type = "text") {
  const node = new Element(name, type); node.value = value; node.defaultValue = value;
  form.fields[name] = node; return node;
}
function environment(config, forms = []) {
  const elements = {}, requests = [], sockets = [], timers = [];
  const document = {
    getElementById(id) {
      if (((config.state && config.state !== "OPEN") || config.verification_for_deficiency_id) && /^(odometer-form|odo-)/.test(id)) return null;
      if (!elements[id]) {
        elements[id] = new Element(id);
        if (id === "outcome-form") {
          for (const name of ["outcome_explanation", "category_id", "summary", "description"]) field(elements[id], name);
          field(elements[id], "severity", "HIGH", "radio");
        }
      }
      return elements[id];
    },
    querySelector() { return {value: "synthetic-csrf"}; },
    querySelectorAll(selector) { return selector === ".workflow-write" ? forms : []; },
    createElement(tag) { return new Element("", tag); },
    createTextNode(text) { const node = new Element(); node.textContent = text; return node; },
    addEventListener() {},
  };
  const context = {
    document, crypto: crypto.webcrypto, Uint8Array,
    location: {protocol: "https:", host: "example.invalid"},
    setTimeout(fn) { timers.push(fn); return timers.length; }, clearTimeout() {},
    WebSocket: class { constructor() { sockets.push(this); } },
    fetch(url, options = {}) {
      const request = {url, options, body: options.body ? JSON.parse(options.body) : null};
      requests.push(request);
      return new Promise((resolve, reject) => { request.resolve = resolve; request.reject = reject; });
    },
  };
  vm.createContext(context);
  return {context, document, elements, requests, sockets, timers,
    get: id => document.getElementById(id), posts: () => requests.filter(r => r.body),
    run(file, transform = source => source) { vm.runInContext(transform(fs.readFileSync(path.join(root, file), "utf8")), context); },
  };
}
const tick = () => new Promise(resolve => setImmediate(resolve));
function response(request, status, body) { request.resolve({ok: status < 400, status, json: async () => body}); }
function checklist(state = "OPEN", extra = {}) {
  const config = {state, view: "NORMAL", needs_attention_only: false, session_id: "session", vehicle_id: "vehicle", checklist_version_id: "version",
    urls: {checks: "/checks", checklist: "/checklist", suggestions: "/suggestions", deficiency_detail: "/deficiencies/DEFICIENCY/", socket: "/live", odometer: "/odometer"}};
  Object.assign(config, extra);
  const run = environment(config); run.get("cya-config").textContent = JSON.stringify(config);
  run.run("cya/inspections/templates/inspections/checklist.js", source => source.replace("  // ---- start ",
    "  globalThis.testUI={startCheck,retryCheck,openOutcome,askConflict,stateText,controls,reload,setRows(value){rows=value;},drafts,getEntry(row){return entries[key(row)];},odo,submitOdometer};\n  // ---- start "));
  return run;
}
const row = {item_id: "item", item_label: "Synthetic item", kit_id: "kit-a", kit_label: "Synthetic kit A", kind: "PRESENCE", checklist_version_id: "version", latest_check_id: "basis-a", latest_check_outcome: "PASSED", latest_check_at: "2026-06-01T00:00:00Z", deficiencies: [], allowed_outcomes: ["PASSED", "FAILED", "UNABLE_TO_INSPECT"]};
function newReport(run) {
  const form = run.get("outcome-form");
  for (const [name, value] of Object.entries({category_id: "category", summary: "Synthetic failure", description: "Synthetic description."})) form.fields[name].value = value;
  form.fields.severity.checked = true;
}
function generic(kind) {
  const form = new Element(); form.dataset.kind = kind; form.dataset.url = "/write";
  const button = form.querySelector('button[type="submit"]'); button.type = "submit"; button.textContent = "Save";
  field(form, "csrfmiddlewaretoken", "synthetic-csrf", "hidden");
  const config = {vehicle_id: "vehicle", suggestions_url: "/suggestions"};
  const run = environment(config, [form]); run.get("workflow-config").textContent = JSON.stringify(config);
  run.run("cya/inspections/templates/inspections/workflows.js");
  return {...run, form, button};
}
(async () => {
  for (const state of ["CLOSED", "INCOMPLETE"]) {
    const run = checklist(state); assert.equal(run.sockets.length, 1); assert(run.timers.length);
  }
  {
    const run = checklist(); run.context.testUI.setRows([row]); run.context.testUI.openOutcome(row, "FAILED");
    assert.equal(run.posts().length, 0); assert.equal(run.get("outcome-existing").value, "");
    response(run.requests[0], 200, {status: "OK", suggestions: [{deficiency_id: "existing", summary: "<script>Synthetic</script>", status: "OPEN"}]}); await tick();
    assert.equal(run.get("outcome-existing").value, ""); // a match never selects itself
    assert(run.get("outcome-existing").children[1].textContent.includes("<script>")); // remains text, never markup
    newReport(run); run.get("outcome-form").fire("submit");
    const failed = run.posts()[0]; assert.equal(failed.body.outcome, "FAILED"); assert.equal(failed.body.kit_id, "kit-a");
    assert.equal(failed.body.deficiency.create.severity, "HIGH");
    failed.reject(new Error("synthetic lost response")); await tick();
    run.context.testUI.retryCheck({...row, checklist_version_id: "different", unit: "changed"});
    assert.equal(run.posts()[1].options.body, failed.options.body); // exact frozen retry despite changed row
    response(run.posts()[1], 409, {status: "CONFLICT", newest_check: {check_id: "basis-b", outcome: "PASSED", actor: {display_name: "Synthetic Crew"}}}); await tick();
    run.context.testUI.setRows([]); run.get("conflict-proceed").fire("click");
    const proceed = run.posts()[2]; assert.equal(proceed.body.acknowledges_check_id, "basis-b"); assert.equal(proceed.body.basis_check_id, "basis-b");
    assert.notEqual(proceed.body.operation_id, failed.body.operation_id); assert.deepEqual(proceed.body.deficiency, failed.body.deficiency);
    response(proceed, 201, {status: "COMMITTED", result: {server_recorded_at: "2026-06-01T00:00:00Z"}}); await tick();
    assert(run.get("session-message").textContent.includes("was saved"));
  }
  {
    const run = checklist(); const other = {...row, item_id: "other-item"};
    run.context.testUI.startCheck(row, null, "FAILED", {create: {summary: "Synthetic first failure"}});
    run.context.testUI.startCheck(other, null, "PASSED");
    response(run.posts()[0], 409, {status: "CONFLICT", newest_check: {check_id: "warning-a", outcome: "PASSED"}});
    response(run.posts()[1], 409, {status: "CONFLICT", newest_check: {check_id: "warning-b", outcome: "PASSED"}}); await tick();
    assert.equal(run.get("conflict-dialog").newest, "warning-a");
    run.get("conflict-proceed").fire("click");
    assert.equal(run.posts()[2].body.acknowledges_check_id, "warning-a");
    assert.equal(run.posts()[2].body.deficiency.create.summary, "Synthetic first failure");
    assert.equal(run.get("conflict-dialog").newest, "warning-b");
    run.get("conflict-dialog").fire("cancel"); assert.equal(run.get("conflict-dialog").open, undefined);
  }
  {
    const run = checklist(); run.context.testUI.openOutcome(row, "UNABLE_TO_INSPECT"); newReport(run);
    run.get("outcome-form").fire("submit"); assert.equal(run.posts().length, 0); assert(run.get("outcome-error").textContent.includes("reason"));
    run.get("outcome-form").fields.outcome_explanation.value = "Synthetic access restriction.";
    run.get("outcome-existing").value = "existing"; run.get("outcome-form").fire("submit");
    const body = run.posts()[0].body; assert.equal(body.outcome, "UNABLE_TO_INSPECT"); assert.equal(body.explanation, "Synthetic access restriction.");
    assert.deepEqual(body.deficiency, {existing_id: "existing"}); assert(!("quantity" in body)); assert(!("measurement" in body));
  }
  {
    const run = checklist(); const quantity = {...row, kind: "QUANTITY", expected_quantity: 6};
    run.context.testUI.drafts["item|kit-a"] = "2"; run.context.testUI.openOutcome(quantity, "FAILED"); newReport(run); run.get("outcome-form").fire("submit");
    assert.equal(run.posts()[0].body.quantity, 2);
    assert.equal(run.context.testUI.stateText({...row, obligation_state: "SATISFIED", obligation_outcome: "FAILED"}), "Inspected — failed");
    assert.equal(run.context.testUI.stateText({...row, obligation_state: "SATISFIED", obligation_outcome: "SUPERVISOR_COMPLETED"}), "Completed by supervisor — unable to inspect");
  }
  {
    const run = generic("completion"); field(run.form, "explanation", "Synthetic supervisor explanation."); run.form.dataset.check = "unable";
    run.form.fire("submit"); assert.equal(run.posts().length, 1); assert.equal(run.posts()[0].body.check_id, "unable");
    run.form.fire("submit"); assert.equal(run.posts().length, 1); assert(run.button.disabled);
    run.posts()[0].reject(new Error("synthetic lost response")); await tick();
    assert.equal(run.button.textContent, "Retry same request"); assert(run.form.fields.explanation.disabled); assert(!run.button.disabled);
    run.form.fields.explanation.value = "Changed DOM value must not change an unconfirmed request"; run.form.fire("submit");
    assert.equal(run.posts()[0].options.body, run.posts()[1].options.body);
    response(run.posts()[1], 201, {status: "COMMITTED", result: {}}); await tick(); assert(run.form.querySelector(".workflow-result").textContent.startsWith("Saved"));
    run.form.fire("submit"); assert.equal(run.posts().length, 2);
  }
  {
    const run = generic("explanation"); field(run.form, "explanation", ""); run.form.fire("submit"); assert.equal(run.posts().length, 0);
    run.form.fields.explanation.value = "Synthetic administrator reason."; run.form.fire("submit");
    response(run.posts()[0], 403, {status: "REJECTED", code: "FORBIDDEN"}); await tick();
    assert(run.form.querySelector(".workflow-result").textContent.includes("not permitted")); assert(!run.button.disabled);
  }
  {
    const form = new Element(); form.dataset.kind = "report"; form.dataset.url = "/reports";
    for (const [name, value] of Object.entries({csrfmiddlewaretoken: "csrf", target: "item:item", category_id: "category", severity: "HIGH", summary: "Synthetic summary", description: "Synthetic description"})) field(form, name, value);
    form.fields.severity.checked = true; field(form, "related_deficiency_ids");
    form.querySelector('button[type="submit"]').type = "submit";
    const run = environment({}, [form]); run.get("workflow-config").textContent = JSON.stringify({vehicle_id: "vehicle", suggestions_url: "/suggestions"});
    run.run("cya/inspections/templates/inspections/workflows.js");
    response(run.requests[0], 200, {status: "OK", suggestions: [{deficiency_id: "related", summary: "Synthetic existing", status: "OPEN"}]}); await tick();
    assert.equal(form.fields.related_deficiency_ids.selectedOptions.length, 0);
    form.fields.related_deficiency_ids.children[0].selected = true; form.fire("submit");
    assert.deepEqual(run.posts()[0].body.target, {item_id: "item"}); assert.deepEqual(run.posts()[0].body.related_deficiency_ids, ["related"]);
  }
  {
    const form = new Element(); form.dataset.kind = "report"; form.dataset.url = "/reports";
    for (const [name, value] of Object.entries({csrfmiddlewaretoken: "csrf", target: "kit:physical-kit", category_id: "category", severity: "HIGH", summary: "Synthetic summary", description: "Synthetic description"})) field(form, name, value);
    form.fields.severity.checked = true; field(form, "related_deficiency_ids");
    form.querySelector('button[type="submit"]').type = "submit";
    const run = environment({}, [form]); run.get("workflow-config").textContent = JSON.stringify({vehicle_id: "vehicle", suggestions_url: "/suggestions"});
    run.run("cya/inspections/templates/inspections/workflows.js");
    response(run.requests[0], 200, {status: "OK", suggestions: []}); await tick();
    form.fire("submit"); assert.deepEqual(run.posts()[0].body.target, {kit_id: "physical-kit"});
  }
  {
    const run = generic("repair"); run.form.dataset.expectedEvent = "last-event"; run.form.fire("submit");
    assert.equal(run.posts()[0].body.expected_event_id, "last-event");
  }
  {
    const run = generic("repair"); run.form.dataset.expectedEvent = "last-event"; run.form.fire("submit");
    response(run.posts()[0], 422, {status: "REJECTED", code: "INVALID_TRANSITION"}); await tick();
    const message = run.form.querySelector(".workflow-result").textContent;
    assert(message.includes("status")); assert(!message.includes("scope"));
  }
  {
    const run = generic("repair"); run.form.dataset.expectedEvent = "last-event"; run.form.fire("submit");
    response(run.posts()[0], 422, {status: "REJECTED", code: "EMPTY_SCOPE"}); await tick();
    assert(run.form.querySelector(".workflow-result").textContent.includes("no current checklist items"));
  }
  for (const [kind, fields, expected] of [
    ["extension", {reason: "Synthetic delay", proposed_deadline: "2026-07-01T15:45"}, {reason: "Synthetic delay", proposed_deadline: "2026-07-01T15:45:00.000Z"}],
    ["note", {note: "Synthetic note"}, {note: "Synthetic note"}],
    ["dependency", {explanation: "Synthetic vendor", follow_up_at: "2026-07-02T12:00"}, {explanation: "Synthetic vendor", follow_up_at: "2026-07-02T12:00:00.000Z"}],
    ["dependency-exit", {return_to: "IN_PROGRESS"}, {return_to: "IN_PROGRESS"}],
  ]) {
    const run = generic(kind); run.form.dataset.expectedEvent = "event-seen";
    for (const [name, value] of Object.entries(fields)) field(run.form, name, value);
    run.form.fire("submit");
    assert(run.form.querySelector(".workflow-result").textContent.includes("Pending"));
    const sent = run.posts()[0];
    assert.equal(sent.body.expected_event_id, "event-seen");
    for (const [name, value] of Object.entries(expected)) assert.equal(sent.body[name], value);
    run.form.fire("submit"); assert.equal(run.posts().length, 1);
    sent.reject(new Error("Synthetic lost response")); await tick();
    assert(run.form.querySelector(".workflow-result").textContent.includes("Unconfirmed"));
    for (const input of Object.values(run.form.fields)) { assert(input.disabled); input.value = "Changed DOM"; }
    run.form.fire("submit"); assert.equal(run.posts()[1].options.body, sent.options.body);
    response(run.posts()[1], 409, {status: "REJECTED", code: "DEFICIENCY_CHANGED"}); await tick();
    assert(run.form.querySelector(".workflow-result").textContent.includes("Refresh history"));
  }
  for (const kind of ["extension", "note", "dependency"]) {
    const run = generic(kind); run.form.fire("submit"); assert.equal(run.posts().length, 0);
  }
  {
    const run = generic("dependency-exit"); run.form.dataset.expectedEvent = "event";
    run.form.fire("submit"); assert(!("return_to" in run.posts()[0].body));
  }
  {
    const run = generic("extension"); field(run.form, "reason", "Synthetic delay"); field(run.form, "proposed_deadline", "invalid");
    run.form.fire("submit"); assert.equal(run.posts().length, 0);
  }
  {
    const run = checklist("OPEN", {verification_for_deficiency_id: "deficiency", session_id: undefined});
    run.context.testUI.setRows([row]); run.context.testUI.startCheck(row, null, "PASSED");
    const first = run.posts()[0];
    assert.equal(first.body.purpose, "VERIFICATION"); assert.equal(first.body.verification_for_deficiency_id, "deficiency");
    assert.equal(first.body.kit_id, "kit-a"); assert(!("session_id" in first.body));
    first.reject(new Error("Synthetic lost acknowledgement")); await tick();
    run.context.testUI.reload();
    response(run.requests.at(-1), 200, {status: "OK", items: [], vehicle_status: {status: "IN_SERVICE"}, verification_status: "Closed — Verified", remaining_scope_rows: 0}); await tick();
    assert(run.get("items").textContent.includes("Retry")); // retained even after automatic closure
    run.context.testUI.retryCheck({...row, checklist_version_id: "new-version"});
    assert.equal(run.posts()[1].options.body, first.options.body);
    response(run.posts()[1], 409, {status: "CONFLICT", newest_check: {check_id: "new-basis", outcome: "PASSED"}}); await tick();
    run.context.testUI.setRows([]); run.get("conflict-proceed").fire("click");
    const proceed = run.posts()[2];
    assert.notEqual(proceed.body.operation_id, first.body.operation_id);
    assert.equal(proceed.body.purpose, "VERIFICATION"); assert.equal(proceed.body.verification_for_deficiency_id, "deficiency");
    assert.equal(proceed.body.acknowledges_check_id, "new-basis"); assert.equal(proceed.body.basis_check_id, "new-basis");
    response(proceed, 201, {status: "COMMITTED", result: {verification: {status: "CLOSED_VERIFIED", counts_toward_scope: true, remaining_scope_rows: 0}}}); await tick();
    assert(run.get("session-message").textContent.includes("saved"));
  }
  for (const [kind, observation] of [["PRESENCE", undefined], ["QUANTITY", 2], ["MEASUREMENT", "12.5"]]) {
    const run = checklist("OPEN", {verification_for_deficiency_id: "deficiency", session_id: undefined});
    const observedRow = {...row, kind, unit: "psi"};
    run.context.testUI.openOutcome(observedRow, "FAILED", observation);
    const body = run.posts()[0].body;
    assert.equal(body.outcome, "FAILED"); assert.equal(body.verification_for_deficiency_id, "deficiency");
    assert(!("deficiency" in body)); assert.equal(run.requests.length, 1); // no suggestions or new-report modal
    if (kind === "QUANTITY") assert.equal(body.quantity, 2);
    if (kind === "MEASUREMENT") assert.equal(body.measurement.value, "12.5");
    const controls = run.context.testUI.controls(observedRow, new Element("row"));
    assert(!controls.textContent.includes("Unable to inspect"));
    response(run.posts()[0], 201, {status: "COMMITTED", result: {verification: {status: "IN_PROGRESS", remaining_scope_rows: 1}}}); await tick();
    assert(run.get("session-message").textContent.includes("back in progress"));
  }
  for (const [verification, expected] of [
    [{status: "PENDING_VERIFICATION", counts_toward_scope: true, remaining_scope_rows: 2}, "2 scope rows remain"],
    [{status: "PENDING_VERIFICATION", counts_toward_scope: false}, "does not count"],
    [{status: "PENDING_VERIFICATION", closure_withheld: true, remaining_scope_rows: 0}, "closure withheld"],
  ]) {
    const run = checklist("OPEN", {verification_for_deficiency_id: "deficiency"});
    run.context.testUI.setRows([row]); run.context.testUI.startCheck(row, null, "PASSED");
    response(run.posts()[0], 201, {status: "COMMITTED", result: {verification}}); await tick();
    assert(run.get("session-message").textContent.includes(expected));
  }
  {
    const run = checklist("OPEN", {verification_for_deficiency_id: "deficiency"});
    const controls = run.context.testUI.controls({...row, kind: "VEHICLE_READING"}, new Element("row"));
    assert(controls.textContent.includes("remains outstanding"));
    assert(!controls.textContent.includes("odometer form above"));
  }
  console.log("Offline workflow script regressions passed: outcomes, kit scope, explicit match choice, reasons, exact retries, full conflict payloads, hidden rows, labels, permissions and single-flight forms.");
})().catch(error => { console.error(error); process.exitCode = 1; });
