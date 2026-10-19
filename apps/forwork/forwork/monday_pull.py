"""
Pull everything ForWork brings across from monday.com - users, teams, workspaces, folders, and every board
with its groups, columns, views, items, subitems and updates - through monday.com's API, and write it as an
export bundle (see monday_bundle.py) for monday_import.py to load.

The firm runs this at cut-over so ForWork starts from fresh data: Admin > Import from monday.com, paste an
API token.  Or from a command prompt, with the token in the FORWORK_MONDAY_TOKEN environment variable:

    python -m forwork pull-monday --out monday-export --zip

Use a monday.com admin's API token (profile picture > Developers > My access tokens) so every board is
included.  The token is used for this pull only: it is never written to disk, logged or put in an error
message.  Nothing is changed in monday.com - every request is a read.

monday.com limits how much an account can ask for each minute.  When it says to wait, the pull waits as
long as it is told and carries on, so a large account can take a while.  Each board's file is written as
soon as that board is done and the manifest last, so a pull that stops part way never looks finished.

Automations are not available through monday.com's public API, so the bundle has none; they are set up
again in ForWork.
"""
from __future__ import annotations

import argparse
import datetime as dt
import http.client
import json
import logging
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path
from typing import Callable

from . import __version__
from .monday_bundle import FORMAT, VERSION, BundleError, BundleWriter, parse_json

API_URL = "https://api.monday.com/v2"
API_VERSION = "2025-04"
TOKEN_ENV = "FORWORK_MONDAY_TOKEN"
LIMIT_RETRIES = 5            # waits allowed per request when monday.com's rate or complexity limit is reached
TRANSIENT_RETRIES = 2        # retries after a dropped connection or a gateway error
TRANSIENT_WAIT = 10.0
DEFAULT_WAIT = 30.0          # when monday.com asks us to slow down without saying for how long
MAX_WAIT = 120.0
LIMIT_CODES = {"COMPLEXITY_BUDGET_EXHAUSTED", "ComplexityException", "RATE_LIMIT_EXCEEDED",
               "maxConcurrencyExceeded", "IP_RATE_LIMIT_EXCEEDED"}
BAD_TOKEN = ("monday.com did not accept the API token - copy it again from monday.com (profile picture > "
             "Developers > My access tokens) and paste all of it")

log = logging.getLogger(__name__)
Progress = Callable[[str, int, int], None]

# ---- the queries (operation names let a test stand-in tell them apart) ------------------------------ #
_COLUMNS = "columns { id title type description archived width settings_str }"
_VALUES = ("column_values { id type text value ... on BoardRelationValue { linked_item_ids } "
           "... on MirrorValue { display_value } ... on FormulaValue { display_value } }")
_ITEM = ("id name state created_at updated_at creator_id group { id } " + _VALUES + " subitems { id name state "
         "created_at updated_at creator_id board { id } column_values { id type text value } }")

Q_ACCOUNT = "query Account { account { name slug } }"
Q_USERS = "query Users($page: Int!) { users(limit: 200, page: $page) { id name email title time_zone_identifier } }"
Q_INACTIVE_USERS = ("query InactiveUsers($page: Int!) { users(limit: 200, page: $page, non_active: true) "
                    "{ id name email } }")
Q_TEAMS = "query Teams { teams { id name users { id } } }"
Q_WORKSPACES = "query Workspaces($page: Int!) { workspaces(limit: 100, page: $page) { id name kind description } }"
Q_FOLDERS = "query Folders($page: Int!) { folders(limit: 100, page: $page) { id name workspace { id } parent { id } } }"
Q_ITEMS = ("query BoardItems($board: ID!) { boards(ids: [$board], state: all) { id items_page(limit: 500) "
           "{ cursor items { " + _ITEM + " } } } }")
Q_NEXT_ITEMS = ("query NextItems($cursor: String!) { next_items_page(limit: 500, cursor: $cursor) "
                "{ cursor items { " + _ITEM + " } } }")
Q_UPDATES = ("query BoardUpdates($board: ID!, $page: Int!) { boards(ids: [$board], state: all) { id "
             "updates(limit: 500, page: $page) { id item_id body text_body created_at updated_at creator_id "
             "replies { id body text_body created_at creator_id } assets { id name file_extension file_size } } } }")
