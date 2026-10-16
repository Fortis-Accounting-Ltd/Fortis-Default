/* Fortis Ledger - small behaviours.  Every page works without this script; it only adds conveniences. */
(function () {
  "use strict";
  var csrf = document.body.getAttribute("data-csrf") || "";

  function parseAmount(s) {
    s = String(s || "").replace(/[\s,$]/g, "");
    if (!s || s === "-") return 0;
    var neg = false;
    if (/^\(.*\)$/.test(s)) { neg = true; s = s.slice(1, -1); }
    if (/-$/.test(s) && s.length > 1) { neg = !neg; s = s.slice(0, -1); }
    var n = Number(s);
    if (!isFinite(n)) return NaN;
    return Math.round((neg ? -n : n) * 100) / 100;
  }
  function money(n) {
    return Math.abs(n).toLocaleString("en-AU", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  }

  // selects that submit their form when changed (year picker, filters)
  document.querySelectorAll("[data-autosubmit]").forEach(function (el) {
    el.addEventListener("change", function () { el.form.submit(); });
  });
  // checkboxes that re-check a preview when changed
  document.querySelectorAll("[data-autosubmit-op]").forEach(function (el) {
    el.addEventListener("change", function () {
      var op = document.createElement("input");
      op.type = "hidden"; op.name = "op"; op.value = el.getAttribute("data-autosubmit-op");
      el.form.appendChild(op);
      el.form.submit();
    });
  });

  // confirmation before destructive actions; keep the clicked button's value; stop double submits
  document.querySelectorAll("form").forEach(function (form) {
    form.addEventListener("submit", function (ev) {
      var msg = form.getAttribute("data-confirm");
      if (msg && !window.confirm(msg)) { ev.preventDefault(); return; }
      // the clicked button's name/value is already in the submitted data; disabling afterwards is safe
      if ((form.method || "").toLowerCase() === "post") {
        setTimeout(function () {
          form.querySelectorAll("button[type=submit], button:not([type])").forEach(function (b) { b.disabled = true; });
        }, 0);
      }
    });
  });

  // filter a table's rows by text
  document.querySelectorAll("input[data-filter]").forEach(function (input) {
    var table = document.getElementById(input.getAttribute("data-filter"));
    if (!table) return;
    input.addEventListener("input", function () {
      var q = input.value.trim().toLowerCase();
      table.querySelectorAll("tbody tr").forEach(function (tr) {
        tr.classList.toggle("hidden", q && tr.textContent.toLowerCase().indexOf(q) < 0);
      });
    });
  });

  // fill an input from a suggestion button
  document.querySelectorAll("[data-fill]").forEach(function (btn) {
    btn.addEventListener("click", function () {
      var target = document.getElementById(btn.getAttribute("data-fill"));
      if (target) { target.value = btn.getAttribute("data-value"); target.focus(); }
    });
  });

  // folder picker - only inside the desktop window, where pywebview offers a native dialog
  function enableBrowse() {
    if (!(window.pywebview && window.pywebview.api && window.pywebview.api.choose_folder)) return;
    document.querySelectorAll("[data-browse-folder]").forEach(function (btn) {
      btn.classList.remove("hidden");
      btn.addEventListener("click", function () {
        window.pywebview.api.choose_folder().then(function (path) {
          if (path) document.getElementById(btn.getAttribute("data-browse-folder")).value = path;
        });
      });
    });
  }
  window.addEventListener("pywebviewready", enableBrowse);
  enableBrowse();

  // show only the client-detail fields that apply to the entity type
  var typeSel = document.querySelector("[data-entity-type]");
  function applyType() {
    var t = typeSel.value;
    document.querySelectorAll("[data-show-for]").forEach(function (el) {
      el.classList.toggle("hidden", el.getAttribute("data-show-for").split(" ").indexOf(t) < 0);
    });
  }
  if (typeSel) { typeSel.addEventListener("change", applyType); applyType(); }

  // keep the edit lock alive while a client is open; notice if someone takes it over
  var hb = document.body.getAttribute("data-heartbeat");
  if (hb) {
    setInterval(function () {
      fetch(hb, { method: "POST", headers: { "X-CSRF-Token": csrf }, credentials: "same-origin" })
        .then(function (r) { return r.ok ? r.json() : null; })
        .then(function (d) {
          if (d && d.readonly) {
            var b = document.getElementById("lock-lost");
            if (b) { b.textContent = d.user + " (" + d.machine + ") has taken over editing this client. Reload the page - your changes can no longer be saved."; b.classList.remove("hidden"); }
          }
        })
        .catch(function () { /* the app is closing */ });
    }, 60000);
  }

  // browser mode: tell the app a page is still open (it stops itself when nothing is)
  var alive = document.body.getAttribute("data-alive");
  if (alive) {
    setInterval(function () {
      fetch(alive, { method: "POST", headers: { "X-CSRF-Token": csrf }, credentials: "same-origin" }).catch(function () {});
    }, 60000);
  }

  // journal editor: live totals, add / remove lines, account names
  var lines = document.getElementById("journal-lines");
  if (lines) {
    var names = {};
    try { names = JSON.parse(document.getElementById("account-names").textContent); } catch (e) { names = {}; }
    var tbody = lines.querySelector("tbody");
    var tpl = document.getElementById("line-template");
    var next = tbody.querySelectorAll("tr").length;

    var totals = function () {
      var dr = 0, cr = 0, bad = false;
      tbody.querySelectorAll("tr").forEach(function (tr) {
        var d = parseAmount(tr.querySelector(".dr").value), c = parseAmount(tr.querySelector(".cr").value);
        if (isNaN(d) || isNaN(c)) { bad = true; return; }
        dr += d; cr += c;
      });
      document.getElementById("total-dr").textContent = money(dr);
      document.getElementById("total-cr").textContent = money(cr);
      var diff = Math.round((dr - cr) * 100) / 100, el = document.getElementById("difference");
      el.textContent = bad ? "check the amounts" : (diff === 0 ? "Balanced" : money(diff) + (diff > 0 ? " more debits" : " more credits"));
      el.className = "num " + (bad || diff !== 0 ? "bad" : "good");
    };
    var showName = function (input) {
      var span = input.parentNode.querySelector(".acct-name");
      var code = input.value.trim().split(" ")[0];
      if (span) span.textContent = names[code] || (code ? "not in the chart" : "");
    };
    var wire = function (tr) {
      tr.querySelectorAll(".amount").forEach(function (inp) {
        inp.addEventListener("input", totals);
        inp.addEventListener("blur", function () {
          var n = parseAmount(inp.value);
          if (inp.value.trim() && !isNaN(n)) inp.value = n === 0 ? "" : (n < 0 ? "-" : "") + money(n);
          totals();
        });
      });
      var acct = tr.querySelector(".account");
      acct.addEventListener("change", function () { acct.value = acct.value.trim().split(" ")[0]; showName(acct); });
      acct.addEventListener("input", function () { showName(acct); });
      tr.querySelector(".remove-line").addEventListener("click", function () {
        if (tbody.querySelectorAll("tr").length > 2) { tr.remove(); } else { tr.querySelectorAll("input").forEach(function (i) { i.value = ""; }); }
        totals();
      });
    };
    tbody.querySelectorAll("tr").forEach(wire);
    document.getElementById("add-line").addEventListener("click", function () {
      var html = tpl.innerHTML.replace(/__i__/g, String(next++));
      var holder = document.createElement("tbody");
      holder.innerHTML = html.trim();
      var tr = holder.firstElementChild;
      tbody.appendChild(tr);
      wire(tr);
      tr.querySelector(".account").focus();
    });
    totals();
  }

  // import preview: show the inputs that match each line's action
  document.querySelectorAll("[data-plan-action]").forEach(function (sel) {
    sel.addEventListener("change", function () {
      var tr = sel.closest("tr");
      tr.classList.remove("act-post", "act-new", "act-skip");
      tr.classList.add("act-" + sel.value);
    });
  });
})();
