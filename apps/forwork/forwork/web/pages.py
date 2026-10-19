"""The screens: signing in, home, boards, My work, the inbox, search, a board's activity and automations."""
from __future__ import annotations

import datetime as dt
import mimetypes
import re
import tempfile
from collections import defaultdict
from pathlib import Path

from flask import (Blueprint, abort, current_app, flash, g, jsonify, make_response, redirect, render_template, request,
                   send_file, session, url_for)

from .. import automations as autos
from ..auth import MicrosoftSignIn, SignInError, create_session, end_session, user_for_identity
from ..model import Status, creatable_types
from . import LOOPBACK, cookie_name

bp = Blueprint("ui", __name__)


def svc():
    return current_app.config["FW_SVC"]


def cfg():
    return current_app.config["FW_CONFIG"]


def safe_next(nxt: str | None) -> str:
    """Only send people back to a page on this site (never //other-site, /\\other-site or a scheme)."""
    from urllib.parse import urlsplit
    if (nxt and nxt.startswith("/") and not nxt.startswith("//") and not re.search(r"[\\\x00-\x20\x7f]", nxt)
            and not urlsplit(nxt).netloc and not urlsplit(nxt).scheme):
        return nxt
    return url_for("ui.home")


def _set_cookie(resp, token: str) -> None:
    resp.set_cookie(cookie_name(cfg()), token, httponly=True, secure=cfg().secure_cookies, samesite="Lax",
                    path="/", max_age=7 * 24 * 3600)


def sidebar() -> dict:
    """Workspaces, their folders and boards, and the person's favourites - for the left-hand menu."""
    s = svc()
    boards = s.boards(states=("active",))
    favs = s.favourites(g.user) if g.user else set()
    by_ws = defaultdict(lambda: {"folders": defaultdict(list), "boards": []})
    for b in boards:
        (by_ws[b["workspace_id"]]["folders"][b["folder_id"]] if b["folder_id"] else by_ws[b["workspace_id"]]["boards"]).append(b)
    folders = {f["id"]: f for f in s.folders()}
    out = []
    for w in s.workspaces():
        entry = by_ws.get(w["id"], {"folders": {}, "boards": []})
        out.append({"ws": w, "boards": entry["boards"],
                    "folders": [{"folder": folders[fid], "boards": bs} for fid, bs in entry["folders"].items() if fid in folders]
                    + [{"folder": f, "boards": []} for f in folders.values() if f["workspace_id"] == w["id"]
                       and f["id"] not in entry["folders"]]})
    return {"workspaces": out, "favourites": [b for b in boards if b["id"] in favs],
            "archived_count": svc().db.scalar("SELECT COUNT(*) FROM boards WHERE state = 'archived'", default=0)}


@bp.app_context_processor
def _nav():
    return {"nav": sidebar() if g.get("user") else None}


# ---- signing in ------------------------------------------------------------------------------------ #
@bp.get("/health")
def health():
    return jsonify(ok=True)


@bp.get("/signin")
def signin():
    if g.user:
        return redirect(safe_next(request.args.get("next")))
    dev_users = []
    if cfg().dev_login and request.remote_addr in LOOPBACK:
        dev_users = svc().users()
    return render_template("signin.html", next=safe_next(request.args.get("next")), microsoft=cfg().microsoft_ready,
                           dev_users=dev_users, error=request.args.get("error"))


@bp.get("/auth/microsoft")
def auth_start():
    try:
        url, pending = MicrosoftSignIn(cfg()).start(request.host_url, login_hint=request.cookies.get("forwork_hint", ""))
    except SignInError as e:
        return redirect(url_for("ui.signin", error=str(e)))
    session.clear()
    session.permanent = True
    session["pending"] = pending
    session["next"] = safe_next(request.args.get("next"))
    return redirect(url)