Q_SUBITEM_COLUMNS = "query SubitemColumns($board: ID!) { boards(ids: [$board], state: all) { id " + _COLUMNS + " } }"


def boards_query(view_settings: bool = True) -> str:
    """The boards list; views.settings_str is left out for API versions that do not have it."""
    views = "views { id name type settings_str }" if view_settings else "views { id name type }"
    return ("query Boards($page: Int!) { boards(limit: 100, page: $page, state: all) { id name state board_kind type "
            "items_count description workspace { id name } board_folder_id updated_at owners { id } "
            "groups { id title color position archived deleted } " + _COLUMNS + " " + views + " } }")


# ---- talking to monday.com ----------------------------------------------------------------------- #
class MondayError(Exception):
    """A pull that cannot go on.  kind: auth (the token), limit (monday.com's limits), network, api."""

    def __init__(self, message: str, kind: str = "api"):
        super().__init__(message)
        self.kind = kind


class _Unreachable(Exception):
    pass


class _Retry(Exception):
    def __init__(self, seconds: float, why: str, final: str, group: str, kind: str):
        super().__init__(why)
        self.seconds, self.why, self.final, self.group, self.kind = seconds, why, final, group, kind


_DIRECT = urllib.request.build_opener(urllib.request.ProxyHandler({}))
_TOKEN_SHAPE = re.compile(r"[\x21-\x7e]+")      # printable, no spaces - anything else cannot go in a header


def _http(method: str, url: str, headers: dict, data: bytes | None, timeout: float) -> tuple[int, dict, bytes]:
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    host = urllib.parse.urlsplit(url).hostname or ""
    opener = _DIRECT if host in ("localhost", "127.0.0.1", "::1") else urllib.request.build_opener()
    try:
        with opener.open(req, timeout=timeout) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        try:
            body = e.read()
        except (OSError, http.client.HTTPException):
            body = b""
        return e.code, dict(e.headers or {}), body
    except (urllib.error.URLError, http.client.HTTPException, TimeoutError, OSError) as e:
        raise _Unreachable(str(getattr(e, "reason", None) or e)) from None


def _errors(reply) -> list[dict]:
    """monday.com's errors as [{message, code, retry}], from either of the shapes it uses."""
    out: list[dict] = []
    if not isinstance(reply, dict):
        return out
    for e in reply.get("errors") or []:
        if isinstance(e, dict):
            ext = e.get("extensions") if isinstance(e.get("extensions"), dict) else {}
            out.append({"message": str(e.get("message") or ""), "code": str(ext.get("code") or ""),
                        "retry": ext.get("retry_in_seconds")})
        else:
            out.append({"message": str(e), "code": "", "retry": None})
    if reply.get("error_code") or reply.get("error_message"):
        data = reply.get("error_data") if isinstance(reply.get("error_data"), dict) else {}
        out.append({"message": str(reply.get("error_message") or ""), "code": str(reply.get("error_code") or ""),
                    "retry": data.get("retry_in_seconds")})
    return out


def _wait_seconds(errors: list[dict], headers: dict) -> float:
    """How long monday.com asked us to wait: retry_in_seconds, else Retry-After, else "reset in N seconds"."""
    lower = {str(k).lower(): v for k, v in (headers or {}).items()}
    candidates = [e["retry"] for e in errors] + [lower.get("retry-after")]
    for e in errors:
        m = re.search(r"reset in (\d+(?:\.\d+)?) second", e["message"])
        if m:
            candidates.append(m.group(1))
    for c in candidates:
        try:
            if c is not None and c != "":
                return max(1.0, min(MAX_WAIT, float(c)))
        except (TypeError, ValueError):
            continue
    return DEFAULT_WAIT


def _is_bad_token(e: dict) -> bool:
    return "not authenticated" in e["message"].lower() or e["code"] == "UNAUTHENTICATED"


