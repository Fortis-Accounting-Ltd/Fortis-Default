"""Connect to Xero, sign in with the office email, desktop icons - end to end against a stand-in provider."""
import datetime as dt
import re
import sys
import time
from decimal import Decimal
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from fortis_ledger import shortcut
from fortis_ledger.ledger import Ledger
from fortis_ledger.settings import Settings
from fortis_ledger.store import FolderStore
from fortis_ledger.web import create_app
from fortis_ledger.xero_connect import Endpoints, has_balances, year_ends

from fake_oauth import MS_CLIENT, TENANT, XERO_CLIENT, FakeProvider, follow, free_port

TOKEN = "tok"


def tb(rows):
    return {"Reports": [{"ReportID": "TrialBalance", "Rows": [
        {"RowType": "Header", "Cells": [{"Value": "Account"}, {"Value": "Debit"}, {"Value": "Credit"},
                                        {"Value": "YTD Debit"}, {"Value": "YTD Credit"}]},
        {"RowType": "Section", "Title": "Accounts", "Rows": [
            {"RowType": "Row", "Cells": [{"Value": f"{n} ({c})", "Attributes": [{"Id": "account", "Value": i}]},
                                         {"Value": ""}, {"Value": ""}, {"Value": dr}, {"Value": cr}]}
            for i, c, n, dr, cr in rows]}]}]}


XERO_DATA = {
    "org": {"Organisations": [{"Name": "Migrated Pty Ltd", "LegalName": "Migrated Pty Ltd", "OrganisationEntityType": "COMPANY",
                               "RegistrationNumber": "64640451663", "FinancialYearEndMonth": 6, "FinancialYearEndDay": 30,
                               "OrganisationID": "org1", "Class": "STANDARD"}]},
    "accounts": {"Accounts": [
        {"AccountID": "a1", "Code": "200", "Name": "Sales", "Type": "REVENUE", "ReportingCode": "REV.TRA.GOO"},
        {"AccountID": "a2", "Code": "400", "Name": "Advertising", "Type": "EXPENSE", "ReportingCode": "EXP.ADV"},
        {"AccountID": "a3", "Code": "090", "Name": "Bank", "Type": "BANK", "ReportingCode": "ASS.CUR.CAS.BAN"},
        {"AccountID": "a4", "Code": "960", "Name": "Retained Earnings", "Type": "EQUITY", "ReportingCode": "EQU.RET"},
        {"AccountID": "a5", "Code": "970", "Name": "Owner A Share Capital", "Type": "EQUITY", "ReportingCode": "EQU.SHA"}]},
    "tbs": {"2025-06-30": tb([("a1", "200", "Sales", "", "1000.00"), ("a2", "400", "Advertising", "200.00", ""),
                              ("a3", "090", "Bank", "900.00", ""), ("a5", "970", "Owner A Share Capital", "", "100.00")]),
            "2026-06-30": tb([("a1", "200", "Sales", "", "2000.00"), ("a2", "400", "Advertising", "500.00", ""),
                              ("a3", "090", "Bank", "2400.00", ""), ("a4", "960", "Retained Earnings", "", "800.00"),
                              ("a5", "970", "Owner A Share Capital", "", "100.00")])},
}


@pytest.fixture
def provider():
    with FakeProvider(XERO_DATA) as p:
        yield p


