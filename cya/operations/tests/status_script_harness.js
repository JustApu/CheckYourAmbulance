/* Offline client regressions for status.js. No browser claim: a minimal DOM, socket and fetch are injected. */
"use strict";
const assert = require("node:assert/strict");
const crypto = require("node:crypto");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const root = path.resolve(__dirname, "../templates/operations");
const source = fs.readFileSync(path.join(root, "status.js"), "utf8");

class Element {
  constructor(id = "") {
    this.id = id; this.value = ""; this.children = []; this.listeners = {}; this.dataset = {};
    this.fields = {}; this.hidden = false; this.disabled = false; this.textContent = ""; this.innerHTML = "";
  }
  addEventListener(name, fn) { this.listeners[name] = fn; }
  fire(name) { return this.listeners[name]({preventDefault() {}}); }
  contains(other) { return other === this; }
  reset() { for (const f of Object.values(this.fields)) f.value = f.defaultValue || ""; this.wasReset = true; }
  querySelector(selector) {
    const named = selector.match(/^\[name="([^"]+)"\]$/);
    if (named) return this.fields[named[1]] || null;
    return (this.queries ||= {})[selector] ||= new Element();
  }
  querySelectorAll(selector) {
    if (selector === '[name="inspection_item"]:checked') return this.selectedItems || [];
    return Object.values(this.fields).concat([this.querySelector('button[type="submit"]')]); }
}

function form(kind, fields, dataset = {}) {
  const f = new Element();
  f.dataset = {kind, ...dataset};
  for (const [name, value] of Object.entries(fields)) { const e = new Element(); e.value = value; f.fields[name] = e; }
  f.fields.csrfmiddlewaretoken = Object.assign(new Element(), {value: "token"});
  f.querySelector('button[type="submit"]').textContent = "Save";
  return f;
}

function run(forms, replies) {
  const config = {vehicle_id: "v1", panel_url: "/panel/", set_url: "/set/", request_url: "/ask/", ad_hoc_url: "/adhoc/",
    decision_url_template: "/api/v1/status-requests/REQUEST/decisions", socket: "/ws/"};
  const ids = {}, sockets = [], calls = [], timers = [];
  for (const id of ["status-live", "status-live-banner", "status-stale", "status-update"]) ids[id] = new Element(id);
  for (const id of ["status-live-banner", "status-stale", "status-update"]) ids[id].hidden = true;
  ids["status-config"] = {textContent: JSON.stringify(config)};
  const document = {
    getElementById: (id) => ids[id], activeElement: null,
    querySelectorAll: (s) => (s === ".status-write" ? forms : []),
  };
  ids["status-live"].querySelectorAll = (s) => (s === ".status-write" ? ids["status-live"].forms || [] : []);
  const context = {
    document, crypto, location: {protocol: "https:", host: "example.test"}, console, Uint8Array, JSON, Promise,
    WebSocket: function (url) { sockets.push(this); this.url = url; },
    setTimeout: (fn) => { timers.push(fn); return timers.length; }, clearTimeout: () => {},
    fetch: (url, options = {}) => {
      calls.push({url, options});
      const reply = replies.shift();
      if (reply instanceof Error) return Promise.reject(reply);
      const answer = {
        ok: reply.ok !== false, status: reply.status || 200,
        json: () => Promise.resolve(reply.body), text: () => Promise.resolve(reply.text || ""),
      };
      return reply.gate ? reply.gate.then(() => answer) : Promise.resolve(answer);
    },
  };
  vm.runInNewContext(source, context);
  return {ids, sockets, calls, timers, document};
}
const settle = () => new Promise((resolve) => setImmediate(resolve));

