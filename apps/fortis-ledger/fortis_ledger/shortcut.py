"""
Desktop icons (Windows shortcuts) for Fortis Ledger: one that opens the app, and one that opens it at
Connect to Xero.  Made with Windows' own shortcut maker (WScript.Shell through PowerShell), on the
desktop Windows reports - including a desktop that OneDrive has moved into the cloud folder.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ICONS = {
    "app": {"name": "Fortis Ledger", "args": [], "description": "Fortis Ledger - the firm's ledger and Financial Reports"},
    "xero": {"name": "Fortis Ledger - Connect to Xero", "args": ["--start", "xero"],
             "description": "Open Fortis Ledger at Connect to Xero"},
}

PS_SCRIPT = (
    "$ErrorActionPreference='Stop';"
    "$d=[Environment]::GetFolderPath('Desktop');"
    "$p=Join-Path $d ($env:FL_NAME + '.lnk');"
    "$s=(New-Object -ComObject WScript.Shell).CreateShortcut($p);"
    "$s.TargetPath=$env:FL_TARGET;$s.Arguments=$env:FL_ARGS;$s.WorkingDirectory=$env:FL_DIR;"
    "$s.IconLocation=$env:FL_ICON;$s.Description=$env:FL_DESC;$s.Save();Write-Output $p"
)


class ShortcutError(Exception):
    pass


def available() -> bool:
    return os.name == "nt"


def launch_command() -> tuple[str, list[str], str, str]:
    """(program, arguments, icon, start-in folder) that start this copy of Fortis Ledger."""
    if getattr(sys, "frozen", False):
        exe = sys.executable
        return exe, [], f"{exe},0", str(Path(exe).parent)
    exe = Path(sys.executable)
    pythonw = exe.with_name("pythonw.exe")
    program = str(pythonw if pythonw.exists() else exe)
    source = Path(__file__).resolve().parents[1]          # the folder holding fortis_ledger/, for "-m fortis_ledger"
    icon = source / "packaging" / "fortis-ledger.ico"
    return program, ["-m", "fortis_ledger"], f"{icon},0" if icon.exists() else f"{program},0", str(source)


def create(kind: str = "app", runner=subprocess.run) -> str:
    """Put the icon on the desktop; returns the shortcut's path."""
    if not available():
        raise ShortcutError("desktop icons can only be made on Windows")
    spec = ICONS[kind]
    program, base_args, icon, start_in = launch_command()
    args = base_args + spec["args"]
    env = {**os.environ, "FL_NAME": spec["name"], "FL_TARGET": program,
           "FL_ARGS": subprocess.list2cmdline(args), "FL_DIR": start_in, "FL_ICON": icon,
           "FL_DESC": spec["description"]}
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        r = runner(["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", PS_SCRIPT],
                   env=env, capture_output=True, text=True, timeout=60, creationflags=flags)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise ShortcutError(f"Windows could not make the desktop icon ({e})") from None
    if r.returncode != 0:
        raise ShortcutError(f"Windows could not make the desktop icon: {(r.stderr or r.stdout).strip()[:300]}")
    return (r.stdout or "").strip().splitlines()[-1] if (r.stdout or "").strip() else spec["name"]
