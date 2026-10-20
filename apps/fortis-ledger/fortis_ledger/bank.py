"""
Bank statements and cash coding - the way Xero's cash coding works.

* A bank statement - the CSV (or Excel) file a bank's internet banking exports - is imported into a bank
  account in the chart.  Its lines are kept as the bank sent them, unreconciled.  The importer finds the
  columns itself (date, amount or spent / received, payee, description, reference, balance) for the common
  Australian banks and Xero's own statement template, and a person checks them on screen before anything
  is saved.  Lines already imported (from an overlapping statement) are skipped.
* Cash coding gives each line an account - suggested from how the same payee was coded before - and, for a
  client registered for GST, a tax rate.  Reconciling posts the line as a bank transaction: the bank
  account against the coded account, with the GST split out to the GST account.  Unreconciling takes it
  out of the ledger again and the line goes back to be coded.
* A transfer between two accounts that both have statements is posted once: when the second side is coded
  to the first account, it is matched to the transaction already posted there instead of posting again.

Storage: clients/<slug>/bank/<account code>.json holds one bank account's statement lines, how each was
coded and the imports they came from.  Bank transactions are numbered B2026-0001 ... per financial year,
and the ledger reads them from these files - a year of bank lines is one file, not hundreds of journals.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import re
import uuid
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from decimal import Decimal

from .model import Journal, Line, now_iso
from .money import ZERO, as_str, q2, to_dec
from .store import read_json, write_json

# Xero's Australian tax rates: the GST share of a GST-inclusive amount (10% = 1/11)
TAX_RATES: dict[str, int] = {
    "GST on Expenses": 10, "GST on Income": 10, "GST on Capital": 10,
    "GST Free Expenses": 0, "GST Free Income": 0, "GST Free Capital": 0,
    "Input Taxed": 0, "BAS Excluded": 0,
}
# what a person is offered, in this order
TAX_CHOICES = ["GST on Income", "GST Free Income", "GST on Expenses", "GST Free Expenses", "GST on Capital",
               "GST Free Capital", "Input Taxed", "BAS Excluded"]
NUMBER_RE = re.compile(r"^B(\d{4})-(\d{4,})$")
MATCH_DAYS = 7            # the two sides of a transfer between accounts are matched within a week
LINE_ID_RE = re.compile(r"^l[0-9a-f]{10}$")


class BankError(Exception):
    pass


# ---------------------------------------------------------------------------------------- #
# reading a statement file
# ---------------------------------------------------------------------------------------- #
ROLES = {"date": "Date", "amount": "Amount (+ received, - spent)", "spent": "Spent (debit)",
         "received": "Received (credit)", "payee": "Payee", "description": "Description",
         "reference": "Reference", "balance": "Balance"}

HEAD = {
    "date": {"date", "transaction date", "trans date", "txn date", "posted date", "posting date", "date posted",
             "effective date", "value date", "processed date", "transactiondate", "booking date"},
    "amount": {"amount", "transaction amount", "amount (aud)", "amount aud", "aud amount", "net amount",
               "value", "amt"},
    "spent": {"debit", "debits", "debit amount", "debit (aud)", "withdrawal", "withdrawals", "withdrawal amount",
              "money out", "paid out", "spent", "dr", "debit amt"},
    "received": {"credit", "credits", "credit amount", "credit (aud)", "deposit", "deposits", "deposit amount",
                 "money in", "paid in", "received", "cr", "credit amt"},
    "payee": {"payee", "payee name", "merchant", "merchant name", "name", "counterparty", "other party",
              "beneficiary", "to/from", "party"},
    "description": {"description", "transaction description", "narrative", "narration", "transaction details",
                    "details", "memo", "particulars", "long description", "description 1", "transaction"},
    "reference": {"reference", "ref", "reference number", "cheque", "cheque number", "cheque no", "chq no",
                  "check number", "serial", "receipt number", "transaction id"},
    "balance": {"balance", "running balance", "closing balance", "account balance", "balance (aud)"},
}


@dataclass
class Layout:
    """Where things are in a statement file.  Column numbers start at 0; header_row -1 means no titles."""
    header_row: int = -1
    cols: dict = field(default_factory=dict)        # role -> column
    date_order: str = "dmy"                         # dmy (Australian), mdy or ymd
    flip: bool = False                              # the file shows money spent as positive amounts

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict | None) -> "Layout":
        d = d or {}
        cols = {k: int(v) for k, v in (d.get("cols") or {}).items() if k in ROLES and v not in (None, "")}
        return cls(int(d.get("header_row", -1)), cols, d.get("date_order") or "dmy", bool(d.get("flip")))


def _norm(h) -> str:
    return " ".join(str(h or "").replace("*", "").replace("_", " ").strip().lower().split())


def _text(v) -> str:
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    return " ".join(str(v).split())[:300]


def _cell(row: list, i):
    if i is None or i < 0 or i >= len(row):
        return None
    return row[i]


def parse_money(v) -> Decimal | None:
    """Bank amounts: '1,234.56', '-55.00', '+12.30', '(55.00)', '55.00 DR', '12.30CR', '$55'.  Blank -> None."""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float, Decimal)):
        return to_dec(v)
    s = str(v).strip().replace("−", "-").replace(" ", "")
    if not s:
        return None
    sign = 1
    m = re.search(r"(CR|DR)\.?$", s, re.I)
    if m:
        sign = -1 if m.group(1).upper() == "DR" else 1
        s = s[:m.start()].strip()
    s = re.sub(r"(?i)^(aud|a\$)\s*|\s*(aud)$", "", s).lstrip("+").strip()
    if s in ("", "-"):
        return None
    if not re.fullmatch(r"[-(]?\$?[\d,]*\.?\d+\)?-?", s.replace(" ", "")):
        return None
    try:
        return to_dec(s) * sign
    except ValueError:
        return None


_MONTH_FORMATS = ("%d %b %Y", "%d %B %Y", "%d-%b-%Y", "%d-%b-%y", "%d %b %y", "%d/%b/%Y", "%d/%b/%y",
                  "%b %d, %Y", "%B %d, %Y", "%b %d %Y", "%d-%B-%Y")


def parse_bank_date(v, order: str = "dmy") -> dt.date | None:
    if v is None or v == "":
        return None
    if isinstance(v, dt.datetime):
        return v.date()
    if isinstance(v, dt.date):
        return v
    s = _text(v)
    s = re.sub(r"(?i)[ T]\d{1,2}:\d{2}(:\d{2})?(\.\d+)?\s*([ap]m)?(Z|[+-]\d{2}:?\d{2})?$", "", s).strip()
    if re.fullmatch(r"\d{8}", s):
        try:
            return dt.datetime.strptime(s, "%Y%m%d").date()
        except ValueError:
            return None
    m = re.fullmatch(r"(\d{1,4})[/.\-](\d{1,2})[/.\-](\d{1,4})", s)
    if m:
        a, b, c = m.groups()
        if len(a) == 4:
            y, mo, d = int(a), int(b), int(c)
        elif order == "mdy":
            mo, d, y = int(a), int(b), int(c)
        else:
            d, mo, y = int(a), int(b), int(c)
        if y < 100:
            y += 2000 if y < 70 else 1900
        try:
            return dt.date(y, mo, d)
        except ValueError:
            return None
    for f in _MONTH_FORMATS:
        try:
            return dt.datetime.strptime(s, f).date()
        except ValueError:
            continue
    return None


def _guess_date_order(values: list) -> str:
    firsts, seconds = [], []
    for v in values:
        m = re.fullmatch(r"(\d{1,2})[/.\-](\d{1,2})[/.\-]\d{2,4}", _text(v))
        if m:
            firsts.append(int(m.group(1)))
            seconds.append(int(m.group(2)))
    if any(x > 12 for x in seconds) and not any(x > 12 for x in firsts):
        return "mdy"
    return "dmy"


def _share(values: list, test) -> float:
    vals = [v for v in values if _text(v)]
    return sum(1 for v in vals if test(v)) / len(vals) if vals else 0.0


def _running_balance(amounts: list[Decimal], balances: list[Decimal]) -> bool:
    """Whether `balances` is the running balance of `amounts` (files can list oldest or newest first)."""
    n = len(amounts)
    if n < 3:
        return False
    fwd = sum(1 for i in range(1, n) if balances[i] - balances[i - 1] == amounts[i])
    back = sum(1 for i in range(1, n) if balances[i - 1] - balances[i] == amounts[i - 1])
    return max(fwd, back) >= 0.8 * (n - 1)


def detect_layout(rows: list[list]) -> Layout:
    """Work out where the date, amounts and text are - from the column titles, or from the values."""
    rows = [list(r) for r in rows]
    header_row, cols = -1, {}
    for i, r in enumerate(rows[:15]):
        found: dict[str, int] = {}
        for j, cell in enumerate(r):
            n = _norm(cell)
            for role, names in HEAD.items():
                if n in names and role not in found and j not in found.values():
                    found[role] = j
                    break
        if "date" in found and {"amount", "spent", "received"} & set(found):
            header_row, cols = i, found
            break
    data = [r for r in (rows[header_row + 1:] if header_row >= 0 else rows) if any(_text(c) for c in r)][:400]
    width = max((len(r) for r in data), default=0)
    column = {j: [_cell(r, j) for r in data] for j in range(width)}
    taken = set(cols.values())
    order = _guess_date_order(column.get(cols["date"], []) if "date" in cols else
                              [v for vals in column.values() for v in vals[:50]])
    if "date" not in cols:
        best = max(((j, _share(v, lambda x: parse_bank_date(x, order) is not None)) for j, v in column.items()),
                   key=lambda t: t[1], default=(None, 0))
        if best[0] is not None and best[1] >= 0.8:
            cols["date"] = best[0]
            taken.add(best[0])
    if not {"amount", "spent", "received"} & set(cols):
        numeric = [j for j in range(width) if j not in taken and
                   _share(column[j], lambda x: parse_money(x) is not None) >= 0.8]
        if len(numeric) == 1:
            cols["amount"] = numeric[0]
        elif len(numeric) >= 2:
            vals = {j: [parse_money(x) or ZERO for x in column[j]] for j in numeric}
            pair = next(((a, b) for a in numeric for b in numeric if a != b and _running_balance(vals[a], vals[b])), None)
            if pair:
                cols["amount"], cols["balance"] = pair
            else:
                a, b = numeric[0], numeric[1]
                exclusive = all(not (_text(x) and _text(y)) for x, y in zip(column[a], column[b]))
                if exclusive:
                    cols["spent"], cols["received"] = a, b
                else:
                    cols["amount"] = a
            taken.update(v for k, v in cols.items() if k in ("amount", "balance", "spent", "received"))
    if "balance" not in cols and "amount" in cols:
        amounts = [parse_money(x) or ZERO for x in column.get(cols["amount"], [])]
        for j in range(width):
            if j in taken or j in cols.values():
                continue
            if _share(column[j], lambda x: parse_money(x) is not None) >= 0.8 and \
                    _running_balance(amounts, [parse_money(x) or ZERO for x in column[j]]):
                cols["balance"] = j
                break
    if "description" not in cols and "payee" not in cols:
        texts = [j for j in range(width) if j not in cols.values() and
                 _share(column[j], lambda x: parse_money(x) is None and parse_bank_date(x, order) is None) >= 0.6]
        if texts:
            cols["description"] = max(texts, key=lambda j: sum(len(_text(x)) for x in column[j]))
    return Layout(header_row, cols, order)


def column_titles(rows: list[list], layout: Layout) -> list[dict]:
    """The file's columns for the 'check the columns' screen: title (or Column n), a sample, the role."""
    data = [r for r in (rows[layout.header_row + 1:] if layout.header_row >= 0 else rows) if any(_text(c) for c in r)]
    width = max([len(r) for r in data[:200]] + [len(rows[layout.header_row]) if layout.header_row >= 0 else 0])
    role_of = {v: k for k, v in layout.cols.items()}
    out = []
    for j in range(width):
        title = _text(_cell(rows[layout.header_row], j)) if layout.header_row >= 0 else ""
        sample = next((_text(_cell(r, j)) for r in data[:20] if _text(_cell(r, j))), "")
        out.append({"index": j, "title": title or f"Column {j + 1}", "sample": sample[:60], "role": role_of.get(j, "")})
    return out


