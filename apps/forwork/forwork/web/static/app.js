/* ForWork - behaviours shared by every page.  board.js adds the board screen on top. */
(function () {
  "use strict";
  var FW = window.FW = {};
  FW.csrf = document.body.getAttribute("data-csrf") || "";

  FW.esc = function (s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  };

  // Colours from data become classes on a script-made stylesheet (the security policy allows no inline styles).
  var sheet = null, made = {};
  function ruleSheet() {
    if (sheet) return sheet;
    try {
      sheet = new CSSStyleSheet();
      document.adoptedStyleSheets = document.adoptedStyleSheets.concat([sheet]);
    } catch (e) {
      for (var i = 0; i < document.styleSheets.length; i++) {
        try { if (document.styleSheets[i].cssRules) { sheet = document.styleSheets[i]; break; } } catch (e2) { /* other origin */ }
      }
    }
    return sheet;
  }
  FW.cls = function (prop, value) {
    value = String(value || "");
    if (!value) return "";
    var key = prop + ":" + value;
    if (made[key]) return made[key];
    var name = "v" + Object.keys(made).length.toString(36);
    if (prop === "--c" && !/^#[0-9a-fA-F]{3,8}$/.test(value)) return "";
    if (prop === "--w" && !/^\d{1,3}(\.\d+)?%$/.test(value)) return "";
    if (prop === "width" && !/^\d{1,5}px$/.test(value)) return "";
    if (["--c", "--w", "width"].indexOf(prop) < 0) return "";
    try { ruleSheet().insertRule("." + name + "{" + prop + ":" + value + "}", ruleSheet().cssRules.length); } catch (e) { return ""; }
    made[key] = name;
    return name;
  };
  FW.c = function (color) { return FW.cls("--c", color); };
  FW.paint = function (root) {
    (root || document).querySelectorAll("[data-c]").forEach(function (el) {
      var k = FW.c(el.getAttribute("data-c"));
      if (k) el.classList.add(k);
      el.removeAttribute("data-c");
    });
  };

  FW.api = function (method, url, body) {
    var opts = { method: method, headers: { "X-CSRF-Token": FW.csrf, "Accept": "application/json" }, credentials: "same-origin" };
    if (body instanceof FormData) opts.body = body;
    else if (body !== undefined) { opts.headers["Content-Type"] = "application/json"; opts.body = JSON.stringify(body); }
    return fetch(url, opts).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (data) {
        if (!r.ok) {
          var err = new Error(data.error || ("The server said " + r.status));
          err.status = r.status;
          throw err;
        }
        return data;
      });
    }, function () { throw new Error("ForWork could not reach the server - check your internet connection."); });
  };

  var toastTimer = null;
  FW.toast = function (msg, isError) {
    var t = document.getElementById("toast");
    if (!t) { t = document.createElement("div"); t.id = "toast"; t.className = "toast"; document.body.appendChild(t); }
    t.textContent = msg;
    t.classList.toggle("error", !!isError);
    t.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { t.hidden = true; }, isError ? 6000 : 2600);
  };

  FW.initials = function (name) {
    var p = String(name || "").split(/[\s.@_-]+/).filter(Boolean);
    if (!p.length) return "?";
    return (p[0][0] + (p.length > 1 ? p[p.length - 1][0] : "")).toUpperCase();
  };

  FW.date = function (iso, withYear) {
    if (!iso) return "";
    var d = new Date(iso.length > 10 ? iso : iso + "T00:00:00");
    if (isNaN(d)) return iso;
    var opts = { day: "numeric", month: "short" };
    if (withYear !== false && (withYear || d.getFullYear() !== new Date().getFullYear())) opts.year = "numeric";
    if (iso.length > 10) { opts.hour = "numeric"; opts.minute = "2-digit"; }
    return d.toLocaleString("en-AU", opts);
  };
  FW.when = function (iso) {
    if (!iso) return "";
    var d = new Date(iso), now = new Date(), s = (now - d) / 1000;
    if (s < 60) return "just now";
    if (s < 3600) return Math.floor(s / 60) + " min ago";
    if (s < 86400 && d.getDate() === now.getDate()) return Math.floor(s / 3600) + " h ago";
    return d.toLocaleString("en-AU", { day: "numeric", month: "short", year: d.getFullYear() !== now.getFullYear() ? "numeric" : undefined,
                                       hour: "numeric", minute: "2-digit" });
  };

  FW.paint();

  // close open <details> menus when clicking elsewhere
  document.addEventListener("click", function (ev) {
    document.querySelectorAll("details.user-menu[open], details.menu-wrap[open]").forEach(function (d) {
      if (!d.contains(ev.target)) d.removeAttribute("open");
    });
  });

  // confirmation before actions; autosubmit selects and checkboxes
  document.addEventListener("submit", function (ev) {
    var msg = (ev.submitter && ev.submitter.getAttribute("data-confirm")) || ev.target.getAttribute("data-confirm");
    if (msg && !window.confirm(msg)) ev.preventDefault();
  });
  document.querySelectorAll("[data-autosubmit]").forEach(function (el) {
    el.addEventListener("change", function () { el.form.requestSubmit ? el.form.requestSubmit() : el.form.submit(); });
  });

  // dialogs
  document.querySelectorAll("[data-dialog]").forEach(function (b) {
    b.addEventListener("click", function () {
      var d = document.getElementById(b.getAttribute("data-dialog"));
      if (d && d.showModal) d.showModal();
    });
  });
  document.addEventListener("click", function (ev) {
    var c = ev.target.closest("[data-close]");
    if (c) { var d = c.closest("dialog"); if (d) d.close(); }
  });

  // filter a list or table by its rows' data-text
  document.querySelectorAll("[data-filter-list]").forEach(function (input) {
    var box = document.getElementById(input.getAttribute("data-filter-list"));
    if (!box) return;
    input.addEventListener("input", function () {
      var q = input.value.trim().toLowerCase();
      box.querySelectorAll("[data-text]").forEach(function (row) {
        row.classList.toggle("hidden", !!q && row.getAttribute("data-text").indexOf(q) < 0);
      });
    });
  });

  // find a board in the sidebar
  var sideFilter = document.querySelector("[data-side-filter]");
  if (sideFilter) {
    sideFilter.addEventListener("input", function () {
      var q = sideFilter.value.trim().toLowerCase();
      document.querySelectorAll(".sidebar .board-link").forEach(function (a) {
        a.classList.toggle("hidden", !!q && a.getAttribute("data-name").indexOf(q) < 0);
      });
      if (q) document.querySelectorAll(".sidebar details").forEach(function (d) { d.open = true; });
    });
  }
  var navToggle = document.querySelector("[data-toggle-nav]");
  if (navToggle) navToggle.addEventListener("click", function () { document.body.classList.toggle("nav-open"); });

  // new board dialog: folders of the chosen workspace; "add workspace/folder with this name"
  var wsSelect = document.querySelector("[data-folder-select]");
  if (wsSelect) {
    var folderSel = wsSelect.form.querySelector("select[name=folder]");
    var sync = function () {
      folderSel.querySelectorAll("option[data-ws]").forEach(function (o) { o.hidden = o.getAttribute("data-ws") !== wsSelect.value; });
      if (folderSel.selectedOptions[0] && folderSel.selectedOptions[0].hidden) folderSel.value = "";
    };
    wsSelect.addEventListener("change", sync);
    sync();
  }

  // unread badge stays current
  var badge = document.querySelector("[data-unread-badge]");
  if (badge && document.body.getAttribute("data-me")) {
    setInterval(function () {
      if (document.hidden) return;
      FW.api("GET", "/api/notifications").then(function (d) {
        var b = badge.querySelector(".badge");
        if (d.unread && !b) { b = document.createElement("span"); b.className = "badge"; badge.appendChild(b); }
        if (b) { if (d.unread) b.textContent = d.unread > 99 ? "99+" : d.unread; else b.remove(); }
      }).catch(function () { /* offline for a moment */ });
    }, 60000);
  }

  // admin: restore dialog
  document.querySelectorAll("[data-restore]").forEach(function (b) {
    b.addEventListener("click", function () {
      var d = document.getElementById("restore-dialog");
      d.querySelector("[data-restore-name]").textContent = b.getAttribute("data-restore");
      d.querySelector("[data-restore-input]").value = b.getAttribute("data-restore");
      d.showModal();
    });
  });

  // admin: import progress
  var job = document.getElementById("import-job");
  if (job && job.getAttribute("data-job")) {
    job.hidden = false;
    var url = job.getAttribute("data-url").replace("JOB", job.getAttribute("data-job"));
    var poll = function () {
      FW.api("GET", url).then(function (j) {
        job.querySelector("[data-job-message]").textContent = j.error ? "Stopped: " + j.error : j.message;
        var p = job.querySelector("[data-job-progress]");
        if (j.total) { p.max = j.total; p.value = j.done; } else { p.removeAttribute("value"); }
        if (j.status === "done") {
          var r = j.report || {};
          p.max = 1; p.value = 1;
          job.querySelector("[data-job-result]").innerHTML =
            "<p class='ok-text'>Imported " + r.boards + " boards, " + r.items + " items, " + r.subitems + " subitems and " +
            r.updates + " updates.</p>" + (r.skipped_boards && r.skipped_boards.length ? "<p class='muted small'>Skipped: " +
            FW.esc(r.skipped_boards.join(", ")) + "</p>" : "") + (r.warnings && r.warnings.length ?
            "<details><summary>" + r.warnings.length + " notes</summary><ul class='small'>" +
            r.warnings.map(function (w) { return "<li>" + FW.esc(w) + "</li>"; }).join("") + "</ul></details>" : "");
        } else if (j.status !== "error") {
          setTimeout(poll, 1500);
        }
      }).catch(function (e) { job.querySelector("[data-job-message]").textContent = e.message; });
    };
    poll();
  }

  // automations form: show the parts for the chosen trigger/action; labels of the chosen status column
  document.querySelectorAll("[data-show-part]").forEach(function (sel) {
    var part = sel.getAttribute("data-show-part");
    var sync = function () {
      sel.form.querySelectorAll("[data-part='" + part + "']").forEach(function (el) {
        var on = el.getAttribute("data-when") === sel.value;
        el.hidden = !on;
        el.querySelectorAll("select, input").forEach(function (i) { i.disabled = !on; });
      });
    };
    sel.addEventListener("change", sync);
    sync();
  });
  document.querySelectorAll("[data-labels-for]").forEach(function (colSel) {
    var labSel = colSel.form.querySelector("[name='" + colSel.getAttribute("data-labels-for") + "']");
    var sync = function () {
      var first = null;
      labSel.querySelectorAll("option[data-col]").forEach(function (o) {
        var on = o.getAttribute("data-col") === colSel.value;
        o.hidden = !on;
        if (on && !first) first = o;
      });
      if (labSel.selectedOptions[0] && labSel.selectedOptions[0].hidden) labSel.value = labSel.querySelector("option:not([data-col])") ? "any" : (first ? first.value : "");
    };
    colSel.addEventListener("change", sync);
    sync();
  });
})();
