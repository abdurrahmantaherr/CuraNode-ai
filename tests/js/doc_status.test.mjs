// Tests for frontend/static/js/doc-status.js (run: node --test tests/js).
// No DOM and no network: the poller gets a fake clock, a fake fetch and a fake visibility flag.
import test from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";
import path from "node:path";

const require = createRequire(import.meta.url);
const here = path.dirname(fileURLToPath(import.meta.url));
const { createPoller, DEFAULTS } = require(path.join(here, "../../frontend/static/js/doc-status.js"));

// A manual clock: timers only fire when the test advances time.
function fakeClock() {
  let t = 0;
  let nextId = 1;
  const pending = new Map();
  return {
    now: () => t,
    timers: {
      setTimeout(fn, ms) { const id = nextId++; pending.set(id, { fn, at: t + ms }); return id; },
      clearTimeout(id) { pending.delete(id); },
    },
    pendingCount: () => pending.size,
    async advance(ms) {
      const target = t + ms;
      for (;;) {
        const due = [...pending.entries()].filter(([, v]) => v.at <= target).sort((a, b) => a[1].at - b[1].at)[0];
        if (!due) break;
        pending.delete(due[0]);
        t = due[1].at;
        due[1].fn();
        await flush();
      }
      t = target;
    },
  };
}

const flush = async () => { for (let i = 0; i < 8; i++) await Promise.resolve(); };

// A fetch whose answers the test controls. Each call returns a handle to settle it.
function fakeFetch() {
  const calls = [];
  const fn = (url, init) => {
    let resolve, reject;
    const promise = new Promise((res, rej) => { resolve = res; reject = rej; });
    const call = { url, init, resolve, reject, aborted: false };
    if (init && init.signal) init.signal.addEventListener("abort", () => { call.aborted = true; reject(new Error("aborted")); });
    calls.push(call);
    return promise;
  };
  fn.calls = calls;
  return fn;
}

const json = (data, status = 200, headers = {}) => ({
  ok: status >= 200 && status < 300,
  status,
  headers: { get: (name) => headers[name] ?? null },
  json: async () => data,
});

function setup(extra = {}) {
  const clock = fakeClock();
  const fetch = fakeFetch();
  const log = { status: [], problems: [], recovered: 0 };
  const hiddenRef = { value: false };
  const poller = createPoller({
    url: "/api/v1/documents/abc",
    fetch,
    timers: clock.timers,
    now: clock.now,
    isHidden: () => hiddenRef.value,
    signature: (d) => d.status,
    onStatus: (d, info) => { log.status.push([d.status, info.changed]); return extra.done ? extra.done(d) : false; },
    onProblem: (k) => log.problems.push(k),
    onRecovered: () => { log.recovered += 1; },
    ...extra.options,
  });
  return { clock, fetch, log, poller, hiddenRef };
}

test("slow processing: the interval grows while the status stays the same, capped at maxMs", async () => {
  const { clock, fetch, poller } = setup();
  poller.start();
  assert.equal(fetch.calls.length, 1, "the first check is immediate");
  const seen = [];
  for (let i = 0; i < 8; i++) {
    fetch.calls[fetch.calls.length - 1].resolve(json({ status: "processing" }));
    await flush();
    seen.push(poller.state().delay);
    await clock.advance(poller.state().delay);
  }
  assert.ok(seen[0] >= DEFAULTS.baseMs, "starts at the base interval or above");
  assert.ok(seen.every((d, i) => i === 0 || d >= seen[i - 1]), "never shrinks while nothing changes");
  assert.equal(Math.max(...seen), DEFAULTS.maxMs, "stops growing at maxMs");
  poller.stop();
});

