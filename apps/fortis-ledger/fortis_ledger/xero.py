"""
Moving a client off Xero.

Two routes, same result (a client whose chart is its own Xero chart, report codes included, and whose
years hold the Xero trial balances):

* Files exported from Xero by staff - Accounting > Chart of accounts > Export (CSV), and
  Reports > Trial Balance as at each year end > Export (Excel or CSV).  Use migrate_from_files.
* The Xero API (where the client file is connected) - Accounts, Reports/TrialBalance and Organisation
  JSON.  Use migrate_from_api.

Trial balances are imported oldest first; each later year posts only the difference from the
balances carried forward, so the ledger's closing balances equal Xero's at every year end.
"""
from __future__ import annotations

import csv
import re
from pathlib import Path

from .chartbook import XERO_HEADER, default_report_code
from .imports import BalanceRow, apply_plan, plan_balances, plan_from_file
from .ledger import Ledger
from .model import Client
from .money import to_dec
from .store import FolderStore

API_TYPES = {
    "BANK": "Bank", "CURRENT": "Current Asset", "CURRLIAB": "Current Liability", "DEPRECIATN": "Depreciation",
    "DIRECTCOSTS": "Direct Costs", "EQUITY": "Equity", "EXPENSE": "Expense", "FIXED": "Fixed Asset",
    "INVENTORY": "Inventory", "LIABILITY": "Liability", "NONCURRENT": "Non-current Asset",
    "OTHERINCOME": "Other Income", "OVERHEADS": "Overhead", "PREPAYMENT": "Prepayment", "REVENUE": "Revenue",
    "SALES": "Sales", "TERMLIAB": "Non-current Liability", "PAYGLIABILITY": "Current Liability",
    "SUPERANNUATIONEXPENSE": "Expense", "SUPERANNUATIONLIABILITY": "Current Liability",
    "WAGESEXPENSE": "Expense", "WAGESPAYABLELIABILITY": "Current Liability",
}
ENTITY_TYPES = {"COMPANY": "company", "TRUST": "trust", "PARTNERSHIP": "partnership", "SOLETRADER": "sole_trader",
                "INDIVIDUAL": "sole_trader"}


def format_abn(raw: str) -> str:
    d = re.sub(r"\D", "", raw or "")
    return f"{d[0:2]} {d[2:5]} {d[5:8]} {d[8:11]}" if len(d) == 11 else (raw or "")


def client_fields_from_org(org_json: dict) -> dict:
    org = (org_json.get("Organisations") or [org_json])[0]
    etype = ENTITY_TYPES.get((org.get("OrganisationEntityType") or org.get("OrganisationType") or "").upper())
    if not etype:
        raise ValueError(f"Xero entity type {org.get('OrganisationEntityType')} is not supported "
                         f"(company, trust, partnership or sole trader)")
    legal = org.get("LegalName") or ""
    return {"name": org.get("Name") or legal, "type": etype,
            "legal_name": legal if legal and legal != org.get("Name") else "",
            "abn": format_abn(org.get("RegistrationNumber") or ""),
            "fy_end_month": int(org.get("FinancialYearEndMonth") or 6),
            "fy_end_day": int(org.get("FinancialYearEndDay") or 30),
            "source": {"xero_organisation_id": org.get("OrganisationID", ""), "xero_class": org.get("Class", "")}}


def chart_rows_from_api(accounts_json: dict) -> list[dict]:
    rows = []
    for a in accounts_json.get("Accounts", []):
        if (a.get("Status") or "ACTIVE") == "DELETED":
            continue
        code = (a.get("Code") or "").strip()
        if not code:
            continue   # Xero allows code-less accounts (e.g. some bank accounts); they get one on import
        typ = API_TYPES.get((a.get("Type") or "").upper(), a.get("Type") or "")
        rc = (a.get("ReportingCode") or "").upper() or default_report_code(typ)
        rows.append({"*Code": code, "Report Code": rc, "*Name": a.get("Name", ""),
                     "Reporting Name": "", "*Type": typ,
                     "*Tax Code": a.get("TaxType", ""), "Description": a.get("Description", "") or "",
                     "Dashboard": "No", "Expense Claims": "No", "Enable Payments": "No"})
    return rows


def write_chart_csv(rows: list[dict], path: str | Path) -> Path:
    path = Path(path)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=XERO_HEADER, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    return path


