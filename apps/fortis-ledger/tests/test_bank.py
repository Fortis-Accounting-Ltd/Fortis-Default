"""Bank statements and cash coding: reading bank files, duplicates, coding with GST, transfers, suggestions."""
import datetime as dt
from decimal import Decimal

import pytest
from openpyxl import Workbook

from fortis_ledger import bank
from fortis_ledger.bank import BankError, BankFeed, Coding, detect_layout, parse_bank_date, parse_money, read_lines
from fortis_ledger.imports import read_table
from fortis_ledger.ledger import Ledger, LedgerError

# CommBank: no column titles, newest first, signed amounts, running balance
COMMBANK = """30/06/2026,"-11.00","MONTHLY ACCOUNT FEE","+3,134.00"
16/06/2026,"-55.00","OFFICEWORKS 0123 SYDNEY AU Card xx1234 Value Date: 14/06/2026","+3,145.00"
10/06/2026,"+2,000.00","Direct Credit 123456 ACME PTY LTD INV 1001","+3,200.00"
02/06/2026,"+1,200.00","Transfer from xx9999 CommBank app","+1,200.00"
"""

# Westpac: titles, separate debit and credit columns, a serial number
WESTPAC = """Bank Account,Date,Narrative,Debit Amount,Credit Amount,Balance,Categories,Serial
032000123456,01/07/2025,DEPOSIT ONLINE 1234 SALES,,1100.00,1100.00,DEP,
032000123456,03/07/2025,EFTPOS PURCHASE OFFICEWORKS,55.00,,1045.00,POS,
032000123456,04/07/2025,CHEQUE 000123,200.00,,845.00,CHQ,000123
"""

# NAB: titles with a blank one, '05 Jul 25' dates, merchant name as the payee
NAB = """Date,Amount,Account Number,,Transaction Type,Transaction Details,Balance,Category,Merchant Name
05 Jul 25,-12.50,083-123 12345678,,EFTPOS DEBIT,COFFEE CLUB SYDNEY,987.50,Dining,Coffee Club
06 Jul 25,250.00,083-123 12345678,,DEPOSIT,PAYMENT FROM J SMITH,1237.50,Income,
"""


def rows_of(tmp_path, text, name="statement.csv"):
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return read_table(p)


def lines_of(tmp_path, text, name="statement.csv"):
    rows = rows_of(tmp_path, text, name)
    layout = detect_layout(rows)
    lines, problems = read_lines(rows, layout)
    return lines, layout, problems


@pytest.fixture
def gst_client(store):
    c = store.create_client("Bank Co Pty Ltd", "company", gst_registered=True)
    L = Ledger(store, c.slug)
    L.ensure_year(2026)
    return Ledger(store, c.slug)


def load(L, code, text, tmp_path, name="statement.csv"):
    lines, layout, problems = lines_of(tmp_path, text, name)
    assert not problems, problems
    return bank.import_lines(L, code, lines, name, layout)


# ---- reading bank files -------------------------------------------------------------------- #
def test_money_and_dates():
    assert parse_money("55.00 DR") == Decimal("-55.00")
    assert parse_money("12.30CR") == Decimal("12.30")
    assert parse_money("+1,234.56") == Decimal("1234.56")
    assert parse_money("(55.00)") == Decimal("-55.00")
    assert parse_money("$1,000") == Decimal("1000.00")
    assert parse_money("") is None and parse_money("abc") is None and parse_money(None) is None
    assert parse_bank_date("05 Jul 25") == dt.date(2025, 7, 5)
    assert parse_bank_date("2026-06-30T00:00:00") == dt.date(2026, 6, 30)
    assert parse_bank_date("30/06/2026 10:15") == dt.date(2026, 6, 30)
    assert parse_bank_date("20260630") == dt.date(2026, 6, 30)
    assert parse_bank_date("06/30/2026", "mdy") == dt.date(2026, 6, 30)
    assert parse_bank_date("Closing balance") is None


def test_commbank_file_without_titles(tmp_path):
    lines, layout, problems = lines_of(tmp_path, COMMBANK)
    assert not problems
    assert layout.header_row == -1 and layout.cols == {"date": 0, "amount": 1, "balance": 3, "description": 2}
    assert [ln["date"] for ln in lines] == [dt.date(2026, 6, 2), dt.date(2026, 6, 10), dt.date(2026, 6, 16),
                                            dt.date(2026, 6, 30)]          # stored oldest first
    assert [ln["amount"] for ln in lines] == [Decimal("1200.00"), Decimal("2000.00"), Decimal("-55.00"), Decimal("-11.00")]
    assert lines[-1]["balance"] == Decimal("3134.00")