def read_lines(rows: list[list], layout: Layout) -> tuple[list[dict], list[str]]:
    """Statement lines (oldest first) and any rows that could not be read."""
    c = layout.cols
    if "date" not in c:
        return [], ["choose the column that holds the date"]
    if "amount" not in c and "spent" not in c and "received" not in c:
        return [], ["choose the column that holds the amount (or the spent and received columns)"]
    out, problems = [], []
    first = layout.header_row + 1 if layout.header_row >= 0 else 0
    for k, r in enumerate(rows[first:], start=first + 1):
        if not any(_text(x) for x in r):
            continue
        raw_date = _cell(r, c["date"])
        d = parse_bank_date(raw_date, layout.date_order)
        if "amount" in c:
            amt = parse_money(_cell(r, c["amount"]))
        else:
            spent, received = parse_money(_cell(r, c.get("spent"))), parse_money(_cell(r, c.get("received")))
            amt = None if spent is None and received is None else abs(received or ZERO) - abs(spent or ZERO)
        if d is None:
            # a title, total or note line in the file - only worth a mention when it carries an amount
            if amt is not None and _text(raw_date):
                problems.append(f"row {k}: '{_text(raw_date)}' is not a date - the row was left out")
            continue
        if amt is None:
            problems.append(f"row {k}: no amount - the row was left out")
            continue
        if layout.flip:
            amt = -amt
        amt = q2(amt)
        if amt == 0:
            continue                                   # a nil line has nothing to code
        bal = parse_money(_cell(r, c["balance"])) if "balance" in c else None
        out.append({"date": d, "amount": amt, "payee": _text(_cell(r, c.get("payee"))),
                    "description": _text(_cell(r, c.get("description"))),
                    "reference": _text(_cell(r, c.get("reference"))), "balance": bal, "row": k})
    if len(out) > 1 and out[0]["date"] > out[-1]["date"]:
        out.reverse()                                  # newest-first files are stored oldest first
    return out, problems


