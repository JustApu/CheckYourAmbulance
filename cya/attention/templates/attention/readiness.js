(function () {
  "use strict";
  // Re-reads the panel on a vehicle hint, on reconnect, when the tab is shown again and on a timer.
  // The timer also covers changes no event announces: a deadline or acknowledgement passing.
  var config = JSON.parse(document.getElementById("readiness-config").textContent);
  var panel = document.getElementById("readiness-panel");
  var state = document.getElementById("readiness-live-state");
  var stale = document.getElementById("readiness-stale");
  var refreshing = false, again = false, timer = null, open = 0, ended = false, tried = 0;
  var signedOut = false, interval = null, sockets = [];
  // Issue #278 C: vehicle hints are gathered for up to PUSH_WINDOW_MS, then read once. A busy
  // shift sends several hints a second; each used to cost a whole panel read. The window starts
  // at the first hint and is never extended by later ones.
  // SPEC 8.1.0 §17.3 (OQ-86): this screen re-reads in response to hints at most once every
  // PUSH_FLOOR_MS, measured from the start of its previous read, whatever started that read. A
  // hint inside the floor waits for its end, and one read covers every hint received meanwhile.
  // Reads on (re)connect, when the tab is shown again and on the timer are not delayed.
  var PUSH_WINDOW_MS = 2000, pushTimer = null;
  var PUSH_FLOOR_MS = 10000, floorTimer = null, cooling = false, pending = false;
  // Signed out (a 401 from the panel, issue 37): stop every timer and socket and say so. The page
  // never requests /auth/login/ by itself; the person follows the link when they are ready.
  function stop() {
    if (signedOut) { return; }
    signedOut = true; ended = true;
    clearInterval(interval); clearTimeout(timer); clearTimeout(pushTimer); clearTimeout(floorTimer);
    sockets.forEach(function (s) { s.retired = true; if (s.close) { s.close(); } });
    state.textContent = "Signed out. ";
    var link = document.createElement("a");
    link.href = "/auth/login/"; link.textContent = "Sign in again";
    state.appendChild(link);
    stale.hidden = true;  // its "keeps trying" no longer holds
  }
  function describe() {
    if (signedOut) { return; }
    if (ended) { state.textContent = "Live updates stopped: sign in again to resume."; return; }
    // Plain words (issue #235 C3): every vehicle re-reads on the timer; at most a few also get
    // instant updates, because each browser may hold only a few sockets.
    var total = config.vehicle_count, n = config.sockets.length;
    var every = "Updates every " + config.refresh_seconds + " seconds.";
    if (!n || tried < n) { state.textContent = every; return; }
    if (open < n) {
      state.textContent = "Instant updates paused for some vehicles; the page still updates every " +
        config.refresh_seconds + " seconds.";
      return;
    }
    // SPEC 8.1.0 §17.3: hint-driven reads are at most 10 seconds apart, so not "at once".
    state.textContent = every + (n >= total ? " Changes show within 10 seconds." :
      " Changes on the first " + n + " vehicles show within 10 seconds.");
  }
  function refresh() {
    if (signedOut) { return; }
    if (refreshing) { again = true; return; }
    refreshing = true;
    // This read starts after every hint so far, so it covers any still gathering or waiting.
    if (pushTimer) { clearTimeout(pushTimer); pushTimer = null; }
    pending = false;
    cooling = true; clearTimeout(floorTimer);
    floorTimer = setTimeout(cooled, PUSH_FLOOR_MS);
    fetch(config.panel_url, {credentials: "same-origin", cache: "no-store"}).then(function (r) {
      if (r.status === 401) { stop(); return null; }
      if (!r.ok) { throw new Error("panel"); } return r.text();
    }).then(function (html) {
      if (html === null) { return; }
      var openVehicles = Array.prototype.map.call(
        panel.querySelectorAll(".readiness-details[open]"),
        function (details) { return details.getAttribute("data-vehicle-id"); }
      );
      panel.innerHTML = html;
      Array.prototype.forEach.call(panel.querySelectorAll(".readiness-details"), function (details) {
        if (openVehicles.indexOf(details.getAttribute("data-vehicle-id")) !== -1) { details.open = true; }
      });
      stale.hidden = true;
    }).catch(function () {
      stale.hidden = false;
    }).then(function () {
      refreshing = false;
      if (again && !signedOut) { again = false; schedule(0); }
      else if (pending && !cooling && !signedOut) { refresh(); }  // a slow read outlasted the floor
    });
  }
  function cooled() {
    floorTimer = null; cooling = false;
    if (pending && !refreshing && !signedOut) { refresh(); }
  }
  function schedule(ms) { if (!signedOut) { clearTimeout(timer); timer = setTimeout(refresh, ms); } }
  function hinted() {
    if (signedOut) { return; }
    pending = true;
    // Inside the floor, or during a read: the floor's end (or the read's, if later) reads.
    if (cooling || refreshing || pushTimer) { return; }
    pushTimer = setTimeout(function () { pushTimer = null; refresh(); }, PUSH_WINDOW_MS);
  }
  interval = setInterval(refresh, config.refresh_seconds * 1000);
  document.addEventListener("visibilitychange", function () { if (!document.hidden) { schedule(0); } });
  config.sockets.forEach(function (path) {
    var backoff = 1000, first = false;  // first: this vehicle's first attempt has settled
    function connect() {
      if (signedOut) { return; }
      var scheme = location.protocol === "https:" ? "wss://" : "ws://";
      var socket = new WebSocket(scheme + location.host + path), counted = false;
      sockets.push(socket);
      function attempted() { if (!socket.attempted) { socket.attempted = true; if (!first) { first = true; tried += 1; } } }
      socket.onopen = function () { attempted(); backoff = 1000; counted = true; open += 1; describe(); schedule(0); };
      socket.onmessage = hinted;
      socket.onclose = function (event) {
        if (socket.retired) { return; }
        attempted();
        if (counted) { open -= 1; }
        if (event.code === 4401 || event.code === 4403) { ended = true; describe(); return; }
        describe();
        setTimeout(connect, backoff); backoff = Math.min(backoff * 2, 30000);
      };
    }
    connect();
  });
  describe();
})();