@bp.get("/auth/callback")
def auth_callback():
    pending = session.pop("pending", None)
    nxt = session.pop("next", None)
    try:
        ident = MicrosoftSignIn(cfg()).finish(request.args.to_dict(), pending)
        user = user_for_identity(svc(), cfg(), ident)
    except SignInError as e:
        return redirect(url_for("ui.signin", error=str(e)))
    token, _ = create_session(current_app.config["FW_DB"], user["id"], request.remote_addr or "", request.user_agent.string)
    resp = redirect(safe_next(nxt))
    _set_cookie(resp, token)
    resp.set_cookie("forwork_hint", user["email"], httponly=True, secure=cfg().secure_cookies, samesite="Lax",
                    max_age=180 * 24 * 3600)
    return resp


@bp.post("/auth/dev")
def dev_signin():
    """Sign in as anyone - only with FORWORK_DEV_LOGIN=1 and only from this PC."""
    if not cfg().dev_login or request.remote_addr not in LOOPBACK or (request.host or "").rsplit(":", 1)[0] not in LOOPBACK:
        abort(404)
    u = svc().user(int(request.form.get("user") or 0))
    if not u or not u["active"]:
        abort(400, "pick someone from the list")
    token, _ = create_session(current_app.config["FW_DB"], u["id"], request.remote_addr or "", request.user_agent.string)
    resp = redirect(safe_next(request.form.get("next")))
    _set_cookie(resp, token)
    return resp


@bp.post("/signout")
def signout():
    end_session(current_app.config["FW_DB"], request.cookies.get(cookie_name(cfg())))
    resp = redirect(url_for("ui.signin"))
    resp.delete_cookie(cookie_name(cfg()), path="/")
    return resp


# ---- home, my work, inbox, search ------------------------------------------------------------------- #
@bp.get("/")
def home():
    s = svc()
    work = s.my_work(g.user)
    today = _sydney_now().date().isoformat()
    week = (_sydney_now().date() + dt.timedelta(days=7)).isoformat()
    counts = {"overdue": sum(1 for w in work if w["date"] and w["date"] < today),
              "today": sum(1 for w in work if w["date"] == today),
              "week": sum(1 for w in work if w["date"] and today < w["date"] <= week),
              "all": len(work)}
    recent = s.db.q("SELECT b.id, b.name, b.updated_at, w.name AS ws FROM boards b JOIN workspaces w ON w.id = b.workspace_id "
                    "WHERE b.state = 'active' ORDER BY b.updated_at DESC LIMIT 8")
    hour = _sydney_now().hour
    greeting = "Good morning" if hour < 12 else "Good afternoon" if hour < 18 else "Good evening"
    return render_template("home.html", counts=counts, work=work[:12], recent=recent, greeting=greeting,
                           notifications=s.notifications(g.user, limit=8), today=today)


def _sydney_now() -> dt.datetime:
    try:
        from zoneinfo import ZoneInfo
        return dt.datetime.now(ZoneInfo("Australia/Sydney"))
    except Exception:
        return dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=10)


@bp.get("/my-work")
def my_work():
    s = svc()
    include_done = request.args.get("done") == "1"
    work = s.my_work(g.user, include_done=include_done)
    today = _sydney_now().date()
    buckets = [("Overdue", []), ("Today", []), ("This week", []), ("Next week", []), ("Later", []), ("No date", [])]
    for w in work:
        d = dt.date.fromisoformat(w["date"]) if w["date"] else None
        i = (5 if d is None else 0 if d < today else 1 if d == today else 2 if (d - today).days <= 7
             else 3 if (d - today).days <= 14 else 4)
        buckets[i][1].append(w)
    return render_template("my_work.html", buckets=buckets, total=len(work), include_done=include_done,
                           today=today.isoformat())


@bp.get("/inbox")
def inbox():
    s = svc()
    items = s.notifications(g.user, limit=200)
    return render_template("inbox.html", notifications=items)


@bp.post("/inbox/read")
def inbox_read():
    svc().mark_read(g.user)
    return redirect(url_for("ui.inbox"))


@bp.get("/search")
def search():
    q = (request.args.get("q") or "").strip()
    results = svc().search(q, limit=100, include_archived=request.args.get("archived") == "1") if q else []
    boards = [b for b in svc().boards(states=("active", "archived")) if q and q.lower() in b["name"].lower()]
    return render_template("search.html", q=q, results=results, boards=boards,
                           archived=request.args.get("archived") == "1")


