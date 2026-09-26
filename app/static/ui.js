// Progressive enhancement for the review pages. Everything works without it (plain forms);
// with it, toggling a photo does not reload the page, and thumbnails open a full-size viewer.
(function () {
  "use strict";

  // --- toggle include/exclude in place -------------------------------------------------
  document.addEventListener("submit", function (ev) {
    var form = ev.target;
    if (!form.matches("form[data-toggle]")) return;
    ev.preventDefault();
    var fig = form.closest("figure");
    fetch(form.action, {method: "POST", body: new FormData(form), headers: {"X-Requested-With": "fetch"}})
      .then(function (r) { return r.ok ? r.json() : Promise.reject(r.status); })
      .then(function (d) { fig.classList.toggle("excluded", !!d.excluded); })
      .catch(function () { form.submit(); });               // fall back to the full round trip
  });

  // --- rename without a button: saved as you type (proposals) or when the field is left (folders)
  function saveName(form) {
    var input = form.querySelector("input[name=name]");
    var state = form.querySelector(".save-state");
    if (!input || input.value === form.dataset.saved) return;
    if (state) state.textContent = "saving…";
    fetch(form.action, {method: "POST", body: new FormData(form), headers: {"X-Requested-With": "fetch"}})
      .then(function (r) { return r.ok ? r.json() : Promise.reject(r); })
      .then(function (d) {
        form.dataset.saved = input.value;
        if (d.redirect) { location.replace(d.redirect); return; }
        if (d.name && d.name !== input.value && document.activeElement !== input) input.value = d.name;
        if (state) { state.textContent = "saved"; setTimeout(function () { if (state.textContent === "saved") state.textContent = ""; }, 1500); }
      })
      .catch(function (err) {
        if (state) state.textContent = "not saved";
        if (err && err.json) err.json().then(function (d) { if (state && d.error) state.textContent = "not saved: " + d.error; });
      });
  }
  document.querySelectorAll("form[data-autosave]").forEach(function (form) {
    var input = form.querySelector("input[name=name]");
    if (!input) return;
    form.dataset.saved = input.value;
    var timer = null;
    if (!form.hasAttribute("data-reload")) {                       // proposals: while typing, debounced
      input.addEventListener("input", function () { clearTimeout(timer); timer = setTimeout(function () { saveName(form); }, 700); });
    }
    input.addEventListener("change", function () { clearTimeout(timer); saveName(form); });   // field left / Enter
    form.addEventListener("submit", function (ev) { ev.preventDefault(); clearTimeout(timer); saveName(form); });
  });

  // approve/reject right after editing the name: send the name along instead of racing the autosave
  document.addEventListener("submit", function (ev) {
    var form = ev.target;
    if (!/\/proposal\/[^/]+\/(approve|reject)$/.test(form.getAttribute("action") || "")) return;
    var card = form.closest(".card");
    var nameForm = card && card.querySelector("form[data-autosave]");
    if (!nameForm) return;
    var input = nameForm.querySelector("input[name=name]");
    var remember = nameForm.querySelector("input[name=remember_place]");
    nameForm.dataset.saved = input.value;                        // stop a pending autosave from firing
    var hidden = document.createElement("input");
    hidden.type = "hidden"; hidden.name = "name"; hidden.value = input.value;
    form.appendChild(hidden);
    if (remember && remember.checked) {
      var h2 = document.createElement("input");
      h2.type = "hidden"; h2.name = "remember_place"; h2.value = "1";
      form.appendChild(h2);
    }
  }, true);

  // approve all: every card's current name (and remember-place box) travels with the request
  document.addEventListener("submit", function (ev) {
    var form = ev.target;
    if (!form.hasAttribute("data-approve-all")) return;
    document.querySelectorAll(".card[id] form[data-autosave]").forEach(function (nameForm) {
      var card = nameForm.closest(".card");
      var input = nameForm.querySelector("input[name=name]");
      var remember = nameForm.querySelector("input[name=remember_place]");
      if (!card || !input) return;
      nameForm.dataset.saved = input.value;
      var hidden = document.createElement("input");
      hidden.type = "hidden"; hidden.name = "name_" + card.id; hidden.value = input.value;
      form.appendChild(hidden);
      if (remember && remember.checked) {
        var h2 = document.createElement("input");
        h2.type = "hidden"; h2.name = "remember_place_" + card.id; h2.value = "1";
        form.appendChild(h2);
      }
    });
  }, true);

  // --- busy banner: scanning, clustering or moving, on every page ------------------------------
  var busy = document.getElementById("busy");
  if (busy) {
    var wasBusy = !busy.hidden, text = document.getElementById("busy-text");
    function describe(s) {
      var parts = [];
      if (s.state && s.state.running) {
        var p = s.state.progress || {};
        if (p.phase === "scanning") parts.push("Scanning inbox… " + (p.total ? p.done + " / " + p.total + " new files" : "listing files"));
        else if (p.phase === "clustering") parts.push("Clustering…");
        else parts.push("Working…");
      }
      Object.keys(s.applying || {}).forEach(function (k) {
        var a = s.applying[k], c = a.current;
        parts.push("Moving " + a.name + ": " + a.done + " / " + a.total + (c ? " · " + c.file + " (" + Math.round(c.bytes / 1048576) + " MB, " + c.seconds + " s)" : ""));
      });
      if (s.approved && !Object.keys(s.applying || {}).length && !s.dry_run) parts.push(s.approved + " approved, queued");
      return parts.join(" · ");
    }
    setInterval(function () {
      fetch("/api/status").then(function (r) { return r.json(); }).then(function (s) {
        var msg = describe(s), isBusy = !!msg;
        busy.hidden = !isBusy;
        if (isBusy) text.textContent = msg;
        var page = busy.getAttribute("data-page");
        if (wasBusy && !isBusy && page !== "settings" && page !== "everyday") location.reload();   // fresh counts
        wasBusy = isBusy;
      }).catch(function () {});
    }, 2000);
  }

  // --- moving in the background: refresh progress, reload when done -------------------------
  if (document.querySelector("[data-poll]")) {
    var poll = setInterval(function () {
      fetch("/api/status").then(function (r) { return r.json(); }).then(function (s) {
        Object.keys(s.applying || {}).forEach(function (pid) {
          var el = document.querySelector('[data-progress="' + pid + '"]');
          var a = s.applying[pid], c = a.current;
          if (el) el.textContent = a.done + " / " + a.total + (c ? " · " + c.file + " (" + Math.round(c.bytes / 1048576) + " MB, " + c.seconds + " s)" : "");
        });
        var everyday = document.querySelector('[data-poll="everyday"]');
        if (everyday && !(s.applying && s.applying.everyday)) { clearInterval(poll); location.reload(); }
        var shown = document.querySelectorAll('[data-poll="approved"] .card').length;
        if (shown && s.approved < shown) { clearInterval(poll); location.reload(); }
      }).catch(function () {});
    }, 2000);
  }

  // --- settings: switching dry-run off is the one action that lets files move ------------------
  var dry = document.querySelector("[data-dry-run]");
  if (dry) {
    var wasOn = dry.checked;
    dry.closest("form").addEventListener("submit", function (ev) {
      if (wasOn && !dry.checked && !confirm("Switch dry-run OFF? Approved proposals will be moved now, and further approvals move files immediately.")) {
        ev.preventDefault();
      }
    });
  }

  // --- everyday page: per-day select-all and the selected counter ------------------------
  function countSelected() {
    var c = document.getElementById("selcount");
    if (c) c.textContent = document.querySelectorAll("input[name=paths]:checked").length;
  }
  document.addEventListener("change", function (ev) {
    var t = ev.target;
    if (t.matches("[data-select-all]")) {
      t.closest(".card").querySelectorAll("input[name=paths]").forEach(function (cb) { cb.checked = t.checked; });
    }
    if (t.matches("[data-select-all], input[name=paths]")) countSelected();
  });
  countSelected();

  // --- viewer ---------------------------------------------------------------------------
  var box = document.createElement("div");
  box.className = "viewer";
  box.hidden = true;
  box.innerHTML = '<button class="v-close" title="close (Esc)">&times;</button>' +
    '<button class="v-prev" title="previous (&larr;)">&#8249;</button>' +
    '<div class="v-body"></div>' +
    '<button class="v-next" title="next (&rarr;)">&#8250;</button>' +
    '<div class="v-caption"></div>';
  document.body.appendChild(box);
  var body = box.querySelector(".v-body"), caption = box.querySelector(".v-caption");
  var items = [], idx = -1;

  function show(i) {
    if (i < 0 || i >= items.length) return;
    idx = i;
    var it = items[i];
    body.innerHTML = "";
    var el;
    if (it.type === "video") {
      el = document.createElement("video");
      el.controls = true; el.autoplay = true; el.playsInline = true;
      el.src = it.src;
    } else {
      el = document.createElement("img");
      el.src = it.src;
      el.alt = it.title;
    }
    body.appendChild(el);
    caption.textContent = it.title + "  (" + (i + 1) + "/" + items.length + ")";
    box.hidden = false;
    document.body.style.overflow = "hidden";
  }
  function close() {
    box.hidden = true;
    body.innerHTML = "";
    document.body.style.overflow = "";
  }
  document.addEventListener("click", function (ev) {
    var btn = ev.target.closest("[data-view]");
    if (btn) {
      ev.preventDefault();
      var group = btn.closest(".thumbs") || document;
      var all = Array.prototype.slice.call(group.querySelectorAll("[data-view]"));
      items = all.map(function (b) { return {src: b.getAttribute("data-view"), type: b.getAttribute("data-type"),
                                             title: b.getAttribute("data-title") || ""}; });
      show(all.indexOf(btn));
      return;
    }
    if (box.hidden) return;
    if (ev.target.closest(".v-close") || ev.target === box) close();
    else if (ev.target.closest(".v-prev")) show(idx - 1);
    else if (ev.target.closest(".v-next")) show(idx + 1);
  });
  document.addEventListener("keydown", function (ev) {
    if (box.hidden) return;
    if (ev.key === "Escape") close();
    else if (ev.key === "ArrowLeft") show(idx - 1);
    else if (ev.key === "ArrowRight") show(idx + 1);
  });
})();