def _squash(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def fingerprints(lines: list[dict]) -> list[str]:
    """
    A line's identity for spotting statements imported twice: date, amount and text, plus how many times
    that same line has appeared in its file (two identical coffees on one day are two lines).
    """
    seen: Counter = Counter()
    out = []
    for ln in lines:
        d = ln["date"] if isinstance(ln["date"], str) else ln["date"].isoformat()
        raw = "|".join([d, as_str(to_dec(ln["amount"])), _squash(ln.get("payee", "")),
                        _squash(ln.get("description", "")), _squash(ln.get("reference", ""))])
        key = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]
        seen[key] += 1
        out.append(f"{key}#{seen[key]}")
    return out


# ---------------------------------------------------------------------------------------- #
# one bank account's statement lines
# ---------------------------------------------------------------------------------------- #
def _date(s) -> dt.date:
    return s if isinstance(s, dt.date) else dt.date.fromisoformat(s)


class BankFeed:
    """The statement lines of one bank account (clients/<slug>/bank/<code>.json)."""

    def __init__(self, store, slug: str, code: str):
        self.store, self.slug, self.code = store, slug, code
        self.path = store.bank_path(slug, code)
        data = read_json(self.path, None) or {}
        self.data = {"account": code, "imports": [], "lines": [], "layout": None, **data}

    @property
    def exists(self) -> bool:
        return self.path.exists()

    @property
    def lines(self) -> list[dict]:
        return self.data["lines"]

    def get(self, line_id: str) -> dict | None:
        return next((ln for ln in self.lines if ln["id"] == line_id), None)

    def ordered(self, status: str | None = None) -> list[dict]:
        lines = [ln for ln in self.lines if ln["status"] != "deleted" and (status is None or ln["status"] == status)]
        return sorted(lines, key=lambda ln: (ln["date"], ln["seq"]))

    def save(self) -> None:
        write_json(self.path, self.data)

    def statement_balance(self) -> tuple[dt.date, Decimal] | None:
        """The bank's own balance after the latest statement line that carries one."""
        with_bal = [ln for ln in self.ordered() if ln.get("balance") not in (None, "")]
        if not with_bal:
            return None
        last = with_bal[-1]
        return _date(last["date"]), to_dec(last["balance"])

    def duplicates(self, parsed: list[dict]) -> list[bool]:
        have = {ln.get("fp") for ln in self.lines}
        return [fp in have for fp in fingerprints(parsed)]

    def add(self, parsed: list[dict], filename: str, layout: Layout, user: str) -> dict:
        have = {ln.get("fp") for ln in self.lines}
        seq = max((ln["seq"] for ln in self.lines), default=0)
        imp = {"id": "i" + uuid.uuid4().hex[:8], "at": now_iso(), "by": user, "file": filename}
        added = []
        for p, fp in zip(parsed, fingerprints(parsed)):
            if fp in have:
                continue
            seq += 1
            added.append({"id": "l" + uuid.uuid4().hex[:10], "seq": seq, "date": p["date"].isoformat(),
                          "amount": as_str(p["amount"]), "payee": p.get("payee", ""),
                          "description": p.get("description", ""), "reference": p.get("reference", ""),
                          "balance": as_str(p["balance"]) if p.get("balance") is not None else None,
                          "import": imp["id"], "fp": fp, "status": "unreconciled"})
        imp.update({"lines": len(added), "duplicates": len(parsed) - len(added),
                    "from": min((a["date"] for a in added), default=""), "to": max((a["date"] for a in added), default="")})
        self.lines.extend(added)
        self.data["imports"].append(imp)
        self.data["layout"] = layout.to_dict()
        self.save()
        return imp


