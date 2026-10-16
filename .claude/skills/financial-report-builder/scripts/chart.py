#!/usr/bin/env python3
"""
chart.py - the firm's Xero charts of accounts and the matcher that "posts" each
workpaper line to the right chart account, the way a bookkeeper would when
setting the client up in Xero.

The four charts live in assets/chart-of-accounts/ in Xero's import/export format
(*Code, Report Code, *Name, Reporting Name, *Type, *Tax Code, ...), so a chart
exported from Xero can be dropped in unchanged and the bundled ones can be
imported into Xero.

    company.csv  trust.csv  partnership.csv  sole-trader.csv

Matching a workpaper line (name + the section header it sits under) to a chart
account, in order:
  1. client override (pack overrides / --set-code / --set-account; build_pack.py)
  2. exact name match inside the accounts the section allows
  3. the workpaper's own account code, when it is a chart code in the same section
     and the names fit (client files set up from the firm template)
  4. a specific keyword rule -> report-code family -> most similar chart account
     in that family (or the family code itself when the chart has no such account);
     a rule may name the usual account ("prefer") and may be limited to entity types
  5. strong name similarity (>= STRONG) inside the section
  6. the section's catch-all rule (trading income / cost of sales / expenses)
  7. moderate name similarity (>= WEAK)
  8. the section default (REVIEW)
Per-person equity accounts (Funds contributed / Drawings / Share of profits) are then
numbered by person, in workpaper order: 'Drawings - Jane' and 'Share of profits - Jane'
both land on partner / beneficiary #1 (EQU.DRA.1, EQU.PFT.1), the next name on #2.

The section restriction is what keeps current and non-current, assets and
liabilities, income and expenses apart: a line under "Non-current Liabilities"
can only land on a LIA.NCL account, a line under "Operating Expenses" only on an
expense-side account (plus the few explicit income/equity exceptions a rule
names, e.g. income tax, dividends, distributions, loss on disposal).

CLI
---
  python3 scripts/chart.py show  --type trust [--code 31600] [--report-code LIA.NCL]
  python3 scripts/chart.py find  --type company "loan from director"
  python3 scripts/chart.py codes --type partnership        # report codes and where they print
  python3 scripts/chart.py set-code --type trust --code 31600 --report-code LIA.NCL.LOA.SET
  python3 scripts/chart.py rename --type company --code 13900 --reporting-name "Loans to related parties"
  python3 scripts/chart.py add --type company --code 13950 --name "Loans - Directors (Div 7A)" \
        --account-type "Non-current Asset" --report-code ASS.NCA.REC
"""
from __future__ import annotations

import argparse
import csv
import math
import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent.parent
CHART_DIR = SKILL_DIR / "assets" / "chart-of-accounts"
CHART_FILES = {
    "company": "company.csv",
    "trust": "trust.csv",
    "partnership": "partnership.csv",
    "sole_trader": "sole-trader.csv",
}
ENTITY_ALIASES = {
    "company": "company", "pty ltd": "company", "pty": "company", "corporation": "company",
    "trust": "trust", "family trust": "trust", "discretionary trust": "trust", "unit trust": "trust",
    "partnership": "partnership", "partners": "partnership",
    "sole_trader": "sole_trader", "sole trader": "sole_trader", "soletrader": "sole_trader",
    "sole-trader": "sole_trader", "individual": "sole_trader", "proprietor": "sole_trader",
}
XERO_COLS = ["*Code", "Report Code", "*Name", "Reporting Name", "*Type", "*Tax Code", "Description",
             "Dashboard", "Expense Claims", "Enable Payments"]

STRONG = 0.80   # similarity good enough to post without a rule
WEAK = 0.45     # similarity good enough to post when no rule fired (flagged "similar")


def canonical_type(entity_type: str | None) -> str:
    t = (entity_type or "company").strip().lower()
    if t not in ENTITY_ALIASES:
        raise SystemExit(f"Unknown entity type '{entity_type}'. Use one of: company, trust, partnership, sole_trader.")
    return ENTITY_ALIASES[t]


def chart_path(entity_type: str) -> Path:
    return CHART_DIR / CHART_FILES[canonical_type(entity_type)]


# --------------------------------------------------------------------------- #
# name normalisation and similarity
# --------------------------------------------------------------------------- #
PHRASES: list[tuple[str, str]] = [
    (r"\(do not use\)", " "),
    (r"\(clea?ring\)|\(clrg\)", " clearing "),
    (r"&", " and "),
    (r"\baccounts?\s+receivable\b|\ba/r\b", " trade debtor receivable "),
    (r"\baccounts?\s+payable\b|\ba/p\b", " trade creditor payable "),
    (r"\bm\s*/\s*v\b|\bm\.v\.?|\bmv\b", " motor vehicle "),
    (r"\bacc(?:um(?:ulated)?)?\.?\s*dep(?:n|r|rec|reciation)?\b", " accumulated depreciation "),
    (r"\bacc(?:um(?:ulated)?)?\.?\s*amort(?:isation|ization)?\b", " accumulated amortisation "),
    (r"\bprov(?:\.|ision)?\s+for\b", " provision for "),
    (r"\bdepn\b|\bdepr\b", " depreciation "),
    (r"\bamort\b|amortization", " amortisation "),
    (r"\brego\b", " registration "),
    (r"\bsgc\b", " superannuation guarantee charge "),
    (r"\bsuper\b", " superannuation "),
    (r"\bpayg\s*-?\s*w\b|\bpaygw\b", " payg withholding "),
    (r"\bpayg\s*-?\s*i\b|\bpaygi\b", " payg instalment "),
    (r"install?ments?", " instalment "),
    (r"\br\s*and\s*m\b", " repairs maintenance "),
    (r"\bp\s*and\s*e\b", " plant equipment "),
    (r"\bmobiles?\b|\bphones?\b|\btel\b|\bph\b", " telephone "),
    (r"\bcogs\b", " cost goods sold "),
    (r"\bh\s*/?\s*p\b", " hire purchase "),
    (r"\bita\b", " income tax account "),
    (r"\bica\b", " integrated client account "),
    (r"\bwip\b", " work progress "),
    (r"\blsl\b", " long service leave "),
    (r"\bfbt\b", " fringe benefit tax "),
    (r"\bdiv(?:ision)?\s*7a\b", " division7a loan "),
    (r"\bupe\b", " unpaid present entitlement "),
    (r"\bpty\.?\s*ltd\.?|\blimited\b|\bltd\b", " company "),
    (r"\bw\s*/\s*c\b|workers'?\s*comp(?:ensation)?", " workcover "),
    (r"\brevenue\b", " income "),
    (r"\b(other|sundry) income\b", " miscellaneous income "),
    (r"\bpetrol\b|\bdiesel\b", " fuel "),
    (r"\bpower\b|\blight\b", " electricity "),
    (r"\bbookkeeping\b|\baccountancy\b", " accounting "),
    (r"\bsolicitors?\b|\blawyers?\b", " legal "),
    (r"\bstaff\b", " employee "),
    (r"\bcourier\b", " freight "),
    (r"\bmarketing\b|\bpromotions?\b", " advertising "),
    (r"\baccommodation\b|\bairfares?\b|\bflights?\b", " travel "),
    (r"\bsoftware\b", " computer "),
    (r"\brental\b", " rent "),
    (r"\bpenalt(?:y|ies)\b", " fine "),
]
STOPWORDS = {"the", "of", "and", "for", "on", "to", "in", "a", "an", "at", "cost", "less", "total", "with",
             "from", "by", "per", "as", "is", "exp", "expense", "account", "acc", "no", "nos", "fy", "net",
             "be", "or", "s", "ltd", "pty"}


