/* Progressive enhancement for the medical timeline.

   The timeline works with JavaScript disabled: the search and filters are a plain GET form, the
   tabs, paging and details are plain links, and every result is rendered by the server from the
   real records. This file only adds the loading state: while the next view loads, the list is
   replaced by placeholders (aria-busy on the region, styled in app.css) and the search button shows
   it is working, so a second click cannot send the same request twice. */

(function () {
  "use strict";

  var region = document.getElementById("timeline-region");

  function setBusy() {
    if (region) region.setAttribute("aria-busy", "true");
  }

  function clearBusy() {
    if (region) region.removeAttribute("aria-busy");
    document.querySelectorAll(".tl-filter-form button[type='submit']").forEach(function (btn) {
      if (btn.dataset.idle) {
        btn.disabled = false;
        btn.removeAttribute("aria-busy");
        btn.innerHTML = btn.dataset.idle;
      }
    });
  }

  var form = document.querySelector(".tl-filter-form");
  if (form) {
    form.addEventListener("submit", function (event) {
      if (event.defaultPrevented) return;
      var btn = form.querySelector("button[type='submit']");
      if (!btn || btn.disabled) return;
      // The browser validates the dates first (a "To" before "From" never reaches here).
      if (!form.checkValidity()) return;
      btn.dataset.idle = btn.innerHTML;
      btn.disabled = true;
      btn.setAttribute("aria-busy", "true");
      btn.innerHTML =
        '<span class="spinner" aria-hidden="true"></span>' +
        "<span>" + (btn.dataset.label || "") + "</span>";
      setBusy();
    });
  }

  document.querySelectorAll("[data-tl-nav]").forEach(function (link) {
    link.addEventListener("click", function (event) {
      // A modified click opens another tab; this page is not the one that loads.
      if (event.ctrlKey || event.metaKey || event.shiftKey || event.altKey || event.button) return;
      setBusy();
    });
  });

  // Coming back with the browser's Back button restores the page as it was left: not loading.
  window.addEventListener("pageshow", clearBusy);
})();
