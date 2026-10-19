"""
Board automations - "when this happens, do that", like monday.com's.

The firm's boards rely mostly on one kind: when the Status changes to a label, move the item to a group
(e.g. "Waiting for Review" -> the Review group) and sometimes stamp today's date in a column.  Those come
across from monday.com as they are, and anyone can add more on the board's Automations page.

  trigger  {"type": "status_change", "column": <status column id>, "to": <label id or null = any label>}
           {"type": "item_created"}
           {"type": "column_change", "column": <column id>}
  action   {"type": "move_to_group", "group": <group id>}
           {"type": "set_date_today", "column": <date column id>}
           {"type": "set_status", "column": <status column id>, "label": <label id>}
           {"type": "assign_creator", "column": <people column id>}
           {"type": "clear_value", "column": <column id>}
           {"type": "notify", "column": <people column id> | "users": [user ids], "text": "..."}
           {"type": "archive_item"}

Automations run inside the same transaction as the change that set them off, as "Automation" in the
activity log, and can set off other automations up to three levels deep (so two rules cannot loop forever).
"""
from __future__ import annotations

import datetime as dt

from .db import dumps, loads, now_iso
from .model import Status

TRIGGERS = ("status_change", "item_created", "column_change")
ACTIONS = ("move_to_group", "set_date_today", "set_status", "assign_creator", "clear_value", "notify", "archive_item")


def today_sydney() -> str:
    try:
        from zoneinfo import ZoneInfo
        return dt.datetime.now(ZoneInfo("Australia/Sydney")).date().isoformat()
    except Exception:
        return (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=10)).date().isoformat()


def _rules(c, board_id: int) -> list[dict]:
    out = []
    for r in c.execute("SELECT * FROM automations WHERE board_id = ? AND active = 1 ORDER BY id", (board_id,)).fetchall():
        out.append({"id": r["id"], "trigger": loads(r["trigger"], {}), "action": loads(r["action"], {})})
    return out


def on_value_changed(svc, c, iid: int, col: dict, old, new, depth: int) -> list[int]:
    it = c.execute("SELECT * FROM items WHERE id = ?", (iid,)).fetchone()
    if not it or it["parent_id"]:
        return []
    changed = []
    for rule in _rules(c, it["board_id"]):
        t = rule["trigger"]
        if t.get("column") != col["id"]:
            continue
        if t.get("type") == "status_change":
            if new is None or (t.get("to") is not None and t["to"] != new):
                continue
        elif t.get("type") != "column_change":
            continue
        changed += _run(svc, c, rule, iid, depth + 1)
    return changed


def on_item_created(svc, c, iid: int) -> list[int]:
    it = c.execute("SELECT * FROM items WHERE id = ?", (iid,)).fetchone()
    if not it or it["parent_id"]:
        return []
    changed = []
    for rule in _rules(c, it["board_id"]):
        if rule["trigger"].get("type") == "item_created":
            changed += _run(svc, c, rule, iid, 1)
    return changed