def test_westpac_debit_and_credit_columns(tmp_path):
    lines, layout, problems = lines_of(tmp_path, WESTPAC)
    assert not problems and layout.header_row == 0
    assert layout.cols["spent"] == 3 and layout.cols["received"] == 4 and layout.cols["reference"] == 7
    assert [ln["amount"] for ln in lines] == [Decimal("1100.00"), Decimal("-55.00"), Decimal("-200.00")]
    assert lines[2]["reference"] == "000123" and lines[1]["description"] == "EFTPOS PURCHASE OFFICEWORKS"


def test_nab_file_with_merchant_names(tmp_path):
    lines, layout, problems = lines_of(tmp_path, NAB)
    assert not problems
    assert lines[0]["date"] == dt.date(2025, 7, 5) and lines[0]["amount"] == Decimal("-12.50")
    assert lines[0]["payee"] == "Coffee Club" and lines[0]["description"] == "COFFEE CLUB SYDNEY"
    assert lines[1]["balance"] == Decimal("1237.50")


def test_excel_statement_and_flipped_signs(tmp_path):
    wb = Workbook()
    ws = wb.active
    ws.append(["Statement for account 1234"])                       # a title line before the column titles
    ws.append(["Transaction Date", "Description", "Amount"])
    ws.append([dt.datetime(2026, 3, 1), "UBER TRIP", 23.5])         # this card file shows spending as positive
    ws.append([dt.datetime(2026, 3, 2), "PAYMENT RECEIVED", -100])
    p = tmp_path / "card.xlsx"
    wb.save(p)
    rows = read_table(p)
    layout = detect_layout(rows)
    assert layout.header_row == 1
    layout.flip = True
    lines, problems = read_lines(rows, layout)
    assert not problems
    assert [(ln["date"], ln["amount"]) for ln in lines] == [(dt.date(2026, 3, 1), Decimal("-23.50")),
                                                            (dt.date(2026, 3, 2), Decimal("100.00"))]


# ---- importing --------------------------------------------------------------------------------- #
def test_overlapping_statements_are_not_imported_twice(gst_client, tmp_path):
    L = gst_client
    first = load(L, "18500", COMMBANK, tmp_path)
    assert first["lines"] == 4 and first["duplicates"] == 0
    again = load(L, "18500", COMMBANK, tmp_path)
    assert again["lines"] == 0 and again["duplicates"] == 4
    more = COMMBANK.replace("30/06/2026", "29/06/2026", 0) + '01/06/2026,"-4.50","COFFEE","+0.00"\n'
    assert load(L, "18500", more, tmp_path)["lines"] == 1
    feed = BankFeed(L.store, L.slug, "18500")
    assert len(feed.ordered("unreconciled")) == 5
    assert feed.statement_balance() == (dt.date(2026, 6, 30), Decimal("3134.00"))
    # two identical coffees on one day are two lines - and still only two after importing the file again
    two = '20/06/2026,"-4.50","COFFEE","+0.00"\n20/06/2026,"-4.50","COFFEE","+0.00"\n21/06/2026,"-1.00","X","+0.00"\n'
    assert load(L, "18500", two, tmp_path, "two.csv")["lines"] == 3
    assert load(L, "18500", two, tmp_path, "two.csv")["lines"] == 0


def test_statement_needs_a_balance_sheet_account(gst_client, tmp_path):
    with pytest.raises(BankError):
        load(gst_client, "72100", COMMBANK, tmp_path)


# ---- cash coding --------------------------------------------------------------------------- #
def code(L, account, pairs):
    feed = BankFeed(L.store, L.slug, account)
    by_text = {ln["description"]: ln["id"] for ln in feed.ordered()}
    return bank.reconcile(L, account, [Coding(by_text[text], acct, tax) for text, acct, tax in pairs])


