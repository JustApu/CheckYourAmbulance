/* Offline regressions for the fleet readiness screen script. No browser claim: a minimal DOM,
   deferred fetch, fake sockets and manual timers are injected. */
"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const source = fs.readFileSync(path.resolve(__dirname, "../templates/attention/readiness.js"), "utf8");

function run(config) {
  const panel = {
    openVehicleIds: [],
    _html: "initial",
    get innerHTML() { return this._html; },
    set innerHTML(value) { this._html = value; this.openVehicleIds = []; },
    querySelectorAll(selector) {
      if (selector === ".readiness-details[open]") {
        return this.openVehicleIds.map(vehicleId => ({getAttribute: () => vehicleId}));
      }
      if (selector === ".readiness-details") {
        return this.details || [];
      }
      return [];
    },
    details: [],
  };
  const nodes = {
    "readiness-config": {textContent: JSON.stringify(config)},
    "readiness-panel": panel,
    "readiness-live-state": {textContent: "", children: [], appendChild(node) { this.children.push(node); return node; }},
    "readiness-stale": {hidden: true},
  };
  const requests = [], sockets = [], timeouts = [], intervals = [], listeners = {};
  const document = {
    hidden: false,
    getElementById: id => nodes[id],
    createElement: () => ({}),
    addEventListener(name, fn) { listeners[name] = fn; },
  };
  class FakeSocket { constructor(url) { this.url = url; sockets.push(this); } close() { this.closed = true; } }
  const context = {
    document, location: {protocol: "https:", host: "example.invalid"}, JSON,
    WebSocket: FakeSocket,
    setTimeout(fn, ms) { timeouts.push({fn, ms}); return timeouts.length; },
    clearTimeout() {},
    setInterval(fn, ms) { intervals.push({fn, ms}); return intervals.length; },
    clearInterval(id) { if (intervals[id - 1]) { intervals[id - 1].cleared = true; } },
    fetch(url) {
      const request = {url};
      requests.push(request);
      return new Promise((resolve, reject) => { request.resolve = resolve; request.reject = reject; });
    },
  };
  vm.createContext(context);
  vm.runInContext(source, context);
  return {nodes, requests, sockets, timeouts, intervals, listeners, document,
    fire() { const pending = timeouts.splice(0); pending.forEach(t => t.fn()); }};
}
const tick = () => new Promise(resolve => setImmediate(resolve));
const config = {panel_url: "/fleet/readiness/panel/", sockets: ["/ws/a/", "/ws/b/"], vehicle_count: 4, refresh_seconds: 60};

(async () => {
  {
    const page = run(config);
    assert.equal(page.sockets.length, 2);
    assert.equal(page.sockets[0].url, "wss://example.invalid/ws/a/");
    assert.equal(page.intervals[0].ms, 60000);  // the clock-only refresh
    assert.match(page.nodes["readiness-live-state"].textContent, /^Connecting to live updates/);
    page.sockets[0].onopen();
    assert.match(page.nodes["readiness-live-state"].textContent, /^Connecting/);  // one still pending
    page.sockets[1].onopen();
    assert.match(page.nodes["readiness-live-state"].textContent, /^Live for 2 of 4 vehicles/);
    page.fire();  // the reconcile after (re)connecting
    assert.equal(page.requests.length, 1);
    assert.equal(page.requests[0].url, "/fleet/readiness/panel/");
    page.nodes["readiness-panel"].openVehicleIds = ["vehicle-a"];
    page.nodes["readiness-panel"].details = [
      {open: false, getAttribute: () => "vehicle-a"},
      {open: false, getAttribute: () => "vehicle-b"},
    ];
    page.requests[0].resolve({ok: true, text: async () => "<p>fresh</p>"}); await tick(); await tick();
    assert.equal(page.nodes["readiness-panel"].innerHTML, "<p>fresh</p>");
    assert.equal(page.nodes["readiness-panel"].details[0].open, true);
    assert.equal(page.nodes["readiness-panel"].details[1].open, false);
    // A hint only says to re-read.
    page.sockets[1].onmessage({data: "{}"}); page.fire();
    assert.equal(page.requests.length, 2);
    page.requests[1].reject(new Error("synthetic")); await tick(); await tick();
    assert.equal(page.nodes["readiness-stale"].hidden, false);  // honest about stale data
    assert.equal(page.nodes["readiness-panel"].innerHTML, "<p>fresh</p>");
    // A lost socket is reported and retried; the timer keeps refreshing everything.
    page.sockets[0].onclose({code: 1006});
    assert.match(page.nodes["readiness-live-state"].textContent, /partly lost for 1 of 4/);
    page.fire();
    assert.equal(page.sockets.length, 3);  // reconnecting
    page.intervals[0].fn();
    assert.equal(page.requests.length, 3);
    page.requests[2].resolve({ok: true, text: async () => "<p>newer</p>"}); await tick(); await tick();
    assert.equal(page.nodes["readiness-stale"].hidden, true);
    // Sign-in ended: stop promising live updates.
    page.sockets[1].onclose({code: 4401});
    assert.match(page.nodes["readiness-live-state"].textContent, /sign in again/);
  }
  {
    const page = run({...config, sockets: []});  // nothing live: say so plainly
    assert.equal(page.sockets.length, 0);
    assert.equal(page.nodes["readiness-live-state"].textContent, "The picture re-reads every 60 seconds.");
    page.document.hidden = false; page.listeners.visibilitychange(); page.fire();
    assert.equal(page.requests.length, 1);  // shown again: re-read now
  }
  {
    const page = run(config);  // one read at a time; a hint during a read re-reads after it
    page.intervals[0].fn(); page.intervals[0].fn();
    assert.equal(page.requests.length, 1);
    page.requests[0].resolve({ok: true, text: async () => "x"}); await tick(); await tick();
    page.fire();
    assert.equal(page.requests.length, 2);
  }
  {
    // Issue 37: a 401 means signed out. Stop the timer, hints and sockets, say so with a link,
    // and never request /auth/login/ from the page.
    const page = run(config);
    page.sockets[0].onopen(); page.sockets[1].onopen(); page.fire();
    page.requests[0].resolve({ok: false, status: 401, text: async () => ""}); await tick(); await tick();
    const state = page.nodes["readiness-live-state"];
    assert.equal(state.textContent, "Signed out. ");
    assert.equal(state.children[0].href, "/auth/login/");
    assert.equal(page.nodes["readiness-stale"].hidden, true);
    assert.equal(page.nodes["readiness-panel"].innerHTML, "initial");
    assert(page.sockets.every(s => s.closed));
    const sockets = page.sockets.length;
    assert.equal(page.intervals[0].cleared, true);  // the 60-second timer is stopped
    page.intervals[0].fn(); page.sockets[0].onmessage({data: "{}"}); page.sockets[1].onclose({code: 1006});
    page.listeners.visibilitychange(); page.fire();
    assert.equal(page.requests.length, 1);  // nothing more, and no sign-in request
    assert.equal(page.sockets.length, sockets);  // no reconnect
  }
  console.log("Offline readiness script regressions passed: sockets, hints, timer, stale notice, sign-in end, single read, signed-out stop.");
})().catch(error => { console.error(error); process.exitCode = 1; });
