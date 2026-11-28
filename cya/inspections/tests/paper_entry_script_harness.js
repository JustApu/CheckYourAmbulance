/* Offline Paper entry setup regressions (issue #9 B6). No browser claim: a minimal DOM and
   deferred fetch are injected. Usage: node paper_entry_script_harness.js [path/to/paper_entry.js] */
"use strict";
const assert = require("node:assert/strict");
const crypto = require("node:crypto");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const root = path.resolve(__dirname, "../../..");
const script = process.argv[2] || path.join(root, "cya/inspections/templates/inspections/paper_entry.js");
const PAPER = "/inspection/paper-entry/";
const PAGE = "https://example.invalid/inspection/vehicles/synthetic-vehicle/paper-entry/";
const UUID4 = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
const tick = () => new Promise(resolve => setImmediate(resolve));

class Element {
  constructor(document, tag = "", id = "") {
    this._document = document; this.tagName = tag; this.id = id; this.className = "";
    this.children = []; this.parentNode = null; this.listeners = {}; this.dataset = {}; this.attributes = {};
    this.hidden = false; this.disabled = false; this._text = ""; this._value = ""; this.type = "";
  }
  set textContent(text) { this._text = String(text); for (const c of this.children) c.parentNode = null; this.children = []; }
  get textContent() { return this._text + this.children.map(c => c.textContent).join(""); }
  get value() {
    if (this.tagName !== "select") return this._value;
    return this._value;
  }
  set value(v) { this._value = String(v); }
  get firstChild() { return this.children[0]; }
  appendChild(node) { node.parentNode = this; this.children.push(node); return node; }
  removeChild(node) {
    this.children.splice(this.children.indexOf(node), 1); node.parentNode = null;
    if (this._document.activeElement && node.contains(this._document.activeElement)) this._document.activeElement = null;
  }
  contains(node) { for (let n = node; n; n = n.parentNode) { if (n === this) return true; } return false; }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  getAttribute(name) { return name in this.attributes ? this.attributes[name] : null; }
  removeAttribute(name) { delete this.attributes[name]; }
  addEventListener(name, fn) { (this.listeners[name] ||= []).push(fn); }
  fire(name) { for (const fn of this.listeners[name] || []) fn({preventDefault() {}}); }
  focus() { this._document.activeElement = this; }
  descendants() { return this.children.flatMap(c => [c, ...c.descendants()]); }
}

