"""The JSON API the board screen talks to.  Every change goes through service.py; errors come back as {"error": "..."}."""
from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import re
import uuid

from flask import Blueprint, current_app, g, jsonify, request

from ..db import now_iso
from ..service import Invalid, NotFound, initials
from .pages import describe_activity

bp = Blueprint("api", __name__, url_prefix="/api")


def svc():
    return current_app.config["FW_SVC"]


def body() -> dict:
    d = request.get_json(silent=True)
    if d is None and request.method in ("POST", "PATCH", "PUT") and request.mimetype == "application/json":
        raise Invalid("the request was not readable - reload the page")
    return d or {}


def _int(v, what="number"):
    try:
        return int(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        raise Invalid(f"bad {what}") from None


def people() -> dict:
    us = svc().users(include_inactive=True)
    return {"users": [{"id": u["id"], "name": u["name"], "email": u["email"], "initials": initials(u["name"]),
                       "color": u["color"], "active": bool(u["active"])} for u in us],
            "teams": svc().teams()}


def items_reply(ids) -> dict:
    return {"items": svc().item_payloads(list(dict.fromkeys(i for i in ids if i)))}


# ---- boards -------------------------------------------------------------------------------------------- #
@bp.get("/boards")
def boards():
    ws = {w["id"]: w["name"] for w in svc().workspaces()}
    return jsonify(boards=[{"id": b["id"], "name": b["name"], "workspace": ws.get(b["workspace_id"], ""),
                            "workspace_id": b["workspace_id"], "folder_id": b["folder_id"]}
                           for b in svc().boards(states=("active",))])


@bp.get("/workspaces")
def workspaces():
    return jsonify(workspaces=[{"id": w["id"], "name": w["name"]} for w in svc().workspaces()],
                   folders=[{"id": f["id"], "name": f["name"], "workspace_id": f["workspace_id"]} for f in svc().folders()])


@bp.get("/boards/<int:bid>")
def board(bid):
    p = svc().board_payload(bid, g.user)
    p.update(people())
    p["me"] = g.user["id"]
    return jsonify(p)


@bp.get("/boards/<int:bid>/changes")
def changes(bid):
    return jsonify(svc().changes(bid, request.args.get("since", type=int) or 0))


@bp.post("/boards")
def board_create():
    d = body()
    bid = svc().create_board(g.user, d.get("name") or "New board", _int(d.get("workspace_id"), "workspace"),
                             _int(d.get("folder_id"), "folder"))
    return jsonify(id=bid)


@bp.patch("/boards/<int:bid>")
def board_update(bid):
    d = body()
    kw = {}
    if "name" in d:
        kw["name"] = d["name"]
    if "description" in d:
        kw["description"] = d["description"]
    if "workspace_id" in d:
        kw["workspace_id"] = _int(d["workspace_id"], "workspace")
    if "folder_id" in d:
        kw["folder_id"] = _int(d["folder_id"], "folder")
    svc().update_board(g.user, bid, **kw)
    return jsonify(ok=True)


@bp.post("/boards/<int:bid>/state")
def board_state(bid):
    svc().set_board_state(g.user, bid, body().get("state"))
    return jsonify(ok=True)


@bp.post("/boards/<int:bid>/duplicate")
def board_duplicate(bid):
    d = body()
    nb = svc().duplicate_board(g.user, bid, d.get("name") or "Copy", d.get("mode") or "structure",
                               _int(d.get("workspace_id"), "workspace"), _int(d.get("folder_id"), "folder"))
    return jsonify(id=nb)


@bp.post("/boards/<int:bid>/favourite")
def board_favourite(bid):
    svc().board(bid)
    svc().favourite(g.user, bid, bool(body().get("on")))
    return jsonify(ok=True)


# ---- groups ------------------------------------------------------------------------------------------- #
@bp.post("/boards/<int:bid>/groups")
def group_create(bid):
    d = body()
    return jsonify(id=svc().add_group(g.user, bid, d.get("title") or "New group", d.get("color"), d.get("where") or "top"))


@bp.patch("/groups/<int:gid>")
def group_update(gid):
    d = body()
    svc().update_group(g.user, gid, title=d.get("title"), color=d.get("color"), before=_int(d.get("before")),
                       after=_int(d.get("after")))
    return jsonify(ok=True)


@bp.delete("/groups/<int:gid>")
def group_delete(gid):
    svc().delete_group(g.user, gid, archive=request.args.get("archive") == "1")
    return jsonify(ok=True)


@bp.post("/groups/<int:gid>/restore")
def group_restore(gid):
    svc().restore_group(g.user, gid)
    return jsonify(ok=True)


# ---- columns ------------------------------------------------------------------------------------------ #
@bp.post("/boards/<int:bid>/columns")
def column_create(bid):
    d = body()
    cid = svc().add_column(g.user, bid, d.get("type") or "text", d.get("title") or "New column", d.get("settings"),
                           d.get("scope") or "item", _int(d.get("after")))
    return jsonify(id=cid)


@bp.patch("/columns/<int:cid>")
def column_update(cid):
    d = body()
    svc().update_column(g.user, cid, title=d.get("title"), settings=d.get("settings"), width=_int(d.get("width")),
                        description=d.get("description"), before=_int(d.get("before")), after=_int(d.get("after")))
    return jsonify(ok=True)


@bp.delete("/columns/<int:cid>")
def column_delete(cid):
    svc().delete_column(g.user, cid)
    return jsonify(ok=True)


@bp.get("/boards/<int:bid>/columns/<int:cid>/link-search")
def link_search(bid, cid):
    """Items that can be linked in a Connect boards column: on its boards, matching the search."""
    col = svc().column(cid)
    if col["board_id"] != bid or col["type"] != "board_relation":
        raise Invalid("that is not a Connect boards column")
    boards_ = col["settings"].get("board_ids") or []
    if not boards_:
        return jsonify(items=[], note="This column is not connected to a board yet - choose one in the column's settings.")
    q = (request.args.get("q") or "").strip()
    like = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    marks = ",".join("?" * len(boards_))
    rows = svc().db.q(f"SELECT i.id, i.name, b.name AS board, g.title AS grp FROM items i JOIN boards b ON b.id = i.board_id "
                      f"LEFT JOIN groups g ON g.id = i.group_id WHERE i.board_id IN ({marks}) AND i.state = 'active' "
                      f"AND i.parent_id IS NULL AND i.name LIKE ? ESCAPE '\\' ORDER BY i.name COLLATE NOCASE LIMIT 40",
                      (*boards_, like))
    return jsonify(items=[dict(r) for r in rows])


# ---- items -------------------------------------------------------------------------------------------- #
@bp.post("/boards/<int:bid>/items")
def item_create(bid):
    d = body()
    iid = svc().create_item(g.user, bid, d.get("name") or "New item", _int(d.get("group_id"), "group"),
                            values=d.get("values") or None, parent_id=_int(d.get("parent_id"), "item"),
                            where=d.get("where") or "bottom")
    return jsonify(id=iid, **items_reply([iid]))


@bp.patch("/items/<int:iid>")
def item_update(iid):
    d = body()
    if "name" in d:
        svc().rename_item(g.user, iid, d["name"])
    return jsonify(items_reply([iid]))


@bp.post("/items/<int:iid>/values")
def item_value(iid):
    d = body()
    changed = svc().set_value(g.user, iid, _int(d.get("column"), "column"), d.get("value"))
    return jsonify(items_reply([iid] + changed))


@bp.post("/items/<int:iid>/move")
def item_move(iid):
    d = body()
    svc().move_item(g.user, iid, group_id=_int(d.get("group_id"), "group"), before=_int(d.get("before")),
                    after=_int(d.get("after")))
    return jsonify(items_reply([iid]))


@bp.post("/items/<int:iid>/move-board")
def item_move_board(iid):
    d = body()
    svc().move_item_to_board(g.user, iid, _int(d.get("board_id"), "board"), _int(d.get("group_id"), "group"))
    return jsonify(ok=True)


@bp.post("/items/<int:iid>/duplicate")
def item_duplicate(iid):
    new = svc().duplicate_item(g.user, iid, with_updates=bool(body().get("with_updates")))
    return jsonify(id=new, **items_reply([new]))


@bp.post("/items/<int:iid>/state")
def item_state(iid):
    svc().set_item_state(g.user, iid, body().get("state"))
    return jsonify(ok=True)


@bp.get("/items/<int:iid>")
def item_detail(iid):
    s = svc()
    it = s.item(iid, allow_inactive=True)
    acts = s.activity(item_id=iid, limit=100)
    for a in acts:
        a["text"] = describe_activity(a)
    files = [dict(r) for r in s.db.q("SELECT id, name, size, link, sha256 IS NOT NULL AS stored, update_id, column_id, "
                                     "created_at FROM files WHERE item_id = ? AND state = 'active' ORDER BY id DESC", (iid,))]
    payload = s.item_payloads([iid])
    return jsonify(item=payload[0] if payload else None, state=it["state"], id=iid, parent_id=it["parent_id"],
                   board_id=it["board_id"], updates=s.updates(iid), activity=acts, files=files)


@bp.post("/items/<int:iid>/updates")
def update_create(iid):
    d = body()
    svc().add_update(g.user, iid, d.get("body") or "", parent_id=_int(d.get("parent_id"), "update"))
    return jsonify(updates=svc().updates(iid), **items_reply([iid]))


@bp.patch("/updates/<int:uid>")
def update_edit(uid):
    svc().edit_update(g.user, uid, body().get("body") or "")
    r = svc().db.one("SELECT item_id FROM updates WHERE id = ?", (uid,))
    return jsonify(updates=svc().updates(r[0]))


@bp.delete("/updates/<int:uid>")
def update_delete(uid):
    r = svc().db.one("SELECT item_id FROM updates WHERE id = ?", (uid,))
    if not r:
        raise NotFound("that update no longer exists")
    svc().delete_update(g.user, uid)
    return jsonify(updates=svc().updates(r[0]), **items_reply([r[0]]))


# ---- files ---------------------------------------------------------------------------------------------- #
@bp.post("/items/<int:iid>/files")
def file_upload(iid):
    s = svc()
    s._need_edit(g.user)
    it = s.item(iid)
    f = request.files.get("file")
    if not f or not f.filename:
        raise Invalid("choose a file")
    name = re.sub(r"[\x00-\x1f/\\]+", "_", os.path.basename(f.filename))[:200] or "file"
    files_dir = current_app.config["FW_CONFIG"].files_dir
    files_dir.mkdir(parents=True, exist_ok=True)
    tmp = files_dir / f".upload-{uuid.uuid4().hex}"
    h, size = hashlib.sha256(), 0
    with open(tmp, "wb") as out:
        while True:
            chunk = f.stream.read(1024 * 1024)
            if not chunk:
                break
            h.update(chunk)
            size += len(chunk)
            out.write(chunk)
    sha = h.hexdigest()
    dest = files_dir / sha[:2] / sha
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        tmp.unlink()
    else:
        os.replace(tmp, dest)
    col = _int(request.form.get("column"), "column")
    update_id = _int(request.form.get("update"), "update")
    ctype = mimetypes.guess_type(name)[0] or f.mimetype or "application/octet-stream"
    with s.db.write() as c:
        fid = c.execute("INSERT INTO files (item_id, update_id, column_id, name, size, content_type, sha256, uploaded_by, "
                        "created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (iid, update_id, col, name, size, ctype, sha, g.user["id"], now_iso())).lastrowid
        seq = s._bump(c, it["board_id"])
        s._touch(c, iid, seq, g.user)
        s._log(c, it["board_id"], iid, g.user, "file.add", new={"name": name}, detail={"file": fid})
    if col:
        cur = s.db.scalar("SELECT value FROM item_values WHERE item_id = ? AND column_id = ?", (iid, col))
        ids = (json.loads(cur) if cur else []) + [fid]
        s.set_value(g.user, iid, col, ids)
    return jsonify(file={"id": fid, "name": name, "size": size}, **items_reply([iid]))


@bp.delete("/files/<int:fid>")
def file_delete(fid):
    s = svc()
    s._need_edit(g.user)
    f = s.db.one("SELECT f.*, i.board_id FROM files f JOIN items i ON i.id = f.item_id WHERE f.id = ?", (fid,))
    if not f:
        raise NotFound("that file no longer exists")
    with s.db.write() as c:
        c.execute("UPDATE files SET state = 'deleted' WHERE id = ?", (fid,))
        seq = s._bump(c, f["board_id"])
        s._touch(c, f["item_id"], seq, g.user)
        s._log(c, f["board_id"], f["item_id"], g.user, "file.delete", old={"name": f["name"]}, detail={"file": fid})
    if f["column_id"]:
        cur = s.db.scalar("SELECT value FROM item_values WHERE item_id = ? AND column_id = ?", (f["item_id"], f["column_id"]))
        if cur:
            ids = [x for x in json.loads(cur) if x != fid]
            s.set_value(g.user, f["item_id"], f["column_id"], ids or None)
    return jsonify(ok=True, **items_reply([f["item_id"]]))


# ---- views ---------------------------------------------------------------------------------------------- #
@bp.post("/boards/<int:bid>/views")
def view_create(bid):
    d = body()
    vid = svc().save_view(g.user, bid, d.get("name") or "View", d.get("type") or "table", d.get("settings") or {})
    return jsonify(id=vid, views=svc().views(bid))


@bp.patch("/views/<int:vid>")
def view_update(vid):
    d = body()
    v = svc().db.one("SELECT board_id, name, type FROM views WHERE id = ?", (vid,))
    if not v:
        raise NotFound("that view no longer exists")
    svc().save_view(g.user, v["board_id"], d.get("name") or v["name"], d.get("type") or v["type"], d.get("settings") or {},
                    view_id=vid)
    return jsonify(views=svc().views(v["board_id"]))


@bp.delete("/views/<int:vid>")
def view_delete(vid):
    svc().delete_view(g.user, vid)
    return jsonify(ok=True)


# ---- people, notifications, search ------------------------------------------------------------------------ #
@bp.get("/people")
def people_list():
    return jsonify(people())


@bp.get("/notifications")
def notifications():
    return jsonify(notifications=svc().notifications(g.user, limit=40), unread=svc().unread_count(g.user))


@bp.post("/notifications/read")
def notifications_read():
    ids = [_int(i) for i in body().get("ids") or []]
    svc().mark_read(g.user, [i for i in ids if i] or None)
    return jsonify(unread=svc().unread_count(g.user))


@bp.get("/search")
def search():
    return jsonify(results=svc().search(request.args.get("q") or "", limit=20))


@bp.get("/boards/<int:bid>/groups")
def board_groups(bid):
    rows = svc().db.q("SELECT id, title, color FROM groups WHERE board_id = ? AND state = 'active' ORDER BY position", (bid,))
    return jsonify(groups=[dict(r) for r in rows])

