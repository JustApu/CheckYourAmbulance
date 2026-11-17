(() => {
  document.querySelectorAll("[data-role-change-form]").forEach((form) => {
    const choices = Array.from(form.querySelectorAll("[data-role-choice]"));
    const review = form.querySelector("[data-role-review-button]");
    const reset = form.querySelector("[data-role-reset-button]");
    const status = form.querySelector("[data-role-change-status]");

    const update = () => {
      const changed = choices.filter((choice) => choice.checked !== choice.defaultChecked);
      choices.forEach((choice) => {
        choice.closest(".role-option").classList.toggle("is-assigned", choice.checked);
      });
      review.hidden = changed.length === 0;
      review.disabled = changed.length > 1;
      reset.hidden = changed.length === 0;
      status.hidden = changed.length === 0;
      if (changed.length === 1) {
        const choice = changed[0];
        status.textContent = (choice.checked ? "Add" : "Remove") + " the " + choice.dataset.roleLabel + " role. Review the change before confirming it.";
      } else if (changed.length > 1) {
        status.textContent = "Review one role change at a time. Reset all but one role selection to continue.";
      }
    };

    choices.forEach((choice) => choice.addEventListener("change", update));
    form.addEventListener("reset", () => window.setTimeout(update, 0));
    form.addEventListener("submit", (event) => {
      const changed = choices.filter((choice) => choice.checked !== choice.defaultChecked);
      if (changed.length !== 1) {
        event.preventDefault();
        status.hidden = false;
        status.textContent = changed.length === 0
          ? "Choose one role to change before continuing."
          : "Review one role change at a time. Reset all but one role selection to continue.";
        review.hidden = false;
        review.disabled = changed.length > 1;
      }
    });
    update();
  });
})();
