"""
Adding the Claude connector to the Claude desktop app on this PC.

Claude Desktop starts the local connectors listed under "mcpServers" in its claude_desktop_config.json.
This adds (or removes) the "fortis-ledger" entry there, keeping everything else in the file as it was and a
copy of the old file beside it.  Claude Desktop reads the file when it starts, so it has to be restarted.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

ENTRY = "fortis-ledger"
APP_DIR = Path(__file__).resolve().parent.parent
MCP_EXE = "fortis-ledger-mcp.exe" if os.name == "nt" else "fortis-ledger-mcp"


class ClaudeDesktopError(Exception):
    pass


def server_entry() -> dict:
    """How Claude starts the connector on this PC: the installed console program, or Python for a source copy."""
    if getattr(sys, "frozen", False):
        return {"command": str(Path(sys.executable).resolve().parent / MCP_EXE), "args": []}
    return {"command": sys.executable, "args": ["-m", "fortis_ledger", "--mcp"], "env": {"PYTHONPATH": str(APP_DIR)}}


def config_paths() -> list[Path]:
    """Where Claude Desktop keeps its config on this PC (the Microsoft Store install keeps its own copy)."""
    override = os.environ.get("FORTIS_LEDGER_CLAUDE_CONFIG")
    if override:
        return [Path(p) for p in override.split(os.pathsep) if p]
    if os.name == "nt":
        roaming = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
        local = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
        paths = [roaming / "Claude" / "claude_desktop_config.json"]
        packages = local / "Packages"
        if packages.is_dir():
            paths += [p / "LocalCache" / "Roaming" / "Claude" / "claude_desktop_config.json"
                      for p in sorted(packages.glob("Claude_*"))]
        return paths
    if sys.platform == "darwin":
        return [Path.home() / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"]
    return [Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "Claude" / "claude_desktop_config.json"]


def _read(path: Path) -> dict:
    if not path.exists():
        return {}
    text = path.read_text(encoding="utf-8-sig").strip()
    if not text:
        return {}
    try:
        data = json.loads(text)
    except ValueError as e:
        raise ClaudeDesktopError(f"{path} is not valid JSON ({e}) - fix it in Claude Desktop (Settings > Developer > "
                                 f"Edit Config) first, so nothing in it is lost") from None
    if not isinstance(data, dict) or not isinstance(data.get("mcpServers", {}), dict):
        raise ClaudeDesktopError(f"{path} is not a Claude Desktop config file")
    return data


def _write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        shutil.copyfile(path, path.with_name(path.name + ".bak"))
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _present(p: Path) -> bool:
    """Claude Desktop is installed there: its folder exists, or it is a Microsoft Store install's own copy."""
    return p.parent.is_dir() or "LocalCache" in p.parts


def targets() -> list[Path]:
    """The config files to change: those of the Claude Desktop installed here, else the usual place."""
    paths = config_paths()
    found = [p for p in paths if _present(p)]
    return found or paths[:1]


def status() -> dict:
    """Whether Claude Desktop on this PC knows about the connector, and whether it points at this copy."""
    want = server_entry()
    out = {"installed": any(_present(p) for p in config_paths()), "connected": False, "current": False,
           "paths": []}
    for p in config_paths():
        try:
            entry = (_read(p).get("mcpServers") or {}).get(ENTRY)
        except ClaudeDesktopError:
            entry = None
        if entry:
            out["connected"] = True
            out["current"] = out["current"] or (entry.get("command") == want["command"] and entry.get("args") == want["args"])
            out["paths"].append(str(p))
    return out


def connect() -> list[Path]:
    """Add (or update) the connector in Claude Desktop's config.  Returns the files changed."""
    changed = []
    for p in targets():
        data = _read(p)
        servers = data.setdefault("mcpServers", {})
        servers[ENTRY] = server_entry()
        _write(p, data)
        changed.append(p)
    return changed


def disconnect() -> list[Path]:
    changed = []
    for p in config_paths():
        data = _read(p)
        if ENTRY in (data.get("mcpServers") or {}):
            del data["mcpServers"][ENTRY]
            _write(p, data)
            changed.append(p)
    return changed


def claude_code_command() -> str:
    """The command that adds the connector to Claude Code on this PC."""
    e = server_entry()
    parts = [f'"{e["command"]}"'] + [f'"{a}"' if " " in a else a for a in e["args"]]
    env = "".join(f' --env {k}="{v}"' for k, v in (e.get("env") or {}).items())
    return f"claude mcp add --scope user {ENTRY}{env} -- " + " ".join(parts)
