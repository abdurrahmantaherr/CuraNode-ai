/* Progressive enhancement for the patient profile page.

   Every form on the page works with JavaScript disabled — add, edit and
   remove all post and redirect, and the remove confirmation is a native
   <details>. This file only adds the submitting-state guard (same contract
   as auth.js): disable THAT form's submit button and show a spinner, so a
   double-click cannot submit twice. */

(function () {
  "use strict";

  document.querySelectorAll("form").forEach(function (form) {
    form.addEventListener("submit", function () {
      var btn = form.querySelector('button[type="submit"]');
      if (!btn || btn.disabled) return;
      btn.disabled = true;
      btn.setAttribute("aria-busy", "true");
      btn.innerHTML =
        '<span class="spinner" aria-hidden="true"></span>' +
        "<span>" + (btn.dataset.label || "") + "</span>";
    });
  });
})();