test("successful processing: a changed status is reported once and the poller can stop itself", async () => {
  const { clock, fetch, log, poller } = setup({ done: (d) => d.status !== "processing" });
  poller.start();
  fetch.calls[0].resolve(json({ status: "processing" }));
  await flush();
  await clock.advance(DEFAULTS.baseMs * 3);
  fetch.calls[1].resolve(json({ status: "awaiting_review" }));
  await flush();
  assert.deepEqual(log.status, [["processing", false], ["awaiting_review", true]]);
  assert.equal(poller.isRunning(), false, "polling ends when processing finishes");
  assert.equal(clock.pendingCount(), 0, "no timer is left behind");
  await clock.advance(120000);
  assert.equal(fetch.calls.length, 2, "and nothing is requested afterwards");
});

test("no overlapping requests: checkNow while one is in flight does nothing", async () => {
  const { fetch, poller } = setup();
  poller.start();
  poller.checkNow();
  poller.checkNow();
  assert.equal(fetch.calls.length, 1);
  assert.equal(poller.isBusy(), true);
  poller.stop();
});

test("a repeated 'Check again' after an answer asks once more, not several times", async () => {
  const { fetch, poller } = setup();
  poller.start();
  fetch.calls[0].resolve(json({ status: "processing" }));
  await flush();
  poller.checkNow();
  poller.checkNow();
  assert.equal(fetch.calls.length, 2);
  poller.stop();
});

test("stale response: an answer that arrives after stop() is ignored", async () => {
  const { fetch, log, poller } = setup();
  poller.start();
  poller.stop();
  fetch.calls[0].resolve(json({ status: "awaiting_review" }));
  await flush();
  assert.deepEqual(log.status, []);
});

test("cleanup: stop() aborts the request in flight and clears the timer", async () => {
  const { clock, fetch, poller } = setup();
  poller.start();
  poller.stop();
  assert.equal(fetch.calls[0].aborted, true);
  assert.equal(clock.pendingCount(), 0);
  assert.equal(poller.isBusy(), false);
});

test("a late answer from an aborted request does not count as a failure", async () => {
  const { clock, fetch, log, poller } = setup();
  poller.start();
  poller.stop();
  await flush();
  await clock.advance(120000);
  assert.deepEqual(log.problems, []);
  assert.equal(fetch.calls.length, 1);
});

test("temporary failure: backs off, reports the problem ONCE, then reports recovery", async () => {
  const { clock, fetch, log, poller } = setup();
  poller.start();
  const delays = [];
  for (let i = 0; i < 5; i++) {
    fetch.calls[i].resolve(json({}, 503));
    await flush();
    delays.push(poller.state().delay);
    await clock.advance(poller.state().delay);
  }
  assert.deepEqual(log.problems, ["temporary"], "told once, not on every failed attempt");
  assert.ok(delays[1] > delays[0] && delays[2] > delays[1], "waits longer after each failure");
  assert.ok(Math.max(...delays) <= DEFAULTS.errorMaxMs);
  fetch.calls[5].resolve(json({ status: "processing" }));
  await flush();
  assert.equal(log.recovered, 1);
  assert.equal(poller.state().failures, 0);
  poller.stop();
});

test("a network error (fetch rejects) is a temporary failure, not a crash", async () => {
  const { fetch, poller } = setup();
  poller.start();
  fetch.calls[0].reject(new TypeError("Failed to fetch"));
  await flush();
  assert.equal(poller.isRunning(), true);
  assert.equal(poller.state().failures, 1);
  poller.stop();
});

test("a request that never answers times out and is retried later", async () => {
  const { clock, fetch, poller } = setup();
  poller.start();
  await clock.advance(DEFAULTS.requestTimeoutMs);
  assert.equal(fetch.calls[0].aborted, true);
  assert.equal(poller.state().failures, 1);
  await clock.advance(DEFAULTS.errorBaseMs - 1);
  assert.equal(fetch.calls.length, 1, "waits out the back-off first");
  await clock.advance(1);
  assert.equal(fetch.calls.length, 2, "then tries again");
  poller.stop();
});

