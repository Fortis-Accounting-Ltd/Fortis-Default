"""
Profit and Loss, Balance Sheet and the GST report - the figures behind the screens and the PDF / Excel files.

* Profit and Loss: Trading Income, Cost of Sales, Gross Profit, Other Income, Operating Expenses and Net
  Profit (with Income Tax Expense below it when there is any), then dividends / distributions coded to the
  P&L - laid out the way Xero lays it out, by each account's report code.  Any period (it can span
  financial years), with the same period a year earlier beside it.
* Balance Sheet: Bank, Current Assets, Fixed Assets, Non-current Assets, Current and Non-current
  Liabilities, Net Assets, and Equity with the current year's earnings - as at any date, with the same
  date a year earlier beside it.
* GST report: the GST on each tax rate and the BAS labels (G1, G3, G4, G10, G11, 1A, 1B) for a period.
  Cash basis counts money received and paid (bank transactions); accrual basis also counts journals
  with a tax rate (e.g. invoices raised or bills received that were entered as journals).
"""
from __future__ import annotations

import datetime as dt
from collections import defaultdict
from decimal import Decimal

from .bank import TAX_RATES
from .chartbook import code_sort_key, default_report_code
from .engine import slot_of
from .money import ZERO

GST_SALES = ("GST on Income", "GST Free Income")
GST_PURCHASES = ("GST on Expenses", "GST Free Expenses", "GST on Capital", "GST Free Capital")
GST_CAPITAL = ("GST on Capital", "GST Free Capital")
BASES = {"accrual": "Accrual basis", "cash": "Cash basis"}


class StatementError(Exception):
    pass


# ---------------------------------------------------------------------------------------- #
# periods
# ---------------------------------------------------------------------------------------- #
def year_earlier(d: dt.date) -> dt.date:
    try:
        return d.replace(year=d.year - 1)
    except ValueError:          # 29 February
        return d.replace(year=d.year - 1, day=28)


def _whole_year(L, start: dt.date, end: dt.date) -> int | None:
    fy = L.client.fy_of(end)
    return fy if (start, end) == L.fy_range(fy) else None


def _covered(L, start: dt.date, end: dt.date) -> bool:
    """Whether every financial year the period touches has been started."""
    years = set(L.years())
    fy, last = L.client.fy_of(start), L.client.fy_of(end)
    while fy <= last:
        if fy not in years:
            return False
        fy += 1
    return True


def movements_between(L, start: dt.date, end: dt.date) -> dict[str, Decimal]:
    """Each account's movement from `start` to `end` (both included) - across financial years if need be."""
    out: dict[str, Decimal] = defaultdict(lambda: ZERO)
    for fy in L.years():
        s, e = L.fy_range(fy)
        if e < start or s > end:
            continue
        for code, v in L.movements(fy, upto=min(end, e), since=max(start, s)).items():
            out[code] += v
    return {k: v for k, v in out.items() if v}


def period_label(start: dt.date, end: dt.date, fy: int | None) -> str:
    if fy:
        return f"For the year ended {end.day} {end:%B %Y}"
    return f"For the period {start.day} {start:%B %Y} to {end.day} {end:%B %Y}"


def _rc(a) -> str:
    return (a.report_code or default_report_code(a.type) or "").upper()


def _line(label: str, cy, py=None, code: str = "", kind: str = "line") -> dict:
    return {"kind": kind, "label": label, "code": code, "cy": cy, "py": py}


# ---------------------------------------------------------------------------------------- #
# Profit and Loss
# ---------------------------------------------------------------------------------------- #
def _pl_section(L, a) -> str:
    if L.is_appropriation(a.code):
        return "appropriations"
    rc = _rc(a)
    if rc.startswith("EXP.INC"):
        return "tax"
    if rc == "REV" or rc.startswith("REV.TRA"):
        return "trading"
    if rc.startswith("REV"):
        return "other"
    if rc.startswith("EXP.COS"):
        return "cos"
    return "expenses"


