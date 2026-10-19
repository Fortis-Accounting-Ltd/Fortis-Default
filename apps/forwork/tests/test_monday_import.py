"""Loading a monday.com export: every column type, labels and colours, links, mirrors, updates, views and automations.
Synthetic data only (the repository is public)."""
import io
import time
import zipfile

import pytest

from forwork import automations
from forwork.monday_bundle import BundleWriter
from forwork.monday_import import import_bundle
from forwork.service import Service

STATUS_SETTINGS = {
    "done_colors": [1], "deactivated_labels": [9],
    "labels": {"0": "Working on it", "1": "Done", "2": "Stuck", "3": "To Complete", "9": "Old label", "5": ""},
    "labels_positions_v2": {"3": 0, "0": 1, "1": 2, "2": 3, "9": 4, "5": 5},
    "labels_colors": {"0": {"color": "#fdab3d"}, "1": {"color": "#00c875"}, "2": {"color": "#df2f4a"}, "3": {"color": "#7f5347"},
                      "9": {"color": "#333333"}},
}


def v(type_, value=None, text="", **extra):
    return {"type": type_, "text": text, "value": value, **extra}


def build(folder):
    w = BundleWriter(folder)
    w.write_users([{"id": "1", "name": "Alex Citizen", "email": "alex@example.com.au", "active": True},
                   {"id": "2", "name": "Jo Bloggs", "email": "jo@example.com.au", "active": True},
                   {"id": "3", "name": "Deleted member", "email": "3@deleted.user", "active": False}],
                  [{"id": "50", "name": "Managers", "user_ids": ["1"]}])
    w.write_workspaces([{"id": "100", "name": "Main workspace"}, {"id": "101", "name": "SMSF"}],
                       [{"id": "200", "name": "2026", "workspace_id": "101", "parent_id": None}])
    w.write_board({
        "board": {"id": "10", "name": "Clients", "state": "active", "workspace_id": "100", "folder_id": None, "owner_ids": ["1"]},
        "groups": [{"id": "g1", "title": "Active", "color": "#00c875", "position": "1"}],
        "columns": [{"id": "name", "title": "Client", "type": "name", "settings": {}},
                    {"id": "status", "title": "Stage", "type": "status", "settings": STATUS_SETTINGS}],
        "items": [{"id": "1001", "name": "Example Pty Ltd", "state": "active", "group_id": "g1", "creator_id": "1",
                   "created_at": "2026-07-01T00:00:00Z", "updated_at": "2026-07-02T00:00:00Z",
                   "values": {"status": v("status", {"index": 1}, "Done")}, "subitems": []}],
        "updates": [],
    })
    w.write_board({
        "board": {"id": "20", "name": "Annual Compliance", "state": "active", "workspace_id": "101", "folder_id": "200",
                  "description": "This year's returns"},
        "groups": [{"id": "todo", "title": "To Complete", "color": "#7f5347", "position": "2"},
                   {"id": "wip", "title": "WIP", "color": "#fdab3d", "position": "1"},
                   {"id": "old", "title": "Last year", "color": "#c4c4c4", "position": "3", "archived": True},
                   {"id": "gone", "title": "Deleted", "color": "#c4c4c4", "position": "4", "deleted": True}],
        "columns": [
            {"id": "name", "title": "Name", "type": "name", "settings": {}},
            {"id": "subitems", "title": "Subitems", "type": "subtasks", "settings": {"boardIds": [21]}},
            {"id": "status", "title": "Status", "type": "status", "settings": STATUS_SETTINGS},
            {"id": "person", "title": "Manager", "type": "people", "settings": {"max_people_allowed": "1"}},
            {"id": "team", "title": "Team", "type": "people", "settings": {}},
            {"id": "due", "title": "Due date", "type": "date", "settings": {}},
            {"id": "tl", "title": "Timeline", "type": "timeline", "settings": {}},
            {"id": "fee", "title": "Fee", "type": "numbers", "settings": {"unit": {"symbol": "dollar", "direction": "left"}}},
            {"id": "hrs", "title": "Hours", "type": "numbers", "settings": {}},
            {"id": "rate", "title": "Rate", "type": "formula", "settings": {"formula": "{fee}/{hrs}"}},
            {"id": "notes", "title": "Notes", "type": "long_text", "settings": {}},
            {"id": "tfn", "title": "Reference", "type": "text", "settings": {}},
            {"id": "sw", "title": "Software", "type": "dropdown", "settings": {"labels": [{"id": 1, "name": "Xero"}, {"id": 2, "name": "MYOB"}]}},
            {"id": "wp", "title": "Workpaper", "type": "link", "settings": {}},
            {"id": "ok", "title": "Signed", "type": "checkbox", "settings": {}},
            {"id": "client", "title": "Client", "type": "board_relation", "settings": {"boardIds": [10]}},
            {"id": "stage", "title": "Client stage", "type": "mirror",
             "settings": {"relation_column": {"client": True}, "displayed_linked_columns": {"10": ["status"]}}},
            {"id": "doc", "title": "Doc", "type": "doc", "settings": {}},
            {"id": "btn", "title": "Go", "type": "button", "settings": {"buttonText": "Send"}},
            {"id": "files", "title": "Files", "type": "file", "settings": {}},
            {"id": "x_old", "title": "Archived column", "type": "text", "archived": True, "settings": {}},
        ],
        "subitem_board_id": "21",
        "subitem_columns": [{"id": "name", "title": "Task", "type": "name", "settings": {}},
                            {"id": "status", "title": "Status", "type": "status", "settings": STATUS_SETTINGS}],
        "views": [
            {"id": "v1", "name": "AC view", "type": "TableBoardView",
             "settings": {"columns": {"column_properties": [{"column_id": "tfn", "visible": False}],
                                      "column_order": ["name", "status", "person", "due"]},
                          "group_by": {"conditions": [{"columnId": "status"}], "hideEmptyGroups": True}},
             "filter": {"groups": [{"rules": [{"column_id": "person", "compare_value": ["assigned_to_me", "person-2"], "operator": "ANY_OF"},
                                              {"column_id": "status", "compare_value": [0, 3], "operator": "ANY_OF"}]}]},
             "sort": [{"column_id": "due", "direction": "DESC"}]},
            {"id": "v2", "name": "Build Vibe view", "type": "FeatureBoardView", "settings": {}},
        ],
        "items": [
            {"id": "2001", "name": "Example Trust", "state": "active", "group_id": "wip", "creator_id": "2",
             "created_at": "2026-07-03T01:02:03.000Z", "updated_at": "2026-07-04T00:00:00Z",
             "values": {
                 "status": v("status", {"index": 0}, "Working on it"),
                 "person": v("people", {"personsAndTeams": [{"id": 2, "kind": "person"}]}, "Jo Bloggs"),
                 "team": v("people", {"personsAndTeams": [{"id": 1, "kind": "person"}, {"id": 3, "kind": "person"},
                                                          {"id": 50, "kind": "team"}, {"id": 777, "kind": "person"}]}),
                 "due": v("date", {"date": "2026-10-31", "time": "03:30:00"}, "2026-10-31"),
                 "tl": v("timeline", {"from": "2026-07-01", "to": "2026-09-30"}),
                 "fee": v("numbers", "1500", "1500"),
                 "hrs": v("numbers", "6", "6"),
                 "notes": v("long_text", {"text": "Waiting on\nbank statements"}),
                 "tfn": v("text", "XX-123", "XX-123"),
                 "sw": v("dropdown", {"ids": [2, 9]}, "MYOB"),
                 "wp": v("link", {"url": "https://example.com/wp", "text": "Workpaper"}),
                 "ok": v("checkbox", {"checked": "true"}),
                 "client": v("board_relation", None, None, linked_item_ids=["1001", "999999"]),
                 "stage": v("mirror", None, None, display_value="Done"),
                 "doc": v("doc", {"files": [{"name": "Engagement letter", "fileType": "MONDAY_DOC"}]}),
                 "btn": v("button", {"clicks": 3}, "Send"),
                 "files": v("file", {"files": [{"name": "scan.pdf", "assetId": 55, "fileType": "ASSET"},
                                               {"name": "On OneDrive", "fileType": "ONE_DRIVE", "linkToFile": "https://example.sharepoint.com/x"}]}),
             },
             "subitems": [{"id": "3001", "name": "Collect documents", "state": "active", "creator_id": "1",
                           "values": {"status": v("status", {"index": 1}, "Done")}},
                          {"id": "3002", "name": "Prepare", "state": "active", "values": {}}]},
            {"id": "2002", "name": "Bad link Pty Ltd", "state": "active", "group_id": "todo",
             "values": {"wp": v("link", {"url": "javascript:alert(1)", "text": "click"}),
                        "status": v("status", {"index": 9}, "Old label")}, "subitems": []},
            {"id": "2003", "name": "Archived job", "state": "archived", "group_id": "gone", "values": {}, "subitems": []},
            {"id": "2004", "name": "In archived group", "state": "active", "group_id": "old", "values": {}, "subitems": []},
        ],
        "updates": [
            {"id": "u1", "item_id": "2001", "creator_id": "1", "created_at": "2026-07-05T00:00:00.000Z",
             "body": '<p><a class="user_mention_editor router" href="https://x.monday.com/users/2-jo" data-mention-type="User" '
                     'data-mention-id="2">@Jo Bloggs</a> please check <img src="https://x/y.png"><script>bad()</script></p>',
             "text_body": "@Jo Bloggs please check", "assets": [{"id": "a1", "name": "statement", "file_extension": "pdf", "file_size": 1234}],
             "replies": [{"id": "r1", "body": "<p>On it</p>", "text_body": "On it", "creator_id": "2", "created_at": "2026-07-05T01:00:00Z"}]},
            {"id": "u2", "item_id": "3001", "creator_id": "3", "created_at": "2026-07-06T00:00:00Z", "body": "<p>Subitem note</p>",
             "text_body": "Subitem note", "replies": [], "assets": []},
            {"id": "u3", "item_id": "8888", "creator_id": "1", "created_at": "2026-07-06T00:00:00Z", "body": "<p>orphan</p>", "replies": []},
        ],
        "automations": [
            {"when": {"column": "status", "status_index": 1}, "then": {"move_to_group": "todo"}},
            {"when": {"column": "status", "status_index": 0}, "then": {"set_date_today": "due"}},
            {"when": {"column": "nope", "status_index": 0}, "then": {"move_to_group": "todo"}},
        ],
    })
    w.write_board({"board": {"id": "30", "name": "Old year", "state": "archived", "workspace_id": None},
                   "groups": [], "columns": [{"id": "name", "title": "Name", "type": "name", "settings": {}}],
                   "items": [{"id": "4001", "name": "Old item", "state": "active", "group_id": None, "values": {}, "subitems": []}],
                   "updates": []})
    w.write_manifest({"exported_at": "2026-10-18T00:00:00Z", "source": "test", "boards": ["10", "20", "30"]})


