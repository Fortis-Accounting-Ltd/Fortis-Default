"""Backups: snapshots, restore, folder and Graph targets, retention, the schedule and the command line."""
from __future__ import annotations

import datetime as dt
import gzip
import hashlib
import json
import os
import sqlite3
import threading
import time
from collections import Counter
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import pytest

from fake_graph import CLIENT_ID, SECRET, SITE_HOST, SITE_PATH, TENANT, USER, FakeGraph
from forwork import backup as bk
from forwork.backup import BackupError, BackupManager, due, iso, make_snapshot, prune
from forwork.config import Config
from forwork.db import now_iso
from forwork.graph import CHUNK_SIZE, CHUNK_UNIT, GraphClient, GraphError

UTC = dt.timezone.utc
SYD = ZoneInfo("Australia/Sydney")
H = dt.timedelta(hours=1)


def utc(*a) -> dt.datetime:
    return dt.datetime(*a, tzinfo=UTC)


def syd(*a) -> dt.datetime:
    return dt.datetime(*a, tzinfo=SYD).astimezone(UTC)


class Clock:
    def __init__(self, t: dt.datetime):
        self.t = t

    def __call__(self) -> dt.datetime:
        return self.t


def add_board(db, name: str = "Tax returns", items: int = 3) -> int:
    now = now_iso()
    with db.write() as c:
        ws = c.execute("INSERT INTO workspaces (name, created_at) VALUES ('Main', ?)", (now,)).lastrowid
        b = c.execute("INSERT INTO boards (workspace_id, name, created_at, updated_at, seq) VALUES (?, ?, ?, ?, 1)",
                      (ws, name, now, now)).lastrowid
        g = c.execute("INSERT INTO groups (board_id, title) VALUES (?, 'This week')", (b,)).lastrowid
        for i in range(items):
            c.execute("INSERT INTO items (board_id, group_id, name, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                      (b, g, f"Client {i}", now, now))
        c.execute("INSERT INTO activity (board_id, at, action) VALUES (?, ?, 'create_board')", (b, now))
    return b


def rename_board(db, board_id: int, name: str) -> None:
    with db.write() as c:
        c.execute("UPDATE boards SET name = ?, seq = seq + 1 WHERE id = ?", (name, board_id))


def add_file(config: Config, content: bytes) -> str:
    h = hashlib.sha256(content).hexdigest()
    p = config.files_dir / h[:2] / h
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(content)
    return f"files/{h[:2]}/{h}"


def unpack(snapshot: Path, out: Path) -> sqlite3.Connection:
    out.write_bytes(gzip.decompress(snapshot.read_bytes()))
    return sqlite3.connect(out)


@pytest.fixture
def config(db):
    return Config(data_dir=db.path.parent)


@pytest.fixture
def fake():
    f = FakeGraph().start()
    yield f
    f.stop()


def graph_config(db, fake: FakeGraph) -> Config:
    return Config(data_dir=db.path.parent, ms_tenant_id=TENANT, ms_client_id=CLIENT_ID, graph_client_secret=SECRET,
                  ms_authority=fake.authority, graph_base=fake.graph_base)


def client_for(fake: FakeGraph, secret: str = SECRET) -> GraphClient:
    return GraphClient(TENANT, CLIENT_ID, secret, authority=fake.authority, graph_base=fake.graph_base)


# ---- snapshots and restore ------------------------------------------------------------------ #
def test_snapshot_round_trip_while_people_keep_saving(db, tmp_path):
    b = add_board(db, "Tax returns")
    stop = threading.Event()

    def keep_saving():
        while not stop.is_set():
            with db.write() as c:
                c.execute("INSERT INTO activity (board_id, at, action) VALUES (?, ?, 'edit')", (b, now_iso()))
        db.close()

    writer = threading.Thread(target=keep_saving)
    writer.start()
    try:
        snap = make_snapshot(db, tmp_path / "out", utc(2026, 10, 18, 23, 30, 5))
    finally:
        stop.set()
        writer.join(10)
    assert snap.name == "forwork-20261018-233005Z.db.gz"
    assert [p.name for p in snap.parent.iterdir()] == [snap.name]          # no temporary files left behind
    c = unpack(snap, tmp_path / "copy.db")
    assert c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert c.execute("PRAGMA journal_mode").fetchone()[0] == "delete"      # one self-contained file
    assert [r[0] for r in c.execute("SELECT name FROM boards")] == ["Tax returns"]
    assert c.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 3
    c.close()


def test_restore_is_seen_by_another_threads_open_connection(db, config):
    b = add_board(db, "Before")
    mgr = BackupManager(db, config, clock=Clock(utc(2026, 10, 18, 1, 0)))
    snap = make_snapshot(db, config.backups_dir, utc(2026, 10, 18, 0, 0))
    rename_board(db, b, "After")
    bk.save_settings(db, {"folder": "/srv/onedrive/ForWork Backups"})     # made after the snapshot
    seen, ready, go = [], threading.Event(), threading.Event()

    def other_request():
        seen.append(db.conn().execute("SELECT name FROM boards").fetchall()[0][0])
        ready.set()
        go.wait(10)
        seen.append(db.conn().execute("SELECT name FROM boards").fetchall()[0][0])
        db.close()

    t = threading.Thread(target=other_request)
    t.start()
    assert ready.wait(10)
    pre = mgr.restore(snap)
    go.set()
    t.join(10)
    assert seen == ["After", "Before"]
    assert db.scalar("SELECT name FROM boards") == "Before"
    assert pre.name == "pre-restore-20261018-010000Z.db"
    with sqlite3.connect(pre) as c:
        assert c.execute("SELECT name FROM boards").fetchone()[0] == "After"
    # the backup settings describe this server, so a restore keeps them
    assert bk.load_settings(db)["folder"] == "/srv/onedrive/ForWork Backups"
    assert mgr.status()["last_restore"]["file"] == snap.name
    # a second restore in the same second never overwrites the first pre-restore copy
    assert mgr.restore(snap).name == "pre-restore-20261018-010000Z-2.db"
    assert [p.name for p in config.backups_dir.iterdir() if p.name.startswith(".")] == []


def test_restore_refuses_files_that_are_not_good_backups(db, config, tmp_path):
    add_board(db, "Live")
    mgr = BackupManager(db, config)
    junk = tmp_path / "forwork-20261018-000000Z.db.gz"
    junk.write_bytes(gzip.compress(b"not a database at all" * 100))
    with pytest.raises(BackupError, match="not a ForWork database"):
        mgr.restore(junk)
    plain = tmp_path / "notes.db.gz"
    plain.write_bytes(b"this is not gzip")
    with pytest.raises(BackupError, match="could not be unpacked"):
        mgr.restore(plain)

    snap = make_snapshot(db, tmp_path / "snaps", utc(2026, 10, 18))
    c = unpack(snap, tmp_path / "newer.db")
    c.execute("UPDATE meta SET value = '99' WHERE key = 'schema_version'")
    c.commit()
    c.close()
    newer = tmp_path / "newer.db.gz"
    newer.write_bytes(gzip.compress((tmp_path / "newer.db").read_bytes()))
    with pytest.raises(BackupError, match="newer version of ForWork"):
        mgr.restore(newer)
    assert db.scalar("SELECT name FROM boards") == "Live"
    assert not list(config.backups_dir.glob("pre-restore-*"))


# ---- retention ------------------------------------------------------------------------------ #
def snap_name(t: dt.datetime) -> str:
    return f"forwork-{bk.stamp(t)}.db.gz"


def test_prune_keeps_hourly_then_daily_then_monthly():
    now = syd(2026, 10, 18, 12, 0)
    times = [now - i * 6 * H for i in range(4 * 800)]                       # every 6 hours for 800 days
    names = [snap_name(t) for t in times]
    gone = set(prune(names, now))
    kept = sorted(bk.parse_name(n)[0] for n in names if n not in gone)
    today = now.astimezone(SYD).date()

    def day(t):
        return t.astimezone(SYD).date()

    assert {t for t in times if t >= now - 48 * H} <= set(kept)
    older = [t for t in kept if t < now - 48 * H]
    daily = [t for t in older if (today - day(t)).days < 60]
    assert set(Counter(day(t) for t in daily).values()) == {1}
    for t in daily:                                                          # the last backup of its day
        assert t == max(x for x in times if day(x) == day(t))
    monthly = [t for t in older if (today - day(t)).days >= 60]
    months = Counter((day(t).year, day(t).month) for t in monthly)
    assert set(months.values()) == {1}
    this_month = today.year * 12 + today.month
    assert min(this_month - (y * 12 + m) for y, m in months) >= 0
    assert max(this_month - (y * 12 + m) for y, m in months) == 23
    assert len(kept) < 120


def test_prune_never_deletes_the_newest_and_leaves_other_files_alone():
    now = utc(2026, 10, 18)
    nothing = {"all_hours": 1, "daily_days": 0, "monthly_months": 0}
    assert prune(["forwork-20200101-000000Z.db.gz"], now, nothing) == []
    names = ["forwork-20200101-000000Z.db.gz", "forwork-20200101-000000Z.boards.json",
             "forwork-20210101-000000Z.db.gz", "forwork-20210101-000000Z.boards.json",
             "forwork-20220101-000000Z.boards.json",                        # an export whose snapshot is missing
             "notes.txt", "pre-restore-20200101-000000Z.db", "forwork-20200101-000000Z-SERVER.db.gz",
             ".forwork-20200101-000000Z.db.gz.1234.tmp", "files"]
    assert prune(names, now, nothing) == ["forwork-20200101-000000Z.boards.json", "forwork-20200101-000000Z.db.gz",
                                          "forwork-20220101-000000Z.boards.json"]
    assert prune([], now) == []


def test_folder_target_with_retention_over_time(db, config, tmp_path):
    onedrive = tmp_path / "OneDrive - Fortis"
    onedrive.mkdir()
    folder = onedrive / "ForWork Backups"
    bk.save_settings(db, {"folder": str(folder), "keep": {"all_hours": 24, "daily_days": 3, "monthly_months": 2}})
    b = add_board(db)
    clock = Clock(utc(2026, 1, 1))
    mgr = BackupManager(db, config, clock=clock)
    now = syd(2026, 10, 18, 13, 0)
    runs = {
        "A": syd(2026, 8, 10, 9, 0),       # August: older than the monthly window
        "B": syd(2026, 9, 5, 9, 0),        # September, but not its last
        "C": syd(2026, 9, 20, 9, 0),       # September's last: kept as the monthly
        "D": syd(2026, 10, 1, 9, 0),       # October's last is the newest one
        "E": syd(2026, 10, 16, 9, 0),      # 16 October, but not its last
        "F": syd(2026, 10, 16, 18, 0),     # 16 October's last: kept as the daily
        "H": syd(2026, 10, 17, 10, 0),     # 27 hours old and not the day's last
        "G": syd(2026, 10, 17, 15, 0),     # 22 hours old: kept
        "now": now,
    }
    folder.mkdir()
    (folder / "readme.txt").write_text("not ours")
    (folder / "forwork-20260810-000000Z-SERVER.db.gz").write_text("a OneDrive conflict copy")
    for i, t in enumerate(runs.values()):
        clock.t = t
        rename_board(db, b, f"Version {i}")
        st = mgr.run()
        assert st["targets"]["folder"]["ok"], st
    expected = {snap_name(runs[k]) for k in ("C", "F", "G", "now")}
    in_folder = {p.name for p in folder.iterdir() if p.is_file()}
    assert in_folder == expected | {"readme.txt", "forwork-20260810-000000Z-SERVER.db.gz"}
    assert {p.name for p in config.backups_dir.iterdir()} == expected
    assert st["last_file"] == snap_name(now) and st["last_error"] == "" and st["warning"] == ""
    assert st["targets"]["folder"]["where"] == str(folder)
    c = unpack(folder / snap_name(now), tmp_path / "check.db")
    assert c.execute("SELECT name FROM boards").fetchone()[0] == "Version 8"
    c.close()
    names = [r["name"] for r in mgr.list_backups() if r["where"] == str(folder)]
    assert names == sorted(expected, reverse=True)


def test_attached_files_are_copied_once(db, config, tmp_path):
    folder = tmp_path / "OneDrive" / "ForWork Backups"
    folder.parent.mkdir()
    bk.save_settings(db, {"folder": str(folder)})
    add_board(db)
    a = add_file(config, b"engagement letter")
    b = add_file(config, b"ATO notice")
    st = BackupManager(db, config, clock=Clock(utc(2026, 10, 18, 1))).run()
    assert st["targets"]["folder"]["files_sent"] == 2
    assert (folder / a).read_bytes() == b"engagement letter"
    before = {rel: (folder / rel).stat().st_mtime_ns for rel in (a, b)}
    time.sleep(0.01)
    # a new manager knows nothing about earlier runs, so it checks the folder
    mgr = BackupManager(db, config, clock=Clock(utc(2026, 10, 18, 2)))
    assert mgr.run()["targets"]["folder"]["files_sent"] == 0
    assert {rel: (folder / rel).stat().st_mtime_ns for rel in (a, b)} == before
    c = add_file(config, b"signed tax return")
    mgr.clock = Clock(utc(2026, 10, 18, 3))
    assert mgr.run()["targets"]["folder"]["files_sent"] == 1
    assert (folder / c).read_bytes() == b"signed tax return"


# ---- Microsoft Graph ------------------------------------------------------------------------ #
def test_graph_target_end_to_end_with_an_upload_session(db, fake, tmp_path):
    config = graph_config(db, fake)
    bk.save_settings(db, {"graph": {"enabled": True, "target": {"kind": "user", "user": USER},
                                    "folder": "ForWork Backups"}})
    add_board(db)
    small = add_file(config, b"engagement letter")
    big_content = os.urandom(CHUNK_SIZE + 3 * CHUNK_UNIT + 1234)             # about 6.3 MB: two chunks
    big = add_file(config, big_content)
    fake.fail("chunk", 503, skip=1, headers={"Retry-After": "0"})          # the second chunk fails once

    def boards_export(tmp: Path) -> list[Path]:
        p = tmp / "boards.json"
        p.write_text('{"boards": []}')
        return [p]

    mgr = BackupManager(db, config, extra_exports=boards_export, clock=Clock(utc(2026, 10, 18, 1, 0)))
    st = mgr.run("manual")
    assert st["targets"]["graph"]["ok"], st
    assert st["last_error"] == "" and st["last_ok"] == "2026-10-18T01:00:00Z" and st["last_reason"] == "manual"
    assert st["targets"]["graph"]["where"] == f"{USER}'s OneDrive (ForWork Backups)"
    drive = fake.drives["drive-user"]
    local = config.backups_dir / "forwork-20261018-010000Z.db.gz"
    assert drive.files["ForWork Backups/forwork-20261018-010000Z.db.gz"] == local.read_bytes()
    assert drive.files["ForWork Backups/forwork-20261018-010000Z.boards.json"] == b'{"boards": []}'
    assert (config.backups_dir / "forwork-20261018-010000Z.boards.json").is_file()
    assert drive.files[f"ForWork Backups/{small}"] == b"engagement letter"
    assert drive.files[f"ForWork Backups/{big}"] == big_content
    total = len(big_content)
    assert fake.chunks == [(0, CHUNK_SIZE - 1, total), (CHUNK_SIZE, total - 1, total)]
    assert len(fake.requests("PUT", "upload/")) == 3                         # one chunk sent twice
    assert fake.auth_leaks == []
    assert fake.token_requests == 1                                          # the token is reused

    # files go once: a second run (and a new manager) sends only the snapshot and export
    puts = len(fake.requests("PUT"))
    mgr2 = BackupManager(db, config, extra_exports=boards_export, clock=Clock(utc(2026, 10, 18, 2, 0)))
    st2 = mgr2.run()
    assert st2["targets"]["graph"]["ok"] and st2["targets"]["graph"]["files_sent"] == 0
    assert len(fake.requests("PUT")) - puts == 2

    remote = [r["name"] for r in mgr2.list_backups() if r["where"] != "this server"]
    assert remote == ["forwork-20261018-020000Z.db.gz", "forwork-20261018-020000Z.boards.json",
                      "forwork-20261018-010000Z.db.gz", "forwork-20261018-010000Z.boards.json"]
    # a backup that is only in OneDrive is fetched from there
    local.unlink()
    fetched = mgr2.fetch("forwork-20261018-010000Z.db.gz")
    assert fetched.read_bytes() == drive.files["ForWork Backups/forwork-20261018-010000Z.db.gz"]
    assert fake.auth_leaks == []


def test_graph_client_paths_paging_download_and_delete(fake, tmp_path):
    client = client_for(fake)
    site = {"kind": "site", "site_url": f"https://{SITE_HOST}/{SITE_PATH}", "library": "Documents"}
    drive = client.resolve_drive(site)
    assert drive == "drive-docs"
    assert client.resolve_drive({**site, "library": "Shared Documents"}) == "drive-docs"
    assert client.resolve_drive({**site, "library": "archive"}) == "drive-archive"
    with pytest.raises(GraphError, match="no document library called 'Nope'.*Archive, Documents"):
        client.resolve_drive({**site, "library": "Nope"})
    with pytest.raises(GraphError, match="was not found"):
        client.resolve_drive({**site, "site_url": f"https://{SITE_HOST}/sites/Missing"})
    with pytest.raises(GraphError, match="no OneDrive was found for left@fortisap.com.au"):
        client.resolve_drive({"kind": "user", "user": "left@fortisap.com.au"})
    assert client.resolve_drive({"kind": "drive", "drive_id": "b!abc"}) == "b!abc"

    folder = "Back ups/Smith & Co #2"
    client.ensure_folder(drive, folder)
    client.ensure_folder(drive, folder)                                       # already there: no error
    assert {"Back ups", folder} <= fake.drives["drive-docs"].folders
    names = ["a b.txt", "R&D #1.txt", "100% done.txt", "plain.txt", "tilde~é.txt"]
    for i, n in enumerate(names):
        p = tmp_path / f"f{i}"
        p.write_text(n)
        item = client.upload(drive, p, f"{folder}/{n}")
        assert item["name"] == n
    assert set(fake.drives["drive-docs"].files) == {f"{folder}/{n}" for n in names}

    fake.page_size = 2
    lists = len(fake.requests("GET", "/children"))
    listing = client.list_folder(drive, folder)
    assert sorted(i["name"] for i in listing) == sorted(names)
    assert all(i["size"] > 0 and i["id"] and i["lastModifiedDateTime"] and not i["folder"] for i in listing)
    assert len(fake.requests("GET", "/children")) - lists == 3               # 5 items, 2 a page
    assert client.list_folder(drive, "Not there") == []
    assert [i["name"] for i in client.list_folder(drive, "") if i["folder"]] == ["Back ups"]

    out = client.download(drive, f"{folder}/100% done.txt", tmp_path / "dl" / "done.txt")
    assert out.read_text() == "100% done.txt"
    with pytest.raises(GraphError, match="not in the OneDrive"):
        client.download(drive, f"{folder}/gone.txt", tmp_path / "gone.txt")
    assert not (tmp_path / "gone.txt").exists()
    client.delete(drive, f"{folder}/R&D #1.txt")
    client.delete(drive, f"{folder}/R&D #1.txt")                             # already gone: no error
    assert f"{folder}/R&D #1.txt" not in fake.drives["drive-docs"].files
    with pytest.raises(GraphError):
        client.delete(drive, "")
    assert fake.auth_leaks == []
    assert fake.token_requests == 1


def test_graph_token_is_cached_until_two_minutes_before_expiry(fake, tmp_path):
    client = client_for(fake)
    for _ in range(3):
        client.list_folder("drive-user", "")
    assert fake.token_requests == 1
    fake.tokens.clear()                                                       # Microsoft drops the token early
    client.list_folder("drive-user", "")
    assert fake.token_requests == 2
    fake.expires_in = 100                                                     # under 2 minutes: never reused
    short = client_for(fake)
    short.list_folder("drive-user", "")
    short.list_folder("drive-user", "")
    assert fake.token_requests == 4


def test_graph_problems_read_as_plain_sentences(fake, tmp_path):
    bad = client_for(fake, secret="wrong-secret-xyz")
    with pytest.raises(GraphError) as e:
        bad.list_folder("drive-user", "")
    assert "client secret" in str(e.value) and "wrong-secret-xyz" not in str(e.value)
    assert "wrong-secret" not in repr(bad)
    fake.fail("token", 400, body={"error": "invalid_client",
                                  "error_description": "AADSTS7000222: The provided client secret keys are expired."})
    with pytest.raises(GraphError, match="has expired"):
        client_for(fake).list_folder("drive-user", "")
    with pytest.raises(GraphError, match="Application \\(client\\) ID was not found"):
        GraphClient(TENANT, "00000000-0000-0000-0000-000000000000", SECRET, fake.authority, fake.graph_base).token()
    with pytest.raises(GraphError, match="need the firm's Microsoft 365 app set up"):
        GraphClient("", "", "").token()

    client = client_for(fake)
    fake.fail("graph", 403)
    with pytest.raises(GraphError, match="not allowed to list .*Files.ReadWrite.All"):
        client.list_folder("drive-user", "ForWork Backups")
    client.sleep = lambda s: None
    fake.fail("graph", 503, times=10)
    with pytest.raises(GraphError, match="too busy"):
        client.list_folder("drive-user", "ForWork Backups")

    port = fake._server.server_port
    fake.stop()
    with pytest.raises(GraphError, match="could not reach Microsoft sign-in .*check the internet connection"):
        GraphClient(TENANT, CLIENT_ID, SECRET, f"http://127.0.0.1:{port}", f"http://127.0.0.1:{port}/v1.0").token()


def test_one_target_failing_does_not_stop_the_other(db, fake, tmp_path):
    config = graph_config(db, fake)
    onedrive = tmp_path / "OneDrive"
    onedrive.mkdir()
    bk.save_settings(db, {"folder": str(onedrive / "ForWork Backups"),
                          "graph": {"enabled": True, "target": {"kind": "user", "user": USER}}})
    add_board(db)
    fake.fail("token", 401, times=99, body={"error": "invalid_client",
                                            "error_description": "AADSTS7000215: Invalid client secret provided."})
    mgr = BackupManager(db, config, clock=Clock(utc(2026, 10, 18, 1)))
    st = mgr.run()
    assert st["targets"]["folder"]["ok"] is True and st["targets"]["folder"]["error"] == ""
    assert st["targets"]["graph"]["ok"] is False
    assert "client secret" in st["targets"]["graph"]["error"]
    assert st["last_error"].startswith(f"{USER}'s OneDrive (ForWork Backups): Microsoft did not accept")
    assert st["last_ok"] == "" and st["last_run"] == "2026-10-18T01:00:00Z"
    assert (onedrive / "ForWork Backups" / st["last_file"]).is_file()
    assert db.get_setting("backup_status") == st
    assert SECRET not in json.dumps(st)
    assert mgr.changed_since_last()                    # not a good backup, so the next one will try again

    # the other way round: the folder's parent is missing, OneDrive works
    fake.failures.clear()
    bk.save_settings(db, {"folder": str(tmp_path / "No OneDrive here" / "ForWork Backups"),
                          "graph": {"enabled": True, "target": {"kind": "user", "user": USER}}})
    mgr.clock = Clock(utc(2026, 10, 18, 2))
    st = mgr.run()
    assert st["targets"]["graph"]["ok"] is True
    assert st["targets"]["folder"]["ok"] is False
    assert "does not exist - check OneDrive is set up on the server" in st["targets"]["folder"]["error"]
    assert not (tmp_path / "No OneDrive here").exists()
    assert "ForWork Backups/forwork-20261018-020000Z.db.gz" in fake.drives["drive-user"].files


def test_a_failed_snapshot_is_raised_and_recorded(db, config, monkeypatch):
    def broken(*a, **k):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(bk, "make_snapshot", broken)
    mgr = BackupManager(db, config, clock=Clock(utc(2026, 10, 18, 1)))
    with pytest.raises(BackupError, match="could not be made: the disk is full"):
        mgr.run()
    assert mgr.status()["last_error"].startswith("the backup could not be made")


def test_no_targets_still_keeps_a_local_backup_and_says_so(db, config):
    add_board(db)
    st = BackupManager(db, config, clock=Clock(utc(2026, 10, 18, 1))).run()
    assert st["targets"] == {} and st["last_error"] == ""
    assert "only kept on this server" in st["warning"]
    assert (config.backups_dir / st["last_file"]).is_file()


# ---- the schedule --------------------------------------------------------------------------- #
def test_due_after_the_interval_only_if_something_changed():
    s = bk.clean_settings({"interval_minutes": 60})
    t0 = syd(2026, 10, 19, 10, 0)
    assert due(t0, {}, s, False) == "first"
    st = {"last_run": iso(t0), "last_ok": iso(t0)}
    assert due(t0 + 59 * dt.timedelta(minutes=1), st, s, True) == ""
    assert due(t0 + H, st, s, True) == "scheduled"
    assert due(t0 + 5 * H, st, s, False) == ""
    assert due(t0 - 2 * H, st, s, True) == "scheduled"                      # the clock was set back
    quarter = bk.clean_settings({"interval_minutes": 15})
    assert due(t0 + dt.timedelta(minutes=15), st, quarter, True) == "scheduled"


def test_daily_backup_after_2am_sydney_even_if_nothing_changed():
    s = bk.clean_settings({})
    evening = syd(2026, 10, 18, 17, 0)
    st = {"last_run": iso(evening), "last_ok": iso(evening)}
    assert due(syd(2026, 10, 19, 1, 59), st, s, False) == ""
    assert due(syd(2026, 10, 19, 2, 0), st, s, False) == "daily"
    assert due(syd(2026, 10, 19, 9, 0), st, s, False) == "daily"          # the server was off at 2 am
    done = {"last_run": iso(syd(2026, 10, 19, 2, 1)), "last_ok": iso(syd(2026, 10, 19, 2, 1))}
    assert due(syd(2026, 10, 19, 23, 59), done, s, False) == ""
    assert due(syd(2026, 10, 20, 2, 0), done, s, False) == "daily"
    # a daily backup that failed is tried again each interval, not every minute
    failed = {"last_run": iso(syd(2026, 10, 19, 2, 1)), "last_ok": iso(evening)}
    assert due(syd(2026, 10, 19, 2, 30), failed, s, False) == ""
    assert due(syd(2026, 10, 19, 3, 1), failed, s, False) == "daily"
    # summer and winter time, and the morning the clocks go forward
    assert bk.daily_boundary(utc(2026, 12, 1, 15, 30)) == utc(2026, 12, 1, 15, 0)
    assert bk.daily_boundary(utc(2026, 7, 1, 15, 59)) == utc(2026, 6, 30, 16, 0)
    assert bk.daily_boundary(syd(2026, 10, 4, 3, 30)) == utc(2026, 10, 3, 16, 0)


def test_sydney_time_falls_back_to_utc_plus_10_without_tzdata(monkeypatch):
    def missing(key):
        raise ZoneInfoNotFoundError(key)
    monkeypatch.setattr(bk, "ZoneInfo", missing)
    tz = bk.sydney_tz()
    assert tz.utcoffset(None) == dt.timedelta(hours=10)
    assert bk.daily_boundary(utc(2026, 1, 10, 17, 0), tz) == utc(2026, 1, 10, 16, 0)


def test_changed_since_last(db, config):
    b = add_board(db)
    mgr = BackupManager(db, config, clock=Clock(utc(2026, 10, 18, 1)))
    assert mgr.changed_since_last()
    mgr.run()
    assert not mgr.changed_since_last()
    with db.write() as c:
        c.execute("INSERT INTO activity (board_id, at, action) VALUES (?, ?, 'comment')", (b, now_iso()))
    assert mgr.changed_since_last()
    mgr.run()
    assert not mgr.changed_since_last()
    rename_board(db, b, "Renamed")
    assert mgr.changed_since_last()


def test_scheduler_thread_and_one_backup_at_a_time(db, config):
    b = add_board(db)
    clock = Clock(utc(2026, 10, 18, 1))                                       # 12 noon in Sydney
    mgr = BackupManager(db, config, clock=clock)
    mgr.tick_seconds = 0.02
    mgr.start_scheduler()
    try:
        deadline = time.monotonic() + 10
        while not mgr.status()["last_run"] and time.monotonic() < deadline:
            time.sleep(0.02)
    finally:
        mgr.stop_scheduler()
    st = mgr.status()
    assert st["last_reason"] == "first" and st["last_ok"] == "2026-10-18T01:00:00Z"
    assert len(list(config.backups_dir.glob("*.db.gz"))) == 1
    assert mgr.tick() == ""                                                   # nothing changed, too soon
    clock.t += 2 * H
    rename_board(db, b, "Changed")
    with mgr._run_lock:                                                      # a backup is already running
        assert mgr.tick() == ""
    assert mgr.tick() == "scheduled"
    assert mgr.tick() == ""


# ---- settings and the command line ---------------------------------------------------------- #
def test_settings_merge_defaults_and_check_values(db):
    assert bk.load_settings(db) == bk.DEFAULT_SETTINGS
    db.set_setting("backup", {"interval_minutes": "30", "keep": {"daily_days": 10}})
    s = bk.load_settings(db)
    assert s["interval_minutes"] == 30 and s["keep"] == {"all_hours": 48, "daily_days": 10, "monthly_months": 24}
    db.set_setting("backup", {"interval_minutes": "soon", "keep": {"all_hours": -5}, "graph": "yes"})
    assert bk.load_settings(db) == bk.DEFAULT_SETTINGS
    with pytest.raises(BackupError, match="full path"):
        bk.save_settings(db, {"folder": "OneDrive/backups"})
    with pytest.raises(BackupError, match="between 5 and 1440"):
        bk.save_settings(db, {"interval_minutes": 1})
    with pytest.raises(BackupError, match="email address"):
        bk.save_settings(db, {"graph": {"enabled": True, "target": {"kind": "user", "user": ""}}})
    saved = bk.save_settings(db, {"folder": "C:/Users/svc/OneDrive - Fortis/ForWork Backups",
                                  "graph": {"enabled": "on", "folder": "/Backups/ForWork/",
                                            "target": {"kind": "site", "site_url": f"https://{SITE_HOST}/{SITE_PATH}"}}})
    assert saved["graph"] == {"enabled": True, "folder": "Backups/ForWork",
                              "target": {"kind": "site", "site_url": f"https://{SITE_HOST}/{SITE_PATH}",
                                         "library": "Documents"}}
    assert bk.load_settings(db) == saved


def test_command_line_backup_list_and_restore(db, capsys, monkeypatch):
    for k in list(os.environ):
        if k.startswith("FORWORK_"):
            monkeypatch.delenv(k)
    data = str(db.path.parent)
    b = add_board(db, "Original")
    assert bk.main_backup(["backup", "--data", data]) == 0
    out = capsys.readouterr().out
    assert "Backup made:" in out and "only kept on this server" in out
    name = db.get_setting("backup_status")["last_file"]
    assert bk.main_backup(["backup", "list", "--data", data]) == 0
    assert name in capsys.readouterr().out
    rename_board(db, b, "Changed by mistake")
    assert bk.main_backup(["restore", name, "--data", data, "--yes"]) == 0
    assert "Restored" in capsys.readouterr().out
    assert db.scalar("SELECT name FROM boards") == "Original"
    assert bk.main_backup(["restore", "nonsense.txt", "--data", data, "--yes"]) == 1
    err = capsys.readouterr().err
    assert err.startswith("ForWork: 'nonsense.txt' is not the name of a ForWork backup") and "Traceback" not in err
    monkeypatch.setattr("builtins.input", lambda prompt: "no")
    assert bk.main_backup(["restore", name, "--data", data]) == 1
    assert "Nothing was changed" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        bk.main_backup(["restore", "--data", data])
