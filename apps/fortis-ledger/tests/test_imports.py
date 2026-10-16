import csv
import json
from decimal import Decimal

import openpyxl
import pytest

from fortis_ledger import imports, xero
from fortis_ledger.engine import REPORT_MAP, fr_engine
from fortis_ledger.ledger import Ledger, ValidationError


def write_csv(path, rows):
    with open(path, "w", newline="", encoding="utf-8") as fh:
        csv.writer(fh).writerows(rows)
    return path


@pytest.fixture
def blank_company(store):
    c = store.create_client("Import Co Pty Ltd", "company", directors=["Jane Citizen"])
    return Ledger(store, c.slug)


def test_debit_credit_tb_with_firm_codes(tmp_path, blank_company):
    L = blank_company
    f = write_csv(tmp_path / "tb.csv", [
        ["Code", "Name", "Debit", "Credit"],
        ["18500", "Cheque account", "15,000.00", ""],
        ["51100", "Sales", "", "50,000.00"],
        ["72930", "Accounting fees", "5,000.00", ""],
        ["13600", "Trade debtors", "40,000.00", ""],
        ["43000", "Retained earnings", "", "10,000.00"],
        ["", "Total", "60,000.00", "60,000.00"],
    ])
    plan = imports.plan_from_file(L, f, 2025)
    assert [pl.action for pl in plan.lines] == ["post"] * 5 and not plan.warnings
    res = imports.apply_plan(L, plan)
    assert len(res["journals"]) == 1
    tb = Ledger(L.store, L.slug).trial_balance(2025)
    assert tb["difference"] == 0 and tb["profit"] == Decimal("45000.00")


XERO_TB = [
    ["Trial Balance"], ["Demo Company (AU)"], ["As at 30 June 2026"], [],
    ["Account", "Debit", "Credit", "YTD Debit", "YTD Credit"],
    ["Revenue"],
    ["Sales (200)", "", "", "", "120000.00"],
    ["Interest Income (270)", "", "", "", "500.00"],
    ["Expenses"],
    ["Wages and Salaries (477)", "", "", "60000.00", ""],
    ["Rent (469)", "", "", "20000.00", ""],
    ["Assets"],
    ["Business Bank Account (090)", "", "", "35500.00", ""],
    ["Accounts Receivable (610)", "", "", "15000.00", ""],
    ["Liabilities"],
    ["GST (820)", "", "", "", "4000.00"],
    ["Equity"],
    ["Retained Earnings (960)", "", "", "", "6000.00"],
    ["Total", "", "", "130500.00", "130500.00"],
]


def test_xero_tb_export_layout(tmp_path, blank_company):
    L = blank_company
    f = write_csv(tmp_path / "xero-tb.csv", XERO_TB)
    plan = imports.plan_from_file(L, f, 2026)
    by = {pl.source_code: pl for pl in plan.lines}
    assert set(by) == {"200", "270", "477", "469", "090", "610", "820", "960"}
    assert by["200"].action == "new" and by["200"].report_code.startswith("REV")
    assert by["270"].report_code.startswith("REV.INV.INT")
    assert by["477"].report_code.startswith("EXP")
    assert by["090"].report_code == "ASS.CUR.CAS.BAN"
    assert by["820"].report_code.startswith("LIA.CUR.TAX")
    assert by["960"].report_code == "EQU.RET"
    assert by["610"].action == "new" and by["610"].account != "610"   # firm chart's 610 is a system account
    imports.apply_plan(L, plan)
    L2 = Ledger(L.store, L.slug)
    assert L2.trial_balance(2026)["profit"] == Decimal("40500.00")
    pk = fr_engine.Pack(L2.pack(2026), REPORT_MAP)
    assert not pk.has_errors(), pk.validation_markdown()


