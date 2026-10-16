import datetime as dt
from decimal import Decimal

import pytest

from fortis_ledger.engine import REPORT_MAP, fr_engine
from fortis_ledger.ledger import Ledger, LedgerError, ValidationError
from fortis_ledger.money import to_dec
from fortis_ledger.store import FolderStore, LockedError

from conftest import J


def test_money_parsing():
    assert to_dec("1,234.50") == Decimal("1234.50")
    assert to_dec("(1,234.50)") == Decimal("-1234.50")
    assert to_dec("12.3-") == Decimal("-12.30")
    assert to_dec("-") == Decimal("0.00")
    assert to_dec(114.13113) == Decimal("114.13")
    with pytest.raises(ValueError):
        to_dec("abc")


def test_journal_rules(company):
    L = company
    with pytest.raises(ValidationError) as e:
        L.save_journal(J("2025-06-30", "unbalanced", ("18500", 100, 0), ("51100", 0, 90)), 2025)
    assert any("differ by 10.00" in x for x in e.value.errors)
    with pytest.raises(ValidationError) as e:
        L.save_journal(J("2025-06-30", "bad account", ("99999", 100, 0), ("51100", 0, 100)), 2025)
    assert any("not in the chart" in x for x in e.value.errors)
    with pytest.raises(ValidationError) as e:
        L.save_journal(J("2025-07-01", "wrong year", ("18500", 100, 0), ("51100", 0, 100)), 2025)
    assert any("outside FY2025" in x for x in e.value.errors)
    with pytest.raises(ValidationError) as e:
        L.save_journal(J("2025-06-30", "system account", ("800", 100, 0), ("51100", 0, 100)), 2025)
    assert any("system account" in x for x in e.value.errors)
    # drafts may be unbalanced
    d = L.save_journal(J("2025-06-30", "work in progress", ("18500", 100, 0)), 2025, post=False)
    assert d.status == "draft" and d.number.startswith("J2025-")
    assert L.trial_balance(2025)["difference"] == 0      # drafts never hit the balances


def test_trial_balance_and_carry_forward(company):
    L = company
    tb = L.trial_balance(2025)
    assert tb["difference"] == 0 and tb["profit"] == Decimal("35000.00")
    L.ensure_year(2026)
    L.save_journal(J("2026-06-30", "FY2026 sales", ("13600", 60000, 0), ("51100", 0, 60000)), 2026)
    bal = L.closing(2026)
    assert bal["43000"] == Decimal("-45000.00")          # 10,000 + FY2025 profit 35,000
    assert "51100" in bal and bal["51100"] == Decimal("-60000.00")   # P&L starts at nil each year
    assert bal["13600"] == Decimal("105000.00")
    assert L.trial_balance(2026)["difference"] == 0


def test_void_and_reverse(company):
    L = company
    L.ensure_year(2026)
    j = L.save_journal(J("2026-06-30", "accrual", ("72930", 1000, 0), ("31300", 0, 1000)), 2026)
    assert L.closing(2026)["31300"] == Decimal("-5900.00")
    r = L.reverse_journal(2026, j.number)
    assert r.kind == "reversal" and L.get_journal(2026, j.number).reversed_by == r.number
    L.ensure_year(2027)
    assert L.closing(2027).get("31300") == Decimal("-4900.00")
    with pytest.raises(LedgerError):
        L.void_journal(2026, j.number, "")
    L.void_journal(2026, j.number, "entered in error")
    assert L.get_journal(2026, j.number).status == "void"


def test_lock_year_blocks_changes_and_unlock_needs_reason(company):
    L = company
    L.lock_year(2025, "final report issued")
    with pytest.raises(ValidationError):
        L.save_journal(J("2025-06-30", "late adj", ("72930", 1, 0), ("31300", 0, 1)), 2025)
    with pytest.raises(LedgerError):
        L.unlock_year(2025, "")
    L.unlock_year(2025, "ATO amendment")
    L.save_journal(J("2025-06-30", "late adj", ("72930", 1, 0), ("31300", 0, 1)), 2025)
    actions = [a["action"] for a in L.store.read_audit(L.slug)]
    assert "year.locked" in actions and "year.unlocked" in actions


def test_edit_lock_between_users(store, company):
    other = FolderStore(store.root, user="Someone Else", machine="pc2")
    ok, info = other.acquire_lock(company.slug)
    assert not ok and info["user"] == "Tester"            # Tester holds it from posting the fixture journal
    with pytest.raises(LockedError):
        other.require_lock(company.slug)
    store.release_lock(company.slug)
    assert other.acquire_lock(company.slug)[0]
    with pytest.raises(LockedError):
        Ledger(store, company.slug).save_journal(J("2025-06-30", "x", ("72930", 1, 0), ("31300", 0, 1)), 2025)


