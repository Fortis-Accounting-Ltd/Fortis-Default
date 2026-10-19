"""
Backups - consistent copies of the database, and of the files attached to items, sent to OneDrive.

The live database must stay on the server's own disk (a sync client copying a live SQLite file can
corrupt it), so OneDrive gets snapshots instead:

  1. SQLite's backup API copies the database while the app keeps running - the copy is the database
     exactly as it was at one moment.  The copy is checked (PRAGMA integrity_check) and gzipped to
     backups/forwork-YYYYMMDD-HHMMSSZ.db.gz in the data folder (the time is UTC).
  2. The snapshot, a readable JSON export of the boards (made by the caller) and any attached files
     not sent before go to each target: a OneDrive-synced folder on the server, and/or a OneDrive or
     SharePoint library through Microsoft Graph.  One target failing does not stop the others.
  3. Old backups are thinned out: everything from the last 48 hours, then the last one of each day for
     60 days, then the last one of each month for 24 months (admins can change these).  The newest
     backup is never deleted, and nothing else in the folder is touched.

A backup runs every hour if anything changed, and once a day after 2 am Sydney time even if nothing
did.  How the last run went is kept in the "backup_status" setting, for the admin page.

On a target:

  forwork-20261018-233000Z.db.gz          the database
  forwork-20261018-233000Z.boards.json    readable exports, named after the snapshot they go with
  files/<aa>/<sha256>                     attached files - named by their content, so each is sent
                                          once and never changes; these are not thinned out

To recover after losing the server: copy files/ back into a new data folder, then restore the newest
snapshot with  python -m forwork restore <file>.
"""
from __future__ import annotations

import argparse
import copy
import datetime as dt
import errno
import gzip
import json
import logging
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Callable, Iterable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import db as dbmod
from .config import Config
from .db import Database, DBError
from .graph import GraphClient, GraphError, describe_target

log = logging.getLogger("forwork.backup")

UTC = dt.timezone.utc
DAILY_HOUR = 2                                    # the daily backup runs after 2 am Sydney time
SNAPSHOT_SUFFIX = ".db.gz"
NAME_RE = re.compile(r"^forwork-(\d{8})-(\d{6})Z\.(.+)$")
PRE_RESTORE_RE = re.compile(r"^pre-restore-(\d{8})-(\d{6})Z(-\d+)?\.db$")
HEX2_RE = re.compile(r"^[0-9a-fA-F]{2}$")

DEFAULT_GRAPH_FOLDER = "ForWork Backups"
DEFAULT_SETTINGS: dict = {
    "folder": "",                                 # e.g. C:/Users/svc/OneDrive - Fortis/ForWork Backups
    "graph": {"enabled": False, "target": {"kind": "user", "user": ""}, "folder": DEFAULT_GRAPH_FOLDER},
    "interval_minutes": 60,
    "keep": {"all_hours": 48, "daily_days": 60, "monthly_months": 24},
}
EMPTY_STATUS: dict = {"last_run": "", "last_ok": "", "last_error": "", "last_file": "", "last_reason": "",
                      "warning": "", "fingerprint": "", "targets": {}}


class BackupError(Exception):
    pass


# ---- time ----------------------------------------------------------------------------------- #
def sydney_tz() -> dt.tzinfo:
    try:
        return ZoneInfo("Australia/Sydney")
    except (ZoneInfoNotFoundError, ValueError):
        # no time zone data on this server (Windows without the tzdata package): Sydney standard time
        return dt.timezone(dt.timedelta(hours=10), "AEST")


SYDNEY = sydney_tz()


def iso(t: dt.datetime) -> str:
    return t.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_iso(s) -> dt.datetime | None:
    if not s:
        return None
    try:
        t = dt.datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=UTC)


def stamp(t: dt.datetime) -> str:
    return t.astimezone(UTC).strftime("%Y%m%d-%H%M%SZ")


def parse_name(name: str) -> tuple[dt.datetime, str] | None:
    """(time, "snapshot" | "export") for a backup file's name; None for any other file."""
    m = NAME_RE.match(name or "")
    if not m or "/" in name or "\\" in name:
        return None
    try:
        when = dt.datetime.strptime(m[1] + m[2], "%Y%m%d%H%M%S").replace(tzinfo=UTC)
    except ValueError:
        return None
    return when, ("snapshot" if m[3] == SNAPSHOT_SUFFIX[1:] else "export")