def test_cash_coding_posts_bank_transactions_with_gst(gst_client, tmp_path):
    L = gst_client
    load(L, "18500", COMMBANK, tmp_path)
    res = code(L, "18500", [("Direct Credit 123456 ACME PTY LTD INV 1001", "51100", ""),
                            ("OFFICEWORKS 0123 SYDNEY AU Card xx1234 Value Date: 14/06/2026", "72100", ""),
                            ("MONTHLY ACCOUNT FEE", "72500", "BAS Excluded")])
    assert not res["errors"]
    assert [d["number"] for d in res["done"]] == ["B2026-0001", "B2026-0002", "B2026-0003"]
    L = Ledger(L.store, L.slug)
    tb = L.trial_balance(2026)
    bal = {r["code"]: r["balance"] for r in tb["rows"]}
    assert tb["difference"] == 0
    assert bal["18500"] == Decimal("1934.00")                 # 2,000 - 55 - 11
    assert bal["51100"] == Decimal("-1818.18")                # GST on Income (the account's own rate)
    assert bal["72100"] == Decimal("50.00")                   # GST on Expenses
    assert bal["72500"] == Decimal("11.00")                   # BAS Excluded, as chosen
    assert bal["35010"] == Decimal("-176.82")                 # GST: 181.82 collected less 5.00 paid
    j = L.get_journal(2026, "B2026-0002")
    assert j.kind == "bank" and j.date == dt.date(2026, 6, 16)
    assert [(ln.account, ln.debit, ln.credit, ln.tax, ln.auto) for ln in j.lines] == [
        ("72100", Decimal("50.00"), Decimal("0.00"), "GST on Expenses", False),
        ("35010", Decimal("5.00"), Decimal("0.00"), "", True),
        ("18500", Decimal("0.00"), Decimal("55.00"), "", False)]
    assert "B2026-0002" not in [x.number for x in L.journals(2026, bank=False)]
    assert L.store.read_audit(L.slug)[0]["action"] == "bank.reconciled"
    # bank transactions change on the bank page only
    with pytest.raises(LedgerError):
        L.void_journal(2026, "B2026-0002", "no")
    with pytest.raises(LedgerError):
        L.reverse_journal(2026, "B2026-0002")


def test_not_registered_for_gst_posts_the_whole_amount(store, tmp_path):
    c = store.create_client("No GST", "company")
    L = Ledger(store, c.slug)
    L.ensure_year(2026)
    load(L, "18500", COMMBANK, tmp_path)
    assert not code(L, "18500", [("OFFICEWORKS 0123 SYDNEY AU Card xx1234 Value Date: 14/06/2026", "72100", "GST on Expenses")])["errors"]
    bal = Ledger(store, c.slug).closing(2026)
    assert bal["72100"] == Decimal("55.00") and "35010" not in bal


def test_unreconcile_and_numbers_are_not_reused(gst_client, tmp_path):
    L = gst_client
    load(L, "18500", COMMBANK, tmp_path)
    code(L, "18500", [("MONTHLY ACCOUNT FEE", "72500", ""), ("Direct Credit 123456 ACME PTY LTD INV 1001", "51100", "")])
    feed = BankFeed(L.store, L.slug, "18500")
    fee = next(ln for ln in feed.ordered("reconciled") if ln["description"] == "MONTHLY ACCOUNT FEE")
    out = bank.unreconcile(L, "18500", fee["id"], "wrong account")
    assert out["number"] == "B2026-0002"
    assert "B2026-0002" not in [j.number for j in Ledger(L.store, L.slug).journals(2026)]
    res = code(L, "18500", [("MONTHLY ACCOUNT FEE", "72200", "")])
    assert res["done"][0]["number"] == "B2026-0003"                 # B2026-0002 is never used again
    line = BankFeed(L.store, L.slug, "18500").get(fee["id"])
    assert line["history"][0]["number"] == "B2026-0002" and line["history"][0]["undone"]["reason"] == "wrong account"


def test_locked_and_unstarted_years(gst_client, tmp_path):
    L = gst_client
    load(L, "18500", COMMBANK + '05/07/2026,"-9.00","NEXT YEAR","+0.00"\n', tmp_path)
    res = code(L, "18500", [("NEXT YEAR", "72500", "")])
    assert "FY2027, which has not been started" in res["errors"][next(iter(res["errors"]))]
    code(L, "18500", [("MONTHLY ACCOUNT FEE", "72500", "")])
    L.lock_year(2026, "signed")
    res = code(L, "18500", [("Direct Credit 123456 ACME PTY LTD INV 1001", "51100", "")])
    assert "locked" in next(iter(res["errors"].values())) and not res["done"]
    fee = next(ln for ln in BankFeed(L.store, L.slug, "18500").ordered("reconciled"))
    with pytest.raises(BankError):
        bank.unreconcile(L, "18500", fee["id"])


def test_bad_codings_are_reported_line_by_line(gst_client, tmp_path):
    L = gst_client
    load(L, "18500", COMMBANK, tmp_path)
    res = code(L, "18500", [("MONTHLY ACCOUNT FEE", "", ""), ("Direct Credit 123456 ACME PTY LTD INV 1001", "99999", ""),
                            ("Transfer from xx9999 CommBank app", "18500", ""),
                            ("OFFICEWORKS 0123 SYDNEY AU Card xx1234 Value Date: 14/06/2026", "72100", "")])
    assert sorted(res["errors"].values()) == ["a line cannot be coded to its own bank account", "account 99999 is not in the chart",
                                              "choose an account"]
    assert len(res["done"]) == 1


