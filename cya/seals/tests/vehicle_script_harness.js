/* Offline crew seal-page checks: no network or real agency data. */
"use strict";
const assert = require("node:assert/strict");
const crypto = require("node:crypto");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const script = path.resolve(__dirname, "../templates/seals/vehicle.js");
const tick = () => new Promise(resolve => setImmediate(resolve));

class Node {
  constructor(tag, text = "") {
    this.tag = tag; this.textContent = text; this.children = []; this.listeners = {};
    this.value = ""; this.checked = false; this.hidden = false; this.type = ""; this.attributes = {};
  }
  appendChild(node) { node.parentNode = this; this.children.push(node); return node; }
  removeChild(node) { this.children = this.children.filter(child => child !== node); node.parentNode = null; return node; }
  replaceChildren() { this.children = []; }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  removeAttribute(name) { delete this.attributes[name]; }
  addEventListener(name, callback) { this.listeners[name] = callback; }
  fire(name) { this.listeners[name]({preventDefault() {}}); }
  reportValidity() { return true; }
  focus() {}
}
function find(root, predicate) {
  if (predicate(root)) return root;
  for (const child of root.children) { const found = find(child, predicate); if (found) return found; }
  return null;
}
function page(subject, deferSubjects = false, canRequestAudit = false, paper = null) {
  const requests = [], elements = {}, sockets = [];
  const ids = ["seal-subjects", "seal-work", "seal-form", "seal-message", "seal-work-heading", "seal-cancel"];
  if (paper) {
    ids.push("seal-error", "paper-performers", "paper-print-offer", "paper-print-status", "paper-print-dismiss",
      "paper-banner-heading");
  }
  for (const id of ids) { elements[id] = new Node("div"); }
  if (paper) { elements["seal-error"].hidden = true; elements["paper-print-offer"].hidden = true; }
  const config = {vehicle_id: "vehicle", subjects_url: "/subjects", scope_url: "/scope/KIND/SUBJECT",
    command_url: "/command/ACTION", checks_url: "/checks", socket_url: "/socket", can_request_audit: canRequestAudit,
    report_options: {categories: [], severities: []}};
  if (paper) {
    config.paper_entry = Object.assign({batch_id: "batch", checklist_version_id: "printed-version",
      performed_at: "2026-06-02T10:15:00+00:00", performed_label: "2026-06-02 06:15",
      performers: ["Sample performer"], has_records: false, state: "OPEN"}, paper);
  }
  const document = {
    createElement: tag => new Node(tag),
    getElementById(id) { return id === "seal-config" ? {textContent: JSON.stringify(config)} : elements[id]; },
    querySelector() { return {value: "synthetic-csrf"}; },
    addEventListener() {},
  };
  const context = {document, crypto: crypto.webcrypto, Uint8Array, encodeURIComponent,
    location: {protocol: "https:", host: "example.test"},
    WebSocket: class { constructor(url) { this.url = url; sockets.push(this); } },
    setTimeout: callback => { callback(); return 1; }, clearTimeout() {},
    fetch(url, options) {
      const request = {url, options}; requests.push(request);
      if (url === "/subjects" && !deferSubjects) return Promise.resolve({ok: true, json: async () => ({subjects:
        Array.isArray(subject) ? subject : [subject]})});
      return new Promise((resolve, reject) => { request.resolve = resolve; request.reject = reject; });
    }};
  vm.createContext(context); vm.runInContext(fs.readFileSync(script, "utf8"), context);
  return {elements, requests, sockets};
}

