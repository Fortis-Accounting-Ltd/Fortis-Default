"""
ClientChart - a client's chart of accounts, kept in Xero's import/export CSV columns so it can be
exported to (or imported from) Xero unchanged.  Loading and name matching use the skill's
chart module; edits are written here so the app never exits on a bad edit.
"""
from __future__ import annotations

import csv
import os
import re
import uuid
from pathlib import Path

from .engine import chartlib, describe, slot_of

XERO_HEADER = ["*Code", "Report Code", "*Name", "Reporting Name", "*Type", "*Tax Code", "Description",
               "Dashboard", "Expense Claims", "Enable Payments"]
CODE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.\-]{0,9}$")

# Xero account types, in the order the add-account form offers them
ACCOUNT_TYPES = ["Bank", "Current Asset", "Inventory", "Prepayment", "Fixed Asset", "Non-current Asset",
                 "Current Liability", "Non-current Liability", "Liability", "Equity", "Revenue", "Sales",
                 "Other Income", "Direct Costs", "Expense", "Overhead", "Depreciation"]
PL_TYPES = {"revenue", "sales", "other income", "direct costs", "expense", "overhead", "overheads", "depreciation"}


class ChartError(Exception):
    pass


def default_type(report_code: str) -> str:
    rc = (report_code or "").upper()
    if rc.startswith("ASS.CUR.CAS.BAN"):
        return "Bank"
    if rc.startswith("ASS.CUR.INY"):
        return "Inventory"
    if rc.startswith("ASS.CUR.REC.PRE"):
        return "Prepayment"
    if rc.startswith("ASS.CUR"):
        return "Current Asset"
    if rc.startswith("ASS.NCA.FIX"):
        return "Fixed Asset"
    if rc.startswith("ASS"):
        return "Non-current Asset"
    if rc.startswith("LIA.CUR"):
        return "Current Liability"
    if rc.startswith("LIA"):
        return "Non-current Liability"
    if rc.startswith("EQU"):
        return "Equity"
    if rc == "REV" or rc.startswith("REV.TRA"):
        return "Revenue"
    if rc.startswith("REV"):
        return "Other Income"
    if rc.startswith("EXP.COS"):
        return "Direct Costs"
    if rc.startswith("EXP.DEP"):
        return "Depreciation"
    return "Expense"


def default_report_code(acct_type: str) -> str:
    t = (acct_type or "").strip().lower()
    return {"bank": "ASS.CUR.CAS.BAN", "current asset": "ASS.CUR.REC", "inventory": "ASS.CUR.INY",
            "prepayment": "ASS.CUR.REC.PRE", "fixed asset": "ASS.NCA.FIX.PLA", "non-current asset": "ASS.NCA",
            "current liability": "LIA.CUR.PAY.TRA", "liability": "LIA.CUR.PAY.TRA",
            "non-current liability": "LIA.NCL.LOA.UNS", "equity": "EQU", "revenue": "REV.TRA",
            "sales": "REV.TRA", "other income": "REV.OTH", "direct costs": "EXP.COS", "expense": "EXP",
            "overhead": "EXP", "overheads": "EXP", "depreciation": "EXP.DEP"}.get(t, "")


def is_pl_account(report_code: str, acct_type: str = "") -> bool:
    rc = (report_code or "").upper()
    if rc:
        return rc.startswith(("REV", "EXP"))
    return (acct_type or "").strip().lower() in PL_TYPES


def code_sort_key(code: str):
    digits = re.match(r"^(\d+)(.*)$", code or "")
    if digits:
        return (0, int(digits.group(1)), digits.group(2))
    return (1, 0, code or "")