def daily_boundary(now: dt.datetime, tz: dt.tzinfo | None = None) -> dt.datetime:
    """The latest 2 am Sydney time at or before now, in UTC."""
    tz = tz or SYDNEY
    local = now.astimezone(tz)
    day = local.date() if local.hour >= DAILY_HOUR else local.date() - dt.timedelta(days=1)
    return dt.datetime.combine(day, dt.time(DAILY_HOUR), tzinfo=tz).astimezone(UTC)


# ---- settings ------------------------------------------------------------------------------- #
def _bool(v) -> bool:
    return v is True or str(v).strip().lower() in ("1", "true", "yes", "on")


def clean_settings(raw, strict: bool = False) -> dict:
    """
    DEFAULT_SETTINGS with raw's values merged in.  strict (the settings page): a bad value raises
    BackupError saying what to fix.  Otherwise a bad value falls back to its default, so the scheduled
    backups keep running whatever is stored.
    """
    raw = raw if isinstance(raw, dict) else {}
    s = copy.deepcopy(DEFAULT_SETTINGS)
    problems: list[str] = []

    folder = raw.get("folder") or ""
    folder = folder.strip() if isinstance(folder, str) else ""
    if folder and not (PureWindowsPath(folder).is_absolute() or PurePosixPath(folder).is_absolute()):
        problems.append("the backup folder must be a full path, such as C:/Users/svc/OneDrive - Fortis/ForWork Backups")
    else:
        s["folder"] = folder

    g = raw.get("graph") if isinstance(raw.get("graph"), dict) else {}
    s["graph"]["enabled"] = _bool(g.get("enabled", False))
    t = g.get("target") if isinstance(g.get("target"), dict) else {}
    kind = t.get("kind", "user")
    if kind == "user":
        s["graph"]["target"] = {"kind": "user", "user": str(t.get("user") or "").strip()}
        if s["graph"]["enabled"] and "@" not in s["graph"]["target"]["user"]:
            problems.append("enter the email address of the person whose OneDrive holds the backups")
    elif kind == "site":
        s["graph"]["target"] = {"kind": "site", "site_url": str(t.get("site_url") or "").strip(),
                                "library": str(t.get("library") or "Documents").strip()}
        if s["graph"]["enabled"] and not s["graph"]["target"]["site_url"].lower().startswith("https://"):
            problems.append("the SharePoint site address should look like https://fortisap.sharepoint.com/sites/Fortis")
    elif kind == "drive":
        s["graph"]["target"] = {"kind": "drive", "drive_id": str(t.get("drive_id") or "").strip()}
        if s["graph"]["enabled"] and not s["graph"]["target"]["drive_id"]:
            problems.append("enter the OneDrive drive ID")
    else:
        problems.append("choose where in Microsoft 365 the backups go: a person's OneDrive, a SharePoint library "
                        "or a drive")
    gfolder = str(g.get("folder") or "").strip().strip("/\\")
    if any(p in (".", "..") for p in re.split(r"[/\\]+", gfolder)):
        problems.append("the OneDrive folder name cannot contain '.' or '..'")
    else:
        s["graph"]["folder"] = gfolder or DEFAULT_GRAPH_FOLDER

    def number(value, default: int, low: int, high: int, what: str) -> int:
        try:
            n = int(value if value not in (None, "") else default)
        except (TypeError, ValueError):
            problems.append(f"{what} must be a whole number")
            return default
        if not low <= n <= high:
            problems.append(f"{what} must be between {low} and {high}")
            return default
        return n

    s["interval_minutes"] = number(raw.get("interval_minutes"), 60, 5, 24 * 60, "the time between backups (minutes)")
    keep = raw.get("keep") if isinstance(raw.get("keep"), dict) else {}
    s["keep"] = {
        "all_hours": number(keep.get("all_hours"), 48, 1, 24 * 366, "hours to keep every backup"),
        "daily_days": number(keep.get("daily_days"), 60, 0, 3660, "days to keep a daily backup"),
        "monthly_months": number(keep.get("monthly_months"), 24, 0, 1200, "months to keep a monthly backup"),
    }
    if strict and problems:
        raise BackupError("; ".join(problems))
    return s


def load_settings(db: Database) -> dict:
    return clean_settings(db.get_setting("backup", {}))


def save_settings(db: Database, raw: dict) -> dict:
    """Check and store the backup settings from the settings page; raises BackupError saying what to fix."""
    s = clean_settings(raw, strict=True)
    db.set_setting("backup", s)
    return s