def import_lines(ledger, code: str, lines: list[dict], filename: str, layout: Layout) -> dict:
    """Add a statement's lines to a bank account (lines already there are skipped)."""
    a = ledger.chart.get(code)
    if a is None:
        raise BankError(f"account {code} is not in the chart")
    if ledger.is_pl(code):
        raise BankError(f"{code} {a.name} is a profit and loss account - a statement goes into a bank, "
                        f"credit card or loan account")
    ledger.store.require_lock(ledger.slug)
    imp = BankFeed(ledger.store, ledger.slug, code).add(lines, filename, layout, ledger.store.user)
    ledger.store.audit(ledger.slug, "bank.imported", {"account": code, "file": filename, "lines": imp["lines"],
                                                      "duplicates": imp["duplicates"], "from": imp["from"],
                                                      "to": imp["to"]})
    return imp


# ---------------------------------------------------------------------------------------- #
# bank transactions in the ledger
# ---------------------------------------------------------------------------------------- #
def gst_amount(gross: Decimal, tax: str) -> Decimal:
    rate = TAX_RATES.get(tax or "", 0)
    return q2(abs(gross) * rate / (100 + rate)) if rate else ZERO


def line_journal(code: str, ln: dict) -> Journal:
    """A reconciled statement line as the bank transaction the ledger posts."""
    c = ln["coded"]
    amt = to_dec(ln["amount"])
    gst = to_dec(c.get("gst"))
    net = abs(amt) - gst
    text = ln.get("description") or ln.get("payee") or ""
    narration = (ln.get("payee") or ln.get("description") or "Bank transaction")[:200]
    gst_line = f"GST - {c.get('tax')}"
    tax = c.get("tax", "")
    if amt < 0:
        lines = [Line(c["account"], debit=net, description=text, tax=tax, gst=gst)]
        if gst:
            lines.append(Line(c["gst_account"], debit=gst, description=gst_line, auto=True))
        lines.append(Line(code, credit=abs(amt), description=text))
    else:
        lines = [Line(code, debit=amt, description=text), Line(c["account"], credit=net, description=text,
                                                                  tax=tax, gst=gst)]
        if gst:
            lines.append(Line(c["gst_account"], credit=gst, description=gst_line, auto=True))
    return Journal(date=_date(ln["date"]), narration=narration, lines=lines, number=c.get("number", ""),
                   kind="bank", status="posted", reference=ln.get("reference") or "", uid=ln["id"],
                   source={"bank": code, "line": ln["id"], "tax": c.get("tax", "")},
                   created={"by": c.get("by", ""), "at": c.get("at", "")})


