"""
A stand-in for monday.com's GraphQL API on 127.0.0.1, for testing monday_pull end to end.  It answers the
pull's queries by operation name from a small made-up account (synthetic names only), pages like the real
API - including a first page of updates that comes back one short - checks the token and headers, and can
be told to fail the ways monday.com does: a complexity or rate limit, an HTTP 429, a bad token, an error.
"""
from __future__ import annotations

import copy
import json
import re
import threading
from functools import lru_cache

from flask import Flask, jsonify, request
from werkzeug.serving import make_server

TOKEN = "fake-monday-token-0123456789abcdef"
API_VERSION = "2025-04"
ACCOUNT = {"name": "Example Accounting", "slug": "example-accounting"}

MAIN_BOARD, SUB_BOARD, ARCHIVED_BOARD = "5001", "5002", "5003"
DELETED_BOARD, DOC_BOARD, OBJECT_BOARD = "5004", "5005", "5006"
MAIN_ITEMS = 1200                     # three pages of 500
MAIN_UPDATES = 1120                   # pages of 500, 500, 120 - but the API hides one, so page 1 has 499
HIDDEN_UPDATE = str(900000 + 250)
SUB_UPDATES = 30
ARCHIVED_UPDATES = 2

USERS = [
    {"id": "1001", "name": "Alex Citizen", "email": "alex.citizen@example.com.au", "title": "Partner",
     "time_zone_identifier": "Australia/Sydney"},
    {"id": "1002", "name": "Sam Sample", "email": "sam.sample@example.com.au", "title": "Senior Accountant",
     "time_zone_identifier": "Australia/Brisbane"},
    {"id": "1003", "name": "Jordan Example", "email": "jordan.example@example.com.au", "title": None,
     "time_zone_identifier": "Australia/Perth"},
]
INACTIVE_USERS = [{"id": "1004", "name": "Casey Former", "email": "casey.former@example.com.au"}]
TEAMS = [{"id": "2001", "name": "Workflow Managers", "users": [{"id": "1001"}, {"id": "1002"}]}]
WORKSPACES = [{"id": "3001", "name": "Main workspace", "kind": "open", "description": None},
              {"id": "3002", "name": "Compliance", "kind": "closed", "description": "Tax and BAS work"}]
FOLDERS = [{"id": "4001", "name": "2026", "workspace": {"id": "3002"}, "parent": None}]

GROUPS = [{"id": "new_jobs", "title": "New jobs", "color": "#579bfc", "position": "65536", "archived": False,
           "deleted": False},
          {"id": "in_progress", "title": "In progress", "color": "#fdab3d", "position": "131072", "archived": False,
           "deleted": False},
          {"id": "completed", "title": "Completed", "color": "#00c875", "position": "196608", "archived": False,
           "deleted": False}]
STATUS = ["Working on it", "Done", "Stuck"]
CLIENTS = [("7000", "Example Pty Ltd"), ("7001", "Sample Family Trust"), ("7002", "Citizen Super Fund")]


def _column(cid, title, ctype, settings="", archived=False, description=None, width=None):
    return {"id": cid, "title": title, "type": ctype, "description": description, "archived": archived, "width": width,
            "settings_str": settings if isinstance(settings, str) else json.dumps(settings)}


def _value(cid, ctype, text, value=None, **extra):
    return {"id": cid, "type": ctype, "text": text, "value": None if value is None else json.dumps(value), **extra}


def _board(bid, name, state="active", btype="board", **kw):
    b = {"id": bid, "name": name, "state": state, "board_kind": "public", "type": btype, "items_count": 0,
         "description": None, "workspace": None, "board_folder_id": None, "updated_at": "2026-10-01T00:00:00Z",
         "owners": [{"id": "1001"}], "groups": [], "columns": [_column("name", "Name", "name", "{}")], "views": []}
    b.update(kw)
    return b


def _update(uid: int, item_id: str, n: int, creator: str = "1002") -> dict:
    u = {"id": str(uid), "item_id": item_id, "body": f"<p>Called Example Pty Ltd about job {n}.</p>",
         "text_body": f"Called Example Pty Ltd about job {n}.", "created_at": f"2026-09-{1 + n % 28:02d}T01:00:00Z",
         "updated_at": f"2026-09-{1 + n % 28:02d}T02:00:00Z", "creator_id": creator, "replies": [], "assets": []}
    if n % 50 == 0:
        u["replies"] = [{"id": str(uid + 500000), "body": "<p>Thanks Alex.</p>", "text_body": "Thanks Alex.",
                         "created_at": "2026-09-30T03:00:00Z", "creator_id": "1001"}]
    if n % 100 == 0:
        u["assets"] = [{"id": str(uid + 700000), "name": "engagement-letter.pdf", "file_extension": ".pdf",
                        "file_size": 12345}]
    return u


