"""The Claude connector: the MCP protocol (both eras), the tools, the ledger's rules, and Claude Desktop's config."""
import asyncio
import json
import os
import subprocess
import sys
from decimal import Decimal
from pathlib import Path

import pytest

from fortis_ledger import claude_desktop, connector
from fortis_ledger.connector import Connector
from fortis_ledger.ledger import Ledger
from fortis_ledger.settings import Settings
from fortis_ledger.store import FolderStore

from conftest import J

APP = Path(__file__).resolve().parents[1]
MODERN = {"io.modelcontextprotocol/protocolVersion": "2026-07-28",
          "io.modelcontextprotocol/clientCapabilities": {},
          "io.modelcontextprotocol/clientInfo": {"name": "test", "version": "1"}}


@pytest.fixture
def lib(tmp_path):
    st = FolderStore(tmp_path / "library", user="Alex Citizen", machine="pc1")
    st.initialise()
    c = st.create_client("Claude Test Pty Ltd", "company", gst_registered=True, abn="12 345 678 901")
    L = Ledger(st, c.slug)
    L.ensure_year(2025)
    L.save_journal(J("2025-06-30", "FY2025 balances", ("18500", 15000, 0), ("51100", 0, 20000), ("72930", 5000, 0)), 2025)
    L.close_year(2025)
    st.release_lock(c.slug)                     # set-up done: nobody has the client open
    st.create_client("Other Trust", "trust")
    return tmp_path / "library", c.slug


@pytest.fixture
def conn(lib, monkeypatch):
    monkeypatch.setattr("socket.gethostname", lambda: "pc1")
    return Connector(Settings(data_folder=str(lib[0]), user_name="Alex Citizen"))


def req(conn, method, params=None, rid=1):
    return conn.handle({"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}})


def call(conn, tool, modern=False, **args):
    params = {"name": tool, "arguments": args}
    if modern:
        params["_meta"] = MODERN
    res = req(conn, "tools/call", params)["result"]
    text = res["content"][0]["text"]
    return (json.loads(text) if not res["isError"] else text), res["isError"]


# ---- the protocol --------------------------------------------------------------------------- #
def test_legacy_handshake_and_tools_list(conn):
    r = req(conn, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "c", "version": "1"}})
    assert r["result"]["protocolVersion"] == "2025-06-18" and r["result"]["serverInfo"]["name"] == "fortis-ledger"
    assert r["result"]["capabilities"] == {"tools": {"listChanged": False}} and "Fortis Ledger" in r["result"]["instructions"]
    assert req(conn, "initialize", {"protocolVersion": "1999-01-01"})["result"]["protocolVersion"] == "2025-11-25"
    assert conn.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    tools = req(conn, "tools/list")["result"]
    assert "resultType" not in tools and "ttlMs" not in tools
    names = [t["name"] for t in tools["tools"]]
    assert names[:2] == ["list_clients", "get_chart_of_accounts"] and "create_journal" in names and len(names) == 13
    for t in tools["tools"]:
        assert t["inputSchema"]["type"] == "object" and t["description"] and t["annotations"]["openWorldHint"] is False
    create = next(t for t in tools["tools"] if t["name"] == "create_journal")
    assert create["annotations"]["readOnlyHint"] is False and create["inputSchema"]["required"] == ["client", "date", "narration", "lines"]
    assert req(conn, "ping")["result"] == {}


def test_modern_requests_carry_their_version(conn):
    d = req(conn, "server/discover", {"_meta": MODERN})["result"]
    assert d["resultType"] == "complete" and d["supportedVersions"][0] == "2026-07-28" and "2025-11-25" in d["supportedVersions"]
    assert d["capabilities"] == {"tools": {}} and d["_meta"]["io.modelcontextprotocol/serverInfo"]["name"] == "fortis-ledger"
    assert d["ttlMs"] > 0 and d["cacheScope"] == "private"
    t = req(conn, "tools/list", {"_meta": MODERN})["result"]
    assert t["resultType"] == "complete" and t["cacheScope"] == "private" and len(t["tools"]) == 13
    data, err = call(conn, "list_clients", modern=True)
    assert not err and data["count"] == 2
    res = req(conn, "tools/call", {"name": "list_clients", "arguments": {}, "_meta": MODERN})["result"]
    assert res["structuredContent"]["count"] == 2 and res["resultType"] == "complete"
    # a version the connector does not speak, missing metadata, unknown methods and tools
    bad = req(conn, "tools/list", {"_meta": {**MODERN, "io.modelcontextprotocol/protocolVersion": "2030-01-01"}})["error"]
    assert bad["code"] == -32022 and bad["data"]["requested"] == "2030-01-01" and "2026-07-28" in bad["data"]["supported"]
    no_caps = {k: v for k, v in MODERN.items() if not k.endswith("clientCapabilities")}
    assert req(conn, "tools/list", {"_meta": no_caps})["error"]["code"] == -32602
    assert req(conn, "server/discover")["error"]["code"] == -32602          # discover without _meta is malformed
    assert req(conn, "resources/list", {"_meta": MODERN})["error"]["code"] == -32601
    assert req(conn, "tools/call", {"name": "drop_database", "arguments": {}})["error"]["code"] == -32602
    assert conn.handle({"jsonrpc": "2.0", "id": None, "method": "ping"})["error"]["code"] == -32600
    assert conn.handle({"jsonrpc": "2.0", "id": 9, "result": {}}) is None          # a stray response is ignored
    batch = conn.handle([{"jsonrpc": "2.0", "id": 1, "method": "ping"}, {"jsonrpc": "2.0", "method": "notifications/x"}])
    assert batch == [{"jsonrpc": "2.0", "id": 1, "result": {}}]


