"""
The work itself - boards, groups, columns, items, values, updates and views - and the rules that keep
them consistent.  The web screens and the importers change data only through this module, so every
change is checked the same way, written in one transaction, recorded in the activity log (who, when,
what it was before) and bumps the board's change counter so open pages refresh.

`user` is the signed-in person (a users row as a dict) or None for the system (imports, automations).
"""
from __future__ import annotations

import re
import secrets
from collections import defaultdict

from . import sanitize
from .db import Database, dumps, loads, now_iso
from .model import PALETTE, Ctx, FormulaError, Status, ValueError_, column_type, eval_formula

GROUP_COLORS = ["#579bfc", "#00c875", "#fdab3d", "#a25ddc", "#df2f4a", "#66ccff", "#9cd326", "#ff5ac4", "#037f4c",
                "#784bd1", "#ffcb00", "#7f5347"]
USER_COLORS = ["#579bfc", "#00c875", "#fdab3d", "#a25ddc", "#e2445c", "#0086c0", "#9cd326", "#ff5ac4", "#037f4c",
               "#784bd1", "#ff642e", "#175a63", "#bb3354", "#cab641"]
ROLES = ("admin", "member", "viewer")
MAX_NAME = 500


class WorkError(Exception):
    status = 400


class Invalid(WorkError):
    status = 400


class NotFound(WorkError):
    status = 404


class Forbidden(WorkError):
    status = 403


def initials(name: str) -> str:
    parts = [p for p in re.split(r"[\s.@_-]+", name or "") if p]
    if not parts:
        return "?"
    return (parts[0][0] + (parts[-1][0] if len(parts) > 1 else "")).upper()


def make_key(title: str, taken: set[str]) -> str:
    base = re.sub(r"[^a-z0-9]+", "_", (title or "").lower()).strip("_")[:40] or "column"
    if not base[0].isalpha():
        base = "c_" + base
    key, n = base, 2
    while key in taken:
        key, n = f"{base}_{n}", n + 1
    return key


def _uid(user) -> int | None:
    return user["id"] if user else None


def _clean_name(name: str, what: str = "name") -> str:
    n = re.sub(r"\s+", " ", str(name or "")).strip()
    if not n:
        raise Invalid(f"the {what} cannot be empty")
    if len(n) > MAX_NAME:
        raise Invalid(f"the {what} is too long (500 characters at most)")
    return n


