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
   3. Processing page: reload itself until the document leaves `processing`. */

(function () {
  "use strict";

  // ── 3. Auto-refresh while the document is being read ─────────────────
  var auto = document.querySelector("[data-autorefresh]");
  if (auto) {
    var ms = parseInt(auto.getAttribute("data-autorefresh"), 10) || 4000;
    window.setTimeout(function () { window.location.reload(); }, ms);
  }

  // ── 2. Submitting-state guard ────────────────────────────────────────
  function initSubmitGuard() {
    document.querySelectorAll("form").forEach(function (form) {
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

  form.addEventListener("submit", function (event) {
    var message = check(input.files && input.files[0]);
    if (message) {
      event.preventDefault();
      showError(message);
      return;
    }
    // The request now uploads the file AND runs OCR, which can take a while:
    // swap the form for the processing state so nothing looks frozen.
    form.hidden = true;
    processing.hidden = false;
  });
  }

  // Order matters: the upload page's own validation listener must be
  // registered BEFORE the submitting-state guard, so a blocked submit
  // (`preventDefault`) never leaves the button disabled.
  initUpload();
  initDeleteDialog();
  initSubmitGuard();
})();
