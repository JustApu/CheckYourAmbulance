(function () {
  "use strict";
  // The shell's Saved toast (issue #194); absent when the shell script is not on the page.
  function toast(text, options) { if (window.cyaShell && window.cyaShell.toast) { window.cyaShell.toast(text, options); } }
  var configNode = document.getElementById("workflow-config");
  var config = configNode ? JSON.parse(configNode.textContent) : {};
  // Issue #235 A2: after an in-page action, focus lands on the result, never on <body>.
  function focusMessage(message) {
    if (!message || typeof message.focus !== "function") { return; }
    message.setAttribute("tabindex", "-1"); message.focus();
  }
  // Issue #235 A1: the problem page reloads after a save, so its status, next action, forms and
  // expected event all come from the server. The page names what was saved (?saved=<kind>) and
  // focuses that notice; the marker is then removed so a later reload does not repeat it.
  (function () {
    var notice = document.querySelector("[data-focus-on-load]");
    if (!notice || typeof notice.focus !== "function") { return; }
    notice.focus();
    if (window.history && typeof window.history.replaceState === "function" && config.page_url) {
      window.history.replaceState(null, "", config.page_url);
    }
  })();
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
    DEFICIENCY_NOT_ACTIVE: "That problem is closed or Reported in Error. Choose another report.",
    DEFICIENCY_CHANGED: "This problem changed. Refresh history before making a new decision.",
    INVALID_EXTENSION_DATE: "Choose a future date later than the current deadline.",
    NO_DEADLINE: "A supervisor must set a deadline before an extension can be requested.",
    INVALID_FOLLOW_UP: "Choose a future follow-up date.",
    DEPENDENCY_UNCHANGED: "Change what work is waiting on or its follow-up date.",
    NO_ACTIVE_DEPENDENCY: "The waiting work has ended. Refresh history.",
    DEPENDENCY_ALREADY_RECORDED: "Waiting work is already recorded. Refresh to update it.",
    MALFORMED_REQUEST: "Check the form values and try again.",
    INVALID_TRANSITION: "This problem is not in a status that allows this action.",
    NOT_YET_SUPPORTED: "This action is not available yet. Record a fresh independent check when appropriate.",
    INVALID_ASSIGNEE: "That person cannot be assigned. Choose an active Crew, Supervisor or Supply Officer.",
    ASSIGNEE_UNCHANGED: "That person is already the assignee.",
    INVALID_DEADLINE: "Choose a future deadline.", DEADLINE_UNCHANGED: "That is already the deadline.",
    REQUEST_ALREADY_DECIDED: "This extension request was already decided. Refresh history.",
    REQUEST_SUPERSEDED: "A newer deadline replaced this request. Refresh history.",
    REQUEST_NOT_FOUND: "This extension request is no longer available. Refresh history.",
    ACKNOWLEDGEMENT_MAXIMUM_UNCONFIGURED: "An administrator must set the longest acknowledgement period first.",
    ACKNOWLEDGEMENT_PERIOD_TOO_LONG: "Choose a period no longer than the administrator's maximum.",
    ACKNOWLEDGEMENT_PERIOD_OUT_OF_RANGE: "Choose a period no longer than the administrator's maximum.",
    SEVERITY_UNCHANGED: "Choose a different severity.",
    ALREADY_IN_SCOPE: "An item you chose is already in the verification scope.",
    ALREADY_SUSPENDED: "An item you chose is already suspended for this problem.",
    ITEM_NOT_IN_CHECKLIST: "An item you chose is no longer on the vehicle's checklist. Refresh.",
    ITEM_NOT_IN_SCOPE: "An item you chose is not part of this problem. Refresh this page.",
    KIT_REQUIRED: "Choose the specific kit that contains this item. Refresh this page.", KIT_NOT_ALLOWED: "That item is not in a kit. Refresh.",
    TOO_MANY_SUSPENSIONS: "Too many suspensions at once. Choose fewer items.",
    SUSPENSION_ALREADY_ENDED: "This suspension has already ended. Refresh history.",
    SUSPENSION_NOT_FOUND: "This suspension is no longer available. Refresh history.",
    SCOPE_TOO_LARGE: "Too many items. Choose fewer.",
    VEHICLE_INACTIVE: "This vehicle is inactive.",
    EXPLANATION_REQUIRED: "An explanation is required.",
    OPERATION_OWNED_BY_ANOTHER_ACTOR: "This request belongs to another person. Refresh the page and try again.",
    PAPER_BATCH_INVALIDATED: "This paper entry was invalidated. Refresh history.",
    BATCH_INVALIDATED: "This paper entry was already invalidated. Refresh history."
  };
  // Codes whose meaning depends on the form (issue 20): a repair with an empty scope has nothing
  // left to verify, which the report form's wording does not fit.
  var kindErrors = {
    repair: {EMPTY_SCOPE: "There are no current checklist items left to verify for this problem, so a repair cannot be recorded. Ask a supervisor or administrator to close it."},
    // The repair form carries data-can-close-empty when this user may use the empty-scope close
    // (issue 21). The close is offered only once nothing is left to verify (issue #235 A3), so a
    // scope that emptied after the page loaded needs a refresh to show it.
    "repair-closable": {EMPTY_SCOPE: "There are no current checklist items left to verify for this problem, so a repair cannot be recorded. Refresh this page, then use \u201cClose when there are no items to check\u201d."},
    "empty-scope-close": {
      SCOPE_AVAILABLE: "Current verification items exist; record the repair instead.",
      INVALID_TRANSITION: "This problem's status changed. Refresh its history."
    },
    // SPEC 8.0.0 INV-8, LATE-6 (issue #9 B6b): a paper entry is invalidated as a unit, never by
    // the person who entered it.
    "paper-batch": {
      SELF_INVALIDATION_FORBIDDEN: "You entered this paper entry, so you cannot invalidate it. Ask another supervisor or administrator.",
      FORBIDDEN: "Only a supervisor or administrator other than the person who entered it can invalidate a paper entry.",
      NOT_FOUND: "This paper entry is no longer available. Refresh history."
    },
    // A check mistakenly saved in Normal mode, after the same work was copied in Paper entry.
    "mistaken-normal": {
      SELF_INVALIDATION_FORBIDDEN: "You recorded this check, so you cannot invalidate it. Ask another supervisor or administrator.",
      FORBIDDEN: "Only a supervisor or administrator other than the person who recorded this check can do this.",
      NOT_FOUND: "This check or the chosen paper entry is no longer available. Refresh history.",
      PAPER_CHECK_NOT_NORMAL: "This check was entered in Paper entry mode. If its time or people are wrong, invalidate its paper entry instead.",
      REPLACEMENT_NOT_APPLICABLE: "Only a routine check can be replaced by a paper entry.",
      REPLACEMENT_BATCH_MISMATCH: "The chosen paper entry is for another vehicle. Choose a paper entry of this vehicle.",
      REPLACEMENT_BATCH_INVALIDATED: "The chosen paper entry was invalidated. Choose another, or copy the sheet again through Paper entry.",
      REPLACEMENT_PERFORMED_LATER: "The chosen paper entry was performed after this check was saved. Choose the sheet this check was copied from.",
      REPLACEMENT_CHECK_MISSING: "The chosen paper entry has no saved paper check of this same item. Copy it through Paper entry first, or choose another.",
      PAPER_BATCH_INVALIDATED: "The chosen paper entry was invalidated. Choose another, or copy the sheet again through Paper entry."
    }
  };
  // Forms whose refusals go to their error summary (role=alert) and mark the named field
  // aria-invalid. The fields a refusal names: field_errors paths are the field names.
  var SUMMARY_KINDS = ["paper-batch", "mistaken-normal"];
  var CODE_FIELDS = {
    EXPLANATION_REQUIRED: "explanation",
    REPLACEMENT_BATCH_MISMATCH: "replacement_paper_entry_batch_id",
    REPLACEMENT_BATCH_INVALIDATED: "replacement_paper_entry_batch_id",
    REPLACEMENT_PERFORMED_LATER: "replacement_paper_entry_batch_id",
    REPLACEMENT_CHECK_MISSING: "replacement_paper_entry_batch_id"
  };
  // Paper-entry invalidations post to constant API paths, never to a URL read from the page: the
  // id fills exactly one percent-encoded segment and is never empty, "." or ".." (the issue 27
  // rule). test_workflow_scripts.py pins both paths to the server's routes.
  var PAPER_BATCH_PATH = "/api/v1/paper-entry-batches/";
  var CHECK_PATH = "/api/v1/checks/";
  function segment(id) {
    var encoded = typeof id === "string" ? encodeURIComponent(id) : "";
    if (encoded === "" || encoded === "." || encoded === "..") { throw new Error("This record's link is not valid. Refresh history."); }
    return encoded;
  }
  function target(form) {
    if (form.dataset.kind === "paper-batch") { return PAPER_BATCH_PATH + segment(form.dataset.batch) + "/invalidations"; }
    if (form.dataset.kind === "mistaken-normal") { return CHECK_PATH + segment(form.dataset.check) + "/invalidations"; }
    return form.dataset.url;
  }
  function fieldError(name, message) { var error = new Error(message); error.field = name; return error; }
  function errorText(form, code) {
    var kind = form.dataset.kind;
    if (kind === "repair" && form.dataset.canCloseEmpty === "1") { kind = "repair-closable"; }
    var own = kindErrors[kind];
    return (own && own[code]) || errors[code] || ("Not accepted (" + code + ").");
  }
  function field(form, name) { return form.querySelector('[name="' + name + '"]'); }
  function value(form, name) { var input = field(form, name); return input ? input.value.trim() : ""; }
  function required(text, message) { if (!text) { throw new Error(message); } return text; }
  function utcInstant(text, form) {
    required(text, "Choose an agency-local date and time.");
    return window.CyaTimezones.toUtc(text, form.dataset.timeZone);
  }
  function build(form) {
    var body = {operation_id: uuid4()}, kind = form.dataset.kind;
    if (["repair", "extension", "note", "dependency", "dependency-exit", "assign", "deadline", "start-work",
      "extension-decision", "acknowledge", "regrade", "expand", "add-verification", "suspend",
      "end-suspension", "admin-close", "admin-reopen", "empty-scope-close"].indexOf(kind) !== -1) {
      body.expected_event_id = form.dataset.expectedEvent;
    }
    if (kind === "extension") {
      body.reason = required(value(form, "reason"), "A reason is required.");
      body.proposed_deadline = utcInstant(value(form, "proposed_deadline"), form);
    }
    if (kind === "note") { body.note = required(value(form, "note"), "A note is required."); }
    if (kind === "dependency") {
      body.explanation = required(value(form, "explanation"), "An explanation is required.");
      if (value(form, "follow_up_at")) { body.follow_up_at = utcInstant(value(form, "follow_up_at"), form); }
    }
    if (kind === "dependency-exit" && value(form, "return_to")) { body.return_to = value(form, "return_to"); }
    if (kind === "assign") { body.assignee_id = required(value(form, "assignee_id"), "Choose a person."); }
    if (kind === "deadline") { body.deadline = utcInstant(value(form, "deadline"), form); }
    if (kind === "extension-decision") {
      body.request_event_id = form.querySelector('[name="request_event_id"]').value;
      body.decision = required(value(form, "decision"), "Choose a decision.");
      if (body.decision === "DIFFERENT_DATE") { body.deadline = utcInstant(value(form, "deadline"), form); }
      else if (value(form, "deadline")) { throw new Error("Leave the different date empty unless you approve a different date."); }
    }
    if (kind === "acknowledge") {
      body.action_plan = required(value(form, "action_plan"), "An action plan is required.");
      var days = value(form, "period_days");
      if (!/^[1-9][0-9]{0,4}$/.test(days)) { throw new Error("Enter the period as a whole number of days."); }
      body.period_days = parseInt(days, 10);
    }
    if (kind === "regrade") {
      var level = form.querySelector('[name="severity"]:checked');
      body.severity = required(level && level.value, "Choose a severity using its criteria.");
    }
    if (["expand", "add-verification", "suspend"].indexOf(kind) !== -1) {
      body.items = Array.prototype.map.call(form.querySelectorAll('[name="items"]:checked'), function (e) {
        var parts = e.value.split(":"), entry = {item_id: parts[0]};
        if (parts[1]) { entry.kit_instance_id = parts[1]; }
        if (parts[2]) { entry.item_requirement_id = parts[2]; }
        return entry;
      });
      if (!body.items.length) { throw new Error("Choose at least one item."); }
      if (kind === "add-verification") { body.deadline = utcInstant(value(form, "deadline"), form); }
      if (kind === "suspend" && value(form, "explanation")) { body.explanation = value(form, "explanation"); }
    }
    if (["extension-decision", "acknowledge", "regrade", "expand", "add-verification", "admin-close", "admin-reopen", "empty-scope-close"].indexOf(kind) !== -1) {
      body.explanation = required(value(form, "explanation"), "An explanation is required.");
    }
    if (kind === "end-suspension") {
      body.suspension_id = form.dataset.suspension;
      if (value(form, "explanation")) { body.explanation = value(form, "explanation"); }
    }
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
    if (kind === "mistaken-normal") {
      body.replacement_paper_entry_batch_id = value(form, "replacement_paper_entry_batch_id");
      if (!body.replacement_paper_entry_batch_id) { throw fieldError("replacement_paper_entry_batch_id", "Choose the paper entry that replaces this check."); }
    }
    if (kind === "paper-batch" || kind === "mistaken-normal") {
      body.explanation = value(form, "explanation");
      if (!body.explanation) { throw fieldError("explanation", "An explanation is required."); }
    }
    if (kind === "completion" || kind === "explanation") {
      body.explanation = required(value(form, "explanation"), "An explanation is required.");
      if (kind === "completion") { body.check_id = form.dataset.check; }
    }
    if (kind === "report") {
      body.vehicle_id = config.vehicle_id;
      var target = required(value(form, "target"), "Choose an item, kit, compartment, or vehicle system.");
      var parts = target.split(":");
      if (parts[0] === "systems") {
        var systems = Array.prototype.map.call(form.querySelectorAll('[name="system_ids"]:checked'), function (e) { return e.value; });
        if (!systems.length) { throw new Error("Choose at least one system."); }
        body.target = {vehicle: true, system_ids: systems};
      } else {
        var targetKey = {item: "item_id", kit: "kit_id", compartment: "compartment_id"}[parts[0]];
        if (!targetKey || !parts[1]) { throw new Error("Choose a valid item, kit, compartment, or vehicle system."); }
        body.target = {}; body.target[targetKey] = parts[1];
        if (parts[0] === "item" && parts[2]) { body.target.item_requirement_id = parts[2]; }
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
        if (response.status < 500 && data.status === "REJECTED") {
          var paths = Array.isArray(data.field_errors) ? data.field_errors.map(function (e) { return e && e.path; }) : [];
          return {kind: "failed", code: data.code, paths: paths};
        }
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
    // REC-4: request and url are kept whole, with the operation id, until a definite answer.
    var request = null, url = null, busy = false, finished = false;
    var message = form.querySelector(".workflow-result"), button = form.querySelector('button[type="submit"]');
    var original = button.textContent, csrf = field(form, "csrfmiddlewaretoken").value;
    var summary = SUMMARY_KINDS.indexOf(form.dataset.kind) !== -1 ? form.querySelector(".workflow-errors") : null;
    function clearErrors() {
      if (!summary) { return; }
      summary.textContent = ""; summary.hidden = true;
      ["explanation", "replacement_paper_entry_batch_id"].forEach(function (name) {
        var input = field(form, name); if (input) { input.removeAttribute("aria-invalid"); }
      });
    }
    // The error summary takes focus; each named field is marked aria-invalid.
    function showErrors(text, names) {
      summary.textContent = text; summary.hidden = false;
      names.forEach(function (name) {
        var input = name ? field(form, name) : null; if (input) { input.setAttribute("aria-invalid", "true"); }
      });
      message.textContent = ""; focusMessage(summary);
    }
    if (form.dataset.kind === "report") {
      field(form, "target").addEventListener("change", function () { matches(form); }); matches(form);
    }
    form.addEventListener("submit", function (event) {
      event.preventDefault(); if (busy || finished) { return; }
      if (!request) {
        clearErrors();
        try { url = target(form); request = build(form); } catch (error) {
          request = null; url = null;
          if (summary) { showErrors(error.message, [error.field]); } else { message.textContent = error.message; }
          return;
        }
      }
      busy = true; controls(form, true, true); button.textContent = "Saving…"; message.textContent = "Pending — waiting for acknowledgement.";
      post(url, request, csrf).then(function (answer) {
        busy = false;
        if (answer.kind === "saved") {
          finished = true; button.textContent = "Saved";
          if (config.reload_after_save && config.page_url) {
            message.textContent = "Saved. Updating the page…";
            window.location.assign(config.page_url + "?saved=" + encodeURIComponent(form.dataset.kind));
            return;
          }
          message.textContent = "Saved. View or refresh history for the current state.";
          toast("Saved.", {announce: false});
          form.querySelector(".workflow-refresh").hidden = false;
          focusMessage(message);
        } else if (answer.kind === "failed") {
          request = null; url = null; controls(form, false, false); button.textContent = original;
          if (summary) {
            var names = (answer.paths || []).concat([CODE_FIELDS[answer.code]]);
            showErrors("Not saved. " + errorText(form, answer.code), names);
            return;
          }
          message.textContent = "Not saved. " + errorText(form, answer.code);
          focusMessage(message);
        } else {
          controls(form, false, true); button.textContent = "Retry same request";
          message.textContent = "Unconfirmed: this may have been recorded. Retry sends the identical request and operation id; resolve it before editing.";
          focusMessage(message);
        }
      });
    });
  });
})();