# ---- snapshots ------------------------------------------------------------------------------ #
def _remove(path: Path) -> None:
    for p in (path, Path(f"{path}-wal"), Path(f"{path}-shm"), Path(f"{path}-journal")):
        try:
            p.unlink(missing_ok=True)
        except OSError:
            pass


def _replace(tmp: Path, path: Path, attempts: int = 8) -> None:
    """os.replace, retried briefly: on Windows OneDrive can hold a file open for a moment while it uploads."""
    for i in range(attempts):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if i == attempts - 1:
                raise BackupError(f"{path.name} is in use (OneDrive syncing?) - the backup will try again "
                                  f"next time") from None
            time.sleep(0.25 * (i + 1))


def _copy_db(src: sqlite3.Connection, dest: Path) -> None:
    """A consistent copy of an open database into a new single-file database (no -wal file)."""
    out = sqlite3.connect(dest)
    try:
        src.backup(out)
        out.execute("PRAGMA journal_mode = DELETE")
    finally:
        out.close()


def check_database(conn: sqlite3.Connection, label: str) -> int:
    """Raise BackupError unless conn is an undamaged ForWork database this version can open; returns its schema."""
    try:
        rows = [r[0] for r in conn.execute("PRAGMA integrity_check").fetchall()]
        row = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
    except sqlite3.DatabaseError:
        raise BackupError(f"{label} is not a ForWork database, or it is damaged") from None
    if rows != ["ok"]:
        raise BackupError(f"{label} is damaged (the database check found: {rows[0] if rows else 'nothing'})")
    if row is None:
        raise BackupError(f"{label} is not a ForWork database")
    version = int(row[0])
    if version > dbmod.SCHEMA_VERSION:
        raise BackupError(f"{label} was made by a newer version of ForWork (schema {version}) - update ForWork "
                          f"before restoring it")
    return version


def make_snapshot(db: Database, dest_dir: str | Path, now: dt.datetime | None = None) -> Path:
    """
    A checked, gzipped copy of the database: dest_dir/forwork-YYYYMMDD-HHMMSSZ.db.gz.  Safe while the app
    is running - people can keep saving while the copy is made.
    """
    dest = Path(dest_dir)
    dest.mkdir(parents=True, exist_ok=True)
    final = dest / f"forwork-{stamp(now or dt.datetime.now(UTC))}{SNAPSHOT_SUFFIX}"
    tmp_db = dest / f".snapshot-{uuid.uuid4().hex[:8]}.db"
    tmp_gz = dest / f".{final.name}.{uuid.uuid4().hex[:8]}.tmp"
    try:
        src = sqlite3.connect(db.path, timeout=30)
        try:
            _copy_db(src, tmp_db)            # one step, so the copy is one moment's database
        finally:
            src.close()
        copy_conn = sqlite3.connect(tmp_db)
        try:
            rows = [r[0] for r in copy_conn.execute("PRAGMA integrity_check").fetchall()]
        finally:
            copy_conn.close()
        if rows != ["ok"]:
            found = rows[0] if rows else "no result"
            raise BackupError(f"the copy of the database failed its check ({found}) - the live database may be "
                              f"damaged; tell whoever looks after ForWork before changing anything else")
        with tmp_db.open("rb") as f, tmp_gz.open("wb") as raw:
            with gzip.GzipFile(filename="forwork.db", mode="wb", fileobj=raw, compresslevel=6) as gz:
                shutil.copyfileobj(f, gz, 1024 * 1024)
            raw.flush()
            os.fsync(raw.fileno())
        _replace(tmp_gz, final)
        return final
    finally:
        _remove(tmp_db)
        tmp_gz.unlink(missing_ok=True)


