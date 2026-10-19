"""
End-to-end check of an installed copy - `Fortis Ledger.exe --self-test`.  For each entity type it
sets up a client in a temporary folder, posts a journal, rolls the year forward and produces the
Financial Reports and a Trial Balance.  It imports and cash codes a bank statement (with GST) and
produces the Profit and Loss, Balance Sheet and GST report.  It asks the Claude connector, as Claude would,
to draft a journal.  Then it opens the main screens, so a template or icon missing from the package shows
up here.  Exit code 0 means everything worked; the details go to self-test.txt
in the settings folder (a windowed build has no console).
"""
from __future__ import annotations

import datetime as dt
import json
import os
import tempfile
import traceback
from pathlib import Path

from . import __version__


def run() -> int:
    from . import reports
    from .engine import ENGINE_DIR, report_code_options
    from .ledger import Ledger
    from .model import Journal, Line
    from .money import to_dec
    from .settings import config_dir
    from .store import FolderStore

    out: list[str] = [f"Fortis Ledger {__version__} self-test {dt.datetime.now():%Y-%m-%d %H:%M:%S}",
                      f"report engine: {ENGINE_DIR}", f"report codes known: {len(report_code_options())}"]
    ok = True
    with tempfile.TemporaryDirectory(prefix="fortis-ledger-selftest-", ignore_cleanup_errors=True) as d:
        st = FolderStore(Path(d) / "library", user="self-test", machine="self-test")
        st.initialise()
        for etype in ("company", "trust", "partnership", "sole_trader"):
            try:
                c = st.create_client(f"Self Test {etype.replace('_', ' ').title()}", etype, directors=["A Person"])
                L = Ledger(st, c.slug)
                bank = next(a.code for a in L.chart.usable() if a.report_code.startswith("ASS.CUR.CAS"))
                income = next(a.code for a in L.chart.usable() if a.report_code.startswith("REV"))
                for fy in (2025, 2026):
                    L.ensure_year(fy)
                    L.save_journal(Journal(date=L.fy_range(fy)[1], narration=f"self-test FY{fy}",
                                           lines=[Line(bank, debit=to_dec(1000 * fy - 2024000)),
                                                  Line(income, credit=to_dec(1000 * fy - 2024000))]), fy)
                    if fy == 2025:
                        L.close_year(2025)
                L = Ledger(st, c.slug)
                res = reports.financial_reports(L, 2026, Path(d) / f"{etype}.pdf")
                reports.trial_balance(L, 2026, Path(d) / f"{etype}.xlsx", as_excel=True)
                size = (Path(d) / f"{etype}.pdf").stat().st_size
                errors = [v["detail"] for v in res["validation"] if v["level"] == "error"]
                good = size > 5000 and not errors and res["summary"]["npat"]["cy"] == 2000
                ok &= good
                out.append(f"{etype}: {'ok' if good else 'FAILED'} - pack {size:,} bytes, profit "
                           f"{res['summary']['npat']['cy']:,.0f} (PY {res['summary']['npat']['py']:,.0f})"
                           + (f", errors: {errors}" if errors else ""))
            except Exception:
                ok = False
                out.append(f"{etype}: FAILED\n{traceback.format_exc()}")
        try:
            good, detail = _bank(st, Path(d))
            ok &= good
            out.append(f"bank: {'ok' if good else 'FAILED'} - {detail}")
        except Exception:
            ok = False
            out.append(f"bank: FAILED\n{traceback.format_exc()}")
        try:
            good, detail = _claude(st, Path(d))
            ok &= good
            out.append(f"claude: {'ok' if good else 'FAILED'} - {detail}")
        except Exception:
            ok = False
            out.append(f"claude: FAILED\n{traceback.format_exc()}")
        try:
            bad = _screens(Path(d))
            ok &= not bad
            out.append("screens: " + (f"FAILED - {', '.join(bad)}" if bad else "ok"))
        except Exception:
            ok = False
            out.append(f"screens: FAILED\n{traceback.format_exc()}")
    out.append("PASSED" if ok else "FAILED")
    text = "\n".join(out)
    try:
        p = config_dir() / "self-test.txt"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text + "\n", encoding="utf-8")
    except OSError:
        pass
    import sys
    if sys.stdout:
        print(text, flush=True)
    return 0 if ok else 1


STATEMENT = """Date,Amount,Description,Balance
15/06/2026,2200.00,DIRECT CREDIT ACME PTY LTD INV 1001,"2,200.00"
16/06/2026,-110.00,OFFICEWORKS 0123 SYDNEY AU,"2,090.00"
"""


