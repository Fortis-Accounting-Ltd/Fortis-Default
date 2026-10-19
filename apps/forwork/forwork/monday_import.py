"""
Bringing the monday.com account into ForWork - from an export bundle (monday_bundle.py), made either by
monday_pull.py with an API token or with Claude's monday.com connector.

Everything that makes the boards work comes across: workspaces and folders, boards (archived ones stay
archived), groups with their colours, columns with their labels, colours and order (hidden "deactivated"
labels too), items and subitems with every value, connected items and mirrors, updates with their replies
and @mentions, saved table views (column order, hidden columns, group by, filters, sort), and the status
automations.  People are matched by email; people who have left come across as inactive users so their
names stay on the work they did.

Files uploaded to monday.com are listed on the items and updates they belong to (name and size) but stay
in monday.com: monday only lets a signed-in user download them.  Links to files elsewhere (OneDrive,
SharePoint, Google Drive) come across as links.

Each board is loaded in one transaction, so a board is either all there or not at all.  Boards already
imported are skipped unless replace=True, which loads them again from the bundle.
"""
from __future__ import annotations

import html
import re
from collections import defaultdict
from pathlib import Path

from .db import dumps, now_iso
from .model import MONDAY_TYPES, Ctx, ValueError_, check_color, column_type, parse_number
from .monday_bundle import Bundle
from .sanitize import clean_html, html_to_text
from .service import USER_COLORS, make_key

# monday's colours for label indexes when a board's settings do not list them
LABEL_COLORS = ["#fdab3d", "#00c875", "#df2f4a", "#0086c0", "#a25ddc", "#c4c4c4", "#037f4c", "#579bfc", "#cab641",
                "#ffcb00", "#bb3354", "#ff158a", "#ff5ac4", "#784bd1", "#9cd326", "#66ccff", "#7f5347", "#ff642e",
                "#e2445c", "#333333"]
UNIT_SYMBOLS = {"dollar": "$", "usd": "$", "aud": "$", "euro": "€", "pound": "£", "yen": "¥",
                "percent": "%", "none": ""}
KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
MENTION_A = re.compile(r"<a\b([^>]*)>(.*?)</a>", re.S | re.I)
ATTR = re.compile(r'([\w-]+)\s*=\s*"([^"]*)"')


def _iso(t: str | None) -> str:
    """monday's '2026-10-18T22:18:39.000Z' / '2026-10-18T22:18:39Z' -> '2026-10-18T22:18:39Z'."""
    if not t:
        return now_iso()
    t = str(t).replace(" UTC", "Z").replace(" ", "T")
    m = re.match(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})", t)
    return m.group(1) + "Z" if m else now_iso()


