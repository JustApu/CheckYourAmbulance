(function () {
  "use strict";
  // Crew checklist screen (issue 85 (https://github.com/JustApu/CheckYourAmbulance-archive/issues/85)). Every value from the server is written with textContent,
  // never as markup. The server decides everything: this script only sends intent and shows the
  // acknowledged result. Only a COMMITTED answer is ever shown as Saved (SPEC REC-2).
  var config = JSON.parse(document.getElementById("cya-config").textContent);
  var csrf = document.querySelector("[name=csrfmiddlewaretoken]").value;
  var $ = function (id) { return document.getElementById(id); };
  var list = $("items"), liveBanner = $("live-banner"), notice = $("screen-notice");
  // Paper entry (SPEC 8.0.0 REC-5, LATE-4 to LATE-8): the rows of the version printed on one
  // batch's sheet. Each tap saves one check naming the batch, never a session; the view is the
  // sheet's own order, so no view preference is read or saved and the progress bar is not shown.
  var paper = config.paper_entry || null, paperOffered = !!(paper && paper.has_records);
  var deferredListRender = false;
  list.tabIndex = -1;
  var entries = {};       // item key -> {status, payload, text, operationId}
  var verificationRows = {}, verificationNotice = "";
  var drafts = {};        // item key -> typed value, kept across re-renders
  var rows = [], groupProgressRows = [], focusIntent = null;
  var view = config.view, onlyAttention = config.needs_attention_only;
  var pendingNavigation = readNavigation();
  var layoutMode = pendingNavigation ? pendingNavigation.mode : "COMPARTMENTS";
  var selectedGroupKey = pendingNavigation && pendingNavigation.mode === "COMPARTMENTS" ? pendingNavigation.groupKey : null;
  var navigationRestored = false, navigationAnchorKey = null, navigationSaveTimer = null;
  var focusOnRestore = false;  // issue #235 B2: a resumed position also restores focus
  var loading = false, reloadAgain = false, reloadTimer = null, signedOutNow = false, loadedOnce = false;
  // Live hints (issue #278 A): our committed check IDs, unanswered saves, and an unread hint.
  var ownEvents = {}, ownOrder = [], savesInFlight = 0, hintWaiting = false;
  // A push for our own save may never come (the socket was down, or it came before the answer
  // and was already covered), so only the newest OWN_EVENT_LIMIT ids are remembered. Forgetting
  // one is safe: its push, if it does come, just costs one more read.
  var OWN_EVENT_LIMIT = 50;
  function rememberOwn(id) {
    ownEvents[id] = true; ownOrder.push(id);
    while (ownOrder.length > OWN_EVENT_LIMIT) { delete ownEvents[ownOrder.shift()]; }
  }
  // What the next read must cover (issue #278 B): the whole list, or only these items' rows.
  var needFull = true, pendingItems = {}, MAX_ITEM_READ = 20;
  // Split view (issue #194): at 64rem and wider an open area keeps the area list beside its items.
  var wideQuery = window.matchMedia ? window.matchMedia("(min-width: 64rem)") : null;
  // Loading placeholders (issue #194) stand in until the first answer; never leave them spinning.
  // One block outside both layouts, so Compartments, All items and verification all show it.
  function clearLoading(message) {
    if ($("checklist-skeleton")) { $("checklist-skeleton").hidden = true; }
    if ($("checklist-loading")) { $("checklist-loading").textContent = message || ""; }
  }

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
  var timezoneDisplayWarningLogged = false;
  function when(iso) {
    try {
      return window.CyaTimezones.formatUtc(iso, config.time_zone);
    } catch (_error) {
      if (!timezoneDisplayWarningLogged && window.console && typeof window.console.error === "function") {
        timezoneDisplayWarningLogged = true;
        window.console.error("Agency time zone display is unavailable; showing this timestamp in UTC.");
      }
      var instant = typeof iso === "string" && iso ? new Date(iso) : null;
      return !instant || isNaN(instant.getTime()) ? String(iso || "") :
        instant.toISOString().replace(/\.000Z$/, " UTC").replace(/Z$/, " UTC").replace("T", " ");
    }
  }
  // A kit presence row (KDEF-12) has no kit; its kit list line tells two lines of one kit apart.
  // A loose item's item requirement (SPEC 6.0.0 CHK-11) tells two places of one item apart.
  function key(row) {
    return row.item_id + "|" + (row.kit_id || "") + (row.kit_requirement_id ? "|" + row.kit_requirement_id : "") +
      (row.item_requirement_id ? "|r" + row.item_requirement_id : "");
  }
  function rememberFocus(row, action) { focusIntent = {key: key(row), action: action}; }
  var focusRestoreScheduled = false;
  function focusStatusMessage(message) {
    var status = $("session-message");
    if (!status) { return; }
    if (message) { status.textContent = message; }
    status.tabIndex = -1;
    if (document.activeElement !== status) { status.focus({preventScroll: true}); }
  }
  var inertPageChildren = [];
  function focusableIn(dialog) {
    var candidates = Array.prototype.slice.call(dialog.querySelectorAll("a[href], button, input, select, textarea, [tabindex]"));
    return candidates.filter(function (node) {
      if (node.disabled || node.hidden || node.getAttribute("aria-hidden") === "true") { return false; }
      if (node.tabIndex < 0 || node.getAttribute("tabindex") === "-1") { return false; }
      for (var parent = node; parent && parent !== dialog; parent = parent.parentNode) {
        if (parent.hidden || parent.inert) { return false; }
      }
      return true;
    });
  }
  function bindFocusDialog(dialog, cancel) {
    dialog.setAttribute("role", "dialog");
    dialog.setAttribute("tabindex", "-1");
    dialog.addEventListener("keydown", function (event) {
      if (event.key === "Escape") {
        event.preventDefault();
        if (event.stopPropagation) { event.stopPropagation(); }
        cancel();
        return;
      }
      if (event.key !== "Tab") { return; }
      var controls = focusableIn(dialog), current = document.activeElement;
      if (!controls.length) {
        event.preventDefault();
        dialog.focus({preventScroll: true});
        return;
      }
      var index = controls.indexOf(current);
      if (event.shiftKey && index <= 0) {
        event.preventDefault();
        controls[controls.length - 1].focus({preventScroll: true});
      } else if (!event.shiftKey && (index < 0 || index === controls.length - 1)) {
        event.preventDefault();
        controls[0].focus({preventScroll: true});
      }
    });
    dialog.addEventListener("cancel", function (event) { event.preventDefault(); cancel(); });
  }
  function openFocusDialog(dialog) {
    dialog.closeScheduled = false;
    dialog.hidden = false;
    dialog.setAttribute("aria-modal", "true");
    if (typeof dialog.show === "function") { dialog.show(); }
    else { dialog.setAttribute("open", ""); }
    var controls = focusableIn(dialog);
    (controls[0] || dialog).focus({preventScroll: true});
    inertPageChildren = [];
    Array.prototype.forEach.call(document.body.children, function (child) {
      if (child === dialog || child.tagName === "SCRIPT") { return; }
      inertPageChildren.push({node: child, wasInert: child.inert});
      child.inert = true;
    });
  }
  function closeFocusDialog(dialog, target, afterClose) {
    if (dialog.closeScheduled) { return; }
    dialog.closeScheduled = true;
    var canRestoreTarget = target && document.contains(target), restoreStates = inertPageChildren.slice(), focusRoot = null;
    inertPageChildren = [];
    function setTemporaryInert(node, value) {
      var saved = restoreStates.some(function (entry) { return entry.node === node; });
      if (!saved) { restoreStates.push({node: node, wasInert: node.inert}); }
      node.inert = value;
    }
    if (canRestoreTarget) {
      Array.prototype.some.call(document.body.children, function (child) {
        if (child !== dialog && child.contains(target)) { focusRoot = child; child.inert = false; return true; }
        return false;
      });
      if (focusRoot) {
        var path = [], node = target;
        while (node && node !== focusRoot) { path.unshift(node); node = node.parentNode; }
        var parent = focusRoot;
        path.forEach(function (pathNode) {
          Array.prototype.forEach.call(parent.children, function (sibling) {
            if (sibling !== pathNode) { setTemporaryInert(sibling, true); }
          });
          pathNode.inert = false;
          parent = pathNode;
        });
      }
    }
    if (!focusRoot) {
      restoreStates.forEach(function (entry) { entry.node.inert = entry.wasInert; });
      restoreStates = [];
    }
    dialog.removeAttribute("aria-modal");
    if (canRestoreTarget) { target.focus({preventScroll: true}); }
    function finishClose() {
      if (typeof dialog.close === "function") { dialog.close(); }
      else { dialog.removeAttribute("open"); }
      dialog.hidden = true;
      function restoreBackground() {
        restoreStates.forEach(function (entry) { entry.node.inert = entry.wasInert; });
        dialog.closeScheduled = false;
        if (afterClose) { afterClose(); }
      }
      if (restoreStates.length && typeof window.requestAnimationFrame === "function") {
        window.requestAnimationFrame(restoreBackground);
      } else if (restoreStates.length) { setTimeout(restoreBackground, 0); }
      else { restoreBackground(); }
    }
    if (typeof window.requestAnimationFrame === "function") { window.requestAnimationFrame(finishClose); }
    else { setTimeout(finishClose, 0); }
  }
  function restoreFocusIntent(afterRender) {
    if (!focusIntent) { return; }
    var item = itemForKey(focusIntent.key), target = item && actionIn(item, focusIntent.action);
    if (!target) { focusStatusMessage("This item is no longer on the checklist. Refresh and try again."); focusIntent = null; return; }
    // Busy actions use aria-disabled so focus remains on the named action during a save.
    // Native-disabled controls cannot receive focus and use the visible status message instead.
    if (target.disabled) { if (!afterRender) { focusStatusMessage("This checklist action is no longer available."); focusIntent = null; } return; }
    if (!afterRender) { if (document.activeElement !== target) { target.focus({preventScroll: true}); } focusIntent = null; return; }
    if (focusRestoreScheduled) { return; }
    focusRestoreScheduled = true;
    var defer = typeof window.requestAnimationFrame === "function" ?
      function (callback) { window.requestAnimationFrame(callback); } :
      function (callback) { setTimeout(callback, 0); };
    defer(function () {
      focusRestoreScheduled = false;
      if (!focusIntent) { return; }
      var intent = focusIntent, active = document.activeElement;
      if (active && active !== document.body && active !== list) { focusIntent = null; return; }
      var item = itemForKey(intent.key), target = item && actionIn(item, intent.action);
      if (target && !target.disabled) {
        if (document.activeElement !== target) { target.focus({preventScroll: true}); }
        focusIntent = null;
      } else {
        focusStatusMessage(target ? "This checklist action is no longer available." : "This item is no longer on the checklist. Refresh and try again.");
        focusIntent = null;
      }
    });
  }
  function itemForKey(itemKey) {
    for (var i = 0; i < list.children.length; i++) {
      if (list.children[i].dataset.itemKey === itemKey) { return list.children[i]; }
    }
    return null;
  }
  function actionIn(item, action) {
    if (!item || !action) { return null; }
    var pending = [item];
    while (pending.length) {
      var node = pending.shift();
      if (node.dataset && node.dataset.action === action) { return node; }
      Array.prototype.push.apply(pending, node.children || []);
    }
    return null;
  }

  var MESSAGES = {
    DEFICIENCY_REQUIRED: "Choose or report a problem for this result.",
    UNKNOWN_CATEGORY: "Report choices changed. Refresh and choose a current category.",
    INVALID_SEVERITY: "Report choices changed. Refresh and choose a current severity.",
    DEFICIENCY_NOT_ACTIVE: "That report is closed or Reported in Error. Choose another report.",
    VERIFICATION_NOT_PENDING: "This problem is no longer Pending Verification. Refresh its history.",
    NOT_IN_VERIFICATION_SCOPE: "This item and kit are no longer in the verification scope. Refresh history.",
    INVALID_OBSERVATION: "That value is not accepted for this item.",
    INVALID_OUTCOME: "That result is not allowed for this item.",
    FORBIDDEN: "You are not permitted to record this.",
    AUTHENTICATION_REQUIRED: "Your sign-in has ended. Sign in again.",
    NOT_FOUND: "This item or session is no longer available.",
    SESSION_MISMATCH: "This session does not match the checklist. Start a new session.",
    SESSION_IN_PROGRESS: "Another inspection of this checklist is in progress. Join it from the vehicle page.",
    SESSION_UNAVAILABLE: "This inspection cannot be reopened right now.",
    CHECKLIST_VERSION_NOT_CURRENT: "The checklist changed. Reload the screen.",
    SESSION_NOT_OPEN: "This session is not open. Reopen it to continue.",
    SESSION_DEADLINE_PASSED: "This inspection reached its time limit. Start a new one from the vehicle page.",
    OPERATION_REUSED: "This request changed after it was sent. Tap the result again.",
    OPERATION_OWNED_BY_ANOTHER_ACTOR: "This request belongs to another user. Tap the result again.",
    ANOMALY_CONFIRMATION_REQUIRED: "That reading looks unusual.",
    AUDIT_UNAVAILABLE: "The service cannot record this right now.",
    CONFIGURATION_UNAVAILABLE: "The service is not configured to record this. Tell an administrator.",
    RETRY_LATER: "The service is busy.",
    PAPER_BATCH_INVALIDATED: "This paper entry was invalidated. Start a new paper entry from the vehicle page.",
    PAPER_BATCH_MISMATCH: "This paper entry is for another vehicle. Reload the page.",
    PAPER_VERSION_MISMATCH: "This row is not on the checklist version printed on the sheet. Reload the page.",
    PRINTED_KIT_VERSION_MISSING: "No kit version was named for this kit, so its contents cannot be copied. Start a new paper entry naming the kit reference used.",
    KIT_MOVED_SINCE_PAPER: "This kit has left the vehicle since the sheet's time, so it cannot be copied from the sheet.",
    ITEM_NOT_IN_CHECKLIST: "This item is not on the checklist version printed on the sheet.",
    CHECKLIST_UNAVAILABLE: "The printed checklist cannot be read right now. Try again later.",
    PERFORMED_IN_FUTURE: "The sheet's time is later than now."
  };
  function say(code) { return MESSAGES[code] || ("Not accepted (" + code + ")."); }
  // The shell's Saved toast (issue #194); absent when the shell script is not on the page.
  function toast(text, options) { if (window.cyaShell && window.cyaShell.toast) { window.cyaShell.toast(text, options); } }
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
    if (paper) { p.paper_entry_batch_id = paper.batch_id; }   // a paper check takes no session
    if (config.verification_for_deficiency_id) {
      p.purpose = "VERIFICATION";
      p.verification_for_deficiency_id = config.verification_for_deficiency_id;
    }
    if (row.kit_id) { p.kit_id = row.kit_id; }
    if (row.kit_requirement_id) { p.kit_requirement_id = row.kit_requirement_id; }
    if (row.kit_presence) { p.kit_presence = true; }
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
    // A focused action may stay in the DOM until focus moves, so use the newest server row
    // rather than the snapshot captured by that action's original click handler.
    row = latestRow(row);
    if (!row) { return; }
    var k = key(row);
    if (entries[k] && (entries[k].status === "pending" || entries[k].status === "unconfirmed")) { return; }
    if (k === targetKey) { targetKey = null; }
    var entry = {status: "pending", operationId: uuid4(), basis: row.latest_check_id, value: value,
                 retryable: true, outcome: outcome, deficiency: deficiency, explanation: explanation,
                 dueAt: row.due_at || null};
    entries[k] = entry;
    if (config.verification_for_deficiency_id) { verificationRows[k] = row; }
    send(row, entry);
  }
  function latestRow(row) {
    if (!row) { return null; }
    var rowKey = key(row);
    for (var i = 0; i < rows.length; i++) {
      if (key(rows[i]) === rowKey) { return rows[i]; }
    }
    return loadedOnce ? null : row;
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
    savesInFlight += 1;
    post(config.urls.checks, entry.payload).then(function (answer) {
      savesInFlight -= 1;
      // Issue #278 A: this page's own push needs no read of its own; the reload below covers it.
      if (answer.kind === "committed" && answer.body.result.check_id) { rememberOwn(answer.body.result.check_id); }
      if (answer.kind !== "committed" && hintWaiting && !savesInFlight) { scheduleRead(250); }
      if (answer.kind === "committed") {
        entry.status = "saved"; entry.text = "Recorded " + when(answer.body.result.server_recorded_at);
        if (paper) {
          var copied = answer.body.result.paper_entry;
          entry.text = "Saved as performed " + when(copied && copied.performed_at || paper.performed_at) +
            "; entered " + when(answer.body.result.server_recorded_at);
          paperRecorded();
        }
        // The server's outcome is authoritative (a count can derive Fail); remember which check it is.
        entry.savedOutcome = answer.body.result.outcome || entry.outcome || "PASSED";
        entry.checkId = answer.body.result.check_id;
        entry.recordedAt = answer.body.result.server_recorded_at;
        entry.savedAt = Date.now();
        var verification = answer.body.result.verification;
        if (verification) {
          var detail = verification.status === "CLOSED_VERIFIED" ? "Closed — Verified." :
            verification.status === "IN_PROGRESS" ? "Verification failed — back in progress." :
            verification.closure_withheld ? "Verification is complete, but the problem did not close automatically. Contact a supervisor." :
            verification.counts_toward_scope ? verification.remaining_scope_rows + " verification items still need a check." :
            "This saved check did not verify the repair.";
          entry.text += ". " + detail;
          verificationNotice = "Saved. " + detail;
          $("session-message").textContent = verificationNotice;
        }
        delete drafts[key(row)];
        // The toast is the announcement: the item's own Saved line is rebuilt on every render.
        toast("Saved: " + (outcomeWords[entry.savedOutcome] || "check") + " for " + row.item_label +
          (row.kit_label ? " (" + row.kit_label + ")" : "") + ".");
        // Issue #278 B: an item check changes only its item's rows; a kit presence check can
        // change its kit's contents, so it re-reads the whole list (as its push names no item).
        if (row.kit_presence || row.kit_requirement_id) { scheduleReload(0); }
        else { awaitOwnRows(entry.checkId, row.item_id); }
      } else if (answer.kind === "conflict") {
        entry.status = "idle"; entry.text = "";
        askConflict(row, entry, answer.body.newest_check);
      } else if (answer.kind === "rejected") {
        entry.status = "failed"; entry.text = say(answer.code);
        if (answer.code === "CHECKLIST_VERSION_NOT_CURRENT" || answer.code === "SESSION_NOT_OPEN" ||
            answer.code === "SESSION_DEADLINE_PASSED" || answer.code === "PAPER_BATCH_INVALIDATED") {
          scheduleReload(0);
        }
      } else {
        entry.status = "unconfirmed";
        entry.text = "The server did not answer. This may or may not have been recorded. Retry sends the same request.";
      }
      if (answer.kind !== "conflict") { focusIntent = null; }
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
      (entry.status === "saved" || config.verification_for_deficiency_id ? "" : " Turn off ‘Needs attention only’ to find and retry the item.");
  }

  // ---- conflict dialog (CON-2, CON-3) ----------------------------------------------------
  var dialog = $("conflict-dialog"), conflictQueue = [], conflictShowing = false;
  function askConflict(row, entry, newest) {
    if (conflictShowing) { conflictQueue.push({row: row, entry: entry, newest: newest}); return; }
    conflictShowing = true;
    $("conflict-text").textContent = (newest.actor && newest.actor.display_name ? newest.actor.display_name : "Another crew member") +
      " recorded " + (newest.outcome || "a check").replace(/_/g, " ").toLowerCase() + " for \"" + row.item_label +
      "\" at " + when(newest.server_recorded_at) +
      // A paper check names when it was performed as well (SPEC 8.0.0 LATE-6).
      (newest.performed_at ? " (copied from paper; performed " + when(newest.performed_at) + ")" : "") +
      ". Nothing has been saved from your tap.";
    dialog.dataset.key = key(row);
    // The row itself is kept: a live reload (for example the other person's Pass, with Needs
    // Attention Only on) can drop it from the list while this dialog is open (CON-2).
    dialog.row = row;
    dialog.newest = newest.check_id; dialog.entry = entry;
    var intent = focusIntent && focusIntent.key === key(row) ? focusIntent : null;
    var invokerItem = itemForKey(key(row));
    dialog.invoker = intent && invokerItem ? actionIn(invokerItem, intent.action) : document.activeElement;
    openFocusDialog(dialog);
  }
  function closeDialog(keepFocusIntent, afterClose) {
    conflictShowing = false;
    var invoker = dialog.invoker;
    var item = focusIntent && itemForKey(focusIntent.key);
    var target = invoker && document.contains(invoker) ? invoker : item && actionIn(item, focusIntent.action);
    if (!target) {
      var status = $("session-message");
      if (status) { status.textContent = "This item is no longer on the checklist. Refresh and try again."; status.tabIndex = -1; }
      target = status;
      focusIntent = null;
    } else if (!keepFocusIntent) { focusIntent = null; }
    dialog.invoker = null;
    closeFocusDialog(dialog, target, function () {
      if (deferredListRender) { deferredListRender = false; render(); }
      if (afterClose) { afterClose(); }
    });
  }
  function nextConflict() {
    if (!conflictQueue.length) { return; }
    var next = conflictQueue.shift(); askConflict(next.row, next.entry, next.newest);
  }
  function cancelConflict() {
    delete entries[dialog.dataset.key];
    closeDialog(false, function () { render(); nextConflict(); });
  }
  $("conflict-cancel").addEventListener("click", cancelConflict);
  bindFocusDialog(dialog, cancelConflict);
  $("conflict-proceed").addEventListener("click", function () {
    var k = dialog.dataset.key, row = dialog.row, previous = dialog.entry, warned = dialog.newest;
    // Proceed is a new decision, preserving the full Failed/Unable report and the warning basis.
    // Never substitute a newer background row: another check must receive its own warning.
    var operationId = uuid4();
    var entry = {status: "pending", operationId: operationId, retryable: true,
      payload: Object.assign({}, previous.payload, {operation_id: operationId,
        basis_check_id: warned, acknowledges_check_id: warned})};
    closeDialog(true, function () {
      entries[k] = entry;
      send(row, entry); nextConflict();
    });
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
    var create = el("option", "", "Report a new problem"); create.value = ""; outcomeChoice.appendChild(create);
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
      $("outcome-matches").textContent = body.suggestions.length ? "Choose a matching existing report yourself, or describe a new one." : "No possible matches found. Describe a new problem.";
    }).catch(function () { if (generation === outcomeGeneration) { $("outcome-matches").textContent = "Possible matches could not be loaded. Refresh to choose an existing report, or explicitly report a new one."; } });
    var invokerItem = itemForKey(key(row));
    outcomeDialog.invoker = invokerItem && actionIn(invokerItem, outcome.toLowerCase());
    if (!outcomeDialog.invoker) { outcomeDialog.invoker = document.activeElement; }
    openFocusDialog(outcomeDialog);
  }
  function closeOutcome(keepFocusIntent, afterClose) {
    outcomeGeneration += 1;
    var invoker = outcomeDialog.invoker;
    var item = focusIntent && itemForKey(focusIntent.key);
    var target = invoker && document.contains(invoker) ? invoker : item && actionIn(item, focusIntent.action);
    if (!target) {
      var status = $("session-message");
      if (status) { status.textContent = "This item is no longer on the checklist. Refresh and try again."; status.tabIndex = -1; }
      target = status;
      focusIntent = null;
    } else if (!keepFocusIntent) {
      focusIntent = null;
    }
    outcomeDialog.invoker = null;
    closeFocusDialog(outcomeDialog, target, function () {
      if (deferredListRender) { deferredListRender = false; render(); }
      if (afterClose) { afterClose(); }
    });
  }
  function cancelOutcome() { closeOutcome(false); }
  $("outcome-cancel").addEventListener("click", cancelOutcome);
  bindFocusDialog(outcomeDialog, cancelOutcome);
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
      if (!fields.category_id || !fields.severity || !fields.summary || !fields.description) { $("outcome-error").textContent = "Choose a category and severity, and describe the new problem."; return; }
      deficiency = {create: fields};
    }
    row = latestRow(row);
    if (!row) {
      $("session-message").textContent = "This item is no longer on the checklist. Refresh and try again.";
      closeOutcome(false);
      return;
    }
    closeOutcome(true, function () { startCheck(row, outcomeDialog.observation, outcome, deficiency, explanation); });
  });

  // ---- rendering -------------------------------------------------------------------------
  function stateText(row) {
    if (config.verification_for_deficiency_id) { return "Outstanding verification"; }
    if (!row.obligation_state) { return "No check is currently required"; }
    if (row.obligation_state === "SATISFIED" && row.obligation_outcome === "PASSED") { return "Passed"; }
    if (row.obligation_state === "SATISFIED" && row.obligation_outcome === "FAILED") { return "Failed"; }
    if (row.obligation_state === "SATISFIED" && row.obligation_outcome === "UNABLE_TO_INSPECT") { return "Unable to inspect"; }
    if (row.obligation_state === "SATISFIED" && row.obligation_outcome === "SUPERVISOR_COMPLETED") { return "Supervisor completed: unable to inspect"; }
    var map = {OVERDUE: "Overdue", DUE: "Due", CURRENT: "Due soon", SATISFIED: "Complete",
               UPCOMING: "Not due", SUSPENDED: "Check suspended", EXEMPT: "Exempt", WAIVED: "Waived"};
    return map[row.obligation_state] || "";
  }
  // The outcome this person's acknowledged tap recorded, or null when nothing was saved.
  var outcomeWords = {PASSED: "Pass", FAILED: "Fail", UNABLE_TO_INSPECT: "Unable to inspect", NOT_APPLICABLE: "Not applicable"};
  function savedOutcome(entry) { return entry && entry.status === "saved" ? entry.savedOutcome || "PASSED" : null; }
  // The current window's recorded result, which its outcome button shows (issue #140). This
  // person's acknowledged tap counts until the reloaded row carries that check or a newer one.
  // Otherwise, in a satisfied window, the newest check counts only if it is no older than the
  // satisfaction (after Pass then Fail, Fail: issue 87); an older one may be from an earlier window,
  // so the satisfying outcome is shown. A supervisor completion is not a check result: no mark.
  var unmarkedStates = {SUSPENDED: true, EXEMPT: true, WAIVED: true};
  function recordedOutcome(row, entry) {
    // Paper mode marks what was copied from this sheet, not the current window's result: this
    // person's acknowledged tap, else the batch's effective check of the row (paper_entry_check).
    if (paper) {
      var copied = savedOutcome(entry);
      if (copied) { return copied; }
      return row.paper_entry_check ? row.paper_entry_check.outcome : null;
    }
    // Issue #235 B3: with no current window ("No check is currently required", or "Not due" before the
    // window opens) there is no window result to show, even right after this person's tap.
    if (!config.verification_for_deficiency_id &&
        (!row.obligation_state || row.obligation_state === "UPCOMING")) { return null; }
    // These states have no check result to show, even right after this person's tap.
    if (unmarkedStates[row.obligation_state] ||
        (row.obligation_state === "SATISFIED" && row.obligation_outcome === "SUPERVISOR_COMPLETED")) { return null; }
    var mine = savedOutcome(entry);
    if (mine && entry.checkId && row.latest_check_id !== entry.checkId) {
      var rowAt = row.latest_check_at ? Date.parse(row.latest_check_at) : NaN;
      var mineAt = entry.recordedAt ? Date.parse(entry.recordedAt) : NaN;
      if (!row.latest_check_id || rowAt <= mineAt) { return mine; }
    }
    if (row.obligation_state !== "SATISFIED") { return null; }
    var latestAt = row.latest_check_at ? Date.parse(row.latest_check_at) : NaN;
    var completedAt = row.obligation_completed_at ? Date.parse(row.obligation_completed_at) : NaN;
    if (row.latest_check_outcome && latestAt >= completedAt) { return row.latest_check_outcome; }
    return row.obligation_outcome || null;
  }
  function markRecorded(button, outcome, recorded, entry) {
    if (outcome !== recorded) { return; }
    button.className += " outcome-recorded";
    button.dataset.recorded = outcome;
    // This person's save, just acknowledged: the check mark pops once (none with reduced motion).
    var fresh = savedOutcome(entry) === outcome && entry.savedAt && Date.now() - entry.savedAt < 1500;
    var mark = el("span", "outcome-mark" + (fresh ? " outcome-mark-new" : ""), "✓ ");
    mark.setAttribute("aria-hidden", "true");
    var text = button.textContent;
    button.textContent = "";
    button.appendChild(mark);
    button.appendChild(document.createTextNode(text));
    button.setAttribute("aria-label", button.getAttribute("aria-label") + " (recorded result: " + outcomeWords[outcome] + ")");
  }
  function saveIndicator(entry) {
    // A saved result is shown on its button; the line remains for screen readers (issue #140).
    var p = el("p", "save-state save-state-" + entry.status + (entry.status === "saved" ? " visually-hidden" : ""));
    // A saved check is announced by the shell's toast; this line stays readable on the item.
    if (entry.status !== "saved") { p.setAttribute("role", "status"); }
    var icons = {pending: "⌛", saved: "✓", failed: "✕", unconfirmed: "?"};
    var outcome = savedOutcome(entry);
    // Say which result was saved, so a saved Fail never reads as a saved Pass (issue 87).
    var words = {pending: "Saving…", saved: outcome && outcomeWords[outcome] ? "Saved: " + outcomeWords[outcome] : "Saved",
                 failed: "Not saved", unconfirmed: "Unconfirmed"};
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
    // Outcome buttons stay actions; the one matching the current window's recorded result is also
    // marked with a check mark and named as recorded, never by colour alone (issue #140).
    var recorded = recordedOutcome(row, entry);
    var button = el("button", "button",
      row.kind === "QUANTITY" ? "Record count" : "Pass");
    // Keep a busy action focusable so a pending-save render can retain focus on its named control.
    // Its handler checks the pending state before sending, so aria-disabled is sufficient here.
    button.type = "button";
    if (busy) { button.setAttribute("aria-disabled", "true"); }
    button.dataset.action = row.kind === "QUANTITY" || row.kind === "MEASUREMENT" ? "record" : "pass";
    button.setAttribute("aria-label", (row.kind === "QUANTITY" ? "Record count for " : "Record Pass for ") + label);
    if (row.kind === "QUANTITY" || row.kind === "MEASUREMENT") {
      var input = el("input", "number-input");
      input.disabled = !!busy;
      input.type = "text"; input.inputMode = row.kind === "QUANTITY" ? "numeric" : "decimal";
      input.id = "in-" + li.id; input.value = drafts[k] || "";
      input.dataset.action = "observation";
      input.setAttribute("aria-label", (row.kind === "QUANTITY" ? "Counted quantity" : "Measured value" + (row.unit ? " in " + row.unit : "")) + " for " + label);
      input.addEventListener("input", function () { drafts[k] = input.value; });
      box.appendChild(input);
      if (row.kind === "QUANTITY") { box.appendChild(el("span", "muted", "Expected " + row.expected_quantity)); }
      if (row.unit && row.kind === "MEASUREMENT") { box.appendChild(el("span", "muted", row.unit)); }
      button.addEventListener("click", function () {
        rememberFocus(row, "record");
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
      button.addEventListener("click", function () { rememberFocus(row, "pass"); startCheck(row, null); });
    }
    if (row.allowed_outcomes.indexOf("PASSED") !== -1) { markRecorded(button, "PASSED", recorded, entry); box.appendChild(button); }
    ["FAILED", "UNABLE_TO_INSPECT"].forEach(function (outcome) {
      if (row.allowed_outcomes.indexOf(outcome) === -1 || (config.verification_for_deficiency_id && outcome === "UNABLE_TO_INSPECT")) { return; }
      var action = el("button", "button", outcome === "FAILED" ? "Fail" : "Unable to inspect");
      action.type = "button";
      if (busy) { action.setAttribute("aria-disabled", "true"); }
      action.dataset.action = outcome.toLowerCase();
      action.setAttribute("aria-label", "Record " + (outcome === "FAILED" ? "Fail" : "Unable to inspect") + " for " + label);
      action.addEventListener("click", function () { rememberFocus(row, outcome.toLowerCase()); openOutcome(row, outcome); });
      markRecorded(action, outcome, recorded, entry);
      box.appendChild(action);
    });
    if (entry && entry.retryable && (entry.status === "failed" || entry.status === "unconfirmed")) {
      var retry = el("button", "button", "Retry");
      retry.type = "button";
      retry.dataset.action = "retry";
      retry.setAttribute("aria-label", "Retry " + label);
      retry.addEventListener("click", function () { rememberFocus(row, "retry"); retryCheck(row); });
      box.appendChild(retry);
    }
    return box;
  }
  function navigationStorageKey() {
    if (config.user_id && paper) { return "cya.inspection.navigation.v1:" + config.user_id + ":paper:" + paper.batch_id; }
    return config.user_id && config.session_id ?
      "cya.inspection.navigation.v1:" + config.user_id + ":" + config.session_id : null;
  }
  function readNavigation() {
    var storageKey = navigationStorageKey();
    if (!storageKey) { return null; }
    try {
      var value = JSON.parse(window.localStorage.getItem(storageKey) || "null");
      if (!value || value.version !== 1 || ["COMPARTMENTS", "FLAT"].indexOf(value.mode) === -1) { return null; }
      return {
        mode: value.mode,
        groupKey: typeof value.groupKey === "string" ? value.groupKey.slice(0, 160) : null,
        anchorKey: typeof value.anchorKey === "string" ? value.anchorKey.slice(0, 120) : null
      };
    } catch (_error) { return null; }
  }
  function saveNavigation(anchorKey) {
    var storageKey = navigationStorageKey();
    if (!storageKey || !navigationRestored) { return; }
    try {
      window.localStorage.setItem(storageKey, JSON.stringify({
        version: 1, mode: layoutMode, groupKey: selectedGroupKey, anchorKey: anchorKey || null
      }));
    } catch (_error) { /* Navigation remains usable when storage is unavailable. */ }
  }
  function groupKeyForRow(row) {
    if (row.not_placed) { return "not-placed"; }
    if (row.compartment_id) { return "compartment:" + row.compartment_id; }
    if (row.system_id) { return "system:" + row.system_id; }
    if (row.kit_id) { return "kit:" + row.kit_id; }
    return "vehicle";
  }
  function groupLabelForRow(row) {
    if (row.not_placed) { return "Not placed"; }
    if (row.compartment_label) { return row.compartment_label; }
    if (row.system_label) { return "Vehicle system: " + row.system_label; }
    if (row.kit_label) { return "Kit: " + row.kit_label; }
    return "Vehicle-wide checks";
  }
  function checklistRows() {
    return rows.filter(function (row) {
      return config.verification_for_deficiency_id || row.checklist_version_id === config.checklist_version_id;
    });
  }
  // Issue #235 C2 (owner decision): a short line with the counts that need action, and the
  // rest of the breakdown behind a Details disclosure beside the card.
  function progressText(group) {
    return group.checked + " of " + group.total + " checked · " + group.due + " due · " +
      group.overdue + " overdue · " + group.open_deficiencies + " open problem" +
      (group.open_deficiencies === 1 ? "" : "s");
  }
  function breakdownText(group) {
    return group.due_soon + " due soon · " + group.not_due + " not due · " +
      group.no_obligation + " not required · " + group.exceptions + " exception" +
      (group.exceptions === 1 ? "" : "s");
  }
  // Paper mode: how many of an area's rows the batch holds a check for (or this page just saved).
  function copiedText(group) {
    var copied = 0;
    checklistRows().forEach(function (row) {
      if (groupKeyForRow(row) === group.key && (row.paper_entry_check || savedOutcome(entries[key(row)]))) { copied += 1; }
    });
    return copied + " of " + group.total + " copied from this sheet";
  }
  function renderAreaCards() {
    var cards = $("area-cards"), focusedGroup = null, focusedDetails = false, openDetails = {};
    // In split view the list stays on screen through live updates: keep a focused card focused,
    // on the same control, and keep open Details open.
    for (var node = document.activeElement; node && node !== cards; node = node.parentNode) {
      if (String(node.className || "").indexOf("area-card-details-toggle") !== -1) { focusedDetails = true; }
      if (node.parentNode === cards && node.dataset) { focusedGroup = node.dataset.groupKey; }
    }
    Array.prototype.forEach.call(cards.children, function (card) {
      var details = card.dataset && card.dataset.groupKey && card.children && card.children[1];
      if (details && details.open) { openDetails[card.dataset.groupKey] = true; }
    });
    while (cards.firstChild) { cards.removeChild(cards.firstChild); }
    groupProgressRows.forEach(function (group) {
      var card = el("div", "area-card-item");
      card.dataset.groupKey = group.key;
      var button = el("button", "area-card", "");
      button.type = "button"; button.dataset.groupKey = group.key;
      button.appendChild(el("span", "area-card-title", group.label));
      var left = group.due + group.due_soon + group.overdue;
      if (paper) {
        button.appendChild(el("span", "area-card-summary", copiedText(group)));
        button.appendChild(el("span", "area-card-short", copiedText(group)));
      } else {
        button.appendChild(el("span", "area-card-summary", progressText(group)));
        button.appendChild(el("span", "area-card-short", left ? left + " still due" : "Nothing due"));
      }
      if (group.key === selectedGroupKey) { button.setAttribute("aria-current", "true"); }
      button.addEventListener("click", function () {
        layoutMode = "COMPARTMENTS"; selectedGroupKey = group.key; navigationAnchorKey = null;
        saveNavigation(); render(); $("area-heading").focus({preventScroll: true});
      });
      card.appendChild(button);
      // The window breakdown says nothing about the sheet, so paper mode leaves it out.
      if (paper) { cards.appendChild(card); if (group.key === focusedGroup) { button.focus({preventScroll: true}); } return; }
      var details = el("details", "area-card-details");
      var summary = el("summary", "area-card-details-toggle");
      summary.appendChild(el("span", "", "Details"));
      summary.appendChild(el("span", "visually-hidden", " for " + group.label));
      details.appendChild(summary);
      details.appendChild(el("p", "area-card-breakdown", breakdownText(group)));
      if (openDetails[group.key]) { details.open = true; }
      card.appendChild(details);
      cards.appendChild(card);
      if (group.key === focusedGroup) { (focusedDetails ? summary : button).focus({preventScroll: true}); }
    });
  }
  function applySavedNavigation() {
    if (navigationRestored) { return; }
    navigationRestored = true;
    if (pendingNavigation) {
      focusOnRestore = true;
      layoutMode = pendingNavigation.mode;
      if (layoutMode === "COMPARTMENTS" && pendingNavigation.groupKey &&
          groupProgressRows.some(function (group) { return group.key === pendingNavigation.groupKey; })) {
        selectedGroupKey = pendingNavigation.groupKey;
        navigationAnchorKey = pendingNavigation.anchorKey;
      } else if (layoutMode === "COMPARTMENTS") {
        selectedGroupKey = null;
      } else {
        selectedGroupKey = null;
        navigationAnchorKey = pendingNavigation.anchorKey;
      }
    }
    pendingNavigation = null;
  }
  function restoreNavigationAnchor() {
    var focusIt = focusOnRestore;
    focusOnRestore = false;
    var anchor = navigationAnchorKey ? itemForKey(navigationAnchorKey) : null;
    navigationAnchorKey = null;
    if (anchor && typeof anchor.scrollIntoView === "function") {
      var scroll = function () { anchor.scrollIntoView({block: "start"}); };
      if (typeof window.requestAnimationFrame === "function") { window.requestAnimationFrame(scroll); } else { scroll(); }
    }
    // Issue #235 B2: Tab continues from the restored place, not from the skip link. Focus the
    // saved item, else the open area's heading; never take focus the person already moved.
    if (!focusIt) { return; }
    var active = document.activeElement;
    if (active && active !== document.body) { return; }
    var target = anchor || (layoutMode === "COMPARTMENTS" && selectedGroupKey ? $("area-heading") : null);
    if (target && typeof target.focus === "function") { target.focus({preventScroll: true}); }
  }
  function kitBuckets(itemRows) {
    var buckets = [], kits = Object.create(null);
    itemRows.forEach(function (row) {
      if (!row.kit_id) { buckets.push({rows: [row]}); return; }
      var kitKey = String(row.kit_id), bucket = kits[kitKey];
      if (!bucket) { bucket = {kitId: kitKey, label: row.kit_label || "Kit", rows: []}; kits[kitKey] = bucket; buckets.push(bucket); }
      bucket.rows.push(row);
    });
    return buckets;
  }
  function appendItemRows(target, itemRows) {
    var buckets = kitBuckets(itemRows), labelCounts = Object.create(null), labelIndexes = Object.create(null);
    buckets.forEach(function (bucket) { if (bucket.kitId) { labelCounts[bucket.label] = (labelCounts[bucket.label] || 0) + 1; } });
    var index = 0;
    buckets.forEach(function (bucket) {
      if (bucket.kitId) {
        labelIndexes[bucket.label] = (labelIndexes[bucket.label] || 0) + 1;
        var label = "Kit instance: " + bucket.label;
        if (labelCounts[bucket.label] > 1) { label += " (" + labelIndexes[bucket.label] + ")"; }
        var heading = el("li", "kit-heading");
        heading.appendChild(el("h4", "", label)); target.appendChild(heading);
      }
      bucket.rows.forEach(function (row) { target.appendChild(itemNode(row, index++)); });
    });
  }
  function setLayout(mode) {
    layoutMode = mode; selectedGroupKey = null; navigationAnchorKey = null;
    saveNavigation(); render();
    var target = mode === "COMPARTMENTS" ? $("area-overview-heading") : $("checklist-heading");
    if (target) { target.focus({preventScroll: true}); }
  }
  if ($("layout-compartments")) {
    $("layout-compartments").addEventListener("click", function () { setLayout("COMPARTMENTS"); });
    $("layout-flat").addEventListener("click", function () { setLayout("FLAT"); });
    $("area-overview-button").addEventListener("click", function () {
      selectedGroupKey = null; navigationAnchorKey = null; saveNavigation(); render();
      $("area-overview-heading").focus({preventScroll: true});
    });
    document.addEventListener("scroll", function () {
      if (!navigationRestored || layoutMode === "COMPARTMENTS" && !selectedGroupKey) { return; }
      clearTimeout(navigationSaveTimer);
      navigationSaveTimer = setTimeout(function () {
        var anchorKey = null;
        for (var i = 0; i < list.children.length; i++) {
          var item = list.children[i], rect = item.getBoundingClientRect();
          if (item.dataset.itemKey && rect.bottom > 0 && rect.top < window.innerHeight) { anchorKey = item.dataset.itemKey; break; }
        }
        saveNavigation(anchorKey);
      }, 150);
    }, {passive: true});
  }

  function itemNode(row, index) {
    var k = key(row), entry = entries[k];
    var li = el("li", "item" + (row.item_attention === "ATTENTION" ? " item-attention" : "") + (k === targetKey ? " item-target" : ""));
    li.id = "item-" + index;
    li.dataset.itemKey = k; li.dataset.groupKey = groupKeyForRow(row); li.tabIndex = -1;
    var head = el("div", "item-head");
    var titleText = row.item_label;
    if (row.kit_label && (layoutMode === "FLAT" || config.verification_for_deficiency_id)) { titleText += " — " + row.kit_label; }
    var title = el("h3", "", titleText);
    head.appendChild(title);
    // The explicit deficiency block already communicates attention for this row.
    if (row.item_attention === "ATTENTION" && !row.deficiencies.length) {
      var attentionPill = el("span", "pill pill-needs-attention");
      var warningIcon = el("span", "", "⚠");
      warningIcon.setAttribute("aria-hidden", "true");
      attentionPill.appendChild(warningIcon);
      attentionPill.appendChild(document.createTextNode(" Needs attention"));
      head.appendChild(attentionPill);
    }
    // Paper mode shows what was copied from the sheet, not the current window's state.
    var st = paper ? "" : stateText(row);
    if (st) {
      var status = el("span", "window-status", st);
      status.setAttribute("aria-label", (config.verification_for_deficiency_id ? "Verification state: " : "Current-window state: ") + st);
      status.dataset.state = row.obligation_state || "NONE";
      head.appendChild(status);
    }
    li.appendChild(head);
    var place = [row.compartment_label, row.system_label].filter(Boolean).join(" · ");
    if (place && (layoutMode === "FLAT" || config.verification_for_deficiency_id)) { li.appendChild(el("p", "muted item-location", place)); }
    if (row.instructions) { li.appendChild(el("p", "instructions", row.instructions)); }
    row.deficiencies.forEach(function (d) {
      var box = el("div", "known-deficiency");
      var status = d.status.replace(/_/g, " ").toLowerCase().replace(/^./, function (c) { return c.toUpperCase(); });
      box.appendChild(el("strong", "", "Problem status: " + status + ". Severity: " + d.severity_label + ". "));
      box.appendChild(document.createTextNode(d.summary));
      var historyLink = el("a", "", " View report and history");
      historyLink.href = config.urls.deficiency_detail.replace("DEFICIENCY", encodeURIComponent(d.id));
      box.appendChild(historyLink);
      li.appendChild(box);
    });
    if (row.latest_check_id && row.latest_check_paper) {
      // A paper check stays marked Paper entry, with its performers and who entered it when
      // (SPEC 8.0.0 LATE-5), in every mode.
      var source = row.latest_check_paper, line = el("p", "last-check");
      line.appendChild(document.createTextNode("Latest recorded check: " +
        (outcomeWords[row.latest_check_outcome] || "Outcome unavailable") + " at " + when(row.latest_check_at) + " "));
      line.appendChild(el("span", "pill pill-paper", "Paper entry"));
      line.appendChild(document.createTextNode(" performed by " +
        ((source.performers || []).join(", ") || "unknown") + "; entered by " +
        (source.entered_by || "unknown") + " at " + when(source.entered_at)));
      li.appendChild(line);
    } else if (row.latest_check_id) {
      var outcome = outcomeWords[row.latest_check_outcome] || "Outcome unavailable";
      li.appendChild(el("p", "last-check", "Latest recorded check: " + outcome +
        (row.latest_check_by ? " by " + row.latest_check_by : "") + " at " + when(row.latest_check_at)));
    }
    if (paper && row.paper_entry_check) {
      var copiedLine = el("p", "paper-copied");
      var tick = el("span", "", "✓ "); tick.setAttribute("aria-hidden", "true");
      copiedLine.appendChild(tick);
      copiedLine.appendChild(document.createTextNode("Copied from this sheet: " +
        (outcomeWords[row.paper_entry_check.outcome] || "recorded")));
      li.appendChild(copiedLine);
    }
    li.appendChild(controls(row, li));
    if (entry && entry.status !== "idle") { li.appendChild(saveIndicator(entry)); }
    return li;
  }
  function controlsIn(item) {
    var pending = [item];
    while (pending.length) {
      var node = pending.shift();
      if (String(node.className || "").split(/\s+/).indexOf("item-controls") !== -1) { return node; }
      Array.prototype.push.apply(pending, node.children || []);
    }
    return null;
  }
  function syncFocusedControl(current, replacement) {
    current.className = replacement.className;
    current.disabled = replacement.disabled;
    current.value = replacement.value;
    current.checked = replacement.checked;
    if (current.dataset && replacement.dataset) {
      Object.keys(current.dataset).forEach(function (name) {
        if (!Object.prototype.hasOwnProperty.call(replacement.dataset, name)) { delete current.dataset[name]; }
      });
      Object.keys(replacement.dataset).forEach(function (name) { current.dataset[name] = replacement.dataset[name]; });
    }
    ["aria-label", "aria-disabled"].forEach(function (name) {
      var value = replacement.getAttribute(name);
      if (value === null) { current.removeAttribute(name); }
      else { current.setAttribute(name, value); }
    });
    if (current.type === "button") {
      current.textContent = "";
      var content = replacement.childNodes || replacement.children;
      if (!content.length) { current.textContent = replacement.textContent; }
      Array.prototype.forEach.call(content, function (child) {
        current.appendChild(child.cloneNode(true));
      });
    }
  }
  function preserveFocusedItem(current, replacement, action) {
    var currentControls = controlsIn(current), replacementControls = controlsIn(replacement);
    if (!currentControls || !replacementControls) { return false; }
    if (!actionIn(currentControls, action) || !actionIn(replacementControls, action)) { return false; }
    var desiredControls = Array.prototype.map.call(replacementControls.children, function (next) {
      if (!next.dataset || !next.dataset.action) { return next; }
      var existing = actionIn(currentControls, next.dataset.action);
      if (!existing || existing.type !== next.type) { return next; }
      syncFocusedControl(existing, next);
      return existing;
    });
    Array.prototype.slice.call(currentControls.children).forEach(function (child) {
      if (desiredControls.indexOf(child) === -1) { currentControls.removeChild(child); }
    });
    desiredControls.forEach(function (child, index) {
      if (currentControls.children[index] !== child) {
        currentControls.insertBefore(child, currentControls.children[index] || null);
      }
    });
    current.id = replacement.id;
    current.className = replacement.className;
    current.dataset.itemKey = replacement.dataset.itemKey;
    current.dataset.groupKey = replacement.dataset.groupKey;
    Array.prototype.slice.call(current.children).forEach(function (child) {
      if (child !== currentControls) { current.removeChild(child); }
    });
    var afterControls = false;
    Array.prototype.forEach.call(replacement.children, function (child) {
      if (child === replacementControls) { afterControls = true; return; }
      if (afterControls) { current.appendChild(child); }
      else { current.insertBefore(child, currentControls); }
    });
    return true;
  }
  function render() {
    var modalOpen = (dialog && dialog.open) || (outcomeDialog && outcomeDialog.open);
    deferredListRender = modalOpen;
    var focused = document.activeElement, focusId = focused && focused.id && list.contains(focused) ? focused.id : null;
    var focusKey = null, focusAction = null, focusInList = false;
    for (var parent = focused; parent && parent !== list; parent = parent.parentNode) {
      if (parent.parentNode === list) { focusKey = parent.dataset.itemKey; }
    }
    focusInList = !!focusKey;
    focusAction = focused && focused.dataset ? focused.dataset.action : null;
    var anchor = null, anchorIndex = -1, anchorItemIndex = -1, anchorTop = null;
    for (var visible = 0; visible < list.children.length; visible++) {
      var candidate = list.children[visible], rect = candidate.getBoundingClientRect();
      if (candidate.dataset.itemKey && (typeof window === "undefined" || rect.bottom > 0 && rect.top < window.innerHeight)) {
        anchor = candidate; anchorIndex = visible; anchorTop = rect.top; break;
      }
    }
    function itemIndexAt(childIndex) {
      var index = -1;
      for (var i = 0; i <= childIndex && i < list.children.length; i++) {
        if (list.children[i].dataset.itemKey) { index++; }
      }
      return index;
    }
    if (anchor) { anchorItemIndex = itemIndexAt(anchorIndex); }
    if (focusKey) {
      var focusedItem = itemForKey(focusKey);
      if (focusedItem) {
        anchor = focusedItem; anchorIndex = Array.prototype.indexOf.call(list.children, focusedItem);
        anchorItemIndex = itemIndexAt(anchorIndex);
        anchorTop = focusedItem.getBoundingClientRect().top;
      }
    }
    var allShown = checklistRows();
    applySavedNavigation();
    var selectedGroup = groupProgressRows.filter(function (group) { return group.key === selectedGroupKey; })[0];
    if (layoutMode === "COMPARTMENTS" && selectedGroupKey && !selectedGroup) {
      selectedGroupKey = null; navigationAnchorKey = null; saveNavigation();
    }
    var overview = !!$("area-overview") && layoutMode === "COMPARTMENTS" && !selectedGroupKey;
    var detail = !!$("area-detail") && layoutMode === "COMPARTMENTS" && !!selectedGroupKey;
    var split = detail && !!wideQuery && wideQuery.matches;
    if ($("checklist-body")) { $("checklist-body").className = "checklist-body" + (split ? " checklist-split" : ""); }
    if ($("area-overview")) {
      $("area-overview").hidden = !(overview || split);
      $("area-detail").hidden = !detail;
      $("layout-compartments").setAttribute("aria-pressed", String(layoutMode === "COMPARTMENTS"));
      $("layout-flat").setAttribute("aria-pressed", String(layoutMode === "FLAT"));
      if (overview || split) { renderAreaCards(); }
      if (detail && selectedGroup) { $("area-heading").textContent = selectedGroup.label; }
    }
    var shown = allShown;
    if (detail) { shown = shown.filter(function (row) { return groupKeyForRow(row) === selectedGroupKey; }); }
    // Build rows off-screen. Keep the focused row and its action controls attached while refreshing
    // its label and status; replacing that subtree makes NVDA announce generic document context.
    var replacement = document.createElement("ol"), replacementNodes, preservedRow = null, focusTransferred = false;
    if (!overview) {
      if (detail) { appendItemRows(replacement, shown); }
      else { shown.forEach(function (row, index) { replacement.appendChild(itemNode(row, index)); }); }
    }
    replacementNodes = Array.prototype.slice.call(replacement.children);
    var preservedIndex = -1;
    var preserveKey = focusIntent && focusKey === focusIntent.key ? focusIntent.key : focusKey;
    var preserveAction = focusIntent && focusKey === focusIntent.key ? focusIntent.action : focusAction;
    // A modal's invoking row stays attached while the rest of the list refreshes. If the item
    // disappeared, it is removed; closing the dialog then falls back to the checklist.
    var dialogKey = modalOpen && dialog.open ? dialog.dataset.key :
      modalOpen && outcomeDialog.row ? key(outcomeDialog.row) : null;
    if (dialogKey) {
      var dialogRowExists = rows.some(function (row) {
        return key(row) === dialogKey && (config.verification_for_deficiency_id || row.checklist_version_id === config.checklist_version_id);
      });
      for (var modalIndex = 0; modalIndex < replacementNodes.length; modalIndex++) {
        if (replacementNodes[modalIndex].dataset.itemKey === dialogKey) {
          if (dialogRowExists) {
            preservedRow = itemForKey(dialogKey);
            preservedIndex = modalIndex;
            if (preservedRow) { replacement.removeChild(replacementNodes[modalIndex]); }
          }
          break;
        }
      }
      if (dialogRowExists && !preservedRow) {
        preservedRow = itemForKey(dialogKey);
        if (preservedRow) { preservedIndex = replacementNodes.length; }
      }
    }
    if (!preservedRow && preserveKey && preserveAction) {
      for (var nextIndex = 0; nextIndex < replacementNodes.length; nextIndex++) {
        var nextRow = replacementNodes[nextIndex];
        if (nextRow.dataset && nextRow.dataset.itemKey === preserveKey) {
          var nextAction = actionIn(nextRow, preserveAction);
          if (nextAction) {
            var previousRow = itemForKey(preserveKey);
            if (previousRow && preserveFocusedItem(previousRow, nextRow, preserveAction)) {
              preservedRow = previousRow;
              replacement.removeChild(nextRow);
              replacementNodes[nextIndex] = preservedRow;
            } else {
              preservedRow = nextRow;
            }
            preservedIndex = nextIndex;
            if (preservedRow === nextRow) {
              replacement.removeChild(preservedRow);
              if (previousRow) { list.insertBefore(preservedRow, previousRow); }
              else { list.appendChild(preservedRow); }
              nextAction.focus({preventScroll: true});
            }
            focusTransferred = true;
          }
          break;
        }
      }
    }
    Array.prototype.slice.call(list.children).forEach(function (child) {
      if (child !== preservedRow) { list.removeChild(child); }
    });
    replacementNodes.forEach(function (child, index) {
      if (child.parentNode === replacement) { replacement.removeChild(child); }
      if (child === preservedRow) { return; }
      if (preservedRow && index < preservedIndex) { list.insertBefore(child, preservedRow); }
      else { list.appendChild(child); }
    });
    list.hidden = overview;
    var message = "";
    if (rows.length && !allShown.length) {
      message = "This vehicle's checklist changed after the session started. Start a new session to use the current one.";
    } else if (!allShown.length) {
      message = config.verification_for_deficiency_id ? "No outstanding verification items available here. See the report for scope and status." : onlyAttention ? "Nothing needs attention right now." : "This vehicle's checklist has no items yet. Ask an administrator to add them.";
    } else if (!shown.length && !overview) {
      message = onlyAttention ? detail ? "Nothing in this area needs attention." : "Nothing needs attention right now." : "No checklist items are available in this area.";
    }
    notice.textContent = message; notice.hidden = !message;
    if (!modalOpen) {
      if (focusTransferred && focusIntent) {
        if (entries[focusIntent.key] && entries[focusIntent.key].status !== "pending") { focusIntent = null; }
      } else if (focusIntent && (focusKey === focusIntent.key || focused === list || !focused || focused === document.body)) {
        restoreFocusIntent(true);
      } else if (focusInList) {
        var restoredItem = itemForKey(focusKey);
        var target = restoredItem && actionIn(restoredItem, focusAction);
        if (!target || target.disabled) {
          if (focusAction) { focusStatusMessage("This checklist action is no longer available."); }
          else { (restoredItem || list).focus({preventScroll: true}); }
        } else if (document.activeElement !== target) { target.focus({preventScroll: true}); }
      } else if (focusIntent) {
        focusIntent = null;
      } else if (focusId && $(focusId)) { $(focusId).focus({preventScroll: true}); }
    }
    if (anchor && anchorTop !== null && typeof window !== "undefined" && typeof window.scrollBy === "function") {
      var replacement = itemForKey(anchor.dataset.itemKey);
      if (!replacement && shown.length) {
        var itemNodes = Array.prototype.filter.call(list.children, function (node) { return !!node.dataset.itemKey; });
        if (itemNodes.length) { replacement = itemNodes[Math.min(Math.max(anchorItemIndex, 0), itemNodes.length - 1)]; }
      }
      if (replacement) {
        var shift = replacement.getBoundingClientRect().top - anchorTop;
        if (shift) { window.scrollBy(0, shift); }
      }
    }
    restoreNavigationAnchor();
    renderBar();
  }

  // ---- checklist aids (issue #194): progress, Next unchecked item, jump to a section -------
  // The bar sits at the bottom of the checklist. Progress uses the server's counts for the whole
  // checklist (not the Needs Attention Only filter); Next moves through the rows on this screen.
  // Pressing Next moves focus to the button, so the last item that had focus is remembered.
  var dueStates = {DUE: true, CURRENT: true, OVERDUE: true}, targetKey = null, lastItemKey = null;
  function itemKeyFrom(node) {
    for (; node && node !== list; node = node.parentNode) {
      if (node.parentNode === list && node.dataset && node.dataset.itemKey) { return node.dataset.itemKey; }
    }
    return null;
  }
  list.addEventListener("focusin", function (event) { lastItemKey = itemKeyFrom(event.target) || lastItemKey; });
  function needsCheck(row) {
    var entry = entries[key(row)];
    if (entry && entry.status === "pending") { return false; }
    // A saved tap counts until the reloaded row carries that check.
    if (savedOutcome(entry) && entry.checkId && row.latest_check_id !== entry.checkId) { return false; }
    return !!dueStates[row.obligation_state];
  }
  function flattenBuckets(itemRows) {
    var out = [];
    kitBuckets(itemRows).forEach(function (bucket) { Array.prototype.push.apply(out, bucket.rows); });
    return out;
  }
  // The rows in the order the crew member meets them: area by area in Compartments.
  function orderedRows() {
    var all = checklistRows();
    if (layoutMode !== "COMPARTMENTS") { return all; }
    var out = [];
    groupProgressRows.forEach(function (group) {
      Array.prototype.push.apply(out, flattenBuckets(all.filter(function (row) { return groupKeyForRow(row) === group.key; })));
    });
    return out;
  }
  function progressCounts() {
    var done = 0, left = 0;
    groupProgressRows.forEach(function (group) { done += group.checked; left += group.due + group.due_soon + group.overdue; });
    return {done: done, left: left};
  }
  function renderBar() {
    var bar = $("checklist-bar");
    if (!bar || paper || config.verification_for_deficiency_id || config.state !== "OPEN") { return; }
    bar.hidden = !groupProgressRows.length;
    var counts = progressCounts(), total = counts.done + counts.left;
    var progress = $("checklist-progress");
    progress.max = total || 1; progress.value = total ? counts.done : 0;
    progress.hidden = !total;  // no bar when nothing is due (issue #235 C2)
    $("checklist-progress-text").textContent = !total ? "Nothing is due right now." :
      counts.done + " of " + total + " checked" + (counts.left ? " · " + counts.left + " still due" : " · all done");
    if (!$("jump-panel").hidden) { renderJumpList(); }
  }
  function currentPosition() {
    var focusedKey = itemKeyFrom(document.activeElement);
    if (focusedKey) { return {key: focusedKey, after: true}; }
    if (list.hidden) { return null; }
    if (lastItemKey && itemForKey(lastItemKey)) { return {key: lastItemKey, after: true}; }
    for (var i = 0; i < list.children.length; i++) {
      var item = list.children[i], rect = item.getBoundingClientRect();
      if (item.dataset.itemKey && rect.bottom > 0 && rect.top < window.innerHeight) { return {key: item.dataset.itemKey, after: false}; }
    }
    return null;
  }
  function showItem(itemKey) {
    var item = itemForKey(itemKey);
    if (!item) { return; }
    if (typeof item.scrollIntoView === "function") { item.scrollIntoView({block: "center"}); }
    item.focus({preventScroll: true});
  }
  function goToRow(row) {
    var groupKey = groupKeyForRow(row);
    targetKey = key(row);
    if (layoutMode === "COMPARTMENTS" && groupKey !== selectedGroupKey) {
      selectedGroupKey = groupKey; navigationAnchorKey = null; saveNavigation();
    }
    render();
    lastItemKey = targetKey;
    showItem(targetKey);
  }
  function nextUnchecked() {
    var ordered = orderedRows(), status = $("checklist-bar-status"), start = 0, position = currentPosition();
    if (position) {
      for (var i = 0; i < ordered.length; i++) {
        if (key(ordered[i]) === position.key) { start = i + (position.after ? 1 : 0); break; }
      }
    }
    for (var n = 0; n < ordered.length; n++) {
      var row = ordered[(start + n) % ordered.length];
      if (needsCheck(row)) { status.textContent = ""; goToRow(row); return; }
    }
    status.textContent = progressCounts().left && onlyAttention ?
      "No unchecked items are shown. Turn off ‘Needs attention only’ to see the rest." :
      progressCounts().left ? "No unchecked items are shown on this screen." : "Every due item is checked.";
  }
  function jumpTo(groupKey) {
    if (layoutMode === "COMPARTMENTS") {
      selectedGroupKey = groupKey; navigationAnchorKey = null; saveNavigation(); render();
      var heading = $("area-heading");
      if (typeof heading.scrollIntoView === "function") { heading.scrollIntoView({block: "start"}); }
      heading.focus({preventScroll: true});
      return;
    }
    for (var i = 0; i < list.children.length; i++) {
      if (list.children[i].dataset.groupKey === groupKey) { showItem(list.children[i].dataset.itemKey); return; }
    }
  }
  function renderJumpList() {
    var target = $("jump-list"), focusedKey = null;
    for (var node = document.activeElement; node && node !== target; node = node.parentNode) {
      if (node.dataset && node.dataset.groupKey && node.parentNode && node.parentNode.parentNode === target) { focusedKey = node.dataset.groupKey; }
    }
    while (target.firstChild) { target.removeChild(target.firstChild); }
    var flat = layoutMode !== "COMPARTMENTS", present = {};
    if (flat) { checklistRows().forEach(function (row) { present[groupKeyForRow(row)] = true; }); }
    groupProgressRows.forEach(function (group) {
      if (flat && !present[group.key]) { return; }
      var item = el("li"), button = el("button", "jump-button"), left = group.due + group.due_soon + group.overdue;
      button.type = "button"; button.dataset.groupKey = group.key;
      button.appendChild(el("span", "jump-label", group.label));
      button.appendChild(el("span", "jump-count", left ? left + " still due" : "Nothing due"));
      if (!flat && group.key === selectedGroupKey) { button.setAttribute("aria-current", "true"); }
      button.addEventListener("click", function () { closeJump(false); jumpTo(group.key); });
      item.appendChild(button); target.appendChild(item);
      if (focusedKey === group.key) { button.focus({preventScroll: true}); }
    });
  }
  function closeJump(returnFocus) {
    $("jump-panel").hidden = true;
    $("jump-sections").setAttribute("aria-expanded", "false");
    if (returnFocus) { $("jump-sections").focus({preventScroll: true}); }
  }
  if (!paper && $("checklist-bar") && !config.verification_for_deficiency_id && config.state === "OPEN") {
    $("next-unchecked").addEventListener("click", function () { closeJump(false); nextUnchecked(); });
    $("jump-sections").addEventListener("click", function () {
      if (!$("jump-panel").hidden) { closeJump(true); return; }
      renderJumpList();
      $("jump-panel").hidden = false;
      $("jump-sections").setAttribute("aria-expanded", "true");
    });
    $("checklist-bar").addEventListener("keydown", function (event) {
      if (event.key === "Escape" && !$("jump-panel").hidden) { event.preventDefault(); closeJump(true); }
    });
  }

  // ---- loading and live updates ----------------------------------------------------------
  function setStatus(s) {
    var badge = $("op-status");
    badge.textContent = s.status.replace(/_/g, " ").toLowerCase().replace(/^./, function (c) { return c.toUpperCase(); });
    badge.setAttribute("data-status", s.status);
    $("op-restrictions").textContent = s.restrictions || "";
  }
  // Signed out (a 401 from the re-read, issue 37): stop reloading and reconnecting, keep any
  // Unconfirmed entry's Retry, and say so; the page never requests /auth/login/ by itself.
  function signedOut() {
    if (signedOutNow) { return; }
    signedOutNow = true; stopped = true; clearTimeout(reloadTimer);
    if (!loadedOnce) { clearLoading("The checklist could not be loaded because you are signed out."); }
    if (socket) { socket.retired = true; if (socket.close) { socket.close(); } }
    liveBanner.hidden = false;
    $("live-detail").textContent = "You are signed out, so updates have stopped. Sign in again and reload. " +
      "An item marked Unconfirmed may have been recorded: use its Retry button, not a new tap.";
  }
  // The server's group progress (cya/attention/checklist.py _group_progress), recounted here after
  // an item read patches rows (issue #278 B). Only for the whole, unfiltered Normal list, which is
  // exactly the set of rows the server counts. str(None) is "None" in the server's labels.
  function py(value) { return value === null || value === undefined ? "None" : String(value); }
  var STATE_COUNT = {SATISFIED: "checked", DUE: "due", CURRENT: "due_soon", OVERDUE: "overdue",
    UPCOMING: "not_due", SUSPENDED: "exceptions", EXEMPT: "exceptions", WAIVED: "exceptions"};
  function groupsOf(list) {
    var groups = [], byKey = {}, ids = {};
    list.forEach(function (row) {
      var k, label;
      if (row.not_placed) { k = "not-placed"; label = "Not placed"; }
      else if (row.compartment_id) { k = "compartment:" + row.compartment_id; label = row.compartment_label; }
      else if (row.system_id) { k = "system:" + row.system_id; label = "Vehicle system: " + py(row.system_label); }
      else if (row.kit_id) { k = "kit:" + row.kit_id; label = "Kit: " + py(row.kit_label); }
      else { k = "vehicle"; label = "Vehicle-wide checks"; }
      var group = byKey[k];
      if (!group) {
        group = byKey[k] = {key: k, label: label || "Vehicle-wide checks", total: 0, checked: 0, due: 0,
          due_soon: 0, overdue: 0, not_due: 0, no_obligation: 0, exceptions: 0, open_deficiencies: 0};
        ids[k] = {};
        groups.push(group);
      }
      group.total += 1;
      if (row.obligation_state === null || row.obligation_state === undefined) { group.no_obligation += 1; }
      else if (STATE_COUNT[row.obligation_state]) { group[STATE_COUNT[row.obligation_state]] += 1; }
      (row.deficiencies || []).forEach(function (d) {
        if (d.id && !ids[k][d.id]) { ids[k][d.id] = true; group.open_deficiencies += 1; }
      });
    });
    return groups;
  }
  // Replace the named items' rows in place. False when the answer is not the same set of rows
  // in the same order (an item gained, lost or moved a row): only a whole read can place those.
  function patchRows(itemIds, fresh) {
    var named = {};
    itemIds.forEach(function (id) { named[id] = true; });
    var old = rows.filter(function (r) { return named[r.item_id]; });
    if (old.length !== fresh.length) { return false; }
    for (var i = 0; i < fresh.length; i += 1) {
      if (!named[fresh[i].item_id] || key(fresh[i]) !== key(old[i])) { return false; }
    }
    var next = 0;
    rows = rows.map(function (r) { return named[r.item_id] ? fresh[next++] : r; });
    groupProgressRows = groupsOf(rows);
    return true;
  }
  function currentListView() {
    return config.verification_for_deficiency_id || layoutMode === "FLAT" ? view : "NORMAL";
  }
  function reload() {
    if (signedOutNow) { return; }
    if (loading) { reloadAgain = true; return; }
    loading = true;
    hintWaiting = false;   // this read starts after every hint received so far, so it covers them
    var listView = currentListView();
    // Issue #278 B: only the named items' rows when nothing else asked for the whole list, and
    // only where this page holds every row the server counts (Normal order, no filter).
    var itemIds = Object.keys(pendingItems);
    var partial = !needFull && itemIds.length > 0 && itemIds.length <= MAX_ITEM_READ && loadedOnce &&
      listView === "NORMAL" && !onlyAttention && !config.verification_for_deficiency_id;
    needFull = false; pendingItems = {};
    var q = "?view=" + encodeURIComponent(listView) + "&needs_attention_only=" + (onlyAttention ? "true" : "false");
    if (partial) { q += "&item_ids=" + itemIds.map(encodeURIComponent).join(","); }
    // A session reads its own pinned version's rows (SPEC CHK-4), even after a newer version is
    // published, so an item another checklist also lists is shown here too, with the vehicle's
    // shared state (SPEC 6.0.0 VW-5, issue #256).
    if (config.session_id && !config.verification_for_deficiency_id) {
      q += "&session_id=" + encodeURIComponent(config.session_id);
    } else if (paper) {
      // The version printed on the sheet, as a paper check of the batch resolves it (LATE-4).
      q += "&paper_entry_batch_id=" + encodeURIComponent(paper.batch_id);
    } else if (config.checklist_version_id && !config.verification_for_deficiency_id) {
      q += "&checklist_version_id=" + encodeURIComponent(config.checklist_version_id);
    }
    fetch(config.urls.checklist + q, {credentials: "same-origin", cache: "no-store"}).then(function (r) {
      if (r.status === 401) { signedOut(); return null; }
      return r.json().then(function (b) { return {ok: r.ok, body: b}; });
    }).then(function (a) {
      if (a === null) { return; }
      if (!a.ok || a.body.status !== "OK") { throw new Error("checklist"); }
      var readAt = Date.parse(a.body.as_of);
      if (partial) {
        if (!Array.isArray(a.body.items)) { throw new Error("checklist"); }
        setStatus(a.body.vehicle_status);
        if (patchRows(itemIds, a.body.items)) {
          itemIds.forEach(function (id) { rowReadAt[id] = readAt; });
          reapplyPushed(readAt); $("stale").hidden = true; render();
        }
        else { needFull = true; reloadAgain = true; }   // the rows changed shape: read them all
        return;
      }
      if (!Array.isArray(a.body.group_progress)) { throw new Error("checklist groups"); }
      rows = a.body.items; groupProgressRows = a.body.group_progress; setStatus(a.body.vehicle_status);
      listReadAt = readAt; rowReadAt = {}; reapplyPushed(readAt);
      if (config.verification_for_deficiency_id) {
        // An acknowledgement may be lost after the row was satisfied or removed. Keep its
        // exact request reachable until Retry resolves, even after automatic closure.
        Object.keys(verificationRows).forEach(function (k) {
          if (entries[k] && ["pending", "unconfirmed"].indexOf(entries[k].status) !== -1 &&
              !rows.some(function (r) { return key(r) === k; })) { rows.push(verificationRows[k]); }
        });
        $("session-message").textContent = verificationNotice + " Current report: " + a.body.verification_status + ". Verification items still needing a check: " + a.body.remaining_scope_rows + "." +
          (a.body.unavailable_scope_rows ? " Some items are unavailable on the current checklist. Contact a supervisor; they remain outstanding." : "");
      }
      $("stale").hidden = true; loadedOnce = true; clearLoading(); render();
    }).catch(function () {
      $("stale").hidden = false;   // data on screen may be out of date
      // A failed read covered nothing: what it was asked for stays owed to the next read.
      if (partial) { itemIds.forEach(function (id) { pendingItems[id] = true; }); } else { needFull = true; }
      if (!loadedOnce) {
        clearLoading("The checklist could not be loaded.");
        notice.textContent = "The checklist could not be loaded yet. It tries again when the connection returns, or refresh the page.";
        notice.hidden = false;
      }
    }).then(function () {
      loading = false;
      if (reloadAgain && !signedOutNow) { reloadAgain = false; scheduleRead(0); }
    });
  }
  // scheduleRead runs whatever read is owed; scheduleReload owes the whole list (reconnect,
  // a view change, a refused save, a hint that names no item); scheduleItemRead owes one item.
  function scheduleRead(ms) {
    if (signedOutNow) { return; }
    clearTimeout(reloadTimer); reloadTimer = setTimeout(reload, ms);
  }
  function scheduleReload(ms) { needFull = true; scheduleRead(ms); }
  function scheduleItemRead(itemId, ms) { pendingItems[itemId] = true; scheduleRead(ms); }

  // A push is only a hint: any checklist read that starts after it arrives shows its change
  // (issue #278 A). So a hint is skipped when it is this page's own committed save, whose
  // reload is already scheduled, or when it arrives while a save of ours is still unanswered:
  // that save's reload starts later and covers it. If the save is not committed, or its
  // answer is slow, the hint gets its own read: SPEC §17.3 allows a client to delay a re-read
  // by up to 2 seconds.
  var HINT_WAIT_MS = 2000, hintTimer = null;
  // SPEC 8.3.0 §17.3 (issue #278): after a check commits, the server pushes that item's rows,
  // read for this session, before the hint. They replace the re-read only where this page would
  // make a one-row read (Normal order, no filter, rows loaded), and only when they were read
  // later than the rows shown; the hint they cover then needs no read. Pushed rows are kept
  // briefly so a read that started before them, and answers after, cannot show older rows.
  var OWN_ROWS_WAIT_MS = 1000, listReadAt = 0, rowReadAt = {}, pushedRows = {}, covered = {}, coveredOrder = [];
  var ownWaits = {};
  function shownAt(itemId) { return rowReadAt[itemId] || listReadAt; }
  function rowsApply() {
    return loadedOnce && config.session_id && !config.verification_for_deficiency_id &&
      currentListView() === "NORMAL" && !onlyAttention;
  }
  function cover(eventId) {
    covered[eventId] = true; coveredOrder.push(eventId);
    while (coveredOrder.length > OWN_EVENT_LIMIT) { delete covered[coveredOrder.shift()]; }
  }
  function onRows(frame) {
    if (frame.session_id !== config.session_id || !Array.isArray(frame.items)) { return; }
    var at = Date.parse(frame.as_of);
    var waiting = ownWaits[frame.event_id];
    if (waiting) { clearTimeout(waiting.timer); delete ownWaits[frame.event_id]; }
    // Applied only when read strictly later than the rows shown (Date.parse keeps milliseconds,
    // so reads within one millisecond tie and are not applied), in a view that would make a
    // one-row read, with the same row shape. Anything else covers nothing: the hint that follows
    // reads as before, and a saver waiting for these rows reads now (SPEC 8.3.0: "otherwise it
    // re-reads them").
    if (!(at > 0) || !rowsApply() || !(at > shownAt(frame.item_id)) ||
        !patchRows([frame.item_id], frame.items)) {
      if (waiting) { scheduleItemRead(waiting.itemId, 0); }
      return;
    }
    rowReadAt[frame.item_id] = at;
    pushedRows[frame.item_id] = {at: at, items: frame.items};
    $("stale").hidden = true; render();
    cover(frame.event_id);
  }
  function reapplyPushed(readAt) {
    Object.keys(pushedRows).forEach(function (itemId) {
      var pushed = pushedRows[itemId];
      if (pushed.at > readAt && pushed.at > shownAt(itemId) && patchRows([itemId], pushed.items)) {
        rowReadAt[itemId] = pushed.at;
      }
      if (pushed.at <= readAt) { delete pushedRows[itemId]; }
    });
  }
  // The saver's own check: its pushed rows usually arrive with or before the answer. Wait briefly
  // for them; without them, read the item as before.
  function awaitOwnRows(checkId, itemId) {
    if (checkId && covered[checkId]) { return; }
    if (!checkId || !rowsApply() || !socketOpen) { scheduleItemRead(itemId, 0); return; }
    ownWaits[checkId] = {itemId: itemId, timer: setTimeout(function () {
      delete ownWaits[checkId]; scheduleItemRead(itemId, 0);
    }, OWN_ROWS_WAIT_MS)};
  }
  function onHint(message) {
    var hint = null;
    try { hint = JSON.parse(message.data); } catch (error) { hint = null; }
    if (hint && hint.type === "connection.ready") { return; }   // onopen already reconciles
    if (hint && hint.type === "checklist.rows") { onRows(hint); return; }
    if (hint && hint.type === "vehicle.update" && covered[hint.event_id]) {
      delete covered[hint.event_id];   // its rows are already shown
      if (ownEvents[hint.event_id]) { delete ownEvents[hint.event_id]; }
      return;
    }
    if (hint && hint.type === "vehicle.update" && ownEvents[hint.event_id]) {
      delete ownEvents[hint.event_id];
      return;
    }
    // Issue #278 B: a check hint that names its item owes only that item's rows; anything
    // else (another kind, no item, an unreadable frame) owes the whole list.
    if (hint && hint.type === "vehicle.update" && hint.kind === "check.recorded" &&
        typeof hint.item_id === "string" && hint.item_id) {
      pendingItems[hint.item_id] = true;
    } else {
      needFull = true;
    }
    hintWaiting = true;
    if (!savesInFlight) { scheduleRead(250); return; }
    if (!hintTimer) {
      hintTimer = setTimeout(function () { hintTimer = null; if (hintWaiting) { scheduleRead(0); } }, HINT_WAIT_MS);
    }
  }

  var socket = null, backoff = 1000, stopped = false, socketOpen = false;
  function connect() {
    if (stopped) { return; }
    var scheme = location.protocol === "https:" ? "wss://" : "ws://";
    // SPEC 8.3.0: a session page names its session, so a participant also gets its pushed rows.
    var sessionPart = config.session_id && !config.verification_for_deficiency_id ?
      "?session=" + encodeURIComponent(config.session_id) : "";
    socket = new WebSocket(scheme + location.host + config.urls.socket + sessionPart);
    socket.onopen = function () { backoff = 1000; socketOpen = true; liveBanner.hidden = true; scheduleReload(0); };  // reconcile (17.3)
    socket.onmessage = onHint;
    socket.onclose = function (event) {
      if (socket && socket.retired) { return; }
      socketOpen = false;
      // Rows can no longer arrive: a save still waiting for its own reads its item now.
      Object.keys(ownWaits).forEach(function (id) {
        clearTimeout(ownWaits[id].timer); scheduleItemRead(ownWaits[id].itemId, 0); delete ownWaits[id];
      });
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
    if (config.verification_for_deficiency_id || paper) { return; }
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
  if (!config.verification_for_deficiency_id && !paper) {
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
        if (paper) { payload.paper_entry_batch_id = paper.batch_id; }
        if (confirm && $("odo-why").value.trim()) { payload.explanation = $("odo-why").value.trim(); }
        odo.request = payload;
      }
      odoStatus("pending", "Saving…");
      odo.sending = true; odoControls(true);
      post(config.urls.odometer, odo.request).then(function (a) {
        odo.sending = false; odoControls(false);
        if (a.kind === "committed") {
          odoStatus("saved", paper ? "Saved as performed " +
            when(a.body.result.paper_entry && a.body.result.paper_entry.performed_at || paper.performed_at) +
            "; entered " + when(a.body.result.server_recorded_at) :
            "Recorded " + when(a.body.result.server_recorded_at));
          paperRecorded();
          toast("Readings saved.", {announce: false});
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

  if (wideQuery) {
    var onWide = function () { if (loadedOnce) { render(); } };
    if (wideQuery.addEventListener) { wideQuery.addEventListener("change", onWide); }
    else if (wideQuery.addListener) { wideQuery.addListener(onWide); }
  }

  // ---- paper entry (SPEC 8.0.0 REC-5, LATE-8) ----------------------------------------------
  // After the first acknowledged record of the batch, offer once to print a fresh outage sheet
  // and kit reference. The offer is not a dialog and takes no focus; it is announced once.
  function paperRecorded() {
    if (!paper || paperOffered) { return; }
    paperOffered = true;
    if ($("paper-print-offer")) { $("paper-print-offer").hidden = false; }
    if ($("paper-print-status")) {
      $("paper-print-status").textContent = "First record saved. You can print a fresh outage sheet and kit reference for the rig.";
    }
  }
  if (paper) {
    if ($("paper-performers")) { $("paper-performers").textContent = (paper.performers || []).join(", "); }
    if ($("paper-print-dismiss")) {
      $("paper-print-dismiss").addEventListener("click", function () {
        $("paper-print-offer").hidden = true;
        // The dismissed button is gone: focus returns to the banner, not the top of the page.
        if ($("paper-banner-heading")) { $("paper-banner-heading").focus({preventScroll: true}); }
      });
    }
  }

  // ---- start ------------------------------------------------------------------------------
  syncControls();
  scheduleReload(0);
  connect();
  document.addEventListener("visibilitychange", function () { if (!document.hidden) { scheduleReload(0); } });
})();