def test_serve_reads_and_writes_one_message_per_line(conn):
    import io
    inp = io.BytesIO(b'{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-11-25"}}\n\n'
                     b'not json\n{"jsonrpc":"2.0","method":"notifications/initialized"}\n'
                     b'{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"list_clients","arguments":{"search":"other"}}}\n')
    out = io.BytesIO()
    assert connector.serve(inp, out, conn.settings()) == 0
    lines = out.getvalue().decode("utf-8").splitlines()
    assert len(lines) == 3 and all(json.loads(x)["jsonrpc"] == "2.0" for x in lines)
    assert json.loads(lines[1])["error"]["code"] == -32700
    found = json.loads(json.loads(lines[2])["result"]["content"][0]["text"])
    assert [c["name"] for c in found["clients"]] == ["Other Trust"]


# ---- reading the ledger ----------------------------------------------------------------------- #
def test_read_tools(conn, lib):
    data, _ = call(conn, "list_clients")
    claude = next(c for c in data["clients"] if c["name"] == "Claude Test Pty Ltd")
    assert claude["financial_years"] == ["FY2025", "FY2026"] and claude["gst_registered"] and claude["year_end"] == "30 June"
    assert data["claude_can_post_journals"] is False
    chart, _ = call(conn, "get_chart_of_accounts", client="claude test", search="accounting", fy=2025)
    row = next(a for a in chart["accounts"] if a["code"] == "72930")
    assert row["balance"] == "5000.00" and row["tax_rate"] == "GST on Expenses" and chart["gst_account"] == "35010"
    tb, _ = call(conn, "get_trial_balance", client=lib[1], fy=2025)
    assert tb["balanced"] and tb["net_profit_after_tax"] == "15000.00" and tb["total_debit"] == "20000.00"
    msg, err = call(conn, "get_trial_balance", client="Claude", fy=2031)
    assert err and "FY2031 has not been started" in msg and "FY2025, FY2026" in msg
    msg, err = call(conn, "get_trial_balance", client="Pty")
    assert not err                                        # 'Pty' matches one client only
    msg, err = call(conn, "list_clients", search="zzz")
    assert msg["count"] == 0
    msg, err = call(conn, "get_chart_of_accounts", client="nobody")
    assert err and "No client matches 'nobody'" in msg
    js, _ = call(conn, "list_journals", client="Claude Test", fy=2025)
    assert js["journals"][0]["number"] == "J2025-0001" and js["journals"][0]["total"] == "20000.00"
    j, _ = call(conn, "get_journal", client="Claude Test", number="j2025-0001")
    assert j["fy"] == 2025 and len(j["lines"]) == 3 and j["created"]["by"] == "Alex Citizen"
    gl, _ = call(conn, "get_account_transactions", client="Claude Test", account="18500", fy=2025)
    assert gl["closing_balance"] == "15000.00" and gl["transactions"][0]["number"] == "J2025-0001"
    system = next(a.code for a in Ledger(FolderStore(lib[0]), lib[1]).chart.accounts if a.system)
    gl, err = call(conn, "get_account_transactions", client="Claude Test", account=system, fy=2025)
    assert not err and gl["account"] == system                 # reading a Xero system account is fine
    msg, err = call(conn, "create_journal", client="Claude Test", date="2026-06-30", narration="x",
                    lines=[{"account": system, "debit": 1}, {"account": "31300", "credit": 1}])
    assert err and "Xero system account" in msg               # posting to one is not
    pl, _ = call(conn, "get_profit_and_loss", client="Claude Test", **{"from": "2024-07-01", "to": "2025-06-30"})
    assert pl["net_profit"] == "15000.00" and pl["rows"][0]["label"] == "Trading Income"
    bs, _ = call(conn, "get_balance_sheet", client="Claude Test", as_at="30/06/2025")
    assert bs["net_assets"] == bs["total_equity"] == "15000.00"
    gst, _ = call(conn, "get_gst_report", client="Claude Test", basis="cash")
    assert gst["basis"] == "Cash basis" and gst["net_gst"] == "0.00"