def profit_and_loss(L, start: dt.date, end: dt.date, compare: bool = True) -> dict:
    if start > end:
        raise StatementError("the period starts after it ends")
    if not _covered(L, start, end):
        raise StatementError(f"the period {start:%d/%m/%Y} - {end:%d/%m/%Y} runs into a financial year that has "
                             f"not been started")
    fy = _whole_year(L, start, end)
    if fy:
        p_start, p_end = L.fy_range(fy - 1)
    else:
        p_start, p_end = year_earlier(start), year_earlier(end)
    has_py = compare and _covered(L, p_start, p_end)
    cy = movements_between(L, start, end)
    py = movements_between(L, p_start, p_end) if has_py else {}
    groups: dict[str, list] = defaultdict(list)
    for code in sorted(set(cy) | set(py), key=code_sort_key):
        if not L.is_pl(code):
            continue
        a = L.chart.get(code)
        groups[_pl_section(L, a)].append(a)
    totals = {}
    rows: list[dict] = []

    def section(key: str, title: str, sign: int) -> None:
        c_tot, p_tot = ZERO, ZERO
        accts = groups.get(key, [])
        if accts:
            rows.append(_line(title, None, None, kind="heading"))
        for a in accts:
            c, p = cy.get(a.code, ZERO) * sign, py.get(a.code, ZERO) * sign
            c_tot += c
            p_tot += p
            rows.append(_line(a.name, c, p if has_py else None, code=a.code))
        if accts:
            rows.append(_line(f"Total {title}", c_tot, p_tot if has_py else None, kind="total"))
        totals[key] = (c_tot, p_tot)

    def result(label: str, c, p) -> None:
        rows.append(_line(label, c, p if has_py else None, kind="result"))

    section("trading", "Trading Income", -1)
    section("cos", "Cost of Sales", 1)
    if groups.get("trading") or groups.get("cos"):
        gp = (totals["trading"][0] - totals["cos"][0], totals["trading"][1] - totals["cos"][1])
        result("Gross Profit", *gp)
    else:
        gp = (ZERO, ZERO)
    section("other", "Other Income", -1)
    section("expenses", "Operating Expenses", 1)
    npbt = (gp[0] + totals["other"][0] - totals["expenses"][0], gp[1] + totals["other"][1] - totals["expenses"][1])
    if groups.get("tax"):
        result("Net Profit Before Income Tax", *npbt)
        section("tax", "Income Tax Expense", 1)
        npat = (npbt[0] - totals["tax"][0], npbt[1] - totals["tax"][1])
        result("Net Profit After Income Tax", *npat)
    else:
        npat = npbt
        result("Net Profit", *npat)
    if groups.get("appropriations"):
        title = "Dividends" if L.client.type == "company" else "Distributions"
        section("appropriations", title, 1)
        app = totals["appropriations"]
        result("Retained Profit" if L.client.type == "company" else "Undistributed Profit",
               npat[0] - app[0], npat[1] - app[1])
    columns = [f"FY{fy}" if fy else f"{end:%d %b %Y}"]
    if has_py:
        columns.append(f"FY{fy - 1}" if fy else f"{p_end:%d %b %Y}")
    return {"title": "Profit and Loss", "period": period_label(start, end, fy), "start": start, "end": end,
            "columns": columns, "compare": has_py, "rows": rows, "net_profit": npat[0],
            "net_profit_py": npat[1] if has_py else None, "empty": not any(groups.values())}


# ---------------------------------------------------------------------------------------- #
# Balance Sheet
# ---------------------------------------------------------------------------------------- #
BS_SECTIONS = (("bank", "Bank"), ("current_assets", "Current Assets"), ("fixed", "Fixed Assets"),
               ("noncurrent_assets", "Non-current Assets"), ("current_liabilities", "Current Liabilities"),
               ("noncurrent_liabilities", "Non-current Liabilities"))


def _bs_section(a) -> str:
    rc = _rc(a)
    slot = (slot_of(rc, a.name) or "").upper() if rc else ""
    if rc.startswith("ASS.CUR.CAS"):
        return "bank"
    if rc.startswith(("ASS.NCA.FIX", "ASS.NCA.PPE")) or slot.startswith("ASS.NCA.PPE"):
        return "fixed"
    if rc.startswith("ASS.NCA"):
        return "noncurrent_assets"
    if rc.startswith("ASS"):
        return "current_assets"
    if rc.startswith(("LIA.NCL", "LIA.NCA")):
        return "noncurrent_liabilities"
    if rc.startswith("LIA"):
        return "current_liabilities"
    if rc.startswith("EQU.RET.CUR"):
        return "earnings"
    if rc.startswith("EQU"):
        return "equity"
    return "unclassified"


