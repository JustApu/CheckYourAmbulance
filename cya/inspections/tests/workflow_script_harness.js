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
    this.parentNode = null; this._document = null;
  }
  set textContent(text) {
    if (this._document && this._document.activeElement && this.contains(this._document.activeElement)) this._document.activeElement = null;
    for (const child of this.children) child.parentNode = null;
    this._text = text; this.children = [];
  }
  get textContent() { return this._text || this.children.map(e => e.textContent).join(""); }
  setAttribute(name, value) { this[name] = value; }
  removeAttribute(name) { if (name === "open") this.open = false; else delete this[name]; }
  addEventListener(name, fn) { this.listeners[name] = fn; }
  fire(name) {
    if (name === "click" && this.type === "button" && this._document && !this.disabled) this._document.activeElement = this;
    if (this.listeners[name]) this.listeners[name]({preventDefault() {}});
  }
  appendChild(node) { node.parentNode = this; node._document = this._document; this.children.push(node); return node; }
  removeChild(node) {
    this.children.splice(this.children.indexOf(node), 1);
    if (this._document.activeElement && node.contains(this._document.activeElement)) this._document.activeElement = null;
    node.parentNode = null;
    if (this.id === "items" && this._document.defaultView.simulateListJump) { this._document.defaultView.scrollY = 0; }
  }
  get firstChild() { return this.children[0]; }
  get selectedOptions() { return this.children.filter(node => node.selected); }
  contains(node) { for (let current = node; current; current = current.parentNode) { if (current === this) return true; } return false; }
  showModal() { this.open = true; if (this._document) this._document.activeElement = this; }
  close() { this.open = false; if (this._document) this._document.activeElement = null; }
  focus(options) {
    this.focusOptions = options;
    if (!this.disabled && this._document && this._document.contains(this)) this._document.activeElement = this;
  }
  getBoundingClientRect() {
    if (this.type !== "li" || !this.parentNode || this.parentNode.id !== "items") return {top: 0, bottom: 0};
    const top = this.parentNode.children.indexOf(this) * 100 - this._document.defaultView.scrollY;
    return {top, bottom: top + 100};
  }
  reset() { for (const field of Object.values(this.fields)) { field.value = field.defaultValue; field.checked = false; } }
  querySelector(selector) {
    const match = selector.match(/^\[name="([^"]+)"\](:checked)?$/);
    if (match) { const field = this.fields[match[1]]; return field && (!match[2] || field.checked) ? field : null; }
    return this.queries[selector] ||= new Element();
  }
  querySelectorAll(selector) {
    if (selector === "input, select, textarea, button") return [...Object.values(this.fields), this.querySelector('button[type="submit"]')];
    if (selector === '[name="system_ids"]:checked') return this.systems || [];
    if (selector === '[name="items"]:checked') return (this.items || []).filter(node => node.checked);
    return [];
  }
}
function field(form, name, value = "", type = "text") {
  const node = new Element(name, type); node.value = value; node.defaultValue = value;
  form.fields[name] = node; return node;
}
function environment(config, forms = []) {
  const elements = {}, requests = [], sockets = [], timers = [];
  const window = {scrollY: 0, innerHeight: 200, simulateListJump: false, scrollBy(_x, y) { this.scrollY += y; }};
  const document = {
    defaultView: window,
    activeElement: null,
    getElementById(id) {
      if (((config.state && config.state !== "OPEN") || config.verification_for_deficiency_id) && /^(odometer-form|odo-)/.test(id)) return null;
      if (!elements[id]) {
        elements[id] = new Element(id);
        if (id === "outcome-form") {
          for (const name of ["outcome_explanation", "category_id", "summary", "description"]) field(elements[id], name);
          field(elements[id], "severity", "HIGH", "radio");
        }
      }
      elements[id]._document = document; return elements[id];
    },
    contains(node) {
      const roots = Object.values(elements);
      for (let current = node; current; current = current.parentNode) { if (roots.includes(current)) return true; }
      return false;
    },
    querySelector() { return {value: "synthetic-csrf"}; },
    querySelectorAll(selector) { return selector === ".workflow-write" ? forms : []; },
    createElement(tag) { const node = new Element("", tag); node._document = document; return node; },
    createTextNode(text) { const node = new Element(); node._document = document; node.textContent = text; return node; },
    addEventListener() {},
  };
  const context = {
    document, window, crypto: crypto.webcrypto, Uint8Array,
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
  return {context, document, window, elements, requests, sockets, timers,
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
    "  globalThis.testUI={startCheck,retryCheck,openOutcome,askConflict,stateText,controls,reload,render,setRows(value){rows=value;},drafts,getEntry(row){return entries[key(row)];},odo,submitOdometer};\n  // ---- start "));
  return run;
}
const row = {item_id: "item", item_label: "Synthetic item", kit_id: "kit-a", kit_label: "Synthetic kit A", kind: "PRESENCE", checklist_version_id: "version", latest_check_id: "basis-a", latest_check_outcome: "PASSED", latest_check_at: "2026-06-01T00:00:00Z", deficiencies: [], allowed_outcomes: ["PASSED", "FAILED", "UNABLE_TO_INSPECT"]};
function findAction(node, action) {
  if (node.dataset && node.dataset.action === action) return node;
  for (const child of node.children) { const found = findAction(child, action); if (found) return found; }
  return null;
}
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
  {
    const run = checklist();
    run.context.testUI.setRows([row]); run.context.testUI.render();
    findAction(run.elements.items.children[0], "failed").fire("click");
    response(run.requests[0], 200, {status: "OK", suggestions: []}); await tick();
    run.context.testUI.setRows([]); run.context.testUI.render();
    assert.equal(run.document.activeElement, run.get("outcome-dialog"), "a live update keeps focus in the dialog when the item disappears");
    run.get("outcome-cancel").fire("click");
    assert.equal(run.document.activeElement, run.elements.items, "Cancel falls back to the checklist if the item disappeared");
    run.document.activeElement = null;
    run.context.testUI.setRows([row]); run.context.testUI.render();
    assert.equal(run.document.activeElement, null, "a later live render does not reuse the cancelled focus intent");
  }
  {
    const run = checklist();
    run.context.testUI.setRows([row]); run.context.testUI.render();
    findAction(run.elements.items.children[0], "failed").fire("click");
    response(run.requests[0], 200, {status: "OK", suggestions: []}); await tick();
    run.context.testUI.setRows([]); run.context.testUI.render();
    run.get("outcome-dialog").fire("cancel");
    assert.equal(run.get("outcome-dialog").open, false, "Escape closes the outcome dialog through its cancel handler");
    assert.equal(run.document.activeElement, run.elements.items, "Escape falls back to the checklist if the item disappeared");
    run.document.activeElement = null;
    run.context.testUI.setRows([row]); run.context.testUI.render();
    assert.equal(run.document.activeElement, null, "a later live render does not reuse Escape's cancelled focus intent");
  }
  {
    const run = checklist(), rows = Array.from({length: 8}, (_, index) => ({...row, item_id: "item-" + index, item_label: "Synthetic item " + index}));
    run.context.testUI.setRows(rows); run.context.testUI.render();
    run.window.scrollY = 500; run.window.simulateListJump = true;
    const target = run.elements.items.children[5], pass = findAction(target, "pass"); pass.fire("click");
    assert.equal(run.window.scrollY, 500, "pending render keeps the active checklist item in place");
    assert.equal(run.document.activeElement.type, "li", "pending render moves focus to the item while its action is disabled");
    assert.equal(run.document.activeElement.dataset.itemKey, "item-5|kit-a");
    response(run.posts()[0], 201, {status: "COMMITTED", result: {server_recorded_at: "2026-06-01T00:00:00Z"}}); await tick();
    assert.equal(run.window.scrollY, 500, "acknowledged render keeps the active checklist item in place");
    assert.equal(run.document.activeElement.dataset.action, "pass", "acknowledged render restores the same action focus");
    const fail = findAction(run.elements.items.children[6], "failed"); fail.fire("click");
    response(run.requests[1], 200, {status: "OK", suggestions: []}); await tick();
    run.context.testUI.setRows(rows.slice()); run.context.testUI.render();
    assert.equal(run.document.activeElement, run.get("outcome-dialog"), "a live rerender keeps focus inside the open deficiency dialog");
    newReport(run); run.get("outcome-form").fire("submit");
    const failed = run.posts()[1]; assert.equal(failed.body.outcome, "FAILED"); assert.equal(failed.body.deficiency.create.summary, "Synthetic failure");
    assert.equal(run.window.scrollY, 500, "deficiency submission keeps the failed item in place");
    assert.equal(run.document.activeElement.type, "li", "deficiency submission moves focus to the failed item while its action is disabled");
    response(failed, 201, {status: "COMMITTED", result: {server_recorded_at: "2026-06-01T00:00:00Z"}}); await tick();
    assert.equal(run.window.scrollY, 500, "failed check acknowledgement keeps the item in place");
    assert.equal(run.document.activeElement.dataset.action, "failed", "failed check acknowledgement restores its action focus");
    run.context.testUI.setRows(rows.slice(0, 5)); run.context.testUI.render();
    assert.equal(run.document.activeElement, run.elements.items, "if a live update removes the active row, focus falls back to the checklist");
    assert.equal(run.elements.items.children[4].getBoundingClientRect().top, 100, "removed active row keeps the nearest visible row in place");
  }
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
    run.get("conflict-dialog").fire("cancel"); assert.equal(!!run.get("conflict-dialog").open, false);
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
    // Issue 20: the repair form has its own wording; the report form keeps the shared text.
    const text = run.form.querySelector(".workflow-result").textContent;
    assert(text.includes("no current checklist items left to verify"), text);
    assert(text.includes("Ask a supervisor or administrator to close it"), text);
    assert(!text.includes("Refresh the report"), text);
  }
  {
    // Issue 21: on a page that offers the empty-scope close, the repair refusal points to it.
    const run = generic("repair"); run.form.dataset.expectedEvent = "last-event"; run.form.dataset.canCloseEmpty = "1";
    run.form.fire("submit");
    response(run.posts()[0], 422, {status: "REJECTED", code: "EMPTY_SCOPE"}); await tick();
    const text = run.form.querySelector(".workflow-result").textContent;
    assert(text.includes("Close: no current verification items"), text);
    assert(!text.includes("Ask a supervisor"), text);
  }
  {
    // Issue 21: the empty-scope close needs an explanation, sends the page's event, and a lost
    // response is retried with the identical request and operation id.
    const run = generic("empty-scope-close"); run.form.dataset.expectedEvent = "event-seen";
    field(run.form, "explanation", "");
    run.form.fire("submit"); assert.equal(run.posts().length, 0);
    assert(run.form.querySelector(".workflow-result").textContent.includes("explanation is required"));
    run.form.fields.explanation.value = "Synthetic: the item was retired.";
    run.form.fire("submit");
    const first = run.posts()[0].body;
    assert.deepEqual(Object.keys(first).sort(), ["expected_event_id", "explanation", "operation_id"]);
    assert.equal(first.expected_event_id, "event-seen");
    assert.equal(first.explanation, "Synthetic: the item was retired.");
    run.posts()[0].reject(new Error("synthetic lost response")); await tick();
    assert(run.form.querySelector(".workflow-result").textContent.startsWith("Unconfirmed"));
    run.form.fire("submit");
    assert.deepEqual(run.posts()[1].body, first);
    response(run.posts()[1], 201, {status: "COMMITTED", result: {}}); await tick();
    assert(run.form.querySelector(".workflow-result").textContent.startsWith("Saved"));
  }
  for (const [code, expected] of [
    ["SCOPE_AVAILABLE", "Current verification items exist; record the repair instead."],
    ["DEFICIENCY_CHANGED", "This deficiency changed. Refresh history"],
    ["INVALID_TRANSITION", "no longer Open, In Progress or Awaiting External Dependency"],
  ]) {
    const run = generic("empty-scope-close"); run.form.dataset.expectedEvent = "event-seen";
    field(run.form, "explanation", "Synthetic explanation.");
    run.form.fire("submit");
    response(run.posts()[0], code === "DEFICIENCY_CHANGED" ? 409 : 422, {status: "REJECTED", code}); await tick();
    const text = run.form.querySelector(".workflow-result").textContent;
    assert(text.includes(expected), code + ": " + text);
  }
  {
    // Any other form keeps the shared EMPTY_SCOPE text.
    const run = generic("note"); run.form.dataset.expectedEvent = "last-event"; field(run.form, "note", "Synthetic note");
    run.form.fire("submit");
    response(run.posts()[0], 422, {status: "REJECTED", code: "EMPTY_SCOPE"}); await tick();
    const text = run.form.querySelector(".workflow-result").textContent;
    assert(text.includes("Refresh the report or contact a supervisor"), text);
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
  // Issue #16 supervisor and administrator forms: the payload each kind sends, and local refusals.
  function supervisorForm(kind, values) {
    const run = generic(kind); run.form.dataset.expectedEvent = "event-seen";
    for (const [name, value] of Object.entries(values)) field(run.form, name, value);
    return run;
  }
  {
    const run = supervisorForm("assign", {assignee_id: ""}); run.form.fire("submit"); assert.equal(run.posts().length, 0);
    run.form.fields.assignee_id.value = "person"; run.form.fire("submit");
    assert.deepEqual({...run.posts()[0].body, operation_id: "x"}, {operation_id: "x", expected_event_id: "event-seen", assignee_id: "person"});
  }
  {
    const run = supervisorForm("deadline", {deadline: "2026-06-03T10:00"}); run.form.fire("submit");
    assert.equal(run.posts()[0].body.deadline, "2026-06-03T10:00:00.000Z");
    const start = generic("start-work"); start.form.dataset.expectedEvent = "event-seen"; start.form.fire("submit");
    assert.deepEqual(Object.keys(start.posts()[0].body).sort(), ["expected_event_id", "operation_id"]);
  }
  {
    const decide = values => supervisorForm("extension-decision", {request_event_id: "request", explanation: "Synthetic decision.", deadline: "", ...values});
    let run = decide({decision: "APPROVED"}); run.form.fire("submit");
    assert.equal(run.posts()[0].body.request_event_id, "request"); assert(!("deadline" in run.posts()[0].body));
    run = decide({decision: "APPROVED", deadline: "2026-06-09T08:00"}); run.form.fire("submit"); assert.equal(run.posts().length, 0);
    run = decide({decision: "DIFFERENT_DATE", deadline: "2026-06-09T08:00"}); run.form.fire("submit");
    assert.equal(run.posts()[0].body.deadline, "2026-06-09T08:00:00.000Z");
    run = decide({decision: "DENIED", explanation: " "}); run.form.fire("submit"); assert.equal(run.posts().length, 0);
    run = decide({decision: ""}); run.form.fire("submit"); assert.equal(run.posts().length, 0);
  }
  for (const [days, sent] of [["2", 2], ["0", null], ["1.5", null], ["", null]]) {
    const run = supervisorForm("acknowledge", {explanation: "Synthetic review.", action_plan: "Synthetic plan.", period_days: days});
    run.form.fire("submit");
    if (sent === null) { assert.equal(run.posts().length, 0); } else { assert.equal(run.posts()[0].body.period_days, sent); assert.equal(run.posts()[0].body.action_plan, "Synthetic plan."); }
  }
  {
    const run = supervisorForm("regrade", {explanation: "Synthetic re-grade."}); run.form.fire("submit"); assert.equal(run.posts().length, 0);
    field(run.form, "severity", "LOW", "radio").checked = true; run.form.fire("submit");
    assert.equal(run.posts()[0].body.severity, "LOW");
  }
  {
    const run = supervisorForm("suspend", {explanation: ""}); run.form.fire("submit"); assert.equal(run.posts().length, 0);
    run.form.items = ["item-1:", "item-2:kit-9", "item-3:"].map((value, index) => { const box = new Element("", "checkbox"); box.value = value; box.checked = index < 2; return box; });
    run.form.fire("submit");
    const body = run.posts()[0].body;
    assert.deepEqual(body.items, [{item_id: "item-1"}, {item_id: "item-2", kit_instance_id: "kit-9"}]); assert(!("explanation" in body));
    response(run.posts()[0], 409, {status: "REJECTED", code: "ITEM_NOT_IN_SCOPE"}); await tick();
    assert(run.form.querySelector(".workflow-result").textContent.includes("outside this deficiency's affected scope"));
  }
  {
    // SUS-5: one suspension per form; the explanation is optional and sent only when given.
    const run = supervisorForm("end-suspension", {explanation: ""}); run.form.dataset.suspension = "suspension-7";
    run.form.fire("submit");
    assert.deepEqual({...run.posts()[0].body, operation_id: "x"}, {operation_id: "x", expected_event_id: "event-seen", suspension_id: "suspension-7"});
    response(run.posts()[0], 422, {status: "REJECTED", code: "SUSPENSION_ALREADY_ENDED"}); await tick();
    assert(run.form.querySelector(".workflow-result").textContent.includes("already ended"));
    const explained = supervisorForm("end-suspension", {explanation: " Synthetic reason. "}); explained.form.dataset.suspension = "suspension-8";
    explained.form.fire("submit");
    assert.equal(explained.posts()[0].body.explanation, "Synthetic reason.");
  }
  {
    const run = supervisorForm("add-verification", {explanation: "Synthetic added.", deadline: ""});
    run.form.items = [Object.assign(new Element("", "checkbox"), {value: "item-1:", checked: true})];
    run.form.fire("submit"); assert.equal(run.posts().length, 0); // the deadline is required
    run.form.fields.deadline.value = "2026-06-10T00:00"; run.form.fire("submit");
    assert.equal(run.posts()[0].body.deadline, "2026-06-10T00:00:00.000Z");
  }
  for (const kind of ["admin-close", "admin-reopen", "expand"]) {
    const run = supervisorForm(kind, {explanation: ""});
    run.form.items = [Object.assign(new Element("", "checkbox"), {value: "item-1:", checked: true})];
    run.form.fire("submit"); assert.equal(run.posts().length, 0);
    run.form.fields.explanation.value = "Synthetic explanation."; run.form.fire("submit");
    assert.equal(run.posts()[0].body.expected_event_id, "event-seen");
    response(run.posts()[0], 403, {status: "REJECTED", code: "FORBIDDEN"}); await tick();
    assert(run.form.querySelector(".workflow-result").textContent.includes("not permitted"));
  }
  {
    // Issue 37: a 401 from the verification re-read means signed out. Stop reloading, keep the
    // Retry note, and never request /auth/login/ from the page.
    const run = checklist("OPEN", {verification_for_deficiency_id: "deficiency", session_id: undefined});
    run.context.testUI.reload();
    const read = run.requests.at(-1);
    response(read, 401, {status: "REJECTED", code: "AUTHENTICATION_REQUIRED"}); await tick(); await tick();
    assert(run.get("live-detail").textContent.includes("signed out"));
    assert.equal(run.get("live-banner").hidden, false);
    const count = run.requests.length;
    run.context.testUI.reload();
    run.sockets[0].onmessage({data: "{}"});
    run.timers.forEach(fn => fn());
    assert.equal(run.requests.length, count);
    assert.equal(run.requests.some(r => String(r.url).startsWith("/auth/")), false);
  }
  console.log("Offline workflow script regressions passed: checklist scroll/focus preservation, outcomes, kit scope, explicit match choice, reasons, exact retries, full conflict payloads, hidden rows, labels, permissions, single-flight forms and supervisor forms.");
})().catch(error => { console.error(error); process.exitCode = 1; });