@pytest.fixture
def loaded(db, tmp_path):
    build(tmp_path / "bundle")
    svc = Service(db)
    report = import_bundle(svc, tmp_path / "bundle")
    ac = db.one("SELECT * FROM boards WHERE monday_id = '20'")
    cols = {c["title"]: c for c in svc.columns(ac["id"], "item")}
    items = {i["name"]: i for i in svc.board_payload(ac["id"])["items"]}
    return svc, report, ac, cols, items


def test_counts_and_people(loaded):
    svc, r, ac, cols, items = loaded
    assert (r["boards"], r["items"], r["subitems"]) == (3, 6, 2)
    users = {u["email"]: u for u in svc.users(include_inactive=True)}
    assert users["alex@example.com.au"]["active"] and not users["3@deleted.user"]["active"]
    assert users["3@deleted.user"]["name"] == "Former member"
    assert any(u["email"] == "monday-777@former.invalid" for u in users.values())     # someone monday no longer lists
    assert svc.teams()[0]["name"] == "Managers"


def test_workspaces_folders_and_board_states(loaded):
    svc, r, ac, cols, items = loaded
    folders = {f["id"]: f["name"] for f in svc.folders()}
    assert folders[ac["folder_id"]] == "2026" and ac["description"] == "This year's returns"
    old = svc.db.one("SELECT * FROM boards WHERE monday_id = '30'")
    assert old["state"] == "archived"
    assert svc.db.one("SELECT name FROM workspaces WHERE id = ?", (old["workspace_id"],))[0] == "Main workspace"