# ---- making journals ----------------------------------------------------------------------- #
LINES = [{"account": "72930", "debit": 110, "tax_rate": "GST on Expenses", "description": "June fee"},
         {"account": "31300", "credit": "110.00"}]


def test_create_journal_is_a_draft_until_the_firm_lets_claude_post(conn, lib):
    folder, slug = lib
    j, err = call(conn, "create_journal", client="Claude Test", date="30/06/2026", narration="Accrued fee", lines=LINES,
                  post=True)
    assert not err and j["status"] == "draft" and j["number"] == "J2026-0001" and "has not let Claude post" in j["note"]
    assert [(x["account"], x["debit"], x["credit"]) for x in j["lines"]] == [
        ("72930", "100.00", ""), ("31300", "", "110.00"), ("35010", "10.00", "")]
    L = Ledger(FolderStore(folder), slug)
    assert L.closing(2026).get("72930") is None                  # a draft is not in the balances
    saved = L.get_journal(2026, "J2026-0001")
    assert saved.created["via"] == "Claude" and saved.created["by"] == "Alex Citizen"
    audit = FolderStore(folder).read_audit(slug)[0]
    assert audit["action"] == "journal.draft_saved" and audit["via"] == "Claude" and audit["user"] == "Alex Citizen"
    assert not (folder / "clients" / slug / "lock.json").exists()          # the lock Claude took is handed back
    msg, err = call(conn, "post_journal", client="Claude Test", number="J2026-0001")
    assert err and "has not let Claude post" in msg
    # the firm lets Claude post
    FolderStore(folder).save_firm_settings({"claude": {"can_post": True}})
    j, err = call(conn, "post_journal", client="Claude Test", number="J2026-0001")
    assert not err and j["status"] == "posted" and j["changed"]["via"] == "Claude"
    j2, err = call(conn, "create_journal", client="Claude Test", date="2026-06-30", narration="Laptop", amounts="exclusive",
                   post=True, lines=[{"account": "72100", "debit": "1,000", "tax_rate": "GST on Expenses"},
                                     {"account": "31300", "credit": 1100}])
    assert not err and j2["status"] == "posted" and "note" not in j2 and j2["amounts"] == "GST exclusive"
    bal = Ledger(FolderStore(folder), slug).closing(2026)
    assert bal["72930"] == Decimal("100.00") and bal["72100"] == Decimal("1000.00") and bal["35010"] == Decimal("110.00")
    assert bal["31300"] == Decimal("-1210.00")
    msg, err = call(conn, "post_journal", client="Claude Test", number="J2026-0002")
    assert err and "is posted, not a draft" in msg


def test_create_journal_follows_the_ledger_rules(conn, lib):
    folder, slug = lib
    cases = [
        ({"lines": [{"account": "72930", "debit": 100}, {"account": "31300", "credit": 90}]}, "differ by 10.00"),
        ({"lines": [{"account": "7293", "debit": 100}, {"account": "31300", "credit": 100}]}, "did you mean 72930"),
        ({"lines": [{"account": "72930", "debit": 100, "credit": 5}, {"account": "31300", "credit": 95}]}, "not both"),
        ({"lines": [{"account": "72930", "debit": -100}, {"account": "31300", "credit": -100}]}, "amounts are positive"),
        ({"lines": [{"account": "72930", "debit": 100}]}, "at least two lines"),
        ({"lines": LINES[:1] + [{"account": "31300", "credit": "abc"}]}, "not an amount"),
        ({"lines": [{"account": "72930", "debit": 100, "tax_rate": "VAT"}, {"account": "31300", "credit": 100}]}, "not a tax rate"),
        ({"date": "2027-07-01"}, "FY2028, which has not been started"),
        ({"date": "31/02/2026"}, "date:"),
        ({"narration": ""}, "narration is required"),
    ]
    for change, expected in cases:
        args = {"client": "Claude Test", "date": "30/06/2026", "narration": "Test", "lines": LINES, **change}
        msg, err = call(conn, "create_journal", **args)
        assert err and expected in msg, (change, msg)
    # a locked year
    L = Ledger(FolderStore(folder, user="Alex Citizen", machine="pc1"), slug)
    L.lock_year(2026, "signed")
    L.store.release_lock(slug)
    msg, err = call(conn, "create_journal", client="Claude Test", date="30/06/2026", narration="Late", lines=LINES)
    assert err and "locked" in msg
    assert Ledger(FolderStore(folder), slug).journals(2026, statuses=None) == [
        j for j in Ledger(FolderStore(folder), slug).journals(2026, statuses=None) if j.kind == "close"]