def test_a_transfer_between_two_imported_accounts_posts_once(gst_client, tmp_path):
    L = gst_client
    load(L, "18500", '20/06/2026,"-1000.00","PAYMENT TO CREDIT CARD","+0.00"\n'
                     '21/06/2026,"-1.00","FEE","+0.00"\n22/06/2026,"-2.00","FEE 2","+0.00"\n', tmp_path, "cheque.csv")
    load(L, "32700", '21/06/2026,"+1000.00","PAYMENT RECEIVED THANK YOU","+0.00"\n'
                     '22/06/2026,"-30.00","NETFLIX","+0.00"\n23/06/2026,"-3.00","FEE 3","+0.00"\n', tmp_path, "card.csv")
    first = code(L, "18500", [("PAYMENT TO CREDIT CARD", "32700", "")])
    assert first["done"][0]["matched"] is False
    second = code(L, "32700", [("PAYMENT RECEIVED THANK YOU", "18500", "")])
    assert second["done"][0] == {"line": second["done"][0]["line"], "number": first["done"][0]["number"], "matched": True}
    bal = Ledger(L.store, L.slug).closing(2026)
    assert bal["18500"] == Decimal("-1000.00") and bal["32700"] == Decimal("1000.00")     # once, not twice
    # taking the posted side back out sends the matched side back to be coded too
    cheque = BankFeed(L.store, L.slug, "18500").ordered("reconciled")[0]
    out = bank.unreconcile(L, "18500", cheque["id"], "wrong")
    assert out["also"]["feed"] == "32700"
    assert BankFeed(L.store, L.slug, "32700").get(out["also"]["line"])["status"] == "unreconciled"
    assert not Ledger(L.store, L.slug).closing(2026)


def test_suggestions_learn_from_earlier_coding(gst_client, tmp_path):
    L = gst_client
    load(L, "18500", COMMBANK, tmp_path)
    code(L, "18500", [("OFFICEWORKS 0123 SYDNEY AU Card xx1234 Value Date: 14/06/2026", "72100", "GST on Expenses")])
    load(L, "18500", '25/06/2026,"-19.95","OFFICEWORKS 0456 MELBOURNE AU Card xx1234","+0.00"\n', tmp_path, "b.csv")
    feeds = bank.feeds(L.store, L.slug)
    waiting = BankFeed(L.store, L.slug, "18500").ordered("unreconciled")
    sugg = bank.suggestions(feeds, waiting)
    new = next(ln for ln in waiting if ln["description"].startswith("OFFICEWORKS 0456"))
    assert sugg[new["id"]] == {"account": "72100", "tax": "GST on Expenses"}
    fee = next(ln for ln in waiting if ln["description"] == "MONTHLY ACCOUNT FEE")
    assert fee["id"] not in sugg


def test_deleted_lines_stay_deleted(gst_client, tmp_path):
    L = gst_client
    load(L, "18500", COMMBANK, tmp_path)
    feed = BankFeed(L.store, L.slug, "18500")
    fee = next(ln for ln in feed.ordered() if ln["description"] == "MONTHLY ACCOUNT FEE")
    assert bank.delete_lines(L, "18500", [fee["id"]], "duplicate") == 1
    assert load(L, "18500", COMMBANK, tmp_path)["lines"] == 0
    assert len(BankFeed(L.store, L.slug, "18500").ordered()) == 3


def test_dashboard_summary(gst_client, tmp_path):
    L = gst_client
    load(L, "18500", COMMBANK, tmp_path)
    code(L, "18500", [("MONTHLY ACCOUNT FEE", "72500", "")])
    s = bank.summaries(Ledger(L.store, L.slug), 2026)
    assert len(s) == 1
    assert s[0]["code"] == "18500" and s[0]["to_reconcile"] == 3 and s[0]["reconciled"] == 1
    assert s[0]["statement"] == (dt.date(2026, 6, 30), Decimal("3134.00"))
    assert s[0]["ledger_balance"] == Decimal("-11.00") and s[0]["as_at"] == dt.date(2026, 6, 30)


def test_sync_conflict_copies_of_bank_files_are_reported(gst_client, tmp_path):
    L = gst_client
    load(L, "18500", COMMBANK, tmp_path)
    src = L.store.bank_path(L.slug, "18500")
    (src.parent / "18500-PC2.json").write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    assert L.store.conflicts(L.slug) == ["bank/18500-PC2.json"]
    assert L.store.list_bank_accounts(L.slug) == ["18500"]