class MondayClient:
    """monday.com's GraphQL API with one API token.  query() waits out monday.com's limits and retries."""

    def __init__(self, token: str, endpoint: str = API_URL, api_version: str = API_VERSION, timeout: float = 120,
                 sleep: Callable[[float], None] = time.sleep):
        token = (token or "").strip()
        if not token:
            raise MondayError("no monday.com API token was given - paste the token from monday.com "
                              "(profile picture > Developers > My access tokens)", kind="auth")
        if not _TOKEN_SHAPE.fullmatch(token):
            raise MondayError("that does not look like a monday.com API token - copy it again from monday.com "
                              "(profile picture > Developers > My access tokens)", kind="auth")
        self._token = token
        self.endpoint = endpoint
        self.api_version = api_version
        self.timeout = timeout
        self.sleep = sleep
        self.on_wait: Callable[[float, str], None] | None = None     # told before each wait, for progress pages

    def __repr__(self) -> str:
        return f"MondayClient({self.endpoint!r})"

    def _scrub(self, text: str) -> str:
        return text.replace(self._token, "[API token]")

    def query(self, query: str, variables: dict | None = None) -> dict:
        """Run one query and return its data, or raise MondayError."""
        body = json.dumps({"query": query, "variables": variables or {}}).encode("utf-8")
        tries = {"limit": 0, "transient": 0}
        while True:
            try:
                return self._send(body)
            except _Retry as r:
                tries[r.group] += 1
                if tries[r.group] > (LIMIT_RETRIES if r.group == "limit" else TRANSIENT_RETRIES):
                    raise MondayError(self._scrub(r.final), kind=r.kind) from None
                log.info("%s - waiting %.0f seconds", r.why, r.seconds)
                if self.on_wait:
                    self.on_wait(r.seconds, r.why)
                self.sleep(r.seconds)

    def _send(self, body: bytes) -> dict:
        headers = {"Authorization": self._token, "API-Version": self.api_version, "Content-Type": "application/json",
                   "Accept": "application/json", "User-Agent": f"ForWork/{__version__}"}
        try:
            status, reply_headers, raw = _http("POST", self.endpoint, headers, body, self.timeout)
        except _Unreachable as e:
            raise _Retry(TRANSIENT_WAIT, "monday.com could not be reached",
                         f"could not reach monday.com ({e}) - check the internet connection and try again",
                         "transient", "network") from None
        try:
            reply = json.loads(raw.decode("utf-8")) if raw else None
        except (UnicodeDecodeError, ValueError):
            reply = None
        errors = _errors(reply)
        codes = {e["code"] for e in errors}
        text = self._scrub("; ".join(e["message"] for e in errors if e["message"]))[:500]

        if status == 401 or any(_is_bad_token(e) for e in errors):
            raise MondayError(BAD_TOKEN, kind="auth")
        if "DAILY_LIMIT_EXCEEDED" in codes:
            raise MondayError("monday.com's daily limit on API use for this account has been reached - "
                              "try again tomorrow", kind="limit")
        if status == 429 or codes & LIMIT_CODES:
            raise _Retry(_wait_seconds(errors, reply_headers), "monday.com's limit on requests was reached",
                         "monday.com kept asking ForWork to slow down (its limit on API use was reached) - "
                         "wait ten minutes and try again", "limit", "limit")
        if status in (502, 503, 504):
            raise _Retry(TRANSIENT_WAIT, "monday.com is busy",
                         f"monday.com is not answering properly (HTTP {status}) - try again in a few minutes",
                         "transient", "api")
        if status == 403:
            raise MondayError("monday.com would not let this API token read everything (HTTP 403)"
                              f"{': ' + text if text else ''} - use a monday.com admin's token")
        if status != 200 or errors:
            where = f" (HTTP {status})" if status != 200 else ""
            raise MondayError(f"monday.com returned an error{where}: {text or 'no details were given'}")
        data = reply.get("data") if isinstance(reply, dict) else None
        if not isinstance(data, dict):
            raise MondayError("monday.com sent back something ForWork did not expect - try again in a few minutes")
        return data


# ---- reading the account ------------------------------------------------------------------------ #
def _sid(v) -> str | None:
    return None if v is None or v == "" else str(v)


def _pages(fetch: Callable[[int], list | None]) -> list[dict]:
    """
    Page 1, 2, 3 ... until monday.com sends an empty page.  A page can come back a little short (499 of 500)
    with more to follow, so a short page is not the end.  Rows already seen are dropped.
    """
    out: list[dict] = []
    seen: set[str] = set()
    page = 1
    while True:
        new = [r for r in fetch(page) or [] if r and str(r.get("id")) not in seen]
        if not new:
            return out
        seen.update(str(r.get("id")) for r in new)
        out.extend(new)
        page += 1