class Service:
    def __init__(self, db: Database):
        self.db = db

    # ==== permissions ======================================================================== #
    @staticmethod
    def can_edit(user) -> bool:
        return user is None or (user["active"] and user["role"] in ("admin", "member"))

    def _need_edit(self, user) -> None:
        if not self.can_edit(user):
            raise Forbidden("you can look at ForWork but not change it - ask an admin for member access")

    def _need_admin(self, user) -> None:
        if user is not None and user["role"] != "admin":
            raise Forbidden("only an admin can do this")

    # ==== change log and change counters ====================================================== #
    def _log(self, c, board_id, item_id, user, action, column_id=None, old=None, new=None, detail=None) -> None:
        c.execute("INSERT INTO activity (board_id, item_id, user_id, at, action, column_id, old, new, detail) "
                  "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                  (board_id, item_id, _uid(user), now_iso(), action, column_id,
                   None if old is None else dumps(old), None if new is None else dumps(new),
                   None if detail is None else dumps(detail)))

    def _bump(self, c, board_id: int, struct: bool = False) -> int:
        sql = ("UPDATE boards SET seq = seq + 1, updated_at = ?" + (", struct_seq = seq + 1" if struct else "")
               + " WHERE id = ? RETURNING seq")
        r = c.execute(sql, (now_iso(), board_id)).fetchone()
        if r is None:
            raise NotFound("that board no longer exists")
        return r[0]

    def _writable(self, c, board_id: int) -> None:
        r = c.execute("SELECT state FROM boards WHERE id = ?", (board_id,)).fetchone()
        if not r or r[0] == "deleted":
            raise NotFound("that board does not exist (it may have been deleted)")
        if r[0] != "active":
            raise Invalid("this board is archived, so it is read only - restore it to change it")

    def _touch(self, c, item_id: int, seq: int, user) -> None:
        """Mark an item (and its parent, for a subitem) as changed at this seq."""
        now = now_iso()
        r = c.execute("UPDATE items SET seq = ?, updated_at = ?, updated_by = ? WHERE id = ? RETURNING parent_id",
                      (seq, now, _uid(user), item_id)).fetchone()
        if r and r[0]:
            c.execute("UPDATE items SET seq = ? WHERE id = ?", (seq, r[0]))

    # ==== users ============================================================================== #
    def users(self, include_inactive: bool = False) -> list[dict]:
        sql = "SELECT * FROM users" + ("" if include_inactive else " WHERE active = 1") + " ORDER BY name COLLATE NOCASE"
        return [dict(r) for r in self.db.q(sql)]

    def user(self, uid: int) -> dict | None:
        r = self.db.one("SELECT * FROM users WHERE id = ?", (uid,))
        return dict(r) if r else None

    def user_by_email(self, email: str) -> dict | None:
        r = self.db.one("SELECT * FROM users WHERE email = ? COLLATE NOCASE", ((email or "").strip(),))
        return dict(r) if r else None

    def create_user(self, actor, email: str, name: str, role: str = "member", monday_id: str | None = None,
                    active: bool = True) -> dict:
        self._need_admin(actor)
        email = (email or "").strip().lower()
        if "@" not in email:
            raise Invalid("enter the person's email address")
        if role not in ROLES:
            raise Invalid("the role must be admin, member or viewer")
        if self.user_by_email(email):
            raise Invalid(f"{email} is already in ForWork")
        n = self.db.scalar("SELECT COUNT(*) FROM users", default=0)
        with self.db.write() as c:
            uid = c.execute("INSERT INTO users (email, name, role, active, color, monday_id, created_at) "
                            "VALUES (?, ?, ?, ?, ?, ?, ?)",
                            (email, _clean_name(name or email.split("@")[0], "name"), role, 1 if active else 0,
                             USER_COLORS[n % len(USER_COLORS)], monday_id, now_iso())).lastrowid
            self._log(c, None, None, actor, "user.create", new={"email": email, "role": role})
        return self.user(uid)

    def update_user(self, actor, uid: int, name: str | None = None, role: str | None = None,
                    active: bool | None = None) -> dict:
        self._need_admin(actor)
        u = self.user(uid)
        if not u:
            raise NotFound("that person is not in ForWork")
        if role is not None and role not in ROLES:
            raise Invalid("the role must be admin, member or viewer")
        new_role = role if role is not None else u["role"]
        new_active = u["active"] if active is None else (1 if active else 0)
        if (u["role"] == "admin" and u["active"]) and (new_role != "admin" or not new_active):
            admins = self.db.scalar("SELECT COUNT(*) FROM users WHERE role = 'admin' AND active = 1", default=0)
            if admins <= 1:
                raise Invalid("ForWork needs at least one admin - make someone else an admin first")
        with self.db.write() as c:
            c.execute("UPDATE users SET name = ?, role = ?, active = ? WHERE id = ?",
                      (_clean_name(name, "name") if name is not None else u["name"], new_role, new_active, uid))
            if not new_active:
                c.execute("DELETE FROM sessions WHERE user_id = ?", (uid,))
            self._log(c, None, None, actor, "user.update", old={k: u[k] for k in ("name", "role", "active")},
                      new={"name": name or u["name"], "role": new_role, "active": new_active}, detail={"user": uid})
        return self.user(uid)

    def teams(self) -> list[dict]:
        rows = self.db.q("SELECT t.id, t.name, GROUP_CONCAT(m.user_id) AS members FROM teams t "
                         "LEFT JOIN team_members m ON m.team_id = t.id GROUP BY t.id ORDER BY t.name")
        return [{"id": r["id"], "name": r["name"],
                 "members": [int(x) for x in (r["members"] or "").split(",") if x]} for r in rows]

    def ctx(self) -> Ctx:
        return Ctx({r["id"]: r["name"] for r in self.db.q("SELECT id, name FROM users")},
                   {r["id"]: r["name"] for r in self.db.q("SELECT id, name FROM teams")}, {})

    # ==== workspaces and folders ============================================================== #
    def workspaces(self) -> list[dict]:
        return [dict(r) for r in self.db.q("SELECT * FROM workspaces ORDER BY position, id")]

    def folders(self) -> list[dict]:
        return [dict(r) for r in self.db.q("SELECT * FROM folders ORDER BY position, id")]

    def create_workspace(self, user, name: str, monday_id: str | None = None) -> int:
        self._need_edit(user)
        with self.db.write() as c:
            pos = (c.execute("SELECT MAX(position) FROM workspaces").fetchone()[0] or 0) + 1
            wid = c.execute("INSERT INTO workspaces (name, position, monday_id, created_at) VALUES (?, ?, ?, ?)",
                            (_clean_name(name, "workspace name"), pos, monday_id, now_iso())).lastrowid
            self._log(c, None, None, user, "workspace.create", new={"name": name}, detail={"workspace": wid})
        return wid

    def rename_workspace(self, user, wid: int, name: str) -> None:
        self._need_edit(user)
        with self.db.write() as c:
            if not c.execute("UPDATE workspaces SET name = ? WHERE id = ?", (_clean_name(name, "workspace name"), wid)).rowcount:
                raise NotFound("that workspace no longer exists")

    def create_folder(self, user, workspace_id: int, name: str, monday_id: str | None = None) -> int:
        self._need_edit(user)
        with self.db.write() as c:
            if not c.execute("SELECT 1 FROM workspaces WHERE id = ?", (workspace_id,)).fetchone():
                raise NotFound("that workspace no longer exists")
            pos = (c.execute("SELECT MAX(position) FROM folders WHERE workspace_id = ?", (workspace_id,)).fetchone()[0] or 0) + 1
            return c.execute("INSERT INTO folders (workspace_id, name, position, monday_id) VALUES (?, ?, ?, ?)",
                             (workspace_id, _clean_name(name, "folder name"), pos, monday_id)).lastrowid

    def rename_folder(self, user, fid: int, name: str) -> None:
        self._need_edit(user)
        with self.db.write() as c:
            if not c.execute("UPDATE folders SET name = ? WHERE id = ?", (_clean_name(name, "folder name"), fid)).rowcount:
                raise NotFound("that folder no longer exists")

    def delete_folder(self, user, fid: int) -> None:
        """Boards in the folder move up to the workspace; nothing is lost."""
        self._need_edit(user)
        with self.db.write() as c:
            c.execute("UPDATE boards SET folder_id = NULL WHERE folder_id = ?", (fid,))
            c.execute("DELETE FROM folders WHERE id = ?", (fid,))

    # ==== boards ============================================================================== #
    def boards(self, states=("active",)) -> list[dict]:
        marks = ",".join("?" * len(states))
        rows = self.db.q(f"SELECT b.*, (SELECT COUNT(*) FROM items i WHERE i.board_id = b.id AND i.state = 'active' "
                         f"AND i.parent_id IS NULL) AS item_count FROM boards b WHERE b.state IN ({marks}) "
                         f"ORDER BY b.position, b.name COLLATE NOCASE", tuple(states))
        return [dict(r) for r in rows]

    def board(self, bid: int, allow_deleted: bool = False) -> dict:
        r = self.db.one("SELECT * FROM boards WHERE id = ?", (bid,))
        if r is None or (r["state"] == "deleted" and not allow_deleted):
            raise NotFound("that board does not exist (it may have been deleted)")
        return dict(r)

    def create_board(self, user, name: str, workspace_id: int, folder_id: int | None = None,
                     description: str = "", blank: bool = False, monday_id: str | None = None) -> int:
        """A new board with a group and the usual columns (Name, Owner, Status, Due date) unless blank."""
        self._need_edit(user)
        now = now_iso()
        with self.db.write() as c:
            if not c.execute("SELECT 1 FROM workspaces WHERE id = ?", (workspace_id,)).fetchone():
                raise NotFound("that workspace no longer exists")
            if folder_id and not c.execute("SELECT 1 FROM folders WHERE id = ? AND workspace_id = ?",
                                           (folder_id, workspace_id)).fetchone():
                raise Invalid("that folder is not in this workspace")
            pos = (c.execute("SELECT MAX(position) FROM boards").fetchone()[0] or 0) + 1
            bid = c.execute("INSERT INTO boards (workspace_id, folder_id, name, description, position, created_by, "
                            "created_at, updated_at, monday_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                            (workspace_id, folder_id or None, _clean_name(name, "board name"),
                             str(description or "")[:5000], pos, _uid(user), now, now, monday_id)).lastrowid
            self._insert_column(c, bid, "name", "Name", {}, 0, scope="item", key="name")
            self._insert_column(c, bid, "name", "Subitem", {}, 0, scope="subitem", key="name")
            if not blank:
                self._insert_column(c, bid, "people", "Owner", {}, 1)
                self._insert_column(c, bid, "status", "Status", column_type("status").default_settings(), 2)
                self._insert_column(c, bid, "date", "Due date", {}, 3)
                self._insert_column(c, bid, "people", "Owner", {}, 1, scope="subitem")
                self._insert_column(c, bid, "status", "Status", column_type("status").default_settings(), 2, scope="subitem")
                c.execute("INSERT INTO groups (board_id, title, color, position) VALUES (?, ?, ?, ?)",
                          (bid, "Group 1", GROUP_COLORS[0], 1))
            self._log(c, bid, None, user, "board.create", new={"name": name})
            self._bump(c, bid, struct=True)
        return bid

    def update_board(self, user, bid: int, name: str | None = None, description: str | None = None,
                     workspace_id: int | None = None, folder_id: int | None | str = "keep") -> None:
        self._need_edit(user)
        b = self.board(bid)
        with self.db.write() as c:
            if name is not None:
                c.execute("UPDATE boards SET name = ? WHERE id = ?", (_clean_name(name, "board name"), bid))
            if description is not None:
                c.execute("UPDATE boards SET description = ? WHERE id = ?", (str(description)[:5000], bid))
            if workspace_id is not None:
                if not c.execute("SELECT 1 FROM workspaces WHERE id = ?", (workspace_id,)).fetchone():
                    raise NotFound("that workspace no longer exists")
                c.execute("UPDATE boards SET workspace_id = ?, folder_id = NULL WHERE id = ?", (workspace_id, bid))
            if folder_id != "keep":
                ws = workspace_id or b["workspace_id"]
                if folder_id and not c.execute("SELECT 1 FROM folders WHERE id = ? AND workspace_id = ?",
                                               (folder_id, ws)).fetchone():
                    raise Invalid("that folder is not in the board's workspace")
                c.execute("UPDATE boards SET folder_id = ? WHERE id = ?", (folder_id or None, bid))
            self._log(c, bid, None, user, "board.update",
                      old={"name": b["name"], "description": b["description"]},
                      new={"name": name if name is not None else b["name"],
                           "description": description if description is not None else b["description"]})
            self._bump(c, bid, struct=True)

    def set_board_state(self, user, bid: int, state: str) -> None:
        """archive / restore (any member); delete moves to the bin (restorable by an admin)."""
        self._need_edit(user)
        if state not in ("active", "archived", "deleted"):
            raise Invalid("unknown board state")
        b = self.board(bid, allow_deleted=True)
        if b["state"] == "deleted":
            self._need_admin(user)
        with self.db.write() as c:
            c.execute("UPDATE boards SET state = ? WHERE id = ?", (state, bid))
            self._log(c, bid, None, user, f"board.{ {'active': 'restore', 'archived': 'archive', 'deleted': 'delete'}[state]}",
                      old={"state": b["state"]}, new={"state": state})
            self._bump(c, bid, struct=True)

    def favourite(self, user, bid: int, on: bool) -> None:
        with self.db.write() as c:
            if on:
                c.execute("INSERT OR IGNORE INTO favourites (user_id, board_id) VALUES (?, ?)", (user["id"], bid))
            else:
                c.execute("DELETE FROM favourites WHERE user_id = ? AND board_id = ?", (user["id"], bid))

    def favourites(self, user) -> set[int]:
        return {r[0] for r in self.db.q("SELECT board_id FROM favourites WHERE user_id = ?", (user["id"],))}

    def duplicate_board(self, user, bid: int, name: str, mode: str = "structure", workspace_id: int | None = None,
                        folder_id: int | None = None) -> int:
        """
        A copy of a board - e.g. next year's Annual Compliance board from this year's.  mode is
        "structure" (groups, columns, views, automations), "items" (and the items and subitems with their
        values) or "updates" (and the items' updates too), like monday.com's duplicate options.
        """
        self._need_edit(user)
        if mode not in ("structure", "items", "updates"):
            raise Invalid("choose what to copy")
        src = self.board(bid)
        now = now_iso()
        with self.db.write() as c:
            ws = workspace_id or src["workspace_id"]
            fid = folder_id if workspace_id else (folder_id or src["folder_id"])
            pos = (c.execute("SELECT MAX(position) FROM boards").fetchone()[0] or 0) + 1
            nb = c.execute("INSERT INTO boards (workspace_id, folder_id, name, description, position, created_by, "
                           "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                           (ws, fid, _clean_name(name, "board name"), src["description"], pos, _uid(user), now, now)).lastrowid
            gmap, cmap = {}, {}
            for g in c.execute("SELECT * FROM groups WHERE board_id = ? AND state = 'active' ORDER BY position", (bid,)).fetchall():
                gmap[g["id"]] = c.execute("INSERT INTO groups (board_id, title, color, position) VALUES (?, ?, ?, ?)",
                                          (nb, g["title"], g["color"], g["position"])).lastrowid
            cols = c.execute("SELECT * FROM columns WHERE board_id = ? AND state = 'active' ORDER BY position", (bid,)).fetchall()
            for col in cols:
                cmap[col["id"]] = c.execute(
                    "INSERT INTO columns (board_id, scope, title, type, key, settings, description, position, width) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (nb, col["scope"], col["title"], col["type"], col["key"], col["settings"], col["description"],
                     col["position"], col["width"])).lastrowid
            for col in cols:                                  # mirrors point at this board's relation column
                if col["type"] == "mirror":
                    s = loads(col["settings"], {})
                    if s.get("relation_column") in cmap:
                        s["relation_column"] = cmap[s["relation_column"]]
                        c.execute("UPDATE columns SET settings = ? WHERE id = ?", (dumps(s), cmap[col["id"]]))
            for v in c.execute("SELECT * FROM views WHERE board_id = ? AND state = 'active'", (bid,)).fetchall():
                c.execute("INSERT INTO views (board_id, name, type, settings, position, created_by) VALUES (?, ?, ?, ?, ?, ?)",
                          (nb, v["name"], v["type"], dumps(_remap_view(loads(v["settings"], {}), cmap)), v["position"], _uid(user)))
            for a in c.execute("SELECT * FROM automations WHERE board_id = ?", (bid,)).fetchall():
                t, act = _remap_rule(loads(a["trigger"], {}), cmap, gmap), _remap_rule(loads(a["action"], {}), cmap, gmap)
                if t is not None and act is not None:
                    c.execute("INSERT INTO automations (board_id, trigger, action, active, created_by, created_at) "
                              "VALUES (?, ?, ?, ?, ?, ?)", (nb, dumps(t), dumps(act), a["active"], _uid(user), now))
            n_items = 0
            if mode in ("items", "updates"):
                imap = {}
                rows = c.execute("SELECT * FROM items WHERE board_id = ? AND state = 'active' ORDER BY parent_id IS NOT NULL, position",
                                 (bid,)).fetchall()
                for it in rows:
                    if it["parent_id"] and it["parent_id"] not in imap:
                        continue
                    gid = gmap.get(it["group_id"]) if not it["parent_id"] else None
                    if not it["parent_id"] and gid is None:
                        continue
                    imap[it["id"]] = c.execute(
                        "INSERT INTO items (board_id, group_id, parent_id, name, position, created_by, created_at, updated_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (nb, gid, imap.get(it["parent_id"]), it["name"], it["position"], _uid(user), now, now)).lastrowid
                    n_items += 1
                for iv in c.execute("SELECT iv.* FROM item_values iv JOIN items i ON i.id = iv.item_id "
                                    "WHERE i.board_id = ? AND i.state = 'active'", (bid,)).fetchall():
                    if iv["item_id"] in imap and iv["column_id"] in cmap:
                        c.execute("INSERT INTO item_values (item_id, column_id, value, text, updated_at, updated_by) "
                                  "VALUES (?, ?, ?, ?, ?, ?)",
                                  (imap[iv["item_id"]], cmap[iv["column_id"]], iv["value"], iv["text"], now, _uid(user)))
                if mode == "updates":
                    umap = {}
                    for u in c.execute("SELECT * FROM updates WHERE item_id IN (SELECT id FROM items WHERE board_id = ?) "
                                       "AND state = 'active' ORDER BY parent_id IS NOT NULL, id", (bid,)).fetchall():
                        if u["item_id"] not in imap or (u["parent_id"] and u["parent_id"] not in umap):
                            continue
                        umap[u["id"]] = c.execute(
                            "INSERT INTO updates (item_id, parent_id, body, text, created_by, author_name, created_at, edited_at) "
                            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                            (imap[u["item_id"]], umap.get(u["parent_id"]), u["body"], u["text"], u["created_by"],
                             u["author_name"], u["created_at"], u["edited_at"])).lastrowid
            self._log(c, nb, None, user, "board.duplicate", detail={"from": bid, "mode": mode, "items": n_items})
            self._bump(c, nb, struct=True)
        return nb

    # ==== groups ============================================================================== #
    def add_group(self, user, bid: int, title: str = "New group", color: str | None = None,
                  where: str = "top", monday_id: str | None = None) -> int:
        self._need_edit(user)
        self.board(bid)
        with self.db.write() as c:
            lo, hi, n = c.execute("SELECT MIN(position), MAX(position), COUNT(*) FROM groups WHERE board_id = ? "
                                  "AND state = 'active'", (bid,)).fetchone()
            pos = ((lo or 0) - 1) if where == "top" else ((hi or 0) + 1)
            color = color if color and re.match(r"^#[0-9a-fA-F]{6}$", color) else GROUP_COLORS[n % len(GROUP_COLORS)]
            gid = c.execute("INSERT INTO groups (board_id, title, color, position, monday_id) VALUES (?, ?, ?, ?, ?)",
                            (bid, _clean_name(title, "group name"), color.lower(), pos, monday_id)).lastrowid
            self._log(c, bid, None, user, "group.create", new={"title": title}, detail={"group": gid})
            self._bump(c, bid, struct=True)
        return gid

    def _group(self, gid: int) -> dict:
        r = self.db.one("SELECT * FROM groups WHERE id = ? AND state != 'deleted'", (gid,))
        if r is None:
            raise NotFound("that group no longer exists")
        return dict(r)

    def update_group(self, user, gid: int, title: str | None = None, color: str | None = None,
                     before: int | None = None, after: int | None = None) -> None:
        self._need_edit(user)
        g = self._group(gid)
        with self.db.write() as c:
            if title is not None:
                c.execute("UPDATE groups SET title = ? WHERE id = ?", (_clean_name(title, "group name"), gid))
            if color is not None:
                if not re.match(r"^#[0-9a-fA-F]{6}$", color):
                    raise Invalid("pick a colour from the list")
                c.execute("UPDATE groups SET color = ? WHERE id = ?", (color.lower(), gid))
            if before or after:
                pos = self._between(c, "groups", "board_id = ? AND state = 'active'", (g["board_id"],), before, after, gid)
                c.execute("UPDATE groups SET position = ? WHERE id = ?", (pos, gid))
            self._log(c, g["board_id"], None, user, "group.update", old={"title": g["title"], "color": g["color"]},
                      new={"title": title or g["title"], "color": color or g["color"]}, detail={"group": gid})
            self._bump(c, g["board_id"], struct=True)

    def delete_group(self, user, gid: int, archive: bool = False) -> None:
        """Delete (or archive) a group and the items in it; they can be restored from the board's activity."""
        self._need_edit(user)
        g = self._group(gid)
        state = "archived" if archive else "deleted"
        with self.db.write() as c:
            left = c.execute("SELECT COUNT(*) FROM groups WHERE board_id = ? AND state = 'active' AND id != ?",
                             (g["board_id"], gid)).fetchone()[0]
            if not left:
                raise Invalid("a board needs at least one group - add another group first")
            seq = self._bump(c, g["board_id"], struct=True)
            c.execute("UPDATE groups SET state = ? WHERE id = ?", (state, gid))
            c.execute("UPDATE items SET state = ?, seq = ? WHERE group_id = ? AND state = 'active'", (state, seq, gid))
            self._log(c, g["board_id"], None, user, f"group.{'archive' if archive else 'delete'}",
                      old={"title": g["title"]}, detail={"group": gid})

    def restore_group(self, user, gid: int) -> None:
        self._need_edit(user)
        r = self.db.one("SELECT * FROM groups WHERE id = ?", (gid,))
        if r is None:
            raise NotFound("that group no longer exists")
        with self.db.write() as c:
            seq = self._bump(c, r["board_id"], struct=True)
            c.execute("UPDATE items SET state = 'active', seq = ? WHERE group_id = ? AND state = ?", (seq, gid, r["state"]))
            c.execute("UPDATE groups SET state = 'active' WHERE id = ?", (gid,))
            self._log(c, r["board_id"], None, user, "group.restore", new={"title": r["title"]}, detail={"group": gid})

    def _between(self, c, table: str, where: str, params: tuple, before: int | None, after: int | None,
                 moving: int) -> float:
        """A position for `moving` just before `before` or just after `after` (renumbering if they are too close)."""
        rows = c.execute(f"SELECT id, position FROM {table} WHERE {where} AND id != ? ORDER BY position, id",
                         (*params, moving)).fetchall()
        ids = [r["id"] for r in rows]
        pos = [r["position"] for r in rows]
        if before and before in ids:
            i = ids.index(before)
        elif after and after in ids:
            i = ids.index(after) + 1
        else:
            i = len(ids)
        lo = pos[i - 1] if i > 0 else (pos[0] - 2 if pos else 0)
        hi = pos[i] if i < len(pos) else lo + 2
        if hi - lo < 1e-6:                                    # renumber this list 1, 2, 3 ...
            for n, rid in enumerate(ids, start=1):
                c.execute(f"UPDATE {table} SET position = ? WHERE id = ?", (float(n), rid))
            lo, hi = float(i), float(i + 1)
        return (lo + hi) / 2

    # ==== columns ============================================================================= #
    def _insert_column(self, c, bid, type_, title, settings, position, scope="item", key=None, width=None,
                       description="", monday_id=None) -> int:
        taken = {r[0] for r in c.execute("SELECT key FROM columns WHERE board_id = ? AND scope = ?", (bid, scope))}
        if key is None or key in taken:
            key = make_key(title if key is None else key, taken)
        settings = column_type(type_).clean_settings(settings) if type_ in ("status", "dropdown", "numbers", "board_relation",
                                                                             "mirror", "formula") else (settings or {})
        return c.execute("INSERT INTO columns (board_id, scope, title, type, key, settings, description, position, width, monday_id) "
                         "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                         (bid, scope, title, type_, key, dumps(settings), description or "", position, width,
                          monday_id)).lastrowid

    def column(self, cid: int) -> dict:
        r = self.db.one("SELECT * FROM columns WHERE id = ? AND state = 'active'", (cid,))
        if r is None:
            raise NotFound("that column no longer exists")
        d = dict(r)
        d["settings"] = loads(d["settings"], {})
        return d

    def columns(self, bid: int, scope: str | None = None) -> list[dict]:
        sql = "SELECT * FROM columns WHERE board_id = ? AND state = 'active'" + (" AND scope = ?" if scope else "")
        rows = self.db.q(sql + " ORDER BY position, id", (bid, scope) if scope else (bid,))
        out = []
        for r in rows:
            d = dict(r)
            d["settings"] = loads(d["settings"], {})
            out.append(d)
        return out

    def add_column(self, user, bid: int, type_: str, title: str, settings: dict | None = None, scope: str = "item",
                   after: int | None = None) -> int:
        self._need_edit(user)
        self.board(bid)
        t = column_type(type_)
        if type_ != t.name or not t.creatable:
            raise Invalid(f"ForWork cannot add a {type_} column")
        if scope not in ("item", "subitem"):
            raise Invalid("bad column scope")
        settings = t.clean_settings(settings if settings is not None else t.default_settings())
        self._check_column_settings(bid, type_, settings)
        with self.db.write() as c:
            cid = self._insert_column(c, bid, type_, _clean_name(title, "column name"), settings, 0, scope=scope)
            pos = self._between(c, "columns", "board_id = ? AND scope = ? AND state = 'active'", (bid, scope), None, after, cid)
            c.execute("UPDATE columns SET position = ? WHERE id = ?", (pos, cid))
            self._log(c, bid, None, user, "column.create", column_id=cid, new={"title": title, "type": type_})
            self._bump(c, bid, struct=True)
        return cid

    def _check_column_settings(self, bid: int, type_: str, settings: dict) -> None:
        if type_ == "board_relation":
            for b in settings.get("board_ids") or []:
                if not self.db.one("SELECT 1 FROM boards WHERE id = ? AND state != 'deleted'", (b,)):
                    raise Invalid("one of the boards to connect no longer exists")
        if type_ == "mirror":
            rel = settings.get("relation_column")
            if rel:
                r = self.db.one("SELECT type, board_id FROM columns WHERE id = ?", (rel,))
                if not r or r["type"] != "board_relation" or r["board_id"] != bid:
                    raise Invalid("a mirror column needs a Connect boards column on the same board")
        if type_ == "formula" and settings.get("formula"):
            keys = {r[0]: 1 for r in self.db.q("SELECT key FROM columns WHERE board_id = ?", (bid,))}
            try:
                eval_formula(settings["formula"], keys)
            except FormulaError as e:
                raise Invalid(f"the formula does not work: {e}") from None

    def update_column(self, user, cid: int, title: str | None = None, settings: dict | None = None,
                      width: int | None = None, description: str | None = None, before: int | None = None,
                      after: int | None = None) -> None:
        self._need_edit(user)
        col = self.column(cid)
        t = column_type(col["type"])
        with self.db.write() as c:
            if title is not None:
                c.execute("UPDATE columns SET title = ? WHERE id = ?", (_clean_name(title, "column name"), cid))
            if description is not None:
                c.execute("UPDATE columns SET description = ? WHERE id = ?", (str(description)[:1000], cid))
            if width is not None:
                c.execute("UPDATE columns SET width = ? WHERE id = ?", (max(60, min(800, int(width))), cid))
            if before or after:
                pos = self._between(c, "columns", "board_id = ? AND scope = ? AND state = 'active'",
                                    (col["board_id"], col["scope"]), before, after, cid)
                c.execute("UPDATE columns SET position = ? WHERE id = ?", (pos, cid))
            if settings is not None:
                new = t.clean_settings(settings)
                if col["type"] in ("status", "dropdown"):
                    # a label in use cannot disappear: keep it, hidden, so items keep their value
                    used = self._used_label_ids(c, col)
                    have = {lab["id"] for lab in new["labels"]}
                    for lab in col["settings"].get("labels") or []:
                        if lab["id"] in used and lab["id"] not in have:
                            new["labels"].append({**lab, "hidden": True})
                self._check_column_settings(col["board_id"], col["type"], new)
                c.execute("UPDATE columns SET settings = ? WHERE id = ?", (dumps(new), cid))
                self._refresh_texts(c, cid, col["type"], new)
            self._log(c, col["board_id"], None, user, "column.update", column_id=cid,
                      old={"title": col["title"]}, new={"title": title or col["title"]})
            self._bump(c, col["board_id"], struct=True)

    def _used_label_ids(self, c, col) -> set[int]:
        used = set()
        for (v,) in c.execute("SELECT value FROM item_values WHERE column_id = ?", (col["id"],)):
            v = loads(v)
            if isinstance(v, int):
                used.add(v)
            elif isinstance(v, list):
                used.update(x for x in v if isinstance(x, int))
        return used

    def _refresh_texts(self, c, cid: int, type_: str, settings: dict) -> None:
        t, ctx = column_type(type_), self.ctx()
        for item_id, v in c.execute("SELECT item_id, value FROM item_values WHERE column_id = ?", (cid,)).fetchall():
            c.execute("UPDATE item_values SET text = ? WHERE item_id = ? AND column_id = ?",
                      (t.text(loads(v), settings, ctx), item_id, cid))

    def delete_column(self, user, cid: int) -> None:
        self._need_edit(user)
        col = self.column(cid)
        if col["type"] == "name":
            raise Invalid("the name column cannot be deleted")
        with self.db.write() as c:
            c.execute("UPDATE columns SET state = 'deleted' WHERE id = ?", (cid,))
            self._log(c, col["board_id"], None, user, "column.delete", column_id=cid, old={"title": col["title"]})
            self._bump(c, col["board_id"], struct=True)

    def restore_column(self, user, cid: int) -> None:
        self._need_edit(user)
        r = self.db.one("SELECT * FROM columns WHERE id = ?", (cid,))
        if r is None:
            raise NotFound("that column no longer exists")
        with self.db.write() as c:
            c.execute("UPDATE columns SET state = 'active' WHERE id = ?", (cid,))
            self._log(c, r["board_id"], None, user, "column.restore", column_id=cid, new={"title": r["title"]})
            self._bump(c, r["board_id"], struct=True)

    # ==== items =============================================================================== #
    def item(self, iid: int, allow_inactive: bool = False) -> dict:
        r = self.db.one("SELECT * FROM items WHERE id = ?", (iid,))
        if r is None or (r["state"] != "active" and not allow_inactive):
            raise NotFound("that item no longer exists (it may have been deleted or archived)")
        return dict(r)

    def create_item(self, user, bid: int, name: str, group_id: int | None = None, values: dict | None = None,
                    parent_id: int | None = None, where: str = "bottom", monday_id: str | None = None,
                    run_automations: bool = True) -> int:
        """A new item at the top or bottom of a group, or a subitem (parent_id) at the end of its parent's list."""
        self._need_edit(user)
        board = self.board(bid)
        if board["state"] != "active":
            raise Invalid("this board is archived - restore it to add items")
        now = now_iso()
        with self.db.write() as c:
            if parent_id:
                p = c.execute("SELECT * FROM items WHERE id = ? AND state = 'active'", (parent_id,)).fetchone()
                if not p or p["board_id"] != bid or p["parent_id"]:
                    raise Invalid("subitems go under an item on the same board")
                group_id = None
                pos = (c.execute("SELECT MAX(position) FROM items WHERE parent_id = ?", (parent_id,)).fetchone()[0] or 0) + 1
            else:
                if group_id is None:
                    g = c.execute("SELECT id FROM groups WHERE board_id = ? AND state = 'active' ORDER BY position LIMIT 1",
                                  (bid,)).fetchone()
                    if not g:
                        raise Invalid("add a group to the board first")
                    group_id = g[0]
                elif not c.execute("SELECT 1 FROM groups WHERE id = ? AND board_id = ? AND state = 'active'",
                                   (group_id, bid)).fetchone():
                    raise Invalid("that group is not on this board")
                lo, hi = c.execute("SELECT MIN(position), MAX(position) FROM items WHERE group_id = ? AND state = 'active'",
                                   (group_id,)).fetchone()
                pos = ((lo or 0) - 1) if where == "top" else ((hi or 0) + 1)
            iid = c.execute("INSERT INTO items (board_id, group_id, parent_id, name, position, created_by, created_at, "
                            "updated_at, updated_by, monday_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                            (bid, group_id, parent_id, _clean_name(name, "item name"), pos, _uid(user), now, now,
                             _uid(user), monday_id)).lastrowid
            seq = self._bump(c, bid)
            self._touch(c, iid, seq, user)
            self._log(c, bid, iid, user, "subitem.create" if parent_id else "item.create", new={"name": name},
                      detail={"parent": parent_id} if parent_id else None)
            for cid, raw in (values or {}).items():
                self._set_value(c, user, iid, int(cid), raw, run_automations=False)
            if run_automations:
                from .automations import on_item_created
                on_item_created(self, c, iid)
        return iid

    def rename_item(self, user, iid: int, name: str) -> None:
        self._need_edit(user)
        it = self.item(iid)
        name = _clean_name(name, "item name")
        if name == it["name"]:
            return
        with self.db.write() as c:
            self._writable(c, it["board_id"])
            c.execute("UPDATE items SET name = ? WHERE id = ?", (name, iid))
            seq = self._bump(c, it["board_id"])
            self._touch(c, iid, seq, user)
            self._log(c, it["board_id"], iid, user, "item.rename", old=it["name"], new=name)

    def set_value(self, user, iid: int, cid: int, raw) -> list[int]:
        """Change one cell.  Returns the ids of the items that changed (automations can change others)."""
        self._need_edit(user)
        with self.db.write() as c:
            return self._set_value(c, user, iid, cid, raw)

    def _set_value(self, c, user, iid: int, cid: int, raw, run_automations: bool = True, depth: int = 0) -> list[int]:
        it = c.execute("SELECT * FROM items WHERE id = ?", (iid,)).fetchone()
        if not it or it["state"] != "active":
            raise NotFound("that item no longer exists (it may have been deleted or archived)")
        col = c.execute("SELECT * FROM columns WHERE id = ? AND state = 'active'", (cid,)).fetchone()
        if not col or col["board_id"] != it["board_id"] or col["scope"] != ("subitem" if it["parent_id"] else "item"):
            raise Invalid("that column is not on this item's board")
        if depth == 0:
            self._writable(c, it["board_id"])
        if col["type"] == "name":
            self.rename_item(user, iid, raw)
            return [iid]
        t = column_type(col["type"])
        if not t.editable:
            raise Invalid(f"the {col['title']} column is worked out by ForWork and cannot be typed in")
        settings = loads(col["settings"], {})
        try:
            new = t.normalize(raw, settings)
        except ValueError_ as e:
            raise Invalid(f"{col['title']}: {e}") from None
        except ValueError:
            raise Invalid(f"{col['title']}: that value does not fit this column") from None
        self._check_refs(c, col, settings, new, iid)
        old_row = c.execute("SELECT value, text FROM item_values WHERE item_id = ? AND column_id = ?", (iid, cid)).fetchone()
        old = loads(old_row["value"]) if old_row else None
        if old == new:
            return []
        ctx = self.ctx()
        if col["type"] == "board_relation" and new:
            ctx.item_names.update(self._names(c, new))
        text = t.text(new, settings, ctx) if new is not None else ""
        now = now_iso()
        if new is None:
            c.execute("DELETE FROM item_values WHERE item_id = ? AND column_id = ?", (iid, cid))
        else:
            c.execute("INSERT INTO item_values (item_id, column_id, value, text, updated_at, updated_by) VALUES (?, ?, ?, ?, ?, ?) "
                      "ON CONFLICT(item_id, column_id) DO UPDATE SET value = excluded.value, text = excluded.text, "
                      "updated_at = excluded.updated_at, updated_by = excluded.updated_by",
                      (iid, cid, dumps(new), text, now, _uid(user)))
        seq = self._bump(c, it["board_id"])
        self._touch(c, iid, seq, user)
        self._log(c, it["board_id"], iid, user, "item.value", column_id=cid,
                  old={"value": old, "text": old_row["text"] if old_row else ""}, new={"value": new, "text": text},
                  detail={"automation": True} if depth else None)
        changed = [iid]
        if col["type"] == "people":
            added = set((new or {}).get("users") or []) - set((old or {}).get("users") or [])
            for u in added:
                if u != _uid(user):
                    self._notify(c, u, "assigned", f"assigned you to {it['name']} ({col['title']})", item_id=iid, by=user)
        if run_automations and depth < 3:
            from .automations import on_value_changed
            changed += on_value_changed(self, c, iid, dict(col), old, new, depth)
        return changed

    def _check_refs(self, c, col, settings, new, iid) -> None:
        if new is None:
            return
        if col["type"] == "people":
            for u in new.get("users") or []:
                if not c.execute("SELECT 1 FROM users WHERE id = ?", (u,)).fetchone():
                    raise Invalid("one of those people is not in ForWork")
            for t in new.get("teams") or []:
                if not c.execute("SELECT 1 FROM teams WHERE id = ?", (t,)).fetchone():
                    raise Invalid("one of those teams is not in ForWork")
        elif col["type"] == "board_relation":
            allowed = set(settings.get("board_ids") or [])
            for i in new:
                r = c.execute("SELECT board_id FROM items WHERE id = ? AND state != 'deleted'", (i,)).fetchone()
                if not r or (allowed and r[0] not in allowed):
                    raise Invalid(f"{col['title']}: that item is not on a connected board")
        elif col["type"] == "file":
            for f in new:
                if not c.execute("SELECT 1 FROM files WHERE id = ? AND item_id = ?", (f, iid)).fetchone():
                    raise Invalid("that file is not attached to this item")

    def _names(self, c, ids) -> dict:
        ids = [int(i) for i in ids]
        out = {}
        for k in range(0, len(ids), 500):
            chunk = ids[k:k + 500]
            for r in c.execute(f"SELECT id, name FROM items WHERE id IN ({','.join('?' * len(chunk))})", chunk):
                out[r[0]] = r[1]
        return out

    def move_item(self, user, iid: int, group_id: int | None = None, before: int | None = None,
                  after: int | None = None) -> None:
        """Move an item to another group and/or position (before/after another item in that group)."""
        self._need_edit(user)
        it = self.item(iid)
        with self.db.write() as c:
            self._writable(c, it["board_id"])
            if it["parent_id"]:
                pos = self._between(c, "items", "parent_id = ? AND state = 'active'", (it["parent_id"],), before, after, iid)
                c.execute("UPDATE items SET position = ? WHERE id = ?", (pos, iid))
                gid = None
            else:
                gid = group_id or it["group_id"]
                if not c.execute("SELECT 1 FROM groups WHERE id = ? AND board_id = ? AND state = 'active'",
                                 (gid, it["board_id"])).fetchone():
                    raise Invalid("that group is not on this board")
                pos = self._between(c, "items", "group_id = ? AND state = 'active'", (gid,), before, after, iid)
                c.execute("UPDATE items SET group_id = ?, position = ? WHERE id = ?", (gid, pos, iid))
            seq = self._bump(c, it["board_id"])
            self._touch(c, iid, seq, user)
            if gid and gid != it["group_id"]:
                names = dict(c.execute("SELECT id, title FROM groups WHERE id IN (?, ?)", (it["group_id"], gid)).fetchall())
                self._log(c, it["board_id"], iid, user, "item.move", old={"group": it["group_id"], "title": names.get(it["group_id"])},
                          new={"group": gid, "title": names.get(gid)})

    def move_item_to_board(self, user, iid: int, board_id: int, group_id: int | None = None) -> None:
        """Move an item (with its subitems and updates) to another board; values go to columns with the same title and type."""
        self._need_edit(user)
        it = self.item(iid)
        if it["parent_id"]:
            raise Invalid("move the parent item instead")
        if board_id == it["board_id"]:
            return self.move_item(user, iid, group_id=group_id)
        dest = self.board(board_id)
        with self.db.write() as c:
            if group_id is None:
                g = c.execute("SELECT id FROM groups WHERE board_id = ? AND state = 'active' ORDER BY position LIMIT 1",
                              (board_id,)).fetchone()
                if not g:
                    raise Invalid("that board has no groups")
                group_id = g[0]
            elif not c.execute("SELECT 1 FROM groups WHERE id = ? AND board_id = ?", (group_id, board_id)).fetchone():
                raise Invalid("that group is not on the board")
            src_cols = {r["id"]: r for r in c.execute("SELECT * FROM columns WHERE board_id = ?", (it["board_id"],))}
            dst_cols = {(r["scope"], r["title"].lower(), r["type"]): r["id"]
                        for r in c.execute("SELECT * FROM columns WHERE board_id = ? AND state = 'active'", (board_id,))}
            ids = [iid] + [r[0] for r in c.execute("SELECT id FROM items WHERE parent_id = ?", (iid,))]
            for x in ids:
                for row in c.execute("SELECT column_id FROM item_values WHERE item_id = ?", (x,)).fetchall():
                    sc = src_cols.get(row[0])
                    target = dst_cols.get((sc["scope"], sc["title"].lower(), sc["type"])) if sc else None
                    if target and target != row[0]:
                        c.execute("UPDATE OR REPLACE item_values SET column_id = ? WHERE item_id = ? AND column_id = ?",
                                  (target, x, row[0]))
                    elif not target:
                        c.execute("DELETE FROM item_values WHERE item_id = ? AND column_id = ?", (x, row[0]))
            hi = c.execute("SELECT MAX(position) FROM items WHERE group_id = ?", (group_id,)).fetchone()[0] or 0
            c.execute("UPDATE items SET board_id = ?, group_id = ?, position = ? WHERE id = ?", (board_id, group_id, hi + 1, iid))
            c.execute("UPDATE items SET board_id = ? WHERE parent_id = ?", (board_id, iid))
            old_seq = self._bump(c, it["board_id"])
            c.execute("UPDATE items SET seq = ? WHERE id IN (%s)" % ",".join("?" * len(ids)), (old_seq, *ids))
            seq = self._bump(c, board_id)
            for x in ids:
                self._touch(c, x, seq, user)
            self._log(c, it["board_id"], iid, user, "item.move_board", old={"board": it["board_id"]},
                      new={"board": board_id, "title": dest["name"]})
            self._log(c, board_id, iid, user, "item.move_board", old={"board": it["board_id"]}, new={"board": board_id})
            # the old board's open pages see the item leave: record a tombstone at its old seq
            c.execute("INSERT INTO activity (board_id, item_id, user_id, at, action, detail) VALUES (?, ?, ?, ?, 'item.left', ?)",
                      (it["board_id"], iid, _uid(user), now_iso(), dumps({"seq": old_seq})))

    def duplicate_item(self, user, iid: int, with_updates: bool = False) -> int:
        self._need_edit(user)
        it = self.item(iid)
        now = now_iso()
        with self.db.write() as c:
            self._writable(c, it["board_id"])
            if it["parent_id"]:
                pos = it["position"] + 0.5
            else:
                nxt = c.execute("SELECT MIN(position) FROM items WHERE group_id = ? AND position > ? AND state = 'active'",
                                (it["group_id"], it["position"])).fetchone()[0]
                pos = (it["position"] + nxt) / 2 if nxt is not None else it["position"] + 1
            new_id = c.execute("INSERT INTO items (board_id, group_id, parent_id, name, position, created_by, created_at, "
                               "updated_at, updated_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                               (it["board_id"], it["group_id"], it["parent_id"], it["name"] + " (copy)", pos, _uid(user),
                                now, now, _uid(user))).lastrowid
            pairs = [(iid, new_id)]
            for s in c.execute("SELECT * FROM items WHERE parent_id = ? AND state = 'active' ORDER BY position", (iid,)).fetchall():
                sid = c.execute("INSERT INTO items (board_id, parent_id, name, position, created_by, created_at, updated_at) "
                                "VALUES (?, ?, ?, ?, ?, ?, ?)", (s["board_id"], new_id, s["name"], s["position"],
                                                                 _uid(user), now, now)).lastrowid
                pairs.append((s["id"], sid))
            for a, b in pairs:
                c.execute("INSERT INTO item_values (item_id, column_id, value, text, updated_at, updated_by) "
                          "SELECT ?, column_id, value, text, ?, ? FROM item_values WHERE item_id = ? "
                          "AND column_id NOT IN (SELECT id FROM columns WHERE type = 'file')", (b, now, _uid(user), a))
                if with_updates:
                    for u in c.execute("SELECT * FROM updates WHERE item_id = ? AND parent_id IS NULL AND state = 'active'", (a,)).fetchall():
                        c.execute("INSERT INTO updates (item_id, body, text, created_by, author_name, created_at) "
                                  "VALUES (?, ?, ?, ?, ?, ?)", (b, u["body"], u["text"], u["created_by"], u["author_name"],
                                                                u["created_at"]))
            seq = self._bump(c, it["board_id"])
            for _, b in pairs:
                self._touch(c, b, seq, user)
            self._log(c, it["board_id"], new_id, user, "item.duplicate", detail={"from": iid})
        return new_id

    def set_item_state(self, user, iid: int, state: str) -> None:
        """archive, delete (to the bin) or restore an item and its subitems."""
        self._need_edit(user)
        if state not in ("active", "archived", "deleted"):
            raise Invalid("unknown item state")
        it = self.item(iid, allow_inactive=True)
        if state == it["state"]:
            return
        with self.db.write() as c:
            if state == "active" and not it["parent_id"]:
                g = c.execute("SELECT state FROM groups WHERE id = ?", (it["group_id"],)).fetchone()
                if not g or g[0] != "active":
                    first = c.execute("SELECT id FROM groups WHERE board_id = ? AND state = 'active' ORDER BY position LIMIT 1",
                                      (it["board_id"],)).fetchone()
                    if not first:
                        raise Invalid("the board has no group to restore the item into")
                    c.execute("UPDATE items SET group_id = ? WHERE id = ?", (first[0], iid))
            seq = self._bump(c, it["board_id"])
            c.execute("UPDATE items SET state = ?, seq = ? WHERE id = ? OR (parent_id = ? AND state = ?)",
                      (state, seq, iid, iid, it["state"]))
            self._touch(c, iid, seq, user)
            action = {"active": "restore", "archived": "archive", "deleted": "delete"}[state]
            self._log(c, it["board_id"], iid, user, f"item.{action}", old={"state": it["state"], "name": it["name"]},
                      new={"state": state})

    # ==== updates (comments) =================================================================== #
    def updates(self, iid: int) -> list[dict]:
        rows = self.db.q("SELECT u.*, us.name AS user_name FROM updates u LEFT JOIN users us ON us.id = u.created_by "
                         "WHERE u.item_id = ? AND u.state = 'active' ORDER BY u.created_at, u.id", (iid,))
        files = defaultdict(list)
        for f in self.db.q("SELECT id, update_id, name, size, sha256 FROM files WHERE item_id = ? AND update_id IS NOT NULL "
                           "AND state = 'active'", (iid,)):
            files[f["update_id"]].append(dict(f))
        top, replies = [], defaultdict(list)
        for r in rows:
            d = dict(r)
            d["author"] = d.pop("user_name") or d["author_name"] or "Someone"
            d["files"] = files.get(d["id"], [])
            (replies[d["parent_id"]] if d["parent_id"] else top).append(d)
        for d in top:
            d["replies"] = replies.get(d["id"], [])
        top.reverse()                                          # newest first, like monday.com
        return top

    def add_update(self, user, iid: int, body: str, parent_id: int | None = None, *, created_at: str | None = None,
                   author_name: str = "", monday_id: str | None = None, notify: bool = True) -> int:
        self._need_edit(user)
        it = self.item(iid, allow_inactive=monday_id is not None)
        clean = sanitize.clean_html(body)
        text = sanitize.html_to_text(clean)
        if not text and "<" not in clean:
            raise Invalid("write something first")
        with self.db.write() as c:
            if parent_id:
                p = c.execute("SELECT * FROM updates WHERE id = ? AND item_id = ?", (parent_id, iid)).fetchone()
                if not p:
                    raise Invalid("you can only reply to an update on the same item")
                if p["parent_id"]:
                    parent_id = p["parent_id"]
            uid = c.execute("INSERT INTO updates (item_id, parent_id, body, text, created_by, author_name, created_at, monday_id) "
                            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                            (iid, parent_id, clean, text, _uid(user), author_name or (user["name"] if user else ""),
                             created_at or now_iso(), monday_id)).lastrowid
            if notify:
                seq = self._bump(c, it["board_id"])
                self._touch(c, iid, seq, user)
                self._log(c, it["board_id"], iid, user, "update.create", new={"text": text[:300]}, detail={"update": uid})
                told = set()
                for m in sanitize.mentioned_users(clean):
                    if m != _uid(user) and m not in told:
                        told.add(m)
                        self._notify(c, m, "mention", f"mentioned you on {it['name']}: {text[:140]}", item_id=iid,
                                     update_id=uid, by=user)
                if parent_id:
                    for (who,) in c.execute("SELECT DISTINCT created_by FROM updates WHERE (id = ? OR parent_id = ?) "
                                            "AND created_by IS NOT NULL", (parent_id, parent_id)).fetchall():
                        if who != _uid(user) and who not in told:
                            told.add(who)
                            self._notify(c, who, "reply", f"replied on {it['name']}: {text[:140]}", item_id=iid,
                                         update_id=uid, by=user)
        return uid

    def edit_update(self, user, update_id: int, body: str) -> None:
        self._need_edit(user)
        u = self.db.one("SELECT u.*, i.board_id FROM updates u JOIN items i ON i.id = u.item_id WHERE u.id = ? "
                        "AND u.state = 'active'", (update_id,))
        if not u:
            raise NotFound("that update no longer exists")
        if user is not None and u["created_by"] != user["id"] and user["role"] != "admin":
            raise Forbidden("you can only edit your own updates")
        clean = sanitize.clean_html(body)
        with self.db.write() as c:
            c.execute("UPDATE updates SET body = ?, text = ?, edited_at = ? WHERE id = ?",
                      (clean, sanitize.html_to_text(clean), now_iso(), update_id))
            seq = self._bump(c, u["board_id"])
            self._touch(c, u["item_id"], seq, user)
            self._log(c, u["board_id"], u["item_id"], user, "update.edit", old={"text": u["text"][:300]},
                      detail={"update": update_id})

    def delete_update(self, user, update_id: int) -> None:
        self._need_edit(user)
        u = self.db.one("SELECT u.*, i.board_id FROM updates u JOIN items i ON i.id = u.item_id WHERE u.id = ?", (update_id,))
        if not u:
            raise NotFound("that update no longer exists")
        if user is not None and u["created_by"] != user["id"] and user["role"] != "admin":
            raise Forbidden("you can only delete your own updates")
        with self.db.write() as c:
            c.execute("UPDATE updates SET state = 'deleted' WHERE id = ? OR parent_id = ?", (update_id, update_id))
            seq = self._bump(c, u["board_id"])
            self._touch(c, u["item_id"], seq, user)
            self._log(c, u["board_id"], u["item_id"], user, "update.delete", old={"text": u["text"][:300]},
                      detail={"update": update_id})

    # ==== notifications ======================================================================== #
    def _notify(self, c, user_id: int, kind: str, text: str, item_id=None, update_id=None, by=None) -> None:
        r = c.execute("SELECT active FROM users WHERE id = ?", (user_id,)).fetchone()
        if not r or not r[0]:
            return
        c.execute("INSERT INTO notifications (user_id, item_id, update_id, kind, text, by_user, created_at) "
                  "VALUES (?, ?, ?, ?, ?, ?, ?)", (user_id, item_id, update_id, kind, text[:500], _uid(by), now_iso()))

    def notifications(self, user, limit: int = 50) -> list[dict]:
        rows = self.db.q("SELECT n.*, u.name AS by_name, i.board_id, i.name AS item_name, i.parent_id, b.name AS board_name "
                         "FROM notifications n LEFT JOIN users u ON u.id = n.by_user LEFT JOIN items i ON i.id = n.item_id "
                         "LEFT JOIN boards b ON b.id = i.board_id WHERE n.user_id = ? ORDER BY n.id DESC LIMIT ?",
                         (user["id"], limit))
        return [dict(r) for r in rows]

    def unread_count(self, user) -> int:
        return self.db.scalar("SELECT COUNT(*) FROM notifications WHERE user_id = ? AND read_at IS NULL", (user["id"],), 0)

    def mark_read(self, user, ids: list[int] | None = None) -> None:
        with self.db.write() as c:
            if ids:
                c.execute(f"UPDATE notifications SET read_at = ? WHERE user_id = ? AND id IN ({','.join('?' * len(ids))})",
                          (now_iso(), user["id"], *[int(i) for i in ids]))
            else:
                c.execute("UPDATE notifications SET read_at = ? WHERE user_id = ? AND read_at IS NULL", (now_iso(), user["id"]))

    # ==== views ================================================================================ #
    def views(self, bid: int) -> list[dict]:
        out = []
        for r in self.db.q("SELECT * FROM views WHERE board_id = ? AND state = 'active' ORDER BY position, id", (bid,)):
            d = dict(r)
            d["settings"] = loads(d["settings"], {})
            out.append(d)
        return out

    def save_view(self, user, bid: int, name: str, type_: str = "table", settings: dict | None = None,
                  view_id: int | None = None, monday_id: str | None = None) -> int:
        self._need_edit(user)
        self.board(bid)
        if type_ not in ("table", "kanban"):
            raise Invalid("a view is a table or a kanban")
        s = _clean_view_settings(settings or {})
        with self.db.write() as c:
            if view_id:
                if not c.execute("UPDATE views SET name = ?, type = ?, settings = ? WHERE id = ? AND board_id = ?",
                                 (_clean_name(name, "view name"), type_, dumps(s), view_id, bid)).rowcount:
                    raise NotFound("that view no longer exists")
            else:
                pos = (c.execute("SELECT MAX(position) FROM views WHERE board_id = ?", (bid,)).fetchone()[0] or 0) + 1
                view_id = c.execute("INSERT INTO views (board_id, name, type, settings, position, created_by, monday_id) "
                                    "VALUES (?, ?, ?, ?, ?, ?, ?)", (bid, _clean_name(name, "view name"), type_, dumps(s),
                                                                     pos, _uid(user), monday_id)).lastrowid
            self._log(c, bid, None, user, "view.save", new={"name": name, "type": type_}, detail={"view": view_id})
            self._bump(c, bid, struct=True)
        return view_id

    def delete_view(self, user, view_id: int) -> None:
        self._need_edit(user)
        v = self.db.one("SELECT * FROM views WHERE id = ?", (view_id,))
        if not v:
            raise NotFound("that view no longer exists")
        with self.db.write() as c:
            c.execute("UPDATE views SET state = 'deleted' WHERE id = ?", (view_id,))
            self._log(c, v["board_id"], None, user, "view.delete", old={"name": v["name"]})
            self._bump(c, v["board_id"], struct=True)

    # ==== reading a board ======================================================================= #
    def board_payload(self, bid: int, user=None, include_items: bool = True) -> dict:
        """Everything the board screen needs in one go (it filters, sorts and groups in the browser)."""
        b = self.board(bid)
        cols = self.columns(bid)
        groups = [dict(r) for r in self.db.q("SELECT id, title, color, position FROM groups WHERE board_id = ? "
                                             "AND state = 'active' ORDER BY position, id", (bid,))]
        out = {
            "board": {k: b[k] for k in ("id", "name", "description", "state", "workspace_id", "folder_id", "seq",
                                        "struct_seq", "updated_at")},
            "groups": groups,
            "columns": [{k: col[k] for k in ("id", "key", "title", "type", "settings", "width", "position", "scope",
                                             "description")} for col in cols],
            "views": self.views(bid),
            "automations": self.db.scalar("SELECT COUNT(*) FROM automations WHERE board_id = ? AND active = 1", (bid,), 0),
            "can_edit": self.can_edit(user) and b["state"] == "active",
            "favourite": bool(user and bid in self.favourites(user)),
        }
        out["items"] = self._items_payload(bid, cols, "board_id = ? AND state = 'active'", (bid,)) if include_items else []
        return out

    def changes(self, bid: int, since: int) -> dict:
        """What changed on a board after seq `since`: changed items, removed item ids, and whether to reload."""
        b = self.board(bid, allow_deleted=True)
        if b["state"] == "deleted":
            return {"seq": b["seq"], "reload": True}
        if b["struct_seq"] > since:
            return {"seq": b["seq"], "reload": True}
        cols = self.columns(bid)
        rows = self.db.q("SELECT id, state, parent_id FROM items WHERE board_id = ? AND seq > ?", (bid, since))
        live = [r["id"] for r in rows if r["state"] == "active" and not r["parent_id"]]
        gone = [r["id"] for r in rows if r["state"] != "active" and not r["parent_id"]]
        gone += [r[0] for r in self.db.q("SELECT item_id FROM activity WHERE board_id = ? AND action = 'item.left' "
                                         "AND CAST(json_extract(detail, '$.seq') AS INTEGER) > ?", (bid, since))]
        items = []
        if live:
            marks = ",".join("?" * len(live))
            items = self._items_payload(bid, cols, f"id IN ({marks}) AND state = 'active'", tuple(live))
        return {"seq": b["seq"], "reload": False, "items": items, "removed": gone}

    def item_payloads(self, ids: list[int]) -> list[dict]:
        """Fresh payloads of some top-level items (after an edit), with their subitems."""
        if not ids:
            return []
        rows = self.db.q(f"SELECT DISTINCT COALESCE(parent_id, id) AS top, board_id FROM items WHERE id IN "
                         f"({','.join('?' * len(ids))})", tuple(ids))
        out = []
        by_board = defaultdict(list)
        for r in rows:
            by_board[r["board_id"]].append(r["top"])
        for bid, tops in by_board.items():
            cols = self.columns(bid)
            out += self._items_payload(bid, cols, f"id IN ({','.join('?' * len(tops))}) AND state = 'active'", tuple(tops))
        return out

    def _items_payload(self, bid: int, cols: list[dict], where: str, params: tuple) -> list[dict]:
        db = self.db
        tops = [dict(r) for r in db.q(f"SELECT id, group_id, name, position, created_at, created_by, updated_at, updated_by "
                                      f"FROM items WHERE parent_id IS NULL AND {where} ORDER BY position, id", params)]
        if not tops:
            return []
        top_ids = [t["id"] for t in tops]
        subs = []
        for k in range(0, len(top_ids), 500):
            chunk = top_ids[k:k + 500]
            subs += [dict(r) for r in db.q(
                f"SELECT id, parent_id, name, position, created_at, created_by, updated_at, updated_by FROM items "
                f"WHERE parent_id IN ({','.join('?' * len(chunk))}) AND state = 'active' ORDER BY position, id", tuple(chunk))]
        all_ids = top_ids + [s["id"] for s in subs]
        values, counts, files = defaultdict(dict), defaultdict(lambda: [0, 0]), defaultdict(int)
        active_cols = {col["id"] for col in cols}
        for k in range(0, len(all_ids), 900):
            chunk = all_ids[k:k + 900]
            marks = ",".join("?" * len(chunk))
            for r in db.q(f"SELECT item_id, column_id, value FROM item_values WHERE item_id IN ({marks})", tuple(chunk)):
                if r["column_id"] in active_cols:
                    values[r["item_id"]][r["column_id"]] = loads(r["value"])
            for r in db.q(f"SELECT item_id, COUNT(*) AS n, SUM(parent_id IS NULL) AS t FROM updates WHERE item_id IN ({marks}) "
                          f"AND state = 'active' GROUP BY item_id", tuple(chunk)):
                counts[r["item_id"]] = [r["n"], r["t"]]
            for r in db.q(f"SELECT item_id, COUNT(*) AS n FROM files WHERE item_id IN ({marks}) AND state = 'active' "
                          f"GROUP BY item_id", tuple(chunk)):
                files[r["item_id"]] = r["n"]
        computed = self._computed(bid, cols, tops, subs, values)
        sub_by_parent = defaultdict(list)
        for s in subs:
            s["values"] = values.get(s["id"], {})
            s["updates"] = counts[s["id"]][0]
            s["files"] = files.get(s["id"], 0)
            s["computed"] = computed.get(s["id"], {})
            sub_by_parent[s.pop("parent_id")].append(s)
        for t in tops:
            t["values"] = values.get(t["id"], {})
            t["updates"] = counts[t["id"]][0]
            t["files"] = files.get(t["id"], 0)
            t["computed"] = computed.get(t["id"], {})
            t["subitems"] = sub_by_parent.get(t["id"], [])
        return tops

    def _computed(self, bid, cols, tops, subs, values) -> dict:
        """Mirror, formula and subitem-progress values, worked out now (they are never stored)."""
        out = defaultdict(dict)
        item_cols = [c for c in cols if c["scope"] == "item"]
        sub_cols = [c for c in cols if c["scope"] == "subitem"]
        # subitems progress: done / total by the first status column of the subitems
        sub_status = next((c for c in sub_cols if c["type"] == "status"), None)
        for col in item_cols:
            if col["type"] == "subtasks":
                per = defaultdict(lambda: [0, 0])
                for s in subs:
                    per[s["parent_id"]][0] += 1
                    if sub_status and Status().is_done(values.get(s["id"], {}).get(sub_status["id"]), sub_status["settings"]):
                        per[s["parent_id"]][1] += 1
                for t in tops:
                    if t["id"] in per:
                        out[t["id"]][col["id"]] = {"total": per[t["id"]][0], "done": per[t["id"]][1]}
        # formulas
        for scope_cols, rows in ((item_cols, tops), (sub_cols, subs)):
            fcols = [c for c in scope_cols if c["type"] == "formula" and c["settings"].get("formula")]
            if not fcols:
                continue
            key_of = {c["id"]: c["key"] for c in scope_cols}
            for r in rows:
                vals = {key_of[cid]: v for cid, v in values.get(r["id"], {}).items() if cid in key_of}
                for f in fcols:
                    try:
                        res = eval_formula(f["settings"]["formula"], vals)
                        out[r["id"]][f["id"]] = None if res is None else round(res, 6)
                    except FormulaError:
                        out[r["id"]][f["id"]] = {"error": True}
        # links (board_relation names) and mirrors
        rel_cols = [c for c in cols if c["type"] == "board_relation"]
        mirrors = [c for c in cols if c["type"] == "mirror" and c["settings"].get("relation_column")]
        if not rel_cols:
            return out
        linked = set()
        for r in tops + subs:
            for c in rel_cols:
                linked.update(values.get(r["id"], {}).get(c["id"]) or [])
        if not linked:
            return out
        info = {}
        ids = list(linked)
        for k in range(0, len(ids), 900):
            chunk = ids[k:k + 900]
            for r in self.db.q(f"SELECT i.id, i.name, i.board_id, i.state, b.name AS board_name FROM items i JOIN boards b "
                               f"ON b.id = i.board_id WHERE i.id IN ({','.join('?' * len(chunk))})", tuple(chunk)):
                info[r["id"]] = {"name": r["name"], "board_id": r["board_id"], "board": r["board_name"],
                                 "gone": r["state"] != "active"}
        for r in tops + subs:
            for c in rel_cols:
                v = values.get(r["id"], {}).get(c["id"])
                if v:
                    out[r["id"]][c["id"]] = [{"id": i, **info[i]} for i in v if i in info]
        if mirrors:
            self._mirrors(mirrors, tops + subs, values, info, out)
        return out

    def _mirrors(self, mirrors, rows, values, info, out) -> None:
        targets = {}                                            # target column id -> column
        for m in mirrors:
            for b, cid in (m["settings"].get("targets") or {}).items():
                if cid not in targets:
                    r = self.db.one("SELECT * FROM columns WHERE id = ?", (cid,))
                    if r:
                        d = dict(r)
                        d["settings"] = loads(d["settings"], {})
                        targets[cid] = d
        if not targets:
            return
        need = defaultdict(set)                                 # target column -> linked item ids
        for r in rows:
            for m in mirrors:
                for i in values.get(r["id"], {}).get(m["settings"]["relation_column"]) or []:
                    if i in info:
                        cid = (m["settings"].get("targets") or {}).get(str(info[i]["board_id"]))
                        if cid in targets:
                            need[cid].add(i)
        tvals = {}
        ctx = self.ctx()
        for cid, ids in need.items():
            col = targets[cid]
            t = column_type(col["type"])
            ids = list(ids)
            for k in range(0, len(ids), 900):
                chunk = ids[k:k + 900]
                for r in self.db.q(f"SELECT item_id, value, text FROM item_values WHERE column_id = ? AND item_id IN "
                                   f"({','.join('?' * len(chunk))})", (cid, *chunk)):
                    v = loads(r["value"])
                    entry = {"text": t.text(v, col["settings"], ctx) if col["type"] != "board_relation" else r["text"]}
                    if col["type"] == "status":
                        lab = Status.label(col["settings"], v)
                        if lab:
                            entry["color"] = lab["color"]
                    tvals[(cid, r["item_id"])] = entry
        for r in rows:
            for m in mirrors:
                chips = []
                for i in values.get(r["id"], {}).get(m["settings"]["relation_column"]) or []:
                    if i in info:
                        cid = (m["settings"].get("targets") or {}).get(str(info[i]["board_id"]))
                        e = tvals.get((cid, i))
                        if e and e["text"]:
                            chips.append(e)
                if chips:
                    out[r["id"]][m["id"]] = chips

    # ==== activity, search, my work ============================================================== #
    def activity(self, bid: int | None = None, item_id: int | None = None, limit: int = 200,
                 before: int | None = None) -> list[dict]:
        where, params = [], []
        if bid:
            where.append("a.board_id = ?")
            params.append(bid)
        if item_id:
            where.append("a.item_id = ?")
            params.append(item_id)
        if before:
            where.append("a.id < ?")
            params.append(before)
        where.append("a.action != 'item.left'")
        sql = ("SELECT a.*, u.name AS user_name, c.title AS column_title, i.name AS item_name FROM activity a "
               "LEFT JOIN users u ON u.id = a.user_id LEFT JOIN columns c ON c.id = a.column_id "
               "LEFT JOIN items i ON i.id = a.item_id WHERE " + " AND ".join(where) + " ORDER BY a.id DESC LIMIT ?")
        out = []
        for r in self.db.q(sql, (*params, limit)):
            d = dict(r)
            for k in ("old", "new", "detail"):
                d[k] = loads(d[k])
            out.append(d)
        return out

    def search(self, query: str, limit: int = 60, include_archived: bool = False) -> list[dict]:
        q = (query or "").strip()
        if len(q) < 2:
            return []
        like = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        states = "('active','archived')" if include_archived else "('active')"
        rows = self.db.q(
            f"SELECT i.id, i.name, i.board_id, i.parent_id, i.group_id, b.name AS board_name, b.state AS board_state, "
            f"g.title AS group_title, g.color AS group_color, "
            f"(SELECT text FROM item_values iv WHERE iv.item_id = i.id AND iv.text LIKE ? ESCAPE '\\' LIMIT 1) AS hit "
            f"FROM items i JOIN boards b ON b.id = i.board_id LEFT JOIN groups g ON g.id = i.group_id "
            f"WHERE i.state = 'active' AND b.state IN {states} AND (i.name LIKE ? ESCAPE '\\' OR EXISTS "
            f"(SELECT 1 FROM item_values iv WHERE iv.item_id = i.id AND iv.text LIKE ? ESCAPE '\\')) "
            f"ORDER BY b.state = 'archived', (i.name LIKE ? ESCAPE '\\') DESC, b.updated_at DESC, i.updated_at DESC LIMIT ?",
            (like, like, like, like, limit))
        return [dict(r) for r in rows]

    def my_work(self, user, include_done: bool = False) -> list[dict]:
        """Items on active boards where the person is in any people column, with the item's status and date."""
        rows = self.db.q(
            "SELECT DISTINCT i.id FROM item_values iv JOIN columns c ON c.id = iv.column_id AND c.type = 'people' "
            "AND c.state = 'active' JOIN items i ON i.id = iv.item_id AND i.state = 'active' "
            "JOIN boards b ON b.id = i.board_id AND b.state = 'active' "
            "WHERE EXISTS (SELECT 1 FROM json_each(iv.value, '$.users') WHERE json_each.value = ?)", (user["id"],))
        ids = [r[0] for r in rows]
        if not ids:
            return []
        out = []
        cols_cache = {}
        for k in range(0, len(ids), 900):
            chunk = ids[k:k + 900]
            for r in self.db.q(f"SELECT i.*, b.name AS board_name, g.title AS group_title, g.color AS group_color, "
                               f"p.name AS parent_name FROM items i JOIN boards b ON b.id = i.board_id "
                               f"LEFT JOIN groups g ON g.id = COALESCE(i.group_id, (SELECT group_id FROM items WHERE id = i.parent_id)) "
                               f"LEFT JOIN items p ON p.id = i.parent_id WHERE i.id IN ({','.join('?' * len(chunk))})",
                               tuple(chunk)):
                if r["parent_id"]:
                    pr = self.db.one("SELECT state FROM items WHERE id = ?", (r["parent_id"],))
                    if not pr or pr[0] != "active":
                        continue
                key = (r["board_id"], "subitem" if r["parent_id"] else "item")
                if key not in cols_cache:
                    cs = self.columns(r["board_id"], key[1])
                    status = next((c for c in cs if c["type"] == "status"), None)
                    dates = [c for c in cs if c["type"] in ("date", "timeline")]
                    due = next((c for c in dates if re.search(r"due|complete|deadline", c["title"], re.I)), dates[0] if dates else None)
                    cols_cache[key] = (status, due)
                status, due = cols_cache[key]
                vals = {x["column_id"]: loads(x["value"]) for x in
                        self.db.q("SELECT column_id, value FROM item_values WHERE item_id = ?", (r["id"],))}
                sv = vals.get(status["id"]) if status else None
                lab = Status.label(status["settings"], sv) if status and sv is not None else None
                if lab and lab.get("done") and not include_done:
                    continue
                dv = vals.get(due["id"]) if due else None
                date = (dv.get("to") if isinstance(dv, dict) else dv) if dv else None
                out.append({"id": r["id"], "name": r["name"], "board_id": r["board_id"], "board_name": r["board_name"],
                            "group_title": r["group_title"], "group_color": r["group_color"], "parent_name": r["parent_name"],
                            "parent_id": r["parent_id"],
                            "status": {"name": lab["name"], "color": lab["color"]} if lab else None,
                            "status_column": status["title"] if status else None,
                            "date": (date or "")[:10] or None, "date_column": due["title"] if due else None,
                            "updated_at": r["updated_at"]})
        out.sort(key=lambda x: (x["date"] is None, x["date"] or "", x["board_name"], x["name"]))
        return out