def feeds(store, slug: str) -> list[BankFeed]:
    return [BankFeed(store, slug, code) for code in store.list_bank_accounts(slug)]


def journals_by_fy(store, slug: str, client) -> dict[int, list[Journal]]:
    """Every posted bank transaction, by financial year (matched transfer sides post nothing)."""
    out: dict[int, list[Journal]] = defaultdict(list)
    for feed in feeds(store, slug):
        for ln in feed.lines:
            c = ln.get("coded") or {}
            if ln["status"] == "reconciled" and c.get("number") and not c.get("match"):
                j = line_journal(feed.code, ln)
                out[client.fy_of(j.date)].append(j)
    return out


class NumberBook:
    """Next bank transaction number per year - never reusing one, even after a line is unreconciled."""

    def __init__(self, all_feeds: list[BankFeed]):
        self.last: dict[int, int] = defaultdict(int)
        for feed in all_feeds:
            for ln in feed.lines:
                for c in [ln.get("coded")] + list(ln.get("history") or []):
                    m = NUMBER_RE.match((c or {}).get("number") or "")
                    if m:
                        self.last[int(m.group(1))] = max(self.last[int(m.group(1))], int(m.group(2)))

    def take(self, fy: int) -> str:
        self.last[fy] += 1
        return f"B{fy}-{self.last[fy]:04d}"


def is_bank_account(a) -> bool:
    t = (a.type or "").strip().lower()
    return t in ("bank", "credit card") or (a.report_code or "").upper().startswith("ASS.CUR.CAS.BAN")


def gst_account(ledger) -> str:
    """The account GST is posted to: Xero's GST account (type GST), else a GST liability account."""
    usable = ledger.chart.usable()
    a = next((a for a in usable if (a.type or "").strip().lower() == "gst"), None)
    a = a or next((a for a in usable if (a.report_code or "").upper().startswith("LIA.CUR.TAX.GST")
                   and "gst" in a.name.lower()), None)
    return a.code if a else ""


def default_tax(account) -> str:
    return account.tax_code if account is not None and account.tax_code in TAX_RATES else "BAS Excluded"


def tax_for_type(acct_type: str, gst_registered: bool) -> str:
    """The tax rate a new account starts with - Xero's defaults for each account type."""
    if not gst_registered:
        return "BAS Excluded"
    t = (acct_type or "").strip().lower()
    if t in ("revenue", "sales", "other income"):
        return "GST on Income"
    if t in ("expense", "overhead", "overheads", "direct costs"):
        return "GST on Expenses"
    if t == "fixed asset":
        return "GST on Capital"
    return "BAS Excluded"


def statement_accounts(ledger) -> tuple[list, list]:
    """Accounts a statement can go into: (bank accounts, other balance-sheet accounts such as cards and loans)."""
    banks, others = [], []
    for a in ledger.chart.usable():
        if is_bank_account(a):
            banks.append(a)
        elif not ledger.is_pl(a.code):
            others.append(a)
    return banks, others