def test_the_edit_lock_is_respected(conn, lib):
    folder, slug = lib
    other = FolderStore(folder, user="Sam Other", machine="pc2")
    assert other.acquire_lock(slug)[0]
    msg, err = call(conn, "create_journal", client="Claude Test", date="30/06/2026", narration="x", lines=LINES)
    assert err and "Sam Other is editing Claude Test Pty Ltd on pc2" in msg
    other.release_lock(slug)
    # the same person has the client open in Fortis Ledger on this PC (signed in with their email): Claude
    # works under their lock and leaves it with them
    conn._settings.last_email = "alex.citizen@fortisap.com.au"
    me = FolderStore(folder, user="alex.citizen@fortisap.com.au", machine="pc1")
    assert me.acquire_lock(slug)[0]
    j, err = call(conn, "create_journal", client="Claude Test", date="30/06/2026", narration="While open", lines=LINES)
    assert not err and j["created"]["by"] == "alex.citizen@fortisap.com.au"
    assert me.lock_info(slug)["mine"]


def test_drafts_claude_did_not_make_stay(conn, lib):
    folder, slug = lib
    L = Ledger(FolderStore(folder, user="Sam Other", machine="pc2"), slug)
    person = L.save_journal(J("2026-06-30", "Sam's draft", ("72930", 1, 0), ("31300", 0, 1)), 2026, post=False)
    L.store.release_lock(slug)
    msg, err = call(conn, "delete_draft_journal", client="Claude Test", number=person.number)
    assert err and "drafted by Sam Other" in msg
    mine, _ = call(conn, "create_journal", client="Claude Test", date="30/06/2026", narration="Oops", lines=LINES)
    gone, err = call(conn, "delete_draft_journal", client="Claude Test", number=mine["number"])
    assert not err and gone["deleted"] == mine["number"]
    numbers = [j.number for j in Ledger(FolderStore(folder), slug).journals(2026, statuses=None)]
    assert person.number in numbers and mine["number"] not in numbers


def test_add_account(conn, lib):
    folder, slug = lib
    a, err = call(conn, "add_account", client="Claude Test", type="Expense", code="61150", name="Staff amenities")
    assert not err and a["added"] == {"code": "61150", "name": "Staff amenities", "type": "Expense", "report_code": "EXP",
                                      "tax_rate": "GST on Expenses"}
    msg, err = call(conn, "add_account", client="Claude Test", type="Expense", code="61150", name="Again")
    assert err and "already exists" in msg
    msg, err = call(conn, "add_account", client="Claude Test", type="Wizard", code="61160", name="x")
    assert err and "type is one of" in msg
    log = [x for x in FolderStore(folder).read_audit(slug) if x["action"] == "account.added"]
    assert log[0]["via"] == "Claude" and log[0]["detail"]["code"] == "61150"


def test_who_claude_works_as(lib, monkeypatch):
    folder, slug = lib
    FolderStore(folder).save_firm_settings({"microsoft": {"tenant_id": "t", "client_id": "c", "required": True}})
    conn = Connector(Settings(data_folder=str(folder), user_name="Alex Citizen"))
    msg, err = call(conn, "list_clients")
    assert err and "requires office sign-in" in msg
    conn = Connector(Settings(data_folder=str(folder), user_name="Alex Citizen", last_email="alex.citizen@fortisap.com.au"))
    call(conn, "create_journal", client="Claude Test", date="30/06/2026", narration="Signed in", lines=LINES)
    assert FolderStore(folder).read_audit(slug)[0]["user"] == "alex.citizen@fortisap.com.au"
    msg, err = call(Connector(Settings(data_folder="")), "list_clients")
    assert err and "not set up on this PC" in msg


# ---- over real pipes, and with the official MCP client ---------------------------------------------- #
def settings_home(tmp_path, folder) -> dict:
    home = tmp_path / "config"
    home.mkdir()
    (home / "settings.json").write_text(json.dumps({"data_folder": str(folder), "user_name": "Alex Citizen"}),
                                        encoding="utf-8")
    return {**os.environ, "FORTIS_LEDGER_HOME": str(home), "PYTHONPATH": str(APP)}


