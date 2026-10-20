"""
Per-user settings (which data folder, the name shown in the change log), kept on the PC - not in
the shared library - so each person can point the app at their own OneDrive sync of the library.
"""
from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .store import MARKER


def config_dir() -> Path:
    env = os.environ.get("FORTIS_LEDGER_HOME")
    if env:
        return Path(env)
    if os.name == "nt":
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "Fortis Ledger"


@dataclass
class Settings:
    data_folder: str = ""
    user_name: str = ""
    recent: list[str] = field(default_factory=list)   # client slugs, most recent first
    icons_made: list[str] = field(default_factory=list)   # desktop icons already put on this PC's desktop
    last_email: str = ""                                  # last office account signed in (offered next time)

    @classmethod
    def path(cls) -> Path:
        return config_dir() / "settings.json"

    @classmethod
    def load(cls) -> "Settings":
        try:
            data = json.loads(cls.path().read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            data = {}
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        s = cls(**known)
        if not s.user_name:
            s.user_name = default_user_name()
        return s

    def save(self) -> None:
        p = self.path()
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(self), indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, p)

    @property
    def configured(self) -> bool:
        return bool(self.data_folder) and Path(self.data_folder).is_dir() and bool(self.user_name.strip())

    def touch_client(self, slug: str) -> None:
        self.recent = [slug] + [s for s in self.recent if s != slug][:9]
        try:
            self.save()
        except OSError:
            pass


def default_user_name() -> str:
    return os.environ.get("USERNAME") or os.environ.get("USER") or ""


def suggest_folders(max_seconds: float = 2.0) -> list[str]:
    """
    Data folders already on this PC: OneDrive puts each synced SharePoint library under
    C:\\Users\\<you>\\<Organisation name>\\<Site> - <Library>.  Looks a few levels down for the marker file.
    """
    home = Path.home()
    roots = [home]
    for env in ("OneDriveCommercial", "OneDrive"):
        if os.environ.get(env):
            roots.append(Path(os.environ[env]))
    found: list[str] = []
    deadline = time.monotonic() + max_seconds
    skip = {"appdata", "node_modules", ".git", "library", "windows", "program files", "$recycle.bin"}

    def walk(d: Path, depth: int) -> None:
        if depth < 0 or time.monotonic() > deadline:
            return
        try:
            if (d / MARKER).is_file():
                found.append(str(d))
                return
            for child in d.iterdir():
                if child.is_dir() and not child.name.startswith(".") and child.name.lower() not in skip:
                    walk(child, depth - 1)
        except (PermissionError, OSError):
            return

    for r in roots:
        walk(r, 3)
    return sorted(set(found))