def test_single_column_natural_sign_with_report_codes(tmp_path, blank_company):
    L = blank_company
    f = write_csv(tmp_path / "classified.csv", [
        ["Account Code", "Account Name", "Report Code", "Balance"],
        ["A1", "Bank", "ASS.CUR.CAS.BAN", "1000"],
        ["I1", "Consulting income", "REV.TRA.SER", "3000"],
        ["E1", "Software", "EXP.ADM", "500"],
        ["L1", "Loan from director", "LIA.NCL.LOA.DIR", "900"],
        ["Q1", "Opening retained earnings", "EQU.RET", "-2400"],
    ])
    plan = imports.plan_from_file(L, f, 2026)
    assert plan.info["sign"] == "natural"
    assert all(pl.action == "new" and pl.account == pl.source_code for pl in plan.lines)
    imports.apply_plan(L, plan)
    L2 = Ledger(L.store, L.slug)
    bal = L2.closing(2026)
    assert bal["I1"] == Decimal("-3000.00") and bal["Q1"] == Decimal("2400.00") and bal["A1"] == Decimal("1000.00")
    assert L2.chart.get("L1").report_code == "LIA.NCL.LOA.DIR"


def test_cy_py_columns_and_second_year_posts_differences(tmp_path, blank_company):
    L = blank_company
    f = write_csv(tmp_path / "tb2.csv", [
        ["Code", "Name", "2026", "2025"],
        ["18500", "Cheque account", "20000", "15000"],
        ["51100", "Sales", "-70000", "-50000"],
        ["72930", "Accounting fees", "6000", "5000"],
        ["13600", "Trade debtors", "99000", "40000"],
        ["43000", "Retained earnings", "-55000", "-10000"],
    ])
    plan = imports.plan_from_file(L, f, 2026)
    assert plan.include_py and plan.info["sign"] == "debit"
    res = imports.apply_plan(L, plan)
    assert len(res["journals"]) == 2
    L2 = Ledger(L.store, L.slug)
    assert L2.closing(2025)["13600"] == Decimal("40000.00")
    assert L2.closing(2026)["13600"] == Decimal("99000.00")
    # FY2026 journal holds only the movements: debtors +59,000, RE nil (carried 10,000 + FY2025 profit 45,000)
    j = [x for x in L2.journals(2026) if x.kind == "import"][0]
    lines = {ln.account: ln.amount for ln in j.lines}
    assert lines["13600"] == Decimal("59000.00") and "43000" not in lines
    # re-importing replaces the previous import rather than doubling it
    imports.apply_plan(L2, imports.plan_from_file(L2, f, 2026))
    assert Ledger(L.store, L.slug).closing(2026)["13600"] == Decimal("99000.00")
    assert len([x for x in Ledger(L.store, L.slug).journals(2026) if x.kind == "import" and x.status == "posted"]) == 1


def test_rounding_and_out_of_balance(tmp_path, blank_company):
    L = blank_company
    f = write_csv(tmp_path / "r.csv", [["Code", "Name", "Debit", "Credit"],
                                       ["18500", "Cheque", "100.03", ""], ["51100", "Sales", "", "100.00"]])
    plan = imports.plan_from_file(L, f, 2026)
    assert "rounding" in plan.warnings[0]
    imports.apply_plan(L, plan)
    j = Ledger(L.store, L.slug).journals(2026)[0]
    assert any("rounding" in ln.description for ln in j.lines)
    g = write_csv(tmp_path / "bad.csv", [["Code", "Name", "Debit", "Credit"],
                                         ["18500", "Cheque", "500", ""], ["51100", "Sales", "", "100"]])
    plan = imports.plan_from_file(L, g, 2027)
    with pytest.raises(ValidationError):
        imports.apply_plan(L, plan)
    imports.apply_plan(L, plan, allow_suspense=True)
    assert any(a.name == "Suspense" for a in Ledger(L.store, L.slug).chart.accounts)


