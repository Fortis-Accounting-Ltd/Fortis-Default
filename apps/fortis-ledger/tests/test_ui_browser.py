"""
The screens in a real browser (Chromium via Playwright): the scripts run under the strict content
security policy, the journal editor totals and adds lines, and a journal posts end to end.
Skipped when Playwright or its browser is not installed.  Set FL_SCREENSHOTS=<folder> to keep screenshots.
"""
import os
import threading
from pathlib import Path

import pytest

from fortis_ledger.settings import Settings
from fortis_ledger.store import FolderStore
from fortis_ledger.web import create_app

sync_api = pytest.importorskip("playwright.sync_api")


@pytest.fixture
def server(tmp_path, monkeypatch):
    from werkzeug.serving import make_server
    monkeypatch.setenv("FORTIS_LEDGER_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    lib = tmp_path / "library"
    lib.mkdir()
    FolderStore(lib, user="Tester").initialise()
    app = create_app(Settings(data_folder=str(lib), user_name="Tester"), token="tok", work_dir=tmp_path / "work")
    app.config["OPENER"] = lambda p, folder: None
    srv = make_server("127.0.0.1", 0, app, threaded=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}", lib
    srv.shutdown()


@pytest.fixture
def page(server):
    with sync_api.sync_playwright() as p:
        browser = None
        for exe in (None, os.environ.get("FL_CHROMIUM"), "/opt/pw-browsers/chromium"):
            if exe is not None and not Path(exe).exists():
                continue
            try:
                browser = p.chromium.launch(executable_path=exe) if exe else p.chromium.launch()
                break
            except Exception:
                continue
        if browser is None:      # no browser on this machine
            pytest.skip("Chromium is not available (set FL_CHROMIUM to its path)")
        pg = browser.new_page(viewport={"width": 1366, "height": 900})
        pg.errors = []
        pg.on("console", lambda m: pg.errors.append(m.text) if m.type == "error" else None)
        pg.on("pageerror", lambda e: pg.errors.append(str(e)))
        yield pg
        browser.close()


def shot(page, name):
    d = os.environ.get("FL_SCREENSHOTS")
    if d:
        Path(d).mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(Path(d) / f"{name}.png"), full_page=True)


def test_post_a_journal_in_the_browser(server, page):
    base, lib = server
    page.goto(f"{base}/?token=tok")
    assert page.locator("h1").inner_text() == "Clients"
    shot(page, "01-clients")
    page.click(".page-head >> text=New client")
    page.fill("#name", "Browser Trust")
    page.select_option("#type", "trust")
    assert page.locator("#trustee").is_visible()          # trust-only fields appear for a trust
    page.select_option("#type", "company")
    assert not page.locator("#trustee").is_visible()
    page.select_option("#type", "trust")
    page.fill("#trustee", "Browser Holdings Pty Ltd")
    page.fill("#directors", "Ann Example")
    page.fill("#first_fy", "2026")
    shot(page, "02-new-client")
    page.click("button:has-text('Create client')")
    page.wait_for_selector("text=FY2026 at a glance")
    shot(page, "03-overview")

    page.click(".actions >> text=New journal")
    page.fill("#date", "30/06/2026")
    page.fill("#narration", "Year-end adjustments")
    page.fill("input[name=account_0]", "18500")
    page.fill("input[name=debit_0]", "1234.5")
    page.fill("input[name=account_1]", "58900")
    page.fill("input[name=credit_1]", "1,000")
    page.locator("input[name=credit_1]").blur()
    assert "234.50 more debits" in page.locator("#difference").inner_text()
    page.click("#add-line")
    page.fill("input[name=account_4]", "58900")
    page.fill("input[name=credit_4]", "234.50")
    page.locator("input[name=credit_4]").blur()
    assert page.locator("#difference").inner_text() == "Balanced"
    assert page.locator("input[name=debit_0]").input_value() == "1,234.50"
    assert page.locator("tbody tr").first.locator(".acct-name").inner_text() != ""
    shot(page, "04-journal")
    page.click("button[value=post]")
    page.wait_for_selector("text=J2026-0001 posted.")
    shot(page, "05-journal-posted")

    page.click(".nav-item:has-text('Trial balance')")
    assert "1,234.50" in page.locator("tfoot").inner_text()
    shot(page, "06-trial-balance")
    for tab, name in (("Chart of accounts", "07-accounts"), ("Import", "08-import"), ("Reports", "09-reports"),
                      ("Year end", "10-year-end"), ("Change log", "11-changes"), ("Client details", "12-details")):
        page.click(f".nav-item:has-text('{tab}')")
        shot(page, name)
    page.click(".nav-item:has-text('Reports')")
    page.click("button:has-text('Generate PDF')")
    page.wait_for_selector("text=saved in the client's reports folder")
    shot(page, "13-report-generated")
    assert not [e for e in page.errors if "favicon" not in e], page.errors


