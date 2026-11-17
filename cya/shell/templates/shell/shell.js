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
