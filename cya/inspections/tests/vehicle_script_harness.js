/* Offline vehicle-page regressions (issue 27). No browser claim: a minimal DOM and deferred fetch
   are injected. Usage: node vehicle_script_harness.js [path/to/vehicle.js] */
"use strict";
const assert = require("node:assert/strict");
const crypto = require("node:crypto");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const root = path.resolve(__dirname, "../../..");
const script = process.argv[2] || path.join(root, "cya/inspections/templates/inspections/vehicle.js");
const SESSIONS = "/inspection/sessions/";
const PAGE = "https://example.invalid/inspection/vehicles/synthetic-vehicle/";
const tick = () => new Promise(resolve => setImmediate(resolve));

function page() {
  const requests = [], assigned = [];
  const message = {textContent: "", hidden: true};
  const button = {dataset: {template: "synthetic-template"}, disabled: false, listeners: {},
    addEventListener(name, fn) { this.listeners[name] = fn; }, tap() { this.listeners.click(); }};
  const config = {sessions_url: "/api/v1/inspection-sessions", vehicle_id: "synthetic-vehicle",
    session_url_template: SESSIONS + "SESSION/"};
  const document = {
    getElementById(id) {
      if (id === "cya-config") return {textContent: JSON.stringify(config)};
      if (id === "vehicle-message") return message;
      throw new Error("unexpected element " + id);
    },
    querySelector() { return {value: "synthetic-csrf"}; },
    querySelectorAll(selector) { return selector === ".start-session" ? [button] : []; },
  };
  const context = {
    document, crypto: crypto.webcrypto, Uint8Array,
    location: {assign(url) { assigned.push(url); }},
    fetch(url, options) {
      const request = {url, body: JSON.parse(options.body)};
      requests.push(request);
      return new Promise((resolve, reject) => { request.resolve = resolve; request.reject = reject; });
    },
  };
  vm.createContext(context);
  vm.runInContext(fs.readFileSync(script, "utf8"), context);
  return {button, message, requests, assigned};
}
function answer(request, status, body) {
  request.resolve({ok: status < 400, status, json: async () => body});
}
const committed = id => ({status: "COMMITTED", result: {session_id: id}});

// The navigated URL resolves to one segment under /inspection/sessions/ that decodes to the id.
function assertOneSegment(url, id) {
  const resolved = new URL(url, PAGE);
  assert.equal(resolved.origin, new URL(PAGE).origin, url);
  assert.ok(resolved.pathname.startsWith(SESSIONS), url);
  const segment = resolved.pathname.slice(SESSIONS.length);
  assert.match(segment, /^[^/]+\/$/, url);
  assert.equal(decodeURIComponent(segment.slice(0, -1)), id, url);
  assert.equal(resolved.search + resolved.hash, "", url);
}

(async () => {
  {
    // A canonical UUID navigates to its session page, unchanged by encoding.
    const run = page();
    const id = "0f8b6c1e-6a3d-4c1e-9b2a-5d7e8f9a0b1c";
    run.button.tap(); answer(run.requests[0], 200, committed(id)); await tick(); await tick();
    assert.deepEqual(run.assigned, [SESSIONS + id + "/"]);
  }
  for (const id of ["../../administration/roles", "a/b", "/absolute", "javascript:alert(1)",
    "https://elsewhere.invalid/", "$&$'$`$$", "<img src=x onerror=alert(1)>", "a?b#c", "%2e%2e",
    "...", "..a", "é\u0000"]) {
    // Malformed ids stay one encoded path segment under /inspection/sessions/.
    const run = page();
    run.button.tap(); answer(run.requests[0], 200, committed(id)); await tick(); await tick();
    assert.equal(run.assigned.length, 1, id);
    assertOneSegment(run.assigned[0], id);
  }
  for (const id of ["", ".", "..", null, undefined, 7]) {
    // Ids that cannot be one segment never navigate; the answer is treated as unconfirmed.
    const run = page();
    run.button.tap(); answer(run.requests[0], 200, committed(id)); await tick(); await tick();
    assert.deepEqual(run.assigned, [], String(id));
    assert.ok(run.message.textContent.startsWith("Unconfirmed"), String(id));
    run.button.tap();
    assert.equal(run.requests[1].body.operation_id, run.requests[0].body.operation_id, String(id));
  }
  for (const [status, code, text] of [[409, "SESSION_UNAVAILABLE", "cannot be started"],
    [403, "FORBIDDEN", "not permitted"]]) {
    // A rejected answer does not navigate; the next tap is a new operation.
    const run = page();
    run.button.tap(); answer(run.requests[0], status, {status: "REJECTED", code}); await tick(); await tick();
    assert.deepEqual(run.assigned, []);
    assert.ok(run.message.textContent.includes(text));
    assert.equal(run.button.disabled, false);
    run.button.tap();
    assert.notEqual(run.requests[1].body.operation_id, run.requests[0].body.operation_id);
  }
  for (const fail of [
    request => request.reject(new Error("synthetic lost response")),
    request => answer(request, 503, {status: "REJECTED", code: "UNAVAILABLE"}),
    request => answer(request, 500, {status: "COMMITTED", result: {session_id: "x"}}),
  ]) {
    // An unconfirmed answer (failed or 5xx) does not navigate; the retry reuses the operation id.
    const run = page();
    run.button.tap(); fail(run.requests[0]); await tick(); await tick();
    assert.deepEqual(run.assigned, []);
    assert.ok(run.message.textContent.startsWith("Unconfirmed"));
    run.button.tap();
    assert.equal(run.requests[1].body.operation_id, run.requests[0].body.operation_id);
  }
  console.log("Offline vehicle script regressions passed: UUID navigation, one encoded segment for malformed ids, refused dot segments, rejected and unconfirmed answers.");
})().catch(error => { console.error(error); process.exitCode = 1; });
