(() => {
  // Issue #235 D5 and #236: after a reload, focus the saved or refused message, the search
  // result line or the search error, so the person continues from what just happened.
  const found = document.querySelector("[data-focus-on-load]");
  if (found) { found.focus(); }

  const list = (labels) => labels.length < 2
    ? labels.join("")
    : labels.slice(0, -1).join(", ") + " and " + labels[labels.length - 1];

  // Issue #236: a row stages any number of role changes; Save applies all of them or none.
  const setUp = (form) => {
    if (form.hasAttribute("data-role-ready")) { return; }
    form.setAttribute("data-role-ready", "");
    const choices = Array.from(form.querySelectorAll("[data-role-choice]"));
    const save = form.querySelector("[data-role-save-button]");
    const reset = form.querySelector("[data-role-reset-button]");
    const status = form.querySelector("[data-role-change-status]");
    const selfWarning = form.querySelector("[data-role-self-warning]");
    const row = form.closest("[data-role-user-row]");
    const staged = () => choices.filter((choice) => choice.checked !== choice.defaultChecked);

    const update = () => {
      const changed = staged();
      choices.forEach((choice) => {
        const option = choice.closest(".role-option");
        const pending = choice.checked !== choice.defaultChecked;
        option.classList.toggle("is-assigned", choice.defaultChecked);
        option.classList.toggle("is-pending", pending);
        const pendingLabel = option.querySelector("[data-role-pending]");
        pendingLabel.textContent = pending ? (choice.checked ? "Adding" : "Removing") : "";
        pendingLabel.hidden = !pending;
      });
      // D1: unticking your own Administrator role ends your access to Administration on save.
      const losingAdministration = form.hasAttribute("data-role-self")
        && changed.some((choice) => choice.value === "ADMINISTRATOR" && !choice.checked);
      if (selfWarning) { selfWarning.hidden = !losingAdministration; }
      save.hidden = changed.length === 0;
      reset.hidden = changed.length === 0;
      status.hidden = changed.length === 0;
      if (row) { row.toggleAttribute("data-role-pending-row", changed.length > 0); }
      if (changed.length > 0) {
        const parts = [];
        const added = changed.filter((choice) => choice.checked).map((choice) => choice.dataset.roleLabel);
        const removed = changed.filter((choice) => !choice.checked).map((choice) => choice.dataset.roleLabel);
        if (added.length) { parts.push("add " + list(added)); }
        if (removed.length) { parts.push("remove " + list(removed)); }
        status.textContent = "Not saved yet: " + parts.join("; ") + ". Select Save to apply "
          + (changed.length === 1 ? "this change." : "these " + changed.length + " changes.")
          + (losingAdministration ? " You will lose access to Administration when you save." : "");
      }
    };

    choices.forEach((choice) => choice.addEventListener("change", update));
    form.addEventListener("reset", () => window.setTimeout(update, 0));
    form.addEventListener("submit", (event) => {
      if (staged().length === 0) {
        event.preventDefault();
        status.hidden = false;
        status.textContent = "Nothing to save. Change a role first.";
      }
    });
    update();
  };

  const setUpAll = () => document.querySelectorAll("[data-role-change-form]").forEach(setUp);
  setUpAll();

  // Owner follow-up on #236: the list follows the search box as the person types, with no
  // Search button press. Rows on the page filter at once; after a short pause the server is
  // asked for every match. Rows with unsaved changes always stay, so no staged edit is lost.
  const searchForm = document.querySelector("[data-role-search-form]");
  const input = searchForm && searchForm.querySelector("[data-role-search]");
  const results = document.querySelector("[data-role-results]");
  if (!input || !results) { return; }
  let timer = null;
  let request = null;

  const filterRows = (query) => {
    results.querySelectorAll("[data-role-user-row]").forEach((row) => {
      const matches = !query || (row.dataset.roleSearchText || "").includes(query);
      row.hidden = !matches && !row.hasAttribute("data-role-pending-row");
    });
  };

  const keepPendingRows = (fresh) => {
    const pending = Array.from(results.querySelectorAll("[data-role-pending-row]"));
    if (!pending.length) { return; }
    let freshList = fresh.querySelector("[data-role-user-list]");
    if (!freshList) {
      freshList = document.createElement("ul");
      freshList.className = "role-user-list";
      freshList.setAttribute("aria-label", "Organization users and roles");
      freshList.setAttribute("data-role-user-list", "");
      fresh.appendChild(freshList);
    }
    pending.forEach((row) => {
      const same = freshList.querySelector('[data-role-user-row="' + row.dataset.roleUserRow + '"]');
      row.hidden = false;
      if (same) { same.replaceWith(row); } else { freshList.prepend(row); }
    });
  };

  const fetchMatches = async () => {
    const query = input.value.trim();
    const url = new URL(searchForm.action, window.location.href);
    if (query) { url.searchParams.set("q", query); }
    if (request) { request.abort(); }
    request = new AbortController();
    try {
      const response = await fetch(url, { signal: request.signal, credentials: "same-origin" });
      if (!response.ok) { return; }
      const page = new DOMParser().parseFromString(await response.text(), "text/html");
      const fresh = page.querySelector("[data-role-results]");
      if (!fresh || input.value.trim() !== query) { return; }
      fresh.querySelectorAll("[data-focus-on-load]").forEach((element) => element.removeAttribute("data-focus-on-load"));
      keepPendingRows(fresh);
      results.replaceChildren(...Array.from(fresh.childNodes).map((node) => document.adoptNode(node)));
      document.title = page.title || document.title;
      window.history.replaceState(null, "", url);
      setUpAll();
    } catch (error) {
      // An aborted or failed lookup leaves the current list as it is.
    }
  };

  input.addEventListener("input", () => {
    filterRows(input.value.trim().toLowerCase());
    window.clearTimeout(timer);
    timer = window.setTimeout(fetchMatches, 300);
  });
  searchForm.addEventListener("submit", (event) => {
    // Enter searches at once without reloading, which would discard staged changes.
    event.preventDefault();
    window.clearTimeout(timer);
    fetchMatches();
  });
})();
