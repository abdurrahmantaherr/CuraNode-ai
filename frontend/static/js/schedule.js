/* Progressive enhancement for the doctor schedule page (D7).

   Every form works with JavaScript disabled: a blank "patients per session"
   is stored as the calculated value by the server (spec R13). This file only
   (1) pre-fills that field live, as start, end or minutes per patient change,
   until the doctor types their own number, and (2) adds the submitting-state
   guard used on the other pages, so a double-click cannot submit twice. */

(function () {
  "use strict";

  var MAX_TOKENS = 200;

  function minutes(value) {
    var m = /^(\d{2}):(\d{2})$/.exec(value || "");
    return m ? parseInt(m[1], 10) * 60 + parseInt(m[2], 10) : null;
  }

  function calc(form) {
    var start = minutes((form.querySelector('[name="start_time"]') || {}).value);
    var end = minutes((form.querySelector('[name="end_time"]') || {}).value);
    var per = parseInt((form.querySelector('[name="slot_minutes"]') || {}).value, 10);
    if (start === null || end === null || !per || end - start < per) return "";
    return String(Math.min(Math.floor((end - start) / per), MAX_TOKENS));
  }

  document.querySelectorAll("input[data-max-tokens]").forEach(function (field) {
    var form = field.form;
    // A field that already holds a number (an edit, or a failed submit with a
    // value) is the doctor's own: never overwrite it.
    var auto = field.value === "";
    field.addEventListener("input", function () { auto = field.value === ""; });
    ["start_time", "end_time", "slot_minutes"].forEach(function (name) {
      var input = form.querySelector('[name="' + name + '"]');
      if (!input) return;
      input.addEventListener("change", function () {
        if (auto) field.value = calc(form);
      });
    });
  });

  document.querySelectorAll("form[method='post']").forEach(function (form) {
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