(async () => {
  {
    const p = page({kind: "kit", id: "kit", label: "Sample", state: "OPEN", tip_id: null}, false, true);
    await tick(); await tick();
    assert.equal(find(p.elements["seal-subjects"], n => n.textContent === "Request audit"), null);
    assert.ok(find(p.elements["seal-subjects"], n => /full inspection before resealing/.test(n.textContent)));
  }
  {
    const p = page(null, true);
    p.sockets[0].onmessage();
    p.requests[1].resolve({ok: true, json: async () => ({subjects: [
      {kind: "kit", id: "kit", label: "New state", state: "OPEN", tip_id: null}]})});
    await tick(); await tick();
    p.requests[0].resolve({ok: true, json: async () => ({subjects: [
      {kind: "kit", id: "kit", label: "Old state", state: "SEALED", tip_id: "tip"}]})});
    await tick(); await tick();
    assert.ok(find(p.elements["seal-subjects"], n => n.textContent === "New state"));
    assert.equal(find(p.elements["seal-subjects"], n => n.textContent === "Old state"), null,
      "an older response cannot overwrite newer seal state");
  }
  {
    const p = page({kind: "kit", id: "kit", label: "Sample", state: "SEALED", tip_id: "tip"});
    await tick(); await tick();
    assert.equal(p.sockets[0].url, "wss://example.test/socket");
    p.sockets[0].onmessage();
    await tick(); await tick();
    assert.equal(p.requests.filter(request => request.url === "/subjects").length, 2,
      "a vehicle update refreshes authoritative seal state");
    p.sockets[0].onclose({code: 4401});
    assert.match(p.elements["seal-message"].textContent, /sign-in or access changed/);
  }
  {
    const p = page({kind: "kit", id: "kit", label: "Sample", state: "SEALED", tip_id: "tip"});
    await tick(); await tick();
    find(p.elements["seal-subjects"], n => n.textContent === "Verify seal").fire("click");
    const form = find(p.elements["seal-form"], n => n.tag === "form");
    find(form, n => n.tag === "input" && n.type === "text").value = "sample-id";
    find(form, n => n.tag === "input" && n.type === "checkbox").checked = true;
    form.fire("submit");
    const first = p.requests[1]; first.reject(new Error("network unavailable")); await tick(); await tick();
    assert.match(p.elements["seal-message"].textContent, /Unconfirmed/);
    find(p.elements["seal-form"], n => n.textContent === "Retry unconfirmed action").fire("click");
    assert.equal(p.requests[2].options.body, first.options.body, "retry keeps the exact operation id and payload");
    p.requests[2].resolve({status: 200, json: async () => ({status: "COMMITTED"})});
    await tick(); await tick(); assert.match(p.elements["seal-message"].textContent, /Recorded/);
  }
  {
    const p = page({kind: "kit", id: "kit", label: "Sample", state: "OPEN", tip_id: null});
    await tick(); await tick();
    find(p.elements["seal-subjects"], n => n.textContent === "Inspect and reseal").fire("click");
    p.requests[1].resolve({status: 200, json: async () => ({status: "COMMITTED",
      result: {start_operation_id: "start"}})});
    await tick(); await tick();
    const get = p.requests[2];
    get.resolve({ok: true, json: async () => ({kit_lines: [], kit_presence: [], items: [{item_id: "item", kit_id: "kit",
      label: "Supply", kit_label: "Sample", kind: "QUANTITY", expected_quantity: 2,
      expiration_tracked: true, unit: null}]})});
    await tick(); await tick();
    const form = find(p.elements["seal-form"], n => n.tag === "form");
    const quantity = find(form, n => n.tag === "input" && n.type === "number");
    const expiration = find(form, n => n.tag === "input" && n.type === "date");
    assert.equal(quantity.value, ""); assert.equal(expiration.value, "");
    assert.ok(find(form, n => /Enter every tracked expiration/.test(n.textContent)));
    expiration.value = "2026-12-31"; expiration.fire("change");
    assert.ok(find(form, n => /2026-12-31/.test(n.textContent)));
  }
  {
    const p = page([
      {kind: "slot", id: "slot", label: "Cabinet", state: "OPEN", tip_id: null},
      {kind: "kit", id: "kit", slot_id: "slot", label: "Nested", state: "SEALED",
        tip_id: "kit-tip", expiration: "2026-12-15"},
    ]);
    await tick(); await tick();
    find(p.elements["seal-subjects"], n => n.textContent === "Inspect and reseal").fire("click");
    p.requests[1].resolve({status: 200, json: async () => ({status: "COMMITTED",
      result: {start_operation_id: "start"}})});
    await tick(); await tick();
    p.requests[2].resolve({ok: true, json: async () => ({kit_lines: [], kit_presence: [], items: [{item_id: "item", kit_id: "kit",
      label: "Supply", kit_label: "Nested", kind: "QUANTITY", expected_quantity: 2,
      expiration_tracked: true, unit: null}]})});
    await tick(); await tick();
    const form = find(p.elements["seal-form"], n => n.tag === "form");
    const nested = find(form, n => n.tag === "fieldset" && n.children.some(c => c.textContent === "Verify nested kit: Nested"));
    find(nested, n => n.tag === "input" && n.type === "text").value = "nested-id";
    find(nested, n => n.tag === "input" && n.type === "checkbox").checked = true;
    find(nested, n => n.textContent === "Verify this kit seal").fire("click");
    p.requests[3].resolve({status: 200, json: async () => ({status: "COMMITTED",
      result: {substitution_id: "nested-substitution"}})});
    await tick(); await tick();
    const item = find(form, n => n.tag === "fieldset" && n.children.some(c => c.textContent === "Supply — Nested"));
    assert.equal(item.hidden, true, "verified nested contents no longer require fresh item entry");
    assert.ok(find(form, n => /2026-12-15/.test(n.textContent)), "nested seal expiration contributes to the cabinet seal");
  }
  {
    const p = page({kind: "slot", id: "slot", label: "Cabinet", state: "OPEN", tip_id: null});
    await tick(); await tick();
    find(p.elements["seal-subjects"], n => n.textContent === "Inspect and reseal").fire("click");
    p.requests[1].resolve({status: 200, json: async () => ({status: "COMMITTED",
      result: {start_operation_id: "start"}})});
    await tick(); await tick();
    p.requests[2].resolve({ok: true, json: async () => ({items: [], kit_presence: [], kit_lines: [{line_id: "line",
      label: "Kit", item_id: "presence", required_count: 1, checklist_version_id: "version"}]})});
    await tick(); await tick();
    const form = find(p.elements["seal-form"], n => n.tag === "form");
    find(form, n => n.tag === "input" && n.type === "number").value = "1";
    find(form, n => n.textContent === "Record kit count").fire("click");
    assert.equal(p.requests[3].url, "/checks");
    assert.equal(JSON.parse(p.requests[3].options.body).kit_requirement_id, "line");
    p.requests[3].resolve({status: 409, json: async () => ({status: "CONFLICT", newest_check: {check_id: "newer"}})});
    await tick(); await tick();
    find(form, n => n.textContent === "Record my count too").fire("click");
    assert.equal(JSON.parse(p.requests[4].options.body).acknowledges_check_id, "newer");
    p.requests[4].resolve({status: 200, json: async () => ({status: "COMMITTED", result: {check_id: "count-check"}})});
    await tick(); await tick();
    find(form, n => n.tag === "input" && n.type === "text").value = "cabinet-id";
    form.fire("submit");
    const seal = JSON.parse(p.requests[5].options.body);
    assert.deepEqual(seal.kit_count_checks, [{line_id: "line", check_id: "count-check"}]);
  }
  {
    const p = page({kind: "slot", id: "slot", label: "Cabinet", state: "OPEN", tip_id: null});
    await tick(); await tick();
    find(p.elements["seal-subjects"], n => n.textContent === "Inspect and reseal").fire("click");
    p.requests[1].resolve({status: 200, json: async () => ({status: "COMMITTED",
      result: {start_operation_id: "start"}})});
    await tick(); await tick();
    p.requests[2].resolve({ok: true, json: async () => ({items: [], kit_presence: [], kit_lines: [{line_id: "line",
      label: "Unbound kit", item_id: "presence", required_count: 1, checklist_version_id: null}]})});
    await tick(); await tick();
    const form = find(p.elements["seal-form"], n => n.tag === "form");
    find(form, n => n.tag === "input" && n.type === "number").value = "1";
    find(form, n => n.textContent === "Record kit count").fire("click");
    const count = JSON.parse(p.requests[3].options.body);
    assert.equal(count.checklist_version_id, null);
    assert.equal(count.inspection_start_operation_id, "start");
    assert.equal(count.kit_requirement_id, "line");
  }
  // ---- Paper entry (SPEC 8.5.0 LATE-7; issue #9 B6c) ----------------------------------------
  const text = (root, wanted) => find(root, n => n.textContent === wanted);
  {
    // The sheet's actions only: no live verification, start or audit, even for a supervisor.
    const p = page([
      {kind: "kit", id: "sealed", label: "Sealed kit", state: "SEALED", tip_id: "tip"},
      {kind: "kit", id: "open", label: "Open kit", state: "OPEN", tip_id: "open-tip"},
    ], false, true, {});
    await tick(); await tick();
    const list = p.elements["seal-subjects"];
    const [sealed, opened] = list.children;
    assert.ok(text(sealed, "Seal intact on the sheet"));
    assert.ok(text(sealed, "Seal broken on the sheet"));
    assert.equal(text(sealed, "Resealed on the sheet"), null, "a reseal follows a copied break");
    assert.ok(text(opened, "Seal intact on the sheet"));
    assert.ok(text(opened, "Resealed on the sheet"));
    assert.equal(text(opened, "Seal broken on the sheet"), null, "an open seal cannot be broken");
    for (const live of ["Verify seal", "Break seal", "Inspect and reseal", "Request audit"]) {
      assert.equal(text(list, live), null, `no live "${live}" in paper mode`);
    }
    assert.equal(p.elements["paper-performers"].textContent, "Sample performer", "names only via textContent");
  }
  {
    const p = page({kind: "kit", id: "kit", label: "Sample", state: "SEALED", tip_id: "tip"}, false, false,
      {state: "INVALIDATED"});
    await tick(); await tick();
    assert.equal(find(p.elements["seal-subjects"], n => n.tag === "button"), null,
      "an invalidated paper entry takes no further record");
  }
  {
    // Verify: the batch is named; REC-4 keeps the exact request until a definite answer.
    const p = page({kind: "kit", id: "kit", label: "Sample", state: "SEALED", tip_id: "tip"}, false, false, {});
    await tick(); await tick();
    text(p.elements["seal-subjects"], "Seal intact on the sheet").fire("click");
    assert.match(p.elements["seal-work-heading"].textContent, /^Seal intact on the sheet: Sample$/);
    const form = find(p.elements["seal-form"], n => n.tag === "form");
    find(form, n => n.tag === "input" && n.type === "text").value = "sample-id";
    find(form, n => n.tag === "input" && n.type === "checkbox").checked = true;
    form.fire("submit");
    const first = p.requests[1];
    assert.equal(first.url, "/command/verify");
    const body = JSON.parse(first.options.body);
    assert.equal(body.paper_entry_batch_id, "batch");
    assert.equal(body.expected_tip_id, "tip");
    assert.equal(body.kit_id, "kit");
    assert.equal(body.marking, "sample-id");
    assert.equal(body.attested, true);
    assert.ok(body.operation_id);
    first.reject(new Error("network unavailable")); await tick(); await tick();
    assert.match(p.elements["seal-message"].textContent, /Unconfirmed/);
    text(p.elements["seal-subjects"], "Seal broken on the sheet").fire("click");
    assert.equal(p.requests.length, 2, "no other record starts while one is unconfirmed");
    assert.match(p.elements["seal-error"].textContent, /Retry the unconfirmed action/);
    text(p.elements["seal-form"], "Retry unconfirmed action").fire("click");
    assert.equal(p.requests[2].options.body, first.options.body, "retry keeps the exact operation id and payload");
    p.requests[2].resolve({status: 503, json: async () => ({status: "REJECTED", code: "RETRY_LATER"})});
    await tick(); await tick();
    assert.match(p.elements["seal-message"].textContent, /Unconfirmed/, "a server failure is not a definite answer");
    assert.equal(p.elements["seal-form"].children.filter(n => n.textContent === "Retry unconfirmed action").length, 1);
    text(p.elements["seal-form"], "Retry unconfirmed action").fire("click");
    assert.equal(p.requests[3].options.body, first.options.body, "the second retry is still the same request");
    assert.equal(p.elements["paper-print-offer"].hidden, true);
    p.requests[3].resolve({status: 200, json: async () => ({status: "COMMITTED", result: {code: "SEAL_SUBSTITUTED"}})});
    await tick(); await tick();
    assert.match(p.elements["seal-message"].textContent, /Saved from the sheet/);
    assert.equal(p.elements["paper-print-offer"].hidden, false, "the print offer follows the first saved record");
    assert.match(p.elements["paper-print-status"].textContent, /First record saved/);
    p.elements["paper-print-dismiss"].fire("click");
    assert.equal(p.elements["paper-print-offer"].hidden, true);
  }
  {
    // A batch that already has records does not offer printing again.
    const p = page({kind: "kit", id: "kit", label: "Sample", state: "SEALED", tip_id: "tip"}, false, false,
      {has_records: true});
    await tick(); await tick();
    text(p.elements["seal-subjects"], "Seal broken on the sheet").fire("click");
    const form = find(p.elements["seal-form"], n => n.tag === "form");
    find(form, n => n.tag === "input" && n.type === "text").value = "Synthetic reason";
    form.fire("submit");
    const body = JSON.parse(p.requests[1].options.body);
    assert.equal(p.requests[1].url, "/command/break");
    assert.equal(body.paper_entry_batch_id, "batch");
    assert.equal(body.reason, "Synthetic reason");
    p.requests[1].resolve({status: 200, json: async () => ({status: "COMMITTED", result: {code: "SEAL_BREAK"}})});
    await tick(); await tick();
    assert.equal(p.elements["paper-print-offer"].hidden, true);
  }
  {
    // Each paper refusal is explained in the alert summary.
    const codes = {
      SEAL_NOT_INTACT_AT_PERFORMANCE: /no intact seal on this at the sheet's time/,
      SEAL_LATER_EVIDENCE: /later seal record/,
      PAPER_ITEM_UNIDENTIFIABLE: /cannot be matched to the checklist or kit versions printed on the sheet/,
      KIT_MOVED_SINCE_PAPER: /not on this vehicle at the sheet's time, or has moved since/,
      PAPER_BATCH_INVALIDATED: /paper entry was invalidated/,
      PAPER_BATCH_MISMATCH: /another vehicle/,
      NOT_FOUND: /no longer available/,
      SEAL_CHANGED: /seal changed/,
      FORBIDDEN: /not permitted/,
      INVALID_SEAL_REQUEST: /what you copied from the sheet/,
      SOMETHING_NEW: /not saved \(SOMETHING_NEW\)/,
    };
    for (const [code, expected] of Object.entries(codes)) {
      const p = page({kind: "kit", id: "kit", label: "Sample", state: "SEALED", tip_id: "tip"}, false, false, {});
      await tick(); await tick();
      text(p.elements["seal-subjects"], "Seal intact on the sheet").fire("click");
      const form = find(p.elements["seal-form"], n => n.tag === "form");
      find(form, n => n.tag === "input" && n.type === "text").value = "sample-id";
      find(form, n => n.tag === "input" && n.type === "checkbox").checked = true;
      form.fire("submit");
      p.requests[1].resolve({status: code === "PAPER_BATCH_INVALIDATED" ? 409 : 422,
        json: async () => ({status: "REJECTED", code})});
      await tick(); await tick();
      assert.match(p.elements["seal-error"].textContent, expected, code);
      assert.equal(p.elements["seal-error"].hidden, false, code);
      text(p.elements["seal-subjects"], "Seal intact on the sheet").fire("click");
      assert.equal(p.elements["seal-error"].hidden, true, "a refusal is definite: the next record may start");
    }
  }
  {
    // Kit reseal: the scope at the sheet's time, no inspection start, no nested verification.
    const p = page([
      {kind: "slot", id: "slot", label: "Cabinet", state: "OPEN", tip_id: "cabinet-tip"},
      {kind: "kit", id: "kit", slot_id: "slot", label: "Nested", state: "SEALED", tip_id: "kit-tip"},
    ], false, false, {});
    await tick(); await tick();
    text(p.elements["seal-subjects"], "Resealed on the sheet").fire("click");
    const scope = p.requests[1];
    assert.equal(scope.url, "/scope/slot/slot?paper_entry_batch_id=batch", "no start; the scope names the batch");
    assert.equal(scope.options.method, undefined);
    scope.resolve({ok: true, status: 200, json: async () => ({
      items: [{item_id: "item", kit_id: "kit", label: "Supply", kit_label: "Nested", kind: "QUANTITY",
        expected_quantity: 2, expiration_tracked: true, unit: null}],
      kit_presence: [{kit_id: "kit", assignment_event_id: "assignment", item_id: "presence-item",
        checklist_version_id: "current-version", kit_label: "Nested", label: "Nested present"}],
      kit_lines: [{line_id: "line", label: "Unbound kit", item_id: "line-item", required_count: 1,
        checklist_version_id: null}]})});
    await tick(); await tick();
    const form = find(p.elements["seal-form"], n => n.tag === "form");
    assert.equal(find(form, n => /Verify nested kit/.test(n.textContent)), null, "no nested kit block in paper mode");
    const lineGroup = find(form, n => n.tag === "fieldset" && n.children.some(c => c.textContent === "Count kits: Unbound kit"));
    const lineCount = find(lineGroup, n => n.tag === "input" && n.type === "number");
    lineCount.value = "-1";
    text(lineGroup, "Save kit count from the sheet").fire("click");
    assert.equal(lineCount.attributes["aria-invalid"], "true", "the input to fix is marked");
    assert.match(p.elements["seal-error"].textContent, /nonnegative whole kit count/);
    assert.equal(p.requests.length, 2);
    lineCount.value = "1";
    text(lineGroup, "Save kit count from the sheet").fire("click");
    assert.equal(lineCount.attributes["aria-invalid"], undefined, "a valid entry clears the mark");
    const line = JSON.parse(p.requests[2].options.body);
    assert.equal(p.requests[2].url, "/checks");
    assert.equal(line.paper_entry_batch_id, "batch");
    assert.equal(line.checklist_version_id, "printed-version", "the version printed on the sheet");
    assert.equal(line.kit_requirement_id, "line");
    assert.equal("inspection_start_operation_id" in line, false);
    assert.equal("session_id" in line, false);
    p.requests[2].resolve({status: 200, json: async () => ({status: "COMMITTED", result: {check_id: "line-check"}})});
    await tick(); await tick();
    const presenceGroup = find(form, n => n.tag === "fieldset" && n.children.some(c => c.textContent === "Confirm kit present: Nested"));
    find(presenceGroup, n => n.tag === "input" && n.type === "number").value = "1";
    text(presenceGroup, "Save presence from the sheet").fire("click");
    const presence = JSON.parse(p.requests[3].options.body);
    assert.equal(presence.paper_entry_batch_id, "batch");
    assert.equal(presence.checklist_version_id, "printed-version");
    assert.equal(presence.kit_presence, true);
    assert.equal("inspection_start_operation_id" in presence, false);
    p.requests[3].resolve({status: 200, json: async () => ({status: "COMMITTED", result: {check_id: "presence-check"}})});
    await tick(); await tick();
    find(form, n => n.tag === "input" && n.type === "number" && n !== lineCount &&
      !find(presenceGroup, m => m === n)).value = "2";
    find(form, n => n.tag === "input" && n.type === "date").value = "2026-12-31";
    find(form, n => n.tag === "input" && n.type === "date").fire("change");
    assert.ok(find(form, n => /earliest expiration from the sheet is 2026-12-31/.test(n.textContent)));
    find(form, n => n.tag === "input" && n.type === "text" && n.required).value = "new-seal-id";
    form.fire("submit");
    const reseal = JSON.parse(p.requests[4].options.body);
    assert.equal(p.requests[4].url, "/command/reseal");
    assert.equal(reseal.paper_entry_batch_id, "batch");
    assert.equal(reseal.slot_id, "slot");
    assert.equal(reseal.expected_tip_id, "cabinet-tip");
    assert.equal("start_operation_id" in reseal, false);
    assert.equal("nested_substitution_ids" in reseal, false);
    assert.deepEqual(reseal.kit_count_checks, [{line_id: "line", check_id: "line-check"}]);
    assert.deepEqual(reseal.kit_presence_checks, [{kit_id: "kit", check_id: "presence-check"}]);
    assert.deepEqual(reseal.observations.map(o => [o.item_id, o.count, o.expiration]), [["item", 2, "2026-12-31"]]);
    p.requests[4].resolve({status: 422, json: async () => ({status: "REJECTED", code: "PAPER_ITEM_UNIDENTIFIABLE"})});
    await tick(); await tick();
    assert.match(p.elements["seal-error"].textContent, /seal stays open until a live full inspection/);
  }
  {
    const p = page({kind: "kit", id: "kit", label: "Sample", state: "OPEN", tip_id: "tip"}, false, false, {});
    await tick(); await tick();
    text(p.elements["seal-subjects"], "Resealed on the sheet").fire("click");
    p.requests[1].resolve({ok: false, status: 404, json: async () => ({status: "REJECTED", code: "NOT_FOUND"})});
    await tick(); await tick();
    assert.match(p.elements["seal-error"].textContent, /not sealable at the sheet's time/);
  }
  process.stdout.write("Seal page harness passed\n");
})().catch(error => { console.error(error); process.exitCode = 1; });