def fetch_users(client: MondayClient) -> list[dict]:
    active = _pages(lambda p: client.query(Q_USERS, {"page": p}).get("users"))
    inactive = _pages(lambda p: client.query(Q_INACTIVE_USERS, {"page": p}).get("users"))
    gone = {str(u["id"]) for u in inactive}
    users = [{"id": str(u["id"]), "name": u.get("name") or "", "email": u.get("email") or "",
              "title": u.get("title") or "", "time_zone_identifier": u.get("time_zone_identifier") or "",
              "active": str(u["id"]) not in gone} for u in active]
    listed = {u["id"] for u in users}
    users += [{"id": str(u["id"]), "name": u.get("name") or "", "email": u.get("email") or "", "title": "",
               "time_zone_identifier": "", "active": False} for u in inactive if str(u["id"]) not in listed]
    return users


def fetch_teams(client: MondayClient) -> list[dict]:
    return [{"id": str(t["id"]), "name": t.get("name") or "",
             "user_ids": [str(u["id"]) for u in t.get("users") or [] if u]}
            for t in client.query(Q_TEAMS).get("teams") or [] if t]


def fetch_workspaces(client: MondayClient) -> list[dict]:
    return [{"id": str(w["id"]), "name": w.get("name") or "", "kind": w.get("kind"),
             "description": w.get("description") or ""}
            for w in _pages(lambda p: client.query(Q_WORKSPACES, {"page": p}).get("workspaces"))]


def fetch_folders(client: MondayClient) -> list[dict]:
    return [{"id": str(f["id"]), "name": f.get("name") or "",
             "workspace_id": _sid((f.get("workspace") or {}).get("id")),
             "parent_id": _sid((f.get("parent") or {}).get("id"))}
            for f in _pages(lambda p: client.query(Q_FOLDERS, {"page": p}).get("folders"))]


def fetch_boards(client: MondayClient) -> list[dict]:
    """Every board the token can see, of every state and type, as monday.com sends them."""
    view_settings = True

    def page(p: int):
        nonlocal view_settings
        if view_settings:
            try:
                return client.query(boards_query(True), {"page": p}).get("boards")
            except MondayError as e:
                if e.kind != "api" or p > 1:
                    raise
                log.info("monday.com would not send view settings - reading boards without them")
                view_settings = False
        return client.query(boards_query(False), {"page": p}).get("boards")

    return _pages(page)


def subitem_board_id(board: dict) -> str | None:
    """A board's subitems live on a board of their own, named in its subtasks column's settings."""
    for c in board.get("columns") or []:
        if c.get("type") == "subtasks":
            settings = parse_json(c.get("settings_str"), {})
            ids = settings.get("boardIds") if isinstance(settings, dict) else None
            if ids:
                return str(ids[0])
    return None


def choose_boards(boards: list[dict], include_archived: bool = True, board_ids: list[str] | None = None) -> list[dict]:
    """The boards to bring across: active (and archived) boards, not docs, subitem boards or deleted boards."""
    states = ("active", "archived") if include_archived else ("active",)
    chosen = [b for b in boards if (b.get("type") or "board") == "board" and b.get("state") in states]
    subitem_boards = {subitem_board_id(b) for b in chosen}
    chosen = [b for b in chosen if str(b["id"]) not in subitem_boards]
    if board_ids:
        wanted = [str(i).strip() for i in ([board_ids] if isinstance(board_ids, str) else board_ids)]
        found = {str(b["id"]) for b in chosen}
        missing = [i for i in wanted if i not in found]
        if missing:
            raise MondayError(f"monday.com has no board to bring across with the ID {', '.join(missing)} - check the "
                              f"number (it is in the board's web address); deleted boards, docs and subitem boards "
                              f"cannot be pulled" + ("" if include_archived else ", and archived boards were left out"))
        chosen = [b for b in chosen if str(b["id"]) in set(wanted)]
    return chosen


def fetch_items(client: MondayClient, board_id: str, note: Callable[[int], None] | None = None) -> list[dict] | None:
    """A board's items in board order, 500 a page; None if the board has gone from monday.com."""
    boards = client.query(Q_ITEMS, {"board": board_id}).get("boards") or []
    if not boards or not boards[0]:
        return None
    page = boards[0].get("items_page") or {}
    items = [i for i in page.get("items") or [] if i]
    cursor = page.get("cursor")
    while cursor:
        page = client.query(Q_NEXT_ITEMS, {"cursor": cursor}).get("next_items_page") or {}
        batch = [i for i in page.get("items") or [] if i]
        if not batch:
            break
        items.extend(batch)
        cursor = page.get("cursor")
        if note:
            note(len(items))
    return items


