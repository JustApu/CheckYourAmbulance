(function () {
  "use strict";
  try {
    var theme = window.localStorage.getItem("cya-theme");
    if (theme === "light" || theme === "dark") {
      document.documentElement.setAttribute("data-theme", theme);
    }
  } catch (_error) { /* storage is optional */ }
})();
