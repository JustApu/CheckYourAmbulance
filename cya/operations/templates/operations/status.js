(function () {
  "use strict";
  var config = JSON.parse(document.getElementById("status-config").textContent);
  var live = document.getElementById("status-live");
  var banner = document.getElementById("status-live-banner");
  var stale = document.getElementById("status-stale");
  var update = document.getElementById("status-update");
  function uuid4() {
    var bytes = new Uint8Array(16); crypto.getRandomValues(bytes);
    bytes[6] = (bytes[6] & 15) | 64; bytes[8] = (bytes[8] & 63) | 128;
    var hex = Array.prototype.map.call(bytes, function (b) { return ("0" + b.toString(16)).slice(-2); }).join("");
    return [hex.slice(0, 8), hex.slice(8, 12), hex.slice(12, 16), hex.slice(16, 20), hex.slice(20)].join("-");
  }
  var errors = {
    FORBIDDEN: "You are not permitted to do this.", AUTHENTICATION_REQUIRED: "Sign in again.",
    NOT_FOUND: "That vehicle or request is no longer available.",
    NO_CHANGE: "The vehicle already has that status. Refresh to see the current status.",
    ALREADY_DECIDED: "Someone already decided this request. Refresh to see the outcome.",
    VEHICLE_INACTIVE: "This vehicle is no longer active.",
    INVALID_DUE_AT: "Choose a future due time in UTC.", INVALID_SCOPE: "The selected items changed. Refresh and choose again.",
    MALFORMED_REQUEST: "Check the entries and try again.",
    CSRF_REJECTED: "Refresh the page to renew its security token.",
    OPERATION_REUSED: "This operation has different content. Refresh before trying again."
  };
  function field(form, name) { return form.querySelector('[name="' + name + '"]'); }
  function value(form, name) { var input = field(form, name); return input ? input.value.trim() : ""; }
  function required(text, message) { if (!text) { throw new Error(message); } return text; }
  function build(form) {
    var body = {operation_id: uuid4()}, kind = form.dataset.kind;
    if (kind === "adhoc") {
      body.items = Array.prototype.map.call(form.querySelectorAll('[name="inspection_item"]:checked'), function (input) {
        return {item_id: input.value, kit_instance_id: input.dataset.kit || null};
      });
      if (!body.items.length || body.items.length > 200) { throw new Error("Choose 1–200 items."); }
      body.due_at = required(value(form, "due_at"), "Choose the due time in UTC.") + ":00Z";
      body.explanation = required(value(form, "explanation"), "An explanation is required.");
    }
    if (kind === "set") {
      body.status = required(value(form, "status"), "Choose a status.");
      body.explanation = required(value(form, "explanation"), "An explanation is required.");
      if (body.status === "LIMITED_SERVICE") { body.restrictions = required(value(form, "restrictions"), "Describe the restrictions for Limited Service."); }
    }
    if (kind === "request") {
      body.requested_status = required(value(form, "requested_status"), "Choose a status.");
      body.reason = required(value(form, "reason"), "A reason is required.");
    }
    if (kind === "decide") {
      body.decision = form.dataset.decision;
      body.explanation = required(value(form, "explanation"), "An explanation is required.");
      if (field(form, "restrictions")) { body.restrictions = required(value(form, "restrictions"), "Describe the restrictions for Limited Service."); }
    }
    return body;
  }
  function target(form) {
    if (form.dataset.kind === "adhoc") { return config.ad_hoc_url; }
    if (form.dataset.kind === "set") { return config.set_url; }
    if (form.dataset.kind === "request") { return config.request_url; }
    return config.decision_url_template.replace("REQUEST", form.dataset.request);
  }
  function post(url, body, csrf) {
    return fetch(url, {method: "POST", credentials: "same-origin", cache: "no-store",
      headers: {"Content-Type": "application/json", "X-CSRFToken": csrf}, body: JSON.stringify(body)
    }).then(function (response) {
      return response.json().then(function (data) {
        if (response.ok && data.status === "COMMITTED") { return {kind: "saved"}; }
        if (response.status < 500 && data.status === "REJECTED") { return {kind: "failed", code: data.code}; }
        return {kind: "unconfirmed"};
      });
    }).catch(function () { return {kind: "unconfirmed"}; });
  }
  function controls(form, busy, locked) {
    Array.prototype.forEach.call(form.querySelectorAll("input, select, textarea, button"), function (e) {
      e.disabled = busy || (locked && e.type !== "submit");
    });
  }

  // A form that is saving or unconfirmed must not be replaced by a live refresh: its operation id
  // is the only way to learn whether the request was recorded.
  function unsettled() {
    return Array.prototype.some.call(document.querySelectorAll(".status-write"), function (f) { return f.unsettled; });
  }
  // Forcing a refresh (after a save, or from the update button) skips only the focus check: it
  // never replaces an unsettled form. The re-read is deferred until the last such form settles.
  var refreshing = false, again = false, deferred = false, timer = null;
  function refresh(force) {
    if (refreshing) { again = true; return; }
    if (unsettled()) { update.hidden = false; deferred = true; return; }
    if (!force && live.contains && live.contains(document.activeElement)) { update.hidden = false; return; }
    deferred = false;
    refreshing = true;
    fetch(config.panel_url, {credentials: "same-origin", cache: "no-store"}).then(function (r) {
      if (!r.ok) { throw new Error("panel"); } return r.text();
    }).then(function (html) {
      // Re-check now: a form may have been submitted, or focused, while this read was in flight.
      if (unsettled()) { update.hidden = false; deferred = true; return; }
      if (!force && live.contains && live.contains(document.activeElement)) { update.hidden = false; return; }
      live.innerHTML = html; update.hidden = true; stale.hidden = true; bind(live);
    }).catch(function () {
      stale.hidden = false;   // what is on screen may be out of date
    }).then(function () {
      refreshing = false;
      if (again) { again = false; schedule(0); }
    });
  }
  function schedule(ms) { clearTimeout(timer); timer = setTimeout(function () { refresh(false); }, ms); }
  function settled() { if (deferred) { refresh(true); } }

  function bind(root) {
    Array.prototype.forEach.call(root.querySelectorAll(".status-write"), function (form) {
      if (form.bound) { return; }
      form.bound = true;
      var request = null, busy = false;
      var message = form.querySelector(".status-result"), button = form.querySelector('button[type="submit"]');
      var original = button.textContent, csrf = field(form, "csrfmiddlewaretoken").value;
      form.addEventListener("submit", function (event) {
        event.preventDefault(); if (busy) { return; }
        if (!request) {
          try { request = build(form); } catch (error) { message.textContent = error.message; return; }
        }
        busy = true; form.unsettled = true; controls(form, true, true); button.textContent = "Saving…";
        message.textContent = "Pending — waiting for acknowledgement.";
        post(target(form), request, csrf).then(function (answer) {
          busy = false;
          if (answer.kind === "saved") {
            request = null; form.unsettled = false; controls(form, false, false); button.textContent = original;
            if (form.reset) { form.reset(); }
            message.textContent = "Saved.";
            refresh(true);
          } else if (answer.kind === "failed") {
            request = null; form.unsettled = false; controls(form, false, false); button.textContent = original;
            message.textContent = "Not saved. " + (errors[answer.code] || ("Not accepted (" + answer.code + ")."));
            settled();
          } else {
            controls(form, false, true); button.textContent = "Retry same request";
            message.textContent = "Unconfirmed: this may have been recorded. Retry sends the identical request and operation id; resolve it before editing.";
          }
        });
      });
    });
  }
  bind(document);
  update.querySelector("button").addEventListener("click", function () { refresh(true); });

  var socket = null, backoff = 1000;
  function connect() {
    var scheme = location.protocol === "https:" ? "wss://" : "ws://";
    socket = new WebSocket(scheme + location.host + config.socket);
    socket.onopen = function () { backoff = 1000; banner.hidden = true; schedule(0); };  // reconcile after a gap
    socket.onmessage = function () { schedule(250); };  // a hint only says to re-read
    socket.onclose = function (event) {
      banner.hidden = false;
      if (event.code === 4401 || event.code === 4403) { return; }   // sign-in ended: do not promise more
      setTimeout(connect, backoff); backoff = Math.min(backoff * 2, 30000);
    };
  }
  connect();
})();