def _bank(st, d: Path) -> tuple[bool, str]:
    """A statement into a GST-registered company: read, imported, cash coded; then the statements and GST report."""
    from . import bank, reports, statements
    from .imports import read_table
    from .ledger import Ledger

    c = st.create_client("Self Test Bank", "company", gst_registered=True)
    L = Ledger(st, c.slug)
    L.ensure_year(2026)
    L = Ledger(st, c.slug)
    p = d / "statement.csv"
    p.write_text(STATEMENT, encoding="utf-8")
    rows = read_table(p)
    layout = bank.detect_layout(rows)
    lines, problems = bank.read_lines(rows, layout)
    acct = next(a.code for a in L.chart.usable() if bank.is_bank_account(a))
    sales = next(a.code for a in L.chart.usable() if a.report_code.startswith("REV.TRA"))
    cost = next(a.code for a in L.chart.usable() if a.report_code.startswith("EXP") and not L.is_appropriation(a.code))
    bank.import_lines(L, acct, lines, p.name, layout)
    ids = [ln["id"] for ln in bank.BankFeed(st, c.slug, acct).ordered()]
    res = bank.reconcile(L, acct, [bank.Coding(ids[0], sales, "GST on Income"), bank.Coding(ids[1], cost, "GST on Expenses")])
    L = Ledger(st, c.slug)
    gst = statements.gst_report(L, *L.fy_range(2026))
    bs = statements.balance_sheet(L, L.fy_range(2026)[1])
    for name, make in (("pl.pdf", lambda f: reports.profit_and_loss(L, f, *L.fy_range(2026))),
                       ("bs.xlsx", lambda f: reports.balance_sheet(L, f, L.fy_range(2026)[1], as_excel=True)),
                       ("gst.pdf", lambda f: reports.gst_report(L, f, *L.fy_range(2026), basis="cash"))):
        make(d / name)
    files = all((d / n).stat().st_size > 1000 for n in ("pl.pdf", "bs.xlsx", "gst.pdf"))
    net = gst["net_gst"]
    good = (not problems and len(lines) == 2 and not res["errors"] and len(res["done"]) == 2 and str(net) == "190.00"
            and bs["out_by"] == 0 and files)
    return good, (f"{len(lines)} lines read, {len(res['done'])} reconciled, net GST {net}, balance sheet out by "
                  f"{bs['out_by']}" + ("" if files else ", report files missing"))


def _claude(st, d: Path) -> tuple[bool, str]:
    """The Claude connector: both MCP handshakes, the tool list, and a GST journal drafted into a client."""
    from .connector import Connector
    from .settings import Settings

    client = next(c for c in st.list_clients() if c.name == "Self Test Bank")
    st.release_lock(client.slug)              # Claude (another program on this PC) can then take it
    conn = Connector(Settings(data_folder=str(d / "library"), user_name="self-test"))
    modern = {"io.modelcontextprotocol/protocolVersion": "2026-07-28", "io.modelcontextprotocol/clientCapabilities": {}}
    init = conn.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}})
    found = conn.handle({"jsonrpc": "2.0", "id": 2, "method": "server/discover", "params": {"_meta": modern}})
    tools = conn.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/list", "params": {"_meta": modern}})
    made = conn.handle({"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {
        "_meta": modern, "name": "create_journal", "arguments": {
            "client": client.slug, "date": "2026-06-30", "narration": "self-test accrual",
            "lines": [{"account": _expense(st, client.slug), "debit": 110, "tax_rate": "GST on Expenses"},
                      {"account": _creditor(st, client.slug), "credit": 110}]}}})
    result = made["result"]
    journal = json.loads(result["content"][0]["text"]) if not result["isError"] else {}
    good = (init["result"]["serverInfo"]["name"] == "fortis-ledger" and "2026-07-28" in found["result"]["supportedVersions"]
            and len(tools["result"]["tools"]) >= 13 and journal.get("status") == "draft"
            and len(journal.get("lines", [])) == 3)
    return good, (f"{len(tools['result']['tools'])} tools, draft {journal.get('number')}" if good
                  else result["content"][0]["text"][:300])


def _expense(st, slug: str) -> str:
    from .ledger import Ledger
    L = Ledger(st, slug)
    return next(a.code for a in L.chart.usable() if a.report_code.startswith("EXP") and not L.is_appropriation(a.code)
                and not a.report_code.startswith(("EXP.INC", "EXP.COS")))


def _creditor(st, slug: str) -> str:
    from .ledger import Ledger
    return next(a.code for a in Ledger(st, slug).chart.usable() if a.report_code.startswith("LIA.CUR.PAY"))


def _screens(d: Path) -> list[str]:
    """Open the main screens on the self-test clients; returns the ones that did not show."""
    from .settings import Settings
    from .store import FolderStore
    from .web import create_app

    home = os.environ.get("FORTIS_LEDGER_HOME")
    os.environ["FORTIS_LEDGER_HOME"] = str(d / "settings")      # keep this PC's own settings untouched
    try:
        library = d / "library"
        clients = FolderStore(library, user="self-test", machine="self-test").list_clients()
        slug = clients[0].slug
        banked = next((c.slug for c in clients if c.name == "Self Test Bank"), slug)
        app = create_app(Settings(data_folder=str(library), user_name="self-test"), token="self-test",
                         work_dir=d / "work")
        c = app.test_client()
        c.get("/?token=self-test")
        bad = []
        for path in ("/", f"/c/{slug}/", f"/c/{slug}/trial-balance", f"/c/{slug}/journals/new", f"/c/{slug}/reports",
                     f"/c/{slug}/year-end", f"/c/{slug}/profit-and-loss", f"/c/{slug}/balance-sheet",
                     f"/c/{banked}/bank", f"/c/{banked}/gst-report", f"/c/{banked}/journals/new",
                     *(f"/c/{banked}/bank/{code}" for code in FolderStore(library).list_bank_accounts(banked)),
                     "/xero", "/firm", "/setup",
                     "/signin?again=1", "/static/app.css", "/static/fortis-logo.svg"):
            r = c.get(path)
            if r.status_code != 200 or (path.startswith("/c/") and b'id="i-home"' not in r.data):
                bad.append(f"{path} ({r.status_code})")
        return bad
    finally:
        if home is None:
            os.environ.pop("FORTIS_LEDGER_HOME", None)
        else:
            os.environ["FORTIS_LEDGER_HOME"] = home