def test_groups_in_order_with_archived_and_without_deleted(loaded):
    svc, r, ac, cols, items = loaded
    groups = svc.db.q("SELECT title, color, state FROM groups WHERE board_id = ? ORDER BY position", (ac["id"],))
    assert [(g["title"], g["state"]) for g in groups][:3] == [("WIP", "active"), ("To Complete", "active"), ("Last year", "archived")]
    assert "Deleted" not in [g["title"] for g in groups]
    states = dict(svc.db.q("SELECT name, state FROM items WHERE board_id = ?", (ac["id"],)))
    assert states["Archived job"] == "archived" and states["In archived group"] == "archived"


def test_status_labels_keep_order_colour_done_and_hidden(loaded):
    svc, r, ac, cols, items = loaded
    labels = cols["Status"]["settings"]["labels"]
    assert [lab["name"] for lab in labels][:4] == ["To Complete", "Working on it", "Done", "Stuck"]
    by_id = {lab["id"]: lab for lab in labels}
    assert by_id[1]["done"] and by_id[0]["color"] == "#fdab3d"
    assert by_id[9]["hidden"]
    assert 5 not in by_id                                      # unnamed and unused
    assert items["Bad link Pty Ltd"]["values"][cols["Status"]["id"]] == 9


def test_every_value_type(loaded):
    svc, r, ac, cols, items = loaded
    it = items["Example Trust"]
    val = lambda title: it["values"].get(cols[title]["id"])  # noqa: E731
    assert val("Status") == 0
    users = {u["email"]: u["id"] for u in svc.users(include_inactive=True)}
    assert val("Manager") == {"users": [users["jo@example.com.au"]]}
    assert cols["Manager"]["settings"] == {"max_people": 1}
    team = val("Team")
    assert users["alex@example.com.au"] in team["users"] and team["teams"]
    assert val("Due date") == "2026-10-31T03:30:00Z"
    assert val("Timeline") == {"from": "2026-07-01", "to": "2026-09-30"}
    assert val("Fee") == 1500 and cols["Fee"]["settings"]["unit"] == "$"
    assert it["computed"][cols["Rate"]["id"]] == 250
    assert val("Notes") == "Waiting on\nbank statements"
    assert val("Reference") == "XX-123"
    assert val("Software") == [2, 9]                           # an option monday no longer lists is kept, hidden
    assert {lab["id"]: lab.get("hidden", False) for lab in cols["Software"]["settings"]["labels"]} == {1: False, 2: False, 9: True}
    assert val("Workpaper") == {"url": "https://example.com/wp", "text": "Workpaper"}
    assert val("Signed") is True
    assert val("Doc") == {"text": "Engagement letter"}
    assert "Go" in cols and val("Go") is None
    assert "Archived column" not in cols
    files = svc.db.q("SELECT name, link, sha256 FROM files WHERE item_id = ? AND column_id = ?", (it["id"], cols["Files"]["id"]))
    assert {(f["name"], f["link"]) for f in files} == {("scan.pdf", None), ("On OneDrive", "https://example.sharepoint.com/x")}
    assert items["Bad link Pty Ltd"]["values"].get(cols["Workpaper"]["id"]) is None


