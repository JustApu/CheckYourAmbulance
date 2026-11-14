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
    this.value = ""; this.checked = false; this.hidden = false; this.type = "";
  }
  appendChild(node) { this.children.push(node); return node; }
  replaceChildren() { this.children = []; }
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
function page(subject) {
  const requests = [], elements = {};
  for (const id of ["seal-subjects", "seal-work", "seal-form", "seal-message", "seal-work-heading", "seal-cancel"]) {
    elements[id] = new Node("div");
  }
  const config = {vehicle_id: "vehicle", subjects_url: "/subjects", scope_url: "/scope/KIND/SUBJECT",
    command_url: "/command/ACTION", can_request_audit: false, report_options: {categories: [], severities: []}};
  const document = {
    createElement: tag => new Node(tag),
    getElementById(id) { return id === "seal-config" ? {textContent: JSON.stringify(config)} : elements[id]; },
    querySelector() { return {value: "synthetic-csrf"}; },
  };
  const context = {document, crypto: crypto.webcrypto, Uint8Array, encodeURIComponent,
    fetch(url, options) {
      const request = {url, options}; requests.push(request);
      if (url === "/subjects") return Promise.resolve({ok: true, json: async () => ({subjects:
        Array.isArray(subject) ? subject : [subject]})});
      return new Promise((resolve, reject) => { request.resolve = resolve; request.reject = reject; });
    }};
  vm.createContext(context); vm.runInContext(fs.readFileSync(script, "utf8"), context);
  return {elements, requests};
}

(async () => {
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
    get.resolve({ok: true, json: async () => ({kit_lines: [], items: [{item_id: "item", kit_id: "kit",
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
    p.requests[2].resolve({ok: true, json: async () => ({kit_lines: [], items: [{item_id: "item", kit_id: "kit",
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
  process.stdout.write("Seal page harness passed\n");
})().catch(error => { console.error(error); process.exitCode = 1; });