def _stem(w: str) -> str:
    if len(w) <= 3 or w.endswith("ss"):
        return w
    if w.endswith("ies"):
        return w[:-3] + "y"
    if w.endswith("xes") or w.endswith("ches") or w.endswith("shes"):
        return w[:-2]
    if w.endswith("s"):
        return w[:-1]
    return w


def normalise(name: str) -> str:
    s = " " + (name or "").lower() + " "
    for pat, rep in PHRASES:
        s = re.sub(pat, rep, s)
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return " ".join(s.split())


def tokens(name: str) -> list[str]:
    out = []
    for w in normalise(name).split():
        if w.isdigit() or w in STOPWORDS:
            continue
        w = _stem(w)
        if w and w not in STOPWORDS:
            out.append(w)
    return out


# --------------------------------------------------------------------------- #
# the chart
# --------------------------------------------------------------------------- #
@dataclass
class ChartAccount:
    code: str
    name: str
    type: str
    report_code: str
    reporting_name: str = ""
    tax_code: str = ""
    description: str = ""
    toks: list[str] = field(default_factory=list)

    @property
    def system(self) -> bool:
        """Xero system / 'DO NOT USE' accounts are never chosen by the matcher."""
        return "do not use" in self.name.lower()

    def short(self) -> str:
        return f"{self.code} {self.name}"


