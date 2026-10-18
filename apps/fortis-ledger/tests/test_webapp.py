import csv
import datetime as dt
import io
import json
import re
from decimal import Decimal

import openpyxl
import pytest

from fortis_ledger import bank
from fortis_ledger.ledger import Ledger
from fortis_ledger.settings import Settings
from fortis_ledger.store import FolderStore
from fortis_ledger.web import create_app

TOKEN = "test-token"


@pytest.fixture
def lib(tmp_path, monkeypatch):
    monkeypatch.setenv("FORTIS_LEDGER_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    d = tmp_path / "library"
    d.mkdir()
    FolderStore(d, user="Tester").initialise()
    return d


@pytest.fixture
def app(tmp_path, lib):
    a = create_app(Settings(data_folder=str(lib), user_name="Tester"), token=TOKEN, work_dir=tmp_path / "work")
    a.config["TESTING"] = True
    a.opened = []
    a.config["OPENER"] = lambda p, folder: a.opened.append((p, folder))
    return a


@pytest.fixture
def web(app):
    c = app.test_client()
    r = c.get(f"/?token={TOKEN}")
    assert r.status_code == 302 and "token" not in r.headers["Location"]
    with c.session_transaction() as s:
        c.csrf = s["csrf"]
    return c


def post(c, url, data=None, **kw):
    data = dict(data or {})
    data.setdefault("_csrf", c.csrf)
    return c.post(url, data=data, **kw)


def flashes(c):
    with c.session_transaction() as s:
        return [m for _, m in s.get("_flashes", [])]


def text(r) -> str:
    return re.sub(r"\s+", " ", r.get_data(as_text=True))


@pytest.fixture
def company(web, lib):
    r = post(web, "/clients/new", {"name": "Web Co Pty Ltd", "type": "company", "abn": "12345678901",
                                   "fy_end_month": "6", "fy_end_day": "30", "first_fy": "2026",
                                   "directors": "Jane Citizen\nJohn Citizen"})
    assert r.status_code == 302
    slug = r.headers["Location"].split("/c/")[1].split("/")[0]
    return slug


def journal_form(date, narration, *lines, op="post", number="", jfy=2026):
    d = {"date": date, "narration": narration, "op": op, "number": number, "jfy": str(jfy)}
    for i, (acct, dr, cr) in enumerate(lines):
        d.update({f"account_{i}": acct, f"debit_{i}": dr, f"credit_{i}": cr, f"desc_{i}": ""})
    return d


def store_for(lib):
    return FolderStore(lib, user="Tester")


# ---- access ------------------------------------------------------------------------------ #
def test_only_this_pc_with_the_token(app):
    c = app.test_client()
    assert c.get("/").status_code == 403
    assert c.get("/?token=wrong").status_code == 403
    assert c.get(f"/?token={TOKEN}", headers={"Host": "evil.example.com"}).status_code == 403
    c.get(f"/?token={TOKEN}")
    assert c.get("/").status_code == 200
    assert c.post("/clients/new", data={"name": "x", "type": "company"}).status_code == 400   # no CSRF token
    r = c.get("/")
    assert "frame-ancestors 'none'" in r.headers["Content-Security-Policy"]
    assert "script-src 'self'" in r.headers["Content-Security-Policy"]


def test_setup_creates_the_data_folder(tmp_path, monkeypatch):
    monkeypatch.setenv("FORTIS_LEDGER_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    app = create_app(Settings(), token=TOKEN, work_dir=tmp_path / "work")
    c = app.test_client()
    c.get(f"/?token={TOKEN}")
    with c.session_transaction() as s:
        c.csrf = s["csrf"]
    assert c.get("/").headers["Location"].endswith("/setup")
    target = tmp_path / "Fortis Ledger - Documents"
    target.mkdir()
    r = post(c, "/setup", {"data_folder": str(target), "user_name": "Alex"})
    assert r.status_code == 200 and "not a Fortis Ledger data folder yet" in text(r)
    r = post(c, "/setup", {"data_folder": str(target), "user_name": "Alex", "create": "yes"})
    assert r.status_code == 302 and (target / "fortis-ledger.json").exists()
    assert Settings.load().data_folder == str(target) and Settings.load().user_name == "Alex"
    assert c.get("/").status_code == 200


# ---- clients and journals ---------------------------------------------------------------- #
def test_new_client_journal_and_trial_balance(web, company, lib):
    r = web.get(f"/c/{company}/")
    assert r.status_code == 200 and "Web Co Pty Ltd" in text(r) and "FY2026" in text(r)
    r = post(web, f"/c/{company}/journals/save",
             journal_form("30/06/2026", "Opening position", ("18500", "15,000.00", ""), ("43000", "", "15000")))
    assert r.status_code == 302, text(r)
    number = r.headers["Location"].rsplit("/", 1)[1]
    assert number == "J2026-0001"
    view = text(web.get(r.headers["Location"]))
    assert "Opening position" in view and "Posted" in view
    tb = text(web.get(f"/c/{company}/trial-balance?fy=2026"))
    assert "15,000.00" in tb and "out by" not in tb
    L = Ledger(store_for(lib), company)
    assert L.closing(2026) == {"18500": Decimal("15000.00"), "43000": Decimal("-15000.00")}
    c = L.client
    assert c.abn == "12 345 678 901" and c.directors == ["Jane Citizen", "John Citizen"]


def test_journal_errors_keep_what_was_typed(web, company, lib):
    r = post(web, f"/c/{company}/journals/save",
             journal_form("30/06/2026", "Unbalanced", ("72930", "100", ""), ("31300", "", "90")))
    page = text(r)
    assert r.status_code == 200 and "differ by 10.00" in page and 'value="72930"' in page
    r = post(web, f"/c/{company}/journals/save",
             journal_form("30/06/2026", "Typo", ("72930", "1O0", ""), ("31300", "", "100")))
    assert r.status_code == 200 and "not an amount" in text(r)
    r = post(web, f"/c/{company}/journals/save",
             journal_form("1/07/2026", "Next year", ("72930", "1", ""), ("31300", "", "1")))
    assert "has not been started" in text(r)
    r = post(web, f"/c/{company}/journals/save",
             journal_form("30/06/2026", "Half done", ("72930", "100", ""), op="draft"))
    assert r.status_code == 302
    L = Ledger(store_for(lib), company)
    assert [j.status for j in L.journals(2026, statuses=None)] == ["draft"]
    assert L.closing(2026) == {}


def test_void_reverse_copy_and_delete(web, company, lib):
    post(web, f"/c/{company}/journals/save",
         journal_form("30/06/2026", "Accrual", ("72930", "500", ""), ("31300", "", "500")))
    r = post(web, f"/c/{company}/journals/2026/J2026-0001/void", {"reason": ""})
    assert r.status_code == 302 and any("reason" in m for m in flashes(web))
    r = post(web, f"/c/{company}/journals/2026/J2026-0001/reverse", {"on": "30/06/2026"})
    assert r.status_code == 302
    L = Ledger(store_for(lib), company)
    assert L.closing(2026) == {}
    assert L.get_journal(2026, "J2026-0001").reversed_by == "J2026-0002"
    copy = text(web.get(f"/c/{company}/journals/new?fy=2026&copy=2026/J2026-0001"))
    assert 'value="Accrual"' in copy and 'value="500.00"' in copy
    post(web, f"/c/{company}/journals/2026/J2026-0002/void", {"reason": "reversed in error"})
    assert Ledger(store_for(lib), company).get_journal(2026, "J2026-0002").status == "void"
    post(web, f"/c/{company}/journals/save", journal_form("30/06/2026", "Draft", ("72930", "1", ""), op="draft"))
    r = post(web, f"/c/{company}/journals/2026/J2026-0003/delete")
    assert r.status_code == 302 and not (lib / "clients" / company / "years" / "2026" / "journals" / "J2026-0003.json").exists()
    listing = text(web.get(f"/c/{company}/journals?fy=2026&status=void"))
    assert ">J2026-0002</a>" in listing and ">J2026-0001</a>" not in listing


def test_editing_a_posted_journal_is_logged(web, company, lib):
    post(web, f"/c/{company}/journals/save", journal_form("30/06/2026", "Fees", ("72930", "100", ""), ("31300", "", "100")))
    assert "Save changes" in text(web.get(f"/c/{company}/journals/2026/J2026-0001?edit=1"))
    r = post(web, f"/c/{company}/journals/save",
             journal_form("30/06/2026", "Fees corrected", ("72930", "120", ""), ("31300", "", "120"), number="J2026-0001"))
    assert r.status_code == 302
    L = Ledger(store_for(lib), company)
    assert L.closing(2026)["72930"] == Decimal("120.00")
    upd = [a for a in L.store.read_audit(company) if a["action"] == "journal.updated"]
    assert upd and upd[0]["detail"]["before"]["narration"] == "Fees"
    log = text(web.get(f"/c/{company}/changes?q=corrected"))
    assert "journal.updated" in log


# ---- accounts ---------------------------------------------------------------------------- #
def test_accounts_add_edit_delete(web, company, lib):
    r = post(web, f"/c/{company}/accounts/add", {"code": "61999", "name": "Website costs", "report_code": "EXP.ADM"})
    assert r.status_code == 302
    r = post(web, f"/c/{company}/accounts/add", {"code": "61999", "name": "Again", "report_code": "EXP.ADM"})
    assert r.status_code == 400 and "already exists" in text(r)
    r = post(web, f"/c/{company}/accounts/add", {"code": "62000X", "name": "Bad", "report_code": "NOPE.XX"})
    assert "unknown report code" in text(r)
    r = post(web, f"/c/{company}/accounts/61999", {"name": "Website and hosting", "report_code": "EXP.ADM",
                                                   "reporting_name": "Website", "type": "Expense"})
    assert r.status_code == 302
    L = Ledger(store_for(lib), company)
    a = L.chart.get("61999")
    assert a.name == "Website and hosting" and a.reporting_name == "Website"
    post(web, f"/c/{company}/journals/save", journal_form("30/06/2026", "Hosting", ("61999", "50", ""), ("18500", "", "50")))
    r = post(web, f"/c/{company}/accounts/61999", {"op": "delete"})
    assert any("cannot be deleted" in m for m in flashes(web))
    assert Ledger(store_for(lib), company).chart.get("61999") is not None
    export = web.get(f"/c/{company}/accounts/export.csv")
    rows = list(csv.reader(io.StringIO(export.get_data(as_text=True))))
    assert rows[0][0] == "*Code" and any(r[0] == "61999" for r in rows)
    page = text(web.get(f"/c/{company}/accounts?q=&show=used&fy=2026"))
    assert "61999" in page and "Website and hosting" in page


# ---- imports ----------------------------------------------------------------------------- #
def upload(web, slug, kind, name, content, **fields):
    data = {"kind": kind, "_csrf": web.csrf, **fields,
            "file": (io.BytesIO(content if isinstance(content, bytes) else content.encode()), name)}
    return web.post(f"/c/{slug}/import/upload", data=data, content_type="multipart/form-data")


TB_CSV = "Code,Name,Debit,Credit\n18500,Cheque account,15000,\n51100,Sales,,50000\n72930,Accounting fees,5000,\n" \
         "13600,Trade debtors,40000,\n43000,Retained earnings,,10000\nZ1,Mystery suspense,0.40,\n"


def test_import_trial_balance_review_and_apply(web, company, lib):
    r = upload(web, company, "balances", "tb 2026.csv", TB_CSV, fy="2026")
    assert r.status_code == 302 and "/import/" in r.headers["Location"]
    review = web.get(r.headers["Location"])
    page = text(review)
    assert "Check the import" in page and "Trade debtors" in page and "rounding" in page
    # make the mystery line (the file's 6th row) a new account with a report code of our choosing
    html = review.get_data(as_text=True)
    assert "Mystery suspense" in html.split('name="action_5"')[0].rsplit('name="action_4"', 1)[1]
    form = {"action_5": "new", "account_5": "10150", "name_5": "Clearing account", "rc_5": "ASS.CUR",
            "op": "apply", "fy": "2026"}
    r = post(web, r.headers["Location"], form)
    assert r.status_code == 302 and "trial-balance" in r.headers["Location"], text(r)
    L = Ledger(store_for(lib), company)
    bal = L.closing(2026)
    assert bal["13600"] == Decimal("40000.00") and bal["10150"] == Decimal("0.40")
    assert L.chart.get("10150").name == "Clearing account"
    assert L.trial_balance(2026)["difference"] == 0


def test_import_rejects_bad_plans_and_files(web, company):
    r = upload(web, company, "balances", "notes.csv", "hello,world\n1,2\n", fy="2026")
    assert r.status_code == 302 and r.headers["Location"].endswith("/import")
    assert any("Could not import" in m for m in flashes(web))
    r = upload(web, company, "chart", "chart.xlsx", b"PK..", fy="2026")
    assert any("use a .csv file" in m for m in flashes(web))
    r = upload(web, company, "balances", "tb.csv", TB_CSV, fy="2026")
    loc = r.headers["Location"]
    r = post(web, loc, {"action_0": "post", "account_0": "99999", "op": "apply", "fy": "2026"})
    assert r.status_code == 200 and "not in the chart" in text(r)


def test_import_journals_csv(web, company, lib):
    content = ("*Narration,*Date,Description,*AccountCode,*TaxRate,*Amount\n"
               "Depreciation,30/06/2026,P&E,78170,BAS Excluded,1200\nDepreciation,30/06/2026,,21810,BAS Excluded,-1200\n")
    r = upload(web, company, "journals", "mj.csv", content)
    page = text(web.get(r.headers["Location"]))
    assert "Depreciation" in page and "1,200.00" in page
    r = post(web, r.headers["Location"], {"op": "post"})
    assert r.status_code == 302
    assert Ledger(store_for(lib), company).closing(2026)["78170"] == Decimal("1200.00")


def test_import_chart_preview_then_apply(web, company, lib):
    content = ("*Code,Report Code,*Name,Reporting Name,*Type,*Tax Code\n"
               "72930,EXP.ADM,Accounting and audit fees,,Expense,GST on Expenses\nX100,REV.TRA.SER,Consulting income,,Revenue,GST on Income\n")
    r = upload(web, company, "chart", "xero-chart.csv", content)
    page = text(web.get(r.headers["Location"]))
    assert "1 new account(s)" in page and "Accounting and audit fees" in page
    r = post(web, r.headers["Location"], {"op": "apply"})
    assert r.status_code == 302
    L = Ledger(store_for(lib), company)
    assert L.chart.get("X100").report_code == "REV.TRA.SER"


def test_import_workpaper(web, company, lib, tmp_path):
    from test_imports import make_workpaper
    p = make_workpaper(tmp_path / "wp.xlsx")
    r = upload(web, company, "workpaper", "Workpaper FY2026.xlsx", p.read_bytes(), fy="2026")
    assert r.status_code == 302
    page = text(web.get(r.headers["Location"]))
    assert "Accounting &amp; Bookkeeping Fees" in page or "Accounting & Bookkeeping Fees" in page
    r = post(web, r.headers["Location"], {"op": "apply", "include_py": "yes", "fy": "2026"})
    assert r.status_code == 302, text(r)
    L = Ledger(store_for(lib), company)
    assert L.trial_balance(2026)["profit"] == Decimal("55000.00") and 2025 in L.years()


# ---- reports, year end ------------------------------------------------------------------- #
def test_reports_are_saved_in_the_library(web, app, company, lib):
    post(web, f"/c/{company}/journals/save",
         journal_form("30/06/2026", "Year", ("18500", "15000", ""), ("51100", "", "20000"), ("72930", "5000", ""),
                      ("43000", "", "0.00"), ("13600", "1000", ""), ("31300", "", "1000")))
    r = post(web, f"/c/{company}/reports/generate", {"report": "financial_reports", "fy": "2026",
                                                     "date_signed": "15 October 2026", "final": "yes"})
    assert r.status_code == 302
    folder = lib / "clients" / company / "years" / "2026" / "reports"
    pdfs = list(folder.glob("Financial Reports FY2026 *.pdf"))
    assert len(pdfs) == 1 and "DRAFT" not in pdfs[0].name
    page = text(web.get(r.headers["Location"]))
    assert pdfs[0].name in page and "Net profit FY2026" in page
    for kind, fmt_ in (("trial_balance", "xlsx"), ("general_ledger", "pdf"), ("journal_report", "xlsx")):
        assert post(web, f"/c/{company}/reports/generate", {"report": kind, "format": fmt_, "fy": "2026"}).status_code == 302
    names = sorted(p.name.split(" FY")[0] for p in folder.iterdir())
    assert names == ["Financial Reports", "General Ledger Detail", "Journal Report", "Trial Balance"]
    got = web.get(f"/c/{company}/reports/2026/file/{pdfs[0].name}")
    assert got.status_code == 200 and got.data[:4] == b"%PDF"
    assert web.get(f"/c/{company}/reports/2026/file/..%2F..%2Fclient.json").status_code == 404
    post(web, f"/c/{company}/reports/2026/open/{pdfs[0].name}")
    assert app.opened and app.opened[-1][0].name == pdfs[0].name


def test_year_end_roll_forward_and_lock(web, company, lib):
    L = Ledger(store_for(lib), company)
    div = next(a.code for a in L.chart.accounts if a.report_code.startswith("EQU.RET.DIV") and not a.system)
    post(web, f"/c/{company}/journals/save",
         journal_form("30/06/2026", "Year", ("18500", "8000", ""), ("51100", "", "10000"), (div, "2000", "")))
    page = text(web.get(f"/c/{company}/year-end?fy=2026"))
    assert "Roll forward to FY2027" in page and div in page
    r = post(web, f"/c/{company}/year-end/close", {"fy": "2026"})
    assert r.status_code == 302
    L = Ledger(store_for(lib), company)
    assert 2027 in L.years() and div not in L.closing(2027)
    post(web, f"/c/{company}/year-end/lock", {"fy": "2026", "reason": "signed"})
    r = post(web, f"/c/{company}/journals/save",
             journal_form("30/06/2026", "Late", ("72930", "1", ""), ("18500", "", "1")))
    assert "locked" in text(r)
    post(web, f"/c/{company}/year-end/unlock", {"fy": "2026", "reason": ""})
    assert any("reason" in m for m in flashes(web))
    assert Ledger(store_for(lib), company).is_locked(2026)
    post(web, f"/c/{company}/year-end/unlock", {"fy": "2026", "reason": "amendment"})
    assert not Ledger(store_for(lib), company).is_locked(2026)
    assert "J2027-0001" in text(web.get(f"/c/{company}/year-end?fy=2026"))


def test_read_only_while_someone_else_edits(web, company, lib):
    web.get("/")                                        # leaving the client hands the lock back
    other = FolderStore(lib, user="Someone Else", machine="pc2")
    assert other.acquire_lock(company)[0]
    page = text(web.get(f"/c/{company}/"))
    assert "Read only." in page and "Someone Else" in page
    r = post(web, f"/c/{company}/journals/save",
             journal_form("30/06/2026", "Sneaky", ("72930", "1", ""), ("18500", "", "1")))
    assert r.status_code == 302 and any("Someone Else is editing" in m for m in flashes(web))
    assert Ledger(store_for(lib), company).journals(2026, statuses=None) == []
    bank_page = text(web.get(f"/c/{company}/bank"))
    assert "Read only." in bank_page and "Upload and check" not in bank_page
    r = post(web, f"/c/{company}/lock/take")
    assert r.status_code == 302 and FolderStore(lib, user="Tester").lock_info(company)["mine"]
    hb = web.post(f"/c/{company}/lock/heartbeat", headers={"X-CSRF-Token": web.csrf})
    assert hb.get_json() == {"readonly": False, "user": "", "machine": ""}


def test_client_details_and_options(web, company, lib):
    r = post(web, f"/c/{company}/details", {"name": "Web Co Pty Ltd", "type": "company", "fy_end_month": "6",
                                            "fy_end_day": "30", "opt_decimals": "2", "opt_contents_page_numbers": "no",
                                            "options_json": '{"note_titles": {"receivables": "Trade Debtors"}}'})
    assert r.status_code == 302
    c = Ledger(store_for(lib), company).client
    assert c.options == {"decimals": 2, "contents_page_numbers": False, "note_titles": {"receivables": "Trade Debtors"}}
    r = post(web, f"/c/{company}/details", {"name": "Web Co Pty Ltd", "type": "company", "fy_end_month": "3",
                                            "fy_end_day": "31", "options_json": "{bad"})
    page = text(r)
    assert "cannot change once a financial year" in page and "Advanced report options" in page


def test_changing_the_entity_type_before_any_journal_swaps_the_chart(web, company, lib):
    r = post(web, f"/c/{company}/details", {"name": "Web Co", "type": "trust", "fy_end_month": "6", "fy_end_day": "30"})
    assert r.status_code == 302
    L = Ledger(store_for(lib), company)
    assert L.client.type == "trust" and any(a.report_code.startswith("EXP.DIS") for a in L.chart.accounts)
    post(web, f"/c/{company}/journals/save", journal_form("30/06/2026", "x", ("18500", "1", ""), ("58900", "", "1")))
    post(web, f"/c/{company}/details", {"name": "Web Co", "type": "company", "fy_end_month": "6", "fy_end_day": "30"})
    assert Ledger(store_for(lib), company).client.type == "trust"


def test_sync_conflict_copies_are_reported(web, company, lib):
    jd = lib / "clients" / company / "years" / "2026" / "journals"
    post(web, f"/c/{company}/journals/save", journal_form("30/06/2026", "x", ("72930", "1", ""), ("31300", "", "1")))
    (jd / "J2026-0001-PC2.json").write_text((jd / "J2026-0001.json").read_text(encoding="utf-8"), encoding="utf-8")
    (lib / "clients" / company / "desktop.ini").write_text("", encoding="utf-8")
    assert store_for(lib).conflicts(company) == ["years/2026/journals/J2026-0001-PC2.json"]
    assert len(Ledger(store_for(lib), company).journals(2026)) == 1          # the copy is not counted twice
    assert "OneDrive left 1 file(s)" in text(web.get(f"/c/{company}/"))


def test_archive_client(web, company, lib):
    post(web, f"/c/{company}/archive", {"confirm_name": "wrong"})
    assert (lib / "clients" / company).exists()
    r = post(web, f"/c/{company}/archive", {"confirm_name": "web co pty ltd"})
    assert r.status_code == 302 and not (lib / "clients" / company).exists()
    assert list((lib / "archived").iterdir())
    assert "No clients yet" in text(web.get("/"))


# ---- moving a client off Xero ------------------------------------------------------------ #
def xero_tb_xlsx(as_at: str, rows) -> bytes:
    wb = openpyxl.Workbook()
    ws = wb.active
    for r in [["Trial Balance"], ["Migrated Pty Ltd"], [f"As at {as_at}"], [],
              ["Account", "Debit", "Credit", "YTD Debit", "YTD Credit"]] + rows:
        ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_migrate_from_xero_exports(web, lib):
    chart = ("*Code,Report Code,*Name,Reporting Name,*Type,*Tax Code\n"
             "200,REV.TRA.GOO,Sales,,Revenue,GST on Income\n400,EXP.ADV,Advertising,,Expense,GST on Expenses\n"
             "090,ASS.CUR.CAS.BAN,Business Bank Account,,Bank,BAS Excluded\n960,EQU.RET,Retained Earnings,,Equity,BAS Excluded\n"
             "970,EQU.SHA,Owner A Share Capital,,Equity,BAS Excluded\n")
    tb25 = xero_tb_xlsx("30 June 2025", [["Sales (200)", "", "", "", 1000], ["Advertising (400)", "", "", 200, ""],
                                         ["Business Bank Account (090)", "", "", 900, ""], ["Owner A Share Capital (970)", "", "", "", 100]])
    tb26 = xero_tb_xlsx("30 June 2026", [["Sales (200)", "", "", "", 2000], ["Advertising (400)", "", "", 500, ""],
                                         ["Business Bank Account (090)", "", "", 2400, ""], ["Retained Earnings (960)", "", "", "", 800],
                                         ["Owner A Share Capital (970)", "", "", "", 100]])
    data = {"_csrf": web.csrf, "name": "Migrated Pty Ltd", "type": "company", "fy_end_month": "6", "fy_end_day": "30",
            "chart": (io.BytesIO(chart.encode()), "ChartOfAccounts.csv"),
            "tbs": [(io.BytesIO(tb26), "Trial_Balance (1).xlsx"), (io.BytesIO(tb25), "Trial_Balance.xlsx")]}
    r = web.post("/migrate", data=data, content_type="multipart/form-data")
    assert r.status_code == 302, text(r)
    slug = r.headers["Location"].split("/c/")[1].split("/")[0]
    page = text(web.get(r.headers["Location"]))
    assert "FY2025" in page and "FY2026" in page
    L = Ledger(store_for(lib), slug)
    assert L.years() == [2025, 2026]
    assert L.closing(2026)["090"] == Decimal("2400.00") and L.closing(2026)["960"] == Decimal("-800.00")
    assert L.trial_balance(2026)["profit"] == Decimal("1500.00")


def test_migrate_rejects_a_trial_balance_that_is_not_at_a_year_end(web, lib):
    chart = "*Code,Report Code,*Name,*Type\n090,ASS.CUR.CAS.BAN,Bank,Bank\n200,REV.TRA,Sales,Revenue\n"
    tb = xero_tb_xlsx("31 March 2026", [["Bank (090)", "", "", 10, ""], ["Sales (200)", "", "", "", 10]])
    data = {"_csrf": web.csrf, "name": "Half Year Pty Ltd", "type": "company", "fy_end_month": "6", "fy_end_day": "30",
            "chart": (io.BytesIO(chart.encode()), "chart.csv"), "tbs": [(io.BytesIO(tb), "tb.xlsx")]}
    r = web.post("/migrate", data=data, content_type="multipart/form-data")
    assert r.status_code == 400 and "not a year end" in text(r)
    assert not [c for c in store_for(lib).list_clients() if c.name == "Half Year Pty Ltd"]


def test_fortis_logo_in_the_header(web):
    page = web.get("/").get_data(as_text=True)
    assert 'src="/static/fortis-logo.svg"' in page and 'alt="Fortis Accounting Partners"' in page
    logo = web.get("/static/fortis-logo.svg")
    assert logo.status_code == 200 and logo.mimetype == "image/svg+xml"
    assert b"<script" not in logo.data


def test_every_screen_renders(web, company):
    post(web, f"/c/{company}/journals/save", journal_form("30/06/2026", "Fees", ("72930", "100", ""), ("31300", "", "100")))
    statement(web, company, "18500", STATEMENT)
    for path in ["/", "/setup", "/clients/new", "/migrate", f"/c/{company}/", f"/c/{company}/journals",
                 f"/c/{company}/bank", f"/c/{company}/bank/18500", f"/c/{company}/bank/18500?tab=done",
                 f"/c/{company}/bank/18500?tab=statements", f"/c/{company}/profit-and-loss", f"/c/{company}/balance-sheet",
                 f"/c/{company}/gst-report", f"/c/{company}/gst-report?basis=cash", f"/c/{company}/journals?kind=bank",
                 f"/c/{company}/journals/new", f"/c/{company}/journals/2026/J2026-0001", f"/c/{company}/accounts",
                 f"/c/{company}/accounts/72930", f"/c/{company}/import", f"/c/{company}/trial-balance",
                 f"/c/{company}/account-transactions", f"/c/{company}/account-transactions?code=72930",
                 f"/c/{company}/reports", f"/c/{company}/year-end", f"/c/{company}/changes", f"/c/{company}/details",
                 f"/c/{company}/trial-balance?fy=2027", f"/c/{company}/journals?fy=2027",
                 f"/c/{company}/profit-and-loss?fy=2027", f"/c/{company}/bank?fy=2027"]:
        r = web.get(path)
        assert r.status_code == 200, (path, r.status_code, text(r)[:500])
        assert "<script>" not in r.get_data(as_text=True)          # no inline script (the CSP forbids it)
    assert web.get("/c/no-such-client-000000/").status_code == 404
    assert web.get(f"/c/{company}/journals/2026/J2026-9999").status_code == 404
    assert web.get(f"/c/{company}/journals/2026/B2026-0009").status_code == 404
    assert web.get(f"/c/{company}/bank/NOPE9").status_code == 404
    assert web.get(f"/c/{company}/bank/..%2Fclient").status_code == 404


# ---- bank statements and cash coding ----------------------------------------------------------- #
STATEMENT = """Date,Amount,Description,Balance
15/06/2026,2000.00,DIRECT CREDIT ACME PTY LTD INV 1001,"2,000.00"
16/06/2026,-55.00,OFFICEWORKS 0123 SYDNEY AU Card xx1234,"1,945.00"
17/06/2026,-11.00,MONTHLY ACCOUNT FEE,"1,934.00"
"""


@pytest.fixture
def gst_company(web, lib):
    r = post(web, "/clients/new", {"name": "GST Co Pty Ltd", "type": "company", "fy_end_month": "6", "fy_end_day": "30",
                                   "first_fy": "2026", "gst_registered": "yes"})
    return r.headers["Location"].split("/c/")[1].split("/")[0]


def bank_upload(web, slug, account, name, content):
    data = {"account": account, "_csrf": web.csrf,
            "file": (io.BytesIO(content if isinstance(content, bytes) else content.encode()), name)}
    return web.post(f"/c/{slug}/bank/upload", data=data, content_type="multipart/form-data")


def statement(web, slug, account, content, name="statement.csv", **form):
    """Upload a statement and import it as the app read it (or with `form` changes); returns the column check page."""
    r = bank_upload(web, slug, account, name, content)
    assert r.status_code == 302 and "/bank/import/" in r.headers["Location"], flashes(web)
    url = r.headers["Location"]
    page = web.get(url).get_data(as_text=True)
    fields = {m.group(1): m.group(2) for m in re.finditer(r'<select name="(role_\d+)"[^>]*>.*?<option value="(\w*)" selected', page, re.S)}
    fields.update({"op": "import", "header_row": re.search(r'<option value="(\d+)" selected>(?:No titles|Row)', page).group(1),
                   "date_order": "dmy", **form})
    r = post(web, url, fields)
    return r, url


def line_ids(web, slug, account):
    return re.findall(r'name="pick" value="(l[0-9a-f]{10})"', web.get(f"/c/{slug}/bank/{account}").get_data(as_text=True))


def test_bank_statement_import_and_duplicates(web, gst_company, lib, tmp_path):
    slug = gst_company
    r, url = statement(web, slug, "18500", STATEMENT)
    assert r.status_code == 302 and r.headers["Location"].endswith("/bank/18500")
    assert any("3 line(s) imported from statement.csv" in m for m in flashes(web))
    # the same statement again: nothing new to import
    r = bank_upload(web, slug, "18500", "statement again.csv", STATEMENT)
    page = text(web.get(r.headers["Location"]))
    assert "Already imported" in page and "Import 0 lines" in page
    r = post(web, r.headers["Location"], {"op": "import", "header_row": "1", "role_0": "date", "role_1": "amount",
                                          "role_2": "description"})
    assert r.status_code == 200 and "nothing new to import" in text(r)
    feed = store_for(lib).bank_path(slug, "18500")
    assert len(json.loads(feed.read_text())["lines"]) == 3
    # a credit card's Excel file shows money spent as positive amounts: turn the signs around
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["Transaction Date", "Details", "Amount"])
    ws.append([dt.datetime(2026, 6, 20), "QANTAS AIRWAYS", 450.0])
    ws.append([dt.datetime(2026, 6, 25), "PAYMENT THANK YOU", -450.0])
    buf = io.BytesIO()
    wb.save(buf)
    r = post(web, f"/c/{slug}/accounts/quick-add", {"type": "Current Liability", "code": "32100", "name": "Visa card"})
    r, _ = statement(web, slug, "32100", buf.getvalue(), name="visa.xlsx", flip="yes")
    assert r.status_code == 302
    card = json.loads(store_for(lib).bank_path(slug, "32100").read_text())
    assert [ln["amount"] for ln in card["lines"]] == ["-450.00", "450.00"] and card["layout"]["flip"] is True
    # two roles on one column, a P&L account, a file that is not a statement
    r = bank_upload(web, slug, "18500", "s.csv", STATEMENT)
    r = post(web, r.headers["Location"], {"op": "import", "header_row": "1", "role_0": "date", "role_1": "amount",
                                          "role_2": "amount"})
    assert "chosen for two columns" in text(r)
    bank_upload(web, slug, "51100", "s.csv", STATEMENT)
    assert any("profit and loss account" in m for m in flashes(web))
    bank_upload(web, slug, "18500", "statement.pdf", b"%PDF-1.4")
    assert any("use the CSV" in m for m in flashes(web))
    bank_upload(web, slug, "18500", "empty.csv", "")
    assert any("Could not read empty.csv" in m for m in flashes(web))
    assert not list((tmp_path / "work").iterdir()) or all(p.name == "s.csv" or p.is_dir() for p in (tmp_path / "work").iterdir())


def test_cash_coding_reconcile_and_unreconcile(web, gst_company, lib):
    slug = gst_company
    statement(web, slug, "18500", STATEMENT)
    ids = line_ids(web, slug, "18500")
    assert len(ids) == 3
    overview = text(web.get(f"/c/{slug}/"))
    assert "Reconcile 3 items" in overview
    # one good line, one to an account that does not exist, one ticked without an account
    r = post(web, f"/c/{slug}/bank/18500/reconcile", {"pick": ids, f"acct_{ids[0]}": "51100 Sale of retail",
                                                      f"acct_{ids[1]}": "99999", f"tax_{ids[1]}": "GST on Expenses"})
    assert r.status_code == 302 and "r=" in r.headers["Location"]
    msgs = flashes(web)
    assert any("1 line(s) reconciled" in m for m in msgs) and any("had no account" in m for m in msgs)
    page = text(web.get(r.headers["Location"]))
    assert "account 99999 is not in the chart" in page and 'value="99999"' in page and "row-error" in page
    r = post(web, f"/c/{slug}/bank/18500/reconcile", {"pick": [ids[1], ids[2]], f"acct_{ids[1]}": "72100",
                                                      f"acct_{ids[2]}": "72500", f"tax_{ids[2]}": "BAS Excluded"})
    L = Ledger(store_for(lib), slug)
    bal = L.closing(2026)
    assert bal["18500"] == Decimal("1934.00") and bal["51100"] == Decimal("-1818.18") and bal["72500"] == Decimal("11.00")
    assert "Everything is reconciled" in text(web.get(f"/c/{slug}/bank/18500"))
    done = text(web.get(f"/c/{slug}/bank/18500?tab=done"))
    assert "B2026-0003" in done and "GST on Expenses" in done
    # the bank transaction in the journals: listed under bank transactions, no edit, void or reverse
    assert "B2026-0001" not in text(web.get(f"/c/{slug}/journals?fy=2026"))
    assert "B2026-0001" in text(web.get(f"/c/{slug}/journals?fy=2026&kind=bank"))
    view = text(web.get(f"/c/{slug}/journals/2026/B2026-0002"))
    assert "Bank transaction" in view and "Bank statement line" in view and "Void" not in view and "Copy" not in view
    assert post(web, f"/c/{slug}/journals/2026/B2026-0002/void", {"reason": "x"}).status_code == 302
    assert any("unreconcile" in m for m in flashes(web))
    # unreconcile: out of the ledger, back to be coded, and its number is not used again
    r = post(web, f"/c/{slug}/bank/18500/unreconcile", {"line": ids[1]})
    assert any("B2026-0002 taken out of the ledger" in m for m in flashes(web))
    assert "72100" not in Ledger(store_for(lib), slug).closing(2026)
    post(web, f"/c/{slug}/bank/18500/reconcile", {"pick": [ids[1]], f"acct_{ids[1]}": "72100"})
    assert "B2026-0004" in text(web.get(f"/c/{slug}/bank/18500?tab=done"))
    assert "Suggested" not in text(web.get(f"/c/{slug}/bank/18500"))
    # the next statement: the same payee is suggested from how it was coded
    nxt = "Date,Amount,Description\n20/06/2026,-77.00,OFFICEWORKS 0456 PARRAMATTA AU Card xx1234\n"
    statement(web, slug, "18500", nxt, name="next.csv")
    coding = text(web.get(f"/c/{slug}/bank/18500"))
    assert "Suggested" in coding and 'value="72100"' in coding
    log = [a["action"] for a in store_for(lib).read_audit(slug)]
    assert {"bank.imported", "bank.reconciled", "bank.unreconciled"} <= set(log)


def test_delete_and_restore_lines(web, gst_company, lib):
    slug = gst_company
    statement(web, slug, "18500", STATEMENT)
    ids = line_ids(web, slug, "18500")
    post(web, f"/c/{slug}/bank/18500/delete", {"pick": [ids[2]], "reason": "fee refunded"})
    assert any("1 line(s) deleted" in m for m in flashes(web))
    assert len(line_ids(web, slug, "18500")) == 2
    page = text(web.get(f"/c/{slug}/bank/18500?tab=statements"))
    assert "MONTHLY ACCOUNT FEE" in page and "fee refunded" in page and "statement.csv" in page
    post(web, f"/c/{slug}/bank/18500/restore", {"pick": [ids[2]]})
    assert len(line_ids(web, slug, "18500")) == 3
    post(web, f"/c/{slug}/bank/18500/reconcile", {})
    assert any("Tick the lines" in m for m in flashes(web))


def test_new_account_pop_up(web, gst_company, lib):
    slug = gst_company
    fetch = {"X-Requested-With": "fetch"}
    r = post(web, f"/c/{slug}/accounts/quick-add", {"type": "Expense", "code": "61150", "name": "Staff amenities"}, headers=fetch)
    assert r.get_json() == {"ok": True, "account": {"code": "61150", "name": "Staff amenities", "type": "Expense",
                                                    "report_code": "EXP", "tax": "GST on Expenses"}}
    r = post(web, f"/c/{slug}/accounts/quick-add", {"type": "Fixed Asset", "code": "15100", "name": "Computers",
                                                    "report_code": "ASS.NCA.FIX.PLA", "tax": "GST on Capital"}, headers=fetch)
    assert r.get_json()["account"]["report_code"] == "ASS.NCA.FIX.PLA"
    r = post(web, f"/c/{slug}/accounts/quick-add", {"type": "Expense", "code": "61150", "name": "Again"}, headers=fetch)
    assert r.status_code == 400 and "already exists" in r.get_json()["error"]
    r = post(web, f"/c/{slug}/accounts/quick-add", {"type": "Magic", "code": "61160", "name": "x"}, headers=fetch)
    assert r.status_code == 400 and "account type" in r.get_json()["error"]
    r = post(web, f"/c/{slug}/accounts/quick-add", {"type": "Revenue", "code": "51950", "name": "Consulting",
                                                    "next": f"/c/{slug}/journals/new"})
    assert r.status_code == 302 and r.headers["Location"].endswith("/journals/new")
    assert any("Account 51950 Consulting added" in m for m in flashes(web))
    r = post(web, f"/c/{slug}/accounts/quick-add", {"type": "Revenue", "code": "51960", "name": "x", "next": "//evil.example"})
    assert r.headers["Location"] == "/"
    L = Ledger(store_for(lib), slug)
    assert L.chart.get("51950").tax_code == "GST on Income" and L.chart.get("15100").type == "Fixed Asset"
    added = [a for a in store_for(lib).read_audit(slug) if a["action"] == "account.added"]
    assert len(added) == 4
    # the pop-up is on the journal editor and the cash coding screen
    assert 'id="new-account"' in web.get(f"/c/{slug}/journals/new").get_data(as_text=True)
    statement(web, slug, "18500", STATEMENT)
    assert 'id="new-account"' in web.get(f"/c/{slug}/bank/18500").get_data(as_text=True)


def test_journal_with_gst(web, gst_company, lib):
    slug = gst_company
    form = journal_form("30/06/2026", "Accrued fee", ("72930", "110", ""), ("31300", "", "110"))
    form["tax_0"] = "GST on Expenses"
    r = post(web, f"/c/{slug}/journals/save", form)
    assert r.status_code == 302, text(r)
    L = Ledger(store_for(lib), slug)
    gst = bank.gst_account(L)
    assert L.closing(2026) == {"72930": Decimal("100.00"), gst: Decimal("10.00"), "31300": Decimal("-110.00")}
    edit = web.get(f"/c/{slug}/journals/2026/J2026-0001?edit=1").get_data(as_text=True)
    assert 'value="110.00"' in edit and "<option selected>GST on Expenses</option>" in edit
    assert len(re.findall(r'name="account_\d+"', edit)) == 4     # 2 lines and 2 blank ones: the GST line is not shown
    view = text(web.get(f"/c/{slug}/journals/2026/J2026-0001"))
    assert "GST on Expenses" in view and "(GST 10.00)" in view
    # GST exclusive: the GST is added on top
    form = journal_form("30/06/2026", "Laptop", ("72100", "1,000", ""), ("31300", "", "1,100"))
    form.update({"tax_0": "GST on Expenses", "amounts": "exclusive"})
    assert post(web, f"/c/{slug}/journals/save", form).status_code == 302
    L = Ledger(store_for(lib), slug)
    assert L.closing(2026)["72100"] == Decimal("1000.00") and L.closing(2026)[gst] == Decimal("110.00")
    # editing it again keeps the amounts as typed
    edit = web.get(f"/c/{slug}/journals/2026/J2026-0002?edit=1").get_data(as_text=True)
    assert 'value="1,000.00"' in edit and '<option value="exclusive" selected>' in edit
    # the GST report on the accrual basis picks the journals up; the cash basis does not
    page = text(web.get(f"/c/{slug}/gst-report?fy=2026&basis=accrual"))
    assert "110.00" in page and "J2026-0002" in page
    assert "left out" in text(web.get(f"/c/{slug}/gst-report?fy=2026&basis=cash"))


def test_gst_registration_on_client_details(web, company, lib):
    assert "Tax rate" not in text(web.get(f"/c/{company}/journals/new"))
    r = post(web, f"/c/{company}/details", {"name": "Web Co Pty Ltd", "type": "company", "fy_end_month": "6",
                                            "fy_end_day": "30", "gst_registered": "yes"})
    assert r.status_code == 302 and Ledger(store_for(lib), company).client.gst_registered
    assert "Tax rate" in text(web.get(f"/c/{company}/journals/new"))
    post(web, f"/c/{company}/details", {"name": "Web Co Pty Ltd", "type": "company", "fy_end_month": "6", "fy_end_day": "30"})
    assert not Ledger(store_for(lib), company).client.gst_registered


def test_profit_and_loss_balance_sheet_and_gst_pages(web, company, lib):
    post(web, f"/c/{company}/journals/save",
         journal_form("30/06/2026", "Year", ("18500", "15000", ""), ("51100", "", "20000"), ("72930", "5000", "")))
    pl = text(web.get(f"/c/{company}/profit-and-loss?fy=2026"))
    assert "Trading Income" in pl and "Net Profit" in pl and "15,000.00" in pl
    pl = text(web.get(f"/c/{company}/profit-and-loss?fy=2026&start=01/07/2025&end=31/12/2025&compare=no"))
    assert "For the period 1 July 2025 to 31 December 2025" in pl and "No income or expenses in this period" in pl
    r = web.get(f"/c/{company}/profit-and-loss?fy=2026&start=01/07/2026&end=30/09/2026")
    assert r.status_code == 200 and "not been started" in text(r) and "For the year ended 30 June 2026" in text(r)
    bs = text(web.get(f"/c/{company}/balance-sheet?fy=2026&as_at=30/06/2026"))
    assert "Total Assets" in bs and "Current Year Earnings" in bs and "differ" not in bs
    gst = text(web.get(f"/c/{company}/gst-report?fy=2026"))
    assert "not marked as registered for GST" in gst and "G1" in gst
    folder = lib / "clients" / company / "years" / "2026" / "reports"
    for kind, extra in (("profit_and_loss", {"start": "01/07/2025", "end": "30/06/2026", "compare": "no"}),
                        ("balance_sheet", {"as_at": "30/06/2026"}), ("gst_report", {"basis": "cash"})):
        for fmt_ in ("pdf", "xlsx"):
            r = post(web, f"/c/{company}/reports/generate", {"report": kind, "format": fmt_, "fy": "2026", **extra})
            assert r.status_code == 302
    names = sorted(p.name.split(" FY")[0] for p in folder.iterdir())
    assert names == ["Balance Sheet"] * 2 + ["GST Report (cash basis)"] * 2 + ["Profit and Loss"] * 2
    r = post(web, f"/c/{company}/reports/generate", {"report": "balance_sheet", "format": "pdf", "fy": "2026",
                                                     "as_at": "01/07/2027"})
    assert r.status_code == 302 and any("not been started" in m for m in flashes(web))

