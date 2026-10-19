/* ForWork - the board screen: groups and items in a table (or kanban), edited in place, with views, filters,
   subitems and the item panel (updates, files, activity).  The page loads the whole board once, then asks the
   server every few seconds what changed, so other people's edits appear without reloading. */
(function () {
  "use strict";
  var FW = window.FW, esc = FW.esc;
  var app = document.getElementById("board-app");
  if (!app) return;
  var BID = +app.getAttribute("data-board");
  var API = app.getAttribute("data-api");
  var boardEl = document.getElementById("board");
  var toolbar = document.getElementById("toolbar");
  var tabs = document.getElementById("view-tabs");
  var pop = document.getElementById("popover");
  var panel = document.getElementById("item-panel");
  var dlg = document.getElementById("dlg");
  var DEFAULT_W = { name: 340, status: 150, people: 120, date: 120, numbers: 110, text: 180, long_text: 220, timeline: 190,
                    board_relation: 210, mirror: 190, dropdown: 180, link: 180, checkbox: 90, subtasks: 120, formula: 110,
                    file: 100, email: 180, phone: 140, location: 200, creation_log: 160, last_updated: 160, readonly: 160 };
  var PALETTE = ["#00c875", "#fdab3d", "#df2f4a", "#579bfc", "#a25ddc", "#c4c4c4", "#ffcb00", "#037f4c", "#0086c0", "#9cd326",
                 "#ff5ac4", "#66ccff", "#784bd1", "#7f5347", "#ff642e", "#bb3354", "#e2445c", "#ff158a", "#cab641", "#175a63",
                 "#333333", "#757575", "#007eb5", "#9d50dd"];
  var LS = "forwork.board." + BID + ".";
  var S = { data: null, cols: {}, itemCols: [], subCols: [], groups: [], items: new Map(), users: {}, teams: {}, view: null,
            settings: {}, dirty: false, text: "", collapsed: new Set(), expanded: new Set(), seq: 0, canEdit: false, me: 0,
            drag: null, editing: false, panelItem: null, panelTab: "updates" };

  function store(k, v) { try { localStorage.setItem(LS + k, JSON.stringify(v)); } catch (e) { /* private mode */ } }
  function recall(k, d) { try { var v = localStorage.getItem(LS + k); return v ? JSON.parse(v) : d; } catch (e) { return d; } }
  function clone(o) { return JSON.parse(JSON.stringify(o || {})); }
  function today() { var d = new Date(); return d.getFullYear() + "-" + ("0" + (d.getMonth() + 1)).slice(-2) + "-" + ("0" + d.getDate()).slice(-2); }
  function c(color) { return FW.c(color); }
  function icon(n, cls) { return '<svg class="' + (cls || "ic") + '" aria-hidden="true"><use href="#i-' + n + '"></use></svg>'; }
  function fmtNum(v, s) {
    if (v === null || v === undefined || v === "") return "";
    var n = Number(v);
    if (!isFinite(n)) return esc(v);
    var d = s && s.decimals != null ? s.decimals : (Math.round(n) === n ? 0 : 2);
    var t = n.toLocaleString("en-AU", { minimumFractionDigits: d, maximumFractionDigits: d });
    var u = (s && s.unit) || "";
    return u ? (s.unit_position === "right" ? t + " " + u : u + t) : t;
  }
  function label(col, id) {
    var labs = (col.settings && col.settings.labels) || [];
    for (var i = 0; i < labs.length; i++) if (labs[i].id === id) return labs[i];
    return null;
  }
  function mainStatus(scope) {
    var list = scope === "subitem" ? S.subCols : S.itemCols;
    for (var i = 0; i < list.length; i++) if (list[i].type === "status") return list[i];
    return null;
  }
  function isDone(item) {
    var col = mainStatus("item");
    if (!col) return false;
    var l = label(col, item.values[col.id]);
    return !!(l && l.done);
  }
  function safeHref(u) { return /^(https?:|mailto:|tel:)/i.test(u || "") ? u : ""; }
  function user(id) { return S.users[id] || { id: id, name: "Former member", initials: "?", color: "#c4c4c4", active: false }; }
  function avatar(u, size) {
    return '<span class="avatar ' + (size || "xs") + (u.active ? "" : " gone") + " " + c(u.color) + '" title="' + esc(u.name) + '">' +
      esc(u.initials || FW.initials(u.name)) + "</span>";
  }

  // ---- loading ------------------------------------------------------------------------------------------------ //
  function load(keepUi) {
    return FW.api("GET", API).then(function (p) { setData(p, keepUi); render(); }).catch(function (e) {
      boardEl.innerHTML = '<div class="no-results">' + esc(e.message) + "</div>";
    });
  }

  function setData(p, keepUi) {
    S.data = p;
    S.cols = {};
    p.columns.forEach(function (col) { S.cols[col.id] = col; });
    S.itemCols = p.columns.filter(function (x) { return x.scope === "item"; });
    S.subCols = p.columns.filter(function (x) { return x.scope === "subitem"; });
    S.groups = p.groups;
    S.items = new Map();
    p.items.forEach(function (it) { S.items.set(it.id, it); });
    S.users = {};
    p.users.forEach(function (u) { S.users[u.id] = u; });
    S.teams = {};
    (p.teams || []).forEach(function (t) { S.teams[t.id] = t; });
    S.seq = p.board.seq;
    S.canEdit = !!p.can_edit;
    S.me = p.me;
    document.getElementById("fav-btn").setAttribute("aria-pressed", p.favourite ? "true" : "false");
    if (!keepUi) {
      S.collapsed = new Set(recall("collapsed", []));
      var want = app.getAttribute("data-view") || recall("view", "");
      selectView(want ? +want : null, true);
    } else if (S.view) {
      var still = p.views.filter(function (v) { return v.id === S.view.id; })[0];
      S.view = still || null;
      if (!S.dirty) S.settings = clone(still ? still.settings : recall("main", {}));
    }
  }

  function selectView(id, quiet) {
    var v = (S.data.views || []).filter(function (x) { return x.id === id; })[0] || null;
    S.view = v;
    S.settings = clone(v ? v.settings : recall("main", {}));
    S.dirty = false;
    store("view", v ? v.id : "");
    if (!quiet) { render(); window.scrollTo(0, 0); }
  }

  // ---- which columns, which items ------------------------------------------------------------------------------ //
  function visibleCols() {
    var st = S.settings, hidden = new Set(st.hidden || []);
    var cols = S.itemCols.filter(function (x) { return x.type !== "name" && !hidden.has(x.id); });
    if (st.order && st.order.length) {
      var pos = {};
      st.order.forEach(function (id, i) { pos[id] = i; });
      cols.sort(function (a, b) {
        var pa = a.id in pos ? pos[a.id] : 1000 + a.position, pb = b.id in pos ? pos[b.id] : 1000 + b.position;
        return pa - pb;
      });
    }
    return cols;
  }
  function nameCol() { return S.itemCols.filter(function (x) { return x.type === "name"; })[0] || { id: 0, title: "Item", type: "name" }; }
  function subNameCol() { return S.subCols.filter(function (x) { return x.type === "name"; })[0] || { id: 0, title: "Subitem", type: "name" }; }

  function cellText(col, it) {
    var v = it.values[col.id], comp = it.computed && it.computed[col.id];
    switch (col.type) {
      case "name": return it.name;
      case "status": var l = label(col, v); return l ? l.name : "";
      case "dropdown": return (v || []).map(function (id) { var l2 = label(col, id); return l2 ? l2.name : ""; }).join(", ");
      case "people":
        if (!v) return "";
        return (v.users || []).map(function (u) { return user(u).name; }).concat((v.teams || []).map(function (t) {
          return (S.teams[t] || {}).name || ""; })).join(", ");
      case "date": return v ? FW.date(v, true) : "";
      case "timeline": return v ? FW.date(v.from, true) + " - " + FW.date(v.to, true) : "";
      case "numbers": return v === undefined || v === null ? "" : String(v);
      case "checkbox": return v ? "yes" : "";
      case "link": return v ? (v.text || v.url) : "";
      case "email": return v ? v.email : "";
      case "phone": return v ? v.phone : "";
      case "location": return v ? v.address : "";
      case "board_relation": return (comp || []).map(function (x) { return x.name; }).join(", ");
      case "mirror": return (comp || []).map(function (x) { return x.text; }).join(", ");
      case "formula": return comp === undefined || comp === null || typeof comp === "object" ? "" : String(comp);
      case "readonly": return v ? v.text : "";
      default: return v == null ? "" : (typeof v === "object" ? JSON.stringify(v) : String(v));
    }
  }

  function sortKey(col, it) {
    if (col === "name") return [0, it.name.toLowerCase()];
    var v = it.values[col.id];
    switch (col.type) {
      case "status":
        var labs = col.settings.labels || [];
        for (var i = 0; i < labs.length; i++) if (labs[i].id === v) return [0, i];
        return [1, 0];
      case "numbers": return v == null ? [1, 0] : [0, Number(v)];
      case "formula": var f = it.computed && it.computed[col.id]; return typeof f === "number" ? [0, f] : [1, 0];
      case "date": return v ? [0, v] : [1, ""];
      case "timeline": return v ? [0, v.from] : [1, ""];
      default: var t = cellText(col, it).toLowerCase(); return t ? [0, t] : [1, ""];
    }
  }
  function cmp(a, b) { return a[0] - b[0] || (a[1] < b[1] ? -1 : a[1] > b[1] ? 1 : 0); }

  function passes(it) {
    var st = S.settings, f = st.filters || {};
    if (S.text) {
      var q = S.text.toLowerCase();
      var hay = it.name.toLowerCase();
      if (hay.indexOf(q) < 0) {
        var found = S.itemCols.some(function (col) { return cellText(col, it).toLowerCase().indexOf(q) >= 0; }) ||
          (it.subitems || []).some(function (s) { return s.name.toLowerCase().indexOf(q) >= 0; });
        if (!found) return false;
      }
    }
    if (f.people && f.people.length) {
      var want = f.people.map(function (p) { return p === "me" ? S.me : p; });
      var pcols = f.people_columns && f.people_columns.length ? f.people_columns.map(function (id) { return S.cols[id]; }).filter(Boolean)
        : S.itemCols.filter(function (x) { return x.type === "people"; });
      var hit = pcols.some(function (col) {
        var v = it.values[col.id];
        return v && (v.users || []).some(function (u) { return want.indexOf(u) >= 0; });
      });
      if (!hit) return false;
    }
    if (f.groups && f.groups.length && f.groups.indexOf(it.group_id) < 0) return false;
    var rules = f.rules || [];
    for (var i = 0; i < rules.length; i++) {
      var r = rules[i], col = S.cols[r.column];
      if (!col) continue;
      var v = it.values[col.id], ok;
      if (r.op === "is_empty") ok = v == null || v === "" || (Array.isArray(v) && !v.length);
      else if (r.op === "is_not_empty") ok = !(v == null || v === "" || (Array.isArray(v) && !v.length));
      else {
        var vals = (r.values || []).map(function (x) { return x === "me" ? S.me : x; });
        if (col.type === "status") ok = vals.indexOf(v === undefined ? null : v) >= 0 || (v == null && vals.indexOf("none") >= 0);
        else if (col.type === "people") ok = !!v && (v.users || []).some(function (u) { return vals.indexOf(u) >= 0; });
        else if (col.type === "dropdown") ok = !!v && v.some(function (x) { return vals.indexOf(x) >= 0; });
        else if (col.type === "checkbox") ok = vals.indexOf(!!v) >= 0;
        else ok = vals.indexOf(cellText(col, it)) >= 0;
        if (r.op === "not_any_of") ok = !ok;
      }
      if (!ok) return false;
    }
    return true;
  }

  function sortItems(list) {
    var srt = (S.settings.sort || []).map(function (s) { return { col: s.column === "name" ? "name" : S.cols[s.column], dir: s.dir }; })
      .filter(function (s) { return s.col; });
    if (!srt.length) return list;
    return list.slice().sort(function (a, b) {
      for (var i = 0; i < srt.length; i++) {
        var r = cmp(sortKey(srt[i].col, a), sortKey(srt[i].col, b));
        if (r) return srt[i].dir === "desc" ? -r : r;
      }
      return a.position - b.position;
    });
  }

  // groups to show: the board's groups, or groups made from a column's values ("group by")
  function sections() {
    var items = [];
    S.items.forEach(function (it) { if (passes(it)) items.push(it); });
    items.sort(function (a, b) { return a.position - b.position || a.id - b.id; });
    var gb = S.settings.group_by && S.cols[S.settings.group_by];
    var out = [];
    if (!gb) {
      var by = {};
      items.forEach(function (it) { (by[it.group_id] = by[it.group_id] || []).push(it); });
      S.groups.forEach(function (g) { out.push({ id: g.id, key: "g" + g.id, title: g.title, color: g.color, real: true, items: sortItems(by[g.id] || []) }); });
      return out;
    }
    var buckets = {}, order = [];
    function add(key, title, color, it) {
      if (!buckets[key]) { buckets[key] = { id: null, key: key, title: title, color: color, real: false, items: [] }; order.push(key); }
      buckets[key].items.push(it);
    }
    if (gb.type === "status") (gb.settings.labels || []).forEach(function (l) {
      buckets["l" + l.id] = { id: null, key: "l" + l.id, title: l.name || "(blank)", color: l.color, real: false, items: [] };
      order.push("l" + l.id);
    });
    items.forEach(function (it) {
      var v = it.values[gb.id];
      if (gb.type === "status") {
        if (v == null) add("none", "No " + gb.title, "#c4c4c4", it); else add("l" + v, (label(gb, v) || {}).name || "?", (label(gb, v) || {}).color, it);
      } else {
        var t = cellText(gb, it);
        add(t ? "t" + t : "none", t || "No " + gb.title, t ? PALETTE[order.length % PALETTE.length] : "#c4c4c4", it);
      }
    });
    order.forEach(function (k) {
      var b = buckets[k];
      if (b.items.length || !S.settings.hide_empty_groups && gb.type === "status") { b.items = sortItems(b.items); out.push(b); }
    });
    return out;
  }

  // ---- drawing ------------------------------------------------------------------------------------------------- //
  function render() {
    renderTabs();
    renderToolbar();
    if (S.view && S.view.type === "kanban") renderKanban(); else renderTable();
  }

  function renderTabs() {
    var h = '<button class="view-tab ' + (!S.view ? "active" : "") + '" data-view="">' + icon("table", "ic sm") + " Main table</button>";
    (S.data.views || []).forEach(function (v) {
      h += '<button class="view-tab ' + (S.view && S.view.id === v.id ? "active" : "") + '" data-view="' + v.id + '">' +
        icon(v.type === "kanban" ? "kanban" : "table", "ic sm") + " " + esc(v.name) + "</button>";
    });
    if (S.canEdit) h += '<button class="view-tab" data-act="new-view" title="Add a view">' + icon("plus", "ic sm") + "</button>";
    tabs.innerHTML = h;
  }

  function renderToolbar() {
    var st = S.settings, f = st.filters || {};
    var people = {};
    S.items.forEach(function (it) {
      S.itemCols.forEach(function (col) {
        if (col.type === "people" && it.values[col.id]) (it.values[col.id].users || []).forEach(function (u) { people[u] = (people[u] || 0) + 1; });
      });
    });
    var ppl = Object.keys(people).map(Number).filter(function (u) { return user(u).active; })
      .sort(function (a, b) { return people[b] - people[a]; }).slice(0, 10);
    var sel = new Set((f.people || []).map(function (p) { return p === "me" ? S.me : p; }));
    var nRules = (f.rules || []).length;
    var h = "";
    if (S.canEdit) h += '<button class="btn primary" data-act="new-item">' + icon("plus") + " New item</button>";
    h += '<label class="search-box">' + icon("search", "ic sm") + '<input type="search" id="board-search" placeholder="Search this board" value="' +
      esc(S.text) + '"></label>';
    h += '<span class="people-filter" title="Show items for a person">' + ppl.map(function (u) {
      return '<span data-person="' + u + '" class="' + (sel.has(u) ? "sel" : "") + '">' + avatar(user(u), "sm") + "</span>";
    }).join("") + "</span>";
    h += '<button class="btn ghost ' + (nRules ? "on" : "") + '" data-act="filter">' + icon("filter", "ic sm") + " Filter" + (nRules ? " (" + nRules + ")" : "") + "</button>";
    h += '<button class="btn ghost ' + ((st.sort || []).length ? "on" : "") + '" data-act="sort">' + icon("sort", "ic sm") + " Sort</button>";
    h += '<button class="btn ghost ' + ((st.hidden || []).length ? "on" : "") + '" data-act="hide">' + icon("eye", "ic sm") + " Columns</button>";
    h += '<button class="btn ghost ' + (st.group_by ? "on" : "") + '" data-act="groupby">' + icon("group", "ic sm") + " Group by</button>";
    if (S.view && S.view.type === "kanban") h += '<button class="btn ghost" data-act="kanban-col">' + icon("kanban", "ic sm") + " Cards by</button>";
    if (S.dirty && S.view && S.canEdit) h += '<button class="btn save-view" data-act="save-view">Save changes to this view</button>';
    else if (S.dirty && !S.view && S.canEdit) h += '<button class="btn ghost save-view" data-act="new-view">Save as a view</button>';
    if (S.view && S.canEdit) h += '<details class="menu-wrap"><summary class="icon-btn" title="View menu">' + icon("more") +
      '</summary><div class="menu"><button data-act="rename-view">' + icon("edit") + ' Rename view</button><button class="danger" data-act="delete-view">' +
      icon("trash") + " Delete view</button></div></details>";
    toolbar.innerHTML = h;
    var input = document.getElementById("board-search");
    input.addEventListener("input", function () { S.text = input.value.trim(); renderBody(); });
  }

  function renderBody() { if (S.view && S.view.type === "kanban") renderKanban(); else renderTable(); }

  function colW(col) { return col.width || DEFAULT_W[col.type] || 150; }

  function renderTable() {
    var cols = visibleCols(), nc = nameCol();
    var secs = sections();
    var total = 0;
    secs.forEach(function (s) { total += s.items.length; });
    var tableW = colW(nc) + cols.reduce(function (a, col) { return a + colW(col); }, 0) + 44;
    var widthCls = FW.cls("width", tableW + "px");
    var colgroup = '<colgroup><col class="' + FW.cls("width", colW(nc) + "px") + '">' + cols.map(function (col) {
      return '<col class="' + FW.cls("width", colW(col) + "px") + '">';
    }).join("") + '<col class="' + FW.cls("width", "44px") + '"></colgroup>';
    var head = '<thead><tr><th class="c-name sticky-col">' + esc(nc.title) + "</th>" + cols.map(function (col) {
      return '<th data-col="' + col.id + '" title="' + esc(col.title + (col.description ? " - " + col.description : "")) + '">' + esc(col.title) +
        (S.canEdit ? '<button class="icon-btn col-menu" data-act="col-menu" data-col="' + col.id + '">' + icon("down", "ic sm") +
          '</button><span class="resize" data-col="' + col.id + '"></span>' : "") + "</th>";
    }).join("") + '<th class="add-col">' + (S.canEdit ? '<button data-act="add-col" title="Add a column">' + icon("plus") + "</button>" : "") +
      "</th></tr></thead>";
    var h = "";
    if (S.settings.group_by) h += '<p class="group-by-note">Grouped by ' + esc((S.cols[S.settings.group_by] || {}).title || "") + ".</p>";
    secs.forEach(function (sec) {
      var collapsed = S.collapsed.has(sec.key);
      h += '<section class="group ' + c(sec.color) + (collapsed ? " collapsed" : "") + '" data-key="' + sec.key + '"' +
        (sec.real ? ' data-group="' + sec.id + '"' : "") + ">";
      h += '<div class="group-head"><button class="group-toggle" data-act="toggle-group">' + icon("down") + "</button>" +
        '<span class="group-title" ' + (sec.real && S.canEdit ? 'data-act="rename-group"' : "") + ">" + esc(sec.title) + "</span>" +
        '<span class="group-count">' + sec.items.length + " item" + (sec.items.length === 1 ? "" : "s") + "</span>" +
        (sec.real && S.canEdit ? '<button class="icon-btn group-menu-btn" data-act="group-menu">' + icon("more") + "</button>" : "") + "</div>";
      if (!collapsed) {
        h += '<table class="grid ' + widthCls + '">' + colgroup + head + "<tbody>";
        sec.items.forEach(function (it) { h += rowHtml(it, cols); });
        h += "</tbody><tfoot>";
        if (S.canEdit && (sec.real || !S.settings.group_by)) {
          h += '<tr class="add-row"><td class="sticky-col"><input data-add-item="' + (sec.id || "") + '" placeholder="+ Add item" maxlength="500"></td><td colspan="' +
            (cols.length + 1) + '"></td></tr>';
        }
        h += footHtml(sec.items, cols) + "</tfoot></table>";
      }
      h += "</section>";
    });
    if (!secs.length || !total && (S.text || (S.settings.filters && Object.keys(S.settings.filters).length))) {
      h += '<div class="no-results">' + (S.items.size ? "No items match the search or filters." : "This board has no items yet.") + "</div>";
    }
    if (S.canEdit && !S.settings.group_by) h += '<button class="btn ghost" data-act="add-group">' + icon("plus") + " Add new group</button>";
    boardEl.innerHTML = h;
    FW.paint(boardEl);
  }

  function rowHtml(it, cols) {
    var nc = nameCol();
    var subs = it.subitems || [];
    var open = S.expanded.has(it.id);
    var h = '<tr class="row' + (S.panelItem === it.id ? " selected" : "") + '" data-item="' + it.id + '">';
    h += '<td class="cell c-name sticky-col" data-col="' + nc.id + '"><div class="name-wrap">' +
      (S.canEdit && !(S.settings.sort || []).length && !S.settings.group_by ? '<span class="drag" draggable="true" title="Drag to move">' + icon("drag", "ic sm") + "</span>" : "") +
      '<button class="sub-toggle ' + (open ? "open" : "") + '" data-act="toggle-sub" title="Subitems">' + icon("chevron") +
      (subs.length ? subs.length : "") + "</button>" +
      '<span class="name-text" data-act="rename-item">' + esc(it.name) + "</span>" +
      '<button class="open-item ' + (it.updates ? "has" : "") + '" data-act="open-item" title="Open (updates, files, activity)">' + icon("chat", "ic sm") +
      (it.updates ? '<span class="n">' + it.updates + "</span>" : "") + "</button>" +
      (S.canEdit ? '<button class="icon-btn" data-act="item-menu" title="More">' + icon("more", "ic sm") + "</button>" : "") +
      "</div></td>";
    cols.forEach(function (col) { h += cellTd(col, it); });
    h += '<td class="add-col"></td></tr>';
    if (open) h += subHtml(it, cols.length + 2);
    return h;
  }

  function cellTd(col, it) {
    var ro = !S.canEdit || !editable(col);
    return '<td class="cell t-' + col.type + (ro ? " ro" : "") + '" data-col="' + col.id + '">' + cellHtml(col, it) + "</td>";
  }

  function editable(col) {
    return ["text", "long_text", "numbers", "status", "dropdown", "people", "date", "timeline", "checkbox", "link", "email", "phone",
            "location", "board_relation", "file", "name"].indexOf(col.type) >= 0;
  }

  function cellHtml(col, it) {
    var v = it.values[col.id], comp = it.computed && it.computed[col.id];
    switch (col.type) {
      case "status":
        var l = label(col, v);
        return l ? '<div class="status-fill ' + c(l.color) + '" title="' + esc(l.name) + '">' + esc(l.name) + "</div>" : '<div class="status-fill empty"></div>';
      case "people":
        if (!v) return "";
        var us = (v.users || []).map(user);
        var hs = us.slice(0, 3).map(function (u) { return avatar(u); }).join("");
        (v.teams || []).slice(0, 2).forEach(function (t) {
          var tm = S.teams[t] || { name: "Team" };
          hs += avatar({ name: tm.name, initials: FW.initials(tm.name), color: "#757575", active: true });
        });
        return '<span class="people" title="' + esc(cellText(col, it)) + '">' + hs + (us.length > 3 ? '<span class="more">+' + (us.length - 3) + "</span>" : "") + "</span>";
      case "date":
        if (!v) return "";
        var over = v.slice(0, 10) < today() && !isDone(it);
        return '<span class="date-cell' + (over ? " overdue" : "") + '">' + esc(FW.date(v)) + "</span>";
      case "timeline":
        return v ? '<span class="timeline-bar">' + esc(FW.date(v.from, false)) + (v.to !== v.from ? " - " + esc(FW.date(v.to, false)) : "") + "</span>" : "";
      case "numbers": return fmtNum(v, col.settings);
      case "formula":
        if (comp && typeof comp === "object") return '<span class="err-text" title="The formula could not be worked out">!</span>';
        return fmtNum(comp, null);
      case "checkbox": return '<span class="check-box ' + (v ? "on" : "") + '">' + (v ? icon("check") : "") + "</span>";
      case "link":
        if (!v) return "";
        var href = safeHref(v.url);
        return href ? '<a href="' + esc(href) + '" target="_blank" rel="noopener noreferrer" title="' + esc(v.url) + '">' + esc(v.text || v.url) + "</a>" : esc(v.text);
      case "email": return v ? '<a href="mailto:' + esc(v.email) + '">' + esc(v.text || v.email) + "</a>" : "";
      case "phone": return v ? '<a href="tel:' + esc(v.phone) + '">' + esc(v.phone) + "</a>" : "";
      case "dropdown":
        return (v || []).map(function (id) { var x = label(col, id); return x ? '<span class="chip-sm">' + esc(x.name) + "</span>" : ""; }).join("");
      case "board_relation":
        return (comp || []).map(function (x) {
          return '<a class="chip-sm link" href="/items/' + x.id + '" title="' + esc(x.board) + '">' + esc(x.name) + "</a>";
        }).join("");
      case "mirror":
        return (comp || []).map(function (x) { return '<span class="chip-sm' + (x.color ? " c " + c(x.color) : "") + '">' + esc(x.text) + "</span>"; }).join("");
      case "subtasks":
        if (!comp) return "";
        var pct = comp.total ? Math.round(100 * comp.done / comp.total) : 0;
        return '<span class="progress"><span class="bar"><i class="' + FW.cls("--w", pct + "%") + '"></i></span>' + comp.done + "/" + comp.total + "</span>";
      case "file": return v && v.length ? icon("paperclip", "ic sm") + " " + v.length : "";
      case "creation_log": return it.created_by ? avatar(user(it.created_by)) + " " + esc(FW.date((it.created_at || "").slice(0, 10))) : esc(FW.date((it.created_at || "").slice(0, 10)));
      case "last_updated": return (it.updated_by ? avatar(user(it.updated_by)) + " " : "") + esc(FW.when(it.updated_at));
      case "long_text": return v ? esc(String(v).split("\n")[0]) : "";
      case "readonly": return v ? '<span class="muted">' + esc(v.text) + "</span>" : "";
      default: return esc(cellText(col, it));
    }
  }

  function subHtml(it, span) {
    var subs = it.subitems || [], cols = S.subCols.filter(function (x) { return x.type !== "name"; }), nc = subNameCol();
    var w = colW(nc) + cols.reduce(function (a, col) { return a + colW(col); }, 0);
    var h = '<tr class="sub-wrap" data-parent="' + it.id + '"><td colspan="' + span + '"><table class="grid sub ' + FW.cls("width", w + "px") + '"><colgroup>' +
      '<col class="' + FW.cls("width", colW(nc) + "px") + '">' + cols.map(function (col) { return '<col class="' + FW.cls("width", colW(col) + "px") + '">'; }).join("") +
      "</colgroup><thead><tr class=\"sub-head\"><th class=\"c-name\">" + esc(nc.title) + "</th>" +
      cols.map(function (col) { return "<th>" + esc(col.title) + "</th>"; }).join("") + "</tr></thead><tbody>";
    subs.forEach(function (s) {
      h += '<tr class="row sub-row" data-item="' + s.id + '" data-parent="' + it.id + '"><td class="cell c-name"><div class="name-wrap">' +
        '<span class="name-text" data-act="rename-item">' + esc(s.name) + "</span>" +
        '<button class="open-item ' + (s.updates ? "has" : "") + '" data-act="open-item">' + icon("chat", "ic sm") + (s.updates ? '<span class="n">' + s.updates + "</span>" : "") + "</button>" +
        (S.canEdit ? '<button class="icon-btn" data-act="item-menu">' + icon("more", "ic sm") + "</button>" : "") + "</div></td>";
      cols.forEach(function (col) { h += cellTd(col, s); });
      h += "</tr>";
    });
    if (S.canEdit) h += '<tr class="sub-add"><td colspan="' + (cols.length + 1) + '"><input data-add-sub="' + it.id + '" placeholder="+ Add subitem" maxlength="500"></td></tr>';
    return h + "</tbody></table></td></tr>";
  }

  function footHtml(items, cols) {
    var h = '<tr class="foot"><td class="sticky-col"></td>';
    cols.forEach(function (col) {
      var inner = "";
      if (col.type === "status" && items.length) {
        var counts = {}, n = 0;
        items.forEach(function (it) { var v = it.values[col.id]; var k = v == null ? "x" : v; counts[k] = (counts[k] || 0) + 1; n++; });
        inner = '<div class="battery" title="' + esc(Object.keys(counts).map(function (k) {
          var l = k === "x" ? null : label(col, +k); return (l ? l.name : "Empty") + ": " + counts[k]; }).join(", ")) + '">';
        (col.settings.labels || []).concat([{ id: "x", color: "#c4c4c4" }]).forEach(function (l) {
          var k = l.id === "x" ? "x" : l.id;
          if (counts[k]) inner += '<i class="' + c(l.color) + " " + FW.cls("--w", (100 * counts[k] / n).toFixed(2) + "%") + '"></i>';
        });
        inner += "</div>";
      } else if (col.type === "numbers" || col.type === "formula") {
        var sum = 0, any = false;
        items.forEach(function (it) {
          var v = col.type === "numbers" ? it.values[col.id] : it.computed && it.computed[col.id];
          if (typeof v === "number") { sum += v; any = true; }
        });
        inner = any ? "<strong>" + fmtNum(Math.round(sum * 100) / 100, col.settings) + "</strong> sum" : "";
      }
      h += '<td class="foot-cell">' + inner + "</td>";
    });
    return h + "<td></td></tr>";
  }

  function renderKanban() {
    var kc = S.cols[S.settings.kanban_column] || mainStatus("item");
    if (!kc) { boardEl.innerHTML = '<div class="no-results">A kanban view needs a status column on the board.</div>'; return; }
    var items = [];
    S.items.forEach(function (it) { if (passes(it)) items.push(it); });
    items = sortItems(items.sort(function (a, b) { return a.position - b.position; }));
    var labs = (kc.settings.labels || []).filter(function (l) { return !l.hidden || items.some(function (it) { return it.values[kc.id] === l.id; }); });
    var cols = [{ id: null, name: "No " + kc.title, color: "#c4c4c4" }].concat(labs);
    var people = S.itemCols.filter(function (x) { return x.type === "people"; })[0];
    var date = S.itemCols.filter(function (x) { return x.type === "date"; })[0];
    var h = '<div class="kanban">';
    cols.forEach(function (l) {
      var mine = items.filter(function (it) { return (it.values[kc.id] == null ? null : it.values[kc.id]) === l.id; });
      if (l.id === null && !mine.length) return;
      h += '<div class="kan-col" data-label="' + (l.id === null ? "" : l.id) + '"><div class="kan-head ' + c(l.color) + '"><span>' + esc(l.name || "(blank)") +
        "</span><span>" + mine.length + "</span></div>";
      mine.slice(0, 300).forEach(function (it) {
        var g = S.groups.filter(function (x) { return x.id === it.group_id; })[0];
        h += '<div class="kan-card" draggable="' + (S.canEdit ? "true" : "false") + '" data-item="' + it.id + '"><div class="t">' + esc(it.name) + '</div><div class="meta">' +
          (people ? cellHtml(people, it) : "") + (date && it.values[date.id] ? "<span>" + esc(FW.date(it.values[date.id])) + "</span>" : "") +
          (g ? '<span class="group-tag ' + c(g.color) + '">' + esc(g.title) + "</span>" : "") + (it.updates ? "<span>" + icon("chat", "ic tiny") + " " + it.updates + "</span>" : "") +
          "</div></div>";
      });
      if (mine.length > 300) h += '<p class="muted small">and ' + (mine.length - 300) + " more - use search or a filter</p>";
      h += "</div>";
    });
    boardEl.innerHTML = h + "</div>";
    FW.paint(boardEl);
  }

  // ---- applying changes from the server --------------------------------------------------------------------------- //
  function applyItems(items, removed) {
    var structural = false;
    (items || []).forEach(function (it) {
      var old = S.items.get(it.id);
      if (!old || old.group_id !== it.group_id || old.position !== it.position) structural = true;
      S.items.set(it.id, it);
    });
    (removed || []).forEach(function (id) { if (S.items.delete(id)) structural = true; });
    if (structural || S.view && S.view.type === "kanban" || (S.settings.sort || []).length || S.settings.group_by) { renderBody(); return; }
    var cols = visibleCols();
    (items || []).forEach(function (it) {
      var row = boardEl.querySelector('tr.row[data-item="' + it.id + '"]:not(.sub-row)');
      if (!row) return;
      var sub = boardEl.querySelector('tr.sub-wrap[data-parent="' + it.id + '"]');
      if (sub) sub.remove();
      if (!passes(it)) { row.remove(); return; }
      var tmp = document.createElement("tbody");
      tmp.innerHTML = rowHtml(it, cols);
      FW.paint(tmp);
      var nodes = Array.prototype.slice.call(tmp.children);
      row.replaceWith.apply(row, nodes);
    });
  }

  function saveValue(itemId, colId, value) {
    return FW.api("POST", "/api/items/" + itemId + "/values", { column: colId, value: value }).then(function (r) {
      applyItems(r.items);
      refreshPanelIfOpen(itemId, r.items);
      return r;
    }).catch(function (e) { FW.toast(e.message, true); throw e; });
  }

  function findItem(id) {
    if (S.items.has(id)) return S.items.get(id);
    var found = null;
    S.items.forEach(function (it) { (it.subitems || []).forEach(function (s) { if (s.id === id) found = s; }); });
    return found;
  }
  function parentOf(id) {
    var found = null;
    S.items.forEach(function (it) { (it.subitems || []).forEach(function (s) { if (s.id === id) found = it; }); });
    return found;
  }

  // ---- live refresh ----------------------------------------------------------------------------------------------- //
  var dot = document.getElementById("live-dot");
  function poll() {
    if (document.hidden || S.editing || !S.data) return;
    FW.api("GET", API + "/changes?since=" + S.seq).then(function (ch) {
      dot.classList.add("on");
      if (ch.reload) { var keep = S.seq; return load(true).then(function () { if (S.seq !== keep && S.panelItem) openPanel(S.panelItem, true); }); }
      if (ch.seq === S.seq) return;
      S.seq = ch.seq;
      if ((ch.items && ch.items.length) || (ch.removed && ch.removed.length)) {
        applyItems(ch.items, ch.removed);
        if (S.panelItem) {
          var top = parentOf(S.panelItem) || findItem(S.panelItem);
          if (top && (ch.items || []).some(function (x) { return x.id === top.id; })) openPanel(S.panelItem, true);
        }
      }
    }).catch(function () { dot.classList.remove("on"); });
  }
  setInterval(poll, 5000);
  document.addEventListener("visibilitychange", function () { if (!document.hidden) poll(); });

  // ---- popovers ------------------------------------------------------------------------------------------------------ //
  function openPop(anchor, html, minW) {
    pop.innerHTML = html;
    pop.hidden = false;
    FW.paint(pop);
    var r = anchor.getBoundingClientRect();
    pop.style.minWidth = "";
    var w = Math.max(minW || 220, pop.offsetWidth);
    var left = Math.min(Math.max(8, r.left), window.innerWidth - w - 12);
    var top = r.bottom + 4;
    if (top + pop.offsetHeight > window.innerHeight - 8) top = Math.max(8, r.top - pop.offsetHeight - 4);
    pop.style.left = left + "px";
    pop.style.top = top + "px";
    S.editing = true;
    var first = pop.querySelector("input:not([type=checkbox]), textarea, select");
    if (first) first.focus();
    return pop;
  }
  function closePop() { pop.hidden = true; pop.innerHTML = ""; S.editing = false; pop.onsubmit = null; }
  document.addEventListener("mousedown", function (ev) {
    if (!pop.hidden && !pop.contains(ev.target) && !ev.target.closest("[data-keep-pop]")) closePop();
  });
  document.addEventListener("keydown", function (ev) {
    if (ev.key === "Escape") {
      if (!pop.hidden) closePop();
      else if (!panel.hidden && !dlg.open) closePanel();
    }
  });
  window.addEventListener("resize", closePop);
  boardEl.addEventListener("scroll", closePop);

  // ---- editing a cell ------------------------------------------------------------------------------------------------- //
  function editCell(td) {
    var tr = td.closest("tr.row");
    var it = findItem(+tr.getAttribute("data-item"));
    var col = S.cols[+td.getAttribute("data-col")];
    if (!it || !col || !S.canEdit || !editable(col)) return;
    var v = it.values[col.id];
    switch (col.type) {
      case "text": case "numbers": case "email": case "phone": case "location":
        inlineInput(td, col.type === "email" ? (v ? v.email : "") : col.type === "phone" ? (v ? v.phone : "") :
          col.type === "location" ? (v ? v.address : "") : (v == null ? "" : String(v)), function (val) { return saveValue(it.id, col.id, val); });
        break;
      case "long_text":
        openPop(td, '<div class="pop-title">' + esc(col.title) + '</div><textarea class="long">' + esc(v || "") + '</textarea><div class="pop-actions"><span></span><button class="btn primary small" data-pop="save">Save</button></div>', 360);
        pop.querySelector("[data-pop=save]").addEventListener("click", function () {
          var t = pop.querySelector("textarea").value;
          closePop();
          saveValue(it.id, col.id, t);
        });
        break;
      case "status": statusPicker(td, it, col); break;
      case "dropdown": dropdownPicker(td, it, col); break;
      case "people": peoplePicker(td, it, col); break;
      case "date":
        openPop(td, '<div class="pop-title">' + esc(col.title) + '</div><input type="date" value="' + esc((v || "").slice(0, 10)) +
          '"><div class="pop-actions"><button class="btn small ghost" data-pop="clear">Clear</button><button class="btn small ghost" data-pop="today">Today</button>' +
          '<button class="btn small primary" data-pop="save">Save</button></div>');
        var di = pop.querySelector("input");
        pop.querySelector("[data-pop=save]").addEventListener("click", function () { closePop(); saveValue(it.id, col.id, di.value || null); });
        pop.querySelector("[data-pop=today]").addEventListener("click", function () { closePop(); saveValue(it.id, col.id, today()); });
        pop.querySelector("[data-pop=clear]").addEventListener("click", function () { closePop(); saveValue(it.id, col.id, null); });
        di.addEventListener("keydown", function (e) { if (e.key === "Enter") { closePop(); saveValue(it.id, col.id, di.value || null); } });
        break;
      case "timeline":
        openPop(td, '<div class="pop-title">' + esc(col.title) + '</div><label>From <input type="date" name="from" value="' + esc(v ? v.from : "") +
          '"></label><label>To <input type="date" name="to" value="' + esc(v ? v.to : "") + '"></label><div class="pop-actions"><button class="btn small ghost" data-pop="clear">Clear</button>' +
          '<button class="btn small primary" data-pop="save">Save</button></div>');
        pop.querySelector("[data-pop=save]").addEventListener("click", function () {
          var f = pop.querySelector("[name=from]").value, t = pop.querySelector("[name=to]").value;
          closePop();
          saveValue(it.id, col.id, f || t ? { from: f || t, to: t || f } : null);
        });
        pop.querySelector("[data-pop=clear]").addEventListener("click", function () { closePop(); saveValue(it.id, col.id, null); });
        break;
      case "checkbox": saveValue(it.id, col.id, !v); break;
      case "link":
        openPop(td, '<div class="pop-title">' + esc(col.title) + '</div><label>Web address <input name="url" value="' + esc(v ? v.url : "") +
          '" placeholder="https://"></label><label>Text to show <input name="text" value="' + esc(v ? v.text : "") +
          '"></label><div class="pop-actions"><button class="btn small ghost" data-pop="clear">Remove</button>' +
          (v && safeHref(v.url) ? '<a class="btn small ghost" target="_blank" rel="noopener noreferrer" href="' + esc(safeHref(v.url)) + '">Open</a>' : "") +
          '<button class="btn small primary" data-pop="save">Save</button></div>', 320);
        pop.querySelector("[data-pop=save]").addEventListener("click", function () {
          var u = pop.querySelector("[name=url]").value.trim(), t = pop.querySelector("[name=text]").value.trim();
          closePop();
          saveValue(it.id, col.id, u ? { url: u, text: t } : null);
        });
        pop.querySelector("[data-pop=clear]").addEventListener("click", function () { closePop(); saveValue(it.id, col.id, null); });
        break;
      case "board_relation": relationPicker(td, it, col); break;
      case "file": openPanel(it.id, false, "files"); break;
    }
  }

  function inlineInput(td, value, save) {
    var old = td.innerHTML;
    td.innerHTML = '<input class="cell-input" value="' + esc(value) + '">';
    var input = td.querySelector("input");
    S.editing = true;
    input.focus();
    input.select();
    var done = false;
    function finish(ok) {
      if (done) return;
      done = true;
      S.editing = false;
      if (ok && input.value.trim() !== String(value).trim()) {
        save(input.value.trim() || null).catch(function () { td.innerHTML = old; });
      } else td.innerHTML = old;
      FW.paint(td);
    }
    input.addEventListener("keydown", function (e) {
      if (e.key === "Enter") finish(true);
      if (e.key === "Escape") { e.stopPropagation(); finish(false); }
    });
    input.addEventListener("blur", function () { finish(true); });
  }

  function statusPicker(td, it, col) {
    var v = it.values[col.id];
    var labs = (col.settings.labels || []).filter(function (l) { return !l.hidden || l.id === v; });
    var h = '<div class="label-grid">' + labs.map(function (l) {
      return '<button class="label-btn ' + c(l.color) + (l.id === v ? " current" : "") + '" data-label="' + l.id + '" title="' + esc(l.name) + '">' + (esc(l.name) || "&nbsp;") + "</button>";
    }).join("") + '</div><div class="pop-actions"><button class="btn small ghost" data-pop="clear">Clear</button><button class="btn small ghost" data-pop="edit">' +
      icon("edit", "ic sm") + " Edit labels</button></div>";
    openPop(td, h, 300);
    pop.querySelectorAll("[data-label]").forEach(function (b) {
      b.addEventListener("click", function () { closePop(); saveValue(it.id, col.id, +b.getAttribute("data-label")); });
    });
    pop.querySelector("[data-pop=clear]").addEventListener("click", function () { closePop(); saveValue(it.id, col.id, null); });
    pop.querySelector("[data-pop=edit]").addEventListener("click", function () { labelEditor(td, col); });
  }

  function dropdownPicker(td, it, col) {
    var cur = new Set(it.values[col.id] || []);
    var labs = (col.settings.labels || []).filter(function (l) { return !l.hidden || cur.has(l.id); });
    var h = '<input class="pop-search" placeholder="Search options"><ul class="pick-list">' + labs.map(function (l) {
      return '<li data-id="' + l.id + '" class="' + (cur.has(l.id) ? "on" : "") + '"><input type="checkbox" ' + (cur.has(l.id) ? "checked" : "") + "> " + esc(l.name) + "</li>";
    }).join("") + '</ul><div class="pop-actions"><button class="btn small ghost" data-pop="edit">Edit options</button><button class="btn small primary" data-pop="save">Save</button></div>';
    openPop(td, h, 260);
    pickListBehaviour(true);
    pop.querySelector("[data-pop=save]").addEventListener("click", function () {
      var ids = Array.prototype.map.call(pop.querySelectorAll("li.on"), function (li) { return +li.getAttribute("data-id"); });
      closePop();
      saveValue(it.id, col.id, ids.length ? ids : null);
    });
    pop.querySelector("[data-pop=edit]").addEventListener("click", function () { labelEditor(td, col); });
  }

  function pickListBehaviour(multi) {
    var search = pop.querySelector(".pop-search");
    if (search) search.addEventListener("input", function () {
      var q = search.value.trim().toLowerCase();
      pop.querySelectorAll(".pick-list li").forEach(function (li) { li.classList.toggle("hidden", !!q && li.textContent.toLowerCase().indexOf(q) < 0); });
    });
    pop.querySelectorAll(".pick-list li").forEach(function (li) {
      li.addEventListener("click", function (e) {
        if (!multi) pop.querySelectorAll(".pick-list li.on").forEach(function (o) { if (o !== li) { o.classList.remove("on"); var cb0 = o.querySelector("input"); if (cb0) cb0.checked = false; } });
        li.classList.toggle("on");
        var cb = li.querySelector("input[type=checkbox]");
        if (cb && e.target !== cb) cb.checked = li.classList.contains("on");
      });
    });
  }

  function peoplePicker(td, it, col) {
    var v = it.values[col.id] || {};
    var cur = new Set(v.users || []), curT = new Set(v.teams || []);
    var list = Object.keys(S.users).map(Number).map(user).filter(function (u) { return u.active || cur.has(u.id); })
      .sort(function (a, b) { return (cur.has(b.id) - cur.has(a.id)) || a.name.localeCompare(b.name); });
    var h = '<input class="pop-search" placeholder="Search people"><ul class="pick-list">' + list.map(function (u) {
      return '<li data-user="' + u.id + '" class="' + (cur.has(u.id) ? "on" : "") + '">' + avatar(u, "sm") + " " + esc(u.name) +
        '<span class="sub">' + esc(u.active ? "" : "former") + "</span></li>";
    }).join("") + Object.keys(S.teams).map(function (t) {
      var tm = S.teams[t];
      return '<li data-team="' + tm.id + '" class="' + (curT.has(tm.id) ? "on" : "") + '">' + icon("users", "ic sm") + " " + esc(tm.name) + '<span class="sub">team</span></li>';
    }).join("") + '</ul><div class="pop-actions"><button class="btn small ghost" data-pop="clear">Clear</button><button class="btn small primary" data-pop="save">Save</button></div>';
    openPop(td, h, 280);
    pickListBehaviour(true);
    pop.querySelector("[data-pop=save]").addEventListener("click", function () {
      var users = Array.prototype.map.call(pop.querySelectorAll("li.on[data-user]"), function (li) { return +li.getAttribute("data-user"); });
      var teams = Array.prototype.map.call(pop.querySelectorAll("li.on[data-team]"), function (li) { return +li.getAttribute("data-team"); });
      closePop();
      saveValue(it.id, col.id, users.length || teams.length ? { users: users, teams: teams } : null);
    });
    pop.querySelector("[data-pop=clear]").addEventListener("click", function () { closePop(); saveValue(it.id, col.id, null); });
  }

  function relationPicker(td, it, col) {
    var cur = (it.computed && it.computed[col.id]) || [];
    var h = '<div class="pop-title">' + esc(col.title) + '</div><div class="chips">' + cur.map(function (x) {
      return '<span class="chip-sm link">' + esc(x.name) + ' <button class="icon-btn" data-remove="' + x.id + '" title="Remove">' + icon("x", "ic tiny") + "</button></span>";
    }).join("") + '</div><input class="pop-search" placeholder="Search items to link"><ul class="pick-list" data-results></ul>';
    openPop(td, h, 320);
    var ids = cur.map(function (x) { return x.id; });
    pop.querySelectorAll("[data-remove]").forEach(function (b) {
      b.addEventListener("click", function () {
        var rid = +b.getAttribute("data-remove");
        closePop();
        saveValue(it.id, col.id, ids.filter(function (x) { return x !== rid; }));
      });
    });
    var input = pop.querySelector(".pop-search"), results = pop.querySelector("[data-results]"), timer = null;
    function search() {
      FW.api("GET", "/api/boards/" + BID + "/columns/" + col.id + "/link-search?q=" + encodeURIComponent(input.value.trim())).then(function (r) {
        results.innerHTML = r.note ? '<li class="muted">' + esc(r.note) + "</li>" : r.items.map(function (x) {
          return '<li data-id="' + x.id + '">' + esc(x.name) + '<span class="sub">' + esc(x.board) + (x.grp ? " · " + esc(x.grp) : "") + "</span></li>";
        }).join("") || '<li class="muted">No items found</li>';
        results.querySelectorAll("li[data-id]").forEach(function (li) {
          li.addEventListener("click", function () {
            closePop();
            saveValue(it.id, col.id, ids.concat([+li.getAttribute("data-id")]));
          });
        });
      }).catch(function (e) { results.innerHTML = '<li class="err-text">' + esc(e.message) + "</li>"; });
    }
    input.addEventListener("input", function () { clearTimeout(timer); timer = setTimeout(search, 250); });
    search();
  }

  // ---- labels and column settings ---------------------------------------------------------------------------------- //
  function labelEditor(anchor, col) {
    var labs = clone(col.settings.labels || []);
    var isStatus = col.type === "status";
    function rowsHtml() {
      return labs.map(function (l, i) {
        return '<div class="lab-row" data-i="' + i + '">' + (isStatus ? '<button class="swatch ' + c(l.color) + '" data-swatch="' + i + '" title="Colour"></button>' : "") +
          '<input type="text" value="' + esc(l.name) + '" data-name="' + i + '">' +
          (isStatus ? '<label class="check small" title="Counts as finished"><input type="checkbox" data-done="' + i + '" ' + (l.done ? "checked" : "") + "> done</label>" : "") +
          '<label class="check small" title="Hidden labels stay on items that have them but are not offered"><input type="checkbox" data-hidden="' + i + '" ' + (l.hidden ? "checked" : "") + "> hide</label></div>";
      }).join("");
    }
    function draw() {
      openPop(anchor, '<div class="pop-title">' + (isStatus ? "Labels" : "Options") + " - " + esc(col.title) + '</div><div class="label-editor">' + rowsHtml() +
        '</div><div class="palette hidden" data-palette>' + PALETTE.map(function (p) { return '<button class="' + c(p) + '" data-color="' + p + '"></button>'; }).join("") +
        '</div><div class="pop-actions"><button class="btn small ghost" data-pop="add">' + icon("plus", "ic sm") + ' Add</button><button class="btn small primary" data-pop="save">Save</button></div>', 380);
      var pickFor = null;
      pop.querySelectorAll("[data-swatch]").forEach(function (b) {
        b.addEventListener("click", function () { pickFor = +b.getAttribute("data-swatch"); pop.querySelector("[data-palette]").classList.remove("hidden"); });
      });
      pop.querySelectorAll("[data-color]").forEach(function (b) {
        b.addEventListener("click", function () { if (pickFor !== null) { collect(); labs[pickFor].color = b.getAttribute("data-color"); draw(); } });
      });
      pop.querySelector("[data-pop=add]").addEventListener("click", function () {
        collect();
        var id = labs.reduce(function (m, l) { return Math.max(m, l.id); }, -1) + 1;
        labs.push({ id: id, name: "New " + (isStatus ? "label" : "option"), color: PALETTE[labs.length % PALETTE.length] });
        draw();
      });
      pop.querySelector("[data-pop=save]").addEventListener("click", function () {
        collect();
        closePop();
        var settings = clone(col.settings);
        settings.labels = labs;
        FW.api("PATCH", "/api/columns/" + col.id, { settings: settings }).then(function () { return load(true); }).catch(function (e) { FW.toast(e.message, true); });
      });
    }
    function collect() {
      pop.querySelectorAll("[data-name]").forEach(function (inp) { labs[+inp.getAttribute("data-name")].name = inp.value; });
      pop.querySelectorAll("[data-done]").forEach(function (cb) { labs[+cb.getAttribute("data-done")].done = cb.checked; });
      pop.querySelectorAll("[data-hidden]").forEach(function (cb) { labs[+cb.getAttribute("data-hidden")].hidden = cb.checked; });
    }
    draw();
  }

  function columnSettings(anchor, col) {
    var s = col.settings || {};
    var h = '<div class="pop-title">Settings - ' + esc(col.title) + "</div>";
    h += '<label>Description <input name="description" value="' + esc(col.description || "") + '" placeholder="What goes in this column"></label>';
    if (col.type === "numbers") {
      h += '<label>Unit <input name="unit" value="' + esc(s.unit || "") + '" maxlength="8" placeholder="$"></label><label>Unit goes <select name="unit_position"><option value="left">before the number</option><option value="right" ' +
        (s.unit_position === "right" ? "selected" : "") + '>after the number</option></select></label><label>Decimal places <input name="decimals" type="number" min="0" max="6" value="' +
        (s.decimals == null ? "" : s.decimals) + '" placeholder="automatic"></label>';
    } else if (col.type === "formula") {
      h += '<label>Formula <input name="formula" value="' + esc(s.formula || "") + '"></label><p class="small muted">Use {key} for other columns: ' +
        S.itemCols.filter(function (x) { return ["numbers", "formula"].indexOf(x.type) >= 0; }).map(function (x) { return esc("{" + x.key + "} = " + x.title); }).join(", ") + "</p>";
    } else if (col.type === "people") {
      h += '<label>Most people allowed <input name="max_people" type="number" min="0" value="' + (s.max_people || 0) + '"></label><p class="small muted">0 = no limit</p>';
    }
    var needsBoards = col.type === "board_relation" || col.type === "mirror";
    h += needsBoards ? '<div data-extra class="small muted">Loading...</div>' : "";
    h += '<div class="pop-actions"><span></span><button class="btn small primary" data-pop="save">Save</button></div>';
    openPop(anchor, h, 340);
    var extra = pop.querySelector("[data-extra]");
    if (col.type === "board_relation") {
      FW.api("GET", "/api/boards").then(function (r) {
        var on = new Set(s.board_ids || []);
        extra.className = "";
        extra.innerHTML = '<div class="pop-title">Connected boards</div><input class="pop-search" placeholder="Search boards"><ul class="pick-list">' + r.boards.map(function (b) {
          return '<li data-board="' + b.id + '" class="' + (on.has(b.id) ? "on" : "") + '"><input type="checkbox" ' + (on.has(b.id) ? "checked" : "") + "> " + esc(b.name) + '<span class="sub">' + esc(b.workspace) + "</span></li>";
        }).join("") + "</ul>";
        pickListBehaviour(true);
      });
    } else if (col.type === "mirror") {
      var rels = S.itemCols.filter(function (x) { return x.type === "board_relation"; });
      extra.className = "";
      extra.innerHTML = '<label>Show from the items linked in <select name="relation_column">' + rels.map(function (x) {
        return '<option value="' + x.id + '" ' + (s.relation_column === x.id ? "selected" : "") + ">" + esc(x.title) + "</option>"; }).join("") +
        '</select></label><div data-targets class="small muted"></div>';
      var relSel = extra.querySelector("[name=relation_column]");
      var drawTargets = function () {
        var rel = S.cols[+relSel.value], box = extra.querySelector("[data-targets]");
        if (!rel) { box.textContent = "Add a Connect boards column first."; return; }
        var boards = rel.settings.board_ids || [];
        Promise.all(boards.map(function (b) { return FW.api("GET", "/api/boards/" + b).then(function (p) { return p; }); })).then(function (ps) {
          box.className = "";
          box.innerHTML = ps.map(function (p) {
            var cur = (s.targets || {})[String(p.board.id)];
            return '<label>Column on ' + esc(p.board.name) + ' <select data-target-board="' + p.board.id + '">' + p.columns.filter(function (x) {
              return x.scope === "item" && x.type !== "name"; }).map(function (x) {
              return '<option value="' + x.id + '" ' + (cur === x.id ? "selected" : "") + ">" + esc(x.title) + "</option>"; }).join("") + "</select></label>";
          }).join("") || "That column is not connected to a board yet.";
        });
      };
      relSel.addEventListener("change", drawTargets);
      drawTargets();
    }
    pop.querySelector("[data-pop=save]").addEventListener("click", function () {
      var ns = clone(s), body = { description: pop.querySelector("[name=description]").value };
      if (col.type === "numbers") {
        ns.unit = pop.querySelector("[name=unit]").value; ns.unit_position = pop.querySelector("[name=unit_position]").value;
        var d = pop.querySelector("[name=decimals]").value; if (d === "") delete ns.decimals; else ns.decimals = +d;
      } else if (col.type === "formula") ns.formula = pop.querySelector("[name=formula]").value;
      else if (col.type === "people") ns.max_people = +pop.querySelector("[name=max_people]").value || 0;
      else if (col.type === "board_relation") ns.board_ids = Array.prototype.map.call(pop.querySelectorAll("li.on[data-board]"), function (li) { return +li.getAttribute("data-board"); });
      else if (col.type === "mirror") {
        var rs = pop.querySelector("[name=relation_column]");
        ns.relation_column = rs ? +rs.value || null : null;
        ns.targets = {};
        pop.querySelectorAll("[data-target-board]").forEach(function (sel) { ns.targets[sel.getAttribute("data-target-board")] = +sel.value; });
      }
      body.settings = ns;
      closePop();
      FW.api("PATCH", "/api/columns/" + col.id, body).then(function () { return load(true); }).catch(function (e) { FW.toast(e.message, true); });
    });
  }

  function columnMenu(btn, col) {
    var h = '<div class="menu-list">' +
      '<button class="menu-btn" data-m="rename">' + icon("edit") + " Rename</button>" +
      (col.type === "status" || col.type === "dropdown" ? '<button class="menu-btn" data-m="labels">' + icon("edit") + " Edit labels</button>" : "") +
      '<button class="menu-btn" data-m="settings">' + icon("settings") + " Settings</button>" +
      '<button class="menu-btn" data-m="asc">' + icon("sort") + " Sort A to Z</button>" +
      '<button class="menu-btn" data-m="desc">' + icon("sort") + " Sort Z to A</button>" +
      '<button class="menu-btn" data-m="hide">' + icon("eye") + " Hide in this view</button>" +
      '<button class="menu-btn" data-m="left">' + icon("chevron") + " Move left</button>" +
      '<button class="menu-btn" data-m="right">' + icon("chevron") + " Move right</button>" +
      (col.type === "status" ? '<button class="menu-btn" data-m="kanban">' + icon("kanban") + " Group by this column</button>" : "") +
      '<button class="menu-btn danger" data-m="delete">' + icon("trash") + " Delete column</button></div>";
    openPop(btn, h, 220);
    pop.querySelectorAll("[data-m]").forEach(function (b) {
      b.addEventListener("click", function () {
        var m = b.getAttribute("data-m");
        if (m === "rename") {
          var t = window.prompt("Column name", col.title);
          closePop();
          if (t && t.trim()) FW.api("PATCH", "/api/columns/" + col.id, { title: t.trim() }).then(function () { load(true); }).catch(function (e) { FW.toast(e.message, true); });
        } else if (m === "labels") labelEditor(btn, col);
        else if (m === "settings") columnSettings(btn, col);
        else if (m === "asc" || m === "desc") { closePop(); changeSettings(function (st) { st.sort = [{ column: col.id, dir: m }]; }); }
        else if (m === "hide") { closePop(); changeSettings(function (st) { st.hidden = (st.hidden || []).concat([col.id]); }); }
        else if (m === "left" || m === "right") {
          closePop();
          var vis = visibleCols(), i = vis.indexOf(col), j = m === "left" ? i - 1 : i + 1;
          if (j < 0 || j >= vis.length) return;
          if (S.view) changeSettings(function (st) { var ids = vis.map(function (x) { return x.id; }); ids.splice(i, 1); ids.splice(j, 0, col.id); st.order = ids; });
          else FW.api("PATCH", "/api/columns/" + col.id, m === "left" ? { before: vis[j].id } : { after: vis[j].id }).then(function () { load(true); });
        } else if (m === "kanban") { closePop(); changeSettings(function (st) { st.group_by = col.id; }); }
        else if (m === "delete") {
          closePop();
          if (window.confirm("Delete the column “" + col.title + "”? Its values go with it (an admin can bring it back from the activity log).")) {
            FW.api("DELETE", "/api/columns/" + col.id).then(function () { load(true); }).catch(function (e) { FW.toast(e.message, true); });
          }
        }
      });
    });
  }

  function addColumn(btn) {
    var opts = document.getElementById("column-types").innerHTML;
    openPop(btn, '<div class="pop-title">Add a column</div><label>Type <select name="type">' + opts + '</select></label><label>Name <input name="title" placeholder="e.g. Reviewer"></label>' +
      '<div class="pop-actions"><span></span><button class="btn small primary" data-pop="add">Add column</button></div>', 260);
    var typeSel = pop.querySelector("[name=type]"), title = pop.querySelector("[name=title]");
    typeSel.addEventListener("change", function () { if (!title.value) title.placeholder = typeSel.selectedOptions[0].textContent; });
    function go() {
      var t = typeSel.value, name = title.value.trim() || typeSel.selectedOptions[0].textContent;
      closePop();
      var last = visibleCols().slice(-1)[0];
      FW.api("POST", "/api/boards/" + BID + "/columns", { type: t, title: name, after: last ? last.id : null }).then(function (r) {
        return load(true).then(function () {
          var col = S.cols[r.id];
          if (col && (t === "board_relation" || t === "mirror" || t === "formula")) {
            var th = boardEl.querySelector('th[data-col="' + r.id + '"]');
            if (th) columnSettings(th, col);
          }
        });
      }).catch(function (e) { FW.toast(e.message, true); });
    }
    pop.querySelector("[data-pop=add]").addEventListener("click", go);
    title.addEventListener("keydown", function (e) { if (e.key === "Enter") go(); });
  }

  // ---- view settings (filter, sort, columns, group by) -------------------------------------------------------------------- //
  function changeSettings(fn) {
    fn(S.settings);
    S.dirty = true;
    if (!S.view) { store("main", S.settings); }
    render();
  }

  function filterPop(btn) {
    var f = S.settings.filters || {}, rules = f.rules || [];
    var cols = S.itemCols.filter(function (x) { return ["status", "people", "dropdown", "checkbox"].indexOf(x.type) >= 0; });
    var h = '<div class="pop-title">Show only items where</div>';
    cols.forEach(function (col) {
      var r = rules.filter(function (x) { return x.column === col.id; })[0];
      var sel = new Set(r ? r.values : []);
      var vals;
      if (col.type === "status" || col.type === "dropdown") vals = (col.settings.labels || []).filter(function (l) { return !l.hidden; }).map(function (l) { return [l.id, l.name || "(blank)"]; });
      else if (col.type === "people") vals = [["me", "Me"]].concat(Object.keys(S.users).map(Number).map(user).filter(function (u) { return u.active; }).map(function (u) { return [u.id, u.name]; }));
      else vals = [[true, "Ticked"], [false, "Not ticked"]];
      h += '<details ' + (r ? "open" : "") + '><summary><strong>' + esc(col.title) + "</strong>" + (r ? " (" + r.values.length + ")" : "") + '</summary><ul class="pick-list" data-rule-col="' + col.id + '">' +
        vals.map(function (v) { return '<li data-v=\'' + esc(JSON.stringify(v[0])) + '\' class="' + (sel.has(v[0]) ? "on" : "") + '"><input type="checkbox" ' + (sel.has(v[0]) ? "checked" : "") + "> " + esc(v[1]) + "</li>"; }).join("") +
        "</ul></details>";
    });
    h += '<div class="pop-actions"><button class="btn small ghost" data-pop="clear">Clear all</button><button class="btn small primary" data-pop="apply">Apply</button></div>';
    openPop(btn, h, 300);
    pickListBehaviour(true);
    pop.querySelector("[data-pop=apply]").addEventListener("click", function () {
      var nr = [];
      pop.querySelectorAll("[data-rule-col]").forEach(function (ul) {
        var vals = Array.prototype.map.call(ul.querySelectorAll("li.on"), function (li) { return JSON.parse(li.getAttribute("data-v")); });
        if (vals.length) nr.push({ column: +ul.getAttribute("data-rule-col"), op: "any_of", values: vals });
      });
      closePop();
      changeSettings(function (st) { st.filters = st.filters || {}; if (nr.length) st.filters.rules = nr; else delete st.filters.rules; });
    });
    pop.querySelector("[data-pop=clear]").addEventListener("click", function () {
      closePop();
      changeSettings(function (st) { delete st.filters; });
      S.text = "";
      render();
    });
  }

  function sortPop(btn) {
    var srt = (S.settings.sort || [])[0] || {};
    var opts = '<option value="">(board order)</option><option value="name" ' + (srt.column === "name" ? "selected" : "") + ">" + esc(nameCol().title) + "</option>" +
      S.itemCols.filter(function (x) { return x.type !== "name"; }).map(function (x) { return '<option value="' + x.id + '" ' + (srt.column === x.id ? "selected" : "") + ">" + esc(x.title) + "</option>"; }).join("");
    openPop(btn, '<div class="pop-title">Sort items in each group by</div><select name="col">' + opts + '</select><label class="check"><input type="checkbox" name="desc" ' +
      (srt.dir === "desc" ? "checked" : "") + '> Reverse order</label><div class="pop-actions"><span></span><button class="btn small primary" data-pop="apply">Apply</button></div>', 260);
    pop.querySelector("[data-pop=apply]").addEventListener("click", function () {
      var v = pop.querySelector("[name=col]").value, d = pop.querySelector("[name=desc]").checked ? "desc" : "asc";
      closePop();
      changeSettings(function (st) { if (v) st.sort = [{ column: v === "name" ? "name" : +v, dir: d }]; else delete st.sort; });
    });
  }

  function hidePop(btn) {
    var hidden = new Set(S.settings.hidden || []);
    openPop(btn, '<div class="pop-title">Columns to show</div><ul class="pick-list">' + S.itemCols.filter(function (x) { return x.type !== "name"; }).map(function (x) {
      return '<li data-id="' + x.id + '" class="' + (hidden.has(x.id) ? "" : "on") + '"><input type="checkbox" ' + (hidden.has(x.id) ? "" : "checked") + "> " + esc(x.title) + "</li>";
    }).join("") + '</ul><div class="pop-actions"><button class="btn small ghost" data-pop="all">Show all</button><button class="btn small primary" data-pop="apply">Apply</button></div>', 260);
    pickListBehaviour(true);
    pop.querySelector("[data-pop=apply]").addEventListener("click", function () {
      var hid = Array.prototype.map.call(pop.querySelectorAll(".pick-list li:not(.on)"), function (li) { return +li.getAttribute("data-id"); });
      closePop();
      changeSettings(function (st) { if (hid.length) st.hidden = hid; else delete st.hidden; });
    });
    pop.querySelector("[data-pop=all]").addEventListener("click", function () { closePop(); changeSettings(function (st) { delete st.hidden; }); });
  }

  function groupByPop(btn) {
    var cur = S.settings.group_by || "";
    var cols = S.itemCols.filter(function (x) { return ["status", "people", "dropdown", "text", "date", "board_relation", "checkbox"].indexOf(x.type) >= 0; });
    openPop(btn, '<div class="pop-title">Group items by</div><select name="col"><option value="">The board\'s groups</option>' + cols.map(function (x) {
      return '<option value="' + x.id + '" ' + (cur === x.id ? "selected" : "") + ">" + esc(x.title) + "</option>"; }).join("") +
      '</select><label class="check"><input type="checkbox" name="hide" ' + (S.settings.hide_empty_groups ? "checked" : "") + '> Hide empty groups</label><div class="pop-actions"><span></span><button class="btn small primary" data-pop="apply">Apply</button></div>', 260);
    pop.querySelector("[data-pop=apply]").addEventListener("click", function () {
      var v = pop.querySelector("[name=col]").value, hide = pop.querySelector("[name=hide]").checked;
      closePop();
      changeSettings(function (st) { if (v) st.group_by = +v; else delete st.group_by; if (hide) st.hide_empty_groups = true; else delete st.hide_empty_groups; });
    });
  }

  function kanbanColPop(btn) {
    var cols = S.itemCols.filter(function (x) { return x.type === "status"; });
    var cur = S.settings.kanban_column || (mainStatus("item") || {}).id;
    openPop(btn, '<div class="pop-title">Cards are grouped by</div><select name="col">' + cols.map(function (x) {
      return '<option value="' + x.id + '" ' + (cur === x.id ? "selected" : "") + ">" + esc(x.title) + "</option>"; }).join("") +
      '</select><div class="pop-actions"><span></span><button class="btn small primary" data-pop="apply">Apply</button></div>');
    pop.querySelector("[data-pop=apply]").addEventListener("click", function () {
      var v = +pop.querySelector("[name=col]").value;
      closePop();
      changeSettings(function (st) { st.kanban_column = v; });
    });
  }

  function newView() {
    dlg.innerHTML = '<form method="dialog" class="dlg-body"><h2>New view</h2><label>Name <input name="name" required maxlength="80" placeholder="e.g. My clients, Review queue"></label>' +
      '<label>Type <select name="type"><option value="table">Table</option><option value="kanban">Kanban (cards by status)</option></select></label>' +
      '<p class="small muted">The view starts with the search, filters, sorting and columns you have now. Everyone sees saved views.</p>' +
      '<div class="dialog-actions"><button type="button" class="btn ghost" data-close>Cancel</button><button class="btn primary" value="ok">Save view</button></div></form>';
    dlg.showModal();
    dlg.querySelector("form").addEventListener("submit", function (e) {
      e.preventDefault();
      var name = dlg.querySelector("[name=name]").value.trim(), type = dlg.querySelector("[name=type]").value;
      var st = clone(S.settings);
      if (S.text) { st.filters = st.filters || {}; st.filters.text = S.text; }
      dlg.close();
      FW.api("POST", "/api/boards/" + BID + "/views", { name: name, type: type, settings: st }).then(function (r) {
        S.data.views = r.views;
        selectView(r.id);
      }).catch(function (e2) { FW.toast(e2.message, true); });
    });
  }

  // ---- panel: updates, files, activity, details ----------------------------------------------------------------------------- //
  function openPanel(itemId, refresh, tab) {
    if (tab) S.panelTab = tab;
    var composing = refresh && panel.querySelector(".editor") && (panel.querySelector(".editor").textContent.trim() || document.activeElement && panel.contains(document.activeElement));
    if (composing) return;
    S.panelItem = itemId;
    boardEl.querySelectorAll("tr.row.selected").forEach(function (r) { r.classList.remove("selected"); });
    var row = boardEl.querySelector('tr.row[data-item="' + itemId + '"]');
    if (row) row.classList.add("selected");
    var url = new URL(location.href);
    url.searchParams.set("item", itemId);
    history.replaceState(null, "", url);
    FW.api("GET", "/api/items/" + itemId).then(function (d) { drawPanel(d); }).catch(function (e) { FW.toast(e.message, true); closePanel(); });
  }

  function closePanel() {
    panel.hidden = true;
    panel.innerHTML = "";
    S.panelItem = null;
    boardEl.querySelectorAll("tr.row.selected").forEach(function (r) { r.classList.remove("selected"); });
    var url = new URL(location.href);
    url.searchParams.delete("item");
    history.replaceState(null, "", url);
  }

  function refreshPanelIfOpen(itemId, items) {
    if (!S.panelItem) return;
    var top = parentOf(S.panelItem) || findItem(S.panelItem);
    if (top && (items || []).some(function (x) { return x.id === top.id; }) && S.panelTab === "details") openPanel(S.panelItem, true);
  }

  function drawPanel(d) {
    var payload = d.item;
    var it = payload && payload.id === d.id ? payload : payload && (payload.subitems || []).filter(function (s) { return s.id === d.id; })[0];
    if (!it) { it = findItem(d.id) || { id: d.id, name: "(archived item)", values: {}, computed: {} }; }
    var isSub = !!d.parent_id;
    var g = S.groups.filter(function (x) { return payload && x.id === payload.group_id; })[0];
    var tabsH = [["updates", "Updates", d.updates.length], ["files", "Files", d.files.length], ["activity", "Activity", 0], ["details", "Details", 0]].map(function (t) {
      return '<button class="view-tab ' + (S.panelTab === t[0] ? "active" : "") + '" data-tab="' + t[0] + '">' + t[1] + (t[2] ? " / " + t[2] : "") + "</button>";
    }).join("");
    var h = '<button class="icon-btn drawer-close" data-act="close-panel" title="Close">' + icon("x") + "</button>" +
      '<div class="drawer-head"><div class="crumbs">' + esc(S.data.board.name) + (g ? " · " + esc(g.title) : "") + (isSub ? " · subitem of " + esc(payload.name) : "") +
      (d.state !== "active" ? ' · <span class="tag">' + esc(d.state) + "</span>" : "") + "</div>" +
      '<h2 data-act="panel-rename">' + esc(it.name) + '</h2><div class="drawer-tabs">' + tabsH + "</div></div><div class=\"drawer-body\">";
    if (S.panelTab === "updates") h += updatesHtml(d);
    else if (S.panelTab === "files") h += filesHtml(d);
    else if (S.panelTab === "activity") h += '<ul class="activity-list">' + (d.activity.map(function (a) {
      return '<li><span class="when">' + esc(FW.when(a.at)) + "</span><span><strong>" + esc(a.user_name || ((a.detail || {}).automation ? "Automation" : "ForWork")) + "</strong> " + esc(a.text) + "</span></li>";
    }).join("") || '<li class="muted">No changes recorded.</li>') + "</ul>";
    else h += detailsHtml(it, isSub);
    h += "</div>";
    panel.innerHTML = h;
    panel.hidden = false;
    panel.setAttribute("data-item", d.id);
    FW.paint(panel);
    panel.querySelectorAll("[data-tab]").forEach(function (b) {
      b.addEventListener("click", function () { S.panelTab = b.getAttribute("data-tab"); drawPanel(d); });
    });
    wirePanel(d, it);
  }

  function updatesHtml(d) {
    var h = "";
    if (S.canEdit) h += '<div class="editor-box" data-new-update>' + editorToolbar() + '<div class="editor" contenteditable="true" data-placeholder="Write an update... type @ to mention someone"></div>' +
      '<div class="editor-actions"><button class="btn primary small" data-act="post-update">Update</button></div></div>';
    if (!d.updates.length) h += '<p class="muted">No updates yet. Updates are the place for questions, notes and decisions about this item.</p>';
    d.updates.forEach(function (u) {
      var who = u.created_by ? user(u.created_by) : { name: u.author || "Someone", initials: FW.initials(u.author), color: "#c4c4c4", active: false };
      h += '<article class="update" data-update="' + u.id + '"><div class="update-head">' + avatar(who, "sm") + '<span class="who">' + esc(u.author || who.name) +
        '</span><span class="when" title="' + esc(u.created_at) + '">' + esc(FW.when(u.created_at)) + (u.edited_at ? " (edited)" : "") + "</span></div>" +
        '<div class="update-body">' + u.body + "</div>" + filesChips(u.files) +
        '<div class="update-foot">' + (S.canEdit ? '<button class="btn small ghost" data-act="reply">' + icon("reply", "ic sm") + " Reply</button>" : "") +
        (u.created_by === S.me ? '<button class="btn small ghost" data-act="edit-update">' + icon("edit", "ic sm") + ' Edit</button><button class="btn small ghost danger" data-act="delete-update">' + icon("trash", "ic sm") + "</button>" : "") +
        "</div>";
      if (u.replies.length) {
        h += '<div class="replies">' + u.replies.map(function (r) {
          var rw = r.created_by ? user(r.created_by) : { name: r.author, initials: FW.initials(r.author), color: "#c4c4c4", active: false };
          return '<div class="reply" data-update="' + r.id + '">' + avatar(rw, "xs") + '<div><div class="small"><strong>' + esc(r.author || rw.name) + "</strong> <span class=\"muted\">" + esc(FW.when(r.created_at)) +
            '</span></div><div class="update-body">' + r.body + "</div>" + filesChips(r.files) + "</div></div>";
        }).join("") + "</div>";
      }
      h += "</article>";
    });
    return h;
  }
  function filesChips(files) {
    if (!files || !files.length) return "";
    return '<div class="update-files">' + files.map(function (f) {
      return f.sha256 ? '<a class="file-chip" href="/files/' + f.id + '" target="_blank" rel="noopener">' + icon("paperclip", "ic tiny") + " " + esc(f.name) + "</a>"
        : '<span class="file-chip gone" title="Uploaded to monday.com - it stays there">' + icon("paperclip", "ic tiny") + " " + esc(f.name) + "</span>";
    }).join("") + "</div>";
  }
  function editorToolbar() {
    return '<div class="toolbar-mini"><button type="button" data-cmd="bold" title="Bold">B</button><button type="button" data-cmd="italic" title="Italic"><i>I</i></button>' +
      '<button type="button" data-cmd="underline" title="Underline"><u>U</u></button><button type="button" data-cmd="insertUnorderedList" title="List">•</button>' +
      '<button type="button" data-cmd="link" title="Link">' + icon("link", "ic sm") + "</button></div>";
  }
  function filesHtml(d) {
    var h = S.canEdit ? '<label class="btn">' + icon("paperclip") + ' Add a file<input type="file" data-upload hidden></label>' : "";
    h += '<ul class="file-list">' + (d.files.map(function (f) {
      var where = f.link ? "link" : f.stored ? (f.size ? (f.size / 1048576).toFixed(2) + " MB" : "") : "kept in monday.com";
      return "<li>" + icon("file") + (f.link || f.stored ? '<a href="/files/' + f.id + '" target="_blank" rel="noopener">' + esc(f.name) + "</a>" : '<span class="muted">' + esc(f.name) + "</span>") +
        '<span class="muted small">' + esc(where) + "</span>" + '<span class="spacer"></span>' +
        (S.canEdit && f.stored ? '<button class="btn small ghost danger" data-del-file="' + f.id + '">' + icon("trash", "ic sm") + "</button>" : "") + "</li>";
    }).join("") || '<li class="muted">No files.</li>') + "</ul>";
    return h;
  }
  function detailsHtml(it, isSub) {
    var cols = (isSub ? S.subCols : S.itemCols).filter(function (x) { return x.type !== "name"; });
    return '<div class="detail-list">' + cols.map(function (col) {
      var ro = !S.canEdit || !editable(col);
      return '<div class="dl-label">' + esc(col.title) + '</div><div class="dl-value"><div class="row" data-item="' + it.id + '"><div class="cell t-' + col.type + (ro ? " ro" : "") +
        '" data-col="' + col.id + '">' + cellHtml(col, it) + "</div></div></div>";
    }).join("") + "</div>" + (it.created_at ? '<p class="small muted">Created ' + esc(FW.when(it.created_at)) + (it.created_by ? " by " + esc(user(it.created_by).name) : "") + "</p>" : "");
  }

  function wirePanel(d, it) {
    panel.querySelectorAll(".toolbar-mini [data-cmd]").forEach(function (b) {
      b.addEventListener("mousedown", function (e) {
        e.preventDefault();
        var cmd = b.getAttribute("data-cmd");
        if (cmd === "link") { var u = window.prompt("Link address (https://...)"); if (u && /^(https?:|mailto:)/i.test(u.trim())) document.execCommand("createLink", false, u.trim()); }
        else document.execCommand(cmd, false, null);
      });
    });
    panel.querySelectorAll(".editor").forEach(mentionable);
    var post = panel.querySelector("[data-act=post-update]");
    if (post) post.addEventListener("click", function () {
      var ed = panel.querySelector("[data-new-update] .editor");
      if (!ed.textContent.trim()) return;
      post.disabled = true;
      FW.api("POST", "/api/items/" + d.id + "/updates", { body: ed.innerHTML }).then(function (r) {
        d.updates = r.updates;
        applyItems(r.items);
        ed.innerHTML = "";
        drawPanel(d);
      }).catch(function (e) { FW.toast(e.message, true); post.disabled = false; });
    });
    panel.querySelectorAll("[data-act=reply]").forEach(function (b) {
      b.addEventListener("click", function () {
        var art = b.closest(".update");
        if (art.querySelector(".reply-box")) return;
        var box = document.createElement("div");
        box.className = "reply-box";
        box.innerHTML = '<div class="editor" contenteditable="true" data-placeholder="Write a reply..."></div><button class="btn small primary">Reply</button>';
        art.appendChild(box);
        var ed = box.querySelector(".editor");
        mentionable(ed);
        ed.focus();
        box.querySelector("button").addEventListener("click", function () {
          if (!ed.textContent.trim()) return;
          FW.api("POST", "/api/items/" + d.id + "/updates", { body: ed.innerHTML, parent_id: +art.getAttribute("data-update") }).then(function (r) {
            d.updates = r.updates;
            applyItems(r.items);
            drawPanel(d);
          }).catch(function (e) { FW.toast(e.message, true); });
        });
      });
    });
    panel.querySelectorAll("[data-act=edit-update]").forEach(function (b) {
      b.addEventListener("click", function () {
        var art = b.closest(".update"), body = art.querySelector(".update-body");
        body.setAttribute("contenteditable", "true");
        body.classList.add("editor");
        body.focus();
        b.textContent = "Save";
        b.addEventListener("click", function save() {
          FW.api("PATCH", "/api/updates/" + art.getAttribute("data-update"), { body: body.innerHTML }).then(function (r) {
            d.updates = r.updates; drawPanel(d);
          }).catch(function (e) { FW.toast(e.message, true); });
          b.removeEventListener("click", save);
        }, { once: true });
      }, { once: true });
    });
    panel.querySelectorAll("[data-act=delete-update]").forEach(function (b) {
      b.addEventListener("click", function () {
        if (!window.confirm("Delete this update and its replies?")) return;
        FW.api("DELETE", "/api/updates/" + b.closest(".update").getAttribute("data-update")).then(function (r) {
          d.updates = r.updates; applyItems(r.items); drawPanel(d);
        }).catch(function (e) { FW.toast(e.message, true); });
      });
    });
    var up = panel.querySelector("[data-upload]");
    if (up) up.addEventListener("change", function () {
      if (!up.files[0]) return;
      var fd = new FormData();
      fd.append("file", up.files[0]);
      FW.toast("Uploading " + up.files[0].name + "...");
      FW.api("POST", "/api/items/" + d.id + "/files", fd).then(function (r) { applyItems(r.items); openPanel(d.id, false, "files"); })
        .catch(function (e) { FW.toast(e.message, true); });
    });
    panel.querySelectorAll("[data-del-file]").forEach(function (b) {
      b.addEventListener("click", function () {
        if (!window.confirm("Delete this file?")) return;
        FW.api("DELETE", "/api/files/" + b.getAttribute("data-del-file")).then(function (r) { applyItems(r.items); openPanel(d.id, false, "files"); })
          .catch(function (e) { FW.toast(e.message, true); });
      });
    });
    var title = panel.querySelector("[data-act=panel-rename]");
    if (title && S.canEdit && d.state === "active") title.addEventListener("click", function () {
      var t = window.prompt("Item name", it.name);
      if (t && t.trim() && t.trim() !== it.name) FW.api("PATCH", "/api/items/" + d.id, { name: t.trim() }).then(function (r) { applyItems(r.items); openPanel(d.id, true); })
        .catch(function (e) { FW.toast(e.message, true); });
    });
    panel.querySelectorAll(".detail-list .cell").forEach(function (cell) {
      cell.addEventListener("click", function () { if (!cell.classList.contains("ro")) editCell(cell); });
    });
  }

  // @mentions: typing @ shows the people list; picking someone inserts a mention they are notified of
  function mentionable(ed) {
    ed.addEventListener("keyup", function (e) {
      if (["ArrowUp", "ArrowDown", "Enter", "Escape"].indexOf(e.key) >= 0) return;
      var sel = window.getSelection();
      if (!sel.rangeCount) return;
      var node = sel.anchorNode, off = sel.anchorOffset;
      if (!node || node.nodeType !== 3) { if (!pop.hidden && pop.querySelector("[data-mention]")) closePop(); return; }
      var before = node.textContent.slice(0, off);
      var m = before.match(/(^|\s)@([\w.'-]{0,30})$/);
      if (!m) { if (!pop.hidden && pop.querySelector("[data-mention]")) closePop(); return; }
      var q = m[2].toLowerCase();
      var list = Object.keys(S.users).map(Number).map(user).filter(function (u) { return u.active && u.name.toLowerCase().indexOf(q) >= 0; }).slice(0, 8);
      if (!list.length) { closePop(); return; }
      var range = sel.getRangeAt(0).cloneRange();
      openPop(ed, '<ul class="pick-list" data-mention>' + list.map(function (u) { return '<li data-user="' + u.id + '">' + avatar(u, "sm") + " " + esc(u.name) + "</li>"; }).join("") + "</ul>", 240);
      ed.focus();
      pop.querySelectorAll("[data-user]").forEach(function (li) {
        li.addEventListener("mousedown", function (ev) {
          ev.preventDefault();
          var u = user(+li.getAttribute("data-user"));
          var r = document.createRange();
          r.setStart(node, off - m[2].length - 1);
          r.setEnd(node, off);
          r.deleteContents();
          var span = document.createElement("span");
          span.className = "mention";
          span.setAttribute("data-user", u.id);
          span.setAttribute("contenteditable", "false");
          span.textContent = "@" + u.name;
          r.insertNode(span);
          var space = document.createTextNode(" ");
          span.after(space);
          var s2 = window.getSelection();
          s2.removeAllRanges();
          var r2 = document.createRange();
          r2.setStartAfter(space);
          s2.addRange(r2);
          closePop();
          ed.focus();
        });
      });
      void range;
    });
  }

  // ---- menus for items, groups and the board ----------------------------------------------------------------------------------- //
  function itemMenu(btn, it) {
    var isSub = !!parentOf(it.id);
    var h = '<div class="menu-list"><button class="menu-btn" data-m="open">' + icon("chat") + " Open</button>" +
      '<button class="menu-btn" data-m="dup">' + icon("copy") + " Duplicate</button>" +
      (!isSub ? '<button class="menu-btn" data-m="sub">' + icon("plus") + " Add subitem</button>" : "") +
      (!isSub ? '<div class="pop-title">Move to group</div>' + S.groups.filter(function (g) { return g.id !== it.group_id; }).map(function (g) {
        return '<button class="menu-btn" data-group="' + g.id + '"><span class="ws-dot ' + c(g.color) + '"></span> ' + esc(g.title) + "</button>"; }).join("") +
        '<button class="menu-btn" data-m="board">' + icon("move") + " Move to another board</button>" : "") +
      '<hr><button class="menu-btn" data-m="archive">' + icon("archive") + " Archive</button><button class=\"menu-btn danger\" data-m=\"delete\">" + icon("trash") + " Delete</button></div>";
    openPop(btn, h, 240);
    pop.querySelectorAll("[data-group]").forEach(function (b) {
      b.addEventListener("click", function () {
        closePop();
        FW.api("POST", "/api/items/" + it.id + "/move", { group_id: +b.getAttribute("data-group") }).then(function (r) { applyItems(r.items); }).catch(function (e) { FW.toast(e.message, true); });
      });
    });
    pop.querySelectorAll("[data-m]").forEach(function (b) {
      b.addEventListener("click", function () {
        var m = b.getAttribute("data-m");
        closePop();
        if (m === "open") openPanel(it.id);
        else if (m === "dup") FW.api("POST", "/api/items/" + it.id + "/duplicate", {}).then(function (r) { applyItems(r.items); }).catch(function (e) { FW.toast(e.message, true); });
        else if (m === "sub") { S.expanded.add(it.id); renderBody(); var inp = boardEl.querySelector('[data-add-sub="' + it.id + '"]'); if (inp) inp.focus(); }
        else if (m === "board") moveToBoard(it);
        else if (m === "archive" || m === "delete") {
          if (m === "delete" && !window.confirm("Delete “" + it.name + "”? An admin can restore it from the bin.")) return;
          FW.api("POST", "/api/items/" + it.id + "/state", { state: m === "archive" ? "archived" : "deleted" }).then(function () {
            var parent = parentOf(it.id);
            if (parent) return FW.api("GET", API + "/changes?since=" + (S.seq)).then(function (ch) { S.seq = ch.seq; applyItems(ch.items, ch.removed); });
            applyItems([], [it.id]);
            FW.toast(m === "archive" ? "Archived." : "Deleted.");
          }).catch(function (e) { FW.toast(e.message, true); });
        }
      });
    });
  }

  function moveToBoard(it) {
    FW.api("GET", "/api/boards").then(function (r) {
      dlg.innerHTML = '<form method="dialog" class="dlg-body"><h2>Move “' + esc(it.name) + '”</h2><label>To board <select name="board">' + r.boards.filter(function (b) { return b.id !== BID; }).map(function (b) {
        return '<option value="' + b.id + '">' + esc(b.name) + " (" + esc(b.workspace) + ")</option>"; }).join("") +
        '</select></label><p class="small muted">Values move to columns with the same name and type on the other board. Updates and subitems go too.</p>' +
        '<div class="dialog-actions"><button type="button" class="btn ghost" data-close>Cancel</button><button class="btn primary">Move</button></div></form>';
      dlg.showModal();
      dlg.querySelector("form").addEventListener("submit", function (e) {
        e.preventDefault();
        var to = +dlg.querySelector("[name=board]").value;
        dlg.close();
        FW.api("POST", "/api/items/" + it.id + "/move-board", { board_id: to }).then(function () { applyItems([], [it.id]); FW.toast("Moved."); })
          .catch(function (e2) { FW.toast(e2.message, true); });
      });
    });
  }

  function groupMenu(btn, gid) {
    var g = S.groups.filter(function (x) { return x.id === gid; })[0];
    if (!g) return;
    var h = '<div class="menu-list"><button class="menu-btn" data-m="rename">' + icon("edit") + " Rename group</button>" +
      '<div class="pop-title">Colour</div><div class="palette">' + PALETTE.map(function (p) { return '<button class="' + c(p) + (p === g.color ? " current" : "") + '" data-color="' + p + '"></button>'; }).join("") + "</div>" +
      '<button class="menu-btn" data-m="add">' + icon("plus") + " Add group</button>" +
      '<button class="menu-btn" data-m="collapse">' + icon("group") + " Collapse all groups</button>" +
      '<button class="menu-btn" data-m="expand">' + icon("group") + " Expand all groups</button>" +
      '<button class="menu-btn" data-m="up">' + icon("chevron") + " Move up</button><button class=\"menu-btn\" data-m=\"down\">" + icon("chevron") + " Move down</button>" +
      '<hr><button class="menu-btn" data-m="archive">' + icon("archive") + " Archive group</button><button class=\"menu-btn danger\" data-m=\"delete\">" + icon("trash") + " Delete group</button></div>";
    openPop(btn, h, 250);
    pop.querySelectorAll("[data-color]").forEach(function (b) {
      b.addEventListener("click", function () {
        closePop();
        FW.api("PATCH", "/api/groups/" + gid, { color: b.getAttribute("data-color") }).then(function () { load(true); }).catch(function (e) { FW.toast(e.message, true); });
      });
    });
    pop.querySelectorAll("[data-m]").forEach(function (b) {
      b.addEventListener("click", function () {
        var m = b.getAttribute("data-m");
        closePop();
        var i = S.groups.indexOf(g);
        if (m === "rename") renameGroup(boardEl.querySelector('[data-group="' + gid + '"] .group-title'), g);
        else if (m === "add") FW.api("POST", "/api/boards/" + BID + "/groups", { title: "New group", where: "top" }).then(function () { load(true); });
        else if (m === "collapse") { S.groups.forEach(function (x) { S.collapsed.add("g" + x.id); }); store("collapsed", Array.from(S.collapsed)); renderBody(); }
        else if (m === "expand") { S.collapsed.clear(); store("collapsed", []); renderBody(); }
        else if (m === "up" && i > 0) FW.api("PATCH", "/api/groups/" + gid, { before: S.groups[i - 1].id }).then(function () { load(true); });
        else if (m === "down" && i < S.groups.length - 1) FW.api("PATCH", "/api/groups/" + gid, { after: S.groups[i + 1].id }).then(function () { load(true); });
        else if (m === "archive" || m === "delete") {
          var n = 0; S.items.forEach(function (it) { if (it.group_id === gid) n++; });
          if (!window.confirm((m === "delete" ? "Delete" : "Archive") + " the group “" + g.title + "” and its " + n + " item" + (n === 1 ? "" : "s") + "?")) return;
          FW.api("DELETE", "/api/groups/" + gid + (m === "archive" ? "?archive=1" : "")).then(function () { load(true); }).catch(function (e) { FW.toast(e.message, true); });
        }
      });
    });
  }

  function renameGroup(el, g) {
    if (!el) return;
    el.setAttribute("contenteditable", "true");
    el.focus();
    document.getSelection().selectAllChildren(el);
    S.editing = true;
    function done(save) {
      el.removeAttribute("contenteditable");
      S.editing = false;
      var t = el.textContent.trim();
      if (save && t && t !== g.title) FW.api("PATCH", "/api/groups/" + g.id, { title: t }).then(function () { g.title = t; }).catch(function (e) { el.textContent = g.title; FW.toast(e.message, true); });
      else el.textContent = g.title;
    }
    el.addEventListener("keydown", function k(e) {
      if (e.key === "Enter") { e.preventDefault(); el.removeEventListener("keydown", k); done(true); }
      if (e.key === "Escape") { el.removeEventListener("keydown", k); done(false); }
    });
    el.addEventListener("blur", function () { if (el.hasAttribute("contenteditable")) done(true); }, { once: true });
  }

  function renameItem(el, it) {
    var td = el.closest(".name-wrap");
    var old = el.textContent;
    el.outerHTML = '<input class="cell-input" value="' + esc(it.name) + '" maxlength="500">';
    var input = td.querySelector("input");
    input.focus();
    input.select();
    S.editing = true;
    var done = false;
    function finish(save) {
      if (done) return;
      done = true;
      S.editing = false;
      var t = input.value.trim();
      if (save && t && t !== it.name) {
        FW.api("PATCH", "/api/items/" + it.id, { name: t }).then(function (r) { applyItems(r.items); }).catch(function (e) { FW.toast(e.message, true); renderBody(); });
      } else {
        var span = document.createElement("span");
        span.className = "name-text";
        span.setAttribute("data-act", "rename-item");
        span.textContent = old;
        input.replaceWith(span);
      }
    }
    input.addEventListener("keydown", function (e) { if (e.key === "Enter") finish(true); if (e.key === "Escape") { e.stopPropagation(); finish(false); } });
    input.addEventListener("blur", function () { finish(true); });
  }

  // ---- clicks on the board ---------------------------------------------------------------------------------------------------- //
  boardEl.addEventListener("click", function (ev) {
    var act = ev.target.closest("[data-act]");
    var tr = ev.target.closest("tr.row");
    var it = tr ? findItem(+tr.getAttribute("data-item")) : null;
    if (act) {
      var a = act.getAttribute("data-act");
      if (a === "toggle-group") {
        var key = act.closest(".group").getAttribute("data-key");
        if (S.collapsed.has(key)) S.collapsed.delete(key); else S.collapsed.add(key);
        store("collapsed", Array.from(S.collapsed));
        renderBody();
      } else if (a === "toggle-sub" && it) { if (S.expanded.has(it.id)) S.expanded.delete(it.id); else S.expanded.add(it.id); applyItemsRedraw(it); }
      else if (a === "open-item" && it) openPanel(it.id);
      else if (a === "item-menu" && it) itemMenu(act, it);
      else if (a === "rename-item" && it && S.canEdit) renameItem(act, it);
      else if (a === "rename-group" && S.canEdit) renameGroup(act, S.groups.filter(function (g) { return g.id === +act.closest(".group").getAttribute("data-group"); })[0]);
      else if (a === "group-menu") groupMenu(act, +act.closest(".group").getAttribute("data-group"));
      else if (a === "col-menu") { ev.stopPropagation(); columnMenu(act, S.cols[+act.getAttribute("data-col")]); }
      else if (a === "add-col") addColumn(act);
      else if (a === "add-group") FW.api("POST", "/api/boards/" + BID + "/groups", { title: "New group", where: "bottom" }).then(function () { load(true); }).catch(function (e) { FW.toast(e.message, true); });
      return;
    }
    var td = ev.target.closest("td.cell");
    if (td && !td.classList.contains("ro") && !td.classList.contains("c-name") && !ev.target.closest("a")) editCell(td);
    var card = ev.target.closest(".kan-card");
    if (card) openPanel(+card.getAttribute("data-item"));
  });
  function applyItemsRedraw(it) {
    var top = parentOf(it.id) || it;
    var row = boardEl.querySelector('tr.row[data-item="' + top.id + '"]:not(.sub-row)');
    if (!row) { renderBody(); return; }
    var sub = boardEl.querySelector('tr.sub-wrap[data-parent="' + top.id + '"]');
    if (sub) sub.remove();
    var tmp = document.createElement("tbody");
    tmp.innerHTML = rowHtml(top, visibleCols());
    FW.paint(tmp);
    row.replaceWith.apply(row, Array.prototype.slice.call(tmp.children));
  }

  // adding items and subitems
  boardEl.addEventListener("keydown", function (ev) {
    var t = ev.target;
    if (ev.key !== "Enter" || t.tagName !== "INPUT") return;
    if (t.hasAttribute("data-add-item")) {
      var name = t.value.trim();
      if (!name) return;
      var gid = +t.getAttribute("data-add-item") || (S.groups[0] || {}).id;
      t.value = "";
      FW.api("POST", "/api/boards/" + BID + "/items", { name: name, group_id: gid }).then(function (r) {
        applyItems(r.items);
        var again = boardEl.querySelector('[data-add-item="' + (t.getAttribute("data-add-item")) + '"]');
        if (again) again.focus();
      }).catch(function (e) { FW.toast(e.message, true); t.value = name; });
    } else if (t.hasAttribute("data-add-sub")) {
      var sname = t.value.trim(), pid = +t.getAttribute("data-add-sub");
      if (!sname) return;
      t.value = "";
      FW.api("POST", "/api/boards/" + BID + "/items", { name: sname, parent_id: pid }).then(function (r) {
        applyItems(r.items);
        applyItemsRedraw(S.items.get(pid));
        var again = boardEl.querySelector('[data-add-sub="' + pid + '"]');
        if (again) again.focus();
      }).catch(function (e) { FW.toast(e.message, true); t.value = sname; });
    }
  });

  // column resizing
  boardEl.addEventListener("mousedown", function (ev) {
    var h = ev.target.closest(".resize");
    if (!h) return;
    ev.preventDefault();
    var col = S.cols[+h.getAttribute("data-col")], startX = ev.clientX, startW = colW(col);
    var th = h.closest("th"), w = startW;
    function move(e) { w = Math.max(60, Math.min(800, startW + e.clientX - startX)); th.classList.add(FW.cls("width", w + "px")); }
    function up() {
      document.removeEventListener("mousemove", move);
      document.removeEventListener("mouseup", up);
      if (w !== startW) { col.width = w; FW.api("PATCH", "/api/columns/" + col.id, { width: w }).then(function () { renderBody(); }).catch(function (e) { FW.toast(e.message, true); }); }
    }
    document.addEventListener("mousemove", move);
    document.addEventListener("mouseup", up);
  });

  // drag and drop: rows between groups / positions, and kanban cards between labels
  boardEl.addEventListener("dragstart", function (ev) {
    var handle = ev.target.closest(".drag"), card = ev.target.closest(".kan-card");
    if (handle) { S.drag = +handle.closest("tr.row").getAttribute("data-item"); }
    else if (card) { S.drag = +card.getAttribute("data-item"); }
    else return;
    ev.dataTransfer.effectAllowed = "move";
    ev.dataTransfer.setData("text/plain", String(S.drag));
  });
  boardEl.addEventListener("dragover", function (ev) {
    if (!S.drag) return;
    var row = ev.target.closest("tr.row:not(.sub-row)"), grp = ev.target.closest(".group[data-group]"), col = ev.target.closest(".kan-col");
    if (!row && !grp && !col) return;
    ev.preventDefault();
    boardEl.querySelectorAll(".drop-before, .drop-into, .drop").forEach(function (x) { x.classList.remove("drop-before", "drop-into", "drop"); });
    if (row) row.classList.add("drop-before"); else if (grp) grp.classList.add("drop-into"); else col.classList.add("drop");
  });
  boardEl.addEventListener("dragleave", function (ev) { if (!boardEl.contains(ev.relatedTarget)) clearDrop(); });
  boardEl.addEventListener("drop", function (ev) {
    if (!S.drag) return;
    ev.preventDefault();
    var id = S.drag, row = ev.target.closest("tr.row:not(.sub-row)"), grp = ev.target.closest(".group[data-group]"), col = ev.target.closest(".kan-col");
    clearDrop();
    S.drag = null;
    if (col) {
      var kc = S.cols[S.settings.kanban_column] || mainStatus("item");
      var lab = col.getAttribute("data-label");
      saveValue(id, kc.id, lab === "" ? null : +lab);
      return;
    }
    var body = {};
    if (row) {
      var target = +row.getAttribute("data-item");
      if (target === id) return;
      body.before = target;
      body.group_id = S.items.get(target).group_id;
    } else if (grp) body.group_id = +grp.getAttribute("data-group");
    else return;
    FW.api("POST", "/api/items/" + id + "/move", body).then(function (r) { applyItems(r.items); }).catch(function (e) { FW.toast(e.message, true); });
  });
  function clearDrop() { boardEl.querySelectorAll(".drop-before, .drop-into, .drop").forEach(function (x) { x.classList.remove("drop-before", "drop-into", "drop"); }); }

  // ---- toolbar, tabs, board header -------------------------------------------------------------------------------------------- //
  toolbar.addEventListener("click", function (ev) {
    var p = ev.target.closest("[data-person]");
    if (p) {
      var u = +p.getAttribute("data-person");
      changeSettings(function (st) {
        st.filters = st.filters || {};
        var list = (st.filters.people || []).map(function (x) { return x === "me" ? S.me : x; });
        var i = list.indexOf(u);
        if (i >= 0) list.splice(i, 1); else list.push(u);
        if (list.length) st.filters.people = list; else delete st.filters.people;
      });
      return;
    }
    var b = ev.target.closest("[data-act]");
    if (!b) return;
    var a = b.getAttribute("data-act");
    if (a === "filter") filterPop(b);
    else if (a === "sort") sortPop(b);
    else if (a === "hide") hidePop(b);
    else if (a === "groupby") groupByPop(b);
    else if (a === "kanban-col") kanbanColPop(b);
    else if (a === "new-view") newView();
    else if (a === "save-view") {
      FW.api("PATCH", "/api/views/" + S.view.id, { name: S.view.name, type: S.view.type, settings: S.settings }).then(function (r) {
        S.data.views = r.views;
        S.dirty = false;
        S.view = r.views.filter(function (v) { return v.id === S.view.id; })[0];
        render();
        FW.toast("View saved.");
      }).catch(function (e) { FW.toast(e.message, true); });
    } else if (a === "rename-view") {
      var n = window.prompt("View name", S.view.name);
      if (n && n.trim()) FW.api("PATCH", "/api/views/" + S.view.id, { name: n.trim(), type: S.view.type, settings: S.view.settings }).then(function (r) {
        S.data.views = r.views; S.view = r.views.filter(function (v) { return v.id === S.view.id; })[0]; render();
      });
    } else if (a === "delete-view") {
      if (window.confirm("Delete the view “" + S.view.name + "” for everyone?")) FW.api("DELETE", "/api/views/" + S.view.id).then(function () {
        S.data.views = S.data.views.filter(function (v) { return v.id !== S.view.id; });
        selectView(null);
      });
    } else if (a === "new-item") {
      var g0 = S.groups[0];
      if (!g0) return;
      FW.api("POST", "/api/boards/" + BID + "/items", { name: "New item", group_id: g0.id, where: "top" }).then(function (r) {
        applyItems(r.items);
        var row = boardEl.querySelector('tr.row[data-item="' + r.id + '"] .name-text');
        if (row) renameItem(row, S.items.get(r.id));
      }).catch(function (e) { FW.toast(e.message, true); });
    }
  });

  tabs.addEventListener("click", function (ev) {
    var b = ev.target.closest(".view-tab");
    if (!b) return;
    if (b.getAttribute("data-act") === "new-view") { newView(); return; }
    var v = b.getAttribute("data-view");
    selectView(v ? +v : null);
  });

  var nameEl = document.getElementById("board-name");
  nameEl.addEventListener("click", function () {
    if (!S.canEdit || nameEl.isContentEditable) return;
    var old = nameEl.textContent;
    nameEl.setAttribute("contenteditable", "true");
    nameEl.focus();
    document.getSelection().selectAllChildren(nameEl);
    function done(save) {
      nameEl.removeAttribute("contenteditable");
      var t = nameEl.textContent.trim();
      if (save && t && t !== old) FW.api("PATCH", "/api/boards/" + BID, { name: t }).then(function () { document.title = t + " | ForWork"; })
        .catch(function (e) { nameEl.textContent = old; FW.toast(e.message, true); });
      else nameEl.textContent = old;
    }
    nameEl.addEventListener("keydown", function k(e) {
      if (e.key === "Enter") { e.preventDefault(); nameEl.removeEventListener("keydown", k); done(true); }
      if (e.key === "Escape") { nameEl.removeEventListener("keydown", k); done(false); }
    });
    nameEl.addEventListener("blur", function () { if (nameEl.isContentEditable) done(true); }, { once: true });
  });

  document.getElementById("fav-btn").addEventListener("click", function () {
    var b = this, on = b.getAttribute("aria-pressed") !== "true";
    FW.api("POST", "/api/boards/" + BID + "/favourite", { on: on }).then(function () { b.setAttribute("aria-pressed", on ? "true" : "false"); FW.toast(on ? "Added to favourites." : "Removed from favourites."); });
  });

  document.getElementById("board-menu").addEventListener("click", function (ev) {
    var b = ev.target.closest("[data-act]");
    if (!b) return;
    document.getElementById("board-menu").removeAttribute("open");
    var a = b.getAttribute("data-act");
    if (a === "describe") {
      dlg.innerHTML = '<form method="dialog" class="dlg-body"><h2>Board description</h2><textarea class="long" name="d">' + esc(S.data.board.description || "") +
        '</textarea><div class="dialog-actions"><button type="button" class="btn ghost" data-close>Cancel</button><button class="btn primary">Save</button></div></form>';
      dlg.showModal();
      dlg.querySelector("form").addEventListener("submit", function (e) {
        e.preventDefault();
        var d = dlg.querySelector("[name=d]").value;
        dlg.close();
        FW.api("PATCH", "/api/boards/" + BID, { description: d }).then(function () { S.data.board.description = d; document.getElementById("board-desc").textContent = d; });
      });
    } else if (a === "duplicate") {
      dlg.innerHTML = '<form method="dialog" class="dlg-body"><h2>Duplicate board</h2><label>New board name <input name="name" required value="' + esc(S.data.board.name) + ' (copy)"></label>' +
        '<label class="check"><input type="radio" name="mode" value="structure" checked> Structure only (groups, columns, views, automations)</label>' +
        '<label class="check"><input type="radio" name="mode" value="items"> Structure and items</label>' +
        '<label class="check"><input type="radio" name="mode" value="updates"> Structure, items and updates</label>' +
        '<p class="small muted">For next year\'s board, choose "Structure and items", then change the statuses and dates on the new board.</p>' +
        '<div class="dialog-actions"><button type="button" class="btn ghost" data-close>Cancel</button><button class="btn primary">Duplicate</button></div></form>';
      dlg.showModal();
      dlg.querySelector("form").addEventListener("submit", function (e) {
        e.preventDefault();
        var body = { name: dlg.querySelector("[name=name]").value, mode: dlg.querySelector("[name=mode]:checked").value };
        dlg.close();
        FW.toast("Copying the board...");
        FW.api("POST", "/api/boards/" + BID + "/duplicate", body).then(function (r) { location.href = "/boards/" + r.id; }).catch(function (e2) { FW.toast(e2.message, true); });
      });
    } else if (a === "move") {
      FW.api("GET", "/api/workspaces").then(function (r) {
        dlg.innerHTML = '<form method="dialog" class="dlg-body"><h2>Move board</h2><label>Workspace <select name="ws">' + r.workspaces.map(function (w) {
          return '<option value="' + w.id + '" ' + (w.id === S.data.board.workspace_id ? "selected" : "") + ">" + esc(w.name) + "</option>"; }).join("") +
          '</select></label><label>Folder <select name="folder"><option value="">(none)</option>' + r.folders.map(function (f) {
          return '<option value="' + f.id + '" data-ws="' + f.workspace_id + '" ' + (f.id === S.data.board.folder_id ? "selected" : "") + ">" + esc(f.name) + "</option>"; }).join("") +
          '</select></label><div class="dialog-actions"><button type="button" class="btn ghost" data-close>Cancel</button><button class="btn primary">Move</button></div></form>';
        dlg.showModal();
        var ws = dlg.querySelector("[name=ws]"), fs = dlg.querySelector("[name=folder]");
        var sync = function () { fs.querySelectorAll("option[data-ws]").forEach(function (o) { o.hidden = o.getAttribute("data-ws") !== ws.value; }); if (fs.selectedOptions[0] && fs.selectedOptions[0].hidden) fs.value = ""; };
        ws.addEventListener("change", sync);
        sync();
        dlg.querySelector("form").addEventListener("submit", function (e) {
          e.preventDefault();
          var body = { workspace_id: +ws.value, folder_id: fs.value ? +fs.value : null };
          dlg.close();
          FW.api("PATCH", "/api/boards/" + BID, body).then(function () { location.reload(); }).catch(function (e2) { FW.toast(e2.message, true); });
        });
      });
    } else if (a === "archive" || a === "restore" || a === "delete") {
      var msg = { archive: "Archive this board? It moves to Archived boards and becomes read only.", restore: "Restore this board?",
                  delete: "Delete this board? An admin can restore it from the bin." }[a];
      if (!window.confirm(msg)) return;
      FW.api("POST", "/api/boards/" + BID + "/state", { state: { archive: "archived", restore: "active", delete: "deleted" }[a] }).then(function () {
        location.href = a === "delete" ? "/" : "/boards/" + BID;
      }).catch(function (e) { FW.toast(e.message, true); });
    }
  });

  panel.addEventListener("click", function (ev) { if (ev.target.closest("[data-act=close-panel]")) closePanel(); });
  dlg.addEventListener("click", function (ev) { if (ev.target.closest("[data-close]")) dlg.close(); });

  // go
  load(false).then(function () {
    var want = +app.getAttribute("data-item");
    if (want) {
      openPanel(want);
      setTimeout(function () {
        var row = boardEl.querySelector('tr.row[data-item="' + want + '"]');
        if (row) row.scrollIntoView({ block: "center" });
      }, 50);
    }
  });
})();
