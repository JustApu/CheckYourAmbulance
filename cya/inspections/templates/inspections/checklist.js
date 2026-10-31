(function () {
  "use strict";
  // Crew checklist screen (issue 85 (https://github.com/JustApu/CheckYourAmbulance-archive/issues/85)). Every value from the server is written with textContent,
  // never as markup. The server decides everything: this script only sends intent and shows the
  // acknowledged result. Only a COMMITTED answer is ever shown as Saved (SPEC REC-2).
  var config = JSON.parse(document.getElementById("cya-config").textContent);
  var csrf = document.querySelector("[name=csrfmiddlewaretoken]").value;
  var $ = function (id) { return document.getElementById(id); };
  var list = $("items"), liveBanner = $("live-banner"), notice = $("screen-notice");
  var entries = {};       // item key -> {status, payload, text, operationId}
  var verificationRows = {}, verificationNotice = "";
  var drafts = {};        // item key -> typed value, kept across re-renders
  var rows = [];
  var view = config.view, onlyAttention = config.needs_attention_only;
  var loading = false, reloadAgain = false, reloadTimer = null;

  function uuid4() {
    var b = new Uint8Array(16);
    crypto.getRandomValues(b);
    b[6] = (b[6] & 15) | 64; b[8] = (b[8] & 63) | 128;
    var h = Array.prototype.map.call(b, function (x) { return ("0" + x.toString(16)).slice(-2); }).join("");
    return [h.slice(0, 8), h.slice(8, 12), h.slice(12, 16), h.slice(16, 20), h.slice(20)].join("-");
  }
  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) { e.className = cls; }
    if (text !== undefined) { e.textContent = text; }
    return e;
  }
  function when(iso) {
    var d = new Date(iso);
    return isNaN(d) ? "" : d.toLocaleString();
  }
  function key(row) { return row.item_id + "|" + (row.kit_id || ""); }

  var MESSAGES = {
    DEFICIENCY_REQUIRED: "Choose or report a deficiency for this result.",
    UNKNOWN_CATEGORY: "Report choices changed. Refresh and choose a current category.",
    INVALID_SEVERITY: "Report choices changed. Refresh and choose a current severity.",
    DEFICIENCY_NOT_ACTIVE: "That report is closed or Reported in Error. Choose another report.",
    VERIFICATION_NOT_PENDING: "This deficiency is no longer Pending Verification. Refresh its history.",
    NOT_IN_VERIFICATION_SCOPE: "This item and kit are no longer in the verification scope. Refresh history.",
    INVALID_OBSERVATION: "That value is not accepted for this item.",
    INVALID_OUTCOME: "That result is not allowed for this item.",
    FORBIDDEN: "You are not permitted to record this.",
    AUTHENTICATION_REQUIRED: "Your sign-in has ended. Sign in again.",
    NOT_FOUND: "This item or session is no longer available.",
    SESSION_MISMATCH: "This session does not match the checklist. Start a new session.",
    CHECKLIST_VERSION_NOT_CURRENT: "The checklist changed. Reload the screen.",
    SESSION_NOT_OPEN: "This session is not open. Reopen it to continue.",
    OPERATION_REUSED: "This request changed after it was sent. Tap the result again.",
    OPERATION_OWNED_BY_ANOTHER_ACTOR: "This request belongs to another user. Tap the result again.",
    ANOMALY_CONFIRMATION_REQUIRED: "That reading looks unusual.",
    AUDIT_UNAVAILABLE: "The service cannot record this right now.",
    CONFIGURATION_UNAVAILABLE: "The service is not configured to record this. Tell an administrator.",
    RETRY_LATER: "The service is busy."
  };
  function say(code) { return MESSAGES[code] || ("Not accepted (" + code + ")."); }

  // ---- sending ---------------------------------------------------------------------------
  // Outcome of one POST: {kind: "committed"|"conflict"|"rejected"|"unconfirmed", ...}.
  // Anything that is not a definite answer from the server is "unconfirmed": the write may or
  // may not have committed, so the same operation id is retried (REC-4).
  function post(url, payload) {
    return fetch(url, {
      method: "POST", credentials: "same-origin", cache: "no-store",
      headers: {"Content-Type": "application/json", "X-CSRFToken": csrf},
      body: JSON.stringify(payload)
    }).then(function (response) {
      return response.json().then(function (body) { return {response: response, body: body}; },
        function () { return {response: response, body: null}; });
    }).then(function (answer) {
      var r = answer.response, b = answer.body;
      if (b && b.status === "COMMITTED" && r.ok) { return {kind: "committed", body: b}; }
      if (b && b.status === "CONFLICT" && r.status === 409) { return {kind: "conflict", body: b}; }
      if (b && b.status === "REJECTED" && r.status < 500) { return {kind: "rejected", code: b.code, body: b}; }
      if (b && b.status === "REJECTED" && b.code !== "RETRY_LATER" && r.status !== 503) {
        return {kind: "rejected", code: b.code, body: b};
      }
      return {kind: "unconfirmed"};
    }, function () { return {kind: "unconfirmed"}; });
  }

  // ---- item rows -------------------------------------------------------------------------
  function checkPayload(row, entry) {
    var p = {
      operation_id: entry.operationId, vehicle_id: config.vehicle_id, item_id: row.item_id,
      checklist_version_id: row.checklist_version_id, purpose: "ROUTINE",
      basis_check_id: entry.basis
    };
    if (config.session_id) { p.session_id = config.session_id; }
    if (config.verification_for_deficiency_id) {
      p.purpose = "VERIFICATION";
      p.verification_for_deficiency_id = config.verification_for_deficiency_id;
    }
    if (row.kit_id) { p.kit_id = row.kit_id; }
    if (entry.acknowledges) { p.acknowledges_check_id = entry.acknowledges; }
    if (entry.outcome) { p.outcome = entry.outcome; }
    if (entry.outcome !== "UNABLE_TO_INSPECT") {
      if (row.kind === "QUANTITY") { p.quantity = entry.value; }
      else {
        p.outcome = entry.outcome || "PASSED";
        if (row.kind === "MEASUREMENT") { p.measurement = {value: entry.value, unit: row.unit}; }
      }
    }
    if (entry.deficiency) { p.deficiency = entry.deficiency; }
    if (entry.explanation) { p.explanation = entry.explanation; }
    return p;
  }
  function startCheck(row, value, outcome, deficiency, explanation) {
    var k = key(row);
    if (entries[k] && (entries[k].status === "pending" || entries[k].status === "unconfirmed")) { return; }
    var entry = {status: "pending", operationId: uuid4(), basis: row.latest_check_id, value: value,
                 retryable: true, outcome: outcome, deficiency: deficiency, explanation: explanation};
    entries[k] = entry;
    if (config.verification_for_deficiency_id) { verificationRows[k] = row; }
    send(row, entry);
  }
  function retryCheck(row) {
    var entry = entries[key(row)];
    if (!entry || entry.status === "pending") { return; }
    entry.status = "pending";   // same operation id and request: a retry, not a new decision
    send(row, entry);
  }
  function send(row, entry) {
    render();
    if (!entry.payload) { entry.payload = checkPayload(row, entry); }
    post(config.urls.checks, entry.payload).then(function (answer) {
      if (answer.kind === "committed") {
        entry.status = "saved"; entry.text = "Recorded " + when(answer.body.result.server_recorded_at);
        var verification = answer.body.result.verification;
        if (verification) {
          var detail = verification.status === "CLOSED_VERIFIED" ? "Closed — Verified." :
            verification.status === "IN_PROGRESS" ? "Verification failed — back in progress." :
            verification.closure_withheld ? "Scope satisfied; automatic closure withheld. Contact a supervisor." :
            verification.counts_toward_scope ? verification.remaining_scope_rows + " scope rows remain." :
            "Saved check does not count toward the verification scope.";
          entry.text += ". " + detail;
          verificationNotice = "Saved. " + detail;
          $("session-message").textContent = verificationNotice;
        }
        delete drafts[key(row)];
        scheduleReload(0);
      } else if (answer.kind === "conflict") {
        entry.status = "idle"; entry.text = "";
        askConflict(row, entry, answer.body.newest_check);
      } else if (answer.kind === "rejected") {
        entry.status = "failed"; entry.text = say(answer.code);
        if (answer.code === "CHECKLIST_VERSION_NOT_CURRENT" || answer.code === "SESSION_NOT_OPEN") {
          scheduleReload(0);
        }
      } else {
        entry.status = "unconfirmed";
        entry.text = "The server did not answer. This may or may not have been recorded. Retry sends the same request.";
      }
      render();
      announceHidden(row, entry);
    });
  }
  // A row that has left the list (Needs Attention Only, after a live reload) cannot show its own
  // indicator, so the outcome is said in the screen's status line instead.
  function announceHidden(row, entry) {
    var shown = rows.some(function (r) { return key(r) === key(row) && (config.verification_for_deficiency_id || r.checklist_version_id === config.checklist_version_id); });
    if (shown || entry.status === "pending" || entry.status === "idle") { return; }
    var words = {saved: "was saved", failed: "was not saved", unconfirmed: "is unconfirmed: it may or may not have been recorded"};
    $("session-message").textContent = "Your check of \"" + row.item_label + "\" " + words[entry.status] + "." +
      (entry.text ? " " + entry.text : "") +
      (entry.status === "saved" || config.verification_for_deficiency_id ? "" : " Turn off Needs Attention Only to see the item and retry.");
  }

  // ---- conflict dialog (CON-2, CON-3) ----------------------------------------------------
  var dialog = $("conflict-dialog"), conflictQueue = [], conflictShowing = false;
  function askConflict(row, entry, newest) {
    if (conflictShowing) { conflictQueue.push({row: row, entry: entry, newest: newest}); return; }
    conflictShowing = true;
    $("conflict-text").textContent = (newest.actor && newest.actor.display_name ? newest.actor.display_name : "Another crew member") +
      " recorded " + (newest.outcome || "a check").replace(/_/g, " ").toLowerCase() + " for \"" + row.item_label +
      "\" at " + when(newest.server_recorded_at) + ". Nothing has been saved from your tap.";
    dialog.dataset.key = key(row);
    // The row itself is kept: a live reload (for example the other person's Pass, with Needs
    // Attention Only on) can drop it from the list while this dialog is open (CON-2).
    dialog.row = row;
    dialog.newest = newest.check_id; dialog.entry = entry;
    if (typeof dialog.showModal === "function") { dialog.showModal(); } else { dialog.setAttribute("open", ""); }
  }
  function closeDialog() { conflictShowing = false; if (dialog.close) { dialog.close(); } dialog.removeAttribute("open"); }
  function nextConflict() {
    if (!conflictQueue.length) { return; }
    var next = conflictQueue.shift(); askConflict(next.row, next.entry, next.newest);
  }
  function cancelConflict() { delete entries[dialog.dataset.key]; closeDialog(); render(); nextConflict(); }
  $("conflict-cancel").addEventListener("click", cancelConflict);
  dialog.addEventListener("cancel", function (event) { event.preventDefault(); cancelConflict(); });
  $("conflict-proceed").addEventListener("click", function () {
    var k = dialog.dataset.key, row = dialog.row, previous = dialog.entry, warned = dialog.newest;
    closeDialog();
    // Proceed is a new decision, preserving the full Failed/Unable report and the warning basis.
    // Never substitute a newer background row: another check must receive its own warning.
    var operationId = uuid4();
    var entry = {status: "pending", operationId: operationId, retryable: true,
      payload: Object.assign({}, previous.payload, {operation_id: operationId,
        basis_check_id: warned, acknowledges_check_id: warned})};
    entries[k] = entry;
    send(row, entry); nextConflict();
  });

  // ---- Failed/Unable: the reporter explicitly chooses a match or a new report -------------
  var outcomeDialog = $("outcome-dialog"), outcomeForm = $("outcome-form");
  var outcomeChoice = $("outcome-existing"), outcomeGeneration = 0;
  function outcomeValue(name) { var input = outcomeForm.querySelector('[name="' + name + '"]'); return input ? input.value.trim() : ""; }
  function openOutcome(row, outcome, observation) {
    if (entries[key(row)] && (entries[key(row)].status === "pending" || entries[key(row)].status === "unconfirmed")) { return; }
    if (config.verification_for_deficiency_id) {
      if (outcome !== "FAILED") { return; }
      if (row.kind === "QUANTITY" || row.kind === "MEASUREMENT") {
        var observed = observation === undefined ? (drafts[key(row)] || "").trim() : String(observation);
        var validObservation = row.kind === "QUANTITY" ? /^[0-9]{1,6}$/.test(observed) : /^-?[0-9]+(\.[0-9]+)?$/.test(observed);
        if (!validObservation) { entries[key(row)] = {status: "failed", text: "Enter the observation before reporting Fail."}; render(); return; }
        observation = row.kind === "QUANTITY" ? parseInt(observed, 10) : observed;
      }
      startCheck(row, observation, "FAILED"); return;
    }
    if (outcome === "FAILED" && (row.kind === "QUANTITY" || row.kind === "MEASUREMENT") && observation === undefined) {
      var raw = (drafts[key(row)] || "").trim();
      var valid = row.kind === "QUANTITY" ? /^[0-9]{1,6}$/.test(raw) : /^-?[0-9]+(\.[0-9]+)?$/.test(raw);
      if (!valid) { entries[key(row)] = {status: "failed", text: "Enter the observation before reporting Fail."}; render(); return; }
      observation = row.kind === "QUANTITY" ? parseInt(raw, 10) : raw;
    }
    outcomeForm.reset(); outcomeDialog.row = row; outcomeDialog.outcome = outcome; outcomeDialog.observation = observation;
    $("outcome-title").textContent = (outcome === "FAILED" ? "Fail: " : "Unable to inspect: ") + row.item_label + (row.kit_label ? " — " + row.kit_label : "");
    outcomeForm.querySelector('[name="outcome_explanation"]').required = outcome === "UNABLE_TO_INSPECT";
    $("outcome-error").textContent = ""; $("outcome-create-fields").hidden = false;
    outcomeChoice.textContent = "";
    var create = el("option", "", "Report a new deficiency"); create.value = ""; outcomeChoice.appendChild(create);
    var generation = ++outcomeGeneration;
    $("outcome-matches").textContent = "Loading possible matches… None will be selected for you.";
    fetch(config.urls.suggestions + "?vehicle_id=" + encodeURIComponent(config.vehicle_id) + "&item_id=" + encodeURIComponent(row.item_id),
      {credentials: "same-origin", cache: "no-store"}).then(function (r) {
      if (!r.ok) { throw new Error("matches"); } return r.json();
    }).then(function (body) {
      if (generation !== outcomeGeneration) { return; }
      if (body.status !== "OK") { throw new Error("matches"); }
      body.suggestions.forEach(function (match) {
        var option = el("option", "", match.summary + " (" + match.status + ")");
        option.value = match.deficiency_id; outcomeChoice.appendChild(option);
      });
      $("outcome-matches").textContent = body.suggestions.length ? "Choose a matching existing report yourself, or describe a new one." : "No possible matches found. Describe a new deficiency.";
    }).catch(function () { if (generation === outcomeGeneration) { $("outcome-matches").textContent = "Possible matches could not be loaded. Refresh to choose an existing report, or explicitly report a new one."; } });
    if (typeof outcomeDialog.showModal === "function") { outcomeDialog.showModal(); } else { outcomeDialog.setAttribute("open", ""); }
  }
  function closeOutcome() { outcomeGeneration += 1; if (outcomeDialog.close) { outcomeDialog.close(); } outcomeDialog.removeAttribute("open"); }
  $("outcome-cancel").addEventListener("click", closeOutcome);
  outcomeChoice.addEventListener("change", function () { $("outcome-create-fields").hidden = !!outcomeChoice.value; });
  outcomeForm.addEventListener("submit", function (event) {
    event.preventDefault();
    var row = outcomeDialog.row, outcome = outcomeDialog.outcome, explanation = outcomeValue("outcome_explanation"), deficiency;
    if (outcome === "UNABLE_TO_INSPECT" && !explanation) { $("outcome-error").textContent = "A reason is required for Unable to inspect."; return; }
    if (outcomeChoice.value) { deficiency = {existing_id: outcomeChoice.value}; }
    else {
      var severity = outcomeForm.querySelector('[name="severity"]:checked');
      var fields = {category_id: outcomeValue("category_id"), severity: severity ? severity.value : "",
        summary: outcomeValue("summary"), description: outcomeValue("description")};
      if (!fields.category_id || !fields.severity || !fields.summary || !fields.description) { $("outcome-error").textContent = "Choose a category and severity, and describe the new deficiency."; return; }
      deficiency = {create: fields};
    }
    closeOutcome(); startCheck(row, outcomeDialog.observation, outcome, deficiency, explanation);
  });

  // ---- rendering -------------------------------------------------------------------------
  function stateText(row) {
    if (config.verification_for_deficiency_id) { return "Outstanding verification"; }
    if (row.obligation_state === "SATISFIED" && row.obligation_outcome === "FAILED") { return "Inspected — failed"; }
    if (row.obligation_state === "SATISFIED" && row.obligation_outcome === "SUPERVISOR_COMPLETED") { return "Completed by supervisor — unable to inspect"; }
    var map = {OVERDUE: "Overdue", DUE: "Due", CURRENT: "Due soon", SATISFIED: "Done for this window",
               UPCOMING: "Not due yet", SUSPENDED: "Check suspended", EXEMPT: "Exempt", WAIVED: "Waived"};
    return map[row.obligation_state] || "";
  }
  function saveIndicator(entry) {
    var p = el("p", "save-state save-state-" + entry.status);
    p.setAttribute("role", "status");
    var icons = {pending: "⌛", saved: "✓", failed: "✕", unconfirmed: "?"};
    var words = {pending: "Saving…", saved: "Saved", failed: "Not saved", unconfirmed: "Unconfirmed"};
    var icon = el("span", "indicator-icon", icons[entry.status]); icon.setAttribute("aria-hidden", "true");
    p.appendChild(icon);
    p.appendChild(el("span", "save-state-text", words[entry.status] + (entry.text && entry.status !== "pending" ? ". " + entry.text : "")));
    return p;
  }
  function controls(row, li) {
    var k = key(row), entry = entries[k], busy = entry && (entry.status === "pending" || entry.status === "unconfirmed");
    var box = el("div", "item-controls");
    if (config.state !== "OPEN") { return box; }
    if (row.kind === "VEHICLE_READING") {
      box.appendChild(el("p", "muted", config.verification_for_deficiency_id ? "A vehicle reading cannot verify this scope through an item check. Contact a supervisor; it remains outstanding." : "Recorded with the odometer form above."));
      return box;
    }
    var label = row.item_label + (row.kit_label ? " (" + row.kit_label + ")" : "");
    var button = el("button", "button primary", row.kind === "QUANTITY" ? "Record count" : "Pass");
    button.type = "button"; button.disabled = !!busy;
    button.setAttribute("aria-label", (row.kind === "QUANTITY" ? "Record count for " : "Pass ") + label);
    if (row.kind === "QUANTITY" || row.kind === "MEASUREMENT") {
      var input = el("input", "number-input");
      input.disabled = !!busy;
      input.type = "text"; input.inputMode = row.kind === "QUANTITY" ? "numeric" : "decimal";
      input.id = "in-" + li.id; input.value = drafts[k] || "";
      input.setAttribute("aria-label", (row.kind === "QUANTITY" ? "Counted quantity" : "Measured value" + (row.unit ? " in " + row.unit : "")) + " for " + label);
      input.addEventListener("input", function () { drafts[k] = input.value; });
      box.appendChild(input);
      if (row.kind === "QUANTITY") { box.appendChild(el("span", "muted", "Expected " + row.expected_quantity)); }
      if (row.unit && row.kind === "MEASUREMENT") { box.appendChild(el("span", "muted", row.unit)); }
      button.addEventListener("click", function () {
        var raw = (input.value || "").trim();
        var ok = row.kind === "QUANTITY" ? /^[0-9]{1,6}$/.test(raw) : /^-?[0-9]+(\.[0-9]+)?$/.test(raw);
        if (!ok) {
          entries[k] = {status: "failed", text: "Enter a number first.", operationId: uuid4()};
          render(); return;
        }
        var observed = row.kind === "QUANTITY" ? parseInt(raw, 10) : raw;
        if (row.kind === "QUANTITY" && observed !== row.expected_quantity) { openOutcome(row, "FAILED", observed); }
        else { startCheck(row, observed); }
      });
      if (row.kind === "MEASUREMENT") { button.textContent = "Record and pass"; }
    } else {
      button.addEventListener("click", function () { startCheck(row, null); });
    }
    if (row.allowed_outcomes.indexOf("PASSED") !== -1) { box.appendChild(button); }
    ["FAILED", "UNABLE_TO_INSPECT"].forEach(function (outcome) {
      if (row.allowed_outcomes.indexOf(outcome) === -1 || (config.verification_for_deficiency_id && outcome === "UNABLE_TO_INSPECT")) { return; }
      var action = el("button", "button", outcome === "FAILED" ? "Fail" : "Unable to inspect");
      action.type = "button"; action.disabled = !!busy;
      action.setAttribute("aria-label", action.textContent + " " + label);
      action.addEventListener("click", function () { openOutcome(row, outcome); });
      box.appendChild(action);
    });
    if (entry && entry.retryable && (entry.status === "failed" || entry.status === "unconfirmed")) {
      var retry = el("button", "button", "Retry");
      retry.type = "button";
      retry.setAttribute("aria-label", "Retry " + label);
      retry.addEventListener("click", function () { retryCheck(row); });
      box.appendChild(retry);
    }
    return box;
  }
  function itemNode(row, index) {
    var k = key(row), entry = entries[k];
    var li = el("li", "item" + (row.item_attention === "ATTENTION" ? " item-attention" : ""));
    li.id = "item-" + index;
    var head = el("div", "item-head");
    var title = el("h3", "", row.item_label + (row.kit_label ? " — " + row.kit_label : ""));
    head.appendChild(title);
    if (row.item_attention === "ATTENTION") { head.appendChild(el("span", "pill pill-attention", "⚠ Needs attention")); }
    var st = stateText(row);
    if (st) { head.appendChild(el("span", "pill", st)); }
    li.appendChild(head);
    var place = [row.compartment_label, row.system_label].filter(Boolean).join(" · ");
    if (place) { li.appendChild(el("p", "muted", place)); }
    if (row.instructions) { li.appendChild(el("p", "instructions", row.instructions)); }
    row.deficiencies.forEach(function (d) {
      var box = el("div", "known-deficiency");
      box.appendChild(el("strong", "", "Known deficiency (" + d.severity_label + ", " + d.status.replace(/_/g, " ").toLowerCase() + "): "));
      box.appendChild(document.createTextNode(d.summary));
      var historyLink = el("a", "", " View report and history");
      historyLink.href = config.urls.deficiency_detail.replace("DEFICIENCY", encodeURIComponent(d.id));
      box.appendChild(historyLink);
      li.appendChild(box);
    });
    if (row.latest_check_id) {
      li.appendChild(el("p", "last-check", "Last check: " + row.latest_check_outcome.replace(/_/g, " ").toLowerCase() +
        (row.latest_check_by ? " by " + row.latest_check_by : "") + " at " + when(row.latest_check_at)));
    }
    li.appendChild(controls(row, li));
    if (entry && entry.status !== "idle") { li.appendChild(saveIndicator(entry)); }
    return li;
  }
  function render() {
    var focused = document.activeElement, focusId = focused && focused.id && list.contains(focused) ? focused.id : null;
    var shown = rows.filter(function (r) { return (config.verification_for_deficiency_id || r.checklist_version_id === config.checklist_version_id); });
    while (list.firstChild) { list.removeChild(list.firstChild); }
    shown.forEach(function (row, i) { list.appendChild(itemNode(row, i)); });
    var message = "";
    if (rows.length && !shown.length) {
      message = "This vehicle's checklist changed after the session started. Start a new session to use the current one.";
    } else if (!rows.length) {
      message = config.verification_for_deficiency_id ? "No outstanding verification items available here. See the report for scope and status." : onlyAttention ? "Nothing needs attention right now." : "This vehicle has no checklist items.";
    } else if (!shown.length) {
      message = "";
    }
    notice.textContent = message; notice.hidden = !message;
    if (focusId && $(focusId)) { $(focusId).focus(); }
  }

  // ---- loading and live updates ----------------------------------------------------------
  function setStatus(s) {
    var badge = $("op-status");
    badge.textContent = s.status.replace(/_/g, " ").toLowerCase().replace(/^./, function (c) { return c.toUpperCase(); });
    badge.setAttribute("data-status", s.status);
    $("op-restrictions").textContent = s.restrictions || "";
  }
  function reload() {
    if (loading) { reloadAgain = true; return; }
    loading = true;
    var q = "?view=" + encodeURIComponent(view) + "&needs_attention_only=" + (onlyAttention ? "true" : "false");
    fetch(config.urls.checklist + q, {credentials: "same-origin", cache: "no-store"}).then(function (r) {
      return r.json().then(function (b) { return {ok: r.ok, body: b}; });
    }).then(function (a) {
      if (!a.ok || a.body.status !== "OK") { throw new Error("checklist"); }
      rows = a.body.items; setStatus(a.body.vehicle_status);
      if (config.verification_for_deficiency_id) {
        // An acknowledgement may be lost after the row was satisfied or removed. Keep its
        // exact request reachable until Retry resolves, even after automatic closure.
        Object.keys(verificationRows).forEach(function (k) {
          if (entries[k] && ["pending", "unconfirmed"].indexOf(entries[k].status) !== -1 &&
              !rows.some(function (r) { return key(r) === k; })) { rows.push(verificationRows[k]); }
        });
        $("session-message").textContent = verificationNotice + " " + a.body.verification_status + ". " + a.body.remaining_scope_rows + " outstanding scope rows." +
          (a.body.unavailable_scope_rows ? " Some items are unavailable on the current checklist. Contact a supervisor; they remain outstanding." : "");
      }
      $("stale").hidden = true; render();
    }).catch(function () {
      $("stale").hidden = false;   // data on screen may be out of date
    }).then(function () {
      loading = false;
      if (reloadAgain) { reloadAgain = false; scheduleReload(0); }
    });
  }
  function scheduleReload(ms) {
    clearTimeout(reloadTimer); reloadTimer = setTimeout(reload, ms);
  }

  var socket = null, backoff = 1000, stopped = false;
  function connect() {
    if (stopped) { return; }
    var scheme = location.protocol === "https:" ? "wss://" : "ws://";
    socket = new WebSocket(scheme + location.host + config.urls.socket);
    socket.onopen = function () { backoff = 1000; liveBanner.hidden = true; scheduleReload(0); };  // reconcile (17.3)
    socket.onmessage = function () { scheduleReload(250); };  // a push is only a hint
    socket.onclose = function (event) {
      liveBanner.hidden = false;
      if (event.code === 4401 || event.code === 4403) {
        // No further attempts will be made, so do not promise any.
        stopped = true;
        $("live-detail").textContent = "Your sign-in has ended or your access changed, so updates will not resume. " +
          "Sign in again and reload. An item marked Unconfirmed may have been recorded: use its Retry button, not a new tap.";
        return;
      }
      setTimeout(connect, backoff); backoff = Math.min(backoff * 2, 30000);
    };
  }

  // ---- view and filter (VW-1, VW-2) ------------------------------------------------------
  function syncControls() {
    if (config.verification_for_deficiency_id) { return; }
    $("view-normal").setAttribute("aria-pressed", String(view === "NORMAL"));
    $("view-priority").setAttribute("aria-pressed", String(view === "PRIORITY"));
    $("only-attention").checked = onlyAttention;
  }
  function savePreferences() {
    var s = $("pref-state");
    s.textContent = "Saving your view…";
    var payload = {operation_id: uuid4(), preferred_view: view, needs_attention_only: onlyAttention};
    post(config.urls.preferences, payload).then(function (a) {
      s.textContent = a.kind === "committed" ? "" : "Your view could not be remembered for next time.";
    });
  }
  function changeView(next) { view = next; syncControls(); savePreferences(); scheduleReload(0); }
  if (!config.verification_for_deficiency_id) {
    $("view-normal").addEventListener("click", function () { changeView("NORMAL"); });
    $("view-priority").addEventListener("click", function () { changeView("PRIORITY"); });
    $("only-attention").addEventListener("change", function () {
      onlyAttention = $("only-attention").checked; savePreferences(); scheduleReload(0);
    });
  }

  // ---- session actions -------------------------------------------------------------------
  var reopenButton = $("reopen-session");
  if (reopenButton) {
    reopenButton.addEventListener("click", function () {
      reopenButton.disabled = true;
      post(config.urls.reopen, {operation_id: uuid4()}).then(function (a) {
        if (a.kind === "committed") { location.reload(); return; }
        reopenButton.disabled = false;
        $("session-message").textContent = a.kind === "rejected" ? say(a.code) : "Unconfirmed. Check the session state and try again.";
      });
    });
  }

  // ---- odometer (ODO-1..ODO-3) -----------------------------------------------------------
  // An ended session has no odometer form (CON-2), so none of this is wired up for it.
  var odoForm = $("odometer-form");
  if (odoForm) {
    // The exact request sent last, kept until the server gives a definite answer so that a lost
    // response is retried unchanged (same operation id, same confirm_anomaly and explanation).
    var odo = {request: null, sending: false};
    // One attempt at a time: while a request is out its inputs and buttons are disabled, so a late
    // answer can never belong to a different request than the one held in odo.request.
    var odoControls = function (disabled) {
      ["odo-value", "odo-unit", "odo-hours", "odo-why", "odo-confirm-button"].forEach(function (id) {
        $(id).disabled = disabled;
      });
      odoForm.querySelector("button[type=submit]").disabled = disabled;
    };
    var odoStatus = function (state, text) {
      var s = $("odometer-state");
      s.className = "save-state save-state-" + state; s.hidden = false;
      var icons = {pending: "⌛", saved: "✓", failed: "✕", unconfirmed: "?"};
      s.querySelector(".indicator-icon").textContent = icons[state];
      s.querySelector(".save-state-text").textContent = text;
    };
    var submitOdometer = function (confirm) {
      if (odo.sending) { return; }
      if (!odo.request) {
        var payload = {
          operation_id: uuid4(), vehicle_id: config.vehicle_id,
          value: $("odo-value").value.trim(), unit: $("odo-unit").value, confirm_anomaly: confirm
        };
        var hours = $("odo-hours").value.trim();
        if (hours) { payload.engine_hours = hours; }
        if (confirm && $("odo-why").value.trim()) { payload.explanation = $("odo-why").value.trim(); }
        odo.request = payload;
      }
      odoStatus("pending", "Saving…");
      odo.sending = true; odoControls(true);
      post(config.urls.odometer, odo.request).then(function (a) {
        odo.sending = false; odoControls(false);
        if (a.kind === "committed") {
          odoStatus("saved", "Recorded " + when(a.body.result.server_recorded_at));
          $("odo-confirm").hidden = true; odo.request = null; scheduleReload(0);
        } else if (a.kind === "rejected" && a.code === "ANOMALY_CONFIRMATION_REQUIRED") {
          odo.request = null;   // the refusal wrote nothing; confirming is a new decision
          odoStatus("failed", "Not saved. " + say(a.code) + " Correct it, or confirm it is right.");
          $("odo-confirm").hidden = false;
        } else if (a.kind === "rejected") {
          odo.request = null; odoStatus("failed", "Not saved. " + say(a.code));
        } else {
          odoStatus("unconfirmed", "Unconfirmed. The server did not answer; this may or may not have been recorded. Submit again to retry the same request.");
        }
      });
    };
    // Either button resends an unanswered request as it was; otherwise it starts a new one.
    odoForm.addEventListener("submit", function (event) {
      event.preventDefault(); submitOdometer(false);
    });
    $("odo-confirm-button").addEventListener("click", function () { submitOdometer(true); });
    ["odo-value", "odo-unit", "odo-hours"].forEach(function (id) {
      $(id).addEventListener("input", function () { odo.request = null; });  // changed intent
    });
    if (config.odometer_unit) { $("odo-unit").value = config.odometer_unit; }
  }

  // ---- start ------------------------------------------------------------------------------
  syncControls();
  scheduleReload(0);
  connect();
  document.addEventListener("visibilitychange", function () { if (!document.hidden) { scheduleReload(0); } });
})();