def fetch_columns(client: MondayClient, board_id: str) -> list[dict]:
    boards = client.query(Q_SUBITEM_COLUMNS, {"board": board_id}).get("boards") or []
    return (boards[0].get("columns") or []) if boards and boards[0] else []


def fetch_updates(client: MondayClient, board_id: str) -> list[dict]:
    def page(p: int):
        boards = client.query(Q_UPDATES, {"board": board_id, "page": p}).get("boards") or []
        return (boards[0].get("updates") or []) if boards and boards[0] else []
    return [_update(u) for u in _pages(page)]


# ---- into the bundle's shape -------------------------------------------------------------------- #
def _column(c: dict) -> dict:
    settings = parse_json(c.get("settings_str"), {})
    return {"id": str(c["id"]), "title": c.get("title") or "", "type": c.get("type"),
            "description": c.get("description"), "archived": bool(c.get("archived")), "width": c.get("width"),
            "settings": settings if settings is not None else {}}


def _view(v: dict) -> dict:
    settings = parse_json(v["settings_str"], {}) if "settings_str" in v else None
    return {"id": str(v["id"]), "name": v.get("name") or "", "type": v.get("type"), "settings": settings}


def _values(column_values) -> dict:
    out = {}
    for cv in column_values or []:
        if not cv:
            continue
        v = {"type": cv.get("type"), "text": cv.get("text"), "value": parse_json(cv.get("value"))}
        if cv.get("linked_item_ids") is not None:
            v["linked_item_ids"] = [str(i) for i in cv["linked_item_ids"]]
        if cv.get("display_value") is not None:
            v["display_value"] = cv["display_value"]
        out[str(cv["id"])] = v
    return out


def _subitem(s: dict) -> dict:
    return {"id": str(s["id"]), "name": s.get("name") or "", "state": s.get("state"), "created_at": s.get("created_at"),
            "updated_at": s.get("updated_at"), "creator_id": _sid(s.get("creator_id")),
            "values": _values(s.get("column_values"))}


def _item(i: dict) -> dict:
    return {"id": str(i["id"]), "name": i.get("name") or "", "state": i.get("state"), "created_at": i.get("created_at"),
            "updated_at": i.get("updated_at"), "creator_id": _sid(i.get("creator_id")),
            "group_id": _sid((i.get("group") or {}).get("id")), "values": _values(i.get("column_values")),
            "subitems": [_subitem(s) for s in i.get("subitems") or [] if s]}


def _update(u: dict) -> dict:
    return {"id": str(u["id"]), "item_id": _sid(u.get("item_id")), "body": u.get("body") or "",
            "text_body": u.get("text_body") or "", "created_at": u.get("created_at"), "updated_at": u.get("updated_at"),
            "creator_id": _sid(u.get("creator_id")),
            "replies": [{"id": str(r["id"]), "body": r.get("body") or "", "text_body": r.get("text_body") or "",
                         "created_at": r.get("created_at"), "creator_id": _sid(r.get("creator_id"))}
                        for r in u.get("replies") or [] if r],
            "assets": [{"id": str(a["id"]), "name": a.get("name") or "", "file_extension": a.get("file_extension"),
                        "file_size": a.get("file_size")} for a in u.get("assets") or [] if a]}


def board_record(board: dict, items: list[dict], updates: list[dict], sub_id: str | None,
                 sub_columns: list[dict]) -> dict:
    """One boards/<id>.json, from monday.com's board, its raw items, and its (already shaped) updates."""
    return {
        "board": {"id": str(board["id"]), "name": board.get("name") or "", "state": board.get("state"),
                  "board_kind": board.get("board_kind"), "description": board.get("description") or "",
                  "workspace_id": _sid((board.get("workspace") or {}).get("id")),
                  "folder_id": _sid(board.get("board_folder_id")), "updated_at": board.get("updated_at"),
                  "owner_ids": [str(o["id"]) for o in board.get("owners") or [] if o]},
        "groups": [{"id": str(g["id"]), "title": g.get("title") or "", "color": g.get("color"),
                    "position": g.get("position"), "archived": bool(g.get("archived")),
                    "deleted": bool(g.get("deleted"))}
                   for g in board.get("groups") or [] if g],
        "columns": [_column(c) for c in board.get("columns") or [] if c],
        "subitem_board_id": sub_id,
        "subitem_columns": [_column(c) for c in sub_columns if c],
        "views": [_view(v) for v in board.get("views") or [] if v],
        "items": [_item(i) for i in items],
        "updates": updates,
    }


