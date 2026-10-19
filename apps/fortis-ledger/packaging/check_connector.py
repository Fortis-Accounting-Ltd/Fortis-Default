"""
Check a built Claude connector program end to end, the way Claude Desktop uses it:

    python packaging/check_connector.py "dist/Fortis Ledger/fortis-ledger-mcp.exe"
    python packaging/check_connector.py --extension "dist/Fortis Ledger/fortis-ledger-mcp.exe"

It makes a data folder with a test client (using the app's source next to this script), starts the program
with its own settings folder, talks MCP to it over stdin / stdout - the initialize handshake of earlier MCP
revisions and the 2026-07-28 per-request form - and has it draft a journal.  With --extension the program
first writes its Claude Desktop extension (.mcpb), and the check runs it as Claude Desktop does: the
extension's script and arguments in a Node.js host that feeds its stdin and reads its stdout as streams, with
the real stdin closed (claude_node_host.js).  Exit code 0 means it worked.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

APP = Path(__file__).resolve().parent.parent
HOST_JS = Path(__file__).resolve().parent / "claude_node_host.js"
sys.path.insert(0, str(APP))

from fortis_ledger.ledger import Ledger  # noqa: E402
from fortis_ledger.store import FolderStore  # noqa: E402

MODERN = {"io.modelcontextprotocol/protocolVersion": "2026-07-28", "io.modelcontextprotocol/clientCapabilities": {}}


def extension_command(exe: str, folder: Path, env: dict) -> tuple[list[str], dict, bool]:
    """Have the program write its extension, unpack it, and return the command Claude Desktop would run, the
    environment it adds, and whether the extension is complete (manifest, script and icon)."""
    bundle = folder / "fortis-ledger.mcpb"
    made = subprocess.run([exe, "--claude-extension", str(bundle)], capture_output=True, text=True, timeout=180,
                          env=env, encoding="utf-8")
    if made.returncode != 0 or not bundle.is_file():
        raise SystemExit(f"FAILED - {exe} did not write the extension (exit {made.returncode}):\n{made.stderr[-2000:]}")
    unpacked = folder / "extension"
    with zipfile.ZipFile(bundle) as z:
        z.extractall(unpacked)
    manifest = json.loads((unpacked / "manifest.json").read_text(encoding="utf-8"))
    cfg = manifest["server"]["mcp_config"]
    node = shutil.which(cfg["command"])
    if not node:
        raise SystemExit("FAILED - Node.js is not on this PC, so the extension cannot be checked")
    icon = (unpacked / manifest.get("icon", "-")).is_file()
    print(f"extension: {manifest['name']} {manifest['version']}, {len(manifest['tools'])} tools, icon {'yes' if icon else 'no'}")
    complete = manifest["name"] == "fortis-ledger" and icon and (unpacked / manifest["server"]["entry_point"]).is_file()
    return [node, *(a.replace("${__dirname}", str(unpacked)) for a in cfg["args"])], dict(cfg.get("env") or {}), complete


def main(exe: str, extension: bool = False) -> int:
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
        text = "".join(json.dumps(m) + "\n" for m in msgs)
        complete = leaked = True
        if extension:
            # as Claude Desktop runs an extension: its Node.js host feeds process.stdin and reads process.stdout
            cmd, extra, complete = extension_command(exe, Path(d), env)
            env.update(extra)
            (Path(d) / "messages.jsonl").write_text(text, encoding="utf-8")
            replies = Path(d) / "replies.jsonl"
            p = subprocess.run([cmd[0], str(HOST_JS), str(Path(d) / "messages.jsonl"), str(replies), *cmd[1:]],
                               stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=180, env=env,
                               encoding="utf-8")
            answer = replies.read_text(encoding="utf-8") if replies.is_file() else ""
            leaked = bool(p.stdout.strip())
        else:
            p = subprocess.run([exe], input=text, capture_output=True, text=True, timeout=180, env=env, encoding="utf-8")
            answer = p.stdout
        print(f"exit {p.returncode}")
        if p.stderr.strip():
            print("stderr:", p.stderr.strip()[-2000:])
        try:
            out = {r["id"]: r for r in (json.loads(x) for x in answer.splitlines() if x.strip())}
            init, tools, disc, made = out[1]["result"], out[2]["result"], out[3]["result"], out[4]["result"]
            created = json.loads(made["content"][0]["text"])
        except (KeyError, ValueError, IndexError) as e:
            print(f"FAILED - unexpected output ({e!r}):\n{answer[-2000:]}")
            return 1
        checks = {
            "initialize": init.get("serverInfo", {}).get("name") == "fortis-ledger" and init.get("protocolVersion") == "2025-06-18",
            "tools": len(tools.get("tools", [])) >= 13,
            "discover": "2026-07-28" in disc.get("supportedVersions", []) and disc.get("resultType") == "complete",
            "journal": not made.get("isError") and created.get("status") == "draft" and created.get("number") == "J2026-0001",
            "saved": (lib / "clients" / c.slug / "years" / "2026" / "journals" / "J2026-0001.json").is_file(),
            "log": (home / "claude-connector.log").is_file(),
        }
        if extension:
            checks["extension"] = complete and not leaked
        for k, ok in checks.items():
            print(f"{k}: {'ok' if ok else 'FAILED'}")
        good = p.returncode == 0 and all(checks.values())
        print("PASSED" if good else "FAILED")
        return 0 if good else 1


if __name__ == "__main__":
    argv = sys.argv[1:]
    via_extension = "--extension" in argv
    argv = [a for a in argv if a != "--extension"]
    if len(argv) != 1:
        raise SystemExit(__doc__)
    raise SystemExit(main(argv[0], extension=via_extension))
