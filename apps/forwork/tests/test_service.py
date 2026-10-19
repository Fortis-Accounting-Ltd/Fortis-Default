"""The work service: boards, columns, items, values, automations, updates, views and the change counters."""
import pytest

from forwork import automations
from forwork.model import eval_formula, FormulaError
from forwork.service import Forbidden, Invalid, Service


@pytest.fixture
def svc(db):
    return Service(db)


@pytest.fixture
def admin(svc):
    return svc.create_user(None, "alex@example.com.au", "Alex Citizen", role="admin")


@pytest.fixture
def board(svc, admin):
    ws = svc.create_workspace(admin, "Main workspace")
    bid = svc.create_board(admin, "Annual Compliance", ws)
    return bid


def col(svc, bid, title):
    return next(c for c in svc.columns(bid) if c["title"] == title)


def test_new_board_has_usual_columns_and_a_group(svc, board):
    p = svc.board_payload(board)
    assert [c["title"] for c in p["columns"] if c["scope"] == "item"] == ["Name", "Owner", "Status", "Due date"]
    assert len(p["groups"]) == 1 and p["items"] == []


def test_values_are_checked_and_shown(svc, admin, board):
    iid = svc.create_item(admin, board, "Example Pty Ltd")
    status, owner, due = col(svc, board, "Status"), col(svc, board, "Owner"), col(svc, board, "Due date")
    svc.set_value(admin, iid, status["id"], "Done")
    svc.set_value(admin, iid, owner["id"], {"users": [admin["id"]]})
    svc.set_value(admin, iid, due["id"], "28/10/2026")
    item = svc.board_payload(board)["items"][0]
    assert item["values"][status["id"]] == 1
    assert item["values"][due["id"]] == "2026-10-28"
    with pytest.raises(Invalid, match="not one of"):
        svc.set_value(admin, iid, status["id"], "Nope")
    with pytest.raises(Invalid, match="not a date"):
        svc.set_value(admin, iid, due["id"], "someday")
    with pytest.raises(Invalid, match="not in ForWork"):
        svc.set_value(admin, iid, owner["id"], {"users": [999]})
    texts = dict(svc.db.q("SELECT column_id, text FROM item_values WHERE item_id = ?", (iid,)))
    assert texts[status["id"]] == "Done" and texts[owner["id"]] == "Alex Citizen" and texts[due["id"]] == "28 Oct 2026"


def test_change_counter_and_changes_feed(svc, admin, board):
    iid = svc.create_item(admin, board, "One")
    seq = svc.board(board)["seq"]
    other = svc.create_item(admin, board, "Two")
    ch = svc.changes(board, seq)
    assert not ch["reload"] and [i["id"] for i in ch["items"]] == [other]
    seq = ch["seq"]
    svc.set_item_state(admin, iid, "archived")
    ch = svc.changes(board, seq)
    assert ch["removed"] == [iid]
    svc.add_column(admin, board, "text", "Notes")
    assert svc.changes(board, ch["seq"])["reload"]


def test_status_change_moves_item_and_stamps_date(svc, admin, board):
    review = svc.add_group(admin, board, "Review", where="bottom")
    status, due = col(svc, board, "Status"), col(svc, board, "Due date")
    automations.save_rule(svc, admin, board, {"type": "status_change", "column": status["id"], "to": 0},
                          {"type": "move_to_group", "group": review})
    automations.save_rule(svc, admin, board, {"type": "status_change", "column": status["id"], "to": 0},
                          {"type": "set_date_today", "column": due["id"]})
    iid = svc.create_item(admin, board, "Example Trust")
    changed = svc.set_value(admin, iid, status["id"], "Working on it")
    assert iid in changed
    it = svc.item(iid)
    assert it["group_id"] == review
    assert svc.db.scalar("SELECT value FROM item_values WHERE item_id = ? AND column_id = ?", (iid, due["id"]))
    log = svc.activity(item_id=iid)
    assert any(a["action"] == "item.move" and a["detail"] == {"automation": 1} for a in log)
    rules = automations.list_rules(svc, board)
    assert rules[0]["text"] == "When Status changes to Working on it, move the item to Review"


def test_automations_cannot_loop(svc, admin, board):
    status = col(svc, board, "Status")
    automations.save_rule(svc, admin, board, {"type": "status_change", "column": status["id"], "to": 0},
                          {"type": "set_status", "column": status["id"], "label": 2})
    automations.save_rule(svc, admin, board, {"type": "status_change", "column": status["id"], "to": 2},
                          {"type": "set_status", "column": status["id"], "label": 0})
    iid = svc.create_item(admin, board, "Loop")
    svc.set_value(admin, iid, status["id"], 0)               # stops after three levels, no error
    assert svc.db.scalar("SELECT COUNT(*) FROM activity WHERE item_id = ? AND action = 'item.value'", (iid,)) <= 5