def test_company_year_end_close_moves_dividends(company):
    L = company
    L.close_year(2025)
    div = next(a.code for a in L.chart.accounts if a.report_code.startswith("EQU.RET.DIV"))
    L.save_journal(J("2026-06-30", "dividend", (div, 5000, 0), ("18500", 0, 5000)), 2026)
    res = L.close_year(2026)
    assert res["journal"].startswith("J2027-") and res["moves"][0][0] == div
    bal = L.closing(2027)
    assert div not in bal and bal["43000"] == Decimal("-40000.00")   # 45,000 - 5,000
    # running it again refreshes the same journal, and notices when the year changes
    assert L.close_status(2026)["stale"] is False
    L.save_journal(J("2026-06-30", "second dividend", (div, 1000, 0), ("18500", 0, 1000)), 2026)
    assert L.close_status(2026)["stale"] is True
    again = L.close_year(2026)
    assert again["journal"] == res["journal"]
    assert Ledger(L.store, L.slug).closing(2027)["43000"] == Decimal("-39000.00")


def test_trust_close_moves_beneficiary_accounts_to_opening(store):
    c = store.create_client("Test Family Trust", "trust", directors=["A Director"])
    L = Ledger(store, c.slug)
    L.ensure_year(2026)
    L.save_journal(J("2026-06-30", "year", ("18500", 2000, 0), ("58900", 0, 3000), ("90010", 3000, 0),
                     ("46010", 600, 0), ("46020", 400, 0), ("31600", 0, 3000)), 2026)
    res = L.close_year(2026)
    created = {a["code"]: a for a in res["created"]}
    assert created and all(a["report_code"].startswith("EQU.ADV.") for a in created.values())
    bal = Ledger(store, c.slug).closing(2027)
    assert "46010" not in bal and "46020" not in bal
    assert sorted(bal[k] for k in created) == [Decimal("400.00"), Decimal("600.00")]


def test_pack_feeds_the_report_engine(company):
    L = company
    L.close_year(2025)
    L.save_journal(J("2026-06-30", "FY2026 sales", ("13600", 60000, 0), ("51100", 0, 60000)), 2026)
    pack = fr_engine.Pack(L.pack(2026), REPORT_MAP)
    assert not pack.has_errors(), pack.validation_markdown()
    s = pack.summary()
    assert s["npat"]["cy"] == 60000 and s["npat"]["py"] == 35000
    assert pack.bs["net_assets"]["cy"] == pack.bs["equity"]["total"]["cy"] == 105100
    # first year: no prior year in the ledger -> single column
    first = fr_engine.Pack(L.pack(2025), REPORT_MAP)
    assert first.single_column


def test_interim_period(company):
    L = company
    L.close_year(2025)
    L.save_journal(J("2025-12-31", "half year", ("13600", 100, 0), ("51100", 0, 100)), 2026)
    L.save_journal(J("2026-06-30", "rest", ("13600", 900, 0), ("51100", 0, 900)), 2026)
    pk = L.pack(2026, upto=dt.date(2025, 12, 31))
    assert pk["entity"]["period_label"] == "For the period 1 July 2025 to 31 December 2025"
    assert next(a for a in pk["accounts"] if a["code"] == "51100")["cy"] == 100.0


def test_profit_is_before_dividends_like_the_reports(store):
    c = store.create_client("Div Co Pty Ltd", "company", directors=["A Director"])
    L = Ledger(store, c.slug)
    L.chart.add("50600", "Dividend paid", "EXP.DIV", "Expense")       # Xero's P&L-coded dividends
    L.ensure_year(2026)
    L.save_journal(J("2026-06-30", "year", ("18500", 8000, 0), ("51100", 0, 10000), ("50600", 2000, 0)), 2026)
    tb = L.trial_balance(2026)
    assert tb["profit"] == Decimal("10000.00") and tb["appropriations"] == Decimal("2000.00")
    assert tb["retained"] == Decimal("8000.00")
    pk = fr_engine.Pack(L.pack(2026), REPORT_MAP)
    assert pk.summary()["npat"]["cy"] == 10000
    L.close_year(2026)
    assert Ledger(store, c.slug).closing(2027)[L.retained_earnings_code()] == Decimal("-8000.00")