def _run(svc, c, rule: dict, iid: int, depth: int) -> list[int]:
    a = rule["action"]
    kind = a.get("type")
    it = c.execute("SELECT * FROM items WHERE id = ?", (iid,)).fetchone()
    if not it or it["state"] != "active":
        return []
    c.execute("UPDATE automations SET runs = runs + 1, last_run = ? WHERE id = ?", (now_iso(), rule["id"]))
    detail = {"automation": rule["id"]}
    if kind == "move_to_group":
        g = c.execute("SELECT id, title FROM groups WHERE id = ? AND board_id = ? AND state = 'active'",
                      (a.get("group"), it["board_id"])).fetchone()
        if not g or g["id"] == it["group_id"]:
            return []
        old = c.execute("SELECT title FROM groups WHERE id = ?", (it["group_id"],)).fetchone()
        top = c.execute("SELECT MIN(position) FROM items WHERE group_id = ? AND state = 'active'", (g["id"],)).fetchone()[0]
        c.execute("UPDATE items SET group_id = ?, position = ? WHERE id = ?", (g["id"], (top or 0) - 1, iid))
        seq = svc._bump(c, it["board_id"])
        svc._touch(c, iid, seq, None)
        svc._log(c, it["board_id"], iid, None, "item.move", old={"group": it["group_id"], "title": old[0] if old else ""},
                 new={"group": g["id"], "title": g["title"]}, detail=detail)
        return [iid]
    if kind == "set_date_today":
        return svc._set_value(c, None, iid, a.get("column"), today_sydney(), depth=depth)
    if kind == "set_status":
        return svc._set_value(c, None, iid, a.get("column"), a.get("label"), depth=depth)
    if kind == "clear_value":
        return svc._set_value(c, None, iid, a.get("column"), None, depth=depth)
    if kind == "assign_creator":
        if not it["created_by"]:
            return []
        cur = loads((c.execute("SELECT value FROM item_values WHERE item_id = ? AND column_id = ?",
                               (iid, a.get("column"))).fetchone() or [None])[0], None) or {}
        users = list(cur.get("users") or [])
        if it["created_by"] in users:
            return []
        return svc._set_value(c, None, iid, a.get("column"), {**cur, "users": users + [it["created_by"]]}, depth=depth)
    if kind == "notify":
        who = set(int(u) for u in a.get("users") or [])
        if a.get("column"):
            v = loads((c.execute("SELECT value FROM item_values WHERE item_id = ? AND column_id = ?",
                                 (iid, a["column"])).fetchone() or [None])[0], None) or {}
            who.update(v.get("users") or [])
        text = (a.get("text") or "an automation flagged this item").strip()[:300]
        for u in who:
            svc._notify(c, u, "automation", f"{it['name']}: {text}", item_id=iid)
        return []
    if kind == "archive_item":
        seq = svc._bump(c, it["board_id"])
        c.execute("UPDATE items SET state = 'archived', seq = ? WHERE id = ? OR parent_id = ?", (seq, iid, iid))
        svc._log(c, it["board_id"], iid, None, "item.archive", old={"state": "active", "name": it["name"]},
                 new={"state": "archived"}, detail=detail)
        return [iid]
    return []


# ---- managing rules ---------------------------------------------------------------------------- #
def check_rule(svc, board_id: int, trigger: dict, action: dict) -> tuple[dict, dict]:
    from .service import Invalid
    cols = {c["id"]: c for c in svc.columns(board_id, "item")}
    groups = {g[0] for g in svc.db.q("SELECT id FROM groups WHERE board_id = ? AND state = 'active'", (board_id,))}
    t, a = dict(trigger or {}), dict(action or {})
    if t.get("type") not in TRIGGERS:
        raise Invalid("choose when the automation runs")
    if t["type"] in ("status_change", "column_change"):
        col = cols.get(_int(t.get("column")))
        if not col or (t["type"] == "status_change" and col["type"] != "status"):
            raise Invalid("choose a status column")
        t["column"] = col["id"]
        if t["type"] == "status_change":
            to = t.get("to")
            t["to"] = None if to in (None, "", "any") else _int(to)
            if t["to"] is not None and not Status.label(col["settings"], t["to"]):
                raise Invalid("that label is not on the column")
    t = {k: v for k, v in t.items() if k in ("type", "column", "to")}
    if a.get("type") not in ACTIONS:
        raise Invalid("choose what the automation does")
    need = {"set_date_today": "date", "set_status": "status", "assign_creator": "people", "clear_value": None}
    if a["type"] == "move_to_group":
        a["group"] = _int(a.get("group"))
        if a["group"] not in groups:
            raise Invalid("choose a group on this board")
    elif a["type"] in need:
        col = cols.get(_int(a.get("column")))
        if not col or (need[a["type"]] and col["type"] != need[a["type"]]):
            raise Invalid(f"choose a {need[a['type']] or ''} column".replace("  ", " "))
        a["column"] = col["id"]
        if a["type"] == "set_status":
            a["label"] = _int(a.get("label"))
            if not Status.label(col["settings"], a["label"]):
                raise Invalid("choose a label")
    elif a["type"] == "notify":
        if a.get("column"):
            col = cols.get(_int(a["column"]))
            if not col or col["type"] != "people":
                raise Invalid("choose a people column")
            a["column"] = col["id"]
        a["users"] = [_int(u) for u in a.get("users") or [] if _int(u)]
        if not a.get("column") and not a["users"]:
            raise Invalid("choose who to notify")
        a["text"] = str(a.get("text") or "")[:300]
    a = {k: v for k, v in a.items() if k in ("type", "group", "column", "label", "users", "text")}
    return t, a


