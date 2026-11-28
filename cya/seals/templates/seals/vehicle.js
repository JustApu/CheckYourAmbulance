(function () {
  "use strict";
  var config = JSON.parse(document.getElementById("seal-config").textContent);
  var csrf = document.querySelector("#seal-csrf [name=csrfmiddlewaretoken]").value;
  var list = document.getElementById("seal-subjects"), work = document.getElementById("seal-work");
  var formArea = document.getElementById("seal-form"), message = document.getElementById("seal-message");
  var heading = document.getElementById("seal-work-heading"), pending = null, subjectsCache = [], loadSequence = 0;
  var errorBox = document.getElementById("seal-error"), invalidInputs = [];
  // Paper entry (SPEC 8.5.0 LATE-7; issue #9 B6c): each verification, break or reseal copied from
  // the outage sheet names the batch and is judged at its performance time. There is no audit
  // request, no live inspection start and no nested kit verification in this mode.
  var paper = config.paper_entry || null, paperOffered = !!(paper && paper.has_records);
  var paperOpen = !!(paper && paper.state === "OPEN");
  var MESSAGES = {
    SEAL_CHANGED: "The seal changed. Refresh and try again.",
    FORBIDDEN: "You are not permitted to perform this action.",
    AUDIT_REQUIRES_SEAL: "The open seal already needs a full inspection before resealing."
  };
  var PAPER_MESSAGES = {
    SEAL_NOT_INTACT_AT_PERFORMANCE: "There was no intact seal on this at the sheet's time, so it cannot be copied as intact. Check the sheet and the order of its seal records.",
    SEAL_LATER_EVIDENCE: "The app already has a later seal record for this, so this record from the sheet cannot be placed before it. Keep it on paper.",
    PAPER_ITEM_UNIDENTIFIABLE: "An item in this reseal section cannot be matched to the checklist or kit versions printed on the sheet, so the reseal is not saved. The seal stays open until a live full inspection and reseal.",
    KIT_MOVED_SINCE_PAPER: "This kit was not on this vehicle at the sheet's time, or has moved since, so it cannot be copied from the sheet.",
    PAPER_BATCH_INVALIDATED: "This paper entry was invalidated. Start a new paper entry from the vehicle page.",
    PAPER_BATCH_MISMATCH: "This paper entry is for another vehicle. Reload the page.",
    PAPER_VERSION_MISMATCH: "This count is not on the checklist version printed on the sheet. Reload the page.",
    PRINTED_KIT_VERSION_MISSING: "No kit version was named for this kit, so its contents cannot be copied. Start a new paper entry naming the kit reference used.",
    ITEM_NOT_IN_CHECKLIST: "This count is not on the checklist version printed on the sheet.",
    CHECKLIST_UNAVAILABLE: "The printed checklist cannot be read right now. Try again later.",
    NOT_FOUND: "This paper entry or seal is no longer available. Reload the page.",
    OPERATION_REUSED: "This request changed after it was sent. Enter it again.",
    INVALID_SEAL_REQUEST: "The record was not saved. Check the seal and what you copied from the sheet, then try again."
  };

  function element(tag, text) {
    var node = document.createElement(tag);
    if (text !== undefined) { node.textContent = text; }
    return node;
  }
  function show(text) { message.textContent = text; }
  // A refusal or a missing entry: announced in the alert summary, with the inputs to fix marked.
  function clearErrors() {
    invalidInputs.forEach(function (input) { input.removeAttribute("aria-invalid"); });
    invalidInputs = [];
    if (errorBox) { errorBox.textContent = ""; errorBox.hidden = true; }
  }
  function fail(text, inputs) {
    clearErrors();
    (inputs || []).forEach(function (input) { input.setAttribute("aria-invalid", "true"); invalidInputs.push(input); });
    if (!errorBox) { show(text); return; }
    show(""); errorBox.textContent = text; errorBox.hidden = false;
  }
  function refusal(code) {
    if (paper && PAPER_MESSAGES[code]) { return PAPER_MESSAGES[code]; }
    if (MESSAGES[code]) { return MESSAGES[code]; }
    return paper ? "The record was not saved (" + code + "). Check the seal and what you copied from the sheet, then try again." :
      "The action was not recorded. Check the seal and the information entered, then try again.";
  }
  // After the first acknowledged record of the batch, offer once to print a fresh outage sheet
  // and kit reference (LATE-4, LATE-8). The offer is not a dialog and takes no focus.
  function paperRecorded() {
    if (!paper || paperOffered) { return; }
    paperOffered = true;
    var offer = document.getElementById("paper-print-offer"), status = document.getElementById("paper-print-status");
    if (offer) { offer.hidden = false; }
    if (status) { status.textContent = "First record saved. You can print a fresh outage sheet and kit reference for the rig."; }
  }
  function uuid4() {
    var b = new Uint8Array(16); crypto.getRandomValues(b);
    b[6] = (b[6] & 15) | 64; b[8] = (b[8] & 63) | 128;
    var h = Array.prototype.map.call(b, function (x) { return ("0" + x.toString(16)).slice(-2); }).join("");
    return [h.slice(0, 8), h.slice(8, 12), h.slice(12, 16), h.slice(16, 20), h.slice(20)].join("-");
  }
  function button(text, action) {
    var b = element("button", text); b.type = "button"; b.className = "button";
    b.addEventListener("click", action); return b;
  }
  function field(parent, text, type, required) {
    var label = element("label", text + " "), input = element("input");
    input.type = type; input.required = !!required; label.appendChild(input); parent.appendChild(label);
    return input;
  }
  function select(parent, labelText, choices) {
    var label = element("label", labelText + " "), input = element("select");
    choices.forEach(function (choice) {
      var option = element("option", choice.label); option.value = choice.value; input.appendChild(option);
    });
    label.appendChild(input); parent.appendChild(label); return input;
  }
  function subjectFields(subject) {
    var body = {vehicle_id: config.vehicle_id, expected_tip_id: subject.tip_id};
    body[subject.kind === "kit" ? "kit_id" : "slot_id"] = subject.id;
    if (paper) { body.paper_entry_batch_id = paper.batch_id; }
    return body;
  }
  // A paper check (a reseal's kit count or presence) names the batch and the printed version;
  // it never names a live inspection start (LATE-7).
  function checkFields(fields, checklistVersionId, startId) {
    var payload = Object.assign({vehicle_id: config.vehicle_id}, fields);
    if (paper) {
      payload.checklist_version_id = paper.checklist_version_id; payload.paper_entry_batch_id = paper.batch_id;
      return payload;
    }
    payload.checklist_version_id = checklistVersionId;
    if (!checklistVersionId && startId) { payload.inspection_start_operation_id = startId; }
    return payload;
  }
  // REC-4: the exact request and operation id are kept until the server gives a definite answer.
  function send(action, body, onSuccess, onConflict) {
    if (pending) { fail("A previous request is unconfirmed. Retry it before making another change."); return; }
    clearErrors();
    body.operation_id = uuid4(); pending = {action: action, body: body, onSuccess: onSuccess, onConflict: onConflict}; transmit();
  }
  function transmit() {
    if (!pending) { return; }
    var request = pending;
    fetch(request.action === "check" ? config.checks_url : config.command_url.replace("ACTION", request.action), {
      method: "POST", credentials: "same-origin", cache: "no-store",
      headers: {"Content-Type": "application/json", "X-CSRFToken": csrf},
      body: JSON.stringify(request.body)
    }).then(function (response) {
      return response.json().then(function (body) { return {status: response.status, body: body}; });
    }).then(function (answer) {
      if (answer.body.status === "COMMITTED") {
        pending = null; clearErrors(); removeRetry(); paperRecorded();
        if (request.onSuccess) { request.onSuccess(answer.body.result); return; }
        work.hidden = true; show(paper ? "Saved from the sheet." : "Recorded."); load(); return;
      }
      if (answer.body.status === "CONFLICT" && answer.status === 409) {
        pending = null; removeRetry();
        if (request.onConflict) { request.onConflict(answer.body); }
        else { fail("Someone else recorded a newer check. Refresh before continuing."); }
        return;
      }
      if (answer.body.status === "REJECTED" && answer.status < 500) {
        pending = null; removeRetry();
        fail(refusal(answer.body.code));
        return;
      }
      throw new Error("unconfirmed");
    }).catch(function () {
      show("Unconfirmed: the server did not confirm the action. Retry the same request before continuing.");
      if (!retryButton) { retryButton = button("Retry unconfirmed action", transmit); }
      if (retryButton.parentNode !== formArea) { formArea.appendChild(retryButton); }
      work.hidden = false;
    });
  }
  var retryButton = null;
  function removeRetry() {
    if (retryButton && retryButton.parentNode) { retryButton.parentNode.removeChild(retryButton); }
    retryButton = null;
  }
  function open(subject, title) {
    if (pending) { fail("Retry the unconfirmed action first."); return false; }
    formArea.replaceChildren(); retryButton = null; heading.textContent = title + ": " + subject.label; work.hidden = false;
    heading.focus(); show(""); clearErrors(); return true;
  }
  function verify(subject) {
    if (!open(subject, paper ? "Seal intact on the sheet" : "Verify seal")) { return; }
    var form = element("form");
    var marking = field(form, paper ? "Crew ID written on the seal, as on the sheet" : "Crew ID written on seal", "text", true);
    var intact = field(form, paper ? "The sheet records this seal as intact" : "I checked that the seal is intact", "checkbox", true);
    var submit = element("button", paper ? "Save seal intact from the sheet" : "Record seal verification");
    submit.type = "submit"; submit.className = "button primary";
    form.appendChild(submit); formArea.appendChild(form);
    form.addEventListener("submit", function (event) {
      event.preventDefault(); if (!form.reportValidity()) { return; }
      if (!marking.value.trim()) { fail("Enter the crew ID written on the seal.", [marking]); return; }
      send("verify", Object.assign(subjectFields(subject), {marking: marking.value, attested: intact.checked}));
    });
  }
  function breakSeal(subject) {
    if (!open(subject, paper ? "Seal broken on the sheet" : "Break seal")) { return; }
    var form = element("form");
    var reason = field(form, paper ? "Reason for breaking, as written on the sheet" : "Reason for breaking", "text", true);
    reason.maxLength = 4000;
    var submit = element("button", paper ? "Save seal broken from the sheet" : "Record break");
    submit.type = "submit"; submit.className = "button primary";
    form.appendChild(submit); formArea.appendChild(form);
    form.addEventListener("submit", function (event) {
      event.preventDefault(); if (!form.reportValidity()) { return; }
      if (!reason.value.trim()) { fail("Enter the reason for breaking the seal.", [reason]); return; }
      send("break", Object.assign(subjectFields(subject), {reason: reason.value}));
    });
  }
  function audit(subject) {
    if (!open(subject, "Request full audit")) { return; }
    formArea.appendChild(element("p", "The seal cannot substitute for checks until the audit is completed."));
    formArea.appendChild(button("Request audit", function () { send("audit", subjectFields(subject)); }));
  }
  function deficiencyFields(parent) {
    var details = element("details"), summary = element("summary", "Report a problem if this item failed");
    details.appendChild(summary); parent.appendChild(details);
    var categories = [{value: "", label: "Choose a category"}].concat(config.report_options.categories.map(function (c) {
      return {value: c.id, label: c.label};
    }));
    var severities = [{value: "", label: "Choose a severity"}].concat(config.report_options.severities.map(function (s) {
      return {value: s.code, label: s.label + ": " + s.description};
    }));
    return {category: select(details, "Category", categories), severity: select(details, "Severity", severities),
      summary: field(details, "Summary", "text", false), description: field(details, "Description", "text", false)};
  }
  function scopeUrl(subject) {
    var url = config.scope_url.replace("KIND", encodeURIComponent(subject.kind)).replace("SUBJECT", encodeURIComponent(subject.id));
    // A paper reseal's scope is read as it stood at the sheet's time (LATE-7).
    return paper ? url + "?paper_entry_batch_id=" + encodeURIComponent(paper.batch_id) : url;
  }
  function missing(d) {
    return [d.category, d.severity, d.summary, d.description].filter(function (input) { return !input.value.trim(); });
  }
  function inspect(subject) {
    if (!open(subject, paper ? "Resealed on the sheet" : "Inspect and reseal")) { return; }
    function scope(startId) {
      fetch(scopeUrl(subject), {credentials: "same-origin", cache: "no-store"}).then(function (response) {
        if (paper && response.status === 404) { throw new Error("absent"); }
        if (!response.ok) { throw new Error("scope unavailable"); } return response.json();
      }).then(function (data) { inspectionForm(subject, data, startId); }).catch(function (error) {
        fail(error.message === "absent" ?
          "This was not sealable at the sheet's time, or this paper entry is no longer available, so a reseal cannot be copied." :
          paper ? "The contents at the sheet's time are unavailable. Refresh and try again." :
          "The current contents are unavailable. Refresh and try again.");
      });
    }
    // Paper entry has no live inspection start: the reseal is taken as performed at the sheet's time.
    if (paper) { scope(null); return; }
    send("start", subjectFields(subject), function (started) { scope(started.start_operation_id); });
  }
  function inspectionForm(subject, data, startId) {
    var form = element("form"), fields = [], nested = {}, counts = {}, presenceChecks = {};
    data.kit_lines.forEach(function (line) {
      var group = element("fieldset"), legend = element("legend", "Count kits: " + line.label);
      group.appendChild(legend); form.appendChild(group);
      var count = field(group, (paper ? "Kit count from the sheet" : "Fresh kit count") + " (target " + line.required_count + ")", "number", false);
      count.min = "0"; count.step = "1";
      var deficiency = deficiencyFields(group), status = element("p"); group.appendChild(status);
      var record = button(paper ? "Save kit count from the sheet" : "Record kit count", function () {
        if (count.value === "" || !Number.isSafeInteger(Number(count.value)) || Number(count.value) < 0) {
          fail("Enter a fresh, nonnegative whole kit count.", [count]); return;
        }
        var quantity = Number(count.value), payload = checkFields({item_id: line.item_id,
          kit_requirement_id: line.line_id, purpose: "ROUTINE", quantity: quantity}, line.checklist_version_id, startId);
        if (quantity !== line.required_count) {
          if (!deficiency.category.value || !deficiency.severity.value || !deficiency.summary.value.trim() ||
              !deficiency.description.value.trim()) { fail("Complete the problem details for the kit count.", missing(deficiency)); return; }
          payload.deficiency = {create: {category_id: deficiency.category.value, severity: deficiency.severity.value,
            summary: deficiency.summary.value, description: deficiency.description.value}};
        }
        function committed(result) {
          counts[line.line_id] = result.check_id; count.disabled = true; record.disabled = true;
          status.textContent = "Kit count recorded.";
        }
        function conflict(result) {
          var newest = result.newest_check;
          if (!newest || !newest.check_id) { show("A newer check exists. Refresh before continuing."); return; }
          status.textContent = "Another crew member recorded a newer check. You may also record your count.";
          group.appendChild(button("Record my count too", function () {
            payload.acknowledges_check_id = newest.check_id; send("check", payload, committed, conflict);
          }));
        }
        send("check", payload, committed, conflict);
      });
      group.appendChild(record);
    });
    data.kit_presence.forEach(function (kit) {
      var group = element("fieldset"), legend = element("legend", "Confirm kit present: " + kit.kit_label);
      group.appendChild(legend); form.appendChild(group);
      var count = field(group, (paper ? "Count from the sheet" : "Fresh count") + " (target 1)", "number", false);
      count.min = "0"; count.max = "1"; count.step = "1";
      var deficiency = deficiencyFields(group), status = element("p"); group.appendChild(status);
      var record = button(paper ? "Save presence from the sheet" : "Record presence", function () {
        if (count.value !== "0" && count.value !== "1") {
          fail("Enter 1 if the assigned kit is present, or 0 if it is missing.", [count]); return;
        }
        var quantity = Number(count.value), payload = checkFields({item_id: kit.item_id, kit_id: kit.kit_id,
          kit_presence: true, purpose: "ROUTINE", quantity: quantity}, kit.checklist_version_id, null);
        if (quantity !== 1) {
          if (!deficiency.category.value || !deficiency.severity.value || !deficiency.summary.value.trim() ||
              !deficiency.description.value.trim()) { fail("Complete the problem details for the missing kit.", missing(deficiency)); return; }
          payload.deficiency = {create: {category_id: deficiency.category.value, severity: deficiency.severity.value,
            summary: deficiency.summary.value, description: deficiency.description.value}};
        }
        function committed(result) {
          presenceChecks[kit.kit_id] = result.check_id; count.disabled = true; record.disabled = true;
          status.textContent = "Kit presence recorded.";
        }
        function conflict(result) {
          var newest = result.newest_check;
          if (!newest || !newest.check_id) { show("A newer check exists. Refresh before continuing."); return; }
          status.textContent = "Another crew member recorded a newer check. You may also record your count.";
          group.appendChild(button("Record my count too", function () {
            payload.acknowledges_check_id = newest.check_id; send("check", payload, committed, conflict);
          }));
        }
        send("check", payload, committed, conflict);
      });
      group.appendChild(record);
    });
    // A paper cabinet reseal inspects every covered kit's items; it takes no nested verification.
    if (subject.kind === "slot" && !paper) {
      subjectsCache.filter(function (kit) {
        return kit.kind === "kit" && kit.slot_id === subject.id && kit.state === "SEALED";
      }).forEach(function (kit) {
        var group = element("fieldset"), label = element("legend", "Verify nested kit: " + kit.label);
        group.appendChild(label);
        var marking = field(group, "Crew ID written on the kit seal", "text", false);
        var intact = field(group, "I checked that this kit seal is intact", "checkbox", false);
        var status = element("p"); group.appendChild(status);
        var verifyButton = button("Verify this kit seal", function () {
          if (!marking.value.trim() || !intact.checked) {
            fail("Enter the kit seal ID and attest that it is intact.", [marking, intact].filter(function (input) {
              return input === marking ? !marking.value.trim() : !intact.checked;
            })); return;
          }
          send("verify", Object.assign(subjectFields(kit), {marking: marking.value, attested: true}), function (result) {
            nested[kit.id] = {id: result.substitution_id, expiration: kit.expiration};
            fields.forEach(function (row) {
              if (row.item.kit_id !== kit.id) { return; }
              row.group.hidden = true;
              [row.count, row.measurement, row.expiration, row.outcome].forEach(function (input) {
                if (input) { input.disabled = true; input.required = false; }
              });
            });
            marking.disabled = true; intact.disabled = true; verifyButton.disabled = true;
            status.textContent = "Kit seal verified."; updateExpiration();
          });
        });
        group.appendChild(verifyButton); form.appendChild(group);
      });
    }
    data.items.forEach(function (item) {
      var group = element("fieldset"), legend = element("legend", item.label + (item.kit_label ? " — " + item.kit_label : ""));
      group.appendChild(legend); var row = {item: item, group: group};
      if (item.kind === "QUANTITY") {
        row.count = field(group, (paper ? "Count from the sheet" : "Fresh count") + " (target " + item.expected_quantity + ")", "number", true);
        row.count.min = "0"; row.count.step = "1";
      }
      if (item.kind === "MEASUREMENT") {
        row.measurement = field(group, "Measurement (" + item.unit + ")", "text", true);
      }
      if (item.expiration_tracked) {
        row.expiration = field(group, paper ? "Expiration date from the sheet" : "Fresh expiration date", "date", true);
      }
      row.outcome = select(group, "Result", item.kind === "QUANTITY" ?
        [{value: "", label: "Use count"}, {value: "FAILED", label: "Failed for another reason"}] :
        [{value: "", label: "Choose result"}, {value: "PASSED", label: "Passed"}, {value: "FAILED", label: "Failed"}]);
      if (item.kind !== "QUANTITY") { row.outcome.required = true; }
      row.deficiency = deficiencyFields(group); fields.push(row); form.appendChild(group);
    });
    var expiry = element("p");
    var marking = field(form, paper ? "Crew ID written on the new seal, as on the sheet" : "Crew ID to write on seal", "text", true);
    function updateExpiration() {
      var dates = fields.filter(function (row) { return row.expiration && !row.group.hidden; });
      var values = dates.map(function (row) { return row.expiration.value; }).filter(Boolean);
      var nestedDates = Object.keys(nested).map(function (id) { return nested[id].expiration; }).filter(Boolean);
      values = values.concat(nestedDates).sort();
      if (paper) {
        expiry.textContent = dates.length === 0 ? "The new seal should say no expiration." :
          dates.some(function (row) { return !row.expiration.value; }) ? "Enter every expiration date from the sheet to work out the seal date." :
          "The earliest expiration from the sheet is " + values[0] + ". Check that the sheet shows this date written on the new seal.";
        return;
      }
      expiry.textContent = dates.length === 0 && nestedDates.length === 0 ? "Write no expiration on the seal." :
        dates.some(function (row) { return !row.expiration.value; }) ? "Enter every tracked expiration to calculate the seal date." :
        "Write this earliest expiration on the seal: " + values[0] + ".";
    }
    fields.forEach(function (row) { if (row.expiration) { row.expiration.addEventListener("change", updateExpiration); } });
    updateExpiration(); form.appendChild(expiry);
    var fresh = field(form, paper ? "The sheet records every listed item as inspected" : "I inspected every listed item", "checkbox", true);
    var written = field(form, paper ? "The sheet records the crew ID and the expiration, or no expiration, written on the new seal" :
      "I wrote the crew ID and the displayed expiration or no expiration", "checkbox", true);
    var submit = element("button", paper ? "Save reseal from the sheet" : "Record inspection and reseal");
    submit.type = "submit"; submit.className = "button primary";
    form.appendChild(submit); formArea.appendChild(form);
    form.addEventListener("submit", function (event) {
      event.preventDefault(); if (!form.reportValidity()) { return; }
      var invalid = false, incomplete = [];
      var observations = fields.filter(function (row) { return !row.group.hidden; }).map(function (row) {
        var count = row.count ? Number(row.count.value) : null;
        var failed = row.outcome.value === "FAILED" || (row.count && count !== row.item.expected_quantity);
        var value = {item_id: row.item.item_id, kit_id: row.item.kit_id, outcome: row.outcome.value || null,
          count: count, expiration: row.expiration ? row.expiration.value : null,
          measurement: row.measurement ? {value: row.measurement.value, unit: row.item.unit} : null};
        if (failed) {
          var d = row.deficiency;
          if (!d.category.value || !d.severity.value || !d.summary.value.trim() || !d.description.value.trim()) {
            invalid = true; incomplete = incomplete.concat(missing(d));
          }
          value.deficiency = {create: {category_id: d.category.value, severity: d.severity.value,
            summary: d.summary.value, description: d.description.value}};
        }
        return value;
      });
      if (invalid) { fail("Complete the problem details for each failed item.", incomplete); return; }
      if (data.kit_lines.some(function (line) { return !counts[line.line_id]; })) {
        fail(paper ? "Save the sheet's count for every kit line before saving the reseal." :
          "Record a fresh count for every kit line before resealing."); return;
      }
      if (data.kit_presence.some(function (kit) { return !presenceChecks[kit.kit_id]; })) {
        fail(paper ? "Save the sheet's presence count for every assigned physical kit before saving the reseal." :
          "Record a fresh presence check for every assigned physical kit before resealing."); return;
      }
      var body = Object.assign(subjectFields(subject), {marking: marking.value,
        inspected_attestation: fresh.checked, written_attestation: written.checked, observations: observations,
        kit_count_checks: data.kit_lines.map(function (line) { return {line_id: line.line_id, check_id: counts[line.line_id]}; }),
        kit_presence_checks: data.kit_presence.map(function (kit) { return {kit_id: kit.kit_id, check_id: presenceChecks[kit.kit_id]}; })});
      // A paper reseal has no live inspection start and no nested seal verification (LATE-7).
      if (!paper) {
        body.start_operation_id = startId;
        body.nested_substitution_ids = Object.keys(nested).map(function (id) { return nested[id].id; });
      }
      send("reseal", body);
    });
  }
  // What the sheet can say about a seal. The list shows each seal as it stands now; the server
  // judges each record at the sheet's time and refuses one it cannot place (LATE-7). An audit
  // request is never copied from paper.
  function paperActions(box, subject) {
    if (!paperOpen) { return; }
    box.appendChild(button("Seal intact on the sheet", function () { verify(subject); }));
    if (subject.state !== "OPEN") {
      box.appendChild(button("Seal broken on the sheet", function () { breakSeal(subject); }));
      box.appendChild(element("p", "If the sheet shows this seal broken and resealed, save the break first, then the reseal."));
    } else {
      box.appendChild(button("Resealed on the sheet", function () { inspect(subject); }));
    }
  }
  function load() {
    var sequence = ++loadSequence;
    fetch(config.subjects_url, {credentials: "same-origin", cache: "no-store"}).then(function (response) {
      if (!response.ok) { throw new Error("unavailable"); } return response.json();
    }).then(function (data) {
      if (sequence !== loadSequence) { return; }
      subjectsCache = data.subjects;
      list.replaceChildren();
      if (!data.subjects.length) { list.appendChild(element("p", "No kits or cabinets on this vehicle are sealed. An administrator chooses which ones are sealed.")); }
      data.subjects.forEach(function (subject) {
        var box = element("article"), h = element("h3", subject.label), info = element("p");
        info.textContent = (subject.kind === "kit" ? "Kit" : "Cabinet") + " · " + subject.state.replace("_", " ").toLowerCase() +
          (subject.no_expiration ? " · no expiration" : subject.expiration ? " · expires " + subject.expiration : "");
        box.appendChild(h); box.appendChild(info);
        if (paper) { list.appendChild(box); paperActions(box, subject); return; }
        if (subject.state === "SEALED") { box.appendChild(button("Verify seal", function () { verify(subject); })); }
        if (subject.state !== "OPEN") { box.appendChild(button("Break seal", function () { breakSeal(subject); })); }
        if (subject.state === "OPEN") { box.appendChild(button("Inspect and reseal", function () { inspect(subject); })); }
        if (config.can_request_audit && !subject.audit_pending && subject.state !== "OPEN") {
          box.appendChild(button("Request audit", function () { audit(subject); }));
        }
        if (config.can_request_audit && subject.state === "OPEN" && !subject.audit_pending) {
          box.appendChild(element("p", "This open seal needs a full inspection before resealing."));
        }
        list.appendChild(box);
      });
    }).catch(function () {
      if (sequence !== loadSequence) { return; }
      // Never leave the loading placeholders in place of an answer (issue #194).
      if (list.querySelector(".skeleton")) { list.replaceChildren(); }
      show("Seal status is unavailable. Refresh and try again.");
    });
  }
  document.getElementById("seal-cancel").addEventListener("click", function () {
    if (pending) { fail("Retry the unconfirmed action before continuing."); return; }
    work.hidden = true; clearErrors();
  });
  if (paper) {
    var performers = document.getElementById("paper-performers"), dismiss = document.getElementById("paper-print-dismiss");
    if (performers) { performers.textContent = (paper.performers || []).join(", "); }
    if (dismiss) {
      dismiss.addEventListener("click", function () {
        document.getElementById("paper-print-offer").hidden = true;
        // The dismissed button is gone: focus returns to the banner, not the top of the page.
        var bannerHeading = document.getElementById("paper-banner-heading");
        if (bannerHeading) { bannerHeading.focus({preventScroll: true}); }
      });
    }
  }
  var socketBackoff = 1000, socketStopped = false, refreshTimer;
  function scheduleRefresh() {
    clearTimeout(refreshTimer); refreshTimer = setTimeout(load, 250);
  }
  function connect() {
    if (socketStopped) { return; }
    var scheme = location.protocol === "https:" ? "wss://" : "ws://";
    var socket = new WebSocket(scheme + location.host + config.socket_url);
    socket.onopen = function () {
      socketBackoff = 1000;
      if (message.textContent === "Live updates are unavailable; reconnecting.") { show(""); }
      scheduleRefresh();
    };
    socket.onmessage = scheduleRefresh;
    socket.onclose = function (event) {
      if (event.code === 4401 || event.code === 4403) {
        socketStopped = true;
        show("Your sign-in or access changed. Sign in again and reload to see updates."); return;
      }
      if (!pending) { show("Live updates are unavailable; reconnecting."); }
      setTimeout(connect, socketBackoff); socketBackoff = Math.min(socketBackoff * 2, 30000);
    };
  }
  document.addEventListener("visibilitychange", function () { if (!document.hidden) { scheduleRefresh(); } });
  load();
  connect();
}());