def pull_board(client: MondayClient, board: dict, every: dict[str, dict],
               note: Callable[[int], None] | None = None) -> dict | None:
    """Everything on one board, ready to write; None if the board has gone from monday.com."""
    bid = str(board["id"])
    items = fetch_items(client, bid, note)
    if items is None:
        return None
    # from the subtasks column, or failing that from where the subitems say they live
    sub_id = subitem_board_id(board) or next((_sid(s["board"].get("id")) for i in items
                                              for s in i.get("subitems") or [] if s and s.get("board")), None)
    sub_columns: list[dict] = []
    if sub_id:
        sub = every.get(sub_id)
        sub_columns = (sub.get("columns") or []) if sub else fetch_columns(client, sub_id)
    updates = fetch_updates(client, bid)
    if sub_id:            # updates on subitems are kept with the parent board
        seen = {u["id"] for u in updates}
        updates += [u for u in fetch_updates(client, sub_id) if u["id"] not in seen]
    return board_record(board, items, updates, sub_id, sub_columns)


def _clear_old_export(folder: Path) -> None:
    """Remove an earlier export from the folder, so the new one has no boards left over from it."""
    manifest = folder / "manifest.json"
    if manifest.is_file():
        old = parse_json(manifest.read_text(encoding="utf-8", errors="replace"), {})
        if not isinstance(old, dict) or old.get("format") != FORMAT:
            raise MondayError(f"{folder} already has a manifest.json that is not from a monday.com export - "
                              f"choose an empty folder")
        manifest.unlink()
    for name in ("users.json", "workspaces.json"):
        (folder / name).unlink(missing_ok=True)
    boards = folder / "boards"
    if boards.is_dir():
        for p in boards.iterdir():
            if p.is_file() and p.name.split(".")[0].isdigit() and p.name.endswith((".json", ".json.tmp")):
                p.unlink()


def pull(token: str, dest: str | Path, *, endpoint: str = API_URL, include_archived: bool = True,
         board_ids: list[str] | None = None, progress: Progress | None = None,
         sleep: Callable[[float], None] = time.sleep) -> dict:
    """
    Pull the monday.com account into an export bundle in dest and return its manifest.  An earlier export
    in dest is replaced.  progress(message, done, total) is told what is happening: total is 0 until the
    boards are listed, then done counts the boards finished ("Pulling Client List (3 of 39)", 2, 39).
    It may raise to stop the pull.
    """
    report: Progress = progress or (lambda message, done, total: None)
    now = {"message": "", "done": 0, "total": 0}

    def say(message: str, done: int = 0, total: int = 0) -> None:
        now.update(message=message, done=done, total=total)
        report(message, done, total)

    client = MondayClient(token, endpoint=endpoint, sleep=sleep)
    client.on_wait = lambda seconds, why: report(f"{now['message']} - {why}, waiting {seconds:.0f} seconds",
                                                 now["done"], now["total"])
    exported_at = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    say("Connecting to monday.com")
    account = client.query(Q_ACCOUNT).get("account") or {}
    say("Reading users and teams")
    users, teams = fetch_users(client), fetch_teams(client)
    say("Reading workspaces and folders")
    workspaces, folders = fetch_workspaces(client), fetch_folders(client)
    say("Reading the list of boards")
    every = fetch_boards(client)
    chosen = choose_boards(every, include_archived=include_archived, board_ids=board_ids)
    by_id = {str(b["id"]): b for b in every}
    total = len(chosen)

    # the token and the choice of boards are good, so now the folder is written
    folder = Path(dest)
    _clear_old_export(folder)
    writer = BundleWriter(folder)
    writer.write_users(users, teams)
    writer.write_workspaces(workspaces, folders)

    def items_so_far(count: int) -> None:
        report(f"{now['message']} - {count:,} items so far", now["done"], now["total"])

    counts = {"boards": 0, "items": 0, "subitems": 0, "updates": 0}
    written: list[str] = []
    skipped: list[dict] = []
    for n, board in enumerate(chosen, 1):
        say(f"Pulling {board.get('name') or board['id']} ({n} of {total})", n - 1, total)
        data = pull_board(client, board, by_id, note=items_so_far)
        if data is None:
            log.warning("board %s was deleted from monday.com during the pull - left out", board["id"])
            skipped.append({"id": str(board["id"]), "name": board.get("name") or ""})
            continue
        writer.write_board(data)
        written.append(data["board"]["id"])
        counts["boards"] += 1
        counts["items"] += len(data["items"])
        counts["subitems"] += sum(len(i["subitems"]) for i in data["items"])
        counts["updates"] += len(data["updates"])

    counts.update(users=len(users), teams=len(teams), workspaces=len(workspaces), folders=len(folders))
    manifest = {"exported_at": exported_at, "source": "monday-api",
                "account": {"name": account.get("name") or "", "slug": account.get("slug") or ""},
                "boards": written, "counts": counts}
    if skipped:
        manifest["skipped_boards"] = skipped
    writer.write_manifest(manifest)
    say(f"Finished - {counts['boards']} boards, {counts['items']:,} items and {counts['updates']:,} updates",
        total, total)
    return {"format": FORMAT, "version": VERSION, **manifest}


