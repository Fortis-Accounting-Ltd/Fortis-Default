"""
Entry point for the packaged app (PyInstaller): 'Fortis Ledger.exe' and the Claude connector 'fortis-ledger-mcp.exe'.

The app's own code (the fortis_ledger package) ships as files beside the program rather than inside it, so an
update can replace just that.  The newest installed update (see fortis_ledger/updates.py) is loaded instead of the
copy installed with the program when it is newer and was made for this program's runtime.  If it fails to load,
the installed copy is used and the update is set aside, so a bad update cannot stop Fortis Ledger from opening.
"""
import json
import os
import re
import sys
from pathlib import Path

RUNTIME = "1"     # the Python runtime and libraries in this program: raise it when they change (updates name the runtime they need)


def _key(v):
    return tuple(int(x) for x in v.split(".")) if isinstance(v, str) and re.fullmatch(r"\d+(\.\d+){1,4}", v) else (0,)


def updates_dir() -> Path:
    """Same place as fortis_ledger.updates.updates_dir()."""
    if os.environ.get("FORTIS_LEDGER_UPDATES"):
        return Path(os.environ["FORTIS_LEDGER_UPDATES"])
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "Fortis Ledger" / "updates"
    if os.environ.get("FORTIS_LEDGER_HOME"):                     # fortis_ledger.settings.config_dir() / "updates"
        return Path(os.environ["FORTIS_LEDGER_HOME"]) / "updates"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Fortis Ledger" / "updates"
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "Fortis Ledger" / "updates"


def bundled_version(base: Path) -> str:
    try:
        m = re.search(r'^__version__ = "([^"]+)"', (base / "fortis_ledger" / "__init__.py").read_text(encoding="utf-8"), re.M)
    except OSError:
        return "0"
    return m.group(1) if m else "0"


def chosen_update(base: Path):
    """The folder of the update to load, or None for the copy installed with the program."""
    d = updates_dir()
    try:
        cur = json.loads((d / "current.json").read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    if not isinstance(cur, dict) or cur.get("bad") or cur.get("runtime") != RUNTIME:
        return None
    folder = d / str(cur.get("dir") or "")
    if not cur.get("dir") or not (folder / "fortis_ledger" / "__init__.py").is_file():
        return None
    return folder if _key(cur.get("version")) > _key(bundled_version(base)) else None


def set_aside(folder: Path, why: str) -> None:
    cur_path = updates_dir() / "current.json"
    try:
        cur = json.loads(cur_path.read_text(encoding="utf-8-sig"))
        cur["bad"] = why[:500]
        cur_path.write_text(json.dumps(cur, indent=1) + "\n", encoding="utf-8")
    except (OSError, ValueError):
        pass


def start():
    os.environ["FORTIS_LEDGER_RUNTIME"] = RUNTIME
    if Path(sys.executable).stem.lower() == "fortis-ledger-mcp" and "--mcp" not in sys.argv:
        sys.argv.insert(1, "--mcp")         # the console program Claude starts: same app, connector mode
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
    update = chosen_update(base)
    if update:
        sys.path.insert(0, str(update))
    try:
        from fortis_ledger.desktop import main
    except Exception as e:                  # the update does not load: use the installed copy, and set the update aside
        if not update:
            raise
        sys.path.remove(str(update))
        for name in [m for m in sys.modules if m == "fortis_ledger" or m.startswith("fortis_ledger.")]:
            del sys.modules[name]
        set_aside(update, f"{type(e).__name__}: {e}")
        from fortis_ledger.desktop import main
    return main()


if __name__ == "__main__":
    raise SystemExit(start())