STATEMENT = """Date,Amount,Description,Balance
15/06/2026,2000.00,DIRECT CREDIT ACME PTY LTD INV 1001,"2,000.00"
16/06/2026,-55.00,OFFICEWORKS 0123 SYDNEY AU Card xx1234,"1,945.00"
17/06/2026,-11.00,MONTHLY ACCOUNT FEE,"1,934.00"
"""


def test_bank_statement_cash_coding_and_reports_in_the_browser(server, page, tmp_path):
    base, lib = server
    page.goto(f"{base}/?token=tok")
    page.click(".page-head >> text=New client")
    page.fill("#name", "Coding Co Pty Ltd")
    page.select_option("#type", "company")
    page.check("#gst_registered")
    page.fill("#first_fy", "2026")
    page.click("button:has-text('Create client')")
    page.wait_for_selector("text=Bring in the bank")

    # import a statement: the columns are found, checked on screen, then imported
    page.click(".nav-item:has-text('Bank')")
    stmt = tmp_path / "CommBank statement.csv"
    stmt.write_text(STATEMENT, encoding="utf-8")
    page.select_option("#stmt-account", "18500")
    page.set_input_files("#stmt-file", str(stmt))
    shot(page, "30-bank")
    page.click("button:has-text('Upload and check')")
    page.wait_for_selector("h1:has-text('Check the statement')")
    assert page.locator("select[name=role_0]").input_value() == "date"
    assert page.locator("select[name=role_1]").input_value() == "amount"
    shot(page, "31-check-columns")
    page.click("button:has-text('Import 3 lines')")
    page.wait_for_selector("text=3 line(s) imported")

    # cash code: one line by the bulk bar, one typed, one to an account made in the pop-up
    rows = page.locator("#coding-table tbody tr")
    assert rows.count() == 3
    rows.nth(0).locator("input[name=pick]").check()
    page.fill("#bulk-account", "51100")
    page.click("#bulk-apply")
    assert rows.nth(0).locator("input.account").input_value() == "51100"
    assert rows.nth(0).locator("select.tax").input_value() == "GST on Income"
    rows.nth(1).locator("input.account").fill("72100")
    rows.nth(1).locator("input.account").press("Enter")              # Enter moves on to the next line
    assert rows.nth(1).locator("input[name=pick]").is_checked()        # coding a line ticks it
    assert rows.nth(1).locator("select.tax").input_value() == "GST on Expenses"
    assert rows.nth(1).locator(".acct-name").inner_text() != ""
    rows.nth(2).locator("input.account").focus()
    page.click(".sticky-actions >> text=New account")
    page.wait_for_selector("#new-account[open]")
    page.select_option("#na-type", "Expense")
    assert page.locator("#na-tax").input_value() == "GST on Expenses"
    page.fill("#na-code", "72510")
    page.fill("#na-name", "Bank fees")
    page.select_option("#na-tax", "BAS Excluded")
    shot(page, "32-new-account")
    page.click("#new-account-form button:has-text('Add account')")
    page.wait_for_selector("#new-account", state="hidden")
    assert rows.nth(2).locator("input.account").input_value() == "72510"
    assert rows.nth(2).locator(".acct-name").inner_text() == "Bank fees"
    assert rows.nth(2).locator("select.tax").input_value() == "BAS Excluded"
    assert page.locator("#reconcile-btn").inner_text() == "Reconcile 3 ticked lines"
    shot(page, "33-cash-coding")
    page.click("#reconcile-btn")
    page.wait_for_selector("text=3 line(s) reconciled")
    assert page.locator("text=Everything is reconciled").is_visible()
    page.click(".segmented >> text=Reconciled")
    assert "B2026-0003" in page.locator("table.grid").inner_text()
    shot(page, "34-reconciled")

    # the dashboard card: statement and ledger agree
    page.click(".nav-item:has-text('Overview')")
    card = page.locator(".bank-card").first
    assert "All reconciled" in card.inner_text() and "Difference" not in card.inner_text()
    assert card.locator("dd").first.inner_text().startswith("1,934.00")
    shot(page, "35-overview-bank")

    # a journal with GST: the GST line is worked out and posted for you
    page.click(".actions >> text=New journal")
    page.fill("#narration", "Accrued accounting fee")
    page.fill("input[name=account_0]", "72930")
    page.locator("input[name=account_0]").blur()
    assert page.locator("select[name=tax_0]").input_value() == "GST on Expenses"
    page.fill("input[name=debit_0]", "110")
    page.fill("input[name=account_1]", "31300")
    page.locator("input[name=account_1]").blur()
    page.fill("input[name=credit_1]", "110")
    page.locator("input[name=credit_1]").blur()
    assert page.locator("#difference").inner_text() == "Balanced"
    assert "GST 10.00" in page.locator("#gst-note").inner_text()
    shot(page, "36-journal-gst")
    page.click("button[value=post]")
    page.wait_for_selector("text=J2026-0001 posted.")
    assert "GST" in page.locator("table.grid").inner_text() and "100.00" in page.locator("table.grid").inner_text()

    # reports
    page.click(".nav-item:has-text('Profit and loss')")
    assert "Net Profit" in page.locator("table.statement").inner_text()
    shot(page, "37-profit-and-loss")
    page.click(".nav-item:has-text('Balance sheet')")
    assert "Total Equity" in page.locator("table.statement").inner_text()
    shot(page, "38-balance-sheet")
    page.click(".nav-item:has-text('GST report')")
    assert "181.82" in page.locator(".cards").inner_text()                 # 1A: GST in the 2,000 received
    accrual = page.locator(".cards").inner_text()
    assert "15.00" in accrual                                              # 1B: 5.00 bank + 10.00 journal
    shot(page, "39-gst-accrual")
    page.click(".segmented >> text=Cash basis")
    assert "journal line(s) with a tax rate are left out" in page.locator(".cards").inner_text()
    shot(page, "40-gst-cash")
    page.click(".nav-item:has-text('All reports')")
    shot(page, "41-reports")
    assert not [e for e in page.errors if "favicon" not in e], page.errors


