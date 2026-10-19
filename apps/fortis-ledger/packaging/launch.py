"""Entry point for the packaged app (PyInstaller): 'Fortis Ledger.exe' and the Claude connector 'fortis-ledger-mcp.exe'."""
import sys
from pathlib import Path

if Path(sys.executable).stem.lower() == "fortis-ledger-mcp" and "--mcp" not in sys.argv:
    sys.argv.insert(1, "--mcp")         # the console program Claude starts: same app, connector mode

from fortis_ledger.desktop import main  # noqa: E402

raise SystemExit(main())
