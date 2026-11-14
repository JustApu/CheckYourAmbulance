(function () {
  "use strict";
  var config = JSON.parse(document.getElementById("seal-config").textContent);
  var csrf = document.querySelector("#seal-csrf [name=csrfmiddlewaretoken]").value;
  var list = document.getElementById("seal-subjects"), work = document.getElementById("seal-work");
  var formArea = document.getElementById("seal-form"), message = document.getElementById("seal-message");
  var heading = document.getElementById("seal-work-heading"), pending = null;

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
  function send(action, body) {
    if (pending) { show("A previous request is unconfirmed. Retry it before making another change."); return; }
    body.operation_id = uuid4(); pending = {action: action, body: body}; transmit();
  }
  function transmit() {
    if (!pending) { return; }
    var request = pending;
    fetch(config.command_url.replace("ACTION", request.action), {
      method: "POST", credentials: "same-origin", cache: "no-store",
      headers: {"Content-Type": "application/json", "X-CSRFToken": csrf},
      body: JSON.stringify(request.body)
    }).then(function (response) {
      return response.json().then(function (body) { return {status: response.status, body: body}; });
    }).then(function (answer) {
      if (answer.body.status === "COMMITTED") {
        pending = null; work.hidden = true; show("Recorded."); load(); return;
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
    var url = config.scope_url.replace("KIND", encodeURIComponent(subject.kind)).replace("SUBJECT", encodeURIComponent(subject.id));
    fetch(url, {credentials: "same-origin", cache: "no-store"}).then(function (response) {
      if (!response.ok) { throw new Error("scope unavailable"); } return response.json();
    }).then(function (data) { inspectionForm(subject, data); }).catch(function () {
      show("The current contents are unavailable. Refresh and try again.");
    });
  }
  function inspectionForm(subject, data) {
    if (data.kit_lines.length) {
      formArea.appendChild(element("p", "This cabinet includes kit counts. Inspect its contents individually until those counts can be recorded here."));
      return;
    }
    var form = element("form"), fields = [], dates = [];
    data.items.forEach(function (item) {
      var group = element("fieldset"), legend = element("legend", item.label + (item.kit_label ? " — " + item.kit_label : ""));
      group.appendChild(legend); var row = {item: item};
      if (item.kind === "QUANTITY") {
        row.count = field(group, "Fresh count (target " + item.expected_quantity + ")", "number", true);
        row.count.min = "0"; row.count.step = "1";
      }
      if (item.kind === "MEASUREMENT") {
        row.measurement = field(group, "Measurement (" + item.unit + ")", "text", true);
      }
      if (item.expiration_tracked) { row.expiration = field(group, "Fresh expiration date", "date", true); dates.push(row.expiration); }
      row.outcome = select(group, "Result", item.kind === "QUANTITY" ?
        [{value: "", label: "Use count"}, {value: "FAILED", label: "Failed for another reason"}] :
        [{value: "", label: "Choose result"}, {value: "PASSED", label: "Passed"}, {value: "FAILED", label: "Failed"}]);
      if (item.kind !== "QUANTITY") { row.outcome.required = true; }
      row.deficiency = deficiencyFields(group); fields.push(row); form.appendChild(group);
    });
    var expiry = element("p"), marking = field(form, "Crew ID to write on seal", "text", true);
    function updateExpiration() {
      var values = dates.map(function (input) { return input.value; }).filter(Boolean).sort();
      expiry.textContent = dates.length === 0 ? "Write no expiration on the seal." :
        values.length !== dates.length ? "Enter every tracked expiration to calculate the seal date." :
        "Write this earliest expiration on the seal: " + values[0] + ".";
    }
    dates.forEach(function (input) { input.addEventListener("change", updateExpiration); });
    updateExpiration(); form.appendChild(expiry);
    var fresh = field(form, "I inspected every listed item", "checkbox", true);
    var written = field(form, "I wrote the crew ID and the displayed expiration or no expiration", "checkbox", true);
    var submit = element("button", "Record inspection and reseal"); submit.type = "submit"; submit.className = "button primary";
    form.appendChild(submit); formArea.appendChild(form);
    form.addEventListener("submit", function (event) {
      event.preventDefault(); if (!form.reportValidity()) { return; }
      var invalid = false;
      var observations = fields.map(function (row) {
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
      send("reseal", Object.assign(subjectFields(subject), {marking: marking.value,
        inspected_attestation: fresh.checked, written_attestation: written.checked, observations: observations}));
    });
  }
  function load() {
    fetch(config.subjects_url, {credentials: "same-origin", cache: "no-store"}).then(function (response) {
      if (!response.ok) { throw new Error("unavailable"); } return response.json();
    }).then(function (data) {
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
