/* Progressive enhancement for the shell. Every page works without this file:
   the sidebar and pill nav are plain links, the "More" sheet is a <details>,
   the language link and sign-out are a link and a form.

   Adds: theme switch, collapsible sidebar, copy buttons, today's date and a
   time-of-day greeting (the server does not send either), and closing the
   "More" sheet on an outside tap. */
(function () {
  "use strict";
  var root = document.documentElement;

  function save(key, value) {
    try { localStorage.setItem(key, value); } catch (e) { /* ignore */ }
  }

  // ── Theme switch: reveal the button, flip between light and dark ──
  function isDark() {
    var t = root.getAttribute("data-theme");
    if (t) return t === "dark";
    return window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches;
  }
  document.querySelectorAll("[data-theme-toggle]").forEach(function (btn) {
    btn.hidden = false;
    btn.addEventListener("click", function () {
      var next = isDark() ? "light" : "dark";
      root.setAttribute("data-theme", next);
      save("cn-theme", next);
    });
  });

  // ── Collapsible sidebar ──
  document.querySelectorAll("[data-sidebar-toggle]").forEach(function (btn) {
    btn.hidden = false;
    btn.addEventListener("click", function () {
      var rail = root.getAttribute("data-sidebar") === "rail";
      if (rail) root.removeAttribute("data-sidebar");
      else root.setAttribute("data-sidebar", "rail");
      save("cn-sidebar", rail ? "wide" : "rail");
    });
  });

  // ── Copy buttons: <button data-copy="text" data-copied="Copied"> ──
  document.querySelectorAll("[data-copy]").forEach(function (btn) {
    if (!navigator.clipboard) return;
    btn.hidden = false;
    btn.addEventListener("click", function () {
      navigator.clipboard.writeText(btn.getAttribute("data-copy")).then(function () {
        var label = btn.querySelector("[data-copy-label]");
        if (!label) return;
        var old = label.textContent;
        label.textContent = btn.getAttribute("data-copied") || old;
        window.setTimeout(function () { label.textContent = old; }, 1800);
      });
    });
  });

  // ── Today's date and a greeting. Without JS these stay empty and are hidden. ──
  var lang = root.getAttribute("lang") || "en";
  var now = new Date();
  document.querySelectorAll("[data-today]").forEach(function (el) {
    try {
      el.textContent = new Intl.DateTimeFormat(lang === "ur" ? "ur-PK" : "en-GB", {
        weekday: "long", day: "numeric", month: "long", timeZone: "Asia/Karachi"
      }).format(now);
      el.hidden = false;
    } catch (e) { /* leave hidden */ }
  });
  document.querySelectorAll("[data-greeting]").forEach(function (el) {
    var hour = parseInt(new Intl.DateTimeFormat("en-GB", { hour: "numeric", hour12: false, timeZone: "Asia/Karachi" }).format(now), 10);
    var key = hour < 12 ? "morning" : hour < 17 ? "afternoon" : "evening";
    var text = el.getAttribute("data-" + key);
    if (text) el.textContent = text;
  });

  // ── Close the "More" sheet when tapping elsewhere ──
  document.addEventListener("click", function (e) {
    document.querySelectorAll(".pillnav__more details[open]").forEach(function (d) {
      if (!d.contains(e.target)) d.removeAttribute("open");
    });
  });
})();