def _balances_at(L, d: dt.date) -> dict[str, Decimal] | None:
    fy = L.client.fy_of(d)
    if fy not in L.years():
        return None
    return L.closing(fy, None if d == L.fy_range(fy)[1] else d)


def balance_sheet(L, as_at: dt.date, compare: bool = True) -> dict:
    cy = _balances_at(L, as_at)
    if cy is None:
        raise StatementError(f"{as_at:%d/%m/%Y} is in FY{L.client.fy_of(as_at)}, which has not been started")
    fy = L.client.fy_of(as_at)
    py_date = L.fy_range(fy - 1)[1] if as_at == L.fy_range(fy)[1] else year_earlier(as_at)
    py = _balances_at(L, py_date) if compare else None
    has_py = py is not None
    py = py or {}
    groups: dict[str, list] = defaultdict(list)
    earnings = [ZERO, ZERO]
    for code in sorted(set(cy) | set(py), key=code_sort_key):
        a = L.chart.get(code)
        if a is not None and L.is_pl(code):
            earnings[0] -= cy.get(code, ZERO)
            earnings[1] -= py.get(code, ZERO)
            continue
        key = _bs_section(a) if a is not None else "unclassified"
        if key == "earnings":
            earnings[0] -= cy.get(code, ZERO)
            earnings[1] -= py.get(code, ZERO)
            continue
        groups[key].append((code, a.name if a else "(not in the chart)"))
    rows: list[dict] = []
    totals: dict[str, list] = {}

    def section(key: str, title: str, sign: int, heading_kind: str = "subheading") -> None:
        items = groups.get(key, [])
        tot = [ZERO, ZERO]
        if items:
            rows.append(_line(title, None, None, kind=heading_kind))
        for code, name in items:
            c, p = cy.get(code, ZERO) * sign, py.get(code, ZERO) * sign
            tot[0] += c
            tot[1] += p
            rows.append(_line(name, c, p if has_py else None, code=code))
        if items:
            rows.append(_line(f"Total {title}", tot[0], tot[1] if has_py else None, kind="total"))
        totals[key] = tot

    def result(label: str, c, p, kind: str = "result") -> None:
        rows.append(_line(label, c, p if has_py else None, kind=kind))

    rows.append(_line("Assets", None, None, kind="heading"))
    for key, title in BS_SECTIONS[:4]:
        section(key, title, 1)
    assets = [sum(totals[k][i] for k, _ in BS_SECTIONS[:4]) for i in (0, 1)]
    result("Total Assets", *assets)
    rows.append(_line("Liabilities", None, None, kind="heading"))
    for key, title in BS_SECTIONS[4:]:
        section(key, title, -1)
    liabilities = [sum(totals[k][i] for k, _ in BS_SECTIONS[4:]) for i in (0, 1)]
    result("Total Liabilities", *liabilities)
    net_assets = [assets[i] - liabilities[i] for i in (0, 1)]
    result("Net Assets", *net_assets, kind="grand")
    rows.append(_line("Equity", None, None, kind="heading"))
    equity = [ZERO, ZERO]
    for code, name in groups.get("equity", []):
        c, p = -cy.get(code, ZERO), -py.get(code, ZERO)
        equity[0] += c
        equity[1] += p
        rows.append(_line(name, c, p if has_py else None, code=code))
    rows.append(_line("Current Year Earnings", earnings[0], earnings[1] if has_py else None))
    equity = [equity[i] + earnings[i] for i in (0, 1)]
    result("Total Equity", *equity, kind="grand")
    if groups.get("unclassified"):
        section("unclassified", "Accounts without a balance sheet report code", 1, heading_kind="heading")
    out_by = net_assets[0] - equity[0]
    columns = [f"{as_at.day} {as_at:%b %Y}"]
    if has_py:
        columns.append(f"{py_date.day} {py_date:%b %Y}")
    return {"title": "Balance Sheet", "period": f"As at {as_at.day} {as_at:%B %Y}", "as_at": as_at,
            "py_date": py_date if has_py else None, "columns": columns, "compare": has_py, "rows": rows,
            "net_assets": net_assets[0], "total_equity": equity[0], "out_by": out_by,
            "unclassified": bool(groups.get("unclassified"))}