def test_links_and_mirrors_across_boards(loaded):
    svc, r, ac, cols, items = loaded
    clients = svc.db.one("SELECT id FROM boards WHERE monday_id = '10'")[0]
    assert cols["Client"]["settings"]["board_ids"] == [clients]
    it = items["Example Trust"]
    assert [x["name"] for x in it["computed"][cols["Client"]["id"]]] == ["Example Pty Ltd"]
    assert it["computed"][cols["Client stage"]["id"]] == [{"text": "Done", "color": "#00c875"}]
    assert any("links pointed to items" in w for w in r["warnings"])


def test_subitems_and_updates(loaded):
    svc, r, ac, cols, items = loaded
    it = items["Example Trust"]
    assert [s["name"] for s in it["subitems"]] == ["Collect documents", "Prepare"]
    assert it["computed"][cols["Subitems"]["id"]] == {"total": 2, "done": 1}
    ups = svc.updates(it["id"])
    jo = svc.user_by_email("jo@example.com.au")
    assert f'<span class="mention" data-user="{jo["id"]}">@Jo Bloggs</span>' in ups[0]["body"]
    assert "script" not in ups[0]["body"] and "<img" not in ups[0]["body"]
    assert ups[0]["replies"][0]["text"] == "On it" and ups[0]["author"] == "Alex Citizen"
    assert [f["name"] for f in ups[0]["files"]] == ["statement.pdf"]
    sub = svc.updates(it["subitems"][0]["id"])
    assert sub[0]["text"] == "Subitem note" and sub[0]["author"] == "Former member"
    assert any("1 updates belong to items" in w for w in r["warnings"])
    assert svc.unread_count(jo) == 0                       # imported mentions do not notify again