def test_viewers_cannot_change_anything(svc, admin, board):
    viewer = svc.create_user(admin, "sam@example.com.au", "Sam Viewer", role="viewer")
    with pytest.raises(Forbidden):
        svc.create_item(viewer, board, "No")


def test_last_admin_cannot_be_removed(svc, admin):
    with pytest.raises(Invalid, match="at least one admin"):
        svc.update_user(admin, admin["id"], role="member")


def test_updates_mentions_and_replies_notify(svc, admin, board):
    jo = svc.create_user(admin, "jo@example.com.au", "Jo Bloggs")
    iid = svc.create_item(admin, board, "Example Pty Ltd")
    uid = svc.add_update(admin, iid, f'<p>Please check <span class="mention" data-user="{jo["id"]}">@Jo</span>'
                                     f'<script>alert(1)</script></p>')
    ups = svc.updates(iid)
    assert "script" not in ups[0]["body"] and "alert" not in ups[0]["body"]
    assert svc.unread_count(jo) == 1
    svc.add_update(jo, iid, "<p>Done</p>", parent_id=uid)
    assert svc.unread_count(admin) == 1                     # a reply to admin's update
    assert svc.updates(iid)[0]["replies"][0]["text"] == "Done"


def test_assigning_someone_notifies_them(svc, admin, board):
    jo = svc.create_user(admin, "jo@example.com.au", "Jo Bloggs")
    iid = svc.create_item(admin, board, "Example Pty Ltd")
    svc.set_value(admin, iid, col(svc, board, "Owner")["id"], {"users": [jo["id"]]})
    n = svc.notifications(jo)
    assert n[0]["kind"] == "assigned" and "Example Pty Ltd" in n[0]["text"]


def test_my_work_lists_assigned_items_not_done(svc, admin, board):
    owner, status = col(svc, board, "Owner"), col(svc, board, "Status")
    a = svc.create_item(admin, board, "Open job", values={owner["id"]: {"users": [admin["id"]]}})
    b = svc.create_item(admin, board, "Done job", values={owner["id"]: {"users": [admin["id"]]}, status["id"]: "Done"})
    ids = [x["id"] for x in svc.my_work(admin)]
    assert a in ids and b not in ids
    assert b in [x["id"] for x in svc.my_work(admin, include_done=True)]


def test_subitems_progress_and_payload(svc, admin, board):
    iid = svc.create_item(admin, board, "Parent")
    sub_status = next(c for c in svc.columns(board, "subitem") if c["type"] == "status")
    with svc.db.write() as c:                                 # imported boards have a Subitems column
        subtasks = svc._insert_column(c, board, "subtasks", "Subitems", {}, 0.5)
    s1 = svc.create_item(admin, board, "Step 1", parent_id=iid)
    svc.create_item(admin, board, "Step 2", parent_id=iid)
    svc.set_value(admin, s1, sub_status["id"], "Done")
    p = svc.board_payload(board)
    assert [s["name"] for s in p["items"][0]["subitems"]] == ["Step 1", "Step 2"]
    assert p["items"][0]["computed"][subtasks] == {"total": 2, "done": 1}
    with pytest.raises(Invalid, match="same board"):
        svc.create_item(admin, board, "Sub of sub", parent_id=s1)


def test_status_labels_in_use_are_hidden_not_lost(svc, admin, board):
    status = col(svc, board, "Status")
    iid = svc.create_item(admin, board, "X", values={status["id"]: "Stuck"})
    svc.update_column(admin, status["id"], settings={"labels": [{"id": 0, "name": "Working", "color": "#fdab3d"}]})
    labels = {lab["id"]: lab for lab in svc.column(status["id"])["settings"]["labels"]}
    assert labels[2]["hidden"] and labels[0]["name"] == "Working"
    assert svc.board_payload(board)["items"][0]["values"][status["id"]] == 2
    assert svc.db.scalar("SELECT text FROM item_values WHERE item_id = ?", (iid,)) == "Stuck"


def test_move_and_reorder(svc, admin, board):
    g2 = svc.add_group(admin, board, "Later", where="bottom")
    a = svc.create_item(admin, board, "A")
    b = svc.create_item(admin, board, "B")
    c = svc.create_item(admin, board, "C")
    svc.move_item(admin, c, before=a)
    names = [i["name"] for i in svc.board_payload(board)["items"]]
    assert names == ["C", "A", "B"]
    svc.move_item(admin, b, group_id=g2)
    assert svc.item(b)["group_id"] == g2


