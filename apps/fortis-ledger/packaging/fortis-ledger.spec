# PyInstaller build of Fortis Ledger - a folder 'dist/Fortis Ledger' with 'Fortis Ledger.exe' (the app, no console)
# and 'fortis-ledger-mcp.exe' (the Claude connector: the same program as a console app, so Claude can talk to it
# over stdin / stdout - launch.py starts it in connector mode).
#   cd apps/fortis-ledger && pyinstaller --noconfirm --clean packaging/fortis-ledger.spec
#
# The program is a Python runtime with the libraries the app uses; the app's own code (fortis_ledger, with its
# templates and report engine) is bundled as plain files, not inside the program, so an update file can replace
# just that (launch.py loads the newest installed update - see fortis_ledger/updates.py).  For updates to be able
# to use any part of Python's standard library, and any part of the libraries the app already uses, all of them
# are bundled, not only what today's code imports.  A new library needs a new runtime: raise RUNTIME in launch.py.
#
# The report engine's scripts are imported from their files at run time, so the modules they use are listed for
# PyInstaller below.
import ast
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

APP = Path(SPECPATH).resolve().parent
PACKAGE = APP / "fortis_ledger"
ENGINE = PACKAGE / "reportengine"
ICON = APP / "packaging" / "fortis-ledger.ico"
NOT_BUNDLED = {"tkinter", "turtle", "turtledemo", "idlelib", "test", "ensurepip", "venv", "lib2to3", "pydoc_data",
               "msilib", "antigravity", "this", "__phello__", "xxsubtype"}
LIBRARIES = ["flask", "werkzeug", "jinja2", "markupsafe", "itsdangerous", "click", "blinker", "reportlab", "openpyxl",
             "et_xmlfile", "PIL"]


def imports_of(folder: Path) -> list[str]:
    names = set()
    local = {f.stem for f in folder.glob("*.py")}
    for f in folder.glob("*.py"):
        for node in ast.walk(ast.parse(f.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                names.update(a.name for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
                names.add(node.module)
    return sorted(n for n in names if n.split(".")[0] not in local and n != "__future__")


def standard_library() -> list[str]:
    out = []
    for name in sorted(sys.stdlib_module_names):
        if name in NOT_BUNDLED or name.startswith("_"):
            continue
        try:
            out += collect_submodules(name, filter=lambda m: m.split(".")[1:2] != ["test"] and ".tests" not in m)
        except Exception:
            out.append(name)
    return out


def package_files() -> list[tuple[str, str]]:
    """fortis_ledger as plain files (code, templates, report engine), without compiled caches."""
    return [(str(f), str(f.parent.relative_to(APP))) for f in sorted(PACKAGE.rglob("*"))
            if f.is_file() and "__pycache__" not in f.parts and f.suffix not in (".pyc", ".pyo")]


hidden = imports_of(ENGINE / "scripts") + standard_library()
for lib in LIBRARIES:
    hidden += collect_submodules(lib)
datas = package_files() + collect_data_files("reportlab")
try:
    import webview  # noqa: F401  - only on Windows builds
    hidden += collect_submodules("webview")
    datas += collect_data_files("webview")
except ImportError:
    pass

a = Analysis([str(APP / "packaging" / "launch.py")], pathex=[str(APP)], hiddenimports=hidden, datas=datas,
             excludes=sorted(NOT_BUNDLED) + ["pytest", "playwright", "IPython", "matplotlib", "numpy", "pandas"])
# the app's own modules load from the files above (or an update), not from inside the program
a.pure = [m for m in a.pure if m[0] != "fortis_ledger" and not m[0].startswith("fortis_ledger.")]
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="Fortis Ledger", console=False,
          icon=str(ICON) if ICON.exists() else None, version=None)
connector = EXE(pyz, a.scripts, [], exclude_binaries=True, name="fortis-ledger-mcp", console=True,
                icon=str(ICON) if ICON.exists() else None, version=None)
coll = COLLECT(exe, connector, a.binaries, a.datas, name="Fortis Ledger")