def test_journal_csv_xero_and_generic(tmp_path, blank_company):
    L = blank_company
    xf = write_csv(tmp_path / "mj.csv", [
        ["*Narration", "*Date", "Description", "*AccountCode", "*TaxRate", "*Amount", "TrackingName1"],
        ["Depreciation", "30/06/2026", "P&E", "78170", "BAS Excluded", "1200", ""],
        ["Depreciation", "30/06/2026", "", "21810", "BAS Excluded", "-1200", ""],
        ["Accrual", "30/06/2026", "", "72930", "BAS Excluded", "3000", ""],
        ["Accrual", "30/06/2026", "", "36300", "BAS Excluded", "-3000", ""],
    ])
    js, errs = imports.parse_journal_rows(imports.read_table(xf))
    assert not errs and [len(j.lines) for j in js] == [2, 2]
    nums = imports.import_journals(L, js, "mj.csv")
    assert len(nums) == 2 and Ledger(L.store, L.slug).closing(2026)["78170"] == Decimal("1200.00")
    gf = write_csv(tmp_path / "g.csv", [
        ["Journal", "Date", "Narration", "Account", "Description", "Debit", "Credit"],
        ["A", "2026-06-30", "Bad", "72930", "", "10", ""],
        ["A", "2026-06-30", "Bad", "36300", "", "", "9"],
    ])
    js, _ = imports.parse_journal_rows(imports.read_table(gf))
    with pytest.raises(ValidationError):
        imports.import_journals(L, js, "g.csv")


def test_chart_merge(tmp_path, blank_company):
    L = blank_company
    f = write_csv(tmp_path / "chart.csv", [
        ["*Code", "Report Code", "*Name", "Reporting Name", "*Type", "*Tax Code"],
        ["72930", "EXP.ADM", "Accounting and audit fees", "", "Expense", "GST on Expenses"],
        ["X100", "REV.TRA.SER", "Consulting income", "", "Revenue", "GST on Income"],
    ])
    res = L.chart.merge(f)
    assert res["added"] == ["X100"] and res["changed"][0]["code"] == "72930"
    assert L.chart.get("72930").name == "Accounting and audit fees"


def make_workpaper(path):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Profit & Loss"
    for r in [["Test Pty Ltd - FY 2026"], [], ["Account Code - Name", "CY", "PY"],
              ["Trading Income"], ["200 - Sales", 100000, 80000], ["Total Trading Income", 100000, 80000],
              ["Operating Expenses"], ["412 - Accounting & Bookkeeping Fees", 5000, 4000],
              ["477 - Wages & Salaries", 40000, 30000], ["Total Operating Expenses", 45000, 34000],
              ["Net Profit", 55000, 46000]]:
        ws.append(r)
    bs = wb.create_sheet("Balance Sheet")
    for r in [["Test Pty Ltd - FY 2026"], ["Particulars", "CY", "PY"], ["Assets"], ["Bank"],
              ["090 - Business Account", 70000, 30000], ["Current Assets"], ["610 - Accounts Receivable", 51000, 20000],
              ["Liabilities"], ["Current Liabilities"], ["820 - GST", 5000, 3000],
              ["Equity"], ["960 - Retained Earnings", 61000, 1000], ["Current Year Earnings", 55000, 46000],
              ["Total Equity", 116000, 47000]]:
        bs.append(r)
    wb.save(path)
    return path


def test_workpaper_import_keeps_workpaper_lines(tmp_path, blank_company):
    L = blank_company
    plan = imports.plan_workpaper(L, make_workpaper(tmp_path / "wp.xlsx"), 2026)
    names = {pl.source_name: pl for pl in plan.lines}
    assert names["Current Year Earnings"].action == "skip"
    assert names["Accounting & Bookkeeping Fees"].action == "new" and names["Accounting & Bookkeeping Fees"].account == "412"
    assert names["Retained Earnings"].report_code == "EQU.RET"
    assert plan.include_py
    imports.apply_plan(L, plan)
    L2 = Ledger(L.store, L.slug)
    assert L2.trial_balance(2026)["profit"] == Decimal("55000.00")
    assert L2.trial_balance(2025)["profit"] == Decimal("46000.00")
    pk = fr_engine.Pack(L2.pack(2026), REPORT_MAP)
    assert not pk.has_errors(), pk.validation_markdown()
    lines = [l["label"] for s in pk.pnl["sections"] if not s.get("computed") for l in s["lines"]]
    assert "Accounting & Bookkeeping Fees" in lines and "Wages & Salaries" in lines