def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def list_rules(svc, board_id: int) -> list[dict]:
    rows = svc.db.q("SELECT * FROM automations WHERE board_id = ? ORDER BY id", (board_id,))
    cols = {c["id"]: c for c in svc.columns(board_id)}
    groups = {g["id"]: g["title"] for g in svc.db.q("SELECT id, title FROM groups WHERE board_id = ?", (board_id,))}
    users = {u["id"]: u["name"] for u in svc.users(include_inactive=True)}
    out = []
    for r in rows:
        t, a = loads(r["trigger"], {}), loads(r["action"], {})
        out.append({"id": r["id"], "trigger": t, "action": a, "active": bool(r["active"]), "runs": r["runs"],
                    "last_run": r["last_run"], "imported": bool(r["imported_from"]),
                    "text": describe(t, a, cols, groups, users)})
    return out


def describe(t: dict, a: dict, cols: dict, groups: dict, users: dict) -> str:
    def col(cid):
        return cols[cid]["title"] if cid in cols else "(deleted column)"

    def label(cid, lid):
        lab = Status.label(cols[cid]["settings"], lid) if cid in cols else None
        return lab["name"] if lab else "(deleted label)"

    if t.get("type") == "status_change":
        when = (f"When {col(t.get('column'))} changes to {label(t.get('column'), t['to'])}" if t.get("to") is not None
                else f"When {col(t.get('column'))} changes")
    elif t.get("type") == "column_change":
        when = f"When {col(t.get('column'))} changes"
    else:
        when = "When an item is created"
    k = a.get("type")
    if k == "move_to_group":
        then = f"move the item to {groups.get(a.get('group'), '(deleted group)')}"
    elif k == "set_date_today":
        then = f"set {col(a.get('column'))} to today"
    elif k == "set_status":
        then = f"set {col(a.get('column'))} to {label(a.get('column'), a.get('label'))}"
    elif k == "assign_creator":
        then = f"add the item's creator to {col(a.get('column'))}"
    elif k == "clear_value":
        then = f"clear {col(a.get('column'))}"
    elif k == "notify":
        who = [users.get(u, "?") for u in a.get("users") or []]
        if a.get("column"):
            who.append(f"the people in {col(a['column'])}")
        then = f"notify {', '.join(who)}" + (f": “{a['text']}”" if a.get("text") else "")
    elif k == "archive_item":
        then = "archive the item"
    else:
        then = "do nothing"
    return f"{when}, {then}"


def save_rule(svc, user, board_id: int, trigger: dict, action: dict, rule_id: int | None = None,
              imported_from: str | None = None) -> int:
    svc._need_edit(user)
    t, a = check_rule(svc, board_id, trigger, action)
    with svc.db.write() as c:
        if rule_id:
            c.execute("UPDATE automations SET trigger = ?, action = ? WHERE id = ? AND board_id = ?",
                      (dumps(t), dumps(a), rule_id, board_id))
        else:
            rule_id = c.execute("INSERT INTO automations (board_id, trigger, action, created_by, created_at, imported_from) "
                                "VALUES (?, ?, ?, ?, ?, ?)", (board_id, dumps(t), dumps(a), user["id"] if user else None,
                                                              now_iso(), imported_from)).lastrowid
        svc._log(c, board_id, None, user, "automation.save", new={"trigger": t, "action": a}, detail={"automation": rule_id})
    return rule_id


def set_rule_active(svc, user, rule_id: int, active: bool) -> None:
    svc._need_edit(user)
    with svc.db.write() as c:
        r = c.execute("SELECT board_id FROM automations WHERE id = ?", (rule_id,)).fetchone()
        if r:
            c.execute("UPDATE automations SET active = ? WHERE id = ?", (1 if active else 0, rule_id))
            svc._log(c, r[0], None, user, "automation.on" if active else "automation.off", detail={"automation": rule_id})


def delete_rule(svc, user, rule_id: int) -> None:
    svc._need_edit(user)
    with svc.db.write() as c:
        r = c.execute("SELECT board_id FROM automations WHERE id = ?", (rule_id,)).fetchone()
        if r:
            c.execute("DELETE FROM automations WHERE id = ?", (rule_id,))
            svc._log(c, r[0], None, user, "automation.delete", detail={"automation": rule_id})