@dataclass
class Coding:
    line: str
    account: str
    tax: str = ""


def _find_transfer(feed_code: str, ln: dict, target: BankFeed) -> dict | None:
    """The posted other side of a transfer: coded to this account, the opposite amount, within a week."""
    amt, when = to_dec(ln["amount"]), _date(ln["date"])
    best, gap = None, None
    for o in target.ordered("reconciled"):
        c = o.get("coded") or {}
        if c.get("account") != feed_code or c.get("match") or c.get("matched_by") or not c.get("number"):
            continue
        if to_dec(o["amount"]) != -amt:
            continue
        g = abs((_date(o["date"]) - when).days)
        if g <= MATCH_DAYS and (gap is None or g < gap):
            best, gap = o, g
    return best


def reconcile(ledger, code: str, codings: list[Coding]) -> dict:
    """
    Post the coded lines of one bank account.  Each line is checked on its own: the good ones are
    reconciled, the others come back with the reason.  Returns {"done": [...], "errors": {line: reason}}.
    """
    store = ledger.store
    store.require_lock(ledger.slug)
    all_feeds = {f.code: f for f in feeds(store, ledger.slug)}
    feed = all_feeds.get(code) or BankFeed(store, ledger.slug, code)
    numbers = NumberBook(list(all_feeds.values()))
    gst_on = bool(getattr(ledger.client, "gst_registered", False))
    gst_code = gst_account(ledger) if gst_on else ""
    years = set(ledger.years())
    done, errors, touched = [], {}, {code}
    # numbered in date order, whatever order the lines were ticked in
    order = {ln["id"]: (ln["date"], ln["seq"]) for ln in feed.lines}
    codings = sorted(codings, key=lambda cd: order.get(cd.line, ("9999", 0)))
    for cd in codings:
        ln = feed.get(cd.line)
        if ln is None or ln["status"] != "unreconciled":
            errors[cd.line] = "this line is no longer waiting to be reconciled"
            continue
        acct = (cd.account or "").strip().split(" ")[0]
        a = ledger.chart.get(acct)
        fy = ledger.client.fy_of(_date(ln["date"]))
        if not acct:
            errors[cd.line] = "choose an account"
        elif a is None:
            errors[cd.line] = f"account {acct} is not in the chart"
        elif a.system:
            errors[cd.line] = f"{a.code} {a.name} is a Xero system account - choose another"
        elif acct == code:
            errors[cd.line] = "a line cannot be coded to its own bank account"
        elif fy not in years:
            errors[cd.line] = f"{_date(ln['date']):%d/%m/%Y} is in FY{fy}, which has not been started"
        elif ledger.is_locked(fy):
            errors[cd.line] = f"FY{fy} is locked (finalised) - unlock it first"
        if cd.line in errors:
            continue
        stamp = {"by": store.user, "at": now_iso()}
        # a transfer to another account with a statement: match the side already posted there
        if acct in all_feeds or store.bank_path(ledger.slug, acct).exists():
            target = all_feeds.setdefault(acct, BankFeed(store, ledger.slug, acct))
            other = _find_transfer(code, ln, target)
            if other is not None:
                other["coded"]["matched_by"] = {"feed": code, "line": ln["id"]}
                ln["coded"] = {"account": acct, "tax": "", "gst": "0.00",
                               "match": {"feed": acct, "line": other["id"], "number": other["coded"]["number"]}, **stamp}
                ln["status"] = "reconciled"
                touched.add(acct)
                done.append({"line": ln["id"], "number": other["coded"]["number"], "matched": True})
                continue
        tax = (cd.tax if cd.tax in TAX_RATES else default_tax(a)) if gst_on else ""
        gst = gst_amount(to_dec(ln["amount"]), tax) if gst_on else ZERO
        if gst and not gst_code:
            errors[cd.line] = "the chart has no GST account - add one (type GST) or choose a GST-free tax rate"
            continue
        ln["coded"] = {"number": "B0000-0000", "account": acct, "tax": tax, "gst": as_str(gst),
                       "gst_account": gst_code if gst else "", **stamp}
        problems = ledger.validate(line_journal(code, ln), fy)
        if problems:
            del ln["coded"]
            errors[cd.line] = "; ".join(problems)
            continue
        ln["coded"]["number"] = numbers.take(fy)
        ln["status"] = "reconciled"
        done.append({"line": ln["id"], "number": ln["coded"]["number"], "matched": False})
    if done:
        all_feeds[code] = feed
        for c in touched:
            all_feeds[c].save()
        ledger.bank_changed()
        store.audit(ledger.slug, "bank.reconciled", {"account": code, "count": len(done),
                                                     "transactions": [d["number"] for d in done]})
    return {"done": done, "errors": errors}


