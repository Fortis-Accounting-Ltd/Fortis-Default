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
    page.click("text=New client")
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

    page.click(".tabs >> text=Trial balance")
    assert "1,234.50" in page.locator("tfoot").inner_text()
    shot(page, "06-trial-balance")
    for tab, name in (("Chart of accounts", "07-accounts"), ("Import", "08-import"), ("Reports", "09-reports"),
                      ("Year end", "10-year-end"), ("Change log", "11-changes"), ("Client details", "12-details")):
        page.click(f".tabs >> text={tab}")
        shot(page, name)
    page.click(".tabs >> text=Reports")
    page.click("button:has-text('Generate PDF')")
    page.wait_for_selector("text=saved in the client's reports folder")
    shot(page, "13-report-generated")
    assert not [e for e in page.errors if "favicon" not in e], page.errors
