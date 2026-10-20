"""
FolderStore - the app's data kept as plain files in one folder, normally a SharePoint document
library synced to the PC by OneDrive.  SharePoint keeps a version history of every file; the app
adds a change log per client and a soft edit lock so two people don't change one client at once.

<root>/
  fortis-ledger.json                    marker and format version, and the firm's settings
  team.json                             who may open which client (when the firm manages access)
  firm-log.jsonl                        the firm's change log: team access, updates shared with everyone
  updates/*.fortisupdate                Fortis Ledger updates shared with everyone who uses the folder
  charts/<type>.csv                     firm master charts (copied from the skill on first use)
  clients/<slug>/
    client.json                         entity facts, report options, labels
    chart.csv                           the client's chart of accounts (Xero import/export columns)
    audit.jsonl                         change log, one JSON object per line, append-only
    lock.json                           who is editing (soft lock with a heartbeat)
    years/<fy>/year.json                year status (locked, lock history)
    years/<fy>/journals/<number>.json   one file per journal
    years/<fy>/reports/                 generated reports
    bank/<account code>.json            a bank account's imported statement lines and their cash coding

Writes go to a temporary file first and are renamed into place, so the sync client never
uploads a half-written file.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import socket
import time
import uuid
from pathlib import Path

from . import __version__
from .engine import CHART_DIR, chartlib
from .model import ENTITY_TYPES, Client, Journal, now_iso

FORMAT = 1
LOCK_STALE_SECONDS = 15 * 60
MARKER = "fortis-ledger.json"
ACCOUNT_CODE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.\-]{0,9}$")


class StoreError(Exception):
    pass


class LockedError(StoreError):
    pass


def slugify(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")
    return (s or "client")[:40].strip("-")


def replace_file(tmp: Path, path: Path, attempts: int = 8) -> None:
    """os.replace, retried briefly: on Windows OneDrive can hold a file open for a moment while it uploads."""
    for i in range(attempts):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if i == attempts - 1:
                tmp.unlink(missing_ok=True)
                raise StoreError(f"{path.name} is in use (OneDrive syncing?) - try again in a moment") from None
            time.sleep(0.25 * (i + 1))


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex[:8]}.tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    replace_file(tmp, path)


def read_json(path: Path, default=None, quiet: bool = False):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default
    except (json.JSONDecodeError, UnicodeDecodeError):
        # a file the sync client is still downloading, or a damaged copy
        if quiet:
            return default
        raise StoreError(f"{path.name} is not readable - check it in SharePoint (version history) "
                         f"or wait for OneDrive to finish syncing") from None


class FolderStore:
    def __init__(self, root: str | Path, user: str = "", machine: str | None = None, agent: str = ""):
        self.root = Path(root)
        self.user = user or os.environ.get("USERNAME") or os.environ.get("USER") or "unknown"
        self.machine = machine or socket.gethostname()
        self.agent = agent          # who acted for the user, e.g. "Claude" - recorded with each change

    # ---- layout ------------------------------------------------------------------------- #
    @property
    def clients_dir(self) -> Path:
        return self.root / "clients"

    @property
    def charts_dir(self) -> Path:
        return self.root / "charts"

    def client_dir(self, slug: str) -> Path:
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,60}", slug or ""):
            raise StoreError(f"bad client id {slug!r}")
        return self.clients_dir / slug

    def year_dir(self, slug: str, fy: int) -> Path:
        return self.client_dir(slug) / "years" / str(int(fy))

    def journal_dir(self, slug: str, fy: int) -> Path:
        return self.year_dir(slug, fy) / "journals"

    def reports_dir(self, slug: str, fy: int) -> Path:
        d = self.year_dir(slug, fy) / "reports"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def chart_path(self, slug: str) -> Path:
        return self.client_dir(slug) / "chart.csv"

    def bank_dir(self, slug: str) -> Path:
        return self.client_dir(slug) / "bank"

    def bank_path(self, slug: str, code: str) -> Path:
        if not ACCOUNT_CODE_RE.match(code or "") or code in (".", ".."):
            raise StoreError(f"bad account code {code!r}")
        return self.bank_dir(slug) / f"{code}.json"

    def list_bank_accounts(self, slug: str) -> list[str]:
        """Codes of the accounts with imported bank statements (their own files only, not sync conflict copies)."""
        d = self.bank_dir(slug)
        if not d.exists():
            return []
        out = []
        for p in d.glob("*.json"):
            data = read_json(p, None, quiet=True)
            if data and data.get("account") == p.stem:
                out.append(p.stem)
        return sorted(out)

    # ---- set up ------------------------------------------------------------------------- #
    def is_initialised(self) -> bool:
        return (self.root / MARKER).exists()

    def initialise(self) -> None:
        """Create the folder structure and copy the firm charts in (never overwrites them)."""
        self.root.mkdir(parents=True, exist_ok=True)
        self.clients_dir.mkdir(exist_ok=True)
        self.charts_dir.mkdir(exist_ok=True)
        for etype, fname in chartlib.CHART_FILES.items():
            dst = self.charts_dir / fname
            if not dst.exists():
                shutil.copyfile(CHART_DIR / fname, dst)
        if not (self.root / MARKER).exists():
            write_json(self.root / MARKER, {"app": "Fortis Ledger", "format": FORMAT, "created": now_iso(),
                                            "created_by": self.user, "version": __version__})

    # ---- firm-wide settings (in the marker file, so every PC shares them) ------------------- #
    def firm_settings(self) -> dict:
        return read_json(self.root / MARKER, {}, quiet=True) or {}

    def save_firm_settings(self, updates: dict) -> dict:
        data = self.firm_settings()
        for k, v in updates.items():
            if v is None:
                data.pop(k, None)
            else:
                data[k] = v
        data["updated"] = {"by": self.user, "at": now_iso()}
        write_json(self.root / MARKER, data)
        return data

    # ---- the team: who may open which client (its own file, so it never clashes with other settings) ---- #
    def read_team(self) -> dict:
        return read_json(self.root / "team.json", {}, quiet=True) or {}

    def save_team(self, data: dict, action: str, detail: dict) -> None:
        write_json(self.root / "team.json", {**data, "updated": {"by": self.user, "at": now_iso()}})
        self.log_firm(action, detail)

    def log_firm(self, action: str, detail: dict) -> None:
        """The firm's own change log (team access, updates shared with everyone), like a client's change log."""
        rec = {"at": now_iso(), "user": self.user, "machine": self.machine, "action": action, "detail": detail}
        with open(self.root / "firm-log.jsonl", "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
            fh.flush()
            os.fsync(fh.fileno())

    def read_firm_log(self, limit: int = 200) -> list[dict]:
        path = self.root / "firm-log.jsonl"
        rows = []
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return list(reversed(rows))[:limit]

    def firm_chart_path(self, etype: str) -> Path:
        fname = chartlib.CHART_FILES[chartlib.canonical_type(etype)]
        p = self.charts_dir / fname
        return p if p.exists() else CHART_DIR / fname

    # ---- clients ------------------------------------------------------------------------ #
    def list_clients(self) -> list[Client]:
        out = []
        if not self.clients_dir.exists():
            return out
        for d in sorted(self.clients_dir.iterdir()):
            data = read_json(d / "client.json") if d.is_dir() else None
            if data:
                out.append(Client.from_dict(data))
        return sorted(out, key=lambda c: c.name.lower())

    def create_client(self, name: str, etype: str, **fields) -> Client:
        etype = chartlib.canonical_type(etype)
        if etype not in ENTITY_TYPES:
            raise StoreError(f"entity type must be one of {', '.join(ENTITY_TYPES)}")
        if not (name or "").strip():
            raise StoreError("client name is required")
        cid = uuid.uuid4().hex[:6]
        slug = f"{slugify(name)}-{cid}"
        client = Client(id=cid, slug=slug, name=name.strip(), type=etype,
                        created={"by": self.user, "at": now_iso()}, **fields)
        d = self.client_dir(slug)
        (d / "years").mkdir(parents=True, exist_ok=True)
        shutil.copyfile(self.firm_chart_path(etype), d / "chart.csv")
        write_json(d / "client.json", client.to_dict())
        self.audit(slug, "client.created", {"name": client.name, "type": etype})
        return client

    def load_client(self, slug: str) -> Client:
        data = read_json(self.client_dir(slug) / "client.json")
        if not data:
            raise StoreError(f"no client {slug}")
        return Client.from_dict(data)

    def save_client(self, client: Client, action: str = "client.updated", detail: dict | None = None) -> None:
        write_json(self.client_dir(client.slug) / "client.json", client.to_dict())
        self.audit(client.slug, action, detail or {})

    # ---- years -------------------------------------------------------------------------- #
    def list_years(self, slug: str) -> list[int]:
        d = self.client_dir(slug) / "years"
        if not d.exists():
            return []
        return sorted(int(p.name) for p in d.iterdir() if p.is_dir() and p.name.isdigit())

    def ensure_year(self, slug: str, fy: int) -> None:
        jd = self.journal_dir(slug, fy)
        if not jd.exists():
            jd.mkdir(parents=True, exist_ok=True)
            write_json(self.year_dir(slug, fy) / "year.json", {"fy": fy, "locked": False, "history": []})
            self.audit(slug, "year.created", {"fy": fy})

    def year_state(self, slug: str, fy: int) -> dict:
        return read_json(self.year_dir(slug, fy) / "year.json", {"fy": fy, "locked": False, "history": []})

    def save_year_state(self, slug: str, fy: int, state: dict) -> None:
        write_json(self.year_dir(slug, fy) / "year.json", state)

    # ---- journals ----------------------------------------------------------------------- #
    def list_journals(self, slug: str, fy: int) -> list[Journal]:
        jd = self.journal_dir(slug, fy)
        if not jd.exists():
            return []
        out = []
        for p in jd.glob("*.json"):
            data = read_json(p, None, quiet=True)
            # only the journal's own file counts; sync conflict copies ("J2026-0001-PC2.json") are reported
            if data and data.get("number") == p.stem:
                out.append(Journal.from_dict(data))
        return sorted(out, key=lambda j: (j.date, j.number))

    def load_journal(self, slug: str, fy: int, number: str) -> Journal:
        data = read_json(self.journal_dir(slug, fy) / f"{_safe_number(number)}.json")
        if not data:
            raise StoreError(f"no journal {number} in FY{fy}")
        return Journal.from_dict(data)

    def next_journal_number(self, slug: str, fy: int) -> str:
        seqs = []
        for p in self.journal_dir(slug, fy).glob("*.json"):
            m = re.fullmatch(rf"J{fy}-(\d+)", p.stem)
            if m:
                seqs.append(int(m.group(1)))
        return f"J{fy}-{(max(seqs) + 1) if seqs else 1:04d}"

    def save_journal(self, slug: str, fy: int, journal: Journal) -> Journal:
        self.ensure_year(slug, fy)
        if not journal.number:
            journal.number = self._claim_number(slug, fy)
        write_json(self.journal_dir(slug, fy) / f"{_safe_number(journal.number)}.json", journal.to_dict())
        return journal

    def _claim_number(self, slug: str, fy: int) -> str:
        """Next journal number, claimed by creating its file so two windows never take the same one."""
        jd = self.journal_dir(slug, fy)
        seq = int(self.next_journal_number(slug, fy).split("-")[1])
        for _ in range(1000):
            number = f"J{fy}-{seq:04d}"
            try:
                os.close(os.open(jd / f"{number}.json", os.O_CREAT | os.O_EXCL | os.O_WRONLY))
                return number
            except FileExistsError:
                seq += 1
        raise StoreError(f"could not allocate a journal number in FY{fy}")

    def list_reports(self, slug: str, fy: int) -> list[Path]:
        d = self.year_dir(slug, fy) / "reports"
        if not d.exists():
            return []
        return sorted((p for p in d.iterdir() if p.is_file() and not p.name.startswith(".")),
                      key=lambda p: p.stat().st_mtime, reverse=True)

    # ---- sync health ------------------------------------------------------------------- #
    def conflicts(self, slug: str) -> list[str]:
        """
        Files the sync client left beside the real ones: conflict copies ('client-PC2.json',
        'J2026-0003 (1).json'), journals whose file name does not match their number, half-written
        temporary files.  Each needs a person to decide which copy is right.
        """
        root = self.client_dir(slug)
        found = []
        if not root.exists():
            return found
        expected_top = {"client.json", "chart.csv", "audit.jsonl", "lock.json", "years", "bank"}
        for p in root.iterdir():
            if p.name not in expected_top and not _ignorable(p.name):
                found.append(p.relative_to(root).as_posix())
        bank = self.bank_dir(slug)
        for p in (bank.iterdir() if bank.exists() else []):
            if _ignorable(p.name):
                if p.name.endswith(".tmp") and time.time() - p.stat().st_mtime > 3600:
                    found.append(p.relative_to(root).as_posix())
                continue
            data = read_json(p, None, quiet=True) if p.suffix == ".json" else None
            if not data or data.get("account") != p.stem:
                found.append(p.relative_to(root).as_posix())
        for fy in self.list_years(slug):
            yd = self.year_dir(slug, fy)
            for p in yd.iterdir():
                if p.name not in {"year.json", "journals", "reports"} and not _ignorable(p.name):
                    found.append(p.relative_to(root).as_posix())
            jd = self.journal_dir(slug, fy)
            for p in (jd.iterdir() if jd.exists() else []):
                if p.name.lower() in OS_FILES:
                    continue
                if p.name.startswith("."):
                    if p.name.endswith(".tmp") and time.time() - p.stat().st_mtime > 3600:
                        found.append(p.relative_to(root).as_posix())
                    continue
                if not re.fullmatch(rf"J{fy}-\d{{4,}}\.json", p.name):
                    found.append(p.relative_to(root).as_posix())
                    continue
                data = read_json(p, None, quiet=True)
                if not data or data.get("number") != p.stem:
                    found.append(p.relative_to(root).as_posix())
        return sorted(found)

    def archive_client(self, slug: str) -> Path:
        """Move a client out of the client list into <root>/archived/ (nothing is deleted)."""
        self.require_lock(slug)
        src = self.client_dir(slug)
        self.audit(slug, "client.archived", {})
        self.release_lock(slug)
        dst = self.root / "archived" / f"{slug}-{time.strftime('%Y%m%d-%H%M%S')}"
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
        return dst

    # ---- change log --------------------------------------------------------------------- #
    def audit(self, slug: str, action: str, detail: dict) -> None:
        rec = {"at": now_iso(), "user": self.user, "machine": self.machine, "action": action, "detail": detail}
        if self.agent:
            rec["via"] = self.agent
        path = self.client_dir(slug) / "audit.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
            fh.flush()
            os.fsync(fh.fileno())

    def last_audit(self, slug: str) -> dict | None:
        """The newest change-log entry, read from the end of the file."""
        path = self.client_dir(slug) / "audit.jsonl"
        try:
            with open(path, "rb") as fh:
                fh.seek(0, os.SEEK_END)
                size = fh.tell()
                fh.seek(max(0, size - 8192))
                tail = fh.read().decode("utf-8", "replace").splitlines()
        except FileNotFoundError:
            return None
        for line in reversed(tail):
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue
        return None

    def audit_tail(self, slug: str, max_bytes: int = 65536) -> list[dict]:
        """The newest change-log entries, newest first (read from the end of the file, so it stays quick)."""
        path = self.client_dir(slug) / "audit.jsonl"
        try:
            with open(path, "rb") as fh:
                fh.seek(0, os.SEEK_END)
                size = fh.tell()
                fh.seek(max(0, size - max_bytes))
                lines = fh.read().decode("utf-8", "replace").splitlines()
        except FileNotFoundError:
            return []
        out = []
        for line in reversed(lines[1:] if size > max_bytes else lines):     # the first may be cut short
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return out

    def read_audit(self, slug: str, limit: int = 500) -> list[dict]:
        path = self.client_dir(slug) / "audit.jsonl"
        if not path.exists():
            return []
        rows = []
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return list(reversed(rows))[:limit]

    # ---- edit lock ---------------------------------------------------------------------- #
    def lock_info(self, slug: str) -> dict | None:
        info = read_json(self.client_dir(slug) / "lock.json", None, quiet=True)
        if not info:
            return None
        info["stale"] = time.time() - float(info.get("heartbeat", 0)) > LOCK_STALE_SECONDS
        info["mine"] = info.get("user") == self.user and info.get("machine") == self.machine
        return info

    def acquire_lock(self, slug: str, force: bool = False) -> tuple[bool, dict | None]:
        info = self.lock_info(slug)
        if info and not (info["stale"] or info["mine"] or force):
            return False, info
        now = time.time()
        new = {"user": self.user, "machine": self.machine, "pid": os.getpid(),
               "since": info["since"] if info and info.get("mine") else now, "heartbeat": now}
        write_json(self.client_dir(slug) / "lock.json", new)
        if force and info and not info["mine"] and not info["stale"]:
            self.audit(slug, "lock.taken_over", {"from": info.get("user"), "machine": info.get("machine")})
        return True, self.lock_info(slug)

    def release_lock(self, slug: str) -> None:
        info = self.lock_info(slug)
        if info and info["mine"]:
            try:
                (self.client_dir(slug) / "lock.json").unlink()
            except FileNotFoundError:
                pass

    def require_lock(self, slug: str) -> None:
        """Raise LockedError unless this user holds (or can take) the client's edit lock."""
        ok, info = self.acquire_lock(slug)
        if not ok:
            raise LockedError(f"{info.get('user')} is editing this client on {info.get('machine')} - "
                              f"open it read-only or ask them to close it.")


OS_FILES = {"desktop.ini", "thumbs.db", ".ds_store"}


def _ignorable(name: str) -> bool:
    """Hidden files (our own temporary files) and the files Windows / macOS put in folders."""
    return name.startswith(".") or name.lower() in OS_FILES


def _safe_number(number: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9-]{1,30}", number or ""):
        raise StoreError(f"bad journal number {number!r}")
    return number