# ---- helpers for copies ----------------------------------------------------------------------- #
def _clean_view_settings(s: dict) -> dict:
    out = {}
    f = s.get("filters") or {}
    filters = {}
    if isinstance(f.get("text"), str) and f["text"].strip():
        filters["text"] = f["text"].strip()[:200]
    if f.get("people"):
        filters["people"] = [int(u) for u in f["people"] if str(u).lstrip("-").isdigit()][:50]
        if f.get("people_columns"):
            filters["people_columns"] = [int(c) for c in f["people_columns"] if str(c).isdigit()][:50]
    rules = []
    for r in f.get("rules") or []:
        try:
            rules.append({"column": int(r["column"]), "values": [v for v in (r.get("values") or [])][:100],
                          "op": r.get("op") if r.get("op") in ("any_of", "not_any_of", "is_empty", "is_not_empty") else "any_of"})
        except (KeyError, TypeError, ValueError):
            continue
    if rules:
        filters["rules"] = rules[:20]
    if f.get("groups"):
        filters["groups"] = [int(g) for g in f["groups"] if str(g).isdigit()][:100]
    if filters:
        out["filters"] = filters
    sort = []
    for srt in s.get("sort") or []:
        col = srt.get("column")
        if col == "name" or str(col).isdigit():
            sort.append({"column": col if col == "name" else int(col), "dir": "desc" if srt.get("dir") == "desc" else "asc"})
    if sort:
        out["sort"] = sort[:3]
    if s.get("hidden"):
        out["hidden"] = [int(c) for c in s["hidden"] if str(c).isdigit()]
    if s.get("order"):
        out["order"] = [int(c) for c in s["order"] if str(c).isdigit()]
    for k in ("kanban_column", "group_by"):
        if s.get(k) and str(s[k]).isdigit():
            out[k] = int(s[k])
    if s.get("hide_empty_groups"):
        out["hide_empty_groups"] = True
    return out