class Importer:
    def __init__(self, svc, actor=None, progress=None):
        self.svc = svc
        self.db = svc.db
        self.actor = actor
        self.progress = progress or (lambda msg, done, total: None)
        self.report = {"boards": 0, "items": 0, "subitems": 0, "values": 0, "updates": 0, "files": 0, "views": 0,
                       "automations": 0, "users": 0, "skipped_boards": [], "warnings": []}
        self.users: dict[str, int] = {}          # monday user id -> ForWork user id
        self.user_names: dict[str, str] = {}
        self.teams: dict[str, int] = {}
        self.boards: dict[str, int] = {}         # monday board id -> ForWork board id
        self.items: dict[str, int] = {}          # monday item id (items and subitems) -> ForWork item id
        self.cols: dict[str, dict] = {}          # monday board id -> {(scope, monday col id): (ForWork col id, type)}
        self.deferred_rel_values = []            # (ForWork item, ForWork column, [monday item ids])
        self.deferred_rel_cols = []              # (ForWork column, [monday board ids])
        self.deferred_mirrors = []               # (ForWork column, monday board id, scope, settings)

    def warn(self, msg: str) -> None:
        if len(self.report["warnings"]) < 500:
            self.report["warnings"].append(msg)

    # ---- people -------------------------------------------------------------------------------- #
    def load_users(self, data: dict) -> None:
        with self.db.write() as c:
            n = c.execute("SELECT COUNT(*) FROM users").fetchone()[0]
            for u in data.get("users") or []:
                mid = str(u["id"])
                email = (u.get("email") or "").strip().lower()
                name = (u.get("name") or "").strip() or email
                self.user_names[mid] = name
                row = c.execute("SELECT id FROM users WHERE monday_id = ? OR email = ? COLLATE NOCASE",
                                (mid, email)).fetchone() if email else None
                if row:
                    self.users[mid] = row[0]
                    c.execute("UPDATE users SET monday_id = COALESCE(monday_id, ?) WHERE id = ?", (mid, row[0]))
                    continue
                gone = (not u.get("active", True) or email.endswith("@deleted.user")
                        or email.endswith("@agent.monday.com") or not email)
                if name.lower().startswith("deleted member") or name.lower().startswith("deleted on"):
                    name = "Former member"
                if not email:
                    email = f"monday-{mid}@former.invalid"
                uid = c.execute("INSERT INTO users (email, name, role, active, color, monday_id, created_at) "
                                "VALUES (?, ?, 'member', ?, ?, ?, ?)",
                                (email, name, 0 if gone else 1, USER_COLORS[n % len(USER_COLORS)], mid, now_iso())).lastrowid
                n += 1
                self.users[mid] = uid
                self.report["users"] += 1
            for t in data.get("teams") or []:
                mid = str(t["id"])
                row = c.execute("SELECT id FROM teams WHERE monday_id = ?", (mid,)).fetchone()
                tid = row[0] if row else c.execute("INSERT INTO teams (name, monday_id) VALUES (?, ?)",
                                                   (t["name"], mid)).lastrowid
                self.teams[mid] = tid
                for m in t.get("user_ids") or []:
                    if str(m) in self.users:
                        c.execute("INSERT OR IGNORE INTO team_members (team_id, user_id) VALUES (?, ?)",
                                  (tid, self.users[str(m)]))

    def user(self, c, mid) -> int | None:
        """A ForWork user for a monday user id - a placeholder 'Former member' if monday no longer lists them."""
        if mid in (None, "", 0, "0"):
            return None
        mid = str(mid)
        if mid not in self.users:
            row = c.execute("SELECT id FROM users WHERE monday_id = ?", (mid,)).fetchone()
            if row:
                self.users[mid] = row[0]
            else:
                self.users[mid] = c.execute(
                    "INSERT INTO users (email, name, role, active, color, monday_id, created_at) "
                    "VALUES (?, 'Former member', 'member', 0, '#c4c4c4', ?, ?)",
                    (f"monday-{mid}@former.invalid", mid, now_iso())).lastrowid
                self.report["users"] += 1
        return self.users[mid]

    # ---- workspaces ------------------------------------------------------------------------------ #
    def load_workspaces(self, data: dict) -> None:
        self.ws, self.folders = {}, {}
        for w in data.get("workspaces") or []:
            row = self.db.one("SELECT id FROM workspaces WHERE monday_id = ?", (str(w["id"]),))
            self.ws[str(w["id"])] = row[0] if row else self.svc.create_workspace(None, w["name"], monday_id=str(w["id"]))
        for f in data.get("folders") or []:
            wid = self.ws.get(str(f.get("workspace_id")))
            if not wid:
                continue
            row = self.db.one("SELECT id FROM folders WHERE monday_id = ?", (str(f["id"]),))
            self.folders[str(f["id"])] = row[0] if row else self.svc.create_folder(None, wid, f["name"], monday_id=str(f["id"]))

    def default_workspace(self) -> int:
        row = self.db.one("SELECT id FROM workspaces WHERE name = 'Main workspace' ORDER BY id LIMIT 1") or \
            self.db.one("SELECT id FROM workspaces ORDER BY position, id LIMIT 1")
        return row[0] if row else self.svc.create_workspace(None, "Main workspace")

    # ---- boards ----------------------------------------------------------------------------------- #
    def run(self, bundle: Bundle, board_ids: list[str] | None = None, replace: bool = False) -> dict:
        self.load_users(bundle.users())
        self.load_workspaces(bundle.workspaces())
        ids = [b for b in bundle.board_ids() if not board_ids or b in board_ids]
        for b in self.db.q("SELECT id, monday_id FROM boards WHERE monday_id IS NOT NULL"):
            self.boards[b["monday_id"]] = b["id"]
        for b in self.db.q("SELECT id, monday_id FROM items WHERE monday_id IS NOT NULL"):
            self.items[b["monday_id"]] = b["id"]
        for n, bid in enumerate(ids, start=1):
            data = bundle.board(bid)
            self.progress(f"Importing {data['board']['name']}", n - 1, len(ids))
            if bid in self.boards:
                if not replace:
                    self.report["skipped_boards"].append(f"{data['board']['name']} (already imported)")
                    continue
                self.remove_board(self.boards.pop(bid))
            self.load_board(data)
        self.resolve()
        self.progress("Finished", len(ids), len(ids))
        with self.db.write() as c:
            self.svc._log(c, None, None, self.actor, "import.monday",
                          detail={k: v for k, v in self.report.items() if not isinstance(v, list)})
        return self.report

    def remove_board(self, fw_bid: int) -> None:
        with self.db.write() as c:
            items = "SELECT id FROM items WHERE board_id = ?"
            c.execute(f"DELETE FROM notifications WHERE item_id IN ({items})", (fw_bid,))
            c.execute(f"DELETE FROM files WHERE item_id IN ({items})", (fw_bid,))
            c.execute(f"UPDATE updates SET parent_id = NULL WHERE item_id IN ({items})", (fw_bid,))
            c.execute(f"DELETE FROM updates WHERE item_id IN ({items})", (fw_bid,))
            c.execute(f"DELETE FROM item_values WHERE item_id IN ({items})", (fw_bid,))
            c.execute("UPDATE items SET parent_id = NULL WHERE board_id = ?", (fw_bid,))
            c.execute("DELETE FROM items WHERE board_id = ?", (fw_bid,))
            for t in ("views", "automations", "favourites", "activity"):
                c.execute(f"DELETE FROM {t} WHERE board_id = ?", (fw_bid,))
            c.execute("DELETE FROM item_values WHERE column_id IN (SELECT id FROM columns WHERE board_id = ?)", (fw_bid,))
            c.execute("DELETE FROM columns WHERE board_id = ?", (fw_bid,))
            c.execute("DELETE FROM groups WHERE board_id = ?", (fw_bid,))
            c.execute("DELETE FROM boards WHERE id = ?", (fw_bid,))
        self.items = {k: v for k, v in self.items.items()
                      if self.db.one("SELECT 1 FROM items WHERE id = ?", (v,)) is not None}

    def load_board(self, data: dict) -> None:
        b = data["board"]
        mbid = str(b["id"])
        now = now_iso()
        ctx = Ctx({r["id"]: r["name"] for r in self.db.q("SELECT id, name FROM users")},
                  {r["id"]: r["name"] for r in self.db.q("SELECT id, name FROM teams")}, {})
        with self.db.write() as c:
            ws = self.ws.get(str(b.get("workspace_id"))) or self.default_workspace()
            folder = self.folders.get(str(b.get("folder_id"))) if b.get("folder_id") else None
            pos = (c.execute("SELECT MAX(position) FROM boards").fetchone()[0] or 0) + 1
            fw_bid = c.execute(
                "INSERT INTO boards (workspace_id, folder_id, name, description, state, position, created_by, created_at, "
                "updated_at, seq, struct_seq, monday_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 1, ?)",
                (ws, folder, b["name"], b.get("description") or "", "archived" if b.get("state") == "archived" else "active",
                 pos, self.user(c, (b.get("owner_ids") or [None])[0]), now, _iso(b.get("updated_at")), mbid)).lastrowid
            self.boards[mbid] = fw_bid
            # groups, in monday's order
            gmap = {}
            groups = [g for g in data.get("groups") or [] if not g.get("deleted")]
            groups.sort(key=lambda g: float(g.get("position") or 0))
            for n, g in enumerate(groups, start=1):
                gmap[g["id"]] = c.execute(
                    "INSERT INTO groups (board_id, title, color, position, state, monday_id) VALUES (?, ?, ?, ?, ?, ?)",
                    (fw_bid, g["title"] or "Group", check_color(g.get("color"), "#579bfc"), float(n),
                     "archived" if g.get("archived") else "active", g["id"])).lastrowid
            if not gmap:
                gmap[None] = c.execute("INSERT INTO groups (board_id, title, color, position) VALUES (?, 'Group 1', "
                                       "'#579bfc', 1)", (fw_bid,)).lastrowid
            first_group = next(iter(gmap.values()))
            # columns (item and subitem)
            used = self._used_labels(data)
            cmap = {}
            for scope, cols in (("item", data.get("columns") or []), ("subitem", data.get("subitem_columns") or [])):
                taken = set()
                if scope == "subitem" and not cols:
                    cols = [{"id": "name", "title": "Subitem", "type": "name", "settings": {}}]
                for n, col in enumerate(c for c in cols if not c.get("archived")):
                    ftype, settings = self.column_settings(col, used.get((scope, col["id"]), {}))
                    key = col["id"] if KEY_RE.match(col["id"]) and col["id"] not in taken else make_key(col["title"], taken)
                    taken.add(key)
                    cid = c.execute(
                        "INSERT INTO columns (board_id, scope, title, type, key, settings, description, position, width, monday_id) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (fw_bid, scope, col["title"] or col["id"], ftype, key, dumps(settings), col.get("description") or "",
                         float(n), col.get("width") or None, col["id"])).lastrowid
                    cmap[(scope, col["id"])] = (cid, ftype, settings)
                    if ftype == "board_relation":
                        self.deferred_rel_cols.append((cid, [str(x) for x in (col.get("settings") or {}).get("boardIds") or []]))
                    elif ftype == "mirror":
                        self.deferred_mirrors.append((cid, mbid, scope, col.get("settings") or {}))
            self.cols[mbid] = {k: v[0] for k, v in cmap.items()}
            # items and subitems
            vals, n_items, n_subs = [], 0, 0
            archived_groups = {gmap[g["id"]] for g in groups if g.get("archived")}
            for n, it in enumerate(data.get("items") or [], start=1):
                state = it.get("state") if it.get("state") in ("archived", "deleted") else "active"
                gid = gmap.get(it.get("group_id"))
                if gid is None and state != "active":
                    # an archived item whose group was deleted in monday.com: keep it in an archived group
                    if "_older" not in gmap:
                        gmap["_older"] = c.execute(
                            "INSERT INTO groups (board_id, title, color, position, state) VALUES (?, ?, '#c4c4c4', ?, "
                            "'archived')", (fw_bid, "Older items (from monday.com)", float(len(gmap) + 1))).lastrowid
                        archived_groups.add(gmap["_older"])
                    gid = gmap["_older"]
                elif gid is None:
                    self.warn(f"{b['name']}: {it['name']} was in a group monday no longer has - put in the first group")
                    gid = first_group
                if state == "active" and gid in archived_groups:
                    state = "archived"                     # items in an archived group are archived with it
                iid = c.execute(
                    "INSERT INTO items (board_id, group_id, name, position, state, created_by, created_at, updated_at, seq, monday_id) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?)",
                    (fw_bid, gid, it["name"] or "(no name)", float(n), state, self.user(c, it.get("creator_id")),
                     _iso(it.get("created_at")), _iso(it.get("updated_at")), str(it["id"]))).lastrowid
                self.items[str(it["id"])] = iid
                n_items += 1
                vals += self.values(c, it, iid, "item", cmap, ctx)
                for m, s in enumerate(it.get("subitems") or [], start=1):
                    sid = c.execute(
                        "INSERT INTO items (board_id, parent_id, name, position, state, created_by, created_at, updated_at, seq, monday_id) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?)",
                        (fw_bid, iid, s["name"] or "(no name)", float(m), state, self.user(c, s.get("creator_id")),
                         _iso(s.get("created_at")), _iso(s.get("updated_at")), str(s["id"]))).lastrowid
                    self.items[str(s["id"])] = sid
                    n_subs += 1
                    vals += self.values(c, s, sid, "subitem", cmap, ctx)
            c.executemany("INSERT OR REPLACE INTO item_values (item_id, column_id, value, text, updated_at) VALUES (?, ?, ?, ?, ?)",
                          vals)
            n_updates = self.updates(c, data, b["name"])
            n_views = self.views(c, data, fw_bid, cmap, b["name"])
            n_rules = self.automations(c, data, fw_bid, cmap, gmap, b["name"])
            self.svc._log(c, fw_bid, None, self.actor, "board.import",
                          detail={"from": "monday.com", "monday_id": mbid, "items": n_items, "subitems": n_subs,
                                  "updates": n_updates})
        r = self.report
        r["boards"] += 1
        r["items"] += n_items
        r["subitems"] += n_subs
        r["values"] += len(vals)
        r["updates"] += n_updates
        r["views"] += n_views
        r["automations"] += n_rules

    @staticmethod
    def _used_labels(data: dict) -> dict:
        """Label ids used by items, per (scope, column), with the label's text (for labels monday no longer lists)."""
        used = defaultdict(dict)
        for it in data.get("items") or []:
            for scope, row in [("item", it)] + [("subitem", s) for s in it.get("subitems") or []]:
                for cid, v in (row.get("values") or {}).items():
                    val = v.get("value")
                    if v.get("type") in ("status", "color") and isinstance(val, dict) and val.get("index") is not None:
                        used[(scope, cid)][int(val["index"])] = v.get("text") or ""
                    elif v.get("type") == "dropdown" and isinstance(val, dict):
                        for i in val.get("ids") or []:
                            used[(scope, cid)][int(i)] = ""
        return used

    def column_settings(self, col: dict, used: dict) -> tuple[str, dict]:
        mtype = col.get("type")
        ftype = MONDAY_TYPES.get(mtype, "readonly")
        s = col.get("settings") or {}
        if ftype == "status":
            labels = s.get("labels") or {}
            if isinstance(labels, list):                # newer API shape: [{"id", "name", "color"}]
                labels = {str(lab.get("id")): lab.get("name") for lab in labels}
            positions = s.get("labels_positions_v2") or {}
            colors = s.get("labels_colors") or {}
            hidden = {int(x) for x in s.get("deactivated_labels") or []}
            done = {int(x) for x in s.get("done_colors") or [1]}
            out = []
            for k, name in labels.items():
                i = int(k)
                if not (name or "").strip() and i not in used:
                    continue
                lab = {"id": i, "name": (name or "").strip(),
                       "color": check_color((colors.get(k) or {}).get("color"), LABEL_COLORS[i % len(LABEL_COLORS)])}
                if i in done:
                    lab["done"] = True
                if i in hidden:
                    lab["hidden"] = True
                out.append(lab)
            have = {lab["id"] for lab in out}
            for i, text in used.items():
                if i not in have:
                    out.append({"id": i, "name": text, "color": LABEL_COLORS[i % len(LABEL_COLORS)], "hidden": True})
            out.sort(key=lambda lab: (positions.get(str(lab["id"]), 1000 + lab["id"]), lab["id"]))
            return ftype, column_type("status").clean_settings({"labels": out})
        if ftype == "dropdown":
            out = []
            hidden = {int(x) for x in s.get("deactivated_labels") or []}
            for lab in s.get("labels") or []:
                entry = {"id": int(lab["id"]), "name": lab.get("name") or ""}
                if int(lab["id"]) in hidden or lab.get("is_deactivated"):
                    entry["hidden"] = True
                out.append(entry)
            have = {lab["id"] for lab in out}
            out += [{"id": i, "name": f"Option {i}", "hidden": True} for i in used if i not in have]
            return ftype, column_type("dropdown").clean_settings({"labels": out})
        if ftype == "numbers":
            unit = s.get("unit") or {}
            sym = ""
            if isinstance(unit, dict):
                sym = unit.get("custom_unit") or UNIT_SYMBOLS.get(str(unit.get("symbol") or "").lower(), unit.get("symbol") or "")
                direction = unit.get("direction") or "left"
            else:
                direction = "left"
            return ftype, column_type("numbers").clean_settings({"unit": sym, "unit_position": direction})
        if ftype == "people":
            limit = int(s.get("max_people_allowed") or 0) if str(s.get("max_people_allowed") or "0").isdigit() else 0
            if mtype == "person":
                limit = 1
            return ftype, ({"max_people": limit} if limit else {})
        if ftype == "formula":
            return ftype, {"formula": str(s.get("formula") or "")}
        if ftype == "mirror":
            return ftype, {"relation_column": None, "targets": {}}      # filled in once every board is in
        if ftype == "board_relation":
            return ftype, {"board_ids": []}                              # filled in once every board is in
        if ftype == "readonly":
            out = {"monday_type": mtype}
            if mtype == "button" and s.get("buttonText"):
                out["button_text"] = s["buttonText"]
            return ftype, out
        return ftype, {}

    def values(self, c, row: dict, iid: int, scope: str, cmap: dict, ctx: Ctx) -> list[tuple]:
        out = []
        for mcid, v in (row.get("values") or {}).items():
            hit = cmap.get((scope, mcid))
            if not hit:
                continue
            cid, ftype, settings = hit
            raw, text = v.get("value"), v.get("text")
            try:
                val = self.value(c, ftype, v.get("type"), raw, text, settings, iid, cid)
            except (ValueError_, ValueError, TypeError, KeyError) as e:
                self.warn(f"a {v.get('type')} value on item {row.get('name')!r} could not be read ({e}) - left empty")
                continue
            if ftype == "board_relation":
                ids = [str(x) for x in v.get("linked_item_ids") or []]
                if ids:
                    self.deferred_rel_values.append((iid, cid, ids))
                continue
            if val is None:
                continue
            out.append((iid, cid, dumps(val), column_type(ftype).text(val, settings, ctx), _iso(row.get("updated_at"))))
        return out

    def value(self, c, ftype, mtype, raw, text, settings, iid, cid):
        t = column_type(ftype)
        if t.computed or ftype == "board_relation":
            return None
        if ftype == "readonly":
            if mtype == "button":
                return None
            if mtype == "doc" and isinstance(raw, dict):
                names = [f.get("name") or "" for f in raw.get("files") or []]
                return t.normalize(", ".join(n for n in names if n) or text, settings)
            return t.normalize(text, settings)
        if raw is None and ftype not in ("text",):
            return None
        if ftype == "text":
            return t.normalize(raw if isinstance(raw, str) else text, settings)
        if ftype == "long_text":
            return t.normalize(raw.get("text") if isinstance(raw, dict) else raw, settings)
        if ftype == "numbers":
            return parse_number(raw)
        if ftype == "status":
            return int(raw["index"]) if isinstance(raw, dict) and raw.get("index") is not None else None
        if ftype == "dropdown":
            known = {lab["id"] for lab in settings.get("labels") or []}
            ids = [int(i) for i in raw.get("ids") or [] if int(i) in known]
            return ids or None
        if ftype == "people":
            users, teams = [], []
            entries = raw.get("personsAndTeams") or ([{"id": raw["id"], "kind": "person"}] if raw.get("id") else [])
            for p in entries:
                if p.get("kind") == "team":
                    if str(p["id"]) in self.teams:
                        teams.append(self.teams[str(p["id"])])
                else:
                    u = self.user(c, p.get("id"))
                    if u and u not in users:
                        users.append(u)
            out = {}
            if users:
                out["users"] = users
            if teams:
                out["teams"] = teams
            return out or None
        if ftype == "date":
            d = raw.get("date")
            if not d:
                return None
            if raw.get("time"):
                return t.normalize(f"{d}T{raw['time'][:8]}Z", settings)
            return t.normalize(d, settings)
        if ftype == "timeline":
            return t.normalize({"from": raw.get("from"), "to": raw.get("to")}, settings)
        if ftype == "checkbox":
            return t.normalize(raw.get("checked"), settings)
        if ftype == "link":
            try:
                return t.normalize({"url": raw.get("url"), "text": raw.get("text")}, settings)
            except ValueError_:
                self.warn(f"a link that is not a web address was left out: {str(raw.get('url'))[:80]}")
                return None
        if ftype == "email":
            try:
                return t.normalize({"email": raw.get("email"), "text": raw.get("text")}, settings)
            except ValueError_:
                return None
        if ftype == "phone":
            return t.normalize({"phone": raw.get("phone"), "country": raw.get("countryShortName")}, settings)
        if ftype == "location":
            return t.normalize(raw, settings)
        if ftype == "file":
            ids = []
            for f in raw.get("files") or []:
                link = None
                if f.get("linkToFile"):
                    try:
                        from .model import safe_url
                        link = safe_url(f["linkToFile"])
                    except ValueError_:
                        link = None
                fid = c.execute("INSERT INTO files (item_id, column_id, name, link, created_at, monday_asset_id) "
                                "VALUES (?, ?, ?, ?, ?, ?)",
                                (iid, cid, f.get("name") or "file", link, now_iso(),
                                 str(f.get("assetId") or "") or None)).lastrowid
                ids.append(fid)
                self.report["files"] += 1
            return ids or None
        return t.normalize(raw, settings)

    # ---- updates ------------------------------------------------------------------------------------ #
    def convert_body(self, body: str) -> str:
        def mention(m):
            attrs = dict(ATTR.findall(m.group(1)))
            inner = m.group(2)
            if attrs.get("data-mention-type") == "User" and attrs.get("data-mention-id", "").isdigit():
                uid = self.users.get(attrs["data-mention-id"])
                if uid:
                    return f'<span class="mention" data-user="{uid}">{inner}</span>'
                return f"<strong>{inner}</strong>"
            if attrs.get("data-mention-type"):
                return f"<strong>{inner}</strong>"
            return m.group(0)
        body = MENTION_A.sub(mention, body or "")
        body = re.sub(r"<img\b[^>]*>", "<em>[image - see the update's files]</em>", body, flags=re.I)
        return clean_html(body)

    def updates(self, c, data: dict, board_name: str) -> int:
        n = 0
        missing = 0
        for u in data.get("updates") or []:
            iid = self.items.get(str(u.get("item_id")))
            if not iid:
                missing += 1
                continue
            if c.execute("SELECT 1 FROM updates WHERE monday_id = ?", (str(u["id"]),)).fetchone():
                continue
            body = self.convert_body(u.get("body") or "")
            text = html_to_text(body) or (u.get("text_body") or "")
            if not body:
                body = f"<p>{html.escape(text)}</p>" if text else "<p></p>"
            uid = c.execute(
                "INSERT INTO updates (item_id, body, text, created_by, author_name, created_at, monday_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (iid, body, text, self.user(c, u.get("creator_id")), self.user_names.get(str(u.get("creator_id")), ""),
                 _iso(u.get("created_at")), str(u["id"]))).lastrowid
            n += 1
            self._update_files(c, u, iid, uid)
            for r in u.get("replies") or []:
                if c.execute("SELECT 1 FROM updates WHERE monday_id = ?", (str(r["id"]),)).fetchone():
                    continue
                rb = self.convert_body(r.get("body") or "")
                rt = html_to_text(rb) or (r.get("text_body") or "")
                rid = c.execute(
                    "INSERT INTO updates (item_id, parent_id, body, text, created_by, author_name, created_at, monday_id) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (iid, uid, rb or f"<p>{html.escape(rt)}</p>", rt, self.user(c, r.get("creator_id")),
                     self.user_names.get(str(r.get("creator_id")), ""), _iso(r.get("created_at")), str(r["id"]))).lastrowid
                self._update_files(c, r, iid, rid)
                n += 1
        if missing:
            self.warn(f"{board_name}: {missing} updates belong to items that are archived or deleted in monday.com "
                      f"and were not brought across")
        return n

    def _update_files(self, c, u: dict, iid: int, uid: int) -> None:
        for a in u.get("assets") or []:
            name = a.get("name") or "file"
            if a.get("file_extension") and not name.lower().endswith(str(a["file_extension"]).lower()):
                name += a["file_extension"] if str(a["file_extension"]).startswith(".") else "." + a["file_extension"]
            c.execute("INSERT INTO files (item_id, update_id, name, size, created_at, monday_asset_id) VALUES (?, ?, ?, ?, ?, ?)",
                      (iid, uid, name, parse_number(a.get("file_size")) if a.get("file_size") else None, now_iso(),
                       str(a.get("id") or "") or None))
            self.report["files"] += 1

    # ---- views and automations ------------------------------------------------------------------------ #
    def views(self, c, data: dict, fw_bid: int, cmap: dict, board_name: str) -> int:
        n = 0
        for v in data.get("views") or []:
            if v.get("type") != "TableBoardView":
                if v.get("name"):
                    self.warn(f"{board_name}: the view {v['name']!r} ({v.get('type') or 'dashboard/chart'}) is a monday.com "
                              f"app or chart view and was not brought across")
                continue
            s = v.get("settings") or {}
            cols = s.get("columns") or {}
            out = {}

            def fw(mcid):
                hit = cmap.get(("item", mcid))
                return hit[0] if hit else None

            hidden = [fw(p["column_id"]) for p in cols.get("column_properties") or [] if p.get("visible") is False]
            if any(hidden):
                out["hidden"] = [h for h in hidden if h]
            order = [fw(x) for x in cols.get("column_order") or []]
            if any(order):
                out["order"] = [o for o in order if o]
            gb = ((s.get("group_by") or {}).get("conditions") or [{}])[0].get("columnId")
            if gb and fw(gb):
                out["group_by"] = fw(gb)
                if (s.get("group_by") or {}).get("hideEmptyGroups"):
                    out["hide_empty_groups"] = True
            rules = []
            for grp in (v.get("filter") or {}).get("groups") or []:
                for r in grp.get("rules") or []:
                    hit = cmap.get(("item", r.get("column_id")))
                    if not hit:
                        continue
                    cid, ftype, settings = hit
                    vals = r.get("compare_value") or []
                    op = (r.get("operator") or "").upper()
                    if ftype == "people" and op == "ANY_OF":
                        people = []
                        for x in vals:
                            if x == "assigned_to_me":
                                people.append("me")
                            elif str(x).startswith("person-") and self.users.get(str(x)[7:]):
                                people.append(self.users[str(x)[7:]])
                        if people:
                            rules.append({"column": cid, "op": "any_of", "values": people})
                    elif ftype == "status" and op == "ANY_OF":
                        rules.append({"column": cid, "op": "any_of", "values": [int(x) for x in vals if str(x).isdigit()]})
                    elif ftype == "status" and op == "CONTAINS_TEXT":
                        ids = [lab["id"] for lab in settings.get("labels") or []
                               if any(str(x).lower() in lab["name"].lower() for x in vals)]
                        if ids:
                            rules.append({"column": cid, "op": "any_of", "values": ids})
                    else:
                        self.warn(f"{board_name}: a filter in the view {v['name']!r} could not be brought across")
            if rules:
                out["filters"] = {"rules": rules}
            sort = []
            for srt in v.get("sort") or []:
                if srt.get("column_id") == "name":
                    sort.append({"column": "name", "dir": "desc" if str(srt.get("direction")).lower() == "desc" else "asc"})
                elif fw(srt.get("column_id")):
                    sort.append({"column": fw(srt["column_id"]),
                                 "dir": "desc" if str(srt.get("direction")).lower() == "desc" else "asc"})
            if sort:
                out["sort"] = sort
            from .service import _clean_view_settings
            clean = _clean_view_settings(out)
            if out.get("filters", {}).get("rules"):              # keep "me" in people filters
                clean.setdefault("filters", {})["rules"] = out["filters"]["rules"]
            n += 1
            c.execute("INSERT INTO views (board_id, name, type, settings, position, monday_id) VALUES (?, ?, 'table', ?, ?, ?)",
                      (fw_bid, v.get("name") or "View", dumps(clean), float(n), str(v.get("id") or "")))
        return n

    def automations(self, c, data: dict, fw_bid: int, cmap: dict, gmap: dict, board_name: str) -> int:
        n = 0
        for rule in data.get("automations") or []:
            w, t = rule.get("when") or {}, rule.get("then") or {}
            col = cmap.get(("item", w.get("column")))
            if not col or col[1] != "status":
                self.warn(f"{board_name}: an automation on a column ForWork does not have was left out")
                continue
            trigger = {"type": "status_change", "column": col[0], "to": w.get("status_index")}
            if "move_to_group" in t and gmap.get(t["move_to_group"]):
                action = {"type": "move_to_group", "group": gmap[t["move_to_group"]]}
            elif "set_date_today" in t and cmap.get(("item", t["set_date_today"])):
                action = {"type": "set_date_today", "column": cmap[("item", t["set_date_today"])][0]}
            else:
                self.warn(f"{board_name}: an automation ForWork cannot do yet was left out ({t})")
                continue
            c.execute("INSERT INTO automations (board_id, trigger, action, created_at, imported_from) VALUES (?, ?, ?, ?, ?)",
                      (fw_bid, dumps(trigger), dumps(action), now_iso(), "monday.com"))
            n += 1
        return n

    # ---- links between boards, once every board is in ----------------------------------------------- #
    def resolve(self) -> None:
        with self.db.write() as c:
            for cid, mboards in self.deferred_rel_cols:
                ids = sorted({self.boards[b] for b in mboards if b in self.boards})
                c.execute("UPDATE columns SET settings = ? WHERE id = ?", (dumps({"board_ids": ids}), cid))
            names = {}
            missing = 0
            rows = []
            for iid, cid, mids in self.deferred_rel_values:
                ids = [self.items[m] for m in mids if m in self.items]
                missing += len(mids) - len(ids)
                if not ids:
                    continue
                for i in ids:
                    if i not in names:
                        r = c.execute("SELECT name FROM items WHERE id = ?", (i,)).fetchone()
                        names[i] = r[0] if r else ""
                rows.append((iid, cid, dumps(ids), ", ".join(names[i] for i in ids if names[i]), now_iso()))
            c.executemany("INSERT OR REPLACE INTO item_values (item_id, column_id, value, text, updated_at) VALUES (?, ?, ?, ?, ?)",
                          rows)
            self.report["values"] += len(rows)
            if missing:
                self.warn(f"{missing} links pointed to items that are not in monday.com any more (deleted or on deleted boards)")
            for cid, mbid, scope, s in self.deferred_mirrors:
                rel = next(iter((s.get("relation_column") or {}).keys()), None)
                rel_id = self.cols.get(mbid, {}).get((scope, rel))
                targets = {}
                for tb, tcols in (s.get("displayed_linked_columns") or {}).items():
                    tb = str(tb)
                    if tb in self.boards and tcols:
                        tcid = self.cols.get(tb, {}).get(("item", tcols[0]))
                        if tcid:
                            targets[str(self.boards[tb])] = tcid
                c.execute("UPDATE columns SET settings = ? WHERE id = ?",
                          (dumps({"relation_column": rel_id, "targets": targets}), cid))
                if not rel_id or not targets:
                    self.warn("a mirror column's connected board was not brought across, so it shows nothing")


def import_bundle(svc, path: str | Path, actor=None, board_ids: list[str] | None = None, replace: bool = False,
                  progress=None) -> dict:
    b = Bundle(path)
    try:
        return Importer(svc, actor, progress).run(b, board_ids=board_ids, replace=replace)
    finally:
        b.close()
