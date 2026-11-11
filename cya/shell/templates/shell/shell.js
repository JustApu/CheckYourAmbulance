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
  var nav = document.querySelector(".site-nav");
  var navToggle = document.getElementById("site-nav-toggle");
  var navList = document.getElementById("site-nav-list");
  var narrowNav = window.matchMedia("(max-width: 47.999rem)");
  function syncNav() {
    if (!navToggle || !navList) { return; }
    navToggle.hidden = !narrowNav.matches;
    if (narrowNav.matches) {
      navList.hidden = navToggle.getAttribute("aria-expanded") !== "true";
    } else {
      navToggle.setAttribute("aria-expanded", "false");
      navList.hidden = false;
    }
  }
  if (navToggle && navList) {
    navToggle.addEventListener("click", function () {
      var expanded = navToggle.getAttribute("aria-expanded") === "true";
      navToggle.setAttribute("aria-expanded", String(!expanded));
      syncNav();
    });
    nav.addEventListener("keydown", function (event) {
      if (event.key === "Escape" && navToggle.getAttribute("aria-expanded") === "true") {
        navToggle.setAttribute("aria-expanded", "false");
        syncNav();
        navToggle.focus();
      }
    });
    navList.addEventListener("click", function (event) {
      if (event.target.closest("a") && narrowNav.matches) {
        navToggle.setAttribute("aria-expanded", "false");
        syncNav();
      }
    });
    narrowNav.addEventListener("change", syncNav);
    syncNav();
  }
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
