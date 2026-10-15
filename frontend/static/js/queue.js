/* Live queue (P5): a page that carries data-autorefresh reloads itself, so the
   patient's place stays current. Without JavaScript the Refresh link does the same. */
(function () {
  "use strict";
  var auto = document.querySelector("[data-autorefresh]");
  if (!auto) return;
  var ms = parseInt(auto.getAttribute("data-autorefresh"), 10) || 15000;
  window.setTimeout(function () {
    // A one-off "Booked." banner arrives as ?saved=...; reloading with it would show the
    // banner again every time, so reload to the same address without it.
    var url = new URL(window.location.href);
    if (url.searchParams.has("saved")) {
      url.searchParams.delete("saved");
      window.location.replace(url.toString());
    } else {
      window.location.reload();
    }
  }, ms);
})();
