"""
The database - one SQLite file (forwork.db) in the data folder on the server.

SQLite is a single file that needs no database server, which suits a firm of our size: the app runs
as one process and every request gets its own connection on its own thread.  Writes take a
process-wide lock and an immediate transaction, so two people saving at once simply queue for a few
milliseconds.  The file must sit on the server's own disk, never in a OneDrive-synced folder: a sync
client copying a live database can corrupt it.  OneDrive gets consistent snapshots instead (backup.py).

Layout of the data folder:

  forwork.db            the database
  files/<aa>/<sha256>   files attached to items and updates (named by their content hash)
  backups/              local copies of the backups (the OneDrive copy is the one that matters)
"""
from __future__ import annotations

import contextlib
import datetime as dt
import json
import sqlite3
import threading
from pathlib import Path

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY,
  email TEXT NOT NULL UNIQUE COLLATE NOCASE,
  name TEXT NOT NULL,
  role TEXT NOT NULL DEFAULT 'member',          -- admin | member | viewer
  active INTEGER NOT NULL DEFAULT 1,
  color TEXT NOT NULL DEFAULT '#579bfc',
  ms_oid TEXT,                                  -- Microsoft Entra object id, set at first sign-in
  monday_id TEXT UNIQUE,
  created_at TEXT NOT NULL,
  last_seen TEXT
);
CREATE TABLE IF NOT EXISTS teams (id INTEGER PRIMARY KEY, name TEXT NOT NULL, monday_id TEXT UNIQUE);
CREATE TABLE IF NOT EXISTS team_members (
  team_id INTEGER NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  PRIMARY KEY (team_id, user_id)
);

