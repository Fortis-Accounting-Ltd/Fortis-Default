# PyInstaller build of Fortis Ledger - a folder 'dist/Fortis Ledger' with 'Fortis Ledger.exe' (the app, no console)
# and 'fortis-ledger-mcp.exe' (the Claude connector: the same program as a console app, so Claude can talk to it
# over stdin / stdout - launch.py starts it in connector mode).
#   cd apps/fortis-ledger && pyinstaller --noconfirm --clean packaging/fortis-ledger.spec
#
# The app's report engine (fortis_ledger/reportengine) is bundled as files and its scripts are imported
# from there at run time, so the charts, report map, wording and fonts resolve next to them exactly as
# they do from the source folder.  PyInstaller cannot see those imports, so the modules the scripts use
# are listed for it below.
import ast
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

APP = Path(SPECPATH).resolve().parent
ENGINE = APP / "fortis_ledger" / "reportengine"
ICON = APP / "packaging" / "fortis-ledger.ico"


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


hidden = imports_of(ENGINE / "scripts") + collect_submodules("reportlab") + collect_submodules("openpyxl")
datas = [(str(APP / "fortis_ledger" / "web" / "templates"), "fortis_ledger/web/templates"),
         (str(APP / "fortis_ledger" / "web" / "static"), "fortis_ledger/web/static"),
         (str(ENGINE / "scripts"), "fortis_ledger/reportengine/scripts"),
         (str(ENGINE / "assets"), "fortis_ledger/reportengine/assets"),
         (str(ENGINE / "SOURCE.md"), "fortis_ledger/reportengine")]
datas += collect_data_files("reportlab")
try:
    import webview  # noqa: F401  - only on Windows builds
    hidden += collect_submodules("webview")
    datas += collect_data_files("webview")
except ImportError:
    pass

a = Analysis([str(APP / "packaging" / "launch.py")], pathex=[str(APP)], hiddenimports=hidden, datas=datas,
             excludes=["tkinter", "pytest", "playwright", "IPython", "matplotlib", "numpy", "pandas"])
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="Fortis Ledger", console=False,
          icon=str(ICON) if ICON.exists() else None, version=None)
connector = EXE(pyz, a.scripts, [], exclude_binaries=True, name="fortis-ledger-mcp", console=True,
                icon=str(ICON) if ICON.exists() else None, version=None)
coll = COLLECT(exe, connector, a.binaries, a.datas, name="Fortis Ledger")
