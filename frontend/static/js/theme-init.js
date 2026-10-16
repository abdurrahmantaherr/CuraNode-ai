/* Applies a saved theme and sidebar state before the first paint, so there is no
   flash. Runs synchronously in <head>. Everything is wrapped: storage can be
   blocked (private windows), and the page must look right without it. */
(function () {
  "use strict";
  var root = document.documentElement;
  try {
    var theme = localStorage.getItem("cn-theme");
    if (theme === "light" || theme === "dark") root.setAttribute("data-theme", theme);
    if (localStorage.getItem("cn-sidebar") === "rail") root.setAttribute("data-sidebar", "rail");
  } catch (e) { /* no saved choice: the device decides */ }
})();
