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
  // header (except data-nosort) that sorts its rows. Only the sorted header carries aria-sort
  // (WAI-ARIA: one at a time). data-filter-label adds a filter box above it. Without this script the table is unchanged. The server still
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
      button.addEventListener("click", function () {
        var direction = th.getAttribute("aria-sort") === "ascending" ? "descending" : "ascending";
        headers.forEach(function (other) { other.removeAttribute("aria-sort"); });  // one sorted header at a time (WAI-ARIA)
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
  // Saved toast (issue #194). One role="status" region, created at load so that screen readers
  // announce later changes. It never takes focus and never blocks a tap (pointer-events: none),
  // and it fades after a few seconds. Call it only after the server has confirmed the save.
  // {announce: false} keeps it visual when the page's own status line already says the same.
  var toastNode = document.createElement("div"), toastTimer = null;
  toastNode.className = "toast";
  toastNode.setAttribute("role", "status");
  document.body.appendChild(toastNode);
  function showToast(text, options) {
    if (options && options.announce === false) { toastNode.setAttribute("aria-hidden", "true"); }
    else { toastNode.removeAttribute("aria-hidden"); }
    toastNode.textContent = "";
    var icon = document.createElement("span");
    icon.className = "toast-icon"; icon.setAttribute("aria-hidden", "true"); icon.textContent = "\u2713";
    toastNode.appendChild(icon);
    toastNode.appendChild(document.createTextNode(text));
    toastNode.className = "toast is-shown";
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () {
      toastNode.className = "toast";
      toastTimer = setTimeout(function () { toastNode.textContent = ""; }, 250);
    }, 3000);
  }
  // Keyboard shortcuts (issue #194). "?" lists them, "/" goes to the page's search box, "G" then a
  // letter goes to a place, and a page may offer its own (data-shortcut-key, e.g. N on a checklist).
  // They never fire while typing, with Ctrl, Alt or Cmd held, or while a dialog is open, and the list
  // has an off switch (WCAG 2.1.4), remembered on this device.
  var shortcutsDialog = document.getElementById("shortcuts-dialog");
  if (shortcutsDialog) {
    var shortcutsKey = "cya.shortcuts", shortcutsOn = true, pendingGo = 0, shortcutsOpener = null;
    var enabledBox = document.getElementById("shortcuts-enabled");
    try { shortcutsOn = window.localStorage.getItem(shortcutsKey) !== "off"; } catch (_error) { /* storage is optional */ }
    var applyShortcuts = function () {
      document.documentElement.classList.toggle("shortcuts-off", !shortcutsOn);
      enabledBox.checked = shortcutsOn;
    };
    applyShortcuts();
    enabledBox.addEventListener("change", function () {
      shortcutsOn = enabledBox.checked;
      try { window.localStorage.setItem(shortcutsKey, shortcutsOn ? "on" : "off"); } catch (_error) { /* storage is optional */ }
      applyShortcuts();
    });
    var openShortcuts = function () {
      shortcutsOpener = document.activeElement;
      if (typeof shortcutsDialog.showModal === "function") { shortcutsDialog.showModal(); } else { shortcutsDialog.setAttribute("open", ""); }
    };
    // The browser returns focus when a modal dialog closes. Step in only when focus was lost to the
    // page (the opener was hidden, as when the list was opened from the More panel), never taking it
    // from where the person has already moved on to.
    shortcutsDialog.addEventListener("close", function () {
      var lost = !document.activeElement || document.activeElement === document.body;
      if (lost) {
        var target = shortcutsOpener && shortcutsOpener.getClientRects().length ? shortcutsOpener : (more && !more.hidden ? more : null);
        if (target) { target.focus(); }
      }
      shortcutsOpener = null;
    });
    document.getElementById("shortcuts-close").addEventListener("click", function () { shortcutsDialog.close(); });
    var shortcutsOpen = document.getElementById("shortcuts-open");
    if (shortcutsOpen) {
      shortcutsOpen.addEventListener("click", function () {
        if (more && !wideNav.matches) { setMore(false); }
        openShortcuts();
      });
    }
    var typing = function (node) {
      return !!node && (node.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(node.tagName));
    };
    var shown = function (node) { return !!node && node.getClientRects().length > 0 && !node.disabled; };
    document.addEventListener("keydown", function (event) {
      if (!shortcutsOn || event.defaultPrevented || event.ctrlKey || event.metaKey || event.altKey) { return; }
      if (typing(event.target) || document.querySelector("dialog[open]")) { pendingGo = 0; return; }
      var key = event.key.length === 1 ? event.key.toLowerCase() : "";
      if (key === "?") { event.preventDefault(); openShortcuts(); return; }
      if (key === "/") {
        var search = document.querySelector('main input[type="search"]');
        if (shown(search)) { event.preventDefault(); search.focus(); }
        return;
      }
      // Letters are plain keys only: Shift+N is not N, and Shift never completes a G sequence.
      if (!/^[a-z]$/.test(key) || event.shiftKey) { pendingGo = 0; return; }
      if (pendingGo && Date.now() - pendingGo < 1500) {
        pendingGo = 0;
        var place = document.querySelector('[data-shortcut-go="' + key + '"]');
        if (place) { event.preventDefault(); place.click(); }
        return;
      }
      if (key === "g") { pendingGo = Date.now(); event.preventDefault(); return; }
      var action = document.querySelector('[data-shortcut-key="' + key + '"]');
      if (shown(action)) { event.preventDefault(); action.click(); }
    });
  }
  // Feature code calls cyaShell.setSaveState(element, "pending" | "saved" | "failed").
  // Unknown states are shown as failed: an unconfirmed entry is never shown as saved.
  window.cyaShell = {
    toast: showToast,
    setSaveState: function (el, state) {
      if (!labels[state]) { state = "failed"; }
      el.className = "save-state save-state-" + state;
      el.setAttribute("data-save-state", state);
      el.querySelector(".indicator-icon").textContent = labels[state][0];
      el.querySelector(".save-state-text").textContent = labels[state][1];
    }
  };
})();
