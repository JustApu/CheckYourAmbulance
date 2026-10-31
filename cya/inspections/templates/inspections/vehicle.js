(function () {
  "use strict";
  // Start, join or reopen: each tap sends one request with its own operation id. An answer that
  // is not a definite one is shown as unconfirmed, and the next tap reuses the same id (REC-4).
  var config = JSON.parse(document.getElementById("cya-config").textContent);
  var csrf = document.querySelector("[name=csrfmiddlewaretoken]").value;
  var message = document.getElementById("vehicle-message");
  var pending = {};   // request key -> operation id of an unconfirmed attempt
  function uuid4() {
    var b = new Uint8Array(16);
    crypto.getRandomValues(b);
    b[6] = (b[6] & 15) | 64; b[8] = (b[8] & 63) | 128;
    var h = Array.prototype.map.call(b, function (x) { return ("0" + x.toString(16)).slice(-2); }).join("");
    return [h.slice(0, 8), h.slice(8, 12), h.slice(12, 16), h.slice(16, 20), h.slice(20)].join("-");
  }
  function show(text) { message.textContent = text; message.hidden = !text; }
  function start(template, separate, button) {
    var k = template + (separate ? "|separate" : "");
    var id = pending[k] || uuid4();
    button.disabled = true; show("Opening…");
    fetch(config.sessions_url, {
      method: "POST", credentials: "same-origin", cache: "no-store",
      headers: {"Content-Type": "application/json", "X-CSRFToken": csrf},
      body: JSON.stringify({operation_id: id, vehicle_id: config.vehicle_id, template_id: template, separate: separate})
    }).then(function (r) {
      return r.json().then(function (b) { return {ok: r.ok, status: r.status, body: b}; });
    }).then(function (a) {
      if (a.ok && a.body.status === "COMMITTED") {
        delete pending[k];
        location.assign(config.session_url_template.replace("SESSION", a.body.result.session_id));
        return;
      }
      if (a.body.status === "REJECTED" && a.status < 500) {
        delete pending[k]; button.disabled = false;
        show(a.body.code === "SESSION_UNAVAILABLE" ? "That inspection cannot be started right now." :
             a.body.code === "FORBIDDEN" ? "You are not permitted to do that." : "Not started (" + a.body.code + ").");
        return;
      }
      throw new Error("unconfirmed");
    }).catch(function () {
      pending[k] = id; button.disabled = false;
      show("Unconfirmed: the server did not answer, so the inspection may or may not have started. Tap again to retry the same request.");
    });
  }
  Array.prototype.forEach.call(document.querySelectorAll(".start-session"), function (b) {
    b.addEventListener("click", function () { start(b.dataset.template, false, b); });
  });
  Array.prototype.forEach.call(document.querySelectorAll(".start-separate"), function (b) {
    b.addEventListener("click", function () { start(b.dataset.template, true, b); });
  });
})();