test("429 honours Retry-After", async () => {
  const { fetch, poller } = setup();
  poller.start();
  fetch.calls[0].resolve(json({}, 429, { "Retry-After": "20" }));
  await flush();
  assert.ok(poller.state().delay >= 20000);
  poller.stop();
});

test("signed out (401) and document gone (404) stop the polling for good", async () => {
  for (const [status, kind] of [[401, "signed_out"], [403, "signed_out"], [404, "gone"]]) {
    const { clock, fetch, log, poller } = setup();
    poller.start();
    fetch.calls[0].resolve(json({}, status));
    await flush();
    assert.deepEqual(log.problems, [kind]);
    assert.equal(poller.isRunning(), false);
    await clock.advance(120000);
    assert.equal(fetch.calls.length, 1, `no further requests after ${status}`);
  }
});

test("a malformed answer (not JSON) is a temporary failure", async () => {
  const { fetch, poller } = setup();
  poller.start();
  fetch.calls[0].resolve({ ok: true, status: 200, headers: { get: () => null }, json: async () => { throw new SyntaxError("bad"); } });
  await flush();
  assert.equal(poller.state().failures, 1);
  poller.stop();
});

test("navigating away and back: a hidden tab asks nothing, and showing it checks at once", async () => {
  const { clock, fetch, hiddenRef, poller } = setup();
  poller.start();
  fetch.calls[0].resolve(json({ status: "processing" }));
  await flush();
  hiddenRef.value = true;
  await clock.advance(DEFAULTS.maxMs * 2);
  assert.equal(fetch.calls.length, 1, "nothing is fetched while hidden");
  hiddenRef.value = false;
  poller.resume();
  assert.equal(fetch.calls.length, 2, "the moment it is shown the real status is fetched");
  poller.stop();
});

test("slow: after a long wait the page is told once, and polling continues", async () => {
  const { clock, fetch, log, poller } = setup({ options: { slowAfterMs: 10000 } });
  poller.start();
  for (let i = 0; i < 6; i++) {
    fetch.calls[i].resolve(json({ status: "processing" }));
    await flush();
    await clock.advance(poller.state().delay);
  }
  assert.deepEqual(log.problems, ["slow"]);
  assert.equal(poller.isRunning(), true);
  poller.stop();
});

test("multiple documents: each poller is independent (own timer, own request)", async () => {
  const a = setup();
  const b = setup();
  a.poller.start();
  b.poller.start();
  a.fetch.calls[0].resolve(json({ status: "awaiting_review" }));
  await flush();
  assert.equal(a.log.status.length, 1);
  assert.equal(b.log.status.length, 0);
  a.poller.stop();
  assert.equal(b.poller.isRunning(), true);
  b.poller.stop();
});

test("start() twice does not start a second loop", async () => {
  const { fetch, poller } = setup();
  poller.start();
  poller.start();
  assert.equal(fetch.calls.length, 1);
  poller.stop();
});

test("the request asks for fresh JSON with the session cookie and no caching", async () => {
  const { fetch, poller } = setup();
  poller.start();
  const init = fetch.calls[0].init;
  assert.equal(init.credentials, "same-origin");
  assert.equal(init.cache, "no-store");
  assert.equal(init.headers.Accept, "application/json");
  poller.stop();
});

test("a body of the wrong shape (an object where a list was expected, or null) is a failed check, not a crash", async () => {
  for (const body of [{ detail: "oops" }, null, "text", 42]) {
    const { fetch, log, poller } = setup({
      options: { signature: (d) => d.map((x) => x.id).join("|") }, // what the list page's poller does
    });
    poller.start();
    fetch.calls[0].resolve(json(body));
    await flush();
    assert.equal(poller.isRunning(), true, "keeps running");
    assert.equal(poller.state().failures, 1, `counted as a failed check for ${JSON.stringify(body)}`);
    assert.deepEqual(log.status, [], "nothing is shown from a body that could not be read");
    poller.stop();
  }
});
