"""
The board screen in a real browser (Chromium via Playwright): the scripts run under the strict content security
policy, a status is changed from its picker and an automation moves the item, an item is added, an update with an
@mention is posted, and the kanban view and a filter work.  Skipped when Playwright or Chromium is not installed.
Set FW_SCREENSHOTS=<folder> to keep screenshots; FW_CHROMIUM=<path> to choose the browser.
"""
import os
import threading
from pathlib import Path

import pytest

from forwork import automations
from forwork.config import Config
from forwork.db import Database
from forwork.service import Service
from forwork.web import create_app

sync_api = pytest.importorskip("playwright.sync_api")


@pytest.fixture
def server(tmp_path):
    from werkzeug.serving import make_server
    cfg = Config(data_dir=tmp_path / "data", secret_key="k" * 64, dev_login=True)
    cfg.data_dir.mkdir()
    db = Database(cfg.db_path)
    svc = Service(db)
    alex = svc.create_user(None, "alex@example.com.au", "Alex Citizen", role="admin")
    svc.create_user(alex, "jo@example.com.au", "Jo Bloggs")
    ws = svc.create_workspace(alex, "Main workspace")
    bid = svc.create_board(alex, "Annual Compliance", ws)
    review = svc.add_group(alex, bid, "Review", where="bottom")
    status = next(c for c in svc.columns(bid) if c["title"] == "Status")
    automations.save_rule(svc, alex, bid, {"type": "status_change", "column": status["id"], "to": 1},
                          {"type": "move_to_group", "group": review})
    for n in range(5):
        svc.create_item(alex, bid, f"Example {n} Pty Ltd")
    app = create_app(cfg, db=db)
    srv = make_server("127.0.0.1", 0, app, threaded=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}", bid, svc, review
    srv.shutdown()


@pytest.fixture
def page(server):
    with sync_api.sync_playwright() as p:
        browser = None
        for exe in (None, os.environ.get("FW_CHROMIUM"), "/opt/pw-browsers/chromium"):
            if exe is not None and not Path(exe).exists():
                continue
            try:
                browser = p.chromium.launch(executable_path=exe) if exe else p.chromium.launch()
                break
            except Exception:
                continue
        if browser is None:
            pytest.skip("Chromium is not available (set FW_CHROMIUM to its path)")
        pg = browser.new_page(viewport={"width": 1400, "height": 900})
        pg.errors = []
        pg.on("pageerror", lambda e: pg.errors.append(str(e)))
        pg.on("console", lambda m: pg.errors.append(m.text) if m.type == "error" else None)
        yield pg
        browser.close()


def shot(pg, name):
    folder = os.environ.get("FW_SCREENSHOTS")
    if folder:
        Path(folder).mkdir(parents=True, exist_ok=True)
        pg.screenshot(path=str(Path(folder) / f"{name}.png"))


def sign_in(pg, base):
    pg.goto(base + "/signin")
    pg.select_option("select[name=user]", label="Alex Citizen (alex@example.com.au, admin)")
    pg.click("text=Sign in")
    pg.wait_for_url(base + "/")


def test_board_screen_end_to_end(server, page):
    base, bid, svc, review = server
    sign_in(page, base)
    shot(page, "home")
    page.goto(f"{base}/boards/{bid}")
    page.wait_for_selector("tr.row")
    assert page.locator("tr.row").count() == 5
    shot(page, "board")

    # change a status from the picker; the automation moves the item to Review
    row = page.locator("tr.row:has(.name-text:text-is('Example 0 Pty Ltd'))")
    row.locator("td.t-status").click()
    page.click("#popover .label-btn:text-is('Done')")
    page.wait_for_selector("section.group:has(.group-title:text-is('Review')) tr.row:has(.name-text:text-is('Example 0 Pty Ltd'))")

    # add an item
    page.fill("section.group:has(.group-title:text-is('Group 1')) input[data-add-item]", "New client Pty Ltd")
    page.press("section.group:has(.group-title:text-is('Group 1')) input[data-add-item]", "Enter")
    page.wait_for_selector("tr.row:has(.name-text:text-is('New client Pty Ltd'))")

    # write an update with an @mention
    page.locator("tr.row:has(.name-text:text-is('New client Pty Ltd')) .open-item").click()
    editor = page.locator("#item-panel [data-new-update] .editor")
    editor.click()
    page.keyboard.type("Over to you @Jo")
    page.wait_for_selector("#popover [data-mention] li")
    page.locator("#popover [data-mention] li").first.dispatch_event("mousedown")
    page.click("#item-panel [data-act=post-update]")
    page.wait_for_selector("#item-panel .update .mention")
    shot(page, "update")
    jo = svc.user_by_email("jo@example.com.au")
    assert svc.unread_count(jo) == 1
    page.click("#item-panel [data-act=close-panel]")

    # kanban view
    page.click("[data-act=new-view]")
    page.fill("#dlg [name=name]", "Cards")
    page.select_option("#dlg [name=type]", "kanban")
    page.click("#dlg button.primary")
    page.wait_for_selector(".kan-col")
    shot(page, "kanban")

    # back to the table, filter to Done
    page.click(".view-tab:text-is('Main table')")
    page.click("[data-act=filter]")
    page.click("#popover details:has(summary:has-text('Status')) summary")
    page.click("#popover details:has(summary:has-text('Status')) li:has-text('Done')")
    page.click("#popover [data-pop=apply]")
    page.wait_for_function("document.querySelectorAll('tr.row').length === 1")
    assert page.errors == []


def test_viewer_sees_a_read_only_board(server, page):
    base, bid, svc, review = server
    admin = svc.user_by_email("alex@example.com.au")
    svc.create_user(admin, "sam@example.com.au", "Sam Viewer", role="viewer")
    page.goto(base + "/signin")
    page.select_option("select[name=user]", label="Sam Viewer (sam@example.com.au, viewer)")
    page.click("text=Sign in")
    page.goto(f"{base}/boards/{bid}")
    page.wait_for_selector("tr.row")
    assert page.locator("[data-act=new-item]").count() == 0
    assert page.locator("input[data-add-item]").count() == 0
    page.locator("tr.row td.t-status").first.click()
    assert page.locator("#popover .label-btn").count() == 0
    assert page.errors == []