def tb_rows_from_api(tb_json: dict, accounts_json: dict) -> list[BalanceRow]:
    """Reports/TrialBalance JSON -> balance rows (YTD debit - YTD credit)."""
    by_id = {a.get("AccountID"): a for a in accounts_json.get("Accounts", [])}
    report = (tb_json.get("Reports") or [tb_json])[0]
    header = []
    out = []
    n = 0

    def walk(rows, section=""):
        nonlocal header, n
        for r in rows or []:
            rt = r.get("RowType")
            if rt == "Header":
                header = [(c.get("Value") or "").strip().lower() for c in r.get("Cells", [])]
            elif rt == "Section":
                walk(r.get("Rows"), r.get("Title") or section)
            elif rt == "Row":
                cells = r.get("Cells", [])
                if not cells:
                    continue
                n += 1
                label = cells[0].get("Value") or ""
                acc_id = next((at.get("Value") for at in cells[0].get("Attributes", []) or []
                               if (at.get("Id") or "").lower() == "account"), None)
                acc = by_id.get(acc_id) or {}
                vals = {header[i] if i < len(header) else str(i): (c.get("Value") or "") for i, c in enumerate(cells)}
                dr = vals.get("ytd debit", vals.get("debit", ""))
                cr = vals.get("ytd credit", vals.get("credit", ""))
                code = (acc.get("Code") or "").strip()
                name = acc.get("Name") or re.sub(r"\s*\([^)]*\)\s*$", "", label)
                if not code:
                    m = re.search(r"\(([A-Za-z0-9.\-]{1,10})\)\s*$", label)
                    code = m.group(1) if m else ""
                typ = API_TYPES.get((acc.get("Type") or "").upper(), "")
                out.append(BalanceRow(line=n, code=code, name=name, amount=to_dec(dr) - to_dec(cr),
                                      report_code=(acc.get("ReportingCode") or "").upper(), acct_type=typ,
                                      section=section))

    walk(report.get("Rows"))
    return out


def _new_client(store: FolderStore, fields: dict, chart_csv: Path) -> Ledger:
    fields = dict(fields)
    name, etype = fields.pop("name"), fields.pop("type")
    c = store.create_client(name, etype, **fields)
    L = Ledger(store, c.slug)
    store.require_lock(c.slug)
    res = L.chart.merge(chart_csv, replace=True)
    store.audit(c.slug, "chart.imported", {"source": chart_csv.name, "added": len(res["added"]),
                                           "changed": len(res["changed"]), "removed": len(res["removed"]),
                                           "replace": True})
    # Xero's retained earnings system account
    re_acct = next((a for a in L.chart.accounts if a.report_code == "EQU.RET" and
                    re.search(r"retained", a.name, re.I)), None)
    if re_acct:
        L.client.retained_earnings_account = re_acct.code
        store.save_client(L.client, "client.updated", {"retained_earnings_account": re_acct.code})
    return Ledger(store, c.slug)


def migrate_from_api(store: FolderStore, org_json: dict, accounts_json: dict, tbs: dict[int, dict],
                     work_dir: str | Path) -> tuple[Client, list[dict]]:
    fields = client_fields_from_org(org_json)
    chart_csv = write_chart_csv(chart_rows_from_api(accounts_json), Path(work_dir) / "xero-chart.csv")
    L = _new_client(store, fields, chart_csv)
    results = []
    for fy in sorted(tbs):
        rows = tb_rows_from_api(tbs[fy], accounts_json)
        plan = plan_balances(L, rows, fy, "year_end", f"Xero trial balance {L.fy_range(fy)[1]:%d/%m/%Y}",
                             {"source": "xero-api"})
        results.append({"fy": fy, "plan": plan, "result": apply_plan(L, plan)})
        L = Ledger(store, L.slug)
    return L.client, results


def migrate_from_files(store: FolderStore, fields: dict, chart_csv: str | Path, tb_files: dict[int, str | Path]
                       ) -> tuple[Client, list[dict]]:
    L = _new_client(store, fields, Path(chart_csv))
    results = []
    for fy in sorted(tb_files):
        plan = plan_from_file(L, tb_files[fy], fy, "year_end")
        plan.include_py = False
        results.append({"fy": fy, "plan": plan, "result": apply_plan(L, plan)})
        L = Ledger(store, L.slug)
    return L.client, results