def unreconcile(ledger, code: str, line_id: str, reason: str = "") -> dict:
    """Take a reconciled line out of the ledger again; it goes back to be coded."""
    store = ledger.store
    store.require_lock(ledger.slug)
    feed = BankFeed(store, ledger.slug, code)
    ln = feed.get(line_id)
    if ln is None or ln["status"] != "reconciled":
        raise BankError("that line is not reconciled")
    fy = ledger.client.fy_of(_date(ln["date"]))
    c = ln["coded"]
    if not c.get("match") and ledger.is_locked(fy):
        raise BankError(f"FY{fy} is locked (finalised) - unlock it first")
    stamp = {"by": store.user, "at": now_iso(), "reason": reason or ""}
    also = None
    if c.get("match"):
        other = BankFeed(store, ledger.slug, c["match"]["feed"])
        o = other.get(c["match"]["line"])
        if o and (o.get("coded") or {}).get("matched_by", {}).get("line") == line_id:
            del o["coded"]["matched_by"]
            other.save()
    elif c.get("matched_by"):
        # the other side of a transfer was matched to this transaction - it goes back to be coded too
        mb = c["matched_by"]
        other = BankFeed(store, ledger.slug, mb["feed"])
        o = other.get(mb["line"])
        if o and o["status"] == "reconciled" and (o.get("coded") or {}).get("match", {}).get("line") == line_id:
            o.setdefault("history", []).append({**o["coded"], "undone": {**stamp, "reason": f"{c['number']} was unreconciled"}})
            del o["coded"]
            o["status"] = "unreconciled"
            other.save()
            also = {"feed": mb["feed"], "line": mb["line"]}
    ln.setdefault("history", []).append({**c, "undone": stamp})
    del ln["coded"]
    ln["status"] = "unreconciled"
    feed.save()
    ledger.bank_changed()
    store.audit(ledger.slug, "bank.unreconciled", {"account": code, "transaction": c.get("number") or
                                                   f"matched to {c.get('match', {}).get('number')}",
                                                   "date": ln["date"], "amount": ln["amount"], "reason": reason or ""})
    return {"number": c.get("number", ""), "also": also}


def delete_lines(ledger, code: str, line_ids: list[str], reason: str = "") -> int:
    """Put unwanted unreconciled lines aside (e.g. a duplicate) - they are kept, marked deleted."""
    store = ledger.store
    store.require_lock(ledger.slug)
    feed = BankFeed(store, ledger.slug, code)
    n = 0
    for lid in line_ids:
        ln = feed.get(lid)
        if ln is not None and ln["status"] == "unreconciled":
            ln["status"] = "deleted"
            ln["deleted"] = {"by": store.user, "at": now_iso(), "reason": reason or ""}
            n += 1
    if n:
        feed.save()
        store.audit(ledger.slug, "bank.lines_deleted", {"account": code, "count": n, "reason": reason or ""})
    return n


def restore_lines(ledger, code: str, line_ids: list[str]) -> int:
    """Bring deleted lines back to be coded."""
    store = ledger.store
    store.require_lock(ledger.slug)
    feed = BankFeed(store, ledger.slug, code)
    n = 0
    for lid in line_ids:
        ln = feed.get(lid)
        if ln is not None and ln["status"] == "deleted":
            ln["status"] = "unreconciled"
            ln.setdefault("history", []).append({"restored": {"by": store.user, "at": now_iso()},
                                                 "deleted": ln.pop("deleted", {})})
            n += 1
    if n:
        feed.save()
        store.audit(ledger.slug, "bank.lines_restored", {"account": code, "count": n})
    return n


def preview(ledger, code: str, rows: list[list], layout: Layout) -> dict:
    """What an import would add: the lines, which are already in the account, and anything to know first."""
    lines, problems = read_lines(rows, layout)
    feed = BankFeed(ledger.store, ledger.slug, code)
    dupes = feed.duplicates(lines) if lines else []
    notes = []
    years = set(ledger.years())
    later = sorted({ledger.client.fy_of(ln["date"]) for ln in lines} - years)
    if later:
        notes.append("Lines in " + ", ".join(f"FY{y}" for y in later) + " can be imported, but they cannot be "
                     "reconciled until that year is started.")
    if lines and layout.cols.get("balance") is not None:
        amounts = [ln["amount"] for ln in lines]
        balances = [ln["balance"] if ln["balance"] is not None else ZERO for ln in lines]
        if len(lines) >= 3 and not _running_balance(amounts, balances):
            notes.append("The balance column does not follow the amounts - check that the amount column (and the "
                         "'money spent shows as positive' box) are right.")
    spent = sum((-ln["amount"] for ln in lines if ln["amount"] < 0), ZERO)
    received = sum((ln["amount"] for ln in lines if ln["amount"] > 0), ZERO)
    return {"lines": lines, "problems": problems, "duplicates": dupes, "new": dupes.count(False),
            "notes": notes, "spent": spent, "received": received,
            "from": lines[0]["date"] if lines else None, "to": lines[-1]["date"] if lines else None}