def test_the_connector_process(tmp_path, lib):
    folder, slug = lib
    msgs = [{"jsonrpc": "2.0", "id": 1, "method": "server/discover", "params": {"_meta": MODERN}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {
                "_meta": MODERN, "name": "create_journal",
                "arguments": {"client": "Claude Test", "date": "30/06/2026", "narration": "Over the pipe", "lines": LINES}}}]
    p = subprocess.run([sys.executable, "-m", "fortis_ledger", "--mcp"], input="".join(json.dumps(m) + "\n" for m in msgs),
                       capture_output=True, text=True, timeout=120, env=settings_home(tmp_path, folder), cwd=str(APP))
    assert p.returncode == 0, p.stderr
    out = [json.loads(x) for x in p.stdout.splitlines()]
    assert [o["id"] for o in out] == [1, 2] and out[0]["result"]["supportedVersions"][0] == "2026-07-28"
    assert json.loads(out[1]["result"]["content"][0]["text"])["number"] == "J2026-0001"
    assert (tmp_path / "config" / "claude-connector.log").exists()


@pytest.mark.parametrize("mode", ["legacy", "auto", "2026-07-28"])
def test_with_the_official_mcp_client(tmp_path, lib, mode):
    mcp = pytest.importorskip("mcp")
    from mcp.client.stdio import StdioServerParameters
    folder, slug = lib
    env = settings_home(tmp_path, folder)
    params = StdioServerParameters(command=sys.executable, args=["-m", "fortis_ledger", "--mcp"], cwd=str(APP),
                                   env={"FORTIS_LEDGER_HOME": env["FORTIS_LEDGER_HOME"], "PYTHONPATH": str(APP)})

    async def run():
        async with mcp.Client(params, mode=mode) as client:
            tools = await client.list_tools()
            made = await client.call_tool("create_journal", {"client": "Claude Test", "date": "2026-06-30",
                                                             "narration": f"From the SDK ({mode})", "lines": LINES})
            wrong = await client.call_tool("get_journal", {"client": "Claude Test", "number": "J2026-0099"})
            return tools, made, wrong

    tools, made, wrong = asyncio.run(run())
    assert len(tools.tools) == 13 and not made.is_error and wrong.is_error
    assert json.loads(made.content[0].text)["status"] == "draft"
    assert "There is no journal J2026-0099" in wrong.content[0].text


# ---- Claude Desktop's config ----------------------------------------------------------------------- #
def test_connect_claude_desktop(tmp_path, monkeypatch):
    cfg = tmp_path / "Claude" / "claude_desktop_config.json"
    monkeypatch.setenv("FORTIS_LEDGER_CLAUDE_CONFIG", str(cfg))
    assert claude_desktop.status() == {"installed": False, "connected": False, "current": False, "paths": []}
    cfg.parent.mkdir()
    cfg.write_text(json.dumps({"mcpServers": {"other": {"command": "x"}}, "globalShortcut": "Ctrl+Space"}), encoding="utf-8")
    assert claude_desktop.connect() == [cfg]
    data = json.loads(cfg.read_text(encoding="utf-8"))
    assert data["globalShortcut"] == "Ctrl+Space" and data["mcpServers"]["other"] == {"command": "x"}
    assert data["mcpServers"]["fortis-ledger"]["args"] == ["-m", "fortis_ledger", "--mcp"]
    assert json.loads((cfg.parent / "claude_desktop_config.json.bak").read_text(encoding="utf-8"))["mcpServers"] == {"other": {"command": "x"}}
    st = claude_desktop.status()
    assert st["connected"] and st["current"] and st["installed"]
    assert claude_desktop.disconnect() == [cfg]
    assert json.loads(cfg.read_text(encoding="utf-8"))["mcpServers"] == {"other": {"command": "x"}}
    cfg.write_text("{ not json", encoding="utf-8")
    with pytest.raises(claude_desktop.ClaudeDesktopError, match="not valid JSON"):
        claude_desktop.connect()
    assert cfg.read_text(encoding="utf-8") == "{ not json"
    cmd = claude_desktop.claude_code_command()
    assert cmd.startswith("claude mcp add --scope user fortis-ledger --env PYTHONPATH=") and cmd.endswith("-m fortis_ledger --mcp")


def test_frozen_copy_points_claude_at_the_connector_program(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "Fortis Ledger" / "Fortis Ledger.exe"))
    entry = claude_desktop.server_entry()
    assert entry == {"command": str(tmp_path / "Fortis Ledger" / claude_desktop.MCP_EXE), "args": []}