def test_xero_api_migration(tmp_path, store):
    accounts = {"Accounts": [
        {"AccountID": "a1", "Code": "200", "Name": "Sales", "Type": "REVENUE", "ReportingCode": "REV.TRA.GOO"},
        {"AccountID": "a2", "Code": "400", "Name": "Advertising", "Type": "EXPENSE", "ReportingCode": "EXP.ADV"},
        {"AccountID": "a3", "Code": "090", "Name": "Bank", "Type": "BANK", "ReportingCode": "ASS.CUR.CAS.BAN"},
        {"AccountID": "a4", "Code": "960", "Name": "Retained Earnings", "Type": "EQUITY", "ReportingCode": "EQU.RET"},
        {"AccountID": "a5", "Code": "970", "Name": "Owner A Share Capital", "Type": "EQUITY", "ReportingCode": "EQU.SHA"},
        {"AccountID": "a6", "Name": "Savings Account", "Type": "BANK", "ReportingCode": "ASS.CUR.CAS.BAN"},   # no code
    ]}

    def tb(rows):
        return {"Reports": [{"ReportID": "TrialBalance", "Rows": [
            {"RowType": "Header", "Cells": [{"Value": "Account"}, {"Value": "Debit"}, {"Value": "Credit"},
                                            {"Value": "YTD Debit"}, {"Value": "YTD Credit"}]},
            {"RowType": "Section", "Title": "Accounts", "Rows": [
                {"RowType": "Row", "Cells": [{"Value": f"{n} ({c})", "Attributes": [{"Id": "account", "Value": i}]},
                                             {"Value": ""}, {"Value": ""}, {"Value": dr}, {"Value": cr}]}
                for i, c, n, dr, cr in rows]}]}]}

    org = {"Organisations": [{"Name": "Migrated Pty Ltd", "LegalName": "Migrated Pty Ltd", "OrganisationEntityType": "COMPANY",
                              "RegistrationNumber": "64640451663", "FinancialYearEndMonth": 6, "FinancialYearEndDay": 30,
                              "OrganisationID": "org1", "Class": "LEDGER"}]}
    tbs = {2025: tb([("a1", "200", "Sales", "", "1000.00"), ("a2", "400", "Advertising", "200.00", ""),
                     ("a3", "090", "Bank", "850.00", ""), ("a6", "", "Savings Account", "50.00", ""),
                     ("a5", "970", "Owner A Share Capital", "", "100.00")]),
           2026: tb([("a1", "200", "Sales", "", "2000.00"), ("a2", "400", "Advertising", "500.00", ""),
                     ("a3", "090", "Bank", "2300.00", ""), ("a6", "", "Savings Account", "100.00", ""),
                     ("a4", "960", "Retained Earnings", "", "800.00"),
                     ("a5", "970", "Owner A Share Capital", "", "100.00")])}
    client, results = xero.migrate_from_api(store, org, accounts, tbs, tmp_path)
    assert client.abn == "64 640 451 663" and client.type == "company"
    L = Ledger(store, client.slug)
    # the code-less bank account gets one code, used in both years (no duplicate account in year two)
    assert {a.code for a in L.chart.accounts} == {"200", "400", "090", "960", "970", "BANK1"}
    assert all(not r["result"]["created_accounts"] for r in results)
    assert L.client.retained_earnings_account == "960"
    assert L.closing(2026)["090"] == Decimal("2300.00") and L.closing(2026)["BANK1"] == Decimal("100.00")
    assert L.closing(2026)["960"] == Decimal("-800.00")
    pk = fr_engine.Pack(L.pack(2026), REPORT_MAP)
    assert not pk.has_errors(), pk.validation_markdown()
    assert json.dumps(results[0]["result"])