# ---- retention ------------------------------------------------------------------------------ #
def prune(names: Iterable[str], now: dt.datetime, keep: dict | None = None) -> list[str]:
    """
    Which backup files to delete.  A snapshot and the exports named after it are kept or deleted
    together.  Kept: everything from the last all_hours; the last snapshot of each day (Sydney time)
    for daily_days; the last of each month for monthly_months; and always the newest snapshot.
    Names that are not ForWork backups are never returned.
    """
    keep = {**DEFAULT_SETTINGS["keep"], **(keep or {})}
    sets: dict[dt.datetime, list[str]] = {}
    snapshots: set[dt.datetime] = set()
    for name in names:
        p = parse_name(name)
        if p:
            sets.setdefault(p[0], []).append(name)
            if p[1] == "snapshot":
                snapshots.add(p[0])
    if not sets:
        return []
    kept = {max(snapshots)} if snapshots else set()
    recent = now - dt.timedelta(hours=int(keep["all_hours"]))
    kept |= {when for when in sets if when >= recent}
    today = now.astimezone(SYDNEY).date()
    this_month = today.year * 12 + today.month
    last_of_day: dict[dt.date, dt.datetime] = {}
    last_of_month: dict[int, dt.datetime] = {}
    for when in sorted(snapshots):                    # oldest first, so each day ends with its last one
        day = when.astimezone(SYDNEY).date()
        month = day.year * 12 + day.month
        if (today - day).days < int(keep["daily_days"]):
            last_of_day[day] = when
        if this_month - month < int(keep["monthly_months"]):
            last_of_month[month] = when
    kept |= set(last_of_day.values()) | set(last_of_month.values())
    return sorted(name for when, group in sets.items() if when not in kept for name in group)


# ---- targets -------------------------------------------------------------------------------- #
def _parts(rel: str) -> list[str]:
    parts = [p for p in str(rel).replace("\\", "/").split("/") if p]
    if not parts or any(p in (".", "..") for p in parts):
        raise BackupError(f"'{rel}' is not a usable backup file name")
    return parts


def _top_level(name: str) -> str:
    if len(_parts(name)) != 1:
        raise BackupError(f"'{name}' is not a backup file name")
    return name


class FolderTarget:
    """
    Backups copied into a folder on the server - normally one OneDrive syncs (the server's OneDrive, or a
    synced SharePoint library).  Each file is written under a temporary name and then renamed, so the
    sync client never uploads half a file.
    """
    name = "folder"

    def __init__(self, path: str | Path):
        self.root = Path(path)
        self.where = str(self.root)
        self.key = f"folder:{self.root}"

    def _path(self, rel: str) -> Path:
        return self.root.joinpath(*_parts(rel))

    def _ready(self) -> None:
        if self.root.is_dir():
            return
        if not self.root.parent.is_dir():
            # don't quietly make a folder OneDrive doesn't sync
            raise BackupError(f"the backup folder cannot be made because {self.root.parent} does not exist - check "
                              f"OneDrive is set up on the server")
        self.root.mkdir(exist_ok=True)

    def put(self, local: Path, rel: str) -> None:
        dst = self._path(rel)
        self._ready()
        dst.parent.mkdir(parents=True, exist_ok=True)
        tmp = dst.with_name(f".{dst.name}.{uuid.uuid4().hex[:8]}.tmp")
        try:
            shutil.copyfile(local, tmp)
            _replace(tmp, dst)
        finally:
            tmp.unlink(missing_ok=True)

    def list(self) -> list[tuple[str, int]]:
        if not self.root.is_dir():
            return []
        return [(p.name, p.stat().st_size) for p in sorted(self.root.iterdir())
                if p.is_file() and not p.name.startswith(".")]

    def delete(self, name: str) -> None:
        self._path(_top_level(name)).unlink(missing_ok=True)

    def has(self, rel: str) -> bool:
        return self._path(rel).is_file()

    def get(self, name: str, local: Path) -> Path:
        local = Path(local)
        tmp = local.with_name(f".{local.name}.{uuid.uuid4().hex[:8]}.tmp")
        try:
            shutil.copyfile(self._path(_top_level(name)), tmp)
            os.replace(tmp, local)
        finally:
            tmp.unlink(missing_ok=True)
        return local


class GraphTarget:
    """Backups uploaded to a OneDrive or SharePoint library through Microsoft Graph (see graph.py)."""
    name = "graph"

    def __init__(self, client: GraphClient, target: dict, folder: str = DEFAULT_GRAPH_FOLDER):
        self.client = client
        self.target = dict(target or {})
        self.folder = "/".join(_parts(folder or DEFAULT_GRAPH_FOLDER))
        self.where = f"{describe_target(self.target)} ({self.folder})"
        self.key = "graph:" + json.dumps([self.target, self.folder], sort_keys=True)
        self._drive = ""
        self._folders: set[str] = set()                 # folders known to exist
        self._listing: dict[str, set[str]] = {}         # sub-folder -> file names, read once per run

    @property
    def drive(self) -> str:
        if not self._drive:
            self._drive = self.client.resolve_drive(self.target)
        return self._drive

    def _remote(self, rel: str = "") -> str:
        return "/".join([self.folder, *(_parts(rel) if rel else [])])

    def put(self, local: Path, rel: str) -> None:
        sub, _, base = "/".join(_parts(rel)).rpartition("/")
        parent = self._remote(sub)
        if parent not in self._folders:
            self.client.ensure_folder(self.drive, parent)
            self._folders.add(parent)
        self.client.upload(self.drive, local, self._remote(rel))
        if sub in self._listing:
            self._listing[sub].add(base)

    def _files(self, sub: str = "") -> list[dict]:
        return [i for i in self.client.list_folder(self.drive, self._remote(sub)) if not i["folder"]]

    def list(self) -> list[tuple[str, int]]:
        return [(i["name"], i["size"]) for i in self._files()]

    def delete(self, name: str) -> None:
        self.client.delete(self.drive, self._remote(_top_level(name)))

    def has(self, rel: str) -> bool:
        sub, _, base = "/".join(_parts(rel)).rpartition("/")
        if sub not in self._listing:
            self._listing[sub] = {i["name"] for i in self._files(sub)}
        return base in self._listing[sub]

    def get(self, name: str, local: Path) -> Path:
        return self.client.download(self.drive, self._remote(_top_level(name)), local)


