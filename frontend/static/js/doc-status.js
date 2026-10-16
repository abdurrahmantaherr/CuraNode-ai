/* Status polling for the medical-document pages.

   Replaces the old "reload the whole page every few seconds" loop. A poller asks the
   existing JSON API (GET /api/v1/documents/{id}, GET /api/v1/documents/in-progress) for the
   document's REAL status, never guesses one, and hands what it got to the page, which
   updates only the parts that changed.

   Rules this file keeps (each is pinned by tests/js/doc_status.test.mjs):
   - One request in flight at a time; the next check is scheduled only after the last one
     answered, so a slow server is never piled onto.
   - A late answer to a request that was superseded or stopped is ignored.
   - Nothing is fetched while the tab is hidden; checking resumes the moment it is shown.
   - The interval grows while nothing changes (base -> max) and resets when something does.
   - Failures back off harder (and honour Retry-After); the page is told about a problem
     ONCE, when it starts, and once more when it recovers - not on every attempt.
   - 401/403 (signed out) and 404 (document gone) stop the polling for good.
   - stop() clears the timer and aborts the request in flight.

   The browser pieces (fetch, timers, visibility) are passed in, so the logic runs under
   Node's test runner with fakes and no DOM. */

(function (root, factory) {
  "use strict";
  if (typeof module === "object" && module.exports) module.exports = factory();
  else root.DocStatus = factory();
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  var DEFAULTS = {
    baseMs: 3000,          // first interval, and the one used again after a change
    maxMs: 15000,          // ceiling while nothing changes
    factor: 1.5,
    errorBaseMs: 5000,     // first wait after a failed check
    errorMaxMs: 30000,
    requestTimeoutMs: 15000,
    problemAfterFailures: 3,  // tell the page about trouble only after this many in a row
    slowAfterMs: 150000       // the backend treats a read older than 120 s as stale
  };

  function createPoller(options) {
    var cfg = {};
    var key;
    for (key in DEFAULTS) cfg[key] = options[key] !== undefined ? options[key] : DEFAULTS[key];

    var fetchFn = options.fetch;
    // Wrapped, not passed bare: a browser's setTimeout throws "Illegal invocation" when it is
    // called as a method of another object (`timers.setTimeout(...)`).
    var timers = options.timers || {
      setTimeout: function (fn, ms) { return setTimeout(fn, ms); },
      clearTimeout: function (id) { return clearTimeout(id); }
    };
    var now = options.now || Date.now;
    var isHidden = options.isHidden || function () { return false; };
    var makeController = options.makeController || function () {
      return typeof AbortController === "function" ? new AbortController() : null;
    };

    var running = false;
    var paused = false;       // waiting for the tab to be shown again
    var timer = null;
    var inflight = null;      // { seq, controller, timeout }
    var seq = 0;
    var failures = 0;
    var problemShown = false;
    var slowShown = false;
    var delay = cfg.baseMs;
    var signature = null;
    var startedAt = 0;

    function emitProblem(kind) { if (options.onProblem) options.onProblem(kind); }

    function clearTimer() {
      if (timer !== null) { timers.clearTimeout(timer); timer = null; }
    }

    function cancelRequest() {
      if (!inflight) return;
      var current = inflight;
      inflight = null;
      if (current.timeout !== null) timers.clearTimeout(current.timeout);
      if (current.controller) {
        try { current.controller.abort(); } catch (e) { /* nothing to abort */ }
      }
    }

    function schedule(ms) {
      clearTimer();
      if (!running) return;
      timer = timers.setTimeout(tick, ms);
    }

    function tick() {
      timer = null;
      if (!running || inflight) return;
      if (isHidden()) { paused = true; return; }   // resume() picks it up
      request();
    }

    function failed(retryAfterMs) {
      failures += 1;
      delay = Math.min(cfg.errorMaxMs, cfg.errorBaseMs * Math.pow(2, failures - 1));
      if (retryAfterMs) delay = Math.max(delay, Math.min(retryAfterMs, cfg.errorMaxMs * 2));
      if (failures >= cfg.problemAfterFailures && !problemShown) {
        problemShown = true;
        emitProblem("temporary");
      }
      schedule(delay);
    }

    function request() {
      seq += 1;
      var mine = seq;
      var controller = makeController();
      var entry = { seq: mine, controller: controller, timeout: null };
      inflight = entry;
      entry.timeout = timers.setTimeout(function () {
        // Treated like any other failed check: abort, then back off.
        if (inflight !== entry) return;
        cancelRequest();
        failed(0);
      }, cfg.requestTimeoutMs);

      var init = {
        method: "GET",
        credentials: "same-origin",
        cache: "no-store",
        headers: { Accept: "application/json" }
      };
      if (controller) init.signal = controller.signal;

      var pending;
      try { pending = fetchFn(options.url, init); } catch (e) { pending = Promise.reject(e); }

      pending.then(function (res) {
        if (inflight !== entry || !running) return;      // stale: superseded or stopped
        var status = res.status;
        if (status === 401 || status === 403) { settle(); emitProblem("signed_out"); stop(); return; }
        if (status === 404) { settle(); emitProblem("gone"); stop(); return; }
        if (status === 429 || status >= 500 || !res.ok) {
          var retry = 0;
          if (res.headers && typeof res.headers.get === "function") {
            var raw = parseInt(res.headers.get("Retry-After"), 10);
            if (raw > 0) retry = raw * 1000;
          }
          settle();
          failed(retry);
          return;
        }
        return res.json().then(function (data) {
          if (inflight !== entry || !running) return;
          settle();
          // A body the page cannot use (the wrong shape) must not leave the poller running but
          // silent: no next check and no word to the person. It is a failed check like any other.
          try { succeeded(data); } catch (e) { if (running) failed(0); }
        });
      }).catch(function () {
        if (inflight !== entry || !running) return;       // aborted by stop()/superseded
        settle();
        failed(0);
      });
    }

    // The request in `inflight` has been answered: release it without aborting.
    function settle() {
      if (!inflight) return;
      if (inflight.timeout !== null) timers.clearTimeout(inflight.timeout);
      inflight = null;
    }

    function succeeded(data) {
      failures = 0;
      if (problemShown) { problemShown = false; if (options.onRecovered) options.onRecovered(); }
      var next = options.signature ? options.signature(data) : null;
      var changed = signature !== null && next !== signature;
      signature = next;
      var done = options.onStatus ? options.onStatus(data, { changed: changed }) : false;
      if (done === true) { stop(); return; }
      if (!running) return;                                // onStatus may have stopped us
      if (changed) delay = cfg.baseMs;
      else delay = Math.min(cfg.maxMs, Math.round(delay * cfg.factor));
      if (!slowShown && cfg.slowAfterMs && now() - startedAt >= cfg.slowAfterMs) {
        slowShown = true;
        emitProblem("slow");
      }
      schedule(delay);
    }

    function start() {
      if (running) return;
      running = true;
      paused = false;
      failures = 0;
      problemShown = false;
      slowShown = false;
      signature = null;
      delay = cfg.baseMs;
      startedAt = now();
      tick();   // the first check is immediate: the page may be stale already
    }

    function stop() {
      running = false;
      paused = false;
      clearTimer();
      cancelRequest();
    }

    // The tab is visible again: check now instead of waiting out the interval.
    function resume() {
      if (!running) return;
      if (paused || (timer !== null && !isHidden())) {
        paused = false;
        checkNow();
      }
    }

    // A person asked ("Check again"). Never starts a second request on top of one in flight.
    function checkNow() {
      if (!running || inflight) return;
      clearTimer();
      tick();
    }

    return {
      start: start,
      stop: stop,
      resume: resume,
      checkNow: checkNow,
      isRunning: function () { return running; },
      isBusy: function () { return inflight !== null; },
      // for tests
      state: function () { return { delay: delay, failures: failures, problemShown: problemShown }; }
    };
  }

  return { createPoller: createPoller, DEFAULTS: DEFAULTS };
});
