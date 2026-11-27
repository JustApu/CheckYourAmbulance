(function () {
  "use strict";
  // Paper entry setup (SPEC 8.0.0 REC-5, LATE-2 to LATE-4; issue #9 B6). It asks once for what
  // the outage sheet says and saves the batch header; each record is then saved on its own from
  // the paper checklist screen. Every server value is written with textContent, never as markup.
  // A request that got no definite answer is kept whole, with its operation id, and the next
  // Start resends it unchanged (REC-4); changing any answer makes the next Start a new request.
  var config = JSON.parse(document.getElementById("cya-config").textContent);
  var csrf = document.querySelector("[name=csrfmiddlewaretoken]").value;
  var $ = function (id) { return document.getElementById(id); };
  var form = $("paper-setup");
  if (!form) { return; }   // no published version: the page says so and offers nothing
  var MAX_PERFORMERS = 20, SEARCH_MIN = 2, SEARCH_MAX = 100, SEARCH_DELAY_MS = 250;
  var chosen = [];          // [{user_id, display_name, active}] in the order added
  var request = null, sending = false;
  var searchTimer = null, searchGeneration = 0;

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
  function clear(node) { while (node.firstChild) { node.removeChild(node.firstChild); } }

  // The paper screen's path: a constant, not text read from the page, so nothing in the DOM can
  // shape where the browser goes (issue 27). test_workflow_scripts.py pins it to the server's
  // route. The batch id fills exactly one percent-encoded segment and is never empty, "." or
  // "..": a refused id is handled like an unconfirmed answer, so Start replays the same request.
  var PAPER_PATH = "/inspection/paper-entry/";
  function paperUrl(id) {
    var segment = typeof id === "string" ? encodeURIComponent(id) : "";
    if (segment === "" || segment === "." || segment === "..") { throw new Error("unconfirmed"); }
    return PAPER_PATH + segment + "/";
  }

  // ---- printed versions (LATE-4) ----------------------------------------------------------
  var versionSelect = $("paper-version");
  function versionById(id) {
    return config.versions.filter(function (v) { return v.version_id === id; })[0] || null;
  }
  config.versions.forEach(function (version) {
    var option = el("option", "", version.label);
    option.value = version.version_id;
    if (version.current) { option.selected = true; versionSelect.value = version.version_id; }
    versionSelect.appendChild(option);
  });
  if (!versionSelect.value && config.versions.length) { versionSelect.value = config.versions[0].version_id; }
  function renderKits() {
    var box = $("paper-kit-fields"), version = versionById(versionSelect.value);
    clear(box);
    var kits = version ? version.kits : [];
    $("paper-kits").hidden = !kits.length;
    kits.forEach(function (kit, index) {
      var id = "paper-kit-" + index;
      var label = el("label", "paper-field", "");
      label.setAttribute("for", id);
      label.appendChild(el("span", "", "Kit version for " + kit.label));
      var select = el("select", "paper-kit-select");
      select.id = id; select.dataset.definition = kit.definition_id;
      var none = el("option", "", "Kit reference not used"); none.value = ""; select.appendChild(none);
      select.value = "";
      kit.versions.forEach(function (option) {
        var node = el("option", "", option.label);
        node.value = option.version_id;
        if (option.current) { node.selected = true; select.value = option.version_id; }
        select.appendChild(node);
      });
      select.addEventListener("change", changed);
      box.appendChild(label); box.appendChild(select);
    });
  }
  versionSelect.addEventListener("change", function () { renderKits(); changed(); });
  renderKits();

  // ---- performers (LATE-3) -----------------------------------------------------------------
  function changed() { request = null; }   // a changed answer is a new decision (REC-4)
  ["paper-date", "paper-time"].forEach(function (id) { $(id).addEventListener("input", changed); $(id).addEventListener("change", changed); });
  function performerText(person) { return person.display_name + (person.active === false ? " (inactive)" : ""); }
  function countText() {
    return !chosen.length ? "Nobody chosen yet." :
      chosen.length + (chosen.length === 1 ? " performer chosen." : " performers chosen.") +
      (chosen.length >= MAX_PERFORMERS ? " That is the most one sheet can name." : "");
  }
  function renderChosen() {
    var list = $("performer-chosen");
    clear(list);
    chosen.forEach(function (person) {
      var item = el("li", "performer-chip");
      item.appendChild(el("span", "performer-chip-name", performerText(person)));
      var remove = el("button", "button performer-remove", "Remove");
      remove.type = "button";
      remove.setAttribute("aria-label", "Remove " + performerText(person));
      remove.addEventListener("click", function () { removePerformer(person.user_id); });
      item.appendChild(remove);
      list.appendChild(item);
    });
    $("performer-count").textContent = countText();
  }
  function addPerformer(person) {
    var status = $("performer-search-status");
    if (chosen.some(function (p) { return p.user_id === person.user_id; })) {
      status.textContent = performerText(person) + " is already chosen.";
      return;
    }
    if (chosen.length >= MAX_PERFORMERS) {
      status.textContent = "A sheet can name at most " + MAX_PERFORMERS + " performers.";
      return;
    }
    chosen.push(person); changed(); renderChosen(); renderResults(lastResults);
    status.textContent = "Added " + performerText(person) + ". " + countText();
    // Focus stays in the search so the next name can be typed at once.
    $("performer-search").focus();
  }
  function removePerformer(userId) {
    var person = chosen.filter(function (p) { return p.user_id === userId; })[0];
    chosen = chosen.filter(function (p) { return p.user_id !== userId; });
    changed(); renderChosen(); renderResults(lastResults);
    $("performer-search-status").textContent = (person ? "Removed " + performerText(person) + ". " : "") + countText();
    // The removed chip's button is gone: focus moves to the search, never to the page top.
    $("performer-search").focus();
  }
  var lastResults = [];
  function renderResults(users) {
    var list = $("performer-results");
    clear(list);
    users.forEach(function (person) {
      var item = el("li");
      var taken = chosen.some(function (p) { return p.user_id === person.user_id; });
      var button = el("button", "button performer-result", (taken ? "Chosen: " : "Add ") + performerText(person));
      button.type = "button"; button.disabled = taken;
      if (person.email) { item.appendChild(button); item.appendChild(el("span", "muted performer-email", person.email)); }
      else { item.appendChild(button); }
      button.addEventListener("click", function () { addPerformer(person); });
      list.appendChild(item);
    });
  }
  function search() {
    var text = $("performer-search").value.trim(), status = $("performer-search-status");
    var generation = ++searchGeneration;
    if (text.length < SEARCH_MIN) { lastResults = []; renderResults([]); status.textContent = text ? "Type at least 2 characters." : ""; return; }
    if (text.length > SEARCH_MAX) { lastResults = []; renderResults([]); status.textContent = "Type at most 100 characters."; return; }
    status.textContent = "Searching…";
    fetch(config.urls.performers + "?q=" + encodeURIComponent(text), {credentials: "same-origin", cache: "no-store"}).then(function (r) {
      return r.json().then(function (b) { return {status: r.status, ok: r.ok, body: b}; });
    }).then(function (a) {
      if (generation !== searchGeneration) { return; }   // a newer search replaced this one
      if (a.status === 403) { lastResults = []; renderResults([]); status.textContent = "You are not permitted to choose other performers."; return; }
      if (!a.ok || !a.body || a.body.status !== "OK" || !Array.isArray(a.body.users)) { throw new Error("search"); }
      lastResults = a.body.users; renderResults(lastResults);
      status.textContent = !lastResults.length ? "No one matches." :
        lastResults.length + (lastResults.length === 1 ? " match." : " matches.") +
        (a.body.truncated ? " More people match: type more of the name to narrow the list." : "");
    }).catch(function () {
      if (generation === searchGeneration) { status.textContent = "The search did not answer. Try again."; }
    });
  }
  if (config.can_attribute) {
    $("performer-search").addEventListener("input", function () {
      clearTimeout(searchTimer); searchTimer = setTimeout(search, SEARCH_DELAY_MS);
    });
    $("performer-add-self").addEventListener("click", function () {
      addPerformer({user_id: config.user_id, display_name: config.own_name, active: true});
    });
    renderChosen();
  } else {
    // A Crew member enters only their own paper records (LATE-3).
    $("performer-self").textContent = "You (" + config.own_name + ")";
  }

  // ---- errors --------------------------------------------------------------------------------
  var FIELD_IDS = {checklist_version_id: "paper-version", performed_on: "paper-date",
    performed_time: "paper-time", performer_ids: config.can_attribute ? "performer-search" : null,
    kit_definition_version_ids: "paper-kit-0"};
  var MESSAGES = {
    PERFORMED_IN_FUTURE: "The date and time are later than now. Copy them exactly as written on the sheet.",
    VERSION_NOT_IN_FORCE: "That checklist version was not yet in force at that date and time. Check the Version ID and the time on the sheet.",
    VERSION_NOT_FOR_VEHICLE: "That checklist version is not one of this vehicle's checklists.",
    KIT_VERSION_NOT_IN_FORCE: "A kit version was not yet in force at that date and time. Check the kit reference you used.",
    KIT_VERSION_NOT_IN_CHECKLIST: "A kit version is not part of the printed checklist version.",
    KIT_VERSION_AMBIGUOUS: "Choose one kit version per kit.",
    VEHICLE_INACTIVE: "This vehicle is inactive, so its paper records cannot be entered.",
    NOT_FOUND: "A checklist version, kit version or person was not found. Reload the page and choose again.",
    FORBIDDEN: "You are not permitted to enter these paper records. Only a Supervisor or Administrator can name other people.",
    AUTHENTICATION_REQUIRED: "Your sign-in has ended. Sign in again.",
    OPERATION_REUSED: "This request changed after it was sent. Select Start paper entry again.",
    OPERATION_OWNED_BY_ANOTHER_ACTOR: "This request belongs to another user. Select Start paper entry again.",
    MALFORMED_REQUEST: "Some answers are not in the expected form. Check the highlighted fields."
  };
  function clearErrors() {
    $("paper-errors").hidden = true; clear($("paper-error-list"));
    Object.keys(FIELD_IDS).forEach(function (name) {
      var field = FIELD_IDS[name] && $(FIELD_IDS[name]);
      if (field) { field.removeAttribute("aria-invalid"); }
    });
  }
  function showErrors(messages, paths) {
    var list = $("paper-error-list");
    clear(list);
    messages.forEach(function (text) { list.appendChild(el("li", "", text)); });
    (paths || []).forEach(function (path) {
      var field = FIELD_IDS[path] && $(FIELD_IDS[path]);
      if (field) { field.setAttribute("aria-invalid", "true"); }
    });
    $("paper-errors").hidden = false;
    $("paper-errors").focus();
  }
  function state(kind, text) {
    var s = $("paper-state");
    s.className = "save-state save-state-" + kind; s.hidden = !text;
    s.textContent = text;
  }

  // ---- start -------------------------------------------------------------------------------
  var DATE = /^\d{4}-\d{2}-\d{2}$/, TIME = /^\d{2}:\d{2}(:00)?$/;
  function build() {
    var problems = [], paths = [];
    var date = $("paper-date").value.trim(), time = $("paper-time").value.trim();
    if (!versionSelect.value) { problems.push("Choose the Version ID printed on the sheet."); paths.push("checklist_version_id"); }
    if (!DATE.test(date)) { problems.push("Enter the date written on the sheet."); paths.push("performed_on"); }
    if (!TIME.test(time)) { problems.push("Enter the time written on the sheet, in hours and minutes."); paths.push("performed_time"); }
    var performers = config.can_attribute ? chosen.map(function (p) { return p.user_id; }) : [config.user_id];
    if (!performers.length) { problems.push("Choose who did the checks."); paths.push("performer_ids"); }
    if (problems.length) { return {problems: problems, paths: paths}; }
    var body = {
      operation_id: uuid4(), vehicle_id: config.vehicle_id, checklist_version_id: versionSelect.value,
      performed_on: date, performed_time: time.slice(0, 5), performer_ids: performers
    };
    var kits = Array.prototype.map.call(document.querySelectorAll(".paper-kit-select"), function (s) { return s.value; })
      .filter(function (value) { return !!value; });
    if (kits.length) { body.kit_definition_version_ids = kits; }
    return {body: body};
  }
  function setBusy(busy) {
    sending = busy;
    $("paper-start").disabled = busy;
  }
  form.addEventListener("submit", function (event) {
    event.preventDefault();
    if (sending) { return; }
    clearErrors();
    if (!request) {
      var built = build();
      if (built.problems) { showErrors(built.problems, built.paths); return; }
      request = built.body;
    }
    var sent = request;
    setBusy(true); state("pending", "Starting…");
    fetch(config.urls.batches, {
      method: "POST", credentials: "same-origin", cache: "no-store",
      headers: {"Content-Type": "application/json", "X-CSRFToken": csrf},
      body: JSON.stringify(sent)
    }).then(function (r) {
      return r.json().then(function (b) { return {ok: r.ok, status: r.status, body: b}; },
        function () { return {ok: r.ok, status: r.status, body: null}; });
    }).then(function (a) {
      if (a.ok && a.body && a.body.status === "COMMITTED") {
        var url = paperUrl(a.body.result && a.body.result.batch_id);
        request = null; state("saved", "Saved. Opening the paper checklist…");
        location.assign(url);
        return;
      }
      if (a.body && a.body.status === "REJECTED" && a.status < 500) {
        // A definite refusal wrote nothing: the next Start is a new request.
        if (request === sent) { request = null; }
        setBusy(false); state("failed", "Not started.");
        showErrors([MESSAGES[a.body.code] || "Not accepted (" + a.body.code + ")."],
          (a.body.field_errors || []).map(function (e) { return e.path; }));
        return;
      }
      throw new Error("unconfirmed");
    }).catch(function () {
      setBusy(false);
      state("unconfirmed", "Unconfirmed: the server did not answer, so the paper entry may or may not have started. Select Start paper entry again to retry the same request.");
    });
  });
})();
