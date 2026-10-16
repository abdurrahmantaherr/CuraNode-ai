/* Progressive enhancement for the medical-document pages.

   Every form works with JavaScript disabled - the upload is a plain multipart
   POST and the review is a plain form. The server is authoritative (it
   re-validates the file's real type and size); this file only makes the flow
   friendlier:

   1. Upload page: drag-over highlight, a selected-file summary with
      remove/change, an early type/size check, and the "reading your document"
      state while the upload + OCR request runs.
   2. Every form: the submitting-state guard shared with auth.js/profile.js
      (disable THAT form's submit button and show a spinner, so a double-click
      cannot submit twice - the server also makes confirm/process idempotent).
   3. Document pages: while a document is being read, ask the API for its real status (doc-status.js)
      and update only what changed - no page reload, nothing typed or scrolled is lost.
   4. Upload page: sent with fetch so a failure can be explained without losing the selected file,
      a second submit is ignored, and a dropped connection is checked against the server before
      the person is asked to upload again (so no duplicate document). */

(function () {
  "use strict";

  // ── 3. Live status (no page reloads) ─────────────────────────────────
  // doc-status.js does the polling; this part connects it to the page. It only ever shows
  // what the API says: a document is `processing` or it is not - no stage is invented.
  var pollers = [];       // every active poller, so pagehide can stop them all
  var lastAnnounced = ""; // the last text sent to the live region (never repeated)

  function $(selector, root) { return (root || document).querySelector(selector); }

  function fill(template, params) {
    return Object.keys(params).reduce(function (text, key) {
      return text.split("{" + key + "}").join(params[key]);
    }, template || "");
  }

  function liveRegion() { return document.getElementById("doc-live"); }

  function liveText(name) {
    var region = liveRegion();
    return (region && region.getAttribute("data-" + name)) || "";
  }

  function statusLabel(status) { return liveText("label-" + status) || status; }

  // One screen-reader announcement per CHANGE: the same text twice in a row is dropped, so a
  // poll that finds nothing new says nothing.
  function announce(text) {
    var region = liveRegion();
    if (!region || !text || text === lastAnnounced) return;
    lastAnnounced = text;
    region.textContent = text;
  }

  function showProblem(text, opts) {
    opts = opts || {};
    var banner = $("[data-doc-problem]");
    if (banner) {
      $("[data-doc-problem-text]", banner).textContent = text;
      var check = $("[data-doc-check-now]", banner);
      var reload = $("[data-doc-reload]", banner);
      if (check) check.hidden = !!opts.reload;
      if (reload) {
        reload.hidden = !opts.reload;
        reload.setAttribute("href", window.location.href);
      }
      banner.hidden = false;
    }
    announce(text);
  }

  function hideProblem() {
    var banner = $("[data-doc-problem]");
    if (banner) banner.hidden = true;
  }

  function onProblem(kind) {
    if (kind === "temporary") showProblem(liveText("msg-temporary"));
    else if (kind === "signed_out") showProblem(liveText("msg-signed-out"), { reload: true });
    else if (kind === "gone") showProblem(liveText("msg-gone"), { reload: true });
    else if (kind === "slow") {
      var slow = $("[data-doc-slow]");
      if (slow) slow.hidden = false;
      announce(liveText("msg-slow"));
    }
  }

  function onRecovered() {
    hideProblem();
    announce(liveText("msg-recovered"));
  }

  function showChecked() {
    var line = $("[data-doc-checked]");
    if (!line) return;
    var time = new Date().toLocaleTimeString(document.documentElement.lang || undefined);
    line.textContent = fill(liveText("msg-checked"), { time: time });
    line.hidden = false;
  }

  // Fetches this page again and returns its HTML as a document, or null when the answer is not
  // this page (an error, or a redirect to the sign-in page).
  function fetchPage() {
    return window.fetch(window.location.href, {
      credentials: "same-origin",
      cache: "no-store",
      headers: { Accept: "text/html" }
    }).then(function (res) {
      if (!res.ok) return null;
      if (res.redirected && new URL(res.url).pathname !== window.location.pathname) return null;
      return res.text();
    }).then(function (html) {
      if (!html) return null;
      return new DOMParser().parseFromString(html, "text/html");
    }).catch(function () { return null; });
  }

  // The page changed in place: new forms and links need their handlers (each is bound once).
  function reinitAfterSwap() {
    initSubmitGuard();
    initDeleteDialog();
  }

  function moveFocusTo(root) {
    var target = $(".entry-row__title", root) || $("h1, h2", root) || root;  // the document's own name line
    if (!target.hasAttribute("tabindex")) target.setAttribute("tabindex", "-1");
    try { target.focus({ preventScroll: true }); } catch (e) { target.focus(); }
  }

  // Detail page: while `processing` it asks the API; once the status changes the page's main
  // block (a different screen - review, failed, saved) is swapped in place.
  function initDetailPoll() {
    var main = $("main[data-doc-poll]");
    if (!main || !window.DocStatus || !window.fetch) return;
    var poller = window.DocStatus.createPoller({
      url: main.getAttribute("data-doc-poll"),
      fetch: window.fetch.bind(window),
      isHidden: function () { return document.hidden; },
      signature: function (data) { return data.status; },
      onProblem: onProblem,
      onRecovered: onRecovered,
      onStatus: function (data) {
        if (data.status === "processing") { showChecked(); return false; }
        announce(fill(liveText("msg-done"), { status: statusLabel(data.status) }));
        fetchPage().then(function (page) {
          var fresh = page && $("main.content", page);
          var current = $("main.content");
          if (!fresh || !current) { window.location.reload(); return; }
          current.innerHTML = fresh.innerHTML;
          if (fresh.hasAttribute("data-doc-poll")) {
            current.setAttribute("data-doc-poll", fresh.getAttribute("data-doc-poll"));
          } else {
            current.removeAttribute("data-doc-poll");
          }
          reinitAfterSwap();
          moveFocusTo(current);
          initDetailPoll();  // a no-op unless the document is somehow still being read
        });
        return true;
      }
    });
    pollers.push(poller);
    poller.start();
  }

  // List page: one request for the in-progress list; when a row that was being read has
  // changed, only the two data-live-region blocks are replaced (the filter form is untouched).
  function initListPoll() {
    var main = $("main[data-doc-list-poll]");
    if (!main || !window.DocStatus || !window.fetch) return;
    var swapping = false;

    function processingRows() {
      return Array.prototype.slice.call(
        document.querySelectorAll('li[data-doc-status="processing"][data-doc-id]')
      );
    }

    var poller = window.DocStatus.createPoller({
      url: main.getAttribute("data-doc-list-poll"),
      fetch: window.fetch.bind(window),
      isHidden: function () { return document.hidden; },
      signature: function (list) {
        return list.map(function (d) { return d.id + ":" + d.status; }).join("|");
      },
      onProblem: onProblem,
      onRecovered: onRecovered,
      onStatus: function (list) {
        showChecked();
        if (swapping) return false;
        var byId = {};
        list.forEach(function (d) { byId[d.id] = d; });
        var changed = processingRows().filter(function (li) {
          var d = byId[li.getAttribute("data-doc-id")];
          return !d || d.status !== "processing";
        });
        if (!changed.length) return false;
        var messages = changed.map(function (li) {
          var d = byId[li.getAttribute("data-doc-id")];
          return fill(liveText("msg-changed"), {
            name: li.getAttribute("data-doc-name") || "",
            status: statusLabel(d ? d.status : "cancelled")
          });
        });
        swapping = true;
        fetchPage().then(function (page) {
          swapping = false;
          var current = $("main.content");
          if (!page || !current) {
            // Could not get the new list: say so, do not reload (that would drop the filters).
            showProblem(liveText("msg-swap-failed"), { reload: true });
            poller.stop();
            return;
          }
          Array.prototype.forEach.call(
            document.querySelectorAll("[data-live-region]"),
            function (region) {
              var fresh = $('[data-live-region="' + region.getAttribute("data-live-region") + '"]', page);
              if (fresh) region.innerHTML = fresh.innerHTML;
            }
          );
          var freshMain = $("main.content", page);
          if (!freshMain || !freshMain.hasAttribute("data-doc-list-poll")) {
            current.removeAttribute("data-doc-list-poll");
            hideProblem();
            poller.stop();
          }
          reinitAfterSwap();
          announce(messages.join(". "));
        });
        return false;
      }
    });
    pollers.push(poller);
    poller.start();
  }

  function initLiveStatus() {
    initDetailPoll();
    initListPoll();

    // "Check again": asks now when a poller is running; otherwise the link / button does its
    // ordinary job (the link reloads the page, which still works without JavaScript).
    document.addEventListener("click", function (event) {
      var target = event.target.closest ? event.target.closest("[data-doc-check-now]") : null;
      if (!target) return;
      var active = pollers.filter(function (p) { return p.isRunning(); });
      if (active.length) {
        event.preventDefault();
        active.forEach(function (p) { p.checkNow(); });
      } else if (target.tagName === "BUTTON") {
        window.location.reload();
      }
    });

    document.addEventListener("visibilitychange", function () {
      if (!document.hidden) pollers.forEach(function (p) { p.resume(); });
    });
    // Leaving the page (or it being frozen in the back/forward cache): nothing keeps running.
    window.addEventListener("pagehide", function () {
      pollers.forEach(function (p) { p.stop(); });
      pollers = [];
    });
    // Coming back to a frozen copy of the page: ask for the real status instead of trusting it.
    window.addEventListener("pageshow", function (event) {
      if (!event.persisted) return;
      pollers.forEach(function (p) { p.stop(); });
      pollers = [];
      initDetailPoll();
      initListPoll();
    });
  }

  // ── 2. Submitting-state guard ────────────────────────────────────────
  function initSubmitGuard() {
    document.querySelectorAll("form").forEach(function (form) {
      if (form.getAttribute("data-guarded")) return;   // each form is bound once, even after a swap
      form.setAttribute("data-guarded", "1");
      form.addEventListener("submit", function (event) {
        if (event.defaultPrevented) return;
        var btn = form.querySelector('button[type="submit"]');
        if (!btn || btn.disabled) return;
        btn.disabled = true;
        btn.setAttribute("aria-busy", "true");
        btn.innerHTML =
          '<span class="spinner" aria-hidden="true"></span>' +
          "<span>" + (btn.dataset.label || "") + "</span>";
      });
    });
  }

  // ── 4. Delete confirmation dialog ────────────────────────────────────
  // Every "Delete" control is a link to a confirmation PAGE. Where <dialog> is supported the
  // click opens the one dialog on the page instead, filled in for that document; the answer is
  // the same POST either way and the server decides whether the deletion is allowed.
  function initDeleteDialog() {
    var dialog = document.getElementById("delete-dialog");
    if (!dialog || typeof dialog.showModal !== "function") return;
    var firstTime = !dialog.getAttribute("data-ready");   // links can be swapped in later; the dialog is bound once
    dialog.setAttribute("data-ready", "1");
    var form = dialog.querySelector("form");
    var nameEl = dialog.querySelector("[data-delete-dialog-name]");
    var savedOnly = dialog.querySelectorAll('[data-when="saved"]');
    var unsavedOnly = dialog.querySelectorAll('[data-when="unsaved"]');
    var box = dialog.querySelector('input[type="checkbox"][name="confirm"]');
    var hidden = dialog.querySelector('input[type="hidden"][name="confirm"]');

    function show(list, visible) {
      list.forEach(function (el) { el.hidden = !visible; });
    }

    document.querySelectorAll("[data-delete-url]").forEach(function (link) {
      if (link.getAttribute("data-bound")) return;
      link.setAttribute("data-bound", "1");
      link.addEventListener("click", function (event) {
        event.preventDefault();
        var saved = link.getAttribute("data-delete-saved") === "1";
        form.setAttribute("action", link.getAttribute("data-delete-url"));
        nameEl.textContent = link.getAttribute("data-delete-name") || "";
        show(savedOnly, saved);
        show(unsavedOnly, !saved);
        // A saved record needs the box ticked; an unsaved upload is confirmed by the button.
        box.checked = false;
        box.disabled = !saved;
        box.required = saved;
        hidden.disabled = saved;
        dialog.showModal();
      });
    });
    if (!firstTime) return;
    dialog.querySelectorAll("[data-dialog-close]").forEach(function (button) {
      button.addEventListener("click", function () { dialog.close(); });
    });
    dialog.addEventListener("click", function (event) {
      if (event.target === dialog) dialog.close(); // a click on the backdrop
    });
  }

  // ── 1. Upload page ───────────────────────────────────────────────────
  function initUpload() {
  var form = document.getElementById("upload-form");
  if (!form) return;

  var input = document.getElementById("f-file");
  var zone = document.getElementById("dropzone");
  var picked = document.getElementById("file-picked");
  var nameEl = document.getElementById("file-name");
  var sizeEl = document.getElementById("file-size");
  var removeBtn = document.getElementById("file-remove");
  var errBox = document.getElementById("e-file");
  var errText = document.getElementById("e-file-text");
  var processing = document.getElementById("processing-state");
  var maxBytes = parseInt(form.getAttribute("data-max-bytes"), 10);
  var ALLOWED = /\.(pdf|jpe?g|png)$/i;

  function showError(message) {
    errText.textContent = message;
    errBox.hidden = !message;
    input.setAttribute("aria-invalid", message ? "true" : "false");
  }

  function humanSize(bytes) {
    return bytes < 1024 * 1024
      ? Math.max(1, Math.round(bytes / 1024)) + " KB"
      : (bytes / (1024 * 1024)).toFixed(1) + " MB";
  }

  // Returns an error message, or "" when the file looks acceptable.
  function check(file) {
    if (!file) return form.getAttribute("data-msg-required");
    if (!ALLOWED.test(file.name)) return form.getAttribute("data-msg-type");
    if (file.size > maxBytes) return form.getAttribute("data-msg-size");
    return "";
  }

  function refresh() {
    var file = input.files && input.files[0];
    if (!file) {
      picked.hidden = true;
      return;
    }
    nameEl.textContent = file.name;
    sizeEl.textContent = humanSize(file.size);
    picked.hidden = false;
    showError(check(file));
  }

  input.addEventListener("change", refresh);

  removeBtn.addEventListener("click", function () {
    input.value = "";
    picked.hidden = true;
    showError("");
    input.focus();
  });

  ["dragenter", "dragover"].forEach(function (type) {
    zone.addEventListener(type, function (event) {
      event.preventDefault();
      zone.classList.add("is-dragover");
    });
  });
  ["dragleave", "drop"].forEach(function (type) {
    zone.addEventListener(type, function () { zone.classList.remove("is-dragover"); });
  });
  zone.addEventListener("drop", function (event) {
    event.preventDefault();
    if (event.dataTransfer && event.dataTransfer.files.length) {
      input.files = event.dataTransfer.files;
      refresh();
    }
  });

  // The upload is ONE request: the server stores the file, reads it and answers with a redirect
  // to the document's page, so there are no separate "uploading" and "reading" stages to
  // report - the screen says exactly that. Sent with fetch so that a failure can be explained
  // WITHOUT losing the form: the selected file and the chosen type stay where they are.
  var submitting = false;     // one upload at a time: a second click or Enter does nothing
  var UPLOAD_TIMEOUT_MS = 180000;

  function setBusy(busy) {
    submitting = busy;
    form.hidden = busy;            // hidden, not removed: the chosen file stays in the input
    processing.hidden = !busy;
    form.setAttribute("aria-busy", busy ? "true" : "false");
  }

  function showProblemBox(message, showList) {
    var box = document.getElementById("upload-problem");
    if (!box) return;
    document.getElementById("upload-problem-text").textContent = message;
    var list = document.getElementById("upload-problem-list");
    if (list) list.hidden = !showList;
    box.hidden = false;
    box.focus();   // role="alert" announces it; focus puts keyboard users next to it
  }

  function clearProblemBox() {
    var box = document.getElementById("upload-problem");
    if (box) box.hidden = true;
  }

  // What the server's own page says went wrong (it re-renders this form with the messages).
  function readServerProblem(html) {
    var page = new DOMParser().parseFromString(html, "text/html");
    var fileBox = page.getElementById("e-file");
    var fileText = fileBox && !fileBox.hidden ? (page.getElementById("e-file-text") || fileBox).textContent.trim() : "";
    var typeBox = page.getElementById("e-document_type");
    var banner = page.querySelector('main .banner--error[role="alert"] > span:last-child');
    var other = [typeBox ? typeBox.textContent.replace(/^!/, "").trim() : "",
                 banner ? banner.textContent.trim() : ""].filter(Boolean).join(" ");
    return { file: fileText, other: other };
  }

  // The connection failed (or timed out) after the file may or may not have arrived. Before
  // asking the person to upload again - which could create a second copy - look for a document
  // with this name and size that was just created.
  function findJustUploaded(file, startedAt) {
    var controller = typeof AbortController === "function" ? new AbortController() : null;
    var timer = window.setTimeout(function () { if (controller) controller.abort(); }, 10000);
    return window.fetch("/api/v1/documents/in-progress?limit=50", {
      credentials: "same-origin",
      cache: "no-store",
      headers: { Accept: "application/json" },
      signal: controller ? controller.signal : undefined
    }).then(function (res) {
      window.clearTimeout(timer);
      if (!res.ok) throw new Error("lookup");
      return res.json();
    }).then(function (items) {
      var match = items.filter(function (d) {
        return d.original_filename === file.name && d.size_bytes === file.size &&
               Date.parse(d.uploaded_at) >= startedAt - 30000;
      })[0];
      return { found: match || null };
    });
  }

  function fail(message, showList) {
    setBusy(false);
    showProblemBox(message, showList);
  }

  function submitUpload() {
    var file = input.files && input.files[0];
    var startedAt = Date.now();
    var controller = typeof AbortController === "function" ? new AbortController() : null;
    var timer = window.setTimeout(function () { if (controller) controller.abort(); }, UPLOAD_TIMEOUT_MS);
    clearProblemBox();
    setBusy(true);

    window.fetch(form.getAttribute("action"), {
      method: "POST",
      body: new FormData(form),
      credentials: "same-origin",
      headers: { Accept: "text/html" },
      signal: controller ? controller.signal : undefined
    }).then(function (res) {
      window.clearTimeout(timer);
      if (res.redirected) {              // created: the server sent us to the document's page
        window.location.assign(res.url);
        return;
      }
      return res.text().then(function (html) {
        var problem = readServerProblem(html);
        setBusy(false);
        if (problem.file) {
          showError(problem.file);
          input.focus();
        }
        if (problem.other || !problem.file) {
          showProblemBox(problem.other || form.getAttribute("data-msg-failed"), false);
        }
      });
    }).catch(function () {
      window.clearTimeout(timer);
      // The answer never arrived. Was the document created anyway?
      findJustUploaded(file, startedAt).then(function (result) {
        if (result.found) {
          window.location.assign("/" + document.documentElement.lang + "/patient/documents/" + result.found.id);
        } else {
          fail(form.getAttribute("data-msg-network"), false);
        }
      }, function () {
        fail(form.getAttribute("data-msg-unconfirmed"), true);
      });
    });
  }

  form.addEventListener("submit", function (event) {
    if (submitting) { event.preventDefault(); return; }   // repeated click / Enter
    var message = check(input.files && input.files[0]);
    if (message) {
      event.preventDefault();
      showError(message);
      return;
    }
    if (!window.fetch || !window.FormData) {
      // Very old browser: the plain form post still works, exactly as before.
      form.hidden = true;
      processing.hidden = false;
      return;
    }
    event.preventDefault();
    submitUpload();
  });
  }

  // Order matters: the upload page's own validation listener must be
  // registered BEFORE the submitting-state guard, so a blocked submit
  // (`preventDefault`) never leaves the button disabled.
  initUpload();
  initDeleteDialog();
  initSubmitGuard();
  initLiveStatus();
})();
