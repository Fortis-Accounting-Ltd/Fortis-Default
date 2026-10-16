# PyInstaller build of Fortis Ledger - a folder 'dist/Fortis Ledger' with 'Fortis Ledger.exe'.
#   cd apps/fortis-ledger && pyinstaller --noconfirm --clean packaging/fortis-ledger.spec
#
# The financial-report-builder skill is bundled as files (financial-report-builder/scripts, assets,
# references) and its scripts are imported from there at run time, so the charts, report map,
# boilerplate and fonts resolve next to them exactly as in the repository.  PyInstaller cannot see
# those imports, so the modules the scripts use are listed for it below.
import ast
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

APP = Path(SPECPATH).resolve().parent
REPO = APP.parents[1]
SKILL = REPO / ".claude" / "skills" / "financial-report-builder"
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


hidden = imports_of(SKILL / "scripts") + collect_submodules("reportlab") + collect_submodules("openpyxl")
datas = [(str(APP / "fortis_ledger" / "web" / "templates"), "fortis_ledger/web/templates"),
         (str(APP / "fortis_ledger" / "web" / "static"), "fortis_ledger/web/static")]
for sub in ("scripts", "assets", "references"):
    datas.append((str(SKILL / sub), f"financial-report-builder/{sub}"))
datas.append((str(SKILL / "SKILL.md"), "financial-report-builder"))
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
coll = COLLECT(exe, a.binaries, a.datas, name="Fortis Ledger")