def test_duplicate_board_for_next_year(svc, admin, board):
    status = col(svc, board, "Status")
    iid = svc.create_item(admin, board, "Example Pty Ltd", values={status["id"]: "Done"})
    svc.create_item(admin, board, "Sub", parent_id=iid)
    svc.add_update(admin, iid, "<p>note</p>")
    review = svc.add_group(admin, board, "Review")
    automations.save_rule(svc, admin, board, {"type": "status_change", "column": status["id"], "to": 1},
                          {"type": "move_to_group", "group": review})
    nb = svc.duplicate_board(admin, board, "Annual Compliance next year", mode="items")
    p = svc.board_payload(nb)
    assert p["items"][0]["name"] == "Example Pty Ltd" and p["items"][0]["subitems"][0]["name"] == "Sub"
    new_status = next(c for c in p["columns"] if c["title"] == "Status")
    assert p["items"][0]["values"][new_status["id"]] == 1
    assert automations.list_rules(svc, nb)[0]["text"].endswith("move the item to Review")
    assert svc.updates(p["items"][0]["id"]) == []
    s = svc.duplicate_board(admin, board, "Structure only")
    assert svc.board_payload(s)["items"] == []


def test_move_item_to_another_board_keeps_matching_values(svc, admin, board):
    status = col(svc, board, "Status")
    iid = svc.create_item(admin, board, "Example Pty Ltd", values={status["id"]: "Stuck"})
    other = svc.create_board(admin, "Other", svc.workspaces()[0]["id"])
    seq = svc.board(board)["seq"]
    svc.move_item_to_board(admin, iid, other)
    assert svc.changes(board, seq)["removed"] == [iid]
    moved = svc.board_payload(other)["items"][0]
    assert moved["values"][col(svc, other, "Status")["id"]] == 2


def test_formula_and_mirror_columns(svc, admin, board):
    ws = svc.workspaces()[0]["id"]
    clients = svc.create_board(admin, "Clients", ws)
    c_status = col(svc, clients, "Status")
    client = svc.create_item(admin, clients, "Example Pty Ltd", values={c_status["id"]: "Done"})
    rel = svc.add_column(admin, board, "board_relation", "Client", {"board_ids": [clients]})
    mir = svc.add_column(admin, board, "mirror", "Client status", {"relation_column": rel, "targets": {str(clients): c_status["id"]}})
    fee = svc.add_column(admin, board, "numbers", "Fee")
    hrs = svc.add_column(admin, board, "numbers", "Hours")
    rate = svc.add_column(admin, board, "formula", "Rate", {"formula": "ROUND({fee} / {hours}, 2)"})
    iid = svc.create_item(admin, board, "Job", values={rel: [client], fee: "1,000", hrs: 3})
    item = svc.board_payload(board)["items"][0]
    assert item["computed"][mir] == [{"text": "Done", "color": "#00c875"}]
    assert item["computed"][rate] == 333.33
    assert item["computed"][rel][0]["name"] == "Example Pty Ltd"
    with pytest.raises(Invalid, match="connected board"):
        svc.set_value(admin, iid, rel, [iid])


def test_formula_calculator_is_safe():
    assert eval_formula("{a}*{b}", {"a": 10, "b": "0.5"}) == 5
    assert eval_formula("IF({a} > 5, 1, 2)", {"a": 6}) == 1
    assert eval_formula("{missing} + 1", {}) == 1
    for bad in ("__import__('os')", "1 +", "open(1)"):
        with pytest.raises(FormulaError):
            eval_formula(bad, {})


def test_search_finds_names_and_values(svc, admin, board):
    notes = svc.add_column(admin, board, "text", "Notes")
    svc.create_item(admin, board, "Example Pty Ltd", values={notes: "waiting on bank statements"})
    assert svc.search("example")[0]["name"] == "Example Pty Ltd"
    assert svc.search("bank statem")[0]["hit"] == "waiting on bank statements"
    assert svc.search("100%") == []


def test_views_are_saved_and_cleaned(svc, admin, board):
    owner = col(svc, board, "Owner")
    vid = svc.save_view(admin, board, "Alex", settings={"filters": {"people": [admin["id"]], "bogus": 1},
                                                        "sort": [{"column": "name", "dir": "desc"}], "hidden": [owner["id"]]})
    v = svc.views(board)[0]
    assert v["id"] == vid and v["settings"] == {"filters": {"people": [admin["id"]]}, "sort": [{"column": "name", "dir": "desc"}],
                                                "hidden": [owner["id"]]}


def test_archived_boards_are_read_only(svc, admin, board):
    status = col(svc, board, "Status")
    iid = svc.create_item(admin, board, "Example Pty Ltd")
    svc.set_board_state(admin, board, "archived")
    for change in (lambda: svc.set_value(admin, iid, status["id"], "Done"), lambda: svc.rename_item(admin, iid, "X"),
                   lambda: svc.move_item(admin, iid), lambda: svc.duplicate_item(admin, iid),
                   lambda: svc.create_item(admin, board, "New")):
        with pytest.raises(Invalid, match="archived"):
            change()
    svc.set_board_state(admin, board, "active")
    svc.set_value(admin, iid, status["id"], "Done")