def test_sign_in_and_connect_to_xero_in_the_browser(tmp_path, monkeypatch):
    """The waiting screens carry on by themselves once the browser sign-in finishes (Microsoft 365, then Xero)."""
    from werkzeug.serving import make_server

    from fake_oauth import MS_CLIENT, TENANT, XERO_CLIENT, FakeProvider, follow, free_port
    from fortis_ledger.xero_connect import Endpoints
    from test_connect import XERO_DATA

    monkeypatch.setenv("FORTIS_LEDGER_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    lib = tmp_path / "library"
    lib.mkdir()
    st = FolderStore(lib, user="Tester")
    st.initialise()
    st.save_firm_settings({"microsoft": {"tenant_id": TENANT, "client_id": MS_CLIENT, "allowed_domains": ["fortisap.com.au"],
                                         "required": True},
                           "xero": {"client_id": XERO_CLIENT, "port": free_port()}})
    with FakeProvider(XERO_DATA) as provider, sync_api.sync_playwright() as p:
        app = create_app(Settings(data_folder=str(lib), user_name="Tester"), token="tok", work_dir=tmp_path / "work")
        app.config["MS_AUTHORITY"] = provider.base
        app.config["XERO_ENDPOINTS"] = Endpoints(authorize=f"{provider.base}/xero/authorize",
                                                 token=f"{provider.base}/xero/token", api=f"{provider.base}/xeroapi")
        opened = []
        app.config["BROWSER"] = opened.append
        srv = make_server("127.0.0.1", 0, app, threaded=True)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{srv.server_port}"
        browser = None
        for exe in (None, os.environ.get("FL_CHROMIUM"), "/opt/pw-browsers/chromium"):
            if exe is not None and not Path(exe).exists():
                continue
            try:
                browser = p.chromium.launch(executable_path=exe) if exe else p.chromium.launch()
                break
            except Exception:
                continue
        if browser is None:
            srv.shutdown()
            pytest.skip("Chromium is not available")
        page = browser.new_page(viewport={"width": 1366, "height": 900})
        errors = []
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        page.on("pageerror", lambda e: errors.append(str(e)))
        try:
            page.goto(f"{base}/?token=tok")
            page.wait_for_selector("text=Sign in to Fortis Ledger")
            shot(page, "20-sign-in")
            page.click("button:has-text('Sign in with Microsoft')")
            page.wait_for_selector(".waiting")
            shot(page, "21-sign-in-waiting")
            assert follow(opened[-1])[0] == 200                     # the user signs in in their browser
            page.wait_for_selector("h1:has-text('Clients')", timeout=10000)
            assert "alex.citizen@fortisap.com.au" in page.locator(".user-menu").inner_text()
            shot(page, "22-signed-in")
            page.click(".nav-item:has-text('Connect to Xero')")
            shot(page, "23-xero")
            page.click("button:has-text('Connect to Xero')")
            page.wait_for_selector(".waiting")
            assert follow(opened[-1])[0] == 200
            page.wait_for_selector("text=Migrated Pty Ltd", timeout=10000)
            shot(page, "24-xero-connected")
            page.click("a:has-text('Bring across')")
            page.wait_for_selector("text=Year ends to bring across")
            shot(page, "25-xero-org")
            page.click("button:has-text('Bring across into Fortis Ledger')")
            page.wait_for_selector("text=is in the ledger", timeout=15000)
            shot(page, "26-xero-done")
            page.goto(f"{base}/firm")
            shot(page, "27-firm-settings")
        finally:
            browser.close()
            srv.shutdown()
        assert not [e for e in errors if "favicon" not in e], errors