(async () => {
  // Ad hoc scope preserves each physical kit and uses an explicit UTC due instant.
  {
    const f = form("adhoc", {due_at: "2027-01-01T18:00", explanation: " Synthetic order "});
    f.selectedItems = [{value: "i1", dataset: {kit: "k1"}}, {value: "i1", dataset: {kit: "k2"}}];
    const page = run([f], [new Error("lost response"), {body: {status: "COMMITTED"}}, {text: "fresh"}]);
    f.fire("submit"); await settle(); await settle();
    const first = page.calls[0];
    const sent = JSON.parse(first.options.body);
    assert.equal(first.url, "/adhoc/");
    assert.equal(sent.due_at, "2027-01-01T18:00:00Z");
    assert.equal(sent.explanation, "Synthetic order");
    assert.deepEqual(sent.items, [{item_id: "i1", kit_instance_id: "k1"}, {item_id: "i1", kit_instance_id: "k2"}]);
    assert.match(f.querySelector(".status-result").textContent, /Unconfirmed/);
    f.fields.explanation.value = "changed while unconfirmed";
    f.fire("submit"); await settle(); await settle();
    assert.equal(page.calls[1].options.body, first.options.body);
    assert.equal(f.querySelector(".status-result").textContent, "Saved.");
  }
  {
    const f = form("adhoc", {due_at: "2027-01-01T18:00", explanation: "Synthetic order"});
    const page = run([f], []);
    f.fire("submit"); await settle();
    assert.equal(page.calls.length, 0);
    assert.match(f.querySelector(".status-result").textContent, /Choose 1/);
  }
  // A saved set request sends the contract fields, resets the form, and re-reads the panel.
  {
    const f = form("set", {status: "LIMITED_SERVICE", explanation: " Why ", restrictions: "No siren"});
    const page = run([f], [{body: {status: "COMMITTED"}}, {text: "<p>fresh</p>"}]);
    f.fire("submit"); await settle(); await settle();
    const sent = JSON.parse(page.calls[0].options.body);
    assert.equal(page.calls[0].url, "/set/");
    assert.deepEqual([sent.status, sent.explanation, sent.restrictions], ["LIMITED_SERVICE", "Why", "No siren"]);
    assert.match(sent.operation_id, /^[0-9a-f-]{36}$/);
    assert.equal(page.calls[0].options.headers["X-CSRFToken"], "token");
    assert.equal(f.querySelector(".status-result").textContent, "Saved.");
    assert.equal(f.wasReset, true);
    assert.equal(page.calls[1].url, "/panel/");
    assert.equal(page.ids["status-live"].innerHTML, "<p>fresh</p>");
  }
  // Limited Service without restrictions, and a blank explanation, are refused before any request.
  {
    const f = form("set", {status: "LIMITED_SERVICE", explanation: "Why", restrictions: ""});
    const page = run([f], []);
    f.fire("submit"); await settle();
    assert.equal(page.calls.length, 0);
    assert.match(f.querySelector(".status-result").textContent, /restrictions/);
    f.fields.restrictions.value = "x"; f.fields.explanation.value = " ";
    f.fire("submit"); await settle();
    assert.equal(page.calls.length, 0);
  }
  // A rejection names the failure and a fresh operation id is used on the next attempt.
  {
    const f = form("request", {requested_status: "OUT_OF_SERVICE", reason: "Leak"});
    const page = run([f], [{status: 409, body: {status: "REJECTED", code: "NO_CHANGE"}},
                           {body: {status: "COMMITTED"}}, {text: "x"}]);
    f.fire("submit"); await settle();
    assert.match(f.querySelector(".status-result").textContent, /^Not saved\. The vehicle already has that status/);
    const first = JSON.parse(page.calls[0].options.body).operation_id;
    f.fire("submit"); await settle();
    assert.notEqual(JSON.parse(page.calls[1].options.body).operation_id, first);
  }
  // A transport failure is unconfirmed: the retry sends the identical body and operation id.
  {
    const f = form("decide", {explanation: "No"}, {decision: "DENY", request: "r1"});
    const page = run([f], [new Error("offline"), {body: {status: "COMMITTED"}}, {text: "x"}]);
    f.fire("submit"); await settle();
    assert.match(f.querySelector(".status-result").textContent, /^Unconfirmed/);
    assert.equal(f.unsettled, true);
    assert.equal(f.querySelector('button[type="submit"]').textContent, "Retry same request");
    f.fire("submit"); await settle();
    assert.equal(page.calls[0].url, "/api/v1/status-requests/r1/decisions");
    assert.equal(page.calls[0].options.body, page.calls[1].options.body);
    assert.equal(f.unsettled, false);
  }
  // An approval needs restrictions only when the form asks for them, and sends its decision.
  {
    const f = form("decide", {explanation: "Ok", restrictions: ""}, {decision: "APPROVE", request: "r1"});
    const page = run([f], [{body: {status: "COMMITTED"}}, {text: "x"}]);
    f.fire("submit"); await settle();
    assert.equal(page.calls.length, 0);
    f.fields.restrictions.value = "Daytime only";
    f.fire("submit"); await settle();
    const sent = JSON.parse(page.calls[0].options.body);
    assert.deepEqual([sent.decision, sent.restrictions], ["APPROVE", "Daytime only"]);
  }
  // A live hint re-reads the panel after the debounce; a reconnect reconciles at once.
  {
    const page = run([], [{text: "<p>after hint</p>"}, {text: "<p>after reconnect</p>"}]);
    assert.equal(page.sockets.length, 1);
    assert.equal(page.sockets[0].url, "wss://example.test/ws/");
    page.sockets[0].onmessage({data: "{}"});
    page.timers.pop()(); await settle(); await settle();
    assert.equal(page.ids["status-live"].innerHTML, "<p>after hint</p>");
    page.sockets[0].onopen(); page.timers.pop()(); await settle(); await settle();
    assert.equal(page.ids["status-live"].innerHTML, "<p>after reconnect</p>");
    assert.equal(page.ids["status-live-banner"].hidden, true);
  }
  // A refresh never replaces a form that is saving or unconfirmed; it offers the update instead.
  {
    const f = form("set", {status: "OUT_OF_SERVICE", explanation: "Why"});
    f.unsettled = true;
    const page = run([f], []);
    page.sockets[0].onmessage({data: "{}"});
    page.timers.pop()(); await settle();
    assert.equal(page.calls.length, 0);
    assert.equal(page.ids["status-update"].hidden, false);
  }
  // A save elsewhere on the page must not replace a form that is still unconfirmed: that form's
  // operation id is the only way to learn whether its request was recorded. The re-read waits
  // until the unconfirmed form settles, whether it is then saved or rejected.
  for (const [label, retry] of [["saved", {body: {status: "COMMITTED"}}],
      ["rejected", {status: 409, body: {status: "REJECTED", code: "NO_CHANGE"}}]]) {
    const stuck = form("decide", {explanation: "No"}, {decision: "DENY", request: "r1"});
    const other = form("request", {requested_status: "OUT_OF_SERVICE", reason: "Leak"});
    const page = run([stuck, other], [new Error("offline"), {body: {status: "COMMITTED"}}, retry,
                                      {text: "<p>after " + label + "</p>"}]);
    stuck.fire("submit"); await settle();
    const body = page.calls[0].options.body;
    other.fire("submit"); await settle(); await settle();
    assert.equal(other.querySelector(".status-result").textContent, "Saved.");
    assert.equal(page.calls.length, 2, label + ": no panel read while a form is unconfirmed");
    assert.equal(page.ids["status-live"].innerHTML, "");
    assert.equal(page.ids["status-update"].hidden, false);
    assert.equal(stuck.unsettled, true);
    stuck.fire("submit"); await settle(); await settle();
    assert.equal(page.calls[2].options.body, body, label + ": identical retry");
    assert.equal(page.calls[3].url, "/panel/", label + ": deferred re-read runs once settled");
    assert.equal(page.ids["status-live"].innerHTML, "<p>after " + label + "</p>");
    // The update button cannot bypass an unsettled form either.
  }
  {
    const stuck = form("decide", {explanation: "No"}, {decision: "DENY", request: "r1"});
    const page = run([stuck], [new Error("offline")]);
    stuck.fire("submit"); await settle();
    page.ids["status-update"].querySelector("button").fire("click"); await settle();
    assert.equal(page.calls.length, 1);
    assert.equal(page.ids["status-live"].innerHTML, "");
  }
  // A panel read already in flight must not replace a form submitted while it was pending: the
  // response is discarded (update notice shown) and the re-read runs once the form settles.
  {
    const f = form("set", {status: "OUT_OF_SERVICE", explanation: "Why"});
    let releasePanel, releasePost;
    const panelGate = new Promise((resolve) => { releasePanel = resolve; });
    const postGate = new Promise((resolve) => { releasePost = resolve; });
    const page = run([f], [{text: "<p>stale</p>", gate: panelGate},
                           {body: {status: "COMMITTED"}, gate: postGate}, {text: "<p>fresh</p>"}]);
    page.sockets[0].onmessage({data: "{}"});
    page.timers.pop()(); await settle();                 // panel read now in flight
    assert.equal(page.calls[0].url, "/panel/");
    f.fire("submit"); await settle();                     // form submitted while it is pending
    assert.equal(f.unsettled, true);
    releasePanel(); await settle(); await settle();
    assert.equal(page.ids["status-live"].innerHTML, "", "stale response must not replace the form");
    assert.equal(page.ids["status-update"].hidden, false);
    releasePost(); await settle(); await settle(); await settle();
    assert.equal(f.unsettled, false);
    assert.equal(page.calls[2].url, "/panel/");
    assert.equal(page.ids["status-live"].innerHTML, "<p>fresh</p>");
  }
  // A failed refresh says the screen may be out of date; a lost socket shows the banner and retries.
  {
    const page = run([], [{ok: false, status: 503, text: ""}]);
    page.sockets[0].onmessage({data: "{}"});
    page.timers.pop()(); await settle(); await settle();
    assert.equal(page.ids["status-stale"].hidden, false);
    page.sockets[0].onclose({code: 1006});
    assert.equal(page.ids["status-live-banner"].hidden, false);
    assert.equal(page.timers.length > 0, true);
    const before = page.sockets.length;
    page.timers.pop()();
    assert.equal(page.sockets.length, before + 1);
    const timers = page.timers.length;
    page.sockets[page.sockets.length - 1].onclose({code: 4401});
    assert.equal(page.timers.length, timers);   // sign-in ended: no further reconnect scheduled
  }
  console.log("status script scenarios passed");
})().catch((error) => { console.error(error); process.exit(1); });