# ---------------------------------------------------------------------------------------- #
# GST report
# ---------------------------------------------------------------------------------------- #
def _side(L, tax: str, ln) -> str:
    if tax in GST_SALES:
        return "sales"
    if tax in GST_PURCHASES:
        return "purchases"
    a = L.chart.get(ln.account)          # Input Taxed: a sale when it is income, otherwise a purchase
    return "sales" if a is not None and _rc(a).startswith("REV") else "purchases"


def gst_report(L, start: dt.date, end: dt.date, basis: str = "accrual") -> dict:
    if basis not in BASES:
        raise StatementError("choose the cash or the accrual basis")
    if start > end:
        raise StatementError("the period starts after it ends")
    lines, left_out = [], 0
    for fy in L.years():
        s, e = L.fy_range(fy)
        if e < start or s > end:
            continue
        for j in L.journals(fy):
            if not (start <= j.date <= end):
                continue
            taxed = [ln for ln in j.lines if not ln.auto and ln.tax in TAX_RATES and ln.tax != "BAS Excluded"]
            if not taxed:
                continue
            if basis == "cash" and j.kind != "bank":
                left_out += len(taxed)
                continue
            for ln in taxed:
                side = _side(L, ln.tax, ln)
                if side == "sales":
                    net, gst = ln.credit - ln.debit, (ln.gst if ln.credit else -ln.gst)
                else:
                    net, gst = ln.debit - ln.credit, (ln.gst if ln.debit else -ln.gst)
                a = L.chart.get(ln.account)
                text = j.narration + (f" - {ln.description}" if ln.description and ln.description != j.narration else "")
                lines.append({"date": j.date, "number": j.number, "fy": fy, "kind": j.kind, "account": ln.account,
                              "account_name": a.name if a else "(not in the chart)", "text": text, "tax": ln.tax,
                              "side": side, "net": net, "gst": gst, "gross": net + gst})
    lines.sort(key=lambda x: (x["side"] != "sales", x["tax"], x["date"], x["number"]))
    by_rate: dict[str, dict] = {}
    for x in lines:
        r = by_rate.setdefault(x["tax"], {"tax": x["tax"], "side": x["side"], "net": ZERO, "gst": ZERO,
                                          "gross": ZERO, "count": 0})
        r["net"] += x["net"]
        r["gst"] += x["gst"]
        r["gross"] += x["gross"]
        r["count"] += 1

    def total(test, field="gross"):
        return sum((x[field] for x in lines if test(x)), ZERO)

    sales = lambda x: x["side"] == "sales"                       # noqa: E731
    purchases = lambda x: x["side"] == "purchases"               # noqa: E731
    labels = [
        ("G1", "Total sales (including any GST)", total(sales)),
        ("G2", "Export sales", ZERO),
        ("G3", "Other GST-free sales", total(lambda x: x["tax"] == "GST Free Income")),
        ("G4", "Input taxed sales", total(lambda x: sales(x) and x["tax"] == "Input Taxed")),
        ("G10", "Capital purchases (including any GST)", total(lambda x: x["tax"] in GST_CAPITAL)),
        ("G11", "Non-capital purchases (including any GST)", total(lambda x: purchases(x) and x["tax"] not in GST_CAPITAL)),
        ("1A", "GST on sales", total(sales, "gst")),
        ("1B", "GST on purchases", total(purchases, "gst")),
    ]
    one_a, one_b = labels[6][2], labels[7][2]
    fy = _whole_year(L, start, end)
    return {"title": "GST Report", "basis": basis, "basis_label": BASES[basis],
            "period": period_label(start, end, fy) if fy else
            f"{start.day} {start:%B %Y} to {end.day} {end:%B %Y}",
            "start": start, "end": end, "lines": lines,
            "rates": sorted(by_rate.values(), key=lambda r: (r["side"] != "sales", r["tax"])), "labels": labels,
            "net_gst": one_a - one_b, "left_out": left_out, "registered": bool(L.client.gst_registered)}


__all__ = ["StatementError", "profit_and_loss", "balance_sheet", "gst_report", "movements_between", "BASES"]
