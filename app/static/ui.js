// Progressive enhancement for the review pages. Everything works without it (plain forms);
// with it, toggling a photo does not reload the page, moves show their progress in place and
// thumbnails open a full-size viewer. The page never reloads itself: what the user is doing
// (ticked photos, a name being typed, an open viewer) must survive a scan that finishes
// meanwhile. When the results changed, the banner offers a reload instead.
(function () {
  "use strict";
  function setText(el, s) { if (el.textContent !== s) el.textContent = s; }   // no DOM write for the same text

  // --- toggle include/exclude: instant, batched --------------------------------------------
  // The photo flips at once; requests go out one at a time per proposal, and clicks made while
  // one is in flight travel together in the next one. A page full of lazily loading thumbnails
  // otherwise queues every click behind the browser's connection limit, and the fourth click
  // waited for the first three to finish.
  var toggles = {};
  function flushToggles(pid) {
    var q = toggles[pid];
    if (!q || q.busy || !q.paths.length) return;
    var paths = q.paths;
    q.paths = [];
    q.busy = true;
    var fd = new FormData();
    paths.forEach(function (p) { fd.append("path", p); });
    fetch(q.action, {method: "POST", body: fd, headers: {"X-Requested-With": "fetch"}})
      .then(function (r) { return r.ok ? r.json() : Promise.reject(r.status); })
      .then(function (d) {
        var ex = {};
        (d.excluded_paths || []).forEach(function (p) { ex[p] = true; });
        q.card.querySelectorAll("figure[data-path]").forEach(function (f) {      // the server's view wins,
          var p = f.getAttribute("data-path");                                  // except for clicks still queued
          if (q.paths.indexOf(p) < 0) f.classList.toggle("excluded", !!ex[p]);
        });
        q.card.classList.remove("unsaved");
      })
      .catch(function () { q.card.classList.add("unsaved"); })
      .then(function () { q.busy = false; flushToggles(pid); });
  }
  document.addEventListener("click", function (ev) {
    var pick = ev.target.closest(".thumbs[data-toggle] .pick");
    if (!pick) return;
    ev.preventDefault();
    var grid = pick.closest(".thumbs"), fig = pick.closest("figure");
    var card = grid.closest(".card") || grid, action = grid.getAttribute("data-toggle");
    var pid = card.id || action;
    fig.classList.toggle("excluded");
    var q = toggles[pid] || (toggles[pid] = {paths: [], busy: false, action: action, card: card});
    q.paths.push(fig.getAttribute("data-path"));
    flushToggles(pid);
  });
  // --- photo lists load lazily: a collapsed list is empty until opened, "show all" gets the rest ---
  function loadMore(details) {
    if (details.dataset.loading) return;
    var loaded = parseInt(details.dataset.loaded, 10), total = parseInt(details.dataset.total, 10);
    var more = details.closest(".card").querySelector("[data-more]");     // gone once everything is loaded
    details.dataset.loading = "1";
    if (more) more.textContent = "loading…";
    fetch(details.getAttribute("data-photos") + "?offset=" + loaded)
      .then(function (r) { return r.ok ? r.text() : Promise.reject(r.status); })
      .then(function (html) {
        details.querySelector(".thumbs").insertAdjacentHTML("beforeend", html);
        var got = details.querySelectorAll(".thumbs figure").length;
        details.dataset.loaded = String(got);
        if (more) {
          if (got >= total) more.remove();
          else more.textContent = "show all " + total + " photos (" + (total - got) + " more)";
        }
      })
      .catch(function () { if (more) more.textContent = "could not load the photos — try again"; })
      .then(function () { delete details.dataset.loading; });
  }
  document.querySelectorAll("details[data-photos]").forEach(function (d) {
    d.addEventListener("toggle", function () { if (d.open && d.dataset.loaded === "0") loadMore(d); });
  });
  document.addEventListener("click", function (ev) {
    var more = ev.target.closest("[data-more]");
    if (!more) return;
    var details = more.closest(".card") ? more.closest(".card").querySelector("details[data-photos]") : null;
    if (details) { details.open = true; loadMore(details); }
  });

  // photos are plain elements (no input or button per photo: password-manager extensions watch
  // every form control on the page); Enter and Space work on them like on a button
  document.addEventListener("keydown", function (ev) {
    if ((ev.key === "Enter" || ev.key === " ") && ev.target.matches(".pick, .view, .act, [data-select-all], [data-more]")) {
      ev.preventDefault();
      ev.target.click();
    }
  });
  // cluster page: one form per action; the clicked element fills in which photo
  document.addEventListener("click", function (ev) {
    var act = ev.target.closest("[data-act]");
    if (!act) return;
    var form = document.getElementById(act.getAttribute("data-act"));
    if (!form) return;
    if (act.hasAttribute("data-confirm") && !confirm(act.getAttribute("data-confirm"))) return;
    form.querySelector("input[name=" + act.getAttribute("data-field") + "]").value = act.getAttribute("data-value");
    form.submit();
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

  // --- one status poll per page: busy banner, move progress, "results changed" offer --------
  var busy = document.getElementById("busy");
  if (busy) {
    var text = document.getElementById("busy-text"), hint = document.getElementById("busy-hint");
    var page = busy.getAttribute("data-page");
    var runsSeen = parseInt(busy.getAttribute("data-runs"), 10), everydayWasMoving = false, reloading = false;
    if (isNaN(runsSeen)) runsSeen = null;                       // rendered with the count: a run that ends before
                                                                // the first poll answers is not missed
    var stateless = page === "dashboard" || page === "log" || page === "clusters";
    function fmt(a) {
      var c = a.current;
      return a.done + " / " + a.total + (c ? " · " + c.file + " (" + Math.round(c.bytes / 1048576) + " MB, " + c.seconds + " s)" : "");
    }
    function describe(s) {
      var parts = [];
      if (s.state && s.state.running) {
        var p = s.state.progress || {};
        if (p.phase === "scanning") parts.push("Scanning inbox… " + (p.total ? p.done + " / " + p.total + " new files" : "listing files"));
        else if (p.phase === "clustering") parts.push("Clustering…");
        else parts.push("Working…");
      }
      Object.keys(s.applying || {}).forEach(function (k) { parts.push("Moving " + s.applying[k].name + ": " + fmt(s.applying[k])); });
      if (s.approved && !Object.keys(s.applying || {}).length && !s.dry_run) parts.push(s.approved + " approved, queued");
      return parts.join(" · ");
    }
    function offerReload(msg) {
      // a page without user state just reloads (nothing to lose, no viewer open); any other page
      // keeps what the user is doing and shows the offer until they take it
      if (stateless && !document.querySelector(".viewer:not([hidden])")) { reloading = true; location.reload(); return; }
      hint.textContent = msg + " — reload to see it";
      hint.hidden = false;
      busy.hidden = false;                                   // shown now, not on the next poll
      if (!text.textContent) busy.classList.add("done");
    }
    function finished(el, what) {
      var card = el.closest(".card");
      if (!card || card.classList.contains("done")) return;
      card.classList.add("done");
      var failed = what.indexOf("failed") === 0;
      var badge = card.querySelector(".badge.warn") || card.querySelector(".badge:not(.trip):not(.local):not(.home)");
      if (badge) { badge.textContent = failed ? "failed" : "moved"; badge.className = "badge " + (failed ? "bad" : "ok"); }
      el.textContent = failed ? what : "done";                          // the card says it; no reload offer
    }
    function progressCards(s) {
      document.querySelectorAll("[data-progress]").forEach(function (el) {
        var pid = el.getAttribute("data-progress");
        var a = (s.applying || {})[pid];
        if (a) { setText(el, fmt(a)); el.dataset.seen = "1"; return; }
        if (pid === "everyday") {
          if (!s.everyday_queued && (el.dataset.seen || everydayWasMoving)) finished(el, "moved");
          return;
        }
        var q = (s.queue || {})[pid];
        if (q === "queued") { el.dataset.seen = "1"; return; }
        if (q) { finished(el, q); return; }                              // "failed: ..."
        if (el.dataset.seen) finished(el, "moved");
        else el.dataset.seen = "1";                     // first sight: approved but not yet queued; next tick tells
      });
    }
    var timer = null;
    function schedule(fast) { clearTimeout(timer); timer = setTimeout(tick, fast ? 2000 : 5000); }
    function tick() {
      if (reloading) return;                                 // a reload is on its way: no second one
      if (document.hidden) { schedule(false); return; }
      fetch("/api/status").then(function (r) { return r.json(); }).then(function (s) {
        var msg = describe(s), isBusy = !!msg;
        setText(text, isBusy ? msg : "");
        if (busy.hidden !== (!isBusy && hint.hidden)) busy.hidden = !isBusy && hint.hidden;
        busy.classList.toggle("done", !isBusy && !hint.hidden);
        if (s.applying && s.applying.everyday) everydayWasMoving = true;
        progressCards(s);
        if (runsSeen === null) runsSeen = s.state.runs;
        else if (s.state.runs !== runsSeen && !s.state.running) {
          runsSeen = s.state.runs;
          if (page !== "settings") offerReload("Run finished, " + s.pending + " proposal" + (s.pending === 1 ? "" : "s") + " waiting");
        }
        schedule(isBusy);
      }).catch(function () { schedule(false); });
    }
    document.addEventListener("visibilitychange", function () { if (!document.hidden) { clearTimeout(timer); tick(); } });
    hint.addEventListener("click", function (ev) { ev.preventDefault(); location.reload(); });
    tick();
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

  // --- a long <select>: a filter box hides the options that do not match -------------------------
  document.querySelectorAll("input[data-filter]").forEach(function (box) {
    var select = document.getElementById(box.getAttribute("data-filter"));
    if (!select) return;
    box.addEventListener("input", function () {
      var q = box.value.trim().toLowerCase();
      select.querySelectorAll("option").forEach(function (o) {
        o.hidden = !!q && o.value !== "new" && o.textContent.toLowerCase().indexOf(q) < 0;
      });
      select.querySelectorAll("optgroup").forEach(function (g) {
        g.hidden = !g.querySelector("option:not([hidden])");
      });
      var chosen = select.options[select.selectedIndex];
      if (chosen && chosen.hidden) {                                        // the first match becomes the choice
        var first = select.querySelector("option:not([hidden]):not([value=new])");
        if (first) select.value = first.value;
      }
    });
  });

  // --- everyday page: selection is a class on the figure; the paths join the form on submit ---
  function setSelected(fig, on) {
    fig.classList.toggle("selected", on);
    var pick = fig.querySelector(".pick");
    if (pick) pick.setAttribute("aria-checked", on ? "true" : "false");
  }
  function countSelected() {
    var c = document.getElementById("selcount");
    if (c) setText(c, String(document.querySelectorAll(".thumbs figure.selected").length));
  }
  document.addEventListener("click", function (ev) {
    var pick = ev.target.closest(".thumbs[data-select] .pick");
    if (pick) {
      ev.preventDefault();
      var fig = pick.closest("figure");
      setSelected(fig, !fig.classList.contains("selected"));
      countSelected();
      return;
    }
    var all = ev.target.closest("[data-select-all]");
    if (all) {
      var scope = all.closest(".card");                                   // a day's card on Everyday; the
      if (!scope.querySelector(".thumbs figure")) scope = all.closest("form") || document;  // bar on a cluster page
      var figs = scope.querySelectorAll(".thumbs figure");
      var every = Array.prototype.every.call(figs, function (f) { return f.classList.contains("selected"); });
      figs.forEach(function (f) { setSelected(f, !every); });
      countSelected();
    }
  });
  var assign = document.getElementById("assign");
  if (assign) {
    assign.addEventListener("submit", function () {
      assign.querySelectorAll("input[name=paths]").forEach(function (i) { i.remove(); });
      document.querySelectorAll(".thumbs figure.selected").forEach(function (f) {
        var i = document.createElement("input");
        i.type = "hidden"; i.name = "paths"; i.value = f.getAttribute("data-path");
        assign.appendChild(i);
      });
    });
  }
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