@bp.get("/archive")
def archive():
    return render_template("archive.html", boards=svc().boards(states=("archived",)),
                           workspaces={w["id"]: w["name"] for w in svc().workspaces()})


# ---- boards ------------------------------------------------------------------------------------------ #
@bp.get("/boards/<int:bid>")
def board(bid):
    b = svc().board(bid)
    ws = svc().db.one("SELECT name FROM workspaces WHERE id = ?", (b["workspace_id"],))
    return render_template("board.html", board=b, workspace=ws[0] if ws else "", column_types=creatable_types(),
                           view=request.args.get("view", ""), open_item=request.args.get("item", ""))


@bp.get("/items/<int:iid>")
def item_link(iid):
    """A link to an item (from search, notifications, emails): opens its board with the item showing."""
    it = svc().item(iid, allow_inactive=True)
    top = it["parent_id"] or it["id"]
    return redirect(url_for("ui.board", bid=it["board_id"], item=top))


@bp.post("/boards/new")
def board_new():
    s = svc()
    ws = int(request.form.get("workspace") or 0) or s.workspaces()[0]["id"]
    folder = int(request.form.get("folder") or 0) or None
    bid = s.create_board(g.user, request.form.get("name") or "New board", ws, folder)
    return redirect(url_for("ui.board", bid=bid))


@bp.post("/workspaces/new")
def workspace_new():
    svc().create_workspace(g.user, request.form.get("name") or "New workspace")
    flash("Workspace added.", "ok")
    return redirect(request.referrer or url_for("ui.home"))


@bp.post("/folders/new")
def folder_new():
    svc().create_folder(g.user, int(request.form.get("workspace") or 0), request.form.get("name") or "New folder")
    flash("Folder added.", "ok")
    return redirect(request.referrer or url_for("ui.home"))


@bp.get("/boards/<int:bid>/activity")
def board_activity(bid):
    b = svc().board(bid)
    before = request.args.get("before", type=int)
    rows = svc().activity(bid=bid, limit=200, before=before)
    return render_template("activity.html", board=b, rows=rows, describe=describe_activity,
                           more=rows[-1]["id"] if len(rows) == 200 else None)


@bp.get("/boards/<int:bid>/automations")
def board_automations(bid):
    s = svc()
    b = s.board(bid)
    cols = s.columns(bid, "item")
    groups = s.db.q("SELECT id, title, color FROM groups WHERE board_id = ? AND state = 'active' ORDER BY position", (bid,))
    return render_template("automations.html", board=b, rules=autos.list_rules(s, bid), columns=cols, groups=groups,
                           users=s.users(), status_cols=[c for c in cols if c["type"] == "status"])


@bp.post("/boards/<int:bid>/automations")
def board_automations_save(bid):
    s = svc()
    f = request.form
    trigger = {"type": f.get("trigger"), "column": f.get("t_column"), "to": f.get("t_to")}
    action = {"type": f.get("action"), "group": f.get("a_group"), "column": f.get(f"a_column_{f.get('action')}"),
              "label": f.get("a_label"), "users": f.getlist("a_users"), "text": f.get("a_text")}
    if trigger["type"] == "status_change" and f.get("t_column"):
        trigger["column"] = f.get("t_column")
    autos.save_rule(s, g.user, bid, trigger, action)
    flash("Automation added.", "ok")
    return redirect(url_for("ui.board_automations", bid=bid))


@bp.post("/automations/<int:rid>")
def automation_change(rid):
    s = svc()
    r = s.db.one("SELECT board_id FROM automations WHERE id = ?", (rid,))
    if not r:
        abort(404)
    op = request.form.get("op")
    if op == "delete":
        autos.delete_rule(s, g.user, rid)
        flash("Automation deleted.", "ok")
    else:
        autos.set_rule_active(s, g.user, rid, op == "on")
    return redirect(url_for("ui.board_automations", bid=r[0]))