def _remap_view(s: dict, cmap: dict) -> dict:
    s = dict(s)
    f = dict(s.get("filters") or {})
    if f.get("rules"):
        f["rules"] = [{**r, "column": cmap.get(r["column"], r["column"])} for r in f["rules"] if r["column"] in cmap]
    if f.get("people_columns"):
        f["people_columns"] = [cmap[c] for c in f["people_columns"] if c in cmap]
    f.pop("groups", None)
    if f:
        s["filters"] = f
    if s.get("sort"):
        s["sort"] = [{**x, "column": x["column"] if x["column"] == "name" else cmap.get(x["column"])} for x in s["sort"]
                     if x["column"] == "name" or x["column"] in cmap]
    for k in ("hidden", "order"):
        if s.get(k):
            s[k] = [cmap[c] for c in s[k] if c in cmap]
    for k in ("kanban_column", "group_by"):
        if s.get(k):
            s[k] = cmap.get(s[k])
    return s


def _remap_rule(r: dict, cmap: dict, gmap: dict) -> dict | None:
    r = dict(r)
    for k in ("column", "date_column", "people_column"):
        if k in r:
            if r[k] not in cmap:
                return None
            r[k] = cmap[r[k]]
    if "group" in r:
        if r["group"] not in gmap:
            return None
        r["group"] = gmap[r["group"]]
    return r


def new_label_id(labels: list[dict]) -> int:
    return max([lab["id"] for lab in labels] + [-1]) + 1


def palette() -> list[dict]:
    return [{"color": c, "name": n} for c, n in PALETTE]


def token() -> str:
    return secrets.token_urlsafe(24)
