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
  let nextTimeout = 0;
  class FakeSocket { constructor(url) { this.url = url; sockets.push(this); } close() { this.closed = true; } }
  const context = {
    document, location: {protocol: "https:", host: "example.invalid"}, JSON,
    WebSocket: FakeSocket,
    setTimeout(fn, ms) { const t = {fn, ms, id: ++nextTimeout}; timeouts.push(t); return t.id; },
    clearTimeout(id) { const t = timeouts.find(x => x.id === id); if (t) { t.cancelled = true; } },
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
    live: () => timeouts.filter(t => !t.cancelled),
    fire() { const pending = timeouts.splice(0); pending.forEach(t => { if (!t.cancelled) { t.fn(); } }); }};
}
const tick = () => new Promise(resolve => setImmediate(resolve));
const config = {panel_url: "/fleet/readiness/panel/", sockets: ["/ws/a/", "/ws/b/"], vehicle_count: 4, refresh_seconds: 60};

(async () => {
  {
    const page = run(config);
    assert.equal(page.sockets.length, 2);
    assert.equal(page.sockets[0].url, "wss://example.invalid/ws/a/");
    assert.equal(page.intervals[0].ms, 60000);  // the clock-only refresh
    assert.equal(page.nodes["readiness-live-state"].textContent, "Updates every 60 seconds.");
    page.sockets[0].onopen();
    assert.equal(page.nodes["readiness-live-state"].textContent, "Updates every 60 seconds.");  // one still pending
    page.sockets[1].onopen();
    assert.equal(page.nodes["readiness-live-state"].textContent,
      "Updates every 60 seconds. Changes on the first 2 vehicles show at once.");
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
    assert.equal(page.nodes["readiness-live-state"].textContent,
      "Instant updates paused for some vehicles; the page still updates every 60 seconds.");
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
    const page = run({...config, sockets: ["/ws/a/"], vehicle_count: 1});  // every vehicle instant
    page.sockets[0].onopen();
    assert.equal(page.nodes["readiness-live-state"].textContent,
      "Updates every 60 seconds. Changes show at once.");
  }
  {
    const page = run({...config, sockets: []});  // nothing live: say so plainly
    assert.equal(page.sockets.length, 0);
    assert.equal(page.nodes["readiness-live-state"].textContent, "Updates every 60 seconds.");
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
  {
    // Issue #278 C: a burst of hints is gathered for 2 s and read once; later hints never push
    // the read back.
    const page = run(config);
    page.sockets[0].onmessage({data: "{}"});
    page.sockets[1].onmessage({data: "{}"});
    page.sockets[0].onmessage({data: "{}"});
    assert.deepEqual(page.live().map(t => t.ms), [2000], "one 2-second window for a burst of hints");
    page.fire();
    assert.equal(page.requests.length, 1, "a burst costs one panel read");
    // SPEC 8.1.0 §17.3 (OQ-86): the next hint-driven read waits until 10 s after that read began.
    assert.deepEqual(page.live().map(t => t.ms), [10000], "the floor starts with the read");
    page.requests[0].resolve({ok: true, text: async () => "a"}); await tick(); await tick();
    page.sockets[1].onmessage({data: "{}"});
    page.sockets[0].onmessage({data: "{}"});
    assert.deepEqual(page.live().map(t => t.ms), [10000], "hints inside the floor open no window");
    assert.equal(page.requests.length, 1);
    page.fire();  // the floor ends: one read covers both hints
    assert.equal(page.requests.length, 2);
    page.sockets[1].onmessage({data: "{}"});  // a hint during a read no longer forces a re-read
    page.requests[1].resolve({ok: true, text: async () => "b"}); await tick(); await tick();
    assert.equal(page.requests.length, 2, "the hint waits for the floor, not the read's end");
    page.fire();
    assert.equal(page.requests.length, 3, "a hint during a read is never lost");
    page.requests[2].resolve({ok: true, text: async () => "c"}); await tick(); await tick();
    page.fire();  // the floor passes with nothing waiting: no read
    assert.equal(page.requests.length, 3);
    page.sockets[0].onmessage({data: "{}"});  // past the floor: the 2-second window again
    assert.deepEqual(page.live().map(t => t.ms), [2000]);
  }
  {
    // The timer is not a hint: it reads at once inside the floor, covers the waiting hint, and
    // restarts the floor.
    const page = run(config);
    page.sockets[0].onmessage({data: "{}"}); page.fire();
    page.requests[0].resolve({ok: true, text: async () => "a"}); await tick(); await tick();
    page.sockets[0].onmessage({data: "{}"});
    page.intervals[0].fn();
    assert.equal(page.requests.length, 2, "the timer's read is not delayed");
    page.requests[1].resolve({ok: true, text: async () => "b"}); await tick(); await tick();
    const floors = page.live();
    assert.deepEqual(floors.map(t => t.ms), [10000], "one floor, restarted by the timer's read");
    page.fire();
    assert.equal(page.requests.length, 2, "the timer's read covered the hint");
  }
  {
    // A read slower than the floor: a hint during it is read as soon as it ends.
    const page = run(config);
    page.sockets[0].onmessage({data: "{}"}); page.fire();
    page.sockets[1].onmessage({data: "{}"});
    page.fire();  // the floor ends while the read is still in flight
    assert.equal(page.requests.length, 1);
    page.requests[0].resolve({ok: true, text: async () => "a"}); await tick(); await tick();
    assert.equal(page.requests.length, 2, "read at once: the floor has passed");
  }
  {
    // Reconnect and showing the tab again read at once, inside the floor.
    const page = run(config);
    page.sockets[0].onmessage({data: "{}"}); page.fire();
    page.requests[0].resolve({ok: true, text: async () => "a"}); await tick(); await tick();
    page.sockets[1].onopen();
    page.timeouts.filter(t => t.ms === 0 && !t.cancelled).forEach(t => { t.cancelled = true; t.fn(); });
    assert.equal(page.requests.length, 2, "a (re)connect is reconciled at once");
  }
  console.log("Offline readiness script regressions passed: sockets, hints (2-second window, 10-second floor), timer, stale notice, sign-in end, single read, signed-out stop.");
})().catch(error => { console.error(error); process.exitCode = 1; });