function page(extra = {}) {
  const requests = [], assigned = [], timers = [], elements = {};
  const config = {
    vehicle_id: "synthetic-vehicle", template_id: "synthetic-template", user_id: "me",
    own_name: "Synthetic <b>Crew</b>", can_attribute: false,
    urls: {batches: "/api/v1/paper-entry-batches", performers: "/api/v1/paper-entry-performers"},
    versions: [
      {version_id: "v2", label: "Own, revision 2 — Version ID v2 <script>", current: true,
        kits: [{definition_id: "d1", label: "Jump kit", versions: [
          {version_id: "k2", label: "Jump kit, revision 2", current: true},
          {version_id: "k1", label: "Jump kit, revision 1", current: false}]}]},
      {version_id: "v1", label: "Own, revision 1 — Version ID v1", current: false, kits: []},
    ],
    ...extra,
  };
  const document = {
    activeElement: null,
    getElementById(id) {
      if (id === "cya-config") return {textContent: JSON.stringify(config)};
      if (!config.can_attribute && /^performer-(search|add-self|results|chosen|count)/.test(id)) return null;
      if (config.can_attribute && id === "performer-self") return null;
      if (!elements[id]) {
        const found = Object.values(elements).flatMap(e => [e, ...e.descendants()]).find(e => e.id === id);
        if (found) return found;
        elements[id] = new Element(document, id === "paper-version" ? "select" : "", id);
      }
      return elements[id];
    },
    querySelector() { return {value: "synthetic-csrf"}; },
    querySelectorAll(selector) {
      assert.equal(selector, ".paper-kit-select");
      return elements["paper-kit-fields"].children.filter(c => c.className === "paper-kit-select");
    },
    createElement(tag) { return new Element(document, tag); },
  };
  const context = {
    document, crypto: crypto.webcrypto, Uint8Array,
    location: {assign(url) { assigned.push(url); }},
    setTimeout(fn) { timers.push(fn); return timers.length; }, clearTimeout(id) { if (id) timers[id - 1] = null; },
    fetch(url, options = {}) {
      const request = {url, body: options.body ? JSON.parse(options.body) : null, options};
      requests.push(request);
      return new Promise((resolve, reject) => { request.resolve = resolve; request.reject = reject; });
    },
  };
  vm.createContext(context);
  vm.runInContext(fs.readFileSync(script, "utf8"), context);
  const get = id => document.getElementById(id);
  return {
    config, document, requests, assigned, timers, get,
    posts: () => requests.filter(r => r.body),
    searches: () => requests.filter(r => !r.body),
    fill(date = "2026-06-02", time = "06:15") {
      get("paper-date").value = date; get("paper-date").fire("input");
      get("paper-time").value = time; get("paper-time").fire("input");
    },
    submit() { get("paper-setup").fire("submit"); },
    type(text) { get("performer-search").value = text; get("performer-search").fire("input"); },
    runTimers() { const due = timers.splice(0); due.forEach(fn => fn && fn()); },
    kitSelects() { return get("paper-kit-fields").children.filter(c => c.className === "paper-kit-select"); },
  };
}
function answer(request, status, body) { request.resolve({ok: status < 400, status, json: async () => body}); }
const committed = id => ({status: "COMMITTED", result: {batch_id: id}});
function assertOneSegment(url, id) {
  const resolved = new URL(url, PAGE);
  assert.equal(resolved.origin, new URL(PAGE).origin, url);
  assert.ok(resolved.pathname.startsWith(PAPER), url);
  const segment = resolved.pathname.slice(PAPER.length);
  assert.match(segment, /^[^/]+\/$/, url);
  assert.equal(decodeURIComponent(segment.slice(0, -1)), id, url);
  assert.equal(resolved.search + resolved.hash, "", url);
}
function person(id, name, active = true) { return {user_id: id, display_name: name, email: id + "@example.invalid", active}; }