@lru_cache(maxsize=1)
def account_data() -> dict:
    """The made-up account: boards (in monday's order), items by board, updates by board."""
    main_columns = [
        _column("name", "Name", "name", "{}", width=300),
        _column("status", "Status", "status", {"labels": {"0": "Working on it", "1": "Done", "2": "Stuck"},
                                               "labels_colors": {"0": {"color": "#fdab3d"}}},
                description="Where the job is up to", width=140),
        _column("person", "Accountant", "people", ""),
        _column("date4", "Due date", "date", "{}"),
        _column("numbers", "Fee", "numbers", {"unit": {"symbol": "$"}}),
        _column("connect_boards", "Client", "board_relation", {"boardIds": [int(ARCHIVED_BOARD)]}),
        _column("mirror", "Client ABN", "mirror", {"relation_column": {"connect_boards": True}}),
        _column("formula", "Fee incl. GST", "formula", {"formula": "{numbers}*1.1"}),
        _column("subitems", "Subitems", "subtasks", {"allowMultipleItems": True, "itemTypeName": "item",
                                                     "displayType": "BOARD_INLINE", "boardIds": [int(SUB_BOARD)]}),
        _column("old_notes", "Old notes", "text", "{}", archived=True),
    ]
    sub_columns = [_column("name", "Name", "name", "{}"),
                   _column("status_1", "Status", "status", {"labels": {"1": "Done"}}),
                   _column("date_1", "Date", "date", "{}")]
    boards = [
        _board(MAIN_BOARD, "Annual Compliance 2026-2027", items_count=MAIN_ITEMS, description="Tax returns",
               workspace={"id": "3002", "name": "Compliance"}, board_folder_id="4001",
               owners=[{"id": "1001"}, {"id": "1002"}], groups=GROUPS, columns=main_columns,
               views=[{"id": "v1", "name": "Main table", "type": "table", "settings_str": '{"filters":[]}'},
                      {"id": "v2", "name": "By accountant", "type": "kanban", "settings_str": ""}]),
        _board(SUB_BOARD, "Subitems of Annual Compliance 2026-2027", btype="sub_items_board",
               workspace={"id": "3002", "name": "Compliance"}, columns=sub_columns),
        _board(ARCHIVED_BOARD, "Client List", state="archived", items_count=3,
               groups=[{"id": "topics", "title": "Clients", "color": "#579bfc", "position": "65536",
                        "archived": False, "deleted": False}],
               columns=[_column("name", "Name", "name", "{}"), _column("text", "ABN", "text", "{}")]),
        _board(DELETED_BOARD, "Old Board", state="deleted"),
        _board(DOC_BOARD, "Office Procedures", btype="document"),
        _board(OBJECT_BOARD, "Contacts Object", btype="custom_object"),
    ]

    main_items, sub_ids = [], []
    for i in range(MAIN_ITEMS):
        k = i % 3
        cid, cname = CLIENTS[k]
        subitems = []
        if i % 10 == 0:
            for s, name in enumerate(["Gather documents", "Lodge return"]):
                sid = str(800000 + i * 2 + s)
                sub_ids.append(sid)
                subitems.append({"id": sid, "name": name, "state": "active", "created_at": "2026-07-02T00:00:00Z",
                                 "updated_at": "2026-07-03T00:00:00Z", "creator_id": "1002", "board": {"id": SUB_BOARD},
                                 "column_values": [_value("status_1", "status", "Done", {"index": 1}),
                                                   _value("date_1", "date", "2026-08-01", {"date": "2026-08-01"})]})
        main_items.append({
            "id": str(600000 + i), "name": f"Example Client {i + 1:04d} - Tax return", "state": "active",
            "created_at": f"2026-07-{1 + i % 28:02d}T09:00:00Z", "updated_at": "2026-10-01T09:00:00Z",
            "creator_id": "1001", "group": {"id": GROUPS[k]["id"]},
            "column_values": [
                _value("status", "status", STATUS[k], {"index": k, "changed_at": "2026-09-01T00:00:00.000Z"}),
                _value("person", "people", "Alex Citizen", {"personsAndTeams": [{"id": 1001, "kind": "person"}]}),
                _value("date4", "date", "2026-10-31", {"date": "2026-10-31"}),
                _value("numbers", "numbers", str(100 + i), str(100 + i)),
                _value("connect_boards", "board_relation", cname, {"linkedPulseIds": [{"linkedPulseId": int(cid)}]},
                       linked_item_ids=[cid]),
                _value("mirror", "mirror", None, None, display_value=f"00 000 000 00{k}"),
                _value("formula", "formula", None, None, display_value=f"{(100 + i) * 1.1:.2f}"),
                _value("subitems", "subtasks", ", ".join(s["name"] for s in subitems),
                       {"linkedPulseIds": [{"linkedPulseId": int(s["id"])} for s in subitems]} if subitems else None),
                _value("old_notes", "text", "", None),
            ],
            "subitems": subitems})
    archived_items = [{"id": cid, "name": cname, "state": "active", "created_at": "2025-01-01T00:00:00Z",
                       "updated_at": "2025-06-01T00:00:00Z", "creator_id": "1004", "group": {"id": "topics"},
                       "column_values": [_value("text", "text", f"00 000 000 00{k}", f"00 000 000 00{k}")],
                       "subitems": []} for k, (cid, cname) in enumerate(CLIENTS)]

    updates = {
        MAIN_BOARD: [_update(900000 + n, main_items[n % MAIN_ITEMS]["id"], n) for n in range(MAIN_UPDATES)],
        SUB_BOARD: [_update(950000 + n, sub_ids[n % len(sub_ids)], n + 1) for n in range(SUB_UPDATES)],
        ARCHIVED_BOARD: [_update(960000 + n, CLIENTS[n][0], n + 1, creator="1004") for n in range(ARCHIVED_UPDATES)],
    }
    return {"boards": boards, "items": {MAIN_BOARD: main_items, ARCHIVED_BOARD: archived_items}, "updates": updates}


