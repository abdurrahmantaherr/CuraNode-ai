/* Live queue (P6): a page that carries data-autorefresh reloads itself, so the
   patient's place stays current. Without JavaScript the Refresh link does the same. */
(function () {
  "use strict";
  var auto = document.querySelector("[data-autorefresh]");
  if (!auto) return;
  var ms = parseInt(auto.getAttribute("data-autorefresh"), 10) || 15000;
  window.setTimeout(function () {
    window.location.reload();
  }, ms);
})();
