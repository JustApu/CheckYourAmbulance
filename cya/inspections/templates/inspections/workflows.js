(function () {
  "use strict";
  var configNode = document.getElementById("workflow-config");
  var config = configNode ? JSON.parse(configNode.textContent) : {};
  function uuid4() {
    var bytes = new Uint8Array(16); crypto.getRandomValues(bytes);
    bytes[6] = (bytes[6] & 15) | 64; bytes[8] = (bytes[8] & 63) | 128;
    var hex = Array.prototype.map.call(bytes, function (b) { return ("0" + b.toString(16)).slice(-2); }).join("");
    return [hex.slice(0, 8), hex.slice(8, 12), hex.slice(12, 16), hex.slice(16, 20), hex.slice(20)].join("-");
  }
  var errors = {
    FORBIDDEN: "You are not permitted to do this.", AUTHOR_REQUIRED: "Only the author can correct this check. Record a fresh check instead.",
    ADMINISTRATOR_REQUIRED: "Only an administrator can invalidate another person's check.",
    SELF_INVALIDATION_FORBIDDEN: "Correct your own entry instead of invalidating it.",
    CHAIN_CHANGED: "The check changed. Refresh history before making a new decision.",
    CHAIN_INVALIDATED: "This chain was invalidated. Record a fresh check instead.",
    SESSION_NOT_OPEN: "Reopen the inspection session before correcting its checks.",
    NOT_FOUND: "This record is no longer available.", AUTHENTICATION_REQUIRED: "Sign in again.",
    UNKNOWN_CATEGORY: "Report choices changed. Refresh and choose a current category.",
    INVALID_SEVERITY: "Report choices changed. Refresh and choose a current severity.",
    EMPTY_SCOPE: "That scope has no current checklist items. Refresh the report or contact a supervisor before recording a repair.",
    INELIGIBLE_CHECK: "This is no longer an eligible linked Unable check. Refresh history.",
    INELIGIBLE_OBLIGATION: "This obligation changed or is no longer current. Refresh history.",
    CSRF_REJECTED: "Refresh the page to renew its security token.",
    INVALID_OBSERVATION: "Enter a valid observation for this item.",
    OBLIGATION_ALREADY_SATISFIED: "This obligation is already satisfied. Refresh history.",
    OBLIGATION_ALREADY_DETERMINED: "This obligation already has a determination. Refresh history.",
    NOT_CURRENT_DETERMINATION: "The determination changed. Refresh history.",
    OPERATION_REUSED: "This operation has different content. Refresh before making another decision.",
    ALREADY_LINKED: "These reports are already linked.", NOT_LINKED: "These reports are not linked.",
    DEFICIENCY_NOT_ACTIVE: "That deficiency is closed or Reported in Error. Choose another report.",
    DEFICIENCY_CHANGED: "This deficiency changed. Refresh history before making a new decision.",
    INVALID_EXTENSION_DATE: "Choose a future date later than the governing deadline.",
    NO_DEADLINE: "A supervisor must set a deadline before an extension can be requested.",
    INVALID_FOLLOW_UP: "Choose a future follow-up date.",
    DEPENDENCY_UNCHANGED: "Change the dependency explanation or follow-up date.",
    NO_ACTIVE_DEPENDENCY: "This dependency has ended. Refresh history.",
    DEPENDENCY_ALREADY_RECORDED: "A dependency is already recorded. Refresh to update it.",
    MALFORMED_REQUEST: "Check the form values and try again.",
    INVALID_TRANSITION: "This deficiency is not in a status that allows this action.",
    NOT_YET_SUPPORTED: "This action is not available yet. Record a fresh independent check when appropriate."
  };
  function field(form, name) { return form.querySelector('[name="' + name + '"]'); }
  function value(form, name) { var input = field(form, name); return input ? input.value.trim() : ""; }
  function required(text, message) { if (!text) { throw new Error(message); } return text; }
  function utcInstant(text) {
    required(text, "Choose a date and time in UTC.");
    var date = new Date(text + "Z");
    if (isNaN(date.getTime())) { throw new Error("Enter a valid date and time in UTC."); }
    return date.toISOString();
  }
  function build(form) {
    var body = {operation_id: uuid4()}, kind = form.dataset.kind;
    if (["repair", "extension", "note", "dependency", "dependency-exit"].indexOf(kind) !== -1) {
      body.expected_event_id = form.dataset.expectedEvent;
    }
    if (kind === "extension") {
      body.reason = required(value(form, "reason"), "A reason is required.");
      body.proposed_deadline = utcInstant(value(form, "proposed_deadline"));
    }
    if (kind === "note") { body.note = required(value(form, "note"), "A note is required."); }
    if (kind === "dependency") {
      body.explanation = required(value(form, "explanation"), "An explanation is required.");
      if (value(form, "follow_up_at")) { body.follow_up_at = utcInstant(value(form, "follow_up_at")); }
    }
    if (kind === "dependency-exit" && value(form, "return_to")) { body.return_to = value(form, "return_to"); }
    if (kind === "link") { body.related_deficiency_id = required(value(form, "related_deficiency_id"), "Choose a report."); }
    if (kind === "correction") {
      body.expected_tip_check_id = form.dataset.tip;
      body.outcome = value(form, "outcome");
      if (field(form, "quantity")) {
        var count = value(form, "quantity");
        if (!/^[0-9]{1,6}$/.test(count)) { throw new Error("Enter the corrected count."); }
        body.quantity = parseInt(count, 10);
      }
      if (field(form, "measurement")) {
        var measured = value(form, "measurement");
        if (!/^-?[0-9]+(\.[0-9]+)?$/.test(measured)) { throw new Error("Enter the corrected measurement."); }
        body.measurement = {value: measured, unit: form.dataset.unit};
      }
      if (value(form, "explanation")) { body.explanation = value(form, "explanation"); }
    }
    if (kind === "completion" || kind === "explanation") {
      body.explanation = required(value(form, "explanation"), "An explanation is required.");
      if (kind === "completion") { body.check_id = form.dataset.check; }
    }
    if (kind === "report") {
      body.vehicle_id = config.vehicle_id;
      var target = required(value(form, "target"), "Choose the affected scope.");
      var parts = target.split(":");
      if (parts[0] === "systems") {
        var systems = Array.prototype.map.call(form.querySelectorAll('[name="system_ids"]:checked'), function (e) { return e.value; });
        if (!systems.length) { throw new Error("Choose at least one system."); }
        body.target = {vehicle: true, system_ids: systems};
      } else {
        var targetKey = {item: "item_id", kit: "kit_id", compartment: "compartment_id"}[parts[0]];
        if (!targetKey || !parts[1]) { throw new Error("Choose a valid affected scope."); }
        body.target = {}; body.target[targetKey] = parts[1];
      }
      body.category_id = required(value(form, "category_id"), "Choose a category.");
      var severity = form.querySelector('[name="severity"]:checked');
      body.severity = required(severity && severity.value, "Choose a severity using its criteria.");
      body.summary = required(value(form, "summary"), "Enter a summary.");
      body.description = required(value(form, "description"), "Describe what is wrong.");
      body.related_deficiency_ids = Array.prototype.map.call(field(form, "related_deficiency_ids").selectedOptions, function (e) { return e.value; });
    }
    return body;
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
  function matches(form) {
    var target = value(form, "target"), selected = field(form, "related_deficiency_ids");
    var note = document.getElementById("match-note"), generation = (form.matchGeneration || 0) + 1;
    form.matchGeneration = generation; selected.textContent = ""; note.textContent = "Loading possible matches…";
    var url = config.suggestions_url + "?vehicle_id=" + encodeURIComponent(config.vehicle_id);
    if (target.indexOf("item:") === 0) { url += "&item_id=" + encodeURIComponent(target.slice(5)); }
    fetch(url, {credentials: "same-origin", cache: "no-store"}).then(function (r) {
      if (!r.ok) { throw new Error("matches"); } return r.json();
    }).then(function (body) {
      if (generation !== form.matchGeneration) { return; }
      if (body.status !== "OK") { throw new Error("matches"); }
      body.suggestions.forEach(function (match) {
        var option = document.createElement("option"); option.value = match.deficiency_id;
        option.textContent = match.summary + " (" + match.status + ")"; selected.appendChild(option);
      });
      note.textContent = body.suggestions.length ? "Choose any related reports yourself; none is selected automatically." : "No possible matches were found.";
    }).catch(function () {
      if (generation === form.matchGeneration) { note.textContent = "Possible matches could not be loaded. Refresh before choosing related reports."; }
    });
  }
  Array.prototype.forEach.call(document.querySelectorAll(".workflow-write"), function (form) {
    var request = null, busy = false, finished = false;
    var message = form.querySelector(".workflow-result"), button = form.querySelector('button[type="submit"]');
    var original = button.textContent, csrf = field(form, "csrfmiddlewaretoken").value;
    if (form.dataset.kind === "report") {
      field(form, "target").addEventListener("change", function () { matches(form); }); matches(form);
    }
    form.addEventListener("submit", function (event) {
      event.preventDefault(); if (busy || finished) { return; }
      if (!request) {
        try { request = build(form); } catch (error) { message.textContent = error.message; return; }
      }
      busy = true; controls(form, true, true); button.textContent = "Saving…"; message.textContent = "Pending — waiting for acknowledgement.";
      post(form.dataset.url, request, csrf).then(function (answer) {
        busy = false;
        if (answer.kind === "saved") {
          finished = true; message.textContent = "Saved. View or refresh history for the current state.";
          button.textContent = "Saved"; form.querySelector(".workflow-refresh").hidden = false;
        } else if (answer.kind === "failed") {
          request = null; controls(form, false, false); button.textContent = original;
          message.textContent = "Not saved. " + (errors[answer.code] || ("Not accepted (" + answer.code + ")."));
        } else {
          controls(form, false, true); button.textContent = "Retry same request";
          message.textContent = "Unconfirmed: this may have been recorded. Retry sends the identical request and operation id; resolve it before editing.";
        }
      });
    });
  });
})();
