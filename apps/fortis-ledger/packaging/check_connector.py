"""
Check a built Claude connector program end to end, the way Claude Desktop uses it:

    python packaging/check_connector.py "dist/Fortis Ledger/fortis-ledger-mcp.exe"

It makes a data folder with a test client (using the app's source next to this script), starts the program
with its own settings folder, talks MCP to it over stdin / stdout - the initialize handshake of earlier MCP
revisions and the 2026-07-28 per-request form - and has it draft a journal.  Exit code 0 means it worked.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

APP = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(APP))

from fortis_ledger.ledger import Ledger  # noqa: E402
from fortis_ledger.store import FolderStore  # noqa: E402

MODERN = {"io.modelcontextprotocol/protocolVersion": "2026-07-28", "io.modelcontextprotocol/clientCapabilities": {}}


def main(exe: str) -> int:
    with tempfile.TemporaryDirectory(prefix="fortis-connector-check-") as d:
        lib, home = Path(d) / "library", Path(d) / "settings"
        st = FolderStore(lib, user="Connector Check", machine="ci")
        st.initialise()
        c = st.create_client("Connector Check Pty Ltd", "company", gst_registered=True)
        Ledger(st, c.slug).ensure_year(2026)
        home.mkdir()
        (home / "settings.json").write_text(json.dumps({"data_folder": str(lib), "user_name": "Connector Check"}),
                                            encoding="utf-8")
        journal = {"client": "Connector Check", "date": "2026-06-30", "narration": "Connector check",
                   "lines": [{"account": "72930", "debit": 110, "tax_rate": "GST on Expenses"},
                             {"account": "31300", "credit": 110}]}
        msgs = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize",
             "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "check", "version": "1"}}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "server/discover", "params": {"_meta": MODERN}},
            {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
             "params": {"_meta": MODERN, "name": "create_journal", "arguments": journal}},
        ]
        env = {**os.environ, "FORTIS_LEDGER_HOME": str(home)}
        p = subprocess.run([exe], input="".join(json.dumps(m) + "\n" for m in msgs), capture_output=True, text=True,
                           timeout=180, env=env, encoding="utf-8")
        print(f"exit {p.returncode}")
        if p.stderr.strip():
            print("stderr:", p.stderr.strip()[-2000:])
        try:
            out = {r["id"]: r for r in (json.loads(x) for x in p.stdout.splitlines() if x.strip())}
            init, tools, disc, made = out[1]["result"], out[2]["result"], out[3]["result"], out[4]["result"]
            created = json.loads(made["content"][0]["text"])
        except (KeyError, ValueError, IndexError) as e:
            print(f"FAILED - unexpected output ({e!r}):\n{p.stdout[-2000:]}")
            return 1
        checks = {
            "initialize": init.get("serverInfo", {}).get("name") == "fortis-ledger" and init.get("protocolVersion") == "2025-06-18",
            "tools": len(tools.get("tools", [])) >= 13,
            "discover": "2026-07-28" in disc.get("supportedVersions", []) and disc.get("resultType") == "complete",
            "journal": not made.get("isError") and created.get("status") == "draft" and created.get("number") == "J2026-0001",
            "saved": (lib / "clients" / c.slug / "years" / "2026" / "journals" / "J2026-0001.json").is_file(),
            "log": (home / "claude-connector.log").is_file(),
        }
        for k, ok in checks.items():
            print(f"{k}: {'ok' if ok else 'FAILED'}")
        good = p.returncode == 0 and all(checks.values())
        print("PASSED" if good else "FAILED")
        return 0 if good else 1


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    raise SystemExit(main(sys.argv[1]))