(async () => {
  {
    // A Crew member names only themself; the version and kit version in force now are the
    // defaults; the header is sent once and opens the paper screen (REC-5, LATE-2 to LATE-4).
    const run = page();
    assert.equal(run.get("performer-self").textContent, "You (Synthetic <b>Crew</b>)", "the name is text, never markup");
    assert.equal(run.get("paper-version").value, "v2", "the version in force now is the default");
    assert.equal(run.get("paper-version").children[0].textContent, "Own, revision 2 — Version ID v2 <script>");
    assert.equal(run.kitSelects().length, 1);
    assert.equal(run.kitSelects()[0].value, "k2", "the kit version in force now is the default");
    assert.equal(run.kitSelects()[0].children[0].textContent, "Kit reference not used");
    assert.equal(run.get("paper-date").value, "", "the date is never prefilled");
    run.fill(); run.submit();
    const [sent] = run.posts();
    assert.match(sent.body.operation_id, UUID4);
    assert.deepEqual({...sent.body, operation_id: "x"}, {operation_id: "x", vehicle_id: "synthetic-vehicle",
      checklist_version_id: "v2", performed_on: "2026-06-02", performed_time: "06:15", performer_ids: ["me"],
      kit_definition_version_ids: ["k2"]});
    assert.equal(sent.url, "/api/v1/paper-entry-batches");
    assert.equal(sent.options.headers["X-CSRFToken"], "synthetic-csrf");
    assert.equal(run.get("paper-start").disabled, true, "one request at a time");
    run.submit();
    assert.equal(run.posts().length, 1, "a second Start while the first is out sends nothing");
    const id = "0f8b6c1e-6a3d-4c1e-9b2a-5d7e8f9a0b1c";
    answer(sent, 201, committed(id)); await tick(); await tick();
    assert.deepEqual(run.assigned, [PAPER + id + "/"]);
  }
  {
    // "Kit reference not used" sends no kit version; another printed version lists its own kits.
    const run = page();
    run.kitSelects()[0].value = ""; run.kitSelects()[0].fire("change");
    run.fill("2026-06-02", "06:15:00"); run.submit();
    assert.equal("kit_definition_version_ids" in run.posts()[0].body, false);
    assert.equal(run.posts()[0].body.performed_time, "06:15", "whole minutes are sent");
    run.get("paper-version").value = "v1"; run.get("paper-version").fire("change");
    assert.equal(run.kitSelects().length, 0);
    assert.equal(run.get("paper-kits").hidden, true, "a version placing no kit asks for none");
  }
  {
    // Missing answers are listed in an error summary that takes focus; nothing is sent.
    const run = page();
    run.submit();
    assert.equal(run.posts().length, 0);
    assert.equal(run.get("paper-errors").hidden, false);
    assert.equal(run.document.activeElement, run.get("paper-errors"));
    assert.equal(run.get("paper-date").getAttribute("aria-invalid"), "true");
    assert.equal(run.get("paper-time").getAttribute("aria-invalid"), "true");
    assert.ok(run.get("paper-error-list").textContent.includes("Enter the date written on the sheet."));
    run.fill(); run.submit();
    assert.equal(run.get("paper-date").getAttribute("aria-invalid"), null, "a new attempt clears the marks");
  }
  {
    // REC-4: an unconfirmed answer keeps the exact request and operation id for the retry; a
    // changed answer is a new decision with a new id.
    for (const fail of [r => r.reject(new Error("lost")), r => answer(r, 503, {status: "REJECTED", code: "RETRY_LATER"}),
      r => answer(r, 500, {status: "COMMITTED", result: {batch_id: "x"}})]) {
      const run = page();
      run.fill(); run.submit(); fail(run.posts()[0]); await tick(); await tick();
      assert.deepEqual(run.assigned, []);
      assert.ok(run.get("paper-state").textContent.startsWith("Unconfirmed"));
      assert.equal(run.get("paper-start").disabled, false);
      run.submit();
      assert.deepEqual(run.posts()[1].body, run.posts()[0].body, "the retry is the same request");
      run.posts()[1].reject(new Error("lost")); await tick(); await tick();
      run.get("paper-time").value = "06:20"; run.get("paper-time").fire("input"); run.submit();
      assert.notEqual(run.posts()[2].body.operation_id, run.posts()[0].body.operation_id);
      assert.equal(run.posts()[2].body.performed_time, "06:20");
    }
  }
  {
    // A definite refusal marks the named field and says why; the next Start is a new request.
    const run = page();
    run.fill(); run.submit();
    answer(run.posts()[0], 422, {status: "REJECTED", code: "PERFORMED_IN_FUTURE", field_errors: [{path: "performed_time", code: "PERFORMED_IN_FUTURE"}]});
    await tick(); await tick();
    assert.deepEqual(run.assigned, []);
    assert.equal(run.get("paper-time").getAttribute("aria-invalid"), "true");
    assert.ok(run.get("paper-error-list").textContent.includes("later than now"));
    assert.equal(run.document.activeElement, run.get("paper-errors"));
    run.submit();
    assert.notEqual(run.posts()[1].body.operation_id, run.posts()[0].body.operation_id);
  }
  for (const id of ["../../administration/roles", "a/b", "/absolute", "javascript:alert(1)",
    "https://elsewhere.invalid/", "<img src=x onerror=alert(1)>", "a?b#c", "%2e%2e", "..."]) {
    // Malformed ids stay one encoded path segment under the constant paper path (issue 27).
    const run = page();
    run.fill(); run.submit(); answer(run.posts()[0], 201, committed(id)); await tick(); await tick();
    assert.equal(run.assigned.length, 1, id);
    assertOneSegment(run.assigned[0], id);
  }
  for (const id of ["", ".", "..", null, undefined, 7]) {
    // Ids that cannot be one segment never navigate; the answer is unconfirmed and retried as is.
    const run = page();
    run.fill(); run.submit(); answer(run.posts()[0], 201, committed(id)); await tick(); await tick();
    assert.deepEqual(run.assigned, [], String(id));
    assert.ok(run.get("paper-state").textContent.startsWith("Unconfirmed"), String(id));
    run.submit();
    assert.equal(run.posts()[1].body.operation_id, run.posts()[0].body.operation_id, String(id));
  }
  {
    // A Supervisor searches (debounced, 2 to 100 characters); nobody is chosen for them.
    const run = page({can_attribute: true});
    assert.equal(run.get("performer-count").textContent, "Nobody chosen yet.");
    run.fill(); run.submit();
    assert.equal(run.posts().length, 0, "no performer, no request");
    assert.equal(run.get("performer-search").getAttribute("aria-invalid"), "true");
    run.type("S"); run.runTimers();
    assert.equal(run.searches().length, 0);
    assert.equal(run.get("performer-search-status").textContent, "Type at least 2 characters.");
    run.type("Sy"); run.type("Syn");
    assert.equal(run.searches().length, 0, "typing waits for a pause");
    run.runTimers();
    assert.equal(run.searches().length, 1, "only the last text is searched");
    assert.equal(run.searches()[0].url, "/api/v1/paper-entry-performers?q=Syn");
    run.type("Synth & co"); run.runTimers();
    assert.equal(run.searches()[1].url, "/api/v1/paper-entry-performers?q=Synth%20%26%20co");
    answer(run.searches()[1], 200, {status: "OK", users: [person("u1", "Synthetic One"), person("u2", "Synthetic <i>Two</i>", false)], truncated: true});
    await tick(); await tick();
    answer(run.searches()[0], 200, {status: "OK", users: [person("stale", "Stale")], truncated: false});
    await tick(); await tick();
    const results = run.get("performer-results").children;
    assert.equal(results.length, 2, "an older answer never replaces a newer search");
    assert.equal(results[1].children[0].textContent, "Add Synthetic <i>Two</i> (inactive)");
    assert.ok(run.get("performer-search-status").textContent.includes("type more of the name"));
    results[1].children[0].fire("click");
    assert.equal(run.get("performer-chosen").children.length, 1);
    assert.equal(run.document.activeElement, run.get("performer-search"), "focus stays in the search after adding");
    assert.equal(run.get("performer-count").textContent, "1 performer chosen.");
    assert.equal(run.get("performer-results").children[1].children[0].disabled, true, "a chosen person is not offered again");
    run.get("performer-add-self").fire("click");
    assert.equal(run.get("performer-count").textContent, "2 performers chosen.");
    run.submit();
    assert.deepEqual(run.posts()[0].body.performer_ids, ["u2", "me"]);
    run.posts()[0].reject(new Error("lost")); await tick(); await tick();
    const chip = run.get("performer-chosen").children[0];
    assert.equal(chip.children[1].getAttribute("aria-label"), "Remove Synthetic <i>Two</i> (inactive)");
    chip.children[1].fire("click");
    assert.equal(run.document.activeElement, run.get("performer-search"), "removing never drops focus to the page");
    assert.equal(run.get("performer-count").textContent, "1 performer chosen.");
    run.submit();
    assert.notEqual(run.posts()[1].body.operation_id, run.posts()[0].body.operation_id, "a changed performer set is a new request");
    assert.deepEqual(run.posts()[1].body.performer_ids, ["me"]);
  }
  {
    // At most 20 performers; a refused search says so.
    const run = page({can_attribute: true});
    run.type("Synthetic"); run.runTimers();
    const users = Array.from({length: 21}, (_, i) => person("p" + i, "Synthetic " + i));
    answer(run.searches()[0], 200, {status: "OK", users, truncated: false}); await tick(); await tick();
    for (let i = 0; i < 21; i++) run.get("performer-results").children[i].children[0].fire("click");
    assert.equal(run.get("performer-chosen").children.length, 20);
    assert.ok(run.get("performer-search-status").textContent.includes("at most 20"));
    run.type("Other"); run.runTimers();
    answer(run.searches()[1], 403, {status: "REJECTED", code: "FORBIDDEN"}); await tick(); await tick();
    assert.equal(run.get("performer-search-status").textContent, "You are not permitted to choose other performers.");
  }
  console.log("Offline paper entry setup regressions passed: defaults, header body, validation summary, REC-4 retry and new decisions, refusals, one encoded segment navigation, performer search, focus and limits.");
})().catch(error => { console.error(error); process.exitCode = 1; });