def describe(e: BaseException) -> str:
    """An error as a sentence for the admin page."""
    if isinstance(e, (BackupError, GraphError, DBError)):
        return str(e)
    if isinstance(e, PermissionError):
        return f"ForWork is not allowed to use {e.filename or 'the folder'} - check the folder's permissions"
    if isinstance(e, FileNotFoundError):
        return f"{e.filename or 'a file'} was not found"
    if isinstance(e, OSError):
        if e.errno == errno.ENOSPC:
            return "the disk is full - free up some space"
        return f"a file could not be read or written ({e.strerror or e})"
    if isinstance(e, sqlite3.Error):
        return f"the database could not be read ({e})"
    log.error("unexpected backup problem", exc_info=e)
    return f"something unexpected went wrong ({type(e).__name__}) - the details are in the server log"


# ---- the scheduler's rule ------------------------------------------------------------------- #
def due(now: dt.datetime, status: dict, settings: dict, changed: bool) -> str:
    """
    Whether to back up now, and why: "first" (never backed up), "scheduled" (interval_minutes since the
    last run and something changed) or "daily" (no good backup since 2 am Sydney time - retried each
    interval while it keeps failing).  "" when no backup is due.
    """
    last_run = parse_iso(status.get("last_run"))
    if last_run is None:
        return "first"
    interval = dt.timedelta(minutes=int(settings.get("interval_minutes") or 60))
    waited = now - last_run >= interval or last_run > now          # a clock set back must not stop backups
    if waited and changed:
        return "scheduled"
    boundary = daily_boundary(now)
    last_ok = parse_iso(status.get("last_ok"))
    if (last_ok is None or last_ok < boundary) and (last_run < boundary or waited):
        return "daily"
    return ""


