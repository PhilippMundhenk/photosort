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
      .then(function (r) { return r.ok ? r.json() : Promise.reject(r.status); })
      .then(function (d) {
        form.dataset.saved = input.value;
        if (d.redirect) { location.replace(d.redirect); return; }
        if (d.name && d.name !== input.value && document.activeElement !== input) input.value = d.name;
        if (state) { state.textContent = "saved"; setTimeout(function () { if (state.textContent === "saved") state.textContent = ""; }, 1500); }
      })
      .catch(function () { if (state) state.textContent = "not saved"; });
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

  // --- moving in the background: refresh progress, reload when done -------------------------
  if (document.querySelector("[data-poll]")) {
    var poll = setInterval(function () {
      fetch("/api/status").then(function (r) { return r.json(); }).then(function (s) {
        Object.keys(s.applying || {}).forEach(function (pid) {
          var el = document.querySelector('[data-progress="' + pid + '"]');
          if (el) el.textContent = s.applying[pid].done + " / " + s.applying[pid].total;
        });
        var shown = document.querySelectorAll("[data-poll] .card").length;
        if (s.approved < shown) { clearInterval(poll); location.reload(); }
      }).catch(function () {});
    }, 2000);
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