def _limit(query: str, field: str) -> int:
    return int(re.search(r"(?<![\w])" + field + r"\(limit: (\d+)", query).group(1))


def _page(rows: list, page: int, limit: int) -> list:
    return rows[(page - 1) * limit: page * limit]


class FakeMonday:
    """
    with FakeMonday() as fake: ... pull(TOKEN, folder, endpoint=fake.url)
    fake.calls lists (operation name, variables) in order.  fake.fail(kind, op=..., board=..., times=...)
    makes the next matching request(s) fail: complexity, complexity_old, 429, daily, 502 or error.
    """

    def __init__(self):
        self.token = TOKEN
        self.calls: list[tuple[str, dict]] = []
        self.failures: list[dict] = []
        self.auth_style = "401"            # or "200": "Not Authenticated" in a 200 reply, like older API versions
        self.reject_view_settings = False  # act like an API version without views.settings_str
        self.hide_subitem_board = False    # leave the subitem board out of the boards list
        self.gone_boards: set[str] = set() # listed, but deleted by the time their items are asked for
        self.data = account_data()
        self.app = Flask("fake-monday")
        self.app.add_url_rule("/v2", "graphql", self._graphql, methods=["POST"])

    def fail(self, kind: str, op: str | None = None, times: int = 1, board: str | None = None, **opts) -> None:
        self.failures.append({"kind": kind, "op": op, "times": times, "board": board, **opts})

    def ops(self, name: str, **match) -> list[dict]:
        """The variables of each call to one operation, optionally only those matching the given variables."""
        return [v for op, v in self.calls if op == name and all(v.get(k) == x for k, x in match.items())]

    # ---- the endpoint ----------------------------------------------------------------------- #
    def _graphql(self):
        if request.headers.get("Authorization") != self.token:
            body = {"errors": [{"message": "Not Authenticated", "extensions": {"code": "UNAUTHENTICATED"}}]}
            return jsonify(body), (200 if self.auth_style == "200" else 401)
        if request.headers.get("API-Version") != API_VERSION or not request.is_json:
            return jsonify({"errors": [{"message": "send API-Version: 2025-04 and a JSON body"}]}), 400
        payload = request.get_json()
        query, variables = payload.get("query") or "", payload.get("variables") or {}
        m = re.match(r"\s*query\s+(\w+)", query)
        op = m.group(1) if m else ""
        self.calls.append((op, variables))
        failure = self._take_failure(op, variables)
        if failure is not None:
            return failure
        handler = getattr(self, f"_op_{op}", None)
        if handler is None:
            return jsonify({"errors": [{"message": f"unknown operation {op!r}"}]}), 200
        result = handler(query, variables)
        if isinstance(result, tuple):
            return result
        return jsonify({"data": result, "account_id": 1})

    def _take_failure(self, op: str, variables: dict):
        for f in self.failures:
            if f["op"] not in (None, op) or (f["board"] and variables.get("board") != f["board"]):
                continue
            f["times"] -= 1
            if f["times"] <= 0:
                self.failures.remove(f)
            return self._failure(f)
        return None

    @staticmethod
    def _failure(f: dict):
        kind = f["kind"]
        if kind == "complexity":
            return jsonify({"errors": [{"message": "Complexity budget exhausted",
                                        "extensions": {"code": "COMPLEXITY_BUDGET_EXHAUSTED", "complexity": 30001,
                                                       "complexity_budget_left": 1000,
                                                       "retry_in_seconds": f.get("retry", 7)}}],
                            "account_id": 1}), 200
        if kind == "complexity_old":
            return jsonify({"error_code": "ComplexityException", "status_code": 429,
                            "error_message": "Complexity budget exhausted, query cost 30001 budget remaining 1000 "
                                             f"out of 1000000 reset in {f.get('retry', 12)} seconds"}), 200
        if kind == "429":
            resp = jsonify({"errors": [{"message": "Rate limit exceeded",
                                        "extensions": {"code": "RATE_LIMIT_EXCEEDED"}}]})
            if f.get("retry_after") is not None:
                resp.headers["Retry-After"] = str(f["retry_after"])
            return resp, 429
        if kind == "daily":
            return jsonify({"errors": [{"message": "Daily limit exceeded",
                                        "extensions": {"code": "DAILY_LIMIT_EXCEEDED"}}]}), 429
        if kind == "502":
            return "<html><body>Bad gateway</body></html>", 502, {"Content-Type": "text/html"}
        return jsonify({"errors": [{"message": f.get("message", "Internal server error"),
                                    "extensions": {"code": "INTERNAL_SERVER_ERROR"}}]}), 200

    # ---- the operations ---------------------------------------------------------------------- #
    def _find_board(self, bid) -> dict | None:
        return next((b for b in self.data["boards"] if b["id"] == str(bid)), None)

    def _op_Account(self, q, v):
        return {"account": ACCOUNT}

    def _op_Users(self, q, v):
        return {"users": _page(USERS, int(v["page"]), _limit(q, "users"))}

    def _op_InactiveUsers(self, q, v):
        assert "non_active: true" in q
        return {"users": _page(INACTIVE_USERS, int(v["page"]), _limit(q, "users"))}

    def _op_Teams(self, q, v):
        return {"teams": TEAMS}

    def _op_Workspaces(self, q, v):
        return {"workspaces": _page(WORKSPACES, int(v["page"]), _limit(q, "workspaces"))}

    def _op_Folders(self, q, v):
        return {"folders": _page(FOLDERS, int(v["page"]), _limit(q, "folders"))}

    def _op_Boards(self, q, v):
        view_settings = re.search(r"views \{[^}]*settings_str", q) is not None
        if view_settings and self.reject_view_settings:
            return jsonify({"errors": [{"message": "Cannot query field \"settings_str\" on type \"BoardView\".",
                                        "extensions": {"code": "GRAPHQL_VALIDATION_FAILED"}}]}), 200
        boards = [b for b in self.data["boards"] if not (self.hide_subitem_board and b["id"] == SUB_BOARD)]
        out = []
        for b in _page(boards, int(v["page"]), _limit(q, "boards")):
            b = copy.deepcopy(b)
            if not view_settings:
                for view in b["views"]:
                    view.pop("settings_str", None)
            out.append(b)
        return {"boards": out}

    def _items_page(self, bid: str, offset: int, limit: int) -> dict:
        items = self.data["items"].get(bid, [])
        end = offset + limit
        return {"cursor": f"cursor-{bid}-{end}" if end < len(items) else None, "items": items[offset:end]}

    def _op_BoardItems(self, q, v):
        b = self._find_board(v["board"])
        if b is None or b["id"] in self.gone_boards:
            return {"boards": []}
        return {"boards": [{"id": b["id"], "items_page": self._items_page(b["id"], 0, _limit(q, "items_page"))}]}

    def _op_NextItems(self, q, v):
        _, bid, offset = v["cursor"].split("-")
        return {"next_items_page": self._items_page(bid, int(offset), _limit(q, "next_items_page"))}

    def _op_BoardUpdates(self, q, v):
        b = self._find_board(v["board"])
        if b is None:
            return {"boards": []}
        page = _page(self.data["updates"].get(b["id"], []), int(v["page"]), _limit(q, "updates"))
        page = [u for u in page if u["id"] != HIDDEN_UPDATE]
        return {"boards": [{"id": b["id"], "updates": page}]}

    def _op_SubitemColumns(self, q, v):
        b = self._find_board(v["board"])
        return {"boards": [{"id": b["id"], "columns": b["columns"]}] if b else []}

    # ---- running it -------------------------------------------------------------------------- #
    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_port}/v2"

    def __enter__(self):
        self.server = make_server("127.0.0.1", 0, self.app, threaded=True)
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        return self

    def __exit__(self, *a):
        self.server.shutdown()
        self.server.server_close()
