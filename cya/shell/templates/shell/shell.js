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
