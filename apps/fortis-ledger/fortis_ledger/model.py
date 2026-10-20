"""Data classes for clients and journals, with their JSON forms."""
from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import asdict, dataclass, field
from decimal import Decimal

from .money import ZERO, as_str, to_dec

ENTITY_TYPES = ("company", "trust", "partnership", "sole_trader")
JOURNAL_KINDS = ("manual", "import", "opening", "close", "reversal", "bank")
STATUSES = ("draft", "posted", "void")


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def parse_date(v) -> dt.date:
    """ISO (2026-06-30), Australian (30/06/2026, 30-06-2026, 30.06.26) or '30 Jun 2026' / '30-Jun-2026'."""
    if isinstance(v, dt.datetime):
        return v.date()
    if isinstance(v, dt.date):
        return v
    s = str(v or "").strip()
    if not s:
        raise ValueError("missing date")
    for f in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y", "%d/%m/%y", "%d-%m-%y", "%d.%m.%y",
              "%d %b %Y", "%d %B %Y", "%d-%b-%Y", "%d-%b-%y", "%Y/%m/%d"):
        try:
            return dt.datetime.strptime(s, f).date()
        except ValueError:
            continue
    if "T" in s:
        try:
            return dt.datetime.fromisoformat(s.replace("Z", "+00:00")).date()
        except ValueError:
            pass
    raise ValueError(f"not a date: {v!r} (use 30/06/2026 or 2026-06-30)")


@dataclass
class Line:
    account: str
    debit: Decimal = ZERO
    credit: Decimal = ZERO
    description: str = ""
    tax: str = ""                 # tax rate, e.g. "GST on Expenses" (clients registered for GST)
    gst: Decimal = ZERO           # the GST on this line - posted on an automatic line to the GST account
    auto: bool = False            # that automatic GST line

    @property
    def amount(self) -> Decimal:
        """Debit positive."""
        return self.debit - self.credit

    def to_dict(self) -> dict:
        d = {"account": self.account, "description": self.description,
             "debit": as_str(self.debit), "credit": as_str(self.credit)}
        if self.tax:
            d["tax"] = self.tax
        if self.gst:
            d["gst"] = as_str(self.gst)
        if self.auto:
            d["auto"] = True
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Line":
        return cls(account=str(d.get("account", "")).strip(), description=d.get("description", "") or "",
                   debit=to_dec(d.get("debit")), credit=to_dec(d.get("credit")), tax=d.get("tax", "") or "",
                   gst=to_dec(d.get("gst")), auto=bool(d.get("auto")))


@dataclass
class Journal:
    date: dt.date
    narration: str
    lines: list[Line] = field(default_factory=list)
    number: str = ""
    kind: str = "manual"
    status: str = "draft"
    reference: str = ""
    uid: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    source: dict = field(default_factory=dict)
    created: dict = field(default_factory=dict)
    updated: dict = field(default_factory=dict)
    voided: dict = field(default_factory=dict)
    reverses: str = ""
    reversed_by: str = ""
    amounts: str = ""             # taxed lines were entered GST "inclusive" (the default) or "exclusive"

    @property
    def total_debit(self) -> Decimal:
        return sum((ln.debit for ln in self.lines), ZERO)

    @property
    def total_credit(self) -> Decimal:
        return sum((ln.credit for ln in self.lines), ZERO)

    @property
    def difference(self) -> Decimal:
        return self.total_debit - self.total_credit

    @property
    def is_balanced(self) -> bool:
        return self.difference == 0

    def to_dict(self) -> dict:
        d = {"number": self.number, "uid": self.uid, "date": self.date.isoformat(), "narration": self.narration,
             "reference": self.reference, "kind": self.kind, "status": self.status,
             "lines": [ln.to_dict() for ln in self.lines], "source": self.source,
             "created": self.created, "updated": self.updated}
        if self.voided:
            d["voided"] = self.voided
        if self.reverses:
            d["reverses"] = self.reverses
        if self.reversed_by:
            d["reversed_by"] = self.reversed_by
        if self.amounts:
            d["amounts"] = self.amounts
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Journal":
        return cls(date=parse_date(d["date"]), narration=d.get("narration", ""), number=d.get("number", ""),
                   lines=[Line.from_dict(x) for x in d.get("lines", [])], kind=d.get("kind", "manual"),
                   status=d.get("status", "draft"), reference=d.get("reference", "") or "",
                   uid=d.get("uid") or uuid.uuid4().hex[:12], source=d.get("source") or {},
                   created=d.get("created") or {}, updated=d.get("updated") or {}, voided=d.get("voided") or {},
                   reverses=d.get("reverses", "") or "", reversed_by=d.get("reversed_by", "") or "",
                   amounts=d.get("amounts", "") or "")


@dataclass
class Client:
    id: str
    slug: str
    name: str
    type: str = "company"
    legal_name: str = ""
    abn: str = ""
    fy_end_month: int = 6
    fy_end_day: int = 30
    directors: list[str] = field(default_factory=list)
    trustee: str = ""
    trustee_type: str = ""
    business_name: str = ""
    proprietor: str = ""
    compiled_by: str = ""
    retained_earnings_account: str = ""
    rounding_account: str = ""
    gst_registered: bool = False         # cash coding splits the GST out of bank lines
    options: dict = field(default_factory=dict)
    labels: dict = field(default_factory=dict)
    notes: str = ""
    created: dict = field(default_factory=dict)
    source: dict = field(default_factory=dict)

    def fy_range(self, fy: int) -> tuple[dt.date, dt.date]:
        """Financial year `fy` is the year that ends in calendar year `fy` (FY2026 = 1 Jul 2025 - 30 Jun 2026)."""
        end = _safe_date(fy, self.fy_end_month, self.fy_end_day)
        prev_end = _safe_date(fy - 1, self.fy_end_month, self.fy_end_day)
        return prev_end + dt.timedelta(days=1), end

    def fy_of(self, d: dt.date) -> int:
        end_this = _safe_date(d.year, self.fy_end_month, self.fy_end_day)
        return d.year if d <= end_this else d.year + 1

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Client":
        known = {k: d[k] for k in cls.__dataclass_fields__ if k in d}
        c = cls(**known)
        c.directors = [x for x in (c.directors or []) if str(x).strip()]
        return c


def _safe_date(year: int, month: int, day: int) -> dt.date:
    try:
        return dt.date(year, month, day)
    except ValueError:  # 29 Feb in a non-leap year, 31st of a 30-day month
        nxt = dt.date(year + (month == 12), month % 12 + 1, 1)
        return nxt - dt.timedelta(days=1)
