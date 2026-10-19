"""
Adding the Claude connector to the Claude desktop app on this PC, two ways.

- As an extension (a .mcpb file): Claude Desktop installs it in one click and keeps it in its own extensions
  folder.  The extension is a manifest and a small Node.js script (Claude Desktop ships with Node.js) that starts
  the connector installed with Fortis Ledger, so it always runs this PC's current copy.
- In claude_desktop_config.json: the "fortis-ledger" entry under "mcpServers", keeping everything else in the
  file as it was and a copy of the old file beside it.  Claude Desktop reads the file when it starts.  Some
  versions of Claude Desktop for Windows rewrite that file when they start and drop entries added this way,
  which is why the extension is the main way.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import zipfile
from pathlib import Path

from . import APP_NAME, __version__
from .settings import config_dir

ENTRY = "fortis-ledger"
APP_DIR = Path(__file__).resolve().parent.parent
MCP_EXE = "fortis-ledger-mcp.exe" if os.name == "nt" else "fortis-ledger-mcp"
ICON = Path(__file__).resolve().parent / "web" / "static" / "icon-512.png"
PLATFORM = "win32" if os.name == "nt" else "darwin" if sys.platform == "darwin" else "linux"

# server/index.js in the extension.  It must not read stdin itself: the connector gets Claude's stdin and stdout.
PROXY_JS = """\
// Fortis Ledger's Claude Desktop extension.  Starts the Claude connector installed with Fortis Ledger on this PC
// (the program and arguments after this script's name, from manifest.json) and leaves Claude talking to it over
// stdin / stdout.
"use strict";
const { spawn } = require("child_process");

const AGAIN = "Install the extension again from Fortis Ledger (Firm settings > Claude).";
const [command, ...args] = process.argv.slice(2);
if (!command) {
  console.error("Fortis Ledger: the extension does not say where the connector is. " + AGAIN);
  process.exit(1);
}
const child = spawn(command, args, { stdio: "inherit", windowsHide: true });
child.on("error", (err) => {
  console.error(`Fortis Ledger: could not start ${command} (${err.message}). If Fortis Ledger was moved or ` +
                `reinstalled somewhere else: ${AGAIN}`);
  process.exit(1);
});
child.on("exit", (code) => process.exit(code === null ? 1 : code));
for (const signal of ["SIGINT", "SIGTERM", "SIGHUP"]) {
  process.on(signal, () => child.kill(signal));
}
"""


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


# ---- the extension --------------------------------------------------------------------------------- #
def _first_sentence(text: str) -> str:
    text = " ".join(text.split())
    end = text.find(". ")
    return text if end < 0 else text[:end + 1]


def extension_manifest() -> dict:
    """manifest.json for this PC: Claude Desktop runs server/index.js with its own Node.js, and the script starts
    the connector installed here with the program and arguments that follow it."""
    from .connector import TOOLS
    e = server_entry()
    m = {
        "manifest_version": "0.3",
        "name": ENTRY,
        "display_name": APP_NAME,
        "version": __version__,
        "description": "Read client ledgers in Fortis Ledger and prepare or post journals, on this PC.",
        "long_description": (
            "Lets Claude read a client's ledger in Fortis Ledger and prepare or post journals, through the ledger's own "
            "rules and as the person using this PC. Every change it makes is marked *via Claude* in the client's change "
            "log, and a partner decides whether Claude may post journals (Firm settings > Claude).\n\n"
            "Fortis Ledger made this extension for this PC: it starts the connector installed with Fortis Ledger. If "
            "Fortis Ledger is moved or reinstalled in another folder, install the extension again from Firm settings > "
            "Claude."),
        "author": {"name": "Fortis Accounting Partners"},
        "server": {
            "type": "node",
            "entry_point": "server/index.js",
            "mcp_config": {"command": "node", "args": ["${__dirname}/server/index.js", e["command"], *e["args"]],
                           "env": dict(e.get("env") or {})},
        },
        "tools": [{"name": t.name, "description": _first_sentence(t.description)} for t in TOOLS],
        "compatibility": {"platforms": [PLATFORM], "runtimes": {"node": ">=16.0.0"}},
    }
    if ICON.is_file():
        m["icon"] = "icon.png"
    return m


def extension_file() -> Path:
    """Where the extension for this PC is written: the person's Fortis Ledger settings folder."""
    return config_dir() / f"{ENTRY}.mcpb"


def build_extension(path: str | Path | None = None) -> Path:
    """Write the Claude Desktop extension (.mcpb, a zip) for this PC and return where it is."""
    path = Path(path) if path else extension_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("manifest.json", json.dumps(extension_manifest(), indent=2) + "\n")
        z.writestr("server/index.js", PROXY_JS)
        if ICON.is_file():
            z.write(ICON, "icon.png")
    os.replace(tmp, path)
    return path


def opens_with_claude() -> bool:
    """Whether opening a .mcpb file on this PC goes to Claude Desktop (it registers the file type when installed)."""
    if sys.platform == "darwin":
        return True
    if os.name != "nt":
        return False
    import winreg
    for root, key in ((winreg.HKEY_CURRENT_USER, r"Software\Classes\.mcpb"), (winreg.HKEY_CLASSES_ROOT, ".mcpb")):
        try:
            winreg.CloseKey(winreg.OpenKey(root, key))
            return True
        except OSError:
            pass
    return False
