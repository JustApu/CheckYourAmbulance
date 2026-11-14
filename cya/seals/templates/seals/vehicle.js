(function () {
  "use strict";
  var config = JSON.parse(document.getElementById("seal-config").textContent);
  var csrf = document.querySelector("#seal-csrf [name=csrfmiddlewaretoken]").value;
  var list = document.getElementById("seal-subjects"), work = document.getElementById("seal-work");
  var formArea = document.getElementById("seal-form"), message = document.getElementById("seal-message");
  var heading = document.getElementById("seal-work-heading"), pending = null, subjectsCache = [];

  function element(tag, text) {
    var node = document.createElement(tag);
    if (text !== undefined) { node.textContent = text; }
    return node;
  }
  function show(text) { message.textContent = text; }
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
    return body;
  }
  function send(action, body, onSuccess, onConflict) {
    if (pending) { show("A previous request is unconfirmed. Retry it before making another change."); return; }
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
        pending = null;
        if (request.onSuccess) { request.onSuccess(answer.body.result); return; }
        work.hidden = true; show("Recorded."); load(); return;
      }
      if (answer.body.status === "CONFLICT" && answer.status === 409) {
        pending = null;
        if (request.onConflict) { request.onConflict(answer.body); }
        else { show("Someone else recorded a newer check. Refresh before continuing."); }
        return;
      }
      if (answer.body.status === "REJECTED" && answer.status < 500) {
        pending = null;
        show(answer.body.code === "SEAL_CHANGED" ? "The seal changed. Refresh and try again." :
          answer.body.code === "FORBIDDEN" ? "You are not permitted to perform this action." :
          "The action was not recorded. Check the seal and the information entered, then try again.");
        return;
      }
      throw new Error("unconfirmed");
    }).catch(function () {
      show("Unconfirmed: the server did not confirm the action. Retry the same request before continuing.");
      var retry = button("Retry unconfirmed action", transmit); formArea.appendChild(retry); work.hidden = false;
    });
  }
  function open(subject, title) {
    if (pending) { show("Retry the unconfirmed action first."); return false; }
    formArea.replaceChildren(); heading.textContent = title + ": " + subject.label; work.hidden = false;
    heading.focus(); show(""); return true;
  }
  function verify(subject) {
    if (!open(subject, "Verify seal")) { return; }
    var form = element("form"), marking = field(form, "Crew ID written on seal", "text", true);
    var intact = field(form, "I checked that the seal is intact", "checkbox", true);
    var submit = element("button", "Record seal verification"); submit.type = "submit"; submit.className = "button primary";
    form.appendChild(submit); formArea.appendChild(form);
    form.addEventListener("submit", function (event) {
      event.preventDefault(); if (!form.reportValidity()) { return; }
      send("verify", Object.assign(subjectFields(subject), {marking: marking.value, attested: intact.checked}));
    });
  }
  function breakSeal(subject) {
    if (!open(subject, "Break seal")) { return; }
    var form = element("form"), reason = field(form, "Reason for breaking", "text", true);
    reason.maxLength = 4000;
    var submit = element("button", "Record break"); submit.type = "submit"; submit.className = "button primary";
    form.appendChild(submit); formArea.appendChild(form);
    form.addEventListener("submit", function (event) {
      event.preventDefault(); if (!form.reportValidity()) { return; }
      send("break", Object.assign(subjectFields(subject), {reason: reason.value}));
    });
  }
  function audit(subject) {
    if (!open(subject, "Request full audit")) { return; }
    formArea.appendChild(element("p", "The seal cannot substitute for checks until the audit is completed."));
    formArea.appendChild(button("Request audit", function () { send("audit", subjectFields(subject)); }));
  }
  function deficiencyFields(parent) {
    var details = element("details"), summary = element("summary", "Report a deficiency if this item failed");
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
  function inspect(subject) {
    if (!open(subject, "Inspect and reseal")) { return; }
    send("start", subjectFields(subject), function (started) {
      var url = config.scope_url.replace("KIND", encodeURIComponent(subject.kind)).replace("SUBJECT", encodeURIComponent(subject.id));
      fetch(url, {credentials: "same-origin", cache: "no-store"}).then(function (response) {
        if (!response.ok) { throw new Error("scope unavailable"); } return response.json();
      }).then(function (data) { inspectionForm(subject, data, started.start_operation_id); }).catch(function () {
        show("The current contents are unavailable. Refresh and try again.");
      });
    });
  }
  function inspectionForm(subject, data, startId) {
    var form = element("form"), fields = [], nested = {}, counts = {};
    data.kit_lines.forEach(function (line) {
      var group = element("fieldset"), legend = element("legend", "Count kits: " + line.label);
      group.appendChild(legend); form.appendChild(group);
      if (!line.checklist_version_id) {
        group.appendChild(element("p", "A checklist for this kit type is not available. Ask a supervisor for help."));
        return;
      }
      var count = field(group, "Fresh kit count (target " + line.required_count + ")", "number", false);
      count.min = "0"; count.step = "1";
      var deficiency = deficiencyFields(group), status = element("p"); group.appendChild(status);
      var record = button("Record kit count", function () {
        if (count.value === "" || !Number.isSafeInteger(Number(count.value)) || Number(count.value) < 0) {
          show("Enter a fresh, nonnegative whole kit count."); return;
        }
        var quantity = Number(count.value), payload = {vehicle_id: config.vehicle_id, item_id: line.item_id,
          checklist_version_id: line.checklist_version_id, kit_requirement_id: line.line_id,
          purpose: "ROUTINE", quantity: quantity};
        if (quantity !== line.required_count) {
          if (!deficiency.category.value || !deficiency.severity.value || !deficiency.summary.value.trim() ||
              !deficiency.description.value.trim()) { show("Complete the deficiency details for the kit count."); return; }
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
    if (subject.kind === "slot") {
      subjectsCache.filter(function (kit) {
        return kit.kind === "kit" && kit.slot_id === subject.id && kit.state === "SEALED";
      }).forEach(function (kit) {
        var group = element("fieldset"), label = element("legend", "Verify nested kit: " + kit.label);
        group.appendChild(label);
        var marking = field(group, "Crew ID written on the kit seal", "text", false);
        var intact = field(group, "I checked that this kit seal is intact", "checkbox", false);
        var status = element("p"); group.appendChild(status);
        var verifyButton = button("Verify this kit seal", function () {
          if (!marking.value.trim() || !intact.checked) { show("Enter the kit seal ID and attest that it is intact."); return; }
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
        row.count = field(group, "Fresh count (target " + item.expected_quantity + ")", "number", true);
        row.count.min = "0"; row.count.step = "1";
      }
      if (item.kind === "MEASUREMENT") {
        row.measurement = field(group, "Measurement (" + item.unit + ")", "text", true);
      }
      if (item.expiration_tracked) { row.expiration = field(group, "Fresh expiration date", "date", true); }
      row.outcome = select(group, "Result", item.kind === "QUANTITY" ?
        [{value: "", label: "Use count"}, {value: "FAILED", label: "Failed for another reason"}] :
        [{value: "", label: "Choose result"}, {value: "PASSED", label: "Passed"}, {value: "FAILED", label: "Failed"}]);
      if (item.kind !== "QUANTITY") { row.outcome.required = true; }
      row.deficiency = deficiencyFields(group); fields.push(row); form.appendChild(group);
    });
    var expiry = element("p"), marking = field(form, "Crew ID to write on seal", "text", true);
    function updateExpiration() {
      var dates = fields.filter(function (row) { return row.expiration && !row.group.hidden; });
      var values = dates.map(function (row) { return row.expiration.value; }).filter(Boolean);
      var nestedDates = Object.keys(nested).map(function (id) { return nested[id].expiration; }).filter(Boolean);
      values = values.concat(nestedDates).sort();
      expiry.textContent = dates.length === 0 && nestedDates.length === 0 ? "Write no expiration on the seal." :
        dates.some(function (row) { return !row.expiration.value; }) ? "Enter every tracked expiration to calculate the seal date." :
        "Write this earliest expiration on the seal: " + values[0] + ".";
    }
    fields.forEach(function (row) { if (row.expiration) { row.expiration.addEventListener("change", updateExpiration); } });
    updateExpiration(); form.appendChild(expiry);
    var fresh = field(form, "I inspected every listed item", "checkbox", true);
    var written = field(form, "I wrote the crew ID and the displayed expiration or no expiration", "checkbox", true);
    var submit = element("button", "Record inspection and reseal"); submit.type = "submit"; submit.className = "button primary";
    form.appendChild(submit); formArea.appendChild(form);
    form.addEventListener("submit", function (event) {
      event.preventDefault(); if (!form.reportValidity()) { return; }
      var invalid = false;
      var observations = fields.filter(function (row) { return !row.group.hidden; }).map(function (row) {
        var count = row.count ? Number(row.count.value) : null;
        var failed = row.outcome.value === "FAILED" || (row.count && count !== row.item.expected_quantity);
        var value = {item_id: row.item.item_id, kit_id: row.item.kit_id, outcome: row.outcome.value || null,
          count: count, expiration: row.expiration ? row.expiration.value : null,
          measurement: row.measurement ? {value: row.measurement.value, unit: row.item.unit} : null};
        if (failed) {
          var d = row.deficiency;
          if (!d.category.value || !d.severity.value || !d.summary.value.trim() || !d.description.value.trim()) { invalid = true; }
          value.deficiency = {create: {category_id: d.category.value, severity: d.severity.value,
            summary: d.summary.value, description: d.description.value}};
        }
        return value;
      });
      if (invalid) { show("Complete the deficiency details for each failed item."); return; }
      if (data.kit_lines.some(function (line) { return !counts[line.line_id]; })) {
        show("Record a fresh count for every kit line before resealing."); return;
      }
      send("reseal", Object.assign(subjectFields(subject), {marking: marking.value,
        inspected_attestation: fresh.checked, written_attestation: written.checked, observations: observations,
        start_operation_id: startId, nested_substitution_ids: Object.keys(nested).map(function (id) { return nested[id].id; }),
        kit_count_checks: data.kit_lines.map(function (line) { return {line_id: line.line_id, check_id: counts[line.line_id]}; })}));
    });
  }
  function load() {
    fetch(config.subjects_url, {credentials: "same-origin", cache: "no-store"}).then(function (response) {
      if (!response.ok) { throw new Error("unavailable"); } return response.json();
    }).then(function (data) {
      subjectsCache = data.subjects;
      list.replaceChildren();
      if (!data.subjects.length) { list.appendChild(element("p", "No sealable kits or cabinets are configured.")); }
      data.subjects.forEach(function (subject) {
        var box = element("article"), h = element("h3", subject.label), info = element("p");
        info.textContent = (subject.kind === "kit" ? "Kit" : "Cabinet") + " · " + subject.state.replace("_", " ").toLowerCase() +
          (subject.no_expiration ? " · no expiration" : subject.expiration ? " · expires " + subject.expiration : "");
        box.appendChild(h); box.appendChild(info);
        if (subject.state === "SEALED") { box.appendChild(button("Verify seal", function () { verify(subject); })); }
        if (subject.state !== "OPEN") { box.appendChild(button("Break seal", function () { breakSeal(subject); })); }
        if (subject.state === "OPEN") { box.appendChild(button("Inspect and reseal", function () { inspect(subject); })); }
        if (config.can_request_audit && !subject.audit_pending) { box.appendChild(button("Request audit", function () { audit(subject); })); }
        list.appendChild(box);
      });
    }).catch(function () { show("Seal status is unavailable. Refresh and try again."); });
  }
  document.getElementById("seal-cancel").addEventListener("click", function () {
    if (pending) { show("Retry the unconfirmed action before continuing."); return; } work.hidden = true;
  });
  load();
}());
