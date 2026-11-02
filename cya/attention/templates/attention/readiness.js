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
  // Signed out (a 401 from the panel, issue 37): stop every timer and socket and say so. The page
  // never requests /auth/login/ by itself; the person follows the link when they are ready.
  function stop() {
    if (signedOut) { return; }
    signedOut = true; ended = true;
    clearInterval(interval); clearTimeout(timer);
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
    var total = config.vehicle_count, every = "The picture re-reads every " + config.refresh_seconds + " seconds.";
    if (!config.sockets.length) { state.textContent = every; return; }
    if (tried < config.sockets.length) { state.textContent = "Connecting to live updates… " + every; return; }
    state.textContent = (open === config.sockets.length ? "Live" : "Live updates partly lost") +
      " for " + open + " of " + total + " vehicles. " + every;
  }
  function refresh() {
    if (signedOut) { return; }
    if (refreshing) { again = true; return; }
    refreshing = true;
    fetch(config.panel_url, {credentials: "same-origin", cache: "no-store"}).then(function (r) {
      if (r.status === 401) { stop(); return null; }
      if (!r.ok) { throw new Error("panel"); } return r.text();
    }).then(function (html) {
      if (html === null) { return; }
      panel.innerHTML = html; stale.hidden = true;
    }).catch(function () {
      stale.hidden = false;
    }).then(function () {
      refreshing = false;
      if (again && !signedOut) { again = false; schedule(0); }
    });
  }
  function schedule(ms) { if (!signedOut) { clearTimeout(timer); timer = setTimeout(refresh, ms); } }
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
      socket.onmessage = function () { schedule(250); };
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
