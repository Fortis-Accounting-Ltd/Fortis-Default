"""Pulling from monday.com - end to end against a stand-in for its GraphQL API, with made-up data only."""
import json
import re
import zipfile

import pytest

from forwork.monday_bundle import FORMAT, Bundle
from forwork.monday_pull import MondayClient, MondayError, main_pull, pull, zip_bundle

from fake_monday import (ARCHIVED_BOARD, ARCHIVED_UPDATES, HIDDEN_UPDATE, MAIN_BOARD, MAIN_ITEMS, MAIN_UPDATES,
                         SUB_BOARD, SUB_UPDATES, TOKEN, FakeMonday)

MAIN_SUBITEMS = (MAIN_ITEMS // 10) * 2


class Sleeps(list):
    """Stands in for time.sleep and remembers how long each wait would have been."""

    def __call__(self, seconds):
        self.append(seconds)


@pytest.fixture
def fake():
    with FakeMonday() as f:
        yield f


@pytest.fixture
def sleeps():
    return Sleeps()


def run_pull(fake, dest, sleeps, **kw):
    return pull(TOKEN, dest, endpoint=fake.url, sleep=sleeps, **kw)


def test_full_pull_writes_a_bundle_that_reads_back(fake, tmp_path, sleeps):
    folder = tmp_path / "export"
    manifest = run_pull(fake, folder, sleeps)
    assert sleeps == []

    b = Bundle(folder)
    assert b.manifest == manifest
    assert manifest["format"] == FORMAT and manifest["source"] == "monday-api"
    assert manifest["account"] == {"name": "Example Accounting", "slug": "example-accounting"}
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", manifest["exported_at"])
    assert manifest["boards"] == [MAIN_BOARD, ARCHIVED_BOARD] == b.board_ids()
    assert manifest["counts"] == {
        "boards": 2, "items": MAIN_ITEMS + 3, "subitems": MAIN_SUBITEMS,
        "updates": (MAIN_UPDATES - 1) + SUB_UPDATES + ARCHIVED_UPDATES,
        "users": 4, "teams": 1, "workspaces": 2, "folders": 1}
    assert "skipped_boards" not in manifest

    users = b.users()
    assert [(u["id"], u["name"], u["active"]) for u in users["users"]] == [
        ("1001", "Alex Citizen", True), ("1002", "Sam Sample", True), ("1003", "Jordan Example", True),
        ("1004", "Casey Former", False)]
    assert users["users"][0] == {"id": "1001", "name": "Alex Citizen", "email": "alex.citizen@example.com.au",
                                 "title": "Partner", "time_zone_identifier": "Australia/Sydney", "active": True}
    assert users["users"][2]["title"] == ""
    assert users["teams"] == [{"id": "2001", "name": "Workflow Managers", "user_ids": ["1001", "1002"]}]

    ws = b.workspaces()
    assert ws["workspaces"][0] == {"id": "3001", "name": "Main workspace", "kind": "open", "description": ""}
    assert ws["folders"] == [{"id": "4001", "name": "2026", "workspace_id": "3002", "parent_id": None}]


def test_board_contents(fake, tmp_path, sleeps):
    run_pull(fake, tmp_path / "export", sleeps)
    main = Bundle(tmp_path / "export").board(MAIN_BOARD)

    assert main["board"] == {"id": MAIN_BOARD, "name": "Annual Compliance 2026-2027", "state": "active",
                             "board_kind": "public", "description": "Tax returns", "workspace_id": "3002",
                             "folder_id": "4001", "updated_at": "2026-10-01T00:00:00Z", "owner_ids": ["1001", "1002"]}
    assert [g["id"] for g in main["groups"]] == ["new_jobs", "in_progress", "completed"]
    assert main["groups"][0] == {"id": "new_jobs", "title": "New jobs", "color": "#579bfc", "position": "65536",
                                 "archived": False, "deleted": False}
    cols = {c["id"]: c for c in main["columns"]}
    assert cols["status"]["settings"]["labels"]["1"] == "Done"
    assert cols["status"]["description"] == "Where the job is up to" and cols["status"]["width"] == 140
    assert cols["person"]["settings"] == {}                     # empty settings_str
    assert cols["old_notes"]["archived"] is True
    assert cols["subitems"]["type"] == "subtasks"
    assert main["views"] == [{"id": "v1", "name": "Main table", "type": "table", "settings": {"filters": []}},
                             {"id": "v2", "name": "By accountant", "type": "kanban", "settings": {}}]
    assert "automations" not in main

    # every item, in board order, across three pages
    items = main["items"]
    assert [i["id"] for i in items] == [str(600000 + n) for n in range(MAIN_ITEMS)]
    first = items[0]
    assert first["name"] == "Example Client 0001 - Tax return" and first["group_id"] == "new_jobs"
    assert first["creator_id"] == "1001" and first["state"] == "active"
    v = first["values"]
    assert v["status"] == {"type": "status", "text": "Working on it",
                           "value": {"index": 0, "changed_at": "2026-09-01T00:00:00.000Z"}}
    assert v["numbers"]["value"] == "100"
    assert v["person"]["value"] == {"personsAndTeams": [{"id": 1001, "kind": "person"}]}
    assert v["connect_boards"]["linked_item_ids"] == ["7000"]
    assert v["mirror"] == {"type": "mirror", "text": None, "value": None, "display_value": "00 000 000 000"}
    assert v["formula"]["display_value"] == "110.00"
    assert v["old_notes"]["value"] is None
    assert "linked_item_ids" not in v["status"] and "display_value" not in v["status"]
    assert items[1]["group_id"] == "in_progress"

    # subitems, and their board's columns
    assert main["subitem_board_id"] == SUB_BOARD
    assert [c["id"] for c in main["subitem_columns"]] == ["name", "status_1", "date_1"]
    assert main["subitem_columns"][1]["settings"] == {"labels": {"1": "Done"}}
    assert [s["name"] for s in first["subitems"]] == ["Gather documents", "Lodge return"]
    assert first["subitems"][0] == {"id": "800000", "name": "Gather documents", "state": "active",
                                    "created_at": "2026-07-02T00:00:00Z", "updated_at": "2026-07-03T00:00:00Z",
                                    "creator_id": "1002",
                                    "values": {"status_1": {"type": "status", "text": "Done", "value": {"index": 1}},
                                               "date_1": {"type": "date", "text": "2026-08-01",
                                                          "value": {"date": "2026-08-01"}}}}
    assert items[1]["subitems"] == []
    assert sum(len(i["subitems"]) for i in items) == MAIN_SUBITEMS

    # updates: the board's own (past the short first page) and then its subitems'
    updates = main["updates"]
    ids = [u["id"] for u in updates]
    assert len(ids) == len(set(ids)) == (MAIN_UPDATES - 1) + SUB_UPDATES
    assert HIDDEN_UPDATE not in ids
    assert ids[MAIN_UPDATES - 2] == str(900000 + MAIN_UPDATES - 1)       # the last page of the board's own
    subitem_ids = {s["id"] for i in items for s in i["subitems"]}
    sub_updates = updates[MAIN_UPDATES - 1:]
    assert len(sub_updates) == SUB_UPDATES and all(u["item_id"] in subitem_ids for u in sub_updates)
    u0 = updates[0]
    assert u0["item_id"] == "600000" and u0["creator_id"] == "1002"
    assert u0["replies"] == [{"id": "1400000", "body": "<p>Thanks Alex.</p>", "text_body": "Thanks Alex.",
                              "created_at": "2026-09-30T03:00:00Z", "creator_id": "1001"}]
    assert u0["assets"] == [{"id": "1600000", "name": "engagement-letter.pdf", "file_extension": ".pdf",
                             "file_size": 12345}]
    assert updates[1]["replies"] == [] and updates[1]["assets"] == []


def test_archived_boards_come_across_and_others_do_not(fake, tmp_path, sleeps):
    folder = tmp_path / "export"
    run_pull(fake, folder, sleeps)
    archived = Bundle(folder).board(ARCHIVED_BOARD)
    assert archived["board"]["state"] == "archived"
    assert archived["board"]["workspace_id"] is None and archived["board"]["folder_id"] is None
    assert [i["name"] for i in archived["items"]] == ["Example Pty Ltd", "Sample Family Trust", "Citizen Super Fund"]
    assert archived["subitem_board_id"] is None and archived["subitem_columns"] == []
    assert len(archived["updates"]) == ARCHIVED_UPDATES
    # the subitem board, the deleted board, the doc and the custom object are not boards of their own
    assert sorted(p.name for p in (folder / "boards").iterdir()) == [f"{MAIN_BOARD}.json", f"{ARCHIVED_BOARD}.json"]
    assert {v["board"] for v in fake.ops("BoardItems")} == {MAIN_BOARD, ARCHIVED_BOARD}


def test_paging(fake, tmp_path, sleeps):
    run_pull(fake, tmp_path / "export", sleeps)
    assert [v["cursor"] for v in fake.ops("NextItems")] == [f"cursor-{MAIN_BOARD}-500", f"cursor-{MAIN_BOARD}-1000"]
    # pages 1-3 of the board's updates, then the empty page 4 - the 499 on page 1 did not stop it
    assert [v["page"] for v in fake.ops("BoardUpdates", board=MAIN_BOARD)] == [1, 2, 3, 4]
    assert [v["page"] for v in fake.ops("BoardUpdates", board=SUB_BOARD)] == [1, 2]
    assert [v["page"] for v in fake.ops("Boards")] == [1, 2]
    assert [v["page"] for v in fake.ops("Users")] == [1, 2]
    assert [v["page"] for v in fake.ops("InactiveUsers")] == [1, 2]
    assert fake.ops("SubitemColumns") == []      # the subitem board was in the boards list


def test_active_only(fake, tmp_path, sleeps):
    manifest = run_pull(fake, tmp_path / "export", sleeps, include_archived=False)
    assert manifest["boards"] == [MAIN_BOARD]
    assert fake.ops("BoardItems", board=ARCHIVED_BOARD) == []


def test_only_chosen_boards(fake, tmp_path, sleeps):
    manifest = run_pull(fake, tmp_path / "export", sleeps, board_ids=[ARCHIVED_BOARD])
    assert manifest["boards"] == [ARCHIVED_BOARD] and manifest["counts"]["items"] == 3
    assert fake.ops("BoardItems", board=MAIN_BOARD) == []
    assert Bundle(tmp_path / "export").users()["users"]          # users still come across in full

    for ids, kw in ([["5004"], {}], [["5005"], {}], [[SUB_BOARD], {}], [["999"], {}],
                    [[ARCHIVED_BOARD], {"include_archived": False}]):
        with pytest.raises(MondayError) as e:
            run_pull(fake, tmp_path / "other", sleeps, board_ids=ids, **kw)
        assert ids[0] in str(e.value)
    assert not (tmp_path / "other").exists()        # nothing written when the boards asked for are not there


def test_waits_out_a_complexity_limit(fake, tmp_path, sleeps):
    fake.fail("complexity", op="BoardItems", board=MAIN_BOARD, retry=7)
    messages = []
    manifest = run_pull(fake, tmp_path / "export", sleeps, progress=lambda m, d, t: messages.append(m))
    assert sleeps == [7]
    assert manifest["counts"]["items"] == MAIN_ITEMS + 3
    assert len(fake.ops("BoardItems", board=MAIN_BOARD)) == 2
    assert any(m.startswith("Pulling Annual Compliance 2026-2027 (1 of 2) - ") and "waiting 7 seconds" in m
               for m in messages)


@pytest.mark.parametrize("kind, opts, waits", [
    ("429", {"retry_after": 3}, [3]),
    ("429", {}, [30]),                       # nothing said about how long: 30 seconds
    ("complexity_old", {"retry": 12}, [12]),  # older reply shape: "... reset in 12 seconds"
    ("502", {}, [10]),
])
def test_waits_out_rate_limits(fake, tmp_path, sleeps, kind, opts, waits):
    fake.fail(kind, op="NextItems", **opts)
    manifest = run_pull(fake, tmp_path / "export", sleeps)
    assert sleeps == waits
    assert manifest["counts"]["items"] == MAIN_ITEMS + 3


def test_gives_up_after_five_waits(fake, tmp_path, sleeps):
    fake.fail("complexity", op="Teams", times=6, retry=2)
    with pytest.raises(MondayError) as e:
        run_pull(fake, tmp_path / "export", sleeps)
    assert sleeps == [2] * 5
    assert e.value.kind == "limit" and "limit" in str(e.value)
    assert not (tmp_path / "export" / "manifest.json").exists()


def test_daily_limit_is_not_waited_out(fake, tmp_path, sleeps):
    fake.fail("daily", op="Account")
    with pytest.raises(MondayError) as e:
        run_pull(fake, tmp_path / "export", sleeps)
    assert sleeps == [] and "daily limit" in str(e.value)


@pytest.mark.parametrize("style", ["401", "200"])
def test_bad_token(fake, tmp_path, sleeps, style):
    fake.auth_style = style
    bad = "not-the-right-token-1234567890"
    with pytest.raises(MondayError) as e:
        pull(bad, tmp_path / "export", endpoint=fake.url, sleep=sleeps)
    text = str(e.value)
    assert text.startswith("monday.com did not accept the API token - ")
    assert bad not in text and bad not in repr(e.value) and e.value.kind == "auth"
    assert sleeps == []
    assert not (tmp_path / "export").exists()        # nothing written before the token is checked


def test_token_never_shows(fake, tmp_path, sleeps):
    fake.fail("error", op="Teams", message=f"Something went wrong with {TOKEN}")
    with pytest.raises(MondayError) as e:
        run_pull(fake, tmp_path / "export", sleeps)
    assert "Something went wrong" in str(e.value) and TOKEN not in str(e.value)

    client = MondayClient(TOKEN, endpoint=fake.url)
    assert TOKEN not in repr(client)
    for token in ("", "   ", "two words", "line\nbreak"):
        with pytest.raises(MondayError) as e:
            MondayClient(token)
        assert e.value.kind == "auth" and (not token.strip() or token not in str(e.value))


def test_cannot_reach_monday(tmp_path, sleeps):
    with FakeMonday() as f:
        url = f.url
    with pytest.raises(MondayError) as e:
        pull(TOKEN, tmp_path / "export", endpoint=url, sleep=sleeps)
    assert e.value.kind == "network" and "could not reach monday.com" in str(e.value)
    assert sleeps == [10, 10]


def test_view_settings_left_out_when_not_offered(fake, tmp_path, sleeps):
    fake.reject_view_settings = True
    run_pull(fake, tmp_path / "export", sleeps)
    main = Bundle(tmp_path / "export").board(MAIN_BOARD)
    assert [(v["id"], v["settings"]) for v in main["views"]] == [("v1", None), ("v2", None)]
    assert {c["id"]: c for c in main["columns"]}["status"]["settings"]["labels"]["0"] == "Working on it"
    assert len(fake.ops("Boards")) == 3              # page 1 refused, then pages 1 and 2 without


def test_subitem_board_missing_from_the_list(fake, tmp_path, sleeps):
    fake.hide_subitem_board = True
    run_pull(fake, tmp_path / "export", sleeps)
    main = Bundle(tmp_path / "export").board(MAIN_BOARD)
    assert [c["id"] for c in main["subitem_columns"]] == ["name", "status_1", "date_1"]
    assert fake.ops("SubitemColumns") == [{"board": SUB_BOARD}]


def test_progress(fake, tmp_path, sleeps):
    calls = []
    run_pull(fake, tmp_path / "export", sleeps, progress=lambda m, d, t: calls.append((m, d, t)))
    assert calls[0] == ("Connecting to monday.com", 0, 0)
    assert ("Pulling Annual Compliance 2026-2027 (1 of 2)", 0, 2) in calls
    assert ("Pulling Annual Compliance 2026-2027 (1 of 2) - 1,000 items so far", 0, 2) in calls
    assert ("Pulling Annual Compliance 2026-2027 (1 of 2) - 1,200 items so far", 0, 2) in calls
    assert ("Pulling Client List (2 of 2)", 1, 2) in calls
    assert calls[-1] == ("Finished - 2 boards, 1,203 items and 1,151 updates", 2, 2)
    done = [d for _, d, t in calls if t]
    assert done == sorted(done)


def test_stopping_part_way_keeps_finished_boards(fake, tmp_path, sleeps):
    folder = tmp_path / "export"
    fake.fail("error", op="BoardItems", board=ARCHIVED_BOARD, message="Internal server error")
    with pytest.raises(MondayError) as e:
        run_pull(fake, folder, sleeps)
    assert "Internal server error" in str(e.value)
    assert (folder / "boards" / f"{MAIN_BOARD}.json").is_file()
    assert not (folder / "boards" / f"{ARCHIVED_BOARD}.json").exists()
    assert not (folder / "manifest.json").exists()

    # pulling again into the same folder replaces the earlier export
    (folder / "boards" / "999.json").write_text("{}")
    manifest = run_pull(fake, folder, sleeps, board_ids=[ARCHIVED_BOARD])
    assert Bundle(folder).board_ids() == manifest["boards"] == [ARCHIVED_BOARD]


def test_board_deleted_during_the_pull(fake, tmp_path, sleeps):
    fake.gone_boards.add(ARCHIVED_BOARD)
    manifest = run_pull(fake, tmp_path / "export", sleeps)
    assert manifest["boards"] == [MAIN_BOARD] == Bundle(tmp_path / "export").board_ids()
    assert manifest["skipped_boards"] == [{"id": ARCHIVED_BOARD, "name": "Client List"}]
    assert manifest["counts"]["boards"] == 1


def test_will_not_overwrite_another_folder(fake, tmp_path, sleeps):
    folder = tmp_path / "somewhere"
    folder.mkdir()
    (folder / "manifest.json").write_text(json.dumps({"name": "something else"}))
    with pytest.raises(MondayError) as e:
        run_pull(fake, folder, sleeps)
    assert "choose an empty folder" in str(e.value)
    assert json.loads((folder / "manifest.json").read_text()) == {"name": "something else"}


def test_zip_round_trip(fake, tmp_path, sleeps):
    folder = tmp_path / "export"
    manifest = run_pull(fake, folder, sleeps)
    (folder / "notes.txt").write_text("not part of the export")
    path = zip_bundle(folder, tmp_path / "zips" / "monday-export.zip")
    with zipfile.ZipFile(path) as z:
        assert sorted(z.namelist()) == ["boards/5001.json", "boards/5003.json", "manifest.json", "users.json",
                                        "workspaces.json"]
    zipped, unzipped = Bundle(path), Bundle(folder)
    try:
        assert zipped.manifest == manifest
        assert zipped.board_ids() == [MAIN_BOARD, ARCHIVED_BOARD]
        assert zipped.board(MAIN_BOARD) == unzipped.board(MAIN_BOARD)
        assert zipped.users() == unzipped.users() and zipped.workspaces() == unzipped.workspaces()
    finally:
        zipped.close()


def test_command_line(fake, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("TEST_MONDAY_TOKEN", TOKEN)
    out = tmp_path / "monday-export"
    rc = main_pull(["--token-env", "TEST_MONDAY_TOKEN", "--out", str(out), "--zip", "--board", ARCHIVED_BOARD,
                    "--endpoint", fake.url])
    printed = capsys.readouterr()
    assert rc == 0, printed.err
    assert "Pulling Client List (1 of 1)" in printed.out
    assert TOKEN not in printed.out + printed.err
    b = Bundle(tmp_path / "monday-export.zip")
    try:
        assert b.board_ids() == [ARCHIVED_BOARD]
    finally:
        b.close()

    rc = main_pull(["--token-env", "TEST_MONDAY_TOKEN", "--out", str(out), "--active-only", "--board", ARCHIVED_BOARD,
                    "--endpoint", fake.url])
    assert rc == 1 and "Not finished" in capsys.readouterr().err
    assert Bundle(out).board_ids() == [ARCHIVED_BOARD]           # the earlier export is still there

    monkeypatch.delenv("TEST_MONDAY_TOKEN")
    assert main_pull(["--token-env", "TEST_MONDAY_TOKEN", "--out", str(tmp_path / "other")]) == 2
    assert "TEST_MONDAY_TOKEN" in capsys.readouterr().err
    assert not (tmp_path / "other").exists()