# ---- the manager ---------------------------------------------------------------------------- #
class BackupManager:
    """
    One per process.  run() makes a backup now; start_scheduler() makes them on time.

    extra_exports(tmp_dir) -> [paths] writes further files to send with each snapshot (the readable
    JSON export of the boards); each is renamed after the snapshot, e.g. boards.json ->
    forwork-20261018-233000Z.boards.json.  clock() -> aware datetime is for the tests.
    """
    tick_seconds = 60.0

    def __init__(self, db: Database, config: Config,
                 extra_exports: Callable[[Path], list[Path]] | None = None,
                 clock: Callable[[], dt.datetime] | None = None):
        self.db = db
        self.config = config
        self.extra_exports = extra_exports
        self.clock = clock or (lambda: dt.datetime.now(UTC))
        self._run_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._graph: GraphClient | None = None
        self._sent: dict[str, set[str]] = {}            # per target: attached files known to be there

    def now(self) -> dt.datetime:
        t = self.clock()
        return t if t.tzinfo else t.replace(tzinfo=UTC)

    @property
    def running(self) -> bool:
        return self._run_lock.locked()

    def graph_client(self) -> GraphClient:
        if self._graph is None:
            c = self.config
            self._graph = GraphClient(c.ms_tenant_id, c.ms_client_id, c.graph_client_secret,
                                      authority=c.ms_authority, graph_base=c.graph_base)
        return self._graph

    def targets(self, settings: dict | None = None) -> list:
        s = settings or load_settings(self.db)
        out: list = []
        if s["folder"]:
            out.append(FolderTarget(s["folder"]))
        if s["graph"]["enabled"]:
            out.append(GraphTarget(self.graph_client(), s["graph"]["target"], s["graph"]["folder"]))
        return out

    # ---- status ---------------------------------------------------------------------------- #
    def status(self) -> dict:
        st = self.db.get_setting("backup_status", {})
        return {**copy.deepcopy(EMPTY_STATUS), **(st if isinstance(st, dict) else {})}

    def fingerprint(self) -> str:
        """Changes whenever a board changes (boards.seq) or anything is logged (activity)."""
        boards = self.db.one("SELECT COUNT(*), COALESCE(SUM(seq), 0) FROM boards")
        last_activity = self.db.scalar("SELECT MAX(id) FROM activity", default=0)
        return f"{boards[0]}:{boards[1]}:{last_activity}"

    def changed_since_last(self) -> bool:
        return self.fingerprint() != self.status().get("fingerprint")

    # ---- a backup -------------------------------------------------------------------------- #
    def run(self, reason: str = "scheduled") -> dict:
        """
        Make a backup now and send it to every target; returns the new status.  Waits while another backup
        finishes.  Problems with a target are recorded in the status, not raised; BackupError is raised only
        if the snapshot itself could not be made.
        """
        with self._run_lock:
            return self._run(reason)

    def _run(self, reason: str) -> dict:
        settings = load_settings(self.db)
        now = self.now()
        status = self.status()
        status.update(last_run=iso(now), last_reason=reason)
        fingerprint = self.fingerprint()        # before the copy, so a change made during it is caught next time
        try:
            snapshot = make_snapshot(self.db, self.config.backups_dir, now)
        except Exception as e:
            status["last_error"] = f"the backup could not be made: {describe(e)}"
            self._save(status)
            raise BackupError(status["last_error"]) from None
        status["last_file"] = snapshot.name
        problems: list[str] = []
        exports: list[Path] = []
        if self.extra_exports:
            try:
                exports = self._exports(snapshot)
            except Exception as e:
                problems.append(f"the readable export could not be made: {describe(e)}")
        files = self._attached_files()
        if reason == "daily":
            self._sent.clear()                  # once a day, check every attached file is really there
        results: dict[str, dict] = {}
        for target in self.targets(settings):
            entry = {"ok": False, "at": "", "error": "", "where": target.where}
            try:
                entry["files_sent"] = self._send(target, snapshot, exports, files)
                entry["deleted"] = len(self._thin([n for n, _ in target.list()], target.delete, now, settings))
                entry["ok"] = True
            except Exception as e:
                entry["error"] = describe(e)
                problems.append(f"{target.where}: {entry['error']}")
                log.warning("backup to %s failed: %s", target.where, entry["error"])
            entry["at"] = iso(self.now())
            results[target.name] = entry
        try:
            local = self.config.backups_dir
            self._thin([p.name for p in local.iterdir() if p.is_file()], lambda n: (local / n).unlink(missing_ok=True),
                       now, settings)
        except OSError as e:
            problems.append(f"old backups on this server could not be cleared: {describe(e)}")
        status["targets"] = results
        status["warning"] = "" if results else ("backups are only kept on this server - set up a OneDrive folder or "
                                                "Microsoft 365 on the backup settings page")
        status["last_error"] = "; ".join(problems)
        if not problems:
            status["last_ok"] = iso(now)
            status["fingerprint"] = fingerprint
        self._save(status)
        return status

    def _save(self, status: dict) -> None:
        self.db.set_setting("backup_status", status)

    def _exports(self, snapshot: Path) -> list[Path]:
        base = snapshot.name[: -len(SNAPSHOT_SUFFIX)]
        out = []
        with tempfile.TemporaryDirectory(prefix=".export-", dir=snapshot.parent) as tmp:
            for p in self.extra_exports(Path(tmp)) or []:
                p = Path(p)
                dst = snapshot.parent / f"{base}.{p.name}"
                shutil.move(str(p), dst)
                out.append(dst)
        return out

    def _attached_files(self) -> list[tuple[str, Path]]:
        """[("files/ab/ab12...", path)] - the content-addressed attachments in the data folder."""
        root = self.config.files_dir
        out = []
        if root.is_dir():
            for sub in sorted(root.iterdir()):
                if sub.is_dir() and HEX2_RE.match(sub.name):
                    for f in sorted(sub.iterdir()):
                        if f.is_file() and not f.name.startswith(".") and not f.name.endswith(".tmp"):
                            out.append((f"files/{sub.name}/{f.name}", f))
        return out

    def _send(self, target, snapshot: Path, exports: list[Path], files: list[tuple[str, Path]]) -> int:
        # attached files first, so a snapshot on the target never refers to a file that isn't there yet
        known = self._sent.setdefault(target.key, set())
        sent = 0
        for rel, path in files:
            if rel in known:
                continue
            if not target.has(rel):
                target.put(path, rel)
                sent += 1
            known.add(rel)
        target.put(snapshot, snapshot.name)
        for p in exports:
            target.put(p, p.name)
        return sent

    @staticmethod
    def _thin(names: list[str], delete: Callable[[str], None], now: dt.datetime, settings: dict) -> list[str]:
        gone = prune(names, now, settings["keep"])
        for name in gone:
            delete(name)
        return gone

    # ---- the schedule ---------------------------------------------------------------------- #
    def tick(self) -> str:
        """One look at the clock: makes a backup if one is due.  Returns why it ran, or ""."""
        if not self._run_lock.acquire(blocking=False):
            return ""                                    # one is running already
        try:
            reason = due(self.now(), self.status(), load_settings(self.db), self.changed_since_last())
            if reason:
                self._run(reason)
            return reason
        finally:
            self._run_lock.release()

    def start_scheduler(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="forwork-backup", daemon=True)
        self._thread.start()

    def stop_scheduler(self, timeout: float = 30) -> None:
        self._stop.set()
        t, self._thread = self._thread, None
        if t:
            t.join(timeout)

    def _loop(self) -> None:
        try:
            while not self._stop.wait(self.tick_seconds):
                try:
                    self.tick()
                except Exception as e:
                    log.error("the scheduled backup failed: %s", describe(e))
        finally:
            self.db.close()                              # this thread's connection

    # ---- finding and restoring ------------------------------------------------------------- #
    def list_backups(self) -> list[dict]:
        """Backups on this server and on each target, newest first: [{"name", "size", "at", "kind", "where"}]."""
        out = []
        local = self.config.backups_dir
        if local.is_dir():
            for p in local.iterdir():
                m = PRE_RESTORE_RE.match(p.name)
                parsed = parse_name(p.name)
                if p.is_file() and (parsed or m):
                    when = parsed[0] if parsed else \
                        dt.datetime.strptime(m[1] + m[2], "%Y%m%d%H%M%S").replace(tzinfo=UTC)
                    out.append({"name": p.name, "size": p.stat().st_size, "at": iso(when),
                                "kind": parsed[1] if parsed else "pre-restore", "where": "this server"})
        for target in self.targets():
            try:
                for name, size in target.list():
                    parsed = parse_name(name)
                    if parsed:
                        out.append({"name": name, "size": size, "at": iso(parsed[0]), "kind": parsed[1],
                                    "where": target.where})
            except Exception as e:
                log.warning("could not list the backups in %s: %s", target.where, describe(e))
        return sorted(out, key=lambda r: (r["at"], r["name"]), reverse=True)

    def fetch(self, name: str) -> Path:
        """A backup by name, from this server or else downloaded from a target into the backups folder."""
        parsed = parse_name(name)
        if not ((parsed and parsed[1] == "snapshot") or PRE_RESTORE_RE.match(name or "")):
            raise BackupError(f"'{name}' is not the name of a ForWork backup (forwork-YYYYMMDD-HHMMSSZ.db.gz)")
        local = self.config.backups_dir / name
        if local.is_file():
            return local
        local.parent.mkdir(parents=True, exist_ok=True)
        for target in self.targets():
            try:
                if any(n == name for n, _ in target.list()):
                    return target.get(name, local)
            except Exception as e:
                log.warning("could not fetch %s from %s: %s", name, target.where, describe(e))
        raise BackupError(f"{name} was not found on this server or in OneDrive")

    def restore(self, snapshot_path: str | Path) -> Path:
        """
        Put a backup back as the live database, while the app runs.  The backup is checked first; the
        database as it was is kept as backups/pre-restore-<time>.db, whose path is returned.  The backup
        settings and status are kept as they are now (they describe this server, not the data).
        """
        src = Path(snapshot_path)
        if not src.is_file():
            raise BackupError(f"{src} was not found")
        backups = self.config.backups_dir
        backups.mkdir(parents=True, exist_ok=True)
        tmp = backups / f".restore-{uuid.uuid4().hex[:8]}.db"
        snap = None
        try:
            try:
                if src.name.endswith(".gz"):
                    with gzip.open(src, "rb") as f, tmp.open("wb") as out:
                        shutil.copyfileobj(f, out, 1024 * 1024)
                else:
                    shutil.copyfile(src, tmp)
            except (OSError, EOFError) as e:              # gzip.BadGzipFile is an OSError
                if isinstance(e, (PermissionError, FileNotFoundError)) or getattr(e, "errno", None) == errno.ENOSPC:
                    raise BackupError(describe(e)) from None
                raise BackupError(f"{src.name} could not be unpacked - it is not a ForWork backup, "
                                  f"or it is damaged") from None
            snap = sqlite3.connect(tmp)
            check_database(snap, src.name)
            db = self.db
            with db._write_lock:
                live = db.conn()
                if live.in_transaction:
                    raise BackupError("a change is still being saved - try the restore again in a moment")
                kept = {k: db.get_setting(k) for k in ("backup", "backup_status")}
                when = self.now()
                pre = backups / f"pre-restore-{stamp(when)}.db"
                n = 1
                while pre.exists():                        # never overwrite an earlier pre-restore copy
                    n += 1
                    pre = backups / f"pre-restore-{stamp(when)}-{n}.db"
                _copy_db(live, pre)
                # into the live connection, so every other thread's connection sees the restored data
                snap.backup(live)
                db.migrate()
                if kept["backup"] is not None:
                    db.set_setting("backup", kept["backup"])
                status = kept["backup_status"] if isinstance(kept["backup_status"], dict) else {}
                status["last_restore"] = {"at": iso(when), "file": src.name, "pre_restore": pre.name}
                db.set_setting("backup_status", status)
            log.info("restored %s; the database before is kept in %s", src.name, pre.name)
            return pre
        finally:
            if snap is not None:
                snap.close()
            _remove(tmp)