@bp.get("/boards/<int:bid>/export.xlsx")
def board_export(bid):
    from ..export import board_xlsx
    b = svc().board(bid)
    tmp = Path(tempfile.mkdtemp(prefix="forwork-")) / "board.xlsx"
    board_xlsx(svc(), bid, tmp)
    name = re.sub(r"[^\w\- ]+", "", b["name"]).strip() or "board"
    return send_file(tmp, as_attachment=True, download_name=f"{name}.xlsx",
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


# ---- files ------------------------------------------------------------------------------------------- #
INLINE_TYPES = {"image/png", "image/jpeg", "image/gif", "application/pdf"}


@bp.get("/files/<int:fid>")
def file_download(fid):
    f = svc().db.one("SELECT * FROM files WHERE id = ? AND state = 'active'", (fid,))
    if not f:
        abort(404)
    if f["link"]:
        return redirect(f["link"])
    if not f["sha256"]:
        return render_template("error.html", code=404,
                               message=f"{f['name']} was uploaded to monday.com and is still there - ForWork only lists it."), 404
    path = cfg().files_dir / f["sha256"][:2] / f["sha256"]
    if not path.is_file():
        abort(404)
    ctype = f["content_type"] or mimetypes.guess_type(f["name"])[0] or "application/octet-stream"
    inline = ctype in INLINE_TYPES and request.args.get("download") != "1"
    resp = make_response(send_file(path, mimetype=ctype if inline else "application/octet-stream",
                                   as_attachment=not inline, download_name=f["name"]))
    resp.headers["Content-Security-Policy"] = "default-src 'none'; img-src 'self'; style-src 'unsafe-inline'; sandbox"
    resp.headers["Cache-Control"] = "private, max-age=3600"
    return resp


# ---- describing the activity log ---------------------------------------------------------------------- #
def describe_activity(a: dict) -> str:
    act, old, new, d = a["action"], a["old"], a["new"], a["detail"] or {}
    col = a.get("column_title") or "a column"
    if act == "item.value":
        o, n = (old or {}).get("text") or "", (new or {}).get("text") or ""
        if o and n:
            return f"changed {col} from “{o}” to “{n}”"
        return f"set {col} to “{n}”" if n else f"cleared {col} (was “{o}”)"
    simple = {
        "item.create": "created the item", "subitem.create": "added a subitem", "item.duplicate": "duplicated an item",
        "item.archive": "archived the item", "item.delete": "deleted the item", "item.restore": "restored the item",
        "update.create": "wrote an update", "update.edit": "edited an update", "update.delete": "deleted an update",
        "board.create": "created the board", "board.update": "changed the board's details", "board.import": "imported the board from monday.com",
        "board.archive": "archived the board", "board.restore": "restored the board", "board.delete": "deleted the board",
        "board.duplicate": "made this board as a copy", "group.create": f"added the group “{(new or {}).get('title', '')}”",
        "group.update": "changed a group", "group.delete": f"deleted the group “{(old or {}).get('title', '')}”",
        "group.archive": f"archived the group “{(old or {}).get('title', '')}”", "group.restore": "restored a group",
        "column.create": f"added the column “{(new or {}).get('title', '')}”", "column.update": f"changed the column {col}",
        "column.delete": f"deleted the column “{(old or {}).get('title', '')}”", "column.restore": "restored a column",
        "view.save": f"saved the view “{(new or {}).get('name', '')}”", "view.delete": "deleted a view",
        "automation.save": "saved an automation", "automation.delete": "deleted an automation",
        "automation.on": "turned an automation on", "automation.off": "turned an automation off",
    }
    if act == "item.rename":
        return f"renamed “{old}” to “{new}”"
    if act == "item.move":
        return f"moved the item from {(old or {}).get('title') or 'a group'} to {(new or {}).get('title') or 'a group'}"
    if act == "item.move_board":
        return f"moved the item to {(new or {}).get('title') or 'another board'}"
    return simple.get(act, act.replace(".", " ")) + (" (automation)" if d.get("automation") else "")


def labels_for(col: dict) -> list[dict]:
    return [lab for lab in col["settings"].get("labels") or [] if not lab.get("hidden")]


bp.add_app_template_global(labels_for, "labels_for")
bp.add_app_template_global(Status.label, "status_label")
