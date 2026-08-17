/* Progressive enhancement for the auth screens.

   The role toggle is a native radio group, so `role` submits correctly with
   JavaScript disabled. Everything below is enhancement only:
     - login:    swap the subheading copy to match the selection
     - register: collapse the doctor-only fields when Patient is selected

   Doctor fields render visible by default, so a no-JS user can still complete
   a doctor registration. */

(function () {
  "use strict";

  var radios = document.querySelectorAll(".js-role");
  var doctorFields = document.getElementById("doctor-fields");
  var loginSub = document.getElementById("login-sub");

  function selected() {
    for (var i = 0; i < radios.length; i++) {
      if (radios[i].checked) return radios[i];
    }
    return null;
  }

  function clearDoctorFields() {
    if (!doctorFields) return;
    // Cleared on switch-back so doctor-only values can never be submitted
    // alongside a patient registration (SPEC §5.2).
    doctorFields.querySelectorAll("input, select").forEach(function (el) {
      el.value = "";
      el.removeAttribute("aria-invalid");
    });
  }

  function sync(userInitiated) {
    var current = selected();
    if (!current) return;

    if (loginSub && current.dataset.sub) {
      loginSub.textContent = current.dataset.sub;
    }

    if (doctorFields) {
      var hideWhen = doctorFields.dataset.hideWhen || "patient";
      var shouldHide = current.value === hideWhen;
      // Only wipe values on an actual user switch, never on initial load —
      // that would discard values the server re-rendered after a failure.
      if (shouldHide && userInitiated) clearDoctorFields();
      doctorFields.hidden = shouldHide;
    }
  }

  radios.forEach(function (radio) {
    radio.addEventListener("change", function () {
      sync(true);
    });
  });

  sync(false);

  /* Submitting state: disable the control and show a spinner, so a
     double-click cannot submit twice (SPEC AC-19). */
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
