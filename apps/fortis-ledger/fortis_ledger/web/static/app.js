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

  // confirmation before destructive actions (on the form, or on the button pressed); stop double submits
  document.querySelectorAll("form").forEach(function (form) {
    form.addEventListener("submit", function (ev) {
      var msg = (ev.submitter && ev.submitter.getAttribute("data-confirm")) || form.getAttribute("data-confirm");
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

  // sign-in in the browser (Microsoft 365, Xero): carry on by itself once it is finished there
  document.querySelectorAll("[data-poll]").forEach(function (el) {
    var url = el.getAttribute("data-poll"), done = el.getAttribute("data-done");
    var timer = setInterval(function () {
      fetch(url, { credentials: "same-origin", headers: { "Accept": "application/json" } })
        .then(function (r) { return r.ok ? r.json() : null; })
        .then(function (d) {
          if (!d || d.status === "waiting") return;
          clearInterval(timer);
          window.location.href = (d.status === "signed_in" && d.next) ? d.next : done;
        })
        .catch(function () { /* try again on the next tick */ });
    }, 1500);
  });

  // close the user menu when clicking elsewhere
  document.addEventListener("click", function (ev) {
    document.querySelectorAll("details.user-menu[open]").forEach(function (d) {
      if (!d.contains(ev.target)) d.removeAttribute("open");
    });
  });

  // browser mode: tell the app a page is still open (it stops itself when nothing is)
  var alive = document.body.getAttribute("data-alive");
  if (alive) {
    setInterval(function () {
      fetch(alive, { method: "POST", headers: { "X-CSRF-Token": csrf }, credentials: "same-origin" }).catch(function () {});
    }, 60000);
  }

  // the client's accounts: code -> [name, default tax rate]; the New account pop-up adds to it
  var accounts = {};
  var infoEl = document.getElementById("account-info");
  if (infoEl) { try { accounts = JSON.parse(infoEl.textContent); } catch (e) { accounts = {}; } }
  function accountCode(input) { return input.value.trim().split(" ")[0]; }
  function showName(input) {
    var span = input.parentNode.querySelector(".acct-name");
    var code = accountCode(input);
    if (span) span.textContent = accounts[code] ? accounts[code][0] : (code ? "not in the chart" : "");
  }

  // "New account" pop-up: account type, number and name (and tax rate); the new account fills the field it was opened for
  var dialog = document.getElementById("new-account");
  var lastAccountInput = null;
  document.addEventListener("focusin", function (ev) {
    if (ev.target.matches && ev.target.matches("input.account")) lastAccountInput = ev.target;
  });
  if (dialog && typeof dialog.showModal === "function") {
    var naForm = document.getElementById("new-account-form");
    var naType = document.getElementById("na-type"), naTax = document.getElementById("na-tax");
    var naRc = document.getElementById("na-rc"), naError = document.getElementById("na-error");
    var defaults = {};
    try { defaults = JSON.parse(document.getElementById("type-defaults").textContent); } catch (e) { defaults = {}; }
    var naTarget = null;
    var applyType = function () {
      var d = defaults[naType.value] || {};
      naRc.placeholder = d.report_code ? d.report_code + " (from the account type)" : "From the account type";
      if (naTax && d.tax) naTax.value = d.tax;
    };
    naType.addEventListener("change", applyType);
    document.querySelectorAll("[data-new-account]").forEach(function (btn) {
      btn.classList.remove("hidden");
      btn.addEventListener("click", function () {
        var t = btn.getAttribute("data-target");
        naTarget = t ? document.getElementById(t) : lastAccountInput;
        naForm.reset();
        naType.value = btn.getAttribute("data-type") || "Expense";
        applyType();
        naError.classList.add("hidden");
        dialog.showModal();
        document.getElementById("na-code").focus();
      });
    });
    dialog.querySelectorAll("[data-close-dialog]").forEach(function (b) {
      b.addEventListener("click", function () { dialog.close(); });
    });
    naForm.addEventListener("submit", function (ev) {
      ev.preventDefault();
      ev.stopImmediatePropagation();
      var submit = naForm.querySelector("button.primary");
      submit.disabled = true;
      fetch(naForm.action, { method: "POST", body: new FormData(naForm), credentials: "same-origin",
                             headers: { "X-Requested-With": "fetch", "Accept": "application/json" } })
        .then(function (r) {
          return r.json().catch(function () { return { ok: false, error: "the account could not be added - reload the page and try again" }; });
        })
        .then(function (d) {
          submit.disabled = false;
          if (!d.ok) { naError.textContent = d.error; naError.classList.remove("hidden"); return; }
          var a = d.account;
          accounts[a.code] = [a.name, a.tax];
          document.querySelectorAll("datalist#dl-accounts").forEach(function (dl) {
            var o = document.createElement("option");
            o.value = a.code; o.textContent = a.code + " " + a.name;
            dl.appendChild(o);
          });
          document.querySelectorAll("select[data-account-select]").forEach(function (sel) {
            var o = document.createElement("option");
            o.value = a.code; o.textContent = a.code + " " + a.name;
            sel.appendChild(o);
          });
          if (naTarget) {
            naTarget.value = a.code;
            naTarget.dispatchEvent(new Event("input", { bubbles: true }));
            naTarget.dispatchEvent(new Event("change", { bubbles: true }));
          }
          dialog.close();
          if (naTarget && naTarget.focus) naTarget.focus();
        })
        .catch(function () {
          submit.disabled = false;
          naError.textContent = "The account could not be added - is the app still running?";
          naError.classList.remove("hidden");
        });
    }, true);
  }

  // cash coding: tick a line when it is coded, code the ticked lines at once, Enter moves to the next line
  var coding = document.getElementById("coding-table");
  if (coding) {
    var bar = document.getElementById("bulk-bar"), count = document.getElementById("ticked-count");
    var picks = function () { return coding.querySelectorAll("tbody input[name=pick]"); };
    var refresh = function () {
      var n = 0;
      picks().forEach(function (p) { if (p.checked) n++; p.closest("tr").classList.toggle("ticked", p.checked); });
      if (count) count.textContent = n;
      var btn = document.getElementById("reconcile-btn");
      if (btn) btn.textContent = n ? "Reconcile " + n + " ticked line" + (n === 1 ? "" : "s") : "Reconcile ticked lines";
    };
    var setTax = function (tr, code) {
      var sel = tr.querySelector("select.tax");
      if (sel && !sel.getAttribute("data-touched") && accounts[code]) sel.value = accounts[code][1] || "";
    };
    if (bar) bar.classList.remove("hidden");
    coding.querySelectorAll("tbody tr").forEach(function (tr) {
      var acct = tr.querySelector("input.account"), pick = tr.querySelector("input[name=pick]");
      var sel = tr.querySelector("select.tax");
      if (sel) sel.addEventListener("change", function () { sel.setAttribute("data-touched", "1"); pick.checked = true; refresh(); });
      acct.addEventListener("input", function () { showName(acct); });
      acct.addEventListener("change", function () {
        acct.value = accountCode(acct);
        showName(acct);
        if (acct.value) { pick.checked = true; setTax(tr, acct.value); }
        refresh();
      });
      acct.addEventListener("keydown", function (ev) {
        if (ev.key !== "Enter") return;
        ev.preventDefault();
        var next = tr.nextElementSibling && tr.nextElementSibling.querySelector("input.account");
        acct.dispatchEvent(new Event("change", { bubbles: true }));
        if (next) next.focus();
      });
      pick.addEventListener("change", refresh);
    });
    var all = document.getElementById("pick-all");
    if (all) all.addEventListener("change", function () { picks().forEach(function (p) { p.checked = all.checked; }); refresh(); });
    var apply = document.getElementById("bulk-apply");
    if (apply) apply.addEventListener("click", function () {
      var code = accountCode(document.getElementById("bulk-account"));
      var bulkTax = bar.querySelector("select.bulk-tax");
      if (!code) { document.getElementById("bulk-account").focus(); return; }
      picks().forEach(function (p) {
        if (!p.checked) return;
        var tr = p.closest("tr"), acct = tr.querySelector("input.account");
        acct.value = code;
        showName(acct);
        var sel = tr.querySelector("select.tax");
        if (sel && bulkTax && bulkTax.value) { sel.value = bulkTax.value; sel.setAttribute("data-touched", "1"); }
        else setTax(tr, code);
      });
    });
    refresh();
  }

  // journal editor: live totals (with GST), add / remove lines, account names
  var lines = document.getElementById("journal-lines");
  if (lines) {
    var rates = {};
    var ratesEl = document.getElementById("tax-rates");
    if (ratesEl) { try { rates = JSON.parse(ratesEl.textContent); } catch (e) { rates = {}; } }
    var amountsSel = document.getElementById("amounts");
    var tbody = lines.querySelector("tbody");
    var tpl = document.getElementById("line-template");
    var next = tbody.querySelectorAll("tr").length;

    var totals = function () {
      var dr = 0, cr = 0, gst = 0, bad = false;
      var exclusive = amountsSel && amountsSel.value === "exclusive";
      tbody.querySelectorAll("tr").forEach(function (tr) {
        var d = parseAmount(tr.querySelector(".dr").value), c = parseAmount(tr.querySelector(".cr").value);
        if (isNaN(d) || isNaN(c)) { bad = true; return; }
        var sel = tr.querySelector("select.tax"), rate = sel ? (rates[sel.value] || 0) : 0;
        if (rate && (d || c) && !(d && c)) {
          var amt = d || c, g = exclusive ? amt * rate / 100 : amt * rate / (100 + rate);
          g = Math.round(g * 100) / 100;
          gst += g;
          if (exclusive) { if (d) d += g; else c += g; }
        }
        dr += d; cr += c;
      });
      document.getElementById("total-dr").textContent = money(dr);
      document.getElementById("total-cr").textContent = money(cr);
      var note = document.getElementById("gst-note");
      if (note) note.textContent = gst ? "(includes GST " + money(gst) + ")" : "";
      var diff = Math.round((dr - cr) * 100) / 100, el = document.getElementById("difference");
      el.textContent = bad ? "check the amounts" : (diff === 0 ? "Balanced" : money(diff) + (diff > 0 ? " more debits" : " more credits"));
      el.className = "num " + (bad || diff !== 0 ? "bad" : "good");
    };
    if (amountsSel) amountsSel.addEventListener("change", totals);
    var wire = function (tr) {
      tr.querySelectorAll(".amount").forEach(function (inp) {
        inp.addEventListener("input", totals);
        inp.addEventListener("blur", function () {
          var n = parseAmount(inp.value);
          if (inp.value.trim() && !isNaN(n)) inp.value = n === 0 ? "" : (n < 0 ? "-" : "") + money(n);
          totals();
        });
      });
      var acct = tr.querySelector(".account"), sel = tr.querySelector("select.tax");
      acct.addEventListener("change", function () {
        acct.value = accountCode(acct);
        showName(acct);
        // a GST client's line starts with the account's own tax rate (GST free and BAS excluded mean no GST)
        if (sel && !sel.getAttribute("data-touched") && accounts[acct.value]) {
          var t = accounts[acct.value][1];
          sel.value = rates[t] ? t : "";
          totals();
        }
      });
      acct.addEventListener("input", function () { showName(acct); });
      if (sel) sel.addEventListener("change", function () { sel.setAttribute("data-touched", "1"); totals(); });
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

  // live notes: ask now and then whether a colleague (or Claude) has changed what this screen shows
  var pulse = document.body.getAttribute("data-pulse");
  var liveBanner = document.getElementById("live-banner");
  if (pulse && liveBanner && window.fetch) {
    var since = document.body.getAttribute("data-pulse-since") || "";
    var ro = document.body.getAttribute("data-pulse-ro");
    var every = Number(document.body.getAttribute("data-pulse-every")) || 10;
    var liveShown = false;
    document.getElementById("live-refresh").addEventListener("click", function (ev) {
      ev.preventDefault();
      window.location.assign(window.location.pathname + window.location.search);   // a fresh GET, never a re-sent form
    });
    var ask = function () {
      if (liveShown || document.hidden) return;
      fetch(pulse + "?since=" + encodeURIComponent(since) + (ro ? "&ro=1" : ""),
            { credentials: "same-origin", headers: { "Accept": "application/json" } })
        .then(function (r) { return r.ok ? r.json() : null; })
        .then(function (d) {
          if (!d || !d.message) return;
          document.getElementById("live-text").textContent = d.message;
          liveBanner.classList.remove("hidden");
          liveShown = true;
        })
        .catch(function () { /* the app is closing */ });
    };
    setInterval(ask, every * 1000);
    document.addEventListener("visibilitychange", function () { if (!document.hidden) ask(); });
  }

  // copy a text box's text (the invite email)
  document.querySelectorAll("[data-copy]").forEach(function (btn) {
    var source = document.getElementById(btn.getAttribute("data-copy"));
    if (!source) return;
    btn.classList.remove("hidden");
    btn.addEventListener("click", function () {
      var label = btn.innerHTML;
      var done = function () {
        btn.textContent = btn.getAttribute("data-copied") || "Copied";
        setTimeout(function () { btn.innerHTML = label; }, 1500);
      };
      var fallback = function () { source.select(); document.execCommand("copy"); done(); };
      if (navigator.clipboard && navigator.clipboard.writeText) navigator.clipboard.writeText(source.value).then(done, fallback);
      else fallback();
    });
  });

  // access to a client: ticking a person means "only these people"
  document.querySelectorAll(".access-people input[type=checkbox]").forEach(function (cb) {
    cb.addEventListener("change", function () {
      var named = cb.form.querySelector("input[name=mode][value=named]");
      if (cb.checked && named) named.checked = true;
    });
  });

  // import preview: show the inputs that match each line's action
  document.querySelectorAll("[data-plan-action]").forEach(function (sel) {
    sel.addEventListener("change", function () {
      var tr = sel.closest("tr");
      tr.classList.remove("act-post", "act-new", "act-skip");
      tr.classList.add("act-" + sel.value);
    });
  });
})();
