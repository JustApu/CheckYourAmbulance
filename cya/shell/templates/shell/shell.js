(function () {
  "use strict";
  var banner = document.getElementById("connection-banner");
  var labels = {
    pending: ["⌛", "Saving…"],
    saved: ["✓", "Saved"],
    failed: ["✕", "Not saved. Try again."],
    unconfirmed: ["?", "Unconfirmed. It may or may not be recorded."]
  };
  function showConnection() { if (banner) { banner.hidden = navigator.onLine !== false; } }
  window.addEventListener("online", showConnection);
  window.addEventListener("offline", function () { if (banner) { banner.hidden = false; } });
  if (navigator.onLine === false && banner) { banner.hidden = false; }
  // Navigation (issue #194): a bottom tab bar on phones, a rail on tablets and a sidebar on
  // laptops and desktops. Below 64rem, More (hamburger icon) opens a panel with its places,
  // the theme and Sign out; at 64rem and wider that panel is always shown.
  var more = document.getElementById("app-nav-more");
  var morePanel = document.getElementById("app-nav-more-panel");
  var wideNav = window.matchMedia("(min-width: 64rem)");
  function setMore(open) {
    more.setAttribute("aria-expanded", String(open));
    syncNav();
  }
  function syncNav() {
    if (!more || !morePanel) { return; }
    var buttonHadFocus = document.activeElement === more;
    more.hidden = wideNav.matches;
    if (wideNav.matches) {
      more.setAttribute("aria-expanded", "false");
      morePanel.hidden = false;
      if (buttonHadFocus) {
        var first = morePanel.querySelector('[aria-current="page"]') || morePanel.querySelector("a, select, button");
        if (first) { first.focus(); }
      }
    } else {
      morePanel.hidden = more.getAttribute("aria-expanded") !== "true";
      if (morePanel.hidden && morePanel.contains(document.activeElement)) { more.focus(); }
    }
  }
  if (more && morePanel) {
    more.addEventListener("click", function () {
      setMore(more.getAttribute("aria-expanded") !== "true");
    });
    document.addEventListener("keydown", function (event) {
      if (event.key === "Escape" && more.getAttribute("aria-expanded") === "true") {
        setMore(false);
        more.focus();
      }
    });
    document.addEventListener("click", function (event) {
      if (more.getAttribute("aria-expanded") === "true" && !more.contains(event.target) && !morePanel.contains(event.target)) {
        setMore(false);
      }
    });
    morePanel.addEventListener("click", function (event) {
      if (event.target.closest("a") && !wideNav.matches) { setMore(false); }
    });
    wideNav.addEventListener("change", syncNav);
    syncNav();
  }
  // Sortable, filterable tables (issue #194). A table marked data-sortable gets a button in each
  // header (except data-nosort) that sorts its rows, reported with aria-sort; data-filter-label
  // adds a filter box above it. Without this script the table is unchanged. The server still
  // owns the data; this only reorders and hides rows already on the page.
  var collator = window.Intl ? new Intl.Collator(undefined, { numeric: true, sensitivity: "base" }) : null;
  function cellText(row, index) {
    var cell = row.cells[index];
    return cell ? cell.textContent.replace(/\s+/g, " ").trim() : "";
  }
  Array.prototype.forEach.call(document.querySelectorAll("table[data-sortable]"), function (table, tableIndex) {
    var body = table.tBodies[0];
    if (!body || !table.tHead || !table.tHead.rows.length) { return; }
    var status = document.createElement("p");
    status.className = "visually-hidden table-status";
    status.setAttribute("role", "status");
    var holder = table.closest(".table-scroll") || table;
    holder.parentNode.insertBefore(status, holder.nextSibling);
    var headers = Array.prototype.slice.call(table.tHead.rows[0].cells);
    headers.forEach(function (th, index) {
      if (th.hasAttribute("data-nosort")) { return; }
      var name = th.textContent.trim();
      var button = document.createElement("button");
      button.type = "button";
      button.className = "sort-button";
      button.textContent = name;
      th.textContent = "";
      th.appendChild(button);
      th.setAttribute("aria-sort", "none");
      button.addEventListener("click", function () {
        var direction = th.getAttribute("aria-sort") === "ascending" ? "descending" : "ascending";
        headers.forEach(function (other) { if (other.hasAttribute("aria-sort")) { other.setAttribute("aria-sort", "none"); } });
        th.setAttribute("aria-sort", direction);
        var sign = direction === "ascending" ? 1 : -1;
        var rows = Array.prototype.slice.call(body.rows);
        rows.sort(function (a, b) {
          var x = cellText(a, index);
          var y = cellText(b, index);
          return sign * (collator ? collator.compare(x, y) : (x < y ? -1 : x > y ? 1 : 0));
        });
        rows.forEach(function (row) { body.appendChild(row); });
        status.textContent = "Sorted by " + name + ", " + direction + ".";
      });
    });
    var label = table.getAttribute("data-filter-label");
    if (!label) { return; }
    var id = "table-filter-" + (tableIndex + 1);
    var box = document.createElement("div");
    box.className = "table-filter";
    var labelNode = document.createElement("label");
    labelNode.setAttribute("for", id);
    labelNode.textContent = label;
    var input = document.createElement("input");
    input.type = "search";
    input.id = id;
    input.autocomplete = "off";
    labelNode.appendChild(input);
    box.appendChild(labelNode);
    holder.parentNode.insertBefore(box, holder);
    input.addEventListener("input", function () {
      var query = input.value.replace(/\s+/g, " ").trim().toLowerCase();
      var rows = Array.prototype.slice.call(body.rows);
      var shown = 0;
      rows.forEach(function (row) {
        var match = !query || row.textContent.replace(/\s+/g, " ").toLowerCase().indexOf(query) !== -1;
        row.hidden = !match;
        if (match) { shown += 1; }
      });
      status.textContent = query ? "Showing " + shown + " of " + rows.length + "." : "Showing all " + rows.length + ".";
    });
  });
  var themeSelect = document.getElementById("theme-select");
  var themeKey = "cya-theme";
  function applyTheme(theme, persist) {
    if (["auto", "light", "dark"].indexOf(theme) === -1) { theme = "auto"; }
    if (theme === "auto") {
      document.documentElement.removeAttribute("data-theme");
    } else {
      document.documentElement.setAttribute("data-theme", theme);
    }
    if (themeSelect) { themeSelect.value = theme; }
    if (persist) {
      try { window.localStorage.setItem(themeKey, theme); } catch (_error) { /* storage is optional */ }
    }
  }
  if (themeSelect) {
    var savedTheme = "auto";
    try { savedTheme = window.localStorage.getItem(themeKey) || "auto"; } catch (_error) { /* storage is optional */ }
    applyTheme(savedTheme, false);
    themeSelect.addEventListener("change", function () { applyTheme(themeSelect.value, true); });
  }
  // Feature code calls cyaShell.setSaveState(element, "pending" | "saved" | "failed").
  // Unknown states are shown as failed: an unconfirmed entry is never shown as saved.
  window.cyaShell = {
    setSaveState: function (el, state) {
      if (!labels[state]) { state = "failed"; }
      el.className = "save-state save-state-" + state;
      el.setAttribute("data-save-state", state);
      el.querySelector(".indicator-icon").textContent = labels[state][0];
      el.querySelector(".save-state-text").textContent = labels[state][1];
    }
  };
})();