# ---- command line --------------------------------------------------------------------------- #
def _size(n: int) -> str:
    for unit in ("bytes", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "bytes" else f"{n:.1f} {unit}"
        n /= 1024
    return str(n)


def main_backup(argv: list[str] | None = None, extra_exports: Callable[[Path], list[Path]] | None = None) -> int:
    """
    python -m forwork backup [--data FOLDER]                  make a backup now
    python -m forwork backup list [--data FOLDER]             list the backups (this server and OneDrive)
    python -m forwork restore FILE [--data FOLDER] [--yes]    put a backup back (a path, or a name from the list)
    """
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) >= 2 and args[0] == "backup" and args[1] in ("list", "restore"):
        args = args[1:]
    p = argparse.ArgumentParser(prog="python -m forwork", description="ForWork backups to OneDrive")
    p.add_argument("command", choices=["backup", "list", "restore"])
    p.add_argument("file", nargs="?", help="restore: the backup file - a path, or a name from 'backup list'")
    p.add_argument("--data", help="the data folder (default: FORWORK_DATA, or ./forwork-data)")
    p.add_argument("--yes", action="store_true", help="restore without asking first")
    a = p.parse_args(args)
    if a.command == "restore" and not a.file:
        p.error("restore needs the backup file - a path, or a name from 'python -m forwork backup list'")
    database = None
    try:
        config = Config.load(a.data)
        database = Database(config.db_path, config.journal_mode)
        mgr = BackupManager(database, config, extra_exports=extra_exports)
        if a.command == "backup":
            st = mgr.run(reason="manual")
            print(f"Backup made: {config.backups_dir / st['last_file']}")
            for t in st["targets"].values():
                print(f"  {t['where']}: " + ("sent" if t["ok"] else f"NOT SENT - {t['error']}"))
            if st["warning"]:
                print(f"Note: {st['warning']}")
            return 1 if st["last_error"] else 0
        if a.command == "list":
            rows = mgr.list_backups()
            if not rows:
                print("No backups yet.")
            for r in rows:
                print(f"{r['name']:<48} {_size(r['size']):>10}  {r['where']}")
            return 0
        path = Path(a.file)
        if not path.is_file():
            path = mgr.fetch(a.file)
        if not a.yes:
            answer = input(f"Replace the ForWork database in {config.data_dir} with {path.name}? "
                           f"Type YES to go ahead: ")
            if answer.strip() != "YES":
                print("Nothing was changed.")
                return 1
        pre = mgr.restore(path)
        print(f"Restored {path.name}.  The database as it was just before is kept in {pre}")
        return 0
    except (BackupError, GraphError, DBError) as e:
        print(f"ForWork: {e}", file=sys.stderr)
        return 1
    finally:
        if database is not None:
            database.close()