@pytest.fixture
def lib(tmp_path, monkeypatch):
    monkeypatch.setenv("FORTIS_LEDGER_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    d = tmp_path / "library"
    d.mkdir()
    FolderStore(d, user="Tester").initialise()
    return d


@pytest.fixture
def app(tmp_path, lib, provider):
    a = create_app(Settings(data_folder=str(lib), user_name="Tester"), token=TOKEN, work_dir=tmp_path / "work")
    a.config["TESTING"] = True
    a.config["MS_AUTHORITY"] = provider.base
    a.config["XERO_ENDPOINTS"] = Endpoints(authorize=f"{provider.base}/xero/authorize",
                                           token=f"{provider.base}/xero/token", api=f"{provider.base}/xeroapi")
    a.opened = []
    a.config["BROWSER"] = a.opened.append
    return a


def client_for(app):
    c = app.test_client()
    c.get(f"/?token={TOKEN}")
    with c.session_transaction() as s:
        c.csrf = s["csrf"]
    return c


def post(c, url, data=None, **kw):
    return c.post(url, data={"_csrf": c.csrf, **(data or {})}, **kw)


def flashes(c):
    with c.session_transaction() as s:
        return [m for _, m in s.get("_flashes", [])]


def text(r):
    return re.sub(r"\s+", " ", r.get_data(as_text=True))


def wait_for(c, url, status, seconds=10):
    deadline = time.time() + seconds
    while time.time() < deadline:
        d = c.get(url).get_json()
        if d and d.get("status") == status:
            return d
        time.sleep(0.05)
    raise AssertionError(f"{url} never reached {status}")


# ---- Connect to Xero -------------------------------------------------------------------- #
def set_up_xero(c):
    port = free_port()
    r = post(c, "/firm", {"op": "xero_save", "xero_client_id": XERO_CLIENT, "xero_port": str(port)})
    assert r.status_code == 302, text(r)
    return port


def test_connect_to_xero_and_bring_a_client_across(app, provider, lib):
    c = client_for(app)
    assert "not set up yet" in text(c.get("/xero"))
    port = set_up_xero(c)
    r = post(c, "/xero/connect")
    assert r.status_code == 302 and len(app.opened) == 1
    q = parse_qs(urlsplit(app.opened[0]).query)
    assert q["client_id"] == [XERO_CLIENT] and q["redirect_uri"] == [f"http://localhost:{port}/callback"]
    assert q["code_challenge_method"] == ["S256"] and "accounting.reports.trialbalance.read" in q["scope"][0]
    assert "waiting" in c.get("/xero/status").get_json()["status"]
    status, page = follow(app.opened[0])                                 # the user signs in to Xero
    assert status == 200 and "Connected to Xero (1 organisation)" in page
    wait_for(c, "/xero/status", "signed_in")
    home = text(c.get("/xero"))
    assert "Migrated Pty Ltd" in home and "staff@fortisap.com.au" in home
    form = text(c.get("/xero/org/org1"))
    assert 'value="Migrated Pty Ltd"' in form and "FY2026" in form and "64 640 451 663" in form
    r = post(c, "/xero/org/org1", {"name": "Migrated Pty Ltd", "type": "company", "directors": "Jane Citizen",
                                   "years": ["2025-06-30", "2026-06-30"], "disconnect": "yes"})
    assert r.status_code == 302 and "/migrated" in r.headers["Location"], text(r)
    slug = r.headers["Location"].split("/c/")[1].split("/")[0]
    L = Ledger(FolderStore(lib, user="Tester"), slug)
    assert L.years() == [2025, 2026] and L.client.directors == ["Jane Citizen"]
    assert L.closing(2026)["090"] == Decimal("2400.00") and L.trial_balance(2026)["profit"] == Decimal("1500.00")
    assert L.client.source["xero_organisation_id"] == "org1"
    assert provider.deleted == ["conn-1"]                                # disconnected afterwards
    audit = [a for a in FolderStore(lib, user="Tester").read_audit(slug) if a["action"] == "xero.imported"]
    assert audit and audit[0]["detail"]["years"] == [2025, 2026]
    page = text(c.get(r.headers["Location"]))
    assert "disconnected from Fortis Ledger's Xero app" in page


def test_xero_refused_or_tampered_sign_in(app, provider):
    c = client_for(app)
    set_up_xero(c)
    post(c, "/xero/connect")
    url = app.opened[-1]
    tampered = url.replace("state=", "state=x")
    status, page = follow(tampered)
    assert status == 400 and "does not belong" in page
    assert c.get("/xero/status").get_json()["status"] == "waiting"      # a wrong state does not end the sign-in
    post(c, "/xero/cancel")
    provider.deny = True
    post(c, "/xero/connect")
    status, page = follow(app.opened[-1])
    assert status == 400 and "did not allow" in page
    assert wait_for(c, "/xero/status", "error")["error"]


def test_bringing_the_same_organisation_twice_needs_a_tick(app, provider, lib):
    c = client_for(app)
    set_up_xero(c)
    post(c, "/xero/connect")
    follow(app.opened[-1])
    wait_for(c, "/xero/status", "signed_in")
    data = {"name": "Migrated Pty Ltd", "type": "company", "years": ["2026-06-30"]}
    assert post(c, "/xero/org/org1", data).status_code == 302
    r = post(c, "/xero/org/org1", data)
    assert r.status_code == 400 and "already in the ledger" in text(r)
    assert post(c, "/xero/org/org1", {**data, "again": "yes"}).status_code == 302


def test_year_ends_and_empty_trial_balances():
    assert year_ends(6, 30, dt.date(2026, 10, 16), 3) == [dt.date(2024, 6, 30), dt.date(2025, 6, 30), dt.date(2026, 6, 30)]
    assert year_ends(6, 30, dt.date(2026, 6, 30), 1) == [dt.date(2025, 6, 30)]
    assert year_ends(12, 31, dt.date(2026, 3, 1), 1) == [dt.date(2025, 12, 31)]
    assert has_balances(XERO_DATA["tbs"]["2026-06-30"])
    assert not has_balances({"Reports": [{"Rows": []}]})


# ---- office email sign-in --------------------------------------------------------------- #
def ms_details(**kw):
    return {"tenant_id": TENANT, "ms_client_id": MS_CLIENT, "allowed_domains": "fortisap.com.au", **kw}


def sign_in(c, app):
    status, page = follow(app.opened[-1])
    assert status == 200, page
    return page


def test_office_sign_in_is_tested_then_required(app, provider, lib):
    c = client_for(app)
    r = post(c, "/firm", {"op": "ms_save", **ms_details(required="yes")})
    assert "Test sign-in" in text(r) and "before making sign-in required" in text(r)
    r = post(c, "/firm", {"op": "ms_test", **ms_details()})
    assert r.headers["Location"].endswith("/signin/test")
    assert "Sign in with Microsoft" in text(c.get("/signin/test"))
    post(c, "/signin/start", {"next": "/firm"})
    q = parse_qs(urlsplit(app.opened[-1]).query)
    assert q["client_id"] == [MS_CLIENT] and "nonce" in q and q["prompt"] == ["select_account"]
    assert sign_in(c, app).startswith("<!doctype html>")
    d = wait_for(c, "/signin/status", "signed_in")
    assert d["email"] == "alex.citizen@fortisap.com.au" and d["next"].endswith("/firm")
    r = post(c, "/firm", {"op": "ms_save", **ms_details(required="yes")})
    assert r.status_code == 302 and FolderStore(lib).firm_settings()["microsoft"]["required"] is True

    # another PC: the app now asks for the office account before anything else
    other = client_for(app)
    r = other.get("/")
    assert r.status_code == 302 and "/signin" in r.headers["Location"]
    assert "/signin" in other.get("/c/anything/journals").headers["Location"]
    post(other, "/signin/start", {"next": "/"})
    sign_in(other, app)
    wait_for(other, "/signin/status", "signed_in")
    page = text(other.get("/"))
    assert "Alex Citizen" in page and "alex.citizen@fortisap.com.au" in page
    r = post(other, "/clients/new", {"name": "Signed Co Pty Ltd", "type": "company", "fy_end_month": "6",
                                     "fy_end_day": "30", "first_fy": "2026"})
    slug = r.headers["Location"].split("/c/")[1].split("/")[0]
    assert FolderStore(lib).read_audit(slug)[-1]["user"] == "alex.citizen@fortisap.com.au"   # the change log has the email

    # after signing in the app only goes on to its own pages
    for bad in ("//evil.example/x", "/\\evil.example/x", "/\t/evil.example", "https://evil.example/"):
        assert urlsplit(other.get("/signin", query_string={"next": bad}).headers["Location"]).path == "/"
    assert other.get("/signin", query_string={"next": "/firm"}).headers["Location"].endswith("/firm")

    # a test sign-in from Firm settings checks the details but does not change who is signed in
    provider.ms_claims = {"preferred_username": "admin@fortisap.com.au", "name": "Admin", "tid": TENANT}
    post(other, "/firm", {"op": "ms_test", **ms_details()})
    post(other, "/signin/start", {"next": "/firm"})
    sign_in(other, app)
    assert wait_for(other, "/signin/status", "signed_in")["email"] == "admin@fortisap.com.au"
    page = text(other.get("/firm"))
    assert "Tested: admin@fortisap.com.au" in page and "alex.citizen@fortisap.com.au" in page
    assert Settings.load().last_email == "alex.citizen@fortisap.com.au"
    post(other, "/signout")
    assert "/signin" in other.get("/").headers["Location"]


def test_office_sign_in_refuses_other_accounts(app, provider):
    c = client_for(app)
    post(c, "/firm", {"op": "ms_test", **ms_details()})
    provider.ms_claims = {"preferred_username": "someone@gmail.com", "name": "Someone", "tid": TENANT}
    post(c, "/signin/start", {"next": "/firm"})
    status, page = follow(app.opened[-1])
    assert status == 400 and "not a firm email address" in page
    assert wait_for(c, "/signin/status", "error")
    post(c, "/firm", {"op": "ms_test", **ms_details()})
    provider.ms_claims = {"preferred_username": "alex.citizen@fortisap.com.au", "name": "R", "tid": "99999999-0000-0000-0000-000000000000"}
    post(c, "/signin/start", {"next": "/firm"})
    status, page = follow(app.opened[-1])
    assert status == 400 and "not in the firm's Microsoft 365" in page


def test_firm_settings_check_the_ids(app):
    c = client_for(app)
    r = post(c, "/firm", {"op": "ms_save", "tenant_id": "fortisap.com.au", "ms_client_id": MS_CLIENT})
    assert "Directory (tenant) ID looks like" in text(r)
    r = post(c, "/firm", {"op": "xero_save", "xero_client_id": "not a client id!"})
    assert "Configuration page" in text(r)


# ---- desktop icons ------------------------------------------------------------------------ #
def test_desktop_icon_button(app, lib):
    made = []
    app.config["SHORTCUT"] = lambda kind: made.append(kind) or f"C:\\Users\\me\\Desktop\\{shortcut.ICONS[kind]['name']}.lnk"
    c = client_for(app)
    r = post(c, "/desktop-icon", {"kind": "xero"})
    assert r.status_code == 302 and made == ["xero"]
    assert any("Connect to Xero.lnk" in m for m in flashes(c))
    assert Settings.load().icons_made == ["xero"]


def test_shortcut_is_made_with_windows_shortcut_maker(monkeypatch):
    calls = []

    class Done:
        returncode, stdout, stderr = 0, "C:\\Users\\me\\Desktop\\Fortis Ledger.lnk\n", ""

    monkeypatch.setattr(shortcut, "available", lambda: True)
    path = shortcut.create("app", runner=lambda cmd, **kw: calls.append((cmd, kw)) or Done())
    cmd, kw = calls[0]
    assert cmd[0] == "powershell" and "WScript.Shell" in cmd[-1] and "GetFolderPath('Desktop')" in cmd[-1]
    assert kw["env"]["FL_NAME"] == "Fortis Ledger" and kw["env"]["FL_ARGS"].endswith("fortis_ledger")
    # run from the source code: the icon starts in the folder that holds fortis_ledger/, so "-m fortis_ledger" works
    assert (Path(kw["env"]["FL_DIR"]) / "fortis_ledger" / "shortcut.py").is_file()
    assert path.endswith("Fortis Ledger.lnk")
    shortcut.create("xero", runner=lambda cmd, **kw: calls.append((cmd, kw)) or Done())
    assert calls[-1][1]["env"]["FL_ARGS"].endswith("--start xero")

    # the installed app: the icon runs Fortis Ledger.exe itself, in its own folder, with its own icon
    exe = r"C:\Program Files\Fortis Ledger\Fortis Ledger.exe"
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", exe)
    shortcut.create("xero", runner=lambda cmd, **kw: calls.append((cmd, kw)) or Done())
    env = calls[-1][1]["env"]
    assert env["FL_TARGET"] == exe and env["FL_ARGS"] == "--start xero" and env["FL_ICON"] == f"{exe},0"
    assert env["FL_DIR"] == str(Path(exe).parent)


def test_shortcut_needs_windows(monkeypatch):
    monkeypatch.setattr(shortcut, "available", lambda: False)
    with pytest.raises(shortcut.ShortcutError):
        shortcut.create("app")