CREATE TABLE IF NOT EXISTS workspaces (
  id INTEGER PRIMARY KEY, name TEXT NOT NULL, position REAL NOT NULL DEFAULT 0,
  monday_id TEXT UNIQUE, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS folders (
  id INTEGER PRIMARY KEY, workspace_id INTEGER NOT NULL REFERENCES workspaces(id),
  name TEXT NOT NULL, position REAL NOT NULL DEFAULT 0, monday_id TEXT UNIQUE
);
CREATE TABLE IF NOT EXISTS boards (
  id INTEGER PRIMARY KEY,
  workspace_id INTEGER NOT NULL REFERENCES workspaces(id),
  folder_id INTEGER REFERENCES folders(id),
  name TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  state TEXT NOT NULL DEFAULT 'active',         -- active | archived | deleted
  position REAL NOT NULL DEFAULT 0,
  created_by INTEGER REFERENCES users(id),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  seq INTEGER NOT NULL DEFAULT 0,               -- bumped on every change (open pages refresh from it)
  struct_seq INTEGER NOT NULL DEFAULT 0,        -- seq of the last change to groups, columns or views
  monday_id TEXT UNIQUE
);
CREATE TABLE IF NOT EXISTS groups (
  id INTEGER PRIMARY KEY,
  board_id INTEGER NOT NULL REFERENCES boards(id),
  title TEXT NOT NULL,
  color TEXT NOT NULL DEFAULT '#579bfc',
  position REAL NOT NULL DEFAULT 0,
  state TEXT NOT NULL DEFAULT 'active',         -- active | archived | deleted
  monday_id TEXT,
  UNIQUE (board_id, monday_id)
);
CREATE TABLE IF NOT EXISTS columns (
  id INTEGER PRIMARY KEY,
  board_id INTEGER NOT NULL REFERENCES boards(id),
  scope TEXT NOT NULL DEFAULT 'item',           -- item | subitem
  title TEXT NOT NULL,
  type TEXT NOT NULL,
  key TEXT NOT NULL,                            -- short name used in formulas, e.g. deal_value
  settings TEXT NOT NULL DEFAULT '{}',
  description TEXT NOT NULL DEFAULT '',
  position REAL NOT NULL DEFAULT 0,
  width INTEGER,
  state TEXT NOT NULL DEFAULT 'active',         -- active | deleted
  monday_id TEXT,
  UNIQUE (board_id, scope, monday_id)
);
CREATE TABLE IF NOT EXISTS items (
  id INTEGER PRIMARY KEY,
  board_id INTEGER NOT NULL REFERENCES boards(id),
  group_id INTEGER REFERENCES groups(id),       -- NULL for subitems
  parent_id INTEGER REFERENCES items(id),       -- set for subitems
  name TEXT NOT NULL,
  position REAL NOT NULL DEFAULT 0,
  state TEXT NOT NULL DEFAULT 'active',         -- active | archived | deleted
  created_by INTEGER REFERENCES users(id),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  updated_by INTEGER REFERENCES users(id),
  seq INTEGER NOT NULL DEFAULT 0,               -- the board's seq when the item last changed
  monday_id TEXT UNIQUE
);
CREATE INDEX IF NOT EXISTS items_board ON items (board_id, state, group_id, position);
CREATE INDEX IF NOT EXISTS items_parent ON items (parent_id);
CREATE INDEX IF NOT EXISTS items_seq ON items (board_id, seq);
CREATE TABLE IF NOT EXISTS item_values (
  item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
  column_id INTEGER NOT NULL REFERENCES columns(id) ON DELETE CASCADE,
  value TEXT NOT NULL,                          -- JSON, see model.py for each column type's shape
  text TEXT NOT NULL DEFAULT '',                -- the value as shown, for search, sorting and export
  updated_at TEXT,
  updated_by INTEGER REFERENCES users(id),
  PRIMARY KEY (item_id, column_id)
);
CREATE INDEX IF NOT EXISTS item_values_column ON item_values (column_id);

CREATE TABLE IF NOT EXISTS updates (
  id INTEGER PRIMARY KEY,
  item_id INTEGER NOT NULL REFERENCES items(id),
  parent_id INTEGER REFERENCES updates(id),     -- set for replies
  body TEXT NOT NULL,                           -- sanitised HTML
  text TEXT NOT NULL DEFAULT '',                -- plain text (search, notifications)
  created_by INTEGER REFERENCES users(id),
  author_name TEXT NOT NULL DEFAULT '',         -- kept for imported updates by people who have left
  created_at TEXT NOT NULL,
  edited_at TEXT,
  state TEXT NOT NULL DEFAULT 'active',
  monday_id TEXT UNIQUE
);
CREATE INDEX IF NOT EXISTS updates_item ON updates (item_id, created_at);

CREATE TABLE IF NOT EXISTS files (
  id INTEGER PRIMARY KEY,
  item_id INTEGER REFERENCES items(id),
  update_id INTEGER REFERENCES updates(id),
  column_id INTEGER REFERENCES columns(id),
  name TEXT NOT NULL,
  size INTEGER,
  content_type TEXT,
  sha256 TEXT,                                  -- NULL for files only listed (not copied) from monday.com
  link TEXT,                                    -- a file kept elsewhere (OneDrive, SharePoint, Google Drive)
  uploaded_by INTEGER REFERENCES users(id),
  created_at TEXT NOT NULL,
  state TEXT NOT NULL DEFAULT 'active',
  monday_asset_id TEXT
);
CREATE INDEX IF NOT EXISTS files_item ON files (item_id);

CREATE TABLE IF NOT EXISTS activity (
  id INTEGER PRIMARY KEY,
  board_id INTEGER,
  item_id INTEGER,
  user_id INTEGER,
  at TEXT NOT NULL,
  action TEXT NOT NULL,
  column_id INTEGER,
  old TEXT,
  new TEXT,
  detail TEXT
);
CREATE INDEX IF NOT EXISTS activity_board ON activity (board_id, id);
CREATE INDEX IF NOT EXISTS activity_item ON activity (item_id, id);

CREATE TABLE IF NOT EXISTS views (
  id INTEGER PRIMARY KEY,
  board_id INTEGER NOT NULL REFERENCES boards(id),
  name TEXT NOT NULL,
  type TEXT NOT NULL DEFAULT 'table',           -- table | kanban
  settings TEXT NOT NULL DEFAULT '{}',          -- filters, sort, hidden columns, kanban column
  position REAL NOT NULL DEFAULT 0,
  created_by INTEGER REFERENCES users(id),
  state TEXT NOT NULL DEFAULT 'active',
  monday_id TEXT
);
CREATE TABLE IF NOT EXISTS automations (
  id INTEGER PRIMARY KEY,
  board_id INTEGER NOT NULL REFERENCES boards(id),
  trigger TEXT NOT NULL,                        -- JSON, see automations.py
  action TEXT NOT NULL,                         -- JSON
  active INTEGER NOT NULL DEFAULT 1,
  created_by INTEGER REFERENCES users(id),
  created_at TEXT NOT NULL,
  runs INTEGER NOT NULL DEFAULT 0,
  last_run TEXT,
  imported_from TEXT
);
CREATE TABLE IF NOT EXISTS notifications (
  id INTEGER PRIMARY KEY,
  user_id INTEGER NOT NULL REFERENCES users(id),
  item_id INTEGER REFERENCES items(id),
  update_id INTEGER REFERENCES updates(id),
  kind TEXT NOT NULL,                           -- mention | assigned | reply | automation
  text TEXT NOT NULL,
  by_user INTEGER REFERENCES users(id),
  created_at TEXT NOT NULL,
  read_at TEXT
);
CREATE INDEX IF NOT EXISTS notifications_user ON notifications (user_id, read_at, id);
CREATE TABLE IF NOT EXISTS favourites (
  user_id INTEGER NOT NULL REFERENCES users(id),
  board_id INTEGER NOT NULL REFERENCES boards(id),
  PRIMARY KEY (user_id, board_id)
);
CREATE TABLE IF NOT EXISTS sessions (
  id TEXT PRIMARY KEY,                          -- SHA-256 of the cookie, never the cookie itself
  user_id INTEGER NOT NULL REFERENCES users(id),
  csrf TEXT NOT NULL,
  created_at TEXT NOT NULL,
  last_seen TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  ip TEXT,
  agent TEXT
);
"""


class DBError(Exception):
    pass


def now_iso() -> str:
    """UTC, to the second: 2026-10-18T23:30:00Z."""
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def dumps(v) -> str:
    return json.dumps(v, ensure_ascii=False, separators=(",", ":"))


def loads(s, default=None):
    if s is None or s == "":
        return default
    try:
        return json.loads(s)
    except (TypeError, ValueError):
        return default


class Database:
    """
    One per process.  conn() gives the calling thread its own connection; write() is a transaction
    that holds the process-wide write lock.
    """

    def __init__(self, path: str | Path, journal_mode: str = "WAL"):
        self.path = Path(path)
        self.journal_mode = journal_mode.upper()
        self._local = threading.local()
        self._write_lock = threading.RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.migrate()

    @property
    def folder(self) -> Path:
        return self.path.parent

    def conn(self) -> sqlite3.Connection:
        c = getattr(self._local, "conn", None)
        if c is None:
            c = sqlite3.connect(self.path, timeout=30, isolation_level=None, check_same_thread=False)
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA foreign_keys = ON")
            c.execute("PRAGMA busy_timeout = 30000")
            c.execute(f"PRAGMA journal_mode = {self.journal_mode}")
            c.execute("PRAGMA synchronous = NORMAL" if self.journal_mode == "WAL" else "PRAGMA synchronous = FULL")
            self._local.conn = c
        return c

    def close(self) -> None:
        c = getattr(self._local, "conn", None)
        if c is not None:
            c.close()
            self._local.conn = None

    @contextlib.contextmanager
    def write(self):
        """
        with db.write() as c: ...   One transaction under the write lock; rolled back on any error.
        Nested calls join the outer transaction.
        """
        c = self.conn()
        with self._write_lock:
            if c.in_transaction:
                yield c
                return
            c.execute("BEGIN IMMEDIATE")
            try:
                yield c
            except BaseException:
                c.execute("ROLLBACK")
                raise
            else:
                c.execute("COMMIT")

    def q(self, sql: str, params=()) -> list[sqlite3.Row]:
        return self.conn().execute(sql, params).fetchall()

    def one(self, sql: str, params=()) -> sqlite3.Row | None:
        return self.conn().execute(sql, params).fetchone()

    def scalar(self, sql: str, params=(), default=None):
        r = self.conn().execute(sql, params).fetchone()
        return default if r is None or r[0] is None else r[0]

    # ---- schema --------------------------------------------------------------------------- #
    def migrate(self) -> None:
        c = self.conn()
        with self._write_lock:
            c.executescript(SCHEMA)          # commits on its own, so not inside write()
        have = self.scalar("SELECT value FROM meta WHERE key = 'schema_version'")
        if have is None:
            with self.write() as w:
                w.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),))
                w.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('created_at', ?)", (now_iso(),))
        elif int(have) > SCHEMA_VERSION:
            raise DBError(f"this database was made by a newer ForWork (schema {have}); update ForWork first")

    # ---- settings (firm-wide, editable by admins) ------------------------------------------ #
    def get_setting(self, key: str, default=None):
        v = self.scalar("SELECT value FROM settings WHERE key = ?", (key,))
        return default if v is None else loads(v, default)

    def set_setting(self, key: str, value) -> None:
        with self.write() as c:
            c.execute("INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                      (key, dumps(value)))