class Chart:
    def __init__(self, path: str | Path, entity_type: str | None = None):
        self.path = Path(path)
        self.entity_type = entity_type
        self.header: list[str] = []
        self.rows: list[dict] = []
        self.accounts: list[ChartAccount] = []
        self._load()
        df = Counter(t for a in self.accounts for t in set(a.toks))
        n = len(self.accounts) or 1
        self.idf = {t: math.log((n + 1) / (c + 1)) + 1.0 for t, c in df.items()}
        self._default_idf = math.log(n + 1) + 1.0

    @classmethod
    def for_type(cls, entity_type: str) -> "Chart":
        et = canonical_type(entity_type)
        return cls(chart_path(et), et)

    def _load(self):
        with open(self.path, newline="", encoding="utf-8-sig") as fh:
            rdr = csv.DictReader(fh)
            self.header = [h for h in (rdr.fieldnames or []) if h is not None]
            for row in rdr:
                row = {(k or "").strip(): (v or "").strip() for k, v in row.items() if k is not None}
                self.rows.append(row)
                norm = {k.lstrip("*").strip().lower().replace(" ", "_"): v for k, v in row.items()}
                code = norm.get("code", "")
                name = norm.get("name", "")
                rcode = norm.get("report_code", "").upper()
                if not (code or name) or name.startswith("#"):
                    continue
                self.accounts.append(ChartAccount(
                    code=code, name=name, type=norm.get("type", ""), report_code=rcode,
                    reporting_name=norm.get("reporting_name", ""), tax_code=norm.get("tax_code", ""),
                    description=norm.get("description", ""), toks=tokens(name)))

    # ---- lookups -------------------------------------------------------- #
    def by_code(self, code: str) -> ChartAccount | None:
        return next((a for a in self.accounts if a.code == str(code).strip()), None)

    def in_family(self, families: list[str]) -> list[ChartAccount]:
        out = []
        for a in self.accounts:
            for f in families:
                if f.startswith("="):
                    if a.report_code == f[1:]:
                        out.append(a)
                        break
                elif a.report_code == f or a.report_code.startswith(f + "."):
                    out.append(a)
                    break
        return out

    def report_codes(self) -> list[str]:
        return sorted({a.report_code for a in self.accounts if a.report_code})

    # ---- similarity ------------------------------------------------------ #
    def similarity(self, a_toks: list[str], b_toks: list[str]) -> float:
        A, B = set(a_toks), set(b_toks)
        if not A or not B:
            return 0.0
        w = lambda t: self.idf.get(t, self._default_idf)
        inter = sum(w(t) for t in A & B)
        return 2 * inter / (sum(w(t) for t in A) + sum(w(t) for t in B))

    # ---- editing (set-code / rename / add) -------------------------------- #
    def _field(self, logical: str) -> str:
        for h in self.header:
            if h.lstrip("*").strip().lower().replace(" ", "_") == logical:
                return h
        raise SystemExit(f"Chart {self.path.name} has no '{logical}' column.")

    def set_report_code(self, code: str, report_code: str) -> tuple[str, str]:
        f_code, f_rc = self._field("code"), self._field("report_code")
        for row in self.rows:
            if row.get(f_code, "") == code:
                old = row.get(f_rc, "")
                row[f_rc] = report_code.upper()
                return old, row[f_rc]
        raise SystemExit(f"No account {code} in {self.path.name}.")

    def set_reporting_name(self, code: str, reporting_name: str) -> tuple[str, str]:
        f_code, f_rn = self._field("code"), self._field("reporting_name")
        for row in self.rows:
            if row.get(f_code, "") == code:
                old = row.get(f_rn, "")
                row[f_rn] = reporting_name
                return old, reporting_name
        raise SystemExit(f"No account {code} in {self.path.name}.")

    def add_account(self, code: str, name: str, acct_type: str, report_code: str, tax_code: str = "BAS Excluded"):
        if self.by_code(code):
            raise SystemExit(f"Account {code} already exists in {self.path.name}.")
        row = {h: "" for h in self.header}
        row[self._field("code")] = code
        row[self._field("name")] = name
        row[self._field("type")] = acct_type
        row[self._field("report_code")] = report_code.upper()
        try:
            row[self._field("tax_code")] = tax_code
        except SystemExit:
            pass
        for h in self.header:
            if h.strip() in ("Dashboard", "Expense Claims", "Enable Payments"):
                row[h] = "No"
        self.rows.append(row)

    def save(self):
        def key(r):
            c = r.get(self._field("code"), "")
            digits = "".join(ch for ch in c if ch.isdigit())
            return (int(digits) if digits else 0, c)
        self.rows.sort(key=key)
        with open(self.path, "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=self.header, extrasaction="ignore")
            w.writeheader()
            w.writerows(self.rows)


# --------------------------------------------------------------------------- #
# sections -> which report codes a line may land on
# --------------------------------------------------------------------------- #
# side: pl / bs.  nature: cr (income / liability / equity) or dr (expense / asset) - used for signs.
SECTIONS = {
    # profit and loss
    "trading income": ("pl", "income", "cr"), "income": ("pl", "income", "cr"), "revenue": ("pl", "income", "cr"),
    "sales": ("pl", "income", "cr"), "trading revenue": ("pl", "income", "cr"),
    "other income": ("pl", "other_income", "cr"), "plus other income": ("pl", "other_income", "cr"),
    "cost of sales": ("pl", "cos", "dr"), "less cost of sales": ("pl", "cos", "dr"),
    "cost of goods sold": ("pl", "cos", "dr"), "direct costs": ("pl", "cos", "dr"),
    "operating expenses": ("pl", "expenses", "dr"), "less operating expenses": ("pl", "expenses", "dr"),
    "expenses": ("pl", "expenses", "dr"), "overheads": ("pl", "expenses", "dr"),
    # balance sheet
    "assets": ("bs", "assets", "dr"), "bank": ("bs", "bank", "dr"), "cash": ("bs", "bank", "dr"),
    "current assets": ("bs", "current_assets", "dr"), "inventory": ("bs", "current_assets", "dr"),
    "prepayments": ("bs", "current_assets", "dr"),
    "fixed assets": ("bs", "fixed_assets", "dr"), "non-current assets": ("bs", "noncurrent_assets", "dr"),
    "non current assets": ("bs", "noncurrent_assets", "dr"),
    "liabilities": ("bs", "liabilities", "cr"), "current liabilities": ("bs", "current_liabilities", "cr"),
    "non-current liabilities": ("bs", "noncurrent_liabilities", "cr"),
    "non current liabilities": ("bs", "noncurrent_liabilities", "cr"),
    "equity": ("bs", "equity", "cr"),
}
# families a line in each section may be posted to (strict).  "=X" = exactly code X.
SECTION_FAMILIES = {
    "income": ["REV"], "other_income": ["REV", "EXP.FOR"], "cos": ["EXP.COS"],
    "expenses": ["EXP", "REV.OTH.CAP", "REV.OTH.INV", "EQU.RET.DIV"],
    "bank": ["ASS.CUR.CAS", "ASS.CUR.TER"],
    "current_assets": ["ASS.CUR"], "fixed_assets": ["ASS.NCA.FIX", "ASS.NCA.INT"],
    "noncurrent_assets": ["ASS.NCA"], "assets": ["ASS"],
    "current_liabilities": ["LIA.CUR"], "noncurrent_liabilities": ["LIA.NCL"], "liabilities": ["LIA"],
    "equity": ["EQU"],
    None: [],
}
# report code used when nothing else places the line (always flagged REVIEW)
SECTION_DEFAULT = {
    "income": "REV.TRA", "other_income": "REV.OTH", "cos": "EXP.COS", "expenses": "EXP.OPR",
    "bank": "ASS.CUR.CAS.BAN", "current_assets": "ASS.CUR.REC", "fixed_assets": "ASS.NCA.FIX.PLA",
    "noncurrent_assets": "ASS.NCA.REC", "assets": "ASS.CUR.REC",
    "current_liabilities": "LIA.CUR.PAY.TRA", "noncurrent_liabilities": "LIA.NCL.LOA.UNS",
    "liabilities": "LIA.CUR.PAY.TRA", "equity": "EQU",
}
CURRENT = {"bank", "current_assets", "current_liabilities"}
NONCURRENT = {"fixed_assets", "noncurrent_assets", "noncurrent_liabilities"}


def section_info(header: str | None) -> tuple[str | None, str | None, str | None]:
    """'Current Liabilities' -> ('bs', 'current_liabilities', 'cr')."""
    if not header:
        return None, None, None
    return SECTIONS.get(header.strip().lower().rstrip(":"), (None, None, None))


# --------------------------------------------------------------------------- #
# keyword rules (Xero report codes).  First match wins.
#   (regex, applies_to, code, why)   code may be a dict {"cur": .., "ncu": .., "prefer": name regex}
#   ("prefer" picks the usual account inside the family before name similarity does)
#   applies_to: asset / liability / equity / bs / income / cos / expense / pl, optionally limited to entity
#   types with '@' (e.g. "liability@trust", "asset@company|trust")
# --------------------------------------------------------------------------- #
ACC_RE = r"accum|less\b|provision for (depreciation|amortisation)|written down|impairment"
RULES: list[tuple[str, str, object, str]] = [
    # ---- assets ---------------------------------------------------------
    (r"cash (on|in|at) hand|petty cash|till|float|undeposited", "asset", "ASS.CUR.CAS", "cash on hand"),
    (r"term deposit|short.?term deposit|at.?call|bank guarantee|cash management", "asset",
     {"cur": "ASS.CUR.TER", "ncu": "ASS.NCA.INV.TER"}, "term deposit / guarantee"),
    (r"credit card|visa|master ?card|amex|american express|diners|bank ?card", "asset", "ASS.CUR.CAS.BAN", "card account in debit"),
    (r"\bbank\b|cheque|saving|saver|transaction|business (one |everyday |online )?acc|offset|everyday|trust account|"
     r"\*{2,}\s*\d|#\s?\d{3}|\bbsb\b|westpac|\banz\b|\bnab\b|national australia|commbank|\bcba\b|commonwealth|macquarie|"
     r"bankwest|st\.? ?george|\bing\b|bendigo|suncorp|hsbc|citi|ubank|up bank|judo|amp bank|paypal|stripe|square|"
     r"airwallex|\bwise\b|payoneer|cash reserve|maximiser|netbank", "asset",
     {"cur": "ASS.CUR.CAS.BAN", "ncu": "ASS.CUR.CAS.BAN", "prefer": r"^cheque account$"}, "bank account"),
    (r"(provision|allowance) for (doubtful|bad|impairment|expected credit)|doubtful debt", "asset", "ASS.CUR.REC.ADA", "doubtful debts"),
    (r"prepay|prepaid", "asset", "ASS.CUR.REC.PRE", "prepayment"),
    (r"work progress", "asset", "ASS.CUR.INY.WIP", "work in progress"),
    (r"inventor|stock on hand|trading stock|finished goods|raw material|\bstock\b", "asset", "ASS.CUR.INY", "inventory"),
    (r"\bgst\b|\bbas\b|\bato\b|integrated client|running balance|income tax (refund|receivable)|tax refund|fuel tax credit|"
     r"withholding|\btfn\b|franking credit|tax credits?\b|refundable", "asset", "ASS.CUR.REC", "tax refund due"),
    (r"trade debtor|debtor|receivable|unbilled|accrued income", "asset",
     {"cur": "ASS.CUR.REC.TRA", "ncu": "ASS.NCA.REC"}, "debtors / receivables"),
    (r"loan|division7a|advance|owing (from|by)|amount (due|owing)|related part|intercompany|unpaid present|beneficiar",
     "asset", {"cur": "ASS.CUR.LOA", "ncu": "ASS.NCA.REC"}, "loan receivable"),
    (r"deposit|bond", "asset", {"cur": "ASS.CUR.REC", "ncu": "ASS.NCA.INV.TER"}, "deposit / bond paid"),
    (r"owner|proprietor|drawings", "asset", {"cur": "ASS.CUR.LOA", "ncu": "ASS.NCA.REC"},
     "owner's current account held as an asset (Receivables, as the firm's Xero sole-trader pack shows)"),
    (r"goodwill", "asset", "ASS.NCA.INT.GOO", "goodwill"),
    (r"leasehold improvement|fit.?out|improvements? to leased", "asset", "ASS.NCA.FIX.LEA", "leasehold improvements"),
    (r"\bland\b", "asset", "ASS.NCA.FIX.LAC", "land"),
    (r"building|property|premises|strata|\bhouse\b|apartment|capital works", "asset", "ASS.NCA.FIX.BUC", "buildings"),
    (r"motor vehicle|vehicle|\bcar\b|\bute\b|truck|\bvan\b|trailer|forklift|motorcycle|hilux|ranger", "asset",
     "ASS.NCA.FIX.VEH", "motor vehicles"),
    (r"low value pool|\blvp\b|general pool|long life pool|project pool|small business pool|\bsbp\b|\bpool\b", "asset",
     "ASS.NCA.FIX.BUC", "depreciation pool"),
    (r"furniture|fixture|fitting|office equip|computer|laptop|hardware|it equip|plant|equipment|machinery|tool|caravan|"
     r"\bboat\b|signage|camera|fitout", "asset", "ASS.NCA.FIX.PLA", "plant and equipment"),
    (r"formation|establishment|incorporation|borrowing|loan cost|trade ?mark|patent|brand|website|domain|"
     r"intellectual property|licen[cs]e|franchise|intangible|research|exploration|debenture", "asset", "ASS.NCA.INT",
     "intangible / deferred cost"),
    (r"shares?\b|units? in|investment|managed fund|portfolio|crypto|bitcoin|securities|\bbonds?\b|\betf\b|listed",
     "asset", "ASS.NCA.INV.SHA", "investment"),
    (r"suspense|clearing|rounding|historical adjustment", "asset", "ASS.CUR", "suspense / clearing"),
    # ---- liabilities ----------------------------------------------------
    (r"unexpired|unearned interest|deferred interest|interest in suspense|unexpired charge", "liability",
     {"cur": "LIA.CUR.HPA.UEI", "ncu": "LIA.NCL.HPA.UEI"}, "unexpired HP interest"),
    (r"chattel", "liability", {"cur": "LIA.CUR.HPA", "ncu": "LIA.NCL.CHM"}, "chattel mortgage"),
    (r"hire purchase|car financ|vehicle financ|equipment financ|finance lease|lease liabilit|"
     r"(toyota|ford|mazda|holden|nissan|hyundai|kia|mitsubishi|bmw|mercedes|audi|volkswagen|isuzu|tesla|lexus|subaru)"
     r".*(finan|loan)|finan.*(hilux|ranger|truck|\bcar\b|vehicle)", "liability",
     {"cur": "LIA.CUR.HPA", "ncu": "LIA.NCL.HPA"}, "hire purchase / vehicle finance"),
    (r"\bgst\b|goods and services tax", "liability", "LIA.CUR.TAX.GST", "GST"),
    (r"no abn|no tfn", "liability", "LIA.CUR.TAX.INC.WTP", "no-ABN / no-TFN withholding"),
    (r"payg withholding|withholding (tax )?payable|payg payable", "liability", "LIA.CUR.PAY.PAY", "PAYG withholding"),
    (r"payg instalment|instalment payable", "liability", "LIA.CUR.PAY.PAY", "PAYG instalment"),
    (r"provision for (income )?tax", "liability", "LIA.CUR.PRO", "provision for income tax"),
    (r"income tax|tax payable|company tax", "liability", "LIA.CUR.TAX.INC", "income tax payable"),
    (r"fringe benefit tax|luxury car tax|wine equalisation|payroll tax", "liability", "LIA.CUR.TAX", "other tax"),
    (r"\bato\b|integrated client|running balance|tax office", "liability", "LIA.CUR.TAX",
     "ATO integrated client / running balance account (Taxation in the firm's Xero packs)"),
    (r"superannuation guarantee charge|\bsgc\b", "liability", "LIA.CUR.TAX", "superannuation guarantee charge (Taxation)"),
    (r"parental leave", "liability", {"cur": "LIA.CUR.PAY.EMP", "ncu": "LIA.CUR.PAY.EMP", "prefer": r"employee entitlements"},
     "paid parental leave payable (current Personnel-related items in the firm's Xero packs)"),
    (r"superannuation", "liability", "LIA.CUR.PAY.EMP", "superannuation payable"),
    (r"owner|proprietor|funds? (introduced|contributed)|drawings", "liability", "LIA.CUR.PAY.TRA",
     "owner's current account held as a liability - REVIEW: keep in Payables, or move to equity (EQU.ADV / EQU.DRA)"),
    (r"wage|salar|payroll|net pay|child support|employee (reimb|deduct)", "liability", "LIA.CUR.PAY.PAY", "wages payable"),
    (r"leave|employee (entitlement|benefit)|holiday pay|director'?s? fee", "liability",
     {"cur": "LIA.CUR.PAY.EMP", "ncu": "LIA.NCL.UDL.1"}, "employee entitlement"),
    (r"dividend", "liability", "LIA.CUR.PRO", "dividend payable"),
    (r"provision|deferred tax|make ?good|warranty", "liability", {"cur": "LIA.CUR.PRO", "ncu": "LIA.NCL.UDL.1"}, "provision"),
    (r"profit distribution|unpaid present|beneficiar.*(entitlement|distribution)|distribution.*payable|"
     r"trust distribution", "liability", {"cur": "LIA.CUR.PAY.TRA", "ncu": "LIA.NCL.PFT"}, "beneficiary entitlement (UPE)"),
    (r"beneficiar", "liability", {"cur": "LIA.CUR.PAY.TRA", "ncu": "LIA.NCL.LOA.SET"}, "beneficiary loan"),
    (r"director|shareholder|\bpartner", "liability", {"cur": "LIA.CUR.PAY.TRA", "ncu": "LIA.NCL.LOA.DIR"},
     "director / shareholder loan"),
    (r"\bloans?\b(?!.*\b(bank|banking|finance|financial|credit|secured|mortgage|westpac|anz|nab|cba|commonwealth|"
     r"macquarie|bankwest|bendigo|suncorp|ing|amp|judo|prospa|moula|capify|ato)\b)", "liability@trust",
     {"cur": "LIA.CUR.PAY.TRA", "ncu": "LIA.NCL.REL", "prefer": r"beneficiary loans"},
     "loan from a person or related party (Payables > Current in the firm's trust packs)"),
    (r"related part|associat|family trust|\btrust\b|company|intercompany|unitholder", "liability",
     {"cur": "LIA.CUR.LOA", "ncu": "LIA.NCL.REL"}, "related-party loan"),
    (r"mortgage", "liability", {"cur": "LIA.CUR.LOA", "ncu": "LIA.NCL.CHM"}, "mortgage"),
    (r"credit card|visa|master ?card|amex|american express|diners|bank ?card", "liability", "LIA.CUR.LOA", "credit card"),
    (r"bank|overdraft|line of credit|business loan|westpac|\banz\b|\bnab\b|\bcba\b|commonwealth|macquarie|bankwest|"
     r"st\.? ?george|bendigo|suncorp", "liability", {"cur": "LIA.CUR.LOA", "ncu": "LIA.NCL.LOA"}, "bank loan / overdraft"),
    (r"loan|borrowing|debenture|note payable|financ", "liability",
     {"cur": "LIA.CUR.LOA", "ncu": "LIA.NCL.LOA.UNS", "prefer": r"unsecured"}, "loan"),
    (r"trade creditor|creditor|payable|accrued|accrual|income in advance|unearned|in advance|deposits? (held|received)|"
     r"customer deposit|client (control|monies|funds)|trust (monies|liabilit)|revenue owned|bond held",
     "liability", {"cur": "LIA.CUR.PAY.TRA", "ncu": "LIA.NCA.PAY"}, "trade / other payable"),
    (r"suspense|clearing|rounding|historical|tracking", "liability", "LIA.CUR", "suspense / clearing"),
    # ---- equity ---------------------------------------------------------
    (r"dividend", "equity", "EQU.RET.DIV", "dividends paid"),
    (r"current year (earning|profit)|profit for the year|income for the year|profit \(?loss\)? earned|this year", "equity",
     "EQU.RET.CUR", "current year earnings"),
    (r"retained|accumulated (loss|profit|fund)|undistributed|prior year", "equity", "EQU.RET", "retained profits"),
    (r"share capital|issued capital|paid.?up|ordinary share|contributed equity|shares? issued|capital shares?", "equity",
     "EQU.SHA", "share capital"),
    (r"revaluation", "equity", "EQU.RES.REV", "revaluation reserve"),
    (r"reserve", "equity", "EQU.RES", "reserve"),
    (r"settle|settlor|unit capital|units? (issued|on issue)|trust capital|corpus", "equity", "EQU.SET", "settled sum / units"),
    (r"drawing", "equity", "EQU.DRA", "drawings"),
    (r"share of (net )?(profit|loss|income)|profit share|current year (share|allocation)|profit distribution|"
     r"distribution of (net )?(profit|income)|(profit|income) (allocation|allocated|distributed)", "equity", "EQU.PFT",
     "share of profit (partner / beneficiary account)"),
    (r"opening|capital|funds? (introduced|contributed)|owner|proprietor|partner|beneficiar|contribution", "equity",
     "EQU.ADV", "capital / funds introduced"),
    # ---- profit and loss: income ----------------------------------------
    (r"interest", "income", "REV.INV.INT", "interest received"),
    (r"dividend|franked|franking|distribution|managed fund", "income", "REV.INV.DIV", "dividends / distributions received"),
    (r"\brent\b|lease income|outgoings recover", "income", "REV.INV.REN", "rent received"),
    (r"government|grant|subsid|jobkeeper|cash ?flow boost|apprentice|stimulus|covid|support payment", "income",
     "REV.GRA.GOV", "government grant / subsidy"),
    (r"(gain|profit|loss) on (sale|disposal)|disposal of", "income", "REV.OTH.CAP", "gain / loss on disposal"),
    (r"(realised|unrealised|foreign|fx|currency|exchange).*(gain|loss)|forex|revaluation", "income",
     {"cur": "REV.OTH", "ncu": "REV.OTH", "prefer": r"^miscellaneous income$"},
     "currency gain shown under income in the workpaper (Other Income, as the firm's Xero packs print it)"),
    (r"fringe benefit tax|employee contribution", "income", "REV.OTH", "FBT employee contribution"),
    (r"other income|sundry|miscellaneous", "income", {"cur": "REV.OTH", "ncu": "REV.OTH", "prefer": r"^miscellaneous income$"},
     "other income"),
    (r"refund|rebate|recover|reimburs|insurance|fuel tax credit|override|discounts? received|rounding", "income",
     "REV.OTH", "other income"),
    # trading income only applies under a trading income header (see Matcher)
    (r".", "trading", ["REV.TRA", "=REV"], "trading income"),
    # ---- profit and loss: cost of sales -----------------------------------
    (r"opening (stock|inventor)", "cos", "EXP.COS.OPE", "opening stock"),
    (r"closing (stock|inventor)|stock (adjust|movement)", "cos", "EXP.COS.CLO", "closing stock"),
    (r".", "cos", "EXP.COS", "cost of sales"),
    # ---- profit and loss: expenses ---------------------------------------
    (r"income tax|company tax|tax expense", "expense", "EXP.INC", "income tax expense"),
    (r"dividend", "expense", ["EQU.RET.DIV", "EXP.DIV"], "dividends paid"),
    (r"distribution|share of (net )?(profit|income)|beneficiar|partner", "expense", "EXP.DIS", "distribution"),
    (r"(carry|carried) forward loss|loss(es)? (applied|recouped)|prior year loss", "expense", ["EXP.CFL", "EXP.UDE"],
     "losses applied"),
    (r"(gain|profit|loss) on (sale|disposal)|disposal of", "expense", "REV.OTH.CAP", "gain / loss on disposal"),
    (r"(realised|unrealised|foreign|fx|currency|exchange).*(gain|loss)|forex|bank revaluation", "expense", "EXP.FOR",
     "currency gain / loss"),
    (r"amortisation", "expense", "EXP.AMO", "amortisation"),
    (r"depreciation|write.?off|instant asset", "expense", "EXP.DEP", "depreciation"),
    (r"interest|borrowing|finance charge|loan fee", "expense", "EXP.INT", "interest / borrowing costs"),
    (r"superannuation", "expense", "EXP.EMP.SUP", "superannuation"),
    (r"wage|salar|payroll|allowance", "expense", "EXP.EMP.WAG", "wages"),
    (r"director'?s? fee", "expense", "EXP.EMP.SHA", "directors fees"),
    (r"workcover|employee|contractor|subcontract|training|uniform|amenit|recruit|leave|holiday pay|labour hire", "expense",
     "EXP.EMP", "employment costs"),
    (r"motor vehicle|fuel|toll|parking|registration|\bcar\b|vehicle|tyre", "expense", "EXP.VEH", "motor vehicle"),
    (r"advertising|sponsor|social media|website", "expense", "EXP.ADV", "advertising"),
    (r"entertain|meal|gifts? to client", "expense", "EXP.ENT", "entertainment"),
    (r"international travel|overseas travel|travel.*international", "expense", "EXP.TRA.INT", "travel - international"),
    (r"travel", "expense", "EXP.TRA.NAT", "travel"),
    (r"bad debt|doubtful debt|debt collection", "expense", "EXP.BAD", "bad debts"),
    (r"insurance", "expense", "EXP.INS", "insurance"),
    (r"\bfine\b|non.?deduct", "expense", "EXP.NON", "fines / non-deductible"),
    (r"donation|charit|gift", "expense", "EXP.UDE", "donations"),
    (r"\brent\b|lease of premises|occupancy|outgoing|council rate|land tax|strata|body corporate", "expense",
     ["EXP.OCC", "EXP.REN", "EXP.OPR"], "occupancy"),
    (r"repair|maintenance", "expense", ["EXP.REP", "EXP.OPR"], "repairs and maintenance"),
    (r"accounting|legal|bank (fee|charge)|filing|asic|subscription|membership|computer|internet|telephone|postage|"
     r"printing|stationery|office|professional|consult|secretarial|audit", "expense", "EXP.ADM", "administration"),
    (r".", "expense", "EXP.OPR", "operating expense"),
]


# The account a bookkeeper would normally use for a code when nothing in the name points elsewhere.
DEFAULT_HINTS = {
    "ASS.CUR.CAS.BAN": r"^cheque account$", "ASS.CUR.CAS": r"cash on hand", "ASS.CUR.REC.TRA": r"trade debtors",
    "ASS.CUR.LOA": r"related party", "ASS.CUR.TER": r"^term deposits", "ASS.NCA.INT": r"formation",
    "ASS.NCA.INV.SHA": r"^shares in listed", "ASS.NCA.FIX.PLA": r"^plant and equipment at cost",
    "ASS.NCA.FIX.PLA.ACC": r"plant and equipment|p&e", "ASS.NCA.FIX.BUC": r"^buildings at cost",
    "LIA.CUR.PAY.TRA": r"^trade creditors", "LIA.CUR.LOA": r"loans - bank", "LIA.CUR.PAY.EMP": r"superannuation payable",
    "LIA.CUR.PAY.PAY": r"payg withholding payable", "LIA.CUR.TAX.INC": r"^income tax payable$",
    "LIA.NCL.LOA": r"bank", "LIA.NCL.UDL.1": r"aggregate employee", "LIA.CUR.PRO": r"^provisions$",
    "LIA.CUR.TAX": r"^tax liabilities$|corporate taxes", "ASS.CUR.REC": r"bills receivable",
    "EQU.RET": r"retained", "EQU.ADV": r"funds contributed", "EQU.DRA": r"^drawings$",
    "REV": r"sale", "REV.TRA.GOO": r"^sales? of retail", "REV.OTH": r"miscellaneous income",
    "REV.INV.INT": r"^interest received$", "EXP.COS": r"^purchase", "EXP.OPR": r"sundry expense",
    "EXP.ADM": r"office supplies", "EXP.INT": r"interest on bank loans", "EXP.DEP": r"plant and equipment",
    "EXP.EMP": r"contractors", "EXP.EMP.WAG": r"wages - (employees|staff)", "EXP.EMP.SUP": r"employees",
    "EXP.VEH": r"other vehicle", "EXP.INS": r"insurance", "EXP.DIS": r"^distribution to (beneficiaries|partner)$",
}


# layout codes a rule may use when the chart has no account carrying them, whose presentation is
# settled in the firm's Xero packs - no REVIEW needed (e.g. a non-current Division 7A loan receivable)
SETTLED_LAYOUT_CODES = {"ASS.NCA.REC", "EQU.PFT", "EQU.DRA", "EQU.ADV", "LIA.NCA.PAY", "LIA.NCL.PFT", "EXP.CFL", "EXP.DIS"}


# per-person equity families (Funds contributed / Drawings / Share of profits - Partner or Beneficiary n)
PERSON_FAMILY = re.compile(r"^(EQU\.(?:ADV|DRA|PFT|CAP))(?:\.(\d+))?$")
PERSON_GENERIC = re.compile(r"^(#?\s*\d+|partner|beneficiary|beneficiaries|owner|proprietor|opening balance|"
                            r"funds (contributed|introduced)|drawings|share of (net )?profits?|capital|current account)$", re.I)


def person_key(name: str) -> str | None:
    """'Drawings - Jane Citizen' -> 'jane citizen'; None when the name carries no person."""
    parts = [x.strip() for x in re.split(r"\s+[-\u2013:]\s+", name or "") if x.strip()]
    for cand in reversed(parts[1:]):
        cand = re.sub(r"^(partner|beneficiary|owner|proprietor)\s*", "", cand, flags=re.I).strip(" #")
        if cand and not PERSON_GENERIC.match(cand):
            return " ".join(cand.lower().split())
    return None


@dataclass
class Match:
    account: ChartAccount | None
    report_code: str
    confidence: str          # override / chart-exact / chart / rule / similar / type / none
    reason: str
    score: float = 0.0
    force_review: bool = False

    @property
    def review(self) -> bool:
        if self.force_review or self.confidence in ("type", "none"):
            return True
        return self.confidence == "rule" and self.account is None and \
            re.sub(r"\.\d+$", "", self.report_code) not in SETTLED_LAYOUT_CODES


class Matcher:
    """Posts workpaper lines to chart accounts.  Stateless apart from the previous BS line
    (accumulated depreciation lines inherit the class of the cost line above them)."""

    def __init__(self, chart: Chart, entity_type: str | None = None):
        self.chart = chart
        self.etype = entity_type
        self.prev_bs_code: str | None = None
        self.person_no: dict[str, int] = {}     # person name -> Partner / Beneficiary number, first come first numbered

    @staticmethod
    def _cls(section: str | None) -> str | None:
        if section in ("bank", "current_assets", "fixed_assets", "noncurrent_assets", "assets"):
            return "asset"
        if section in ("current_liabilities", "noncurrent_liabilities", "liabilities"):
            return "liability"
        if section == "equity":
            return "equity"
        if section in ("income", "other_income"):
            return "income"
        if section == "cos":
            return "cos"
        if section == "expenses":
            return "expense"
        return None

    def _allowed(self, side: str | None, section: str | None) -> list[ChartAccount]:
        fams = SECTION_FAMILIES.get(section) or (["REV", "EXP", "EQU.RET.DIV"] if side == "pl"
                                                 else ["ASS", "LIA", "EQU"] if side == "bs" else [])
        return [a for a in self.chart.in_family(fams) if not a.system] if fams else \
            [a for a in self.chart.accounts if not a.system]

    def _best(self, toks: list[str], cands: list[ChartAccount], prefer_code: str | None = None) -> tuple[ChartAccount | None, float]:
        """Most similar account.  Words that most candidates share (e.g. 'motor vehicle' inside the
        motor-vehicle family) say nothing about which one fits, so they are ignored when ranking a
        family.  Ties: the family's own code, then the usual default account for that code
        (DEFAULT_HINTS, e.g. 'Cheque account' for bank lines), then chart order."""
        common: set[str] = set()
        if len(cands) >= 3:
            cnt = Counter(t for a in cands for t in set(a.toks))
            common = {t for t, c in cnt.items() if c > len(cands) / 2}
        lt = [t for t in toks if t not in common]
        best, best_key = None, None
        for i, a in enumerate(cands):
            s = self.chart.similarity(lt, [t for t in a.toks if t not in common]) if lt else 0.0
            hint = DEFAULT_HINTS.get(a.report_code)
            key = (round(s, 6), 1 if prefer_code and a.report_code == prefer_code else 0,
                   1 if hint and re.search(hint, a.name.lower()) else 0, -i)
            if best_key is None or key > best_key:
                best, best_key = a, key
        return best, (best_key[0] if best_key else 0.0)

    def _rule(self, lname: str, cls: str | None, section: str | None, side: str | None):
        for pat, applies, code, why in RULES:
            applies, _, only = applies.partition("@")
            if only and self.etype not in only.split("|"):
                continue
            if applies == "trading":
                if section != "income":
                    continue
            elif applies in ("asset", "liability", "equity", "income", "cos", "expense"):
                if cls is None:
                    # unknown section: only rules on the right statement side
                    want = ("asset", "liability", "equity") if side == "bs" else ("income", "cos", "expense")
                    if side and applies not in want:
                        continue
                    if applies == "cos":
                        continue
                elif applies != cls:
                    # 'income' rules also serve 'other_income'
                    continue
            if not re.search(pat, lname):
                continue
            prefer = None
            if isinstance(code, dict):
                prefer = code.get("prefer")
                code = code["ncu"] if section in NONCURRENT else code["cur"]
            return (code if isinstance(code, list) else [code]), why, pat == ".", prefer
        return None, None, False, None

    def match(self, name: str, section_header: str | None, line_code: str | None = None,
              side_hint: str | None = None) -> Match:
        side, section, _nature = section_info(section_header)
        side = side or side_hint
        cls = self._cls(section)
        if side == "pl" and cls is None:
            cls = "income" if re.search(r"received|income|revenue|earned|sales", (name or "").lower()) else "expense"
        lname = normalise(name)
        toks = tokens(name)
        allowed = self._allowed(side, section)

        # accumulated depreciation / amortisation with no class word -> inherit the cost line's family
        if side == "bs" and re.search(ACC_RE, lname) and self.prev_bs_code and self.prev_bs_code.startswith("ASS.NCA") \
                and not re.search(r"vehicle|plant|equipment|furniture|fitting|office|computer|building|land|leasehold|"
                                  r"goodwill|formation|pool|borrowing|property|improvement", lname):
            fam = re.sub(r"\.(ACC|IMP)$", "", self.prev_bs_code)
            cands = [a for a in allowed if a.report_code.startswith(fam)]
            acc = [a for a in cands if re.search(r"accum|less", a.name.lower())] or cands
            a, s = self._best(toks, acc)
            m = Match(a, a.report_code if a else fam, "rule",
                      f"accumulated depreciation/amortisation of the line above ({fam})", s)
            self.prev_bs_code = m.report_code
            return m

        # 2. exact name match inside the section
        best, score = self._best(toks, allowed)
        fams, why, catch_all, prefer = self._rule(lname, cls, section, side)
        # the workpaper may carry the firm chart's own account code (client file set up from the template)
        coded = self.chart.by_code(str(line_code).strip()) if line_code else None
        if coded is not None and (coded.system or coded not in allowed):
            coded = None
        code_sim = self.chart.similarity(toks, coded.toks) if coded is not None else 0.0
        code_fits = coded is not None and (code_sim >= WEAK or bool(
            fams and not catch_all and any(coded.report_code.startswith(f.lstrip("=")) for f in fams)))
        if best is not None and toks and set(toks) == set(best.toks):
            m = Match(best, best.report_code, "chart-exact", f"chart account {best.short()}", 1.0)
        elif code_fits:
            m = Match(coded, coded.report_code, "chart-code", f"same code as chart account {coded.short()}", code_sim)
        else:
            m = None

            def by_rule():
                fam_cands = [a for a in self.chart.in_family(fams) if not a.system]
                # a rule may name an exception family outside the strict section list (e.g. income tax)
                in_section = [a for a in fam_cands if a in allowed] or fam_cands
                if prefer:
                    # the rule names the usual account inside the family (e.g. parental leave -> Employee
                    # entitlements), unless the line's own name clearly fits another account
                    a0, s0 = self._best(toks, in_section, prefer_code=fams[0].lstrip("="))
                    if s0 < STRONG:
                        in_section = [a for a in in_section if re.search(prefer, a.name.lower())] or in_section
                flag = "REVIEW" in why
                if in_section:
                    a, s_ = self._best(toks, in_section, prefer_code=fams[0].lstrip("="))
                    return Match(a, a.report_code, "rule", f"{why} -> {a.short()}", s_, force_review=flag)
                code = fams[0].lstrip("=")
                return Match(None, code, "rule", f"{why} -> {code} (no such account in the chart - layout code)", 0.0,
                             force_review=flag)

            # 3. a specific keyword rule
            if fams and not catch_all:
                m = by_rule()
            # 4. strong name similarity
            elif best is not None and score >= STRONG:
                m = Match(best, best.report_code, "chart", f"chart account {best.short()} (similarity {score:.2f})", score)
            # 5. catch-all rule for the section (all trading income / cost of sales / expenses)
            elif fams:
                m = by_rule()
            # 6. moderate similarity
            elif best is not None and score >= WEAK:
                m = Match(best, best.report_code, "similar", f"closest chart account {best.short()} (similarity {score:.2f})", score)
            # 7. section default
            else:
                code = SECTION_DEFAULT.get(section) or ("EXP.OPR" if side == "pl" else "")
                cands = self.chart.in_family(["=" + code]) if code else []
                a = cands[0] if cands else None
                m = Match(a, code, "type" if code else "none",
                          f"no rule or similar account - section default {code}" if code else "no section, no rule", 0.0)
        m = self._number_person(m, name)
        if side == "bs":
            self.prev_bs_code = m.report_code
        return m

    def _number_person(self, m: Match, name: str) -> Match:
        """Per-person equity accounts: the same person keeps the same number across Funds contributed,
        Drawings and Share of profits (Partner / Beneficiary 1, 2, ...), numbered in workpaper order."""
        fam = PERSON_FAMILY.match(m.report_code or "")
        key = person_key(name)
        if not fam or not key:
            return m
        base, n = fam.group(1), fam.group(2)
        if m.confidence in ("chart-exact", "chart-code", "override") and n:
            self.person_no.setdefault(key, int(n))
            return m
        no = self.person_no.setdefault(key, max(self.person_no.values(), default=0) + 1)
        if n and int(n) == no:
            return m
        why = re.sub(r" -> .*$", "", m.reason)
        cands = [a for a in self.chart.in_family(["=" + f"{base}.{no}"]) if not a.system]
        if cands:
            # e.g. partner #2 has both 'Opening balance' and 'Funds contributed' under EQU.ADV.2 - match the name
            a = self._best(tokens(name), cands)[0]
            return Match(a, a.report_code, m.confidence, f"{why}; person #{no} -> {a.short()}", m.score, m.force_review)
        return Match(None, f"{base}.{no}", m.confidence, f"{why}; person #{no} -> {base}.{no} (no chart account - layout code)",
                     m.score, m.force_review)


# --------------------------------------------------------------------------- #
# entity type detection
# --------------------------------------------------------------------------- #
def detect_entity_type(name: str, equity_names: list[str] | None = None) -> tuple[str, str]:
    n = (name or "").lower()
    eq = " | ".join(equity_names or []).lower()
    if re.search(r"super(annuation)? fund|\bsmsf\b", n):
        return "smsf", "name says superannuation fund - out of scope for this skill"
    if re.search(r"\batf\b|as trustee for|trustee for|\btrust\b", n):
        return "trust", "name refers to a trust / trustee"
    if re.search(r"partnership|\bpartners\b", n):
        return "partnership", "name says partnership"
    if re.search(r"\bpty\b|\bltd\b|limited|incorporated", n):
        return "company", "name is a company name"
    if re.search(r"partner", eq):
        return "partnership", "equity has partner accounts"
    if re.search(r"settle|beneficiar|unit capital", eq):
        return "trust", "equity has settled sum / beneficiary accounts"
    if re.search(r"share capital|issued capital", eq):
        return "company", "equity has share capital"
    if re.search(r"drawing|proprietor|owner|funds introduced", eq):
        return "sole_trader", "equity has drawings / owner's funds without partners"
    if re.search(r"\s&\s|\band\b", n):
        return "partnership", "two names joined by '&' / 'and' (check)"
    if re.search(r",", n) or len(n.split()) <= 3:
        return "sole_trader", "looks like an individual's name (check)"
    return "company", "default"


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _print_accounts(accts: list[ChartAccount]):
    print(f"{'Code':7} {'Report code':20} {'Type':22} Name")
    for a in accts:
        rn = f"  [prints as: {a.reporting_name}]" if a.reporting_name else ""
        print(f"{a.code:7} {a.report_code:20} {a.type:22} {a.name}{rn}")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for c in ("show", "find", "codes", "set-code", "rename", "add"):
        p = sub.add_parser(c)
        p.add_argument("--type", required=True, help="company | trust | partnership | sole_trader")
        p.add_argument("--chart", help="explicit chart CSV (default: the bundled chart for --type)")
        if c == "show":
            p.add_argument("--code")
            p.add_argument("--report-code")
        if c == "find":
            p.add_argument("text")
            p.add_argument("--section", help="workpaper section header, e.g. 'Current Liabilities'")
        if c in ("set-code", "rename"):
            p.add_argument("--code", required=True)
        if c == "set-code":
            p.add_argument("--report-code", required=True)
        if c == "rename":
            p.add_argument("--reporting-name", required=True)
        if c == "add":
            p.add_argument("--code", required=True)
            p.add_argument("--name", required=True)
            p.add_argument("--account-type", required=True)
            p.add_argument("--report-code", required=True)
            p.add_argument("--tax-code", default="BAS Excluded")
    args = ap.parse_args(argv)
    et = canonical_type(args.type)
    chart = Chart(args.chart, et) if args.chart else Chart.for_type(et)

    if args.cmd == "show":
        accts = chart.accounts
        if args.code:
            accts = [a for a in accts if a.code == args.code]
        if args.report_code:
            rc = args.report_code.upper()
            accts = [a for a in accts if a.report_code == rc or a.report_code.startswith(rc + ".")]
        _print_accounts(accts)
    elif args.cmd == "find":
        m = Matcher(chart, et).match(args.text, args.section)
        print(f"{args.text!r} under {args.section or '(no section)'}  ->  "
              f"{m.account.short() if m.account else '(no chart account)'}  {m.report_code}  [{m.confidence}] {m.reason}")
        toks = tokens(args.text)
        ranked = sorted(chart.accounts, key=lambda a: -chart.similarity(toks, a.toks))[:8]
        print("\nClosest chart accounts by name:")
        for a in ranked:
            print(f"  {chart.similarity(toks, a.toks):.2f}  {a.code:7} {a.report_code:20} {a.name}")
    elif args.cmd == "codes":
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from fr_engine import describe_code, load_map  # noqa: E402
        rmap = load_map()
        counts = Counter(a.report_code for a in chart.accounts)
        print(f"{'Report code':20} {'#':>3}  Prints as")
        for rc in chart.report_codes():
            print(f"{rc:20} {counts[rc]:>3}  {describe_code(rc, '', rmap)}")
    elif args.cmd == "set-code":
        old, new = chart.set_report_code(args.code, args.report_code)
        chart.save()
        print(f"{chart.path.name}: account {args.code} report code {old or '(blank)'} -> {new}")
    elif args.cmd == "rename":
        old, new = chart.set_reporting_name(args.code, args.reporting_name)
        chart.save()
        print(f"{chart.path.name}: account {args.code} reporting name {old or '(blank)'} -> {new}")
    elif args.cmd == "add":
        chart.add_account(args.code, args.name, args.account_type, args.report_code, args.tax_code)
        chart.save()
        print(f"{chart.path.name}: added {args.code} {args.name} ({args.account_type}, {args.report_code.upper()})")


if __name__ == "__main__":
    try:
        main()
    except BrokenPipeError:      # output piped into head / less
        pass