def zip_bundle(folder: str | Path, zip_path: str | Path) -> Path:
    """Zip an export folder for uploading, with manifest.json at the top of the .zip."""
    folder, zip_path = Path(folder), Path(zip_path)
    if not (folder / "manifest.json").is_file():
        raise BundleError(f"{folder} has no manifest.json - the pull from monday.com did not finish")
    files = [folder / n for n in ("manifest.json", "users.json", "workspaces.json") if (folder / n).is_file()]
    files += sorted((p for p in (folder / "boards").glob("*.json") if p.stem.isdigit()), key=lambda p: int(p.stem))
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = zip_path.with_name(zip_path.name + ".tmp")
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
        for p in files:
            z.write(p, p.relative_to(folder).as_posix())
    tmp.replace(zip_path)
    return zip_path


def main_pull(argv: list[str] | None = None) -> int:
    """python -m forwork pull-monday - the pull from a command prompt."""
    ap = argparse.ArgumentParser(prog="python -m forwork pull-monday",
                                 description="Pull every board, item, update, user and workspace from monday.com "
                                             "into a folder ForWork can import.")
    ap.add_argument("--token-env", default=TOKEN_ENV, metavar="NAME",
                    help=f"environment variable holding the monday.com API token (default {TOKEN_ENV})")
    ap.add_argument("--out", required=True, metavar="FOLDER", help="folder to write the export to")
    ap.add_argument("--zip", action="store_true", help="also make FOLDER.zip, for uploading to ForWork")
    ap.add_argument("--board", action="append", default=[], metavar="ID", help="only this board (repeat for more)")
    ap.add_argument("--active-only", action="store_true", help="leave out archived boards")
    ap.add_argument("--endpoint", default=API_URL, help=argparse.SUPPRESS)
    args = ap.parse_args(argv)

    token = os.environ.get(args.token_env, "").strip()
    if not token:
        print(f"Put the monday.com API token in the {args.token_env} environment variable first "
              f"(profile picture > Developers > My access tokens in monday.com).", file=sys.stderr)
        return 2
    out = Path(args.out)
    try:
        manifest = pull(token, out, endpoint=args.endpoint, include_archived=not args.active_only,
                        board_ids=args.board or None, progress=lambda message, done, total: print(message, flush=True))
        zipped = zip_bundle(out, out.parent / (out.name + ".zip")) if args.zip else None
    except (MondayError, BundleError) as e:
        print(f"Not finished: {e}", file=sys.stderr)
        return 1
    print(f"Saved in {out}" + (f" and {zipped}" if zipped else ""))
    if manifest.get("skipped_boards"):
        names = ", ".join(b["name"] or b["id"] for b in manifest["skipped_boards"])
        print(f"Left out because they were deleted during the pull: {names}")
    return 0