def test_views_and_automations(loaded):
    svc, r, ac, cols, items = loaded
    views = svc.views(ac["id"])
    assert [v["name"] for v in views] == ["AC view"]
    s = views[0]["settings"]
    assert s["hidden"] == [cols["Reference"]["id"]]
    assert s["order"] == [cols[t]["id"] for t in ("Name", "Status", "Manager", "Due date")]
    assert s["group_by"] == cols["Status"]["id"] and s["hide_empty_groups"]
    jo = svc.user_by_email("jo@example.com.au")["id"]
    assert {"column": cols["Manager"]["id"], "op": "any_of", "values": ["me", jo]} in s["filters"]["rules"]
    assert {"column": cols["Status"]["id"], "op": "any_of", "values": [0, 3]} in s["filters"]["rules"]
    assert s["sort"] == [{"column": cols["Due date"]["id"], "dir": "desc"}]
    assert any("Build Vibe view" in w for w in r["warnings"])
    rules = automations.list_rules(svc, ac["id"])
    assert [x["text"] for x in rules] == ["When Status changes to Done, move the item to To Complete",
                                          "When Status changes to Working on it, set Due date to today"]
    assert all(x["imported"] for x in rules)
    # and they run
    item = items["Example Trust"]["id"]
    svc.set_value(None, item, cols["Status"]["id"], "Done")
    assert svc.db.one("SELECT title FROM groups WHERE id = ?", (svc.item(item)["group_id"],))[0] == "To Complete"


def test_import_again_skips_or_replaces(loaded, tmp_path):
    svc, r, ac, cols, items = loaded
    again = import_bundle(svc, tmp_path / "bundle")
    assert again["boards"] == 0 and len(again["skipped_boards"]) == 3
    replaced = import_bundle(svc, tmp_path / "bundle", replace=True)
    assert replaced["boards"] == 3
    assert svc.db.scalar("SELECT COUNT(*) FROM boards WHERE monday_id = '20'") == 1
    assert svc.db.scalar("SELECT COUNT(*) FROM updates") == 3       # u1, its reply, u2 (u3's item does not exist)


def test_admin_imports_an_uploaded_zip(tmp_path):
    from test_web import make_cfg, signed_in
    from forwork.db import Database
    from forwork.web import create_app
    build(tmp_path / "bundle")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for p in (tmp_path / "bundle").rglob("*.json"):
            z.write(p, "export/" + str(p.relative_to(tmp_path / "bundle")))
    buf.seek(0)
    cfg = make_cfg(tmp_path)
    db = Database(cfg.db_path)
    app = create_app(cfg, db=db)
    svc = Service(db)
    admin = svc.create_user(None, "alex@example.com.au", "Alex Citizen", role="admin")
    c = signed_in({"app": app, "db": db}, admin)
    r = c.post("/admin/import", data={"_csrf": c.csrf, "bundle": (buf, "export.zip")}, content_type="multipart/form-data")
    job = r.headers["Location"].split("job=")[1]
    for _ in range(100):
        j = c.get(f"/admin/import/jobs/{job}").get_json()
        if j["status"] != "running":
            break
        time.sleep(0.05)
    assert j["status"] == "done", j
    assert j["report"]["boards"] == 3