class ClientChart:
    def __init__(self, path: str | Path, entity_type: str):
        self.path = Path(path)
        self.entity_type = chartlib.canonical_type(entity_type)
        self.reload()

    def reload(self) -> None:
        self.chart = chartlib.Chart(self.path, self.entity_type)
        self.header = list(self.chart.header) or list(XERO_HEADER)
        self.rows = self.chart.rows
        self._by_code = {a.code: a for a in self.chart.accounts}

    # ---- reading ------------------------------------------------------------------------ #
    @property
    def accounts(self):
        return sorted(self.chart.accounts, key=lambda a: code_sort_key(a.code))

    def get(self, code: str):
        return self._by_code.get(str(code or "").strip())

    def by_name(self, name: str):
        n = " ".join((name or "").lower().split())
        return next((a for a in self.chart.accounts if " ".join(a.name.lower().split()) == n), None)

    def matcher(self):
        return chartlib.Matcher(self.chart, self.entity_type)

    def usable(self):
        """Accounts a journal may post to (Xero 'DO NOT USE' system accounts excluded)."""
        return [a for a in self.accounts if not a.system]

    # ---- editing ------------------------------------------------------------------------ #
    def _field(self, logical: str) -> str:
        for h in self.header:
            if h.lstrip("*").strip().lower().replace(" ", "_") == logical:
                return h
        # chart without the column (simple CSV) - add it
        self.header.append(logical.replace("_", " ").title())
        return self.header[-1]

    def _row(self, code: str) -> dict:
        f = self._field("code")
        for r in self.rows:
            if r.get(f, "").strip() == code:
                return r
        raise ChartError(f"no account {code}")

    @staticmethod
    def check_report_code(report_code: str) -> str:
        rc = (report_code or "").strip().upper()
        if not rc:
            raise ChartError("a report code is required")
        if slot_of(rc) is None:
            raise ChartError(f"unknown report code {rc} - use a Xero report code such as LIA.CUR.PAY.TRA")
        return rc

    def add(self, code: str, name: str, report_code: str, acct_type: str = "", reporting_name: str = "",
            tax_code: str = "BAS Excluded", description: str = ""):
        code = (code or "").strip()
        if not CODE_RE.match(code):
            raise ChartError("account code: up to 10 letters, digits, '.' or '-'")
        if self.get(code):
            raise ChartError(f"account {code} already exists ({self.get(code).name})")
        if not (name or "").strip():
            raise ChartError("account name is required")
        rc = self.check_report_code(report_code)
        row = {h: "" for h in self.header}
        row[self._field("code")] = code
        row[self._field("name")] = name.strip()
        row[self._field("report_code")] = rc
        row[self._field("type")] = acct_type or default_type(rc)
        row[self._field("reporting_name")] = reporting_name or ""
        row[self._field("tax_code")] = tax_code
        row[self._field("description")] = description or ""
        for h in self.header:
            if h.strip() in ("Dashboard", "Expense Claims", "Enable Payments") and not row.get(h):
                row[h] = "No"
        self.rows.append(row)
        self.save()
        return self.get(code)

    def update(self, code: str, name: str | None = None, report_code: str | None = None,
               reporting_name: str | None = None, acct_type: str | None = None) -> dict:
        row = self._row(code)
        changes = {}
        for logical, value in (("name", name), ("reporting_name", reporting_name), ("type", acct_type)):
            if value is not None and row.get(self._field(logical), "") != value.strip():
                changes[logical] = [row.get(self._field(logical), ""), value.strip()]
                row[self._field(logical)] = value.strip()
        if report_code is not None:
            rc = self.check_report_code(report_code)
            if row.get(self._field("report_code"), "").upper() != rc:
                changes["report_code"] = [row.get(self._field("report_code"), ""), rc]
                row[self._field("report_code")] = rc
        if changes:
            self.save()
        return changes

    def next_code(self, base: str) -> str:
        """A free code near `base`: 61100 -> 61100.1, 61100.2 ...; 'X' -> X1, X2 ..."""
        base = (base or "").strip() or "9"
        for i in range(1, 1000):
            cand = f"{base}.{i}" if base[-1].isdigit() else f"{base}{i}"
            cand = cand[:10]
            if not self.get(cand) and CODE_RE.match(cand):
                return cand
        raise ChartError(f"no free code near {base}")

    def save(self) -> None:
        f_code = self._field("code")
        self.rows.sort(key=lambda r: code_sort_key(r.get(f_code, "")))
        tmp = self.path.with_name(f".{self.path.name}.{uuid.uuid4().hex[:8]}.tmp")
        with open(tmp, "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=self.header, extrasaction="ignore")
            w.writeheader()
            w.writerows(self.rows)
        os.replace(tmp, self.path)
        self.reload()

    # ---- whole-chart import ------------------------------------------------------------- #
    def merge(self, other_path: str | Path, replace: bool = False) -> dict:
        """
        Bring in a chart exported from Xero (or another client): new accounts are added, existing codes
        get the incoming name / report code / type / reporting name.  replace=True also removes accounts
        that are not in the incoming chart (only allowed when they carry no transactions - the caller checks).
        """
        incoming = chartlib.Chart(other_path, self.entity_type)
        added, changed, removed = [], [], []
        seen = set()
        for a in incoming.accounts:
            if not a.code:
                continue
            seen.add(a.code)
            rc = (a.report_code or default_report_code(a.type)).upper()
            if rc and slot_of(rc) is None:
                rc = default_report_code(a.type) or rc
            cur = self.get(a.code)
            if cur is None:
                row = {h: "" for h in self.header}
                row[self._field("code")] = a.code
                row[self._field("name")] = a.name
                row[self._field("report_code")] = rc
                row[self._field("type")] = a.type or default_type(rc)
                row[self._field("reporting_name")] = a.reporting_name
                row[self._field("tax_code")] = a.tax_code or "BAS Excluded"
                row[self._field("description")] = a.description
                self.rows.append(row)
                added.append(a.code)
            else:
                row = self._row(a.code)
                diff = {}
                for logical, value in (("name", a.name), ("report_code", rc), ("type", a.type),
                                       ("reporting_name", a.reporting_name)):
                    if value and row.get(self._field(logical), "") != value:
                        diff[logical] = [row.get(self._field(logical), ""), value]
                        row[self._field(logical)] = value
                if diff:
                    changed.append({"code": a.code, "changes": diff})
        if replace:
            f_code = self._field("code")
            keep = [r for r in self.rows if r.get(f_code, "") in seen]
            removed = [r.get(f_code, "") for r in self.rows if r.get(f_code, "") not in seen]
            self.rows[:] = keep
        self.save()
        return {"added": added, "changed": changed, "removed": removed}

    def describe(self, code: str) -> str:
        a = self.get(code)
        return describe(a.report_code, a.name) if a else ""