# ---------------------------------------------------------------------------------------- #
# suggestions: code a line the way the same payee was coded last time
# ---------------------------------------------------------------------------------------- #
NOISE = {
    "CARD", "PURCHASE", "PURCHASES", "EFTPOS", "VISA", "MASTERCARD", "DEBIT", "CREDIT", "POS", "AU", "AUS",
    "AUSTRALIA", "NSW", "VIC", "QLD", "SA", "WA", "TAS", "NT", "ACT", "PTY", "LTD", "THE", "VALUE", "DATE",
    "XX", "XXXX", "TFR", "TO", "FROM", "INTERNET", "ONLINE", "BPAY", "OSKO", "FAST", "NETBANK", "COMMBANK",
    "APP", "REF", "REFERENCE", "RECEIPT", "NO", "AND", "OF", "PAYMENT", "DIRECT", "DEPOSIT", "WITHDRAWAL",
    "TRANSFER", "TRANSACTION", "BANKING", "MOBILE", "INTL", "FEE", "AUD", "CR", "DR", "SYDNEY", "MELBOURNE",
    "BRISBANE", "PERTH", "ADELAIDE", "HOBART", "DARWIN", "CANBERRA",
}


def _words(text: str) -> list[str]:
    return [w for w in re.findall(r"[A-Z][A-Z&']+", (text or "").upper()) if w not in NOISE]


def _keys(ln: dict) -> list[str]:
    sign = "out" if to_dec(ln["amount"]) < 0 else "in"
    keys = []
    for text in (ln.get("payee"), ln.get("description")):
        words = _words(text or "")
        if not words:
            continue
        keys.append(" ".join(words))
        for n in (3, 2, 1):
            if len(words) >= n and (n > 1 or len(words[0]) >= 4):
                keys.append(" ".join(words[:n]))
    return [f"{sign}:{k}" for k in dict.fromkeys(keys)]


def suggestions(all_feeds: list[BankFeed], lines: list[dict]) -> dict[str, dict]:
    """line id -> {"account", "tax"} learned from every line this client has reconciled (latest wins)."""
    memory: dict[str, dict] = {}
    done = sorted((ln for f in all_feeds for ln in f.lines if ln["status"] == "reconciled"),
                  key=lambda ln: ((ln.get("coded") or {}).get("at", ""), ln["date"]))
    for ln in done:
        c = ln["coded"]
        for k in _keys(ln):
            memory[k] = {"account": c["account"], "tax": c.get("tax", "")}
    out = {}
    for ln in lines:
        for k in _keys(ln):
            if k in memory:
                out[ln["id"]] = memory[k]
                break
    return out


# ---------------------------------------------------------------------------------------- #
# the dashboard
# ---------------------------------------------------------------------------------------- #
def summaries(ledger, fy: int | None = None) -> list[dict]:
    """
    One card per bank account: those with statements, plus bank accounts with a balance this year.
    The ledger balance is taken at the date of the latest statement balance, so the two can be compared.
    """
    years = ledger.years()
    by_code = {f.code: f for f in feeds(ledger.store, ledger.slug)}
    codes = list(by_code)
    if fy in years:
        bal = ledger.closing(fy)
        for a in ledger.chart.usable():
            if is_bank_account(a) and a.code not in by_code and bal.get(a.code):
                codes.append(a.code)
    out = []
    for code in codes:
        a = ledger.chart.get(code)
        feed = by_code.get(code)
        stmt = feed.statement_balance() if feed else None
        ledger_bal, as_at = None, None
        when = stmt[0] if stmt else (ledger.fy_range(fy)[1] if fy in years else None)
        if when is not None and ledger.client.fy_of(when) in years:
            as_at = when
            ledger_bal = ledger.closing(ledger.client.fy_of(when), upto=when).get(code, ZERO)
        last = feed.data["imports"][-1] if feed and feed.data["imports"] else None
        out.append({"code": code, "name": a.name if a else "(not in the chart)", "account": a,
                    "to_reconcile": len(feed.ordered("unreconciled")) if feed else 0,
                    "reconciled": len(feed.ordered("reconciled")) if feed else 0,
                    "statement": stmt, "ledger_balance": ledger_bal, "as_at": as_at, "last_import": last,
                    "has_feed": feed is not None})
    out.sort(key=lambda s: (not s["has_feed"], s["code"]))
    return out
