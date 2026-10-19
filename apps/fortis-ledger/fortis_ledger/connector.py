"""
The Claude connector: a Model Context Protocol (MCP) server, so Claude - the Claude desktop app or
Claude Code on this PC - can look at a client's ledger and draft or post journals.

* It runs on this PC only.  Claude starts `fortis-ledger-mcp.exe` (or `python -m fortis_ledger --mcp`)
  and talks to it over standard input and output; nothing listens on the network.
* It works on this PC's data folder (the OneDrive sync of the firm's SharePoint library), as the
  person who uses Fortis Ledger here, through the same rules as the screens: the client's chart, the
  financial year, debits equal credits, locked years, GST, the edit lock and the change log - where
  every change is marked "via Claude".
* Claude posts the journals it is asked to post, or saves drafts for a person to check.  A partner can turn
  posting off (Firm settings > Claude); Claude then only saves drafts.  It can only delete drafts it made.
* It speaks both eras of MCP: the 2026-07-28 revision (every request carries its protocol version in
  `_meta`; `server/discover`) and the earlier revisions with the `initialize` handshake (2024-11-05 to
  2025-11-25), so it works with older and newer Claude apps alike.
"""
from __future__ import annotations

import datetime as dt
import difflib
import json
import logging
import logging.handlers
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import __version__
from . import bank as bankmod
from . import statements
from .chartbook import ACCOUNT_TYPES, ChartError, default_report_code
from .ledger import Ledger, LedgerError, ValidationError
from .model import Journal, Line, parse_date
from .money import ZERO, as_str, to_dec
from .settings import Settings, config_dir, default_user_name
from .store import FolderStore, StoreError

log = logging.getLogger("fortis_ledger.connector")

MODERN_VERSIONS = ("2026-07-28",)
LEGACY_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")       # newest first
SUPPORTED_VERSIONS = MODERN_VERSIONS + LEGACY_VERSIONS
META_VERSION = "io.modelcontextprotocol/protocolVersion"
META_CAPABILITIES = "io.modelcontextprotocol/clientCapabilities"
META_SERVER = "io.modelcontextprotocol/serverInfo"
SERVER_INFO = {"name": "fortis-ledger", "title": "Fortis Ledger", "version": __version__}
PARSE_ERROR, INVALID_REQUEST, METHOD_NOT_FOUND, INVALID_PARAMS, INTERNAL_ERROR = -32700, -32600, -32601, -32602, -32603
UNSUPPORTED_VERSION = -32022
CACHE_MS = 60 * 60 * 1000
AGENT = "Claude"
EDITABLE_KINDS = {"manual", "reversal", "opening"}
ENTITY = {"company": "Company", "trust": "Trust", "partnership": "Partnership", "sole_trader": "Sole trader"}

INSTRUCTIONS = (
    "Fortis Ledger is Fortis Accounting Partners' ledger for its clients (companies, trusts, partnerships and "
    "sole traders). Financial years are named by the year they end: FY2026 is the year ending in 2026 (for a "
    "June year end, 1 July 2025 to 30 June 2026). Find the client with list_clients and the account codes with "
    "get_chart_of_accounts - never guess a code. Look at balances with get_trial_balance or "
    "get_account_transactions before and after a journal. create_journal posts the journal when post is true - "
    "when the user asks for a journal to be posted, entered or put through - and otherwise saves a draft for a "
    "person to check; if the firm has turned off posting by Claude it always saves a draft. Always tell the user "
    "the journal number and whether it is a draft or posted. Amounts are Australian dollars, debits and credits "
    "as positive numbers. "
    "Every change is recorded in the client's change log as made via Claude.")


class ToolError(Exception):
    """A problem Claude can fix (a wrong account, an unbalanced journal) - returned to it as the tool's result."""


class RpcError(Exception):
    def __init__(self, code: int, message: str, data=None):
        super().__init__(message)
        self.code, self.message, self.data = code, message, data


# ---------------------------------------------------------------------------------------- #
# working on the ledger
# ---------------------------------------------------------------------------------------- #
def identity(settings: Settings, firm: dict) -> str:
    """Who Claude acts for: the office email when the firm requires sign-in, else the name set on this PC."""
    ms = firm.get("microsoft") or {}
    if ms.get("required") and ms.get("tenant_id") and ms.get("client_id"):
        if not settings.last_email:
            raise ToolError("This firm requires office sign-in. Open Fortis Ledger on this PC and sign in once - "
                            "Claude then works as you.")
        return settings.last_email
    return (settings.user_name or settings.last_email or default_user_name() or "").strip()


class Workspace:
    """One tool call's view of the data folder (read fresh each time, so it is never out of date)."""

    def __init__(self, settings: Settings):
        folder = settings.data_folder
        if not folder or not Path(folder).is_dir():
            raise ToolError("Fortis Ledger is not set up on this PC yet: open it once and choose the data folder "
                            "(the OneDrive sync of the firm's SharePoint library).")
        probe = FolderStore(folder)
        if not probe.is_initialised():
            raise ToolError(f"{folder} is not a Fortis Ledger data folder - choose it again in Fortis Ledger's Settings.")
        self.settings = settings
        self.firm = probe.firm_settings()
        user = identity(settings, self.firm)
        if not user:
            raise ToolError("Set your name in Fortis Ledger's Settings first - it goes in the change log.")
        self.store = FolderStore(folder, user=user, agent=AGENT)

    @property
    def can_post(self) -> bool:
        """Claude may post journals unless a partner has turned that off in Firm settings."""
        return bool((self.firm.get("claude") or {}).get("can_post", True))

    def client(self, text: str):
        t = str(text or "").strip()
        if not t:
            raise ToolError("Say which client: its id from list_clients, or its name.")
        clients = self.store.list_clients()
        for c in clients:
            if c.slug == t:
                return c
        low = t.lower()
        exact = [c for c in clients if low in (c.name.lower(), (c.legal_name or "").lower())]
        if len(exact) == 1:
            return exact[0]
        digits = "".join(ch for ch in t if ch.isdigit())
        part = [c for c in clients if low in c.name.lower() or low in (c.legal_name or "").lower()
                or (len(digits) >= 6 and digits in (c.abn or "").replace(" ", ""))]
        if len(part) == 1:
            return part[0]
        if part:
            raise ToolError(f"'{t}' matches {len(part)} clients: " +
                            "; ".join(f"{c.name} (id {c.slug})" for c in part[:10]) + ". Say which one.")
        raise ToolError(f"No client matches '{t}'. Use list_clients to see the clients.")

    def ledger(self, text: str) -> Ledger:
        return Ledger(self.store, self.client(text).slug)

    @contextmanager
    def editing(self, L: Ledger):
        """
        Make a change under the client's edit lock.  If the person has the client open in Fortis Ledger on this
        PC, Claude works under their lock; if someone else has it open, nothing changes.  A lock Claude takes
        for the change is handed back straight after.
        """
        st = L.store
        info = st.lock_info(L.slug)
        mine = {self.settings.user_name, self.settings.last_email} - {""}
        if info and not info["stale"] and info.get("machine") == st.machine and info.get("user") in mine:
            st.user = info["user"]            # the same person on this PC, signed in or not
            info = st.lock_info(L.slug)
        if info and not info["stale"] and not info["mine"]:
            raise ToolError(f"{info.get('user')} is editing {L.client.name} on {info.get('machine')}. Claude can "
                            f"change it once they close the client in Fortis Ledger.")
        took = not info or info["stale"]
        try:
            yield
        finally:
            if took:
                st.release_lock(L.slug)


def _year(L: Ledger, fy=None) -> int:
    years = L.years()
    if not years:
        raise ToolError(f"{L.client.name} has no financial year started in Fortis Ledger yet.")
    if fy in (None, ""):
        return years[-1]
    try:
        fy = int(str(fy).upper().replace("FY", "").strip())
    except ValueError:
        raise ToolError(f"fy is the year a financial year ends, e.g. 2026 - not {fy!r}") from None
    if fy not in years:
        raise ToolError(f"FY{fy} has not been started for {L.client.name} (started: "
                        f"{', '.join(f'FY{y}' for y in years)}).")
    return fy


def _date(v, name: str = "date") -> dt.date:
    try:
        return parse_date(v)
    except ValueError as e:
        raise ToolError(f"{name}: {e}") from None


def _amount(v, name: str):
    if v in (None, ""):
        return ZERO
    try:
        d = to_dec(v)
    except ValueError:
        raise ToolError(f"{name}: {v!r} is not an amount") from None
    if d < 0:
        raise ToolError(f"{name}: amounts are positive - put a negative debit in the credit column instead")
    return d


def _account(L: Ledger, text, posting: bool = True):
    raw = str(text or "").strip()
    a = L.chart.get(raw.split(" ")[0]) if raw else None
    if a is None and raw:
        a = L.chart.by_name(raw)
    if a is None:
        usable = L.chart.usable()
        names = {x.name.lower(): x for x in usable}
        close = [names[n] for n in difflib.get_close_matches(raw.lower(), list(names), n=3, cutoff=0.6)]
        code = raw.split(" ")[0]
        close += [x for x in usable if len(code) >= 2 and x.code.startswith(code)][:3]
        close += [x for x in usable if len(code) >= 3 and x.code.startswith(code[:3])][:3]
        hint = ", ".join(dict.fromkeys(f"{x.code} {x.name}" for x in close))
        raise ToolError(f"Account {raw!r} is not in {L.client.name}'s chart" +
                        (f" - did you mean {hint}?" if hint else " - use get_chart_of_accounts to find it."))
    if a.system and posting:
        raise ToolError(f"{a.code} {a.name} is a Xero system account - journals cannot post to it.")
    return a


def _money(d) -> str:
    return as_str(d) if d not in (None, "") else ""


def _journal_out(L: Ledger, j: Journal, fy: int) -> dict:
    names = {a.code: a.name for a in L.chart.accounts}
    lines = []
    for ln in j.lines:
        row = {"account": ln.account, "account_name": names.get(ln.account, "(not in the chart)"),
               "description": ln.description, "debit": _money(ln.debit) if ln.debit else "",
               "credit": _money(ln.credit) if ln.credit else ""}
        if ln.tax:
            row["tax_rate"] = ln.tax
        if ln.gst:
            row["gst"] = as_str(ln.gst)
        if ln.auto:
            row["gst_line"] = True
        lines.append(row)
    out = {"number": j.number, "fy": fy, "date": j.date.isoformat(), "narration": j.narration,
           "reference": j.reference, "kind": j.kind, "status": j.status, "lines": lines,
           "total_debit": as_str(j.total_debit), "total_credit": as_str(j.total_credit)}
    for key, value in (("amounts", "GST exclusive" if j.amounts == "exclusive" else ""), ("created", j.created),
                       ("changed", j.updated), ("voided", j.voided), ("reverses", j.reverses),
                       ("reversed_by", j.reversed_by), ("bank_account", j.source.get("bank") if j.kind == "bank" else "")):
        if value:
            out[key] = value
    return out


def _where(L: Ledger, number: str) -> str:
    return f"Fortis Ledger > {L.client.name} > Journals > {number}"


def _number_year(L: Ledger, number: str, fy=None) -> int:
    n = str(number or "").strip().upper()
    if fy in (None, "") and len(n) > 5 and n[0] in "JB" and n[1:5].isdigit():
        fy = int(n[1:5])
    return _year(L, fy)


def _get_journal(L: Ledger, number: str, fy=None) -> tuple[Journal, int]:
    number = str(number or "").strip().upper()
    if not number:
        raise ToolError("Say which journal, e.g. J2026-0004.")
    year = _number_year(L, number, fy)
    try:
        return L.get_journal(year, number), year
    except StoreError:
        raise ToolError(f"There is no journal {number} in FY{year} for {L.client.name}.") from None


def _rows(data: dict) -> list[dict]:
    rows = []
    for r in data["rows"]:
        row = {"label": r["label"], "kind": r["kind"]}
        if r.get("code"):
            row["account"] = r["code"]
        if r["kind"] not in ("heading", "subheading"):
            row["this_period"] = _money(r["cy"])
            if data["compare"]:
                row["prior_period"] = _money(r["py"])
        rows.append(row)
    return rows


# ---------------------------------------------------------------------------------------- #
# the tools
# ---------------------------------------------------------------------------------------- #
def t_list_clients(ws: Workspace, args: dict) -> dict:
    q = str(args.get("search") or "").strip().lower()
    out = []
    for c in ws.store.list_clients():
        if q and q not in c.name.lower() and q not in (c.legal_name or "").lower() \
                and q.replace(" ", "") not in (c.abn or "").replace(" ", ""):
            continue
        lock = ws.store.lock_info(c.slug)
        row = {"client": c.slug, "name": c.name, "entity_type": ENTITY.get(c.type, c.type), "abn": c.abn,
               "year_end": f"{c.fy_end_day} {dt.date(2000, c.fy_end_month, 1):%B}",
               "financial_years": [f"FY{y}" for y in ws.store.list_years(c.slug)],
               "gst_registered": bool(c.gst_registered)}
        if c.legal_name:
            row["legal_name"] = c.legal_name
        if lock and not lock["stale"] and not lock["mine"]:
            row["being_edited_by"] = f"{lock.get('user')} on {lock.get('machine')}"
        out.append(row)
    return {"clients": out, "count": len(out), "claude_can_post_journals": ws.can_post}


def t_chart(ws: Workspace, args: dict) -> dict:
    L = ws.ledger(args.get("client"))
    fy = _year(L, args.get("fy")) if args.get("fy") not in (None, "") else None
    bal = L.closing(fy) if fy else {}
    words = str(args.get("search") or "").lower().split()
    gst = bool(L.client.gst_registered)
    out = []
    for a in L.chart.accounts:
        hay = f"{a.code} {a.name} {a.type} {a.report_code}".lower()
        if words and not (a.code.lower().startswith(words[0]) or all(w in hay for w in words)):
            continue
        row = {"code": a.code, "name": a.name, "type": a.type, "report_code": a.report_code}
        if gst:
            row["tax_rate"] = bankmod.default_tax(a)
        if a.system:
            row["cannot_post"] = "Xero system account"
        if fy:
            row["balance"] = as_str(bal.get(a.code, ZERO))
        out.append(row)
    res = {"client": L.client.name, "accounts": out, "count": len(out), "gst_registered": gst}
    if gst:
        res["gst_account"] = bankmod.gst_account(L)
    if fy:
        res["balances"] = f"FY{fy} closing balances, debit positive, credit negative"
    return res


def t_trial_balance(ws: Workspace, args: dict) -> dict:
    L = ws.ledger(args.get("client"))
    fy = _year(L, args.get("fy"))
    start, end = L.fy_range(fy)
    upto = _date(args["as_at"], "as_at") if args.get("as_at") else end
    if not start <= upto <= end:
        raise ToolError(f"as_at must be in FY{fy} ({start:%d/%m/%Y} - {end:%d/%m/%Y})")
    tb = L.trial_balance(fy, None if upto == end else upto)
    return {"client": L.client.name, "fy": fy, "as_at": upto.isoformat(),
            "rows": [{"code": r["code"], "name": r["name"], "type": r["type"], "debit": _money(r["debit"]) if r["debit"] else "",
                      "credit": _money(r["credit"]) if r["credit"] else ""} for r in tb["rows"]],
            "total_debit": as_str(tb["total_debit"]), "total_credit": as_str(tb["total_credit"]),
            "net_profit_after_tax": as_str(tb["profit"]), "balanced": tb["difference"] == 0}


def t_list_journals(ws: Workspace, args: dict) -> dict:
    L = ws.ledger(args.get("client"))
    fy = _year(L, args.get("fy"))
    status = args.get("status") or "all"
    if status not in ("all", "draft", "posted", "void"):
        raise ToolError("status is all, draft, posted or void")
    q = str(args.get("search") or "").strip().lower()
    try:
        limit = max(1, min(int(args.get("limit") or 50), 500))
    except (TypeError, ValueError):
        raise ToolError("limit is a number of journals, e.g. 50") from None
    js = L.journals(fy, statuses=None, bank=bool(args.get("include_bank_transactions")))
    js = [j for j in js if status == "all" or j.status == status]
    if q:
        js = [j for j in js if q in j.narration.lower() or q in j.number.lower() or q in (j.reference or "").lower()
              or any(q == ln.account.lower() or q in ln.description.lower() for ln in j.lines)]
    js.sort(key=lambda j: (j.date, j.number), reverse=True)
    rows = [{"number": j.number, "date": j.date.isoformat(), "narration": j.narration, "reference": j.reference,
             "kind": j.kind, "status": j.status, "total": as_str(j.total_debit),
             "made_by": (j.created or {}).get("by", ""), **({"via": "Claude"} if (j.created or {}).get("via") else {})}
            for j in js[:limit]]
    return {"client": L.client.name, "fy": fy, "journals": rows, "shown": len(rows), "matching": len(js)}


def t_get_journal(ws: Workspace, args: dict) -> dict:
    L = ws.ledger(args.get("client"))
    j, fy = _get_journal(L, args.get("number"), args.get("fy"))
    return {"client": L.client.name, **_journal_out(L, j, fy)}


def t_account_transactions(ws: Workspace, args: dict) -> dict:
    L = ws.ledger(args.get("client"))
    a = _account(L, args.get("account"), posting=False) if args.get("account") else None
    if a is None:
        raise ToolError("Say which account (its code).")
    fy = _year(L, args.get("fy"))
    s, e = L.fy_range(fy)
    start = _date(args["from"], "from") if args.get("from") else s
    end = _date(args["to"], "to") if args.get("to") else e
    if not (s <= start <= end <= e):
        raise ToolError(f"from and to must be in FY{fy} ({s:%d/%m/%Y} - {e:%d/%m/%Y}), from before to")
    data = L.general_ledger(fy, start, end, [a.code])
    acc = data[0] if data else {"opening": ZERO, "entries": [], "closing": ZERO, "debits": ZERO, "credits": ZERO}
    entries = acc["entries"]
    shown = entries[-500:]
    return {"client": L.client.name, "account": a.code, "name": a.name, "fy": fy, "from": start.isoformat(),
            "to": end.isoformat(), "opening_balance": as_str(acc["opening"]),
            "transactions": [{"date": x["date"].isoformat(), "number": x["number"], "narration": x["narration"],
                              "description": x["description"], "debit": _money(x["debit"]) if x["debit"] else "",
                              "credit": _money(x["credit"]) if x["credit"] else "", "balance": as_str(x["balance"])}
                             for x in shown],
            "closing_balance": as_str(acc["closing"]), "total_debits": as_str(acc["debits"]),
            "total_credits": as_str(acc["credits"]), "shown": len(shown), "count": len(entries),
            "sign": "balances are debit positive, credit negative"}


def t_profit_and_loss(ws: Workspace, args: dict) -> dict:
    L = ws.ledger(args.get("client"))
    s, e = L.fy_range(_year(L))
    start = _date(args["from"], "from") if args.get("from") else s
    end = _date(args["to"], "to") if args.get("to") else e
    data = statements.profit_and_loss(L, start, end, args.get("compare", True) is not False)
    return {"client": L.client.name, "title": data["title"], "period": data["period"], "columns": data["columns"],
            "rows": _rows(data), "net_profit": as_str(data["net_profit"])}


def t_balance_sheet(ws: Workspace, args: dict) -> dict:
    L = ws.ledger(args.get("client"))
    as_at = _date(args["as_at"], "as_at") if args.get("as_at") else L.fy_range(_year(L))[1]
    data = statements.balance_sheet(L, as_at, args.get("compare", True) is not False)
    return {"client": L.client.name, "title": data["title"], "period": data["period"], "columns": data["columns"],
            "rows": _rows(data), "net_assets": as_str(data["net_assets"]), "total_equity": as_str(data["total_equity"])}


def t_gst_report(ws: Workspace, args: dict) -> dict:
    L = ws.ledger(args.get("client"))
    s, e = L.fy_range(_year(L))
    start = _date(args["from"], "from") if args.get("from") else s
    end = _date(args["to"], "to") if args.get("to") else e
    data = statements.gst_report(L, start, end, args.get("basis") or "accrual")
    lines = data["lines"]
    return {"client": L.client.name, "period": data["period"], "basis": data["basis_label"],
            "bas_labels": [{"label": k, "description": d, "amount": as_str(v)} for k, d, v in data["labels"]],
            "net_gst": as_str(data["net_gst"]), "payable_or_refundable": "payable" if data["net_gst"] >= 0 else "refundable",
            "by_tax_rate": [{"tax_rate": r["tax"], "side": r["side"], "net": as_str(r["net"]), "gst": as_str(r["gst"]),
                             "gross": as_str(r["gross"]), "transactions": r["count"]} for r in data["rates"]],
            "transactions": [{"date": x["date"].isoformat(), "number": x["number"], "account": x["account"],
                              "description": x["text"], "tax_rate": x["tax"], "net": as_str(x["net"]),
                              "gst": as_str(x["gst"]), "gross": as_str(x["gross"])} for x in lines[:200]],
            "transactions_shown": min(len(lines), 200), "transaction_count": len(lines),
            "journal_lines_left_out": data["left_out"], "gst_registered": data["registered"]}


def t_create_journal(ws: Workspace, args: dict) -> dict:
    L = ws.ledger(args.get("client"))
    if not args.get("date"):
        raise ToolError("date is required, e.g. 2026-06-30")
    d = _date(args.get("date"))
    fy = L.client.fy_of(d)
    if fy not in L.years():
        started = ", ".join(f"FY{y}" for y in L.years()) or "none"
        raise ToolError(f"{d:%d/%m/%Y} is in FY{fy}, which has not been started for {L.client.name} "
                        f"(started: {started}). A person starts it in Fortis Ledger (Overview, or roll forward at Year end).")
    narration = str(args.get("narration") or "").strip()
    if not narration:
        raise ToolError("narration is required - say what the journal is for")
    raw = args.get("lines")
    if not isinstance(raw, list) or len(raw) < 2:
        raise ToolError("lines: a journal needs at least two lines, each with an account and a debit or a credit")
    amounts = args.get("amounts") or "inclusive"
    if amounts not in ("inclusive", "exclusive"):
        raise ToolError("amounts is 'inclusive' (GST included in the amounts, the default) or 'exclusive'")
    lines = []
    for i, x in enumerate(raw, 1):
        if not isinstance(x, dict):
            raise ToolError(f"line {i}: each line is an object with account, debit or credit, description, tax_rate")
        a = _account(L, x.get("account"))
        dr, cr = _amount(x.get("debit"), f"line {i} debit"), _amount(x.get("credit"), f"line {i} credit")
        if dr and cr:
            raise ToolError(f"line {i}: give a debit or a credit, not both")
        if not dr and not cr:
            raise ToolError(f"line {i}: the amount is nil")
        tax = str(x.get("tax_rate") or "").strip()
        if tax.lower() in ("no gst", "none", "n/a"):
            tax = ""
        if tax and tax not in bankmod.TAX_RATES:
            raise ToolError(f"line {i}: tax_rate {tax!r} is not a tax rate - use one of {', '.join(bankmod.TAX_CHOICES)}")
        lines.append(Line(a.code, debit=dr, credit=cr, description=str(x.get("description") or "").strip(), tax=tax))
    j = Journal(date=d, narration=narration[:300], reference=str(args.get("reference") or "").strip()[:100],
                lines=lines, amounts="exclusive" if amounts == "exclusive" else "")
    try:
        if any(ln.tax for ln in j.lines):
            L.apply_gst(j)
    except ValidationError as e:
        raise ToolError("The journal was not saved: " + "; ".join(e.errors)) from None
    problems = L.validate(j, fy)
    if problems:
        raise ToolError("The journal was not saved: " + "; ".join(problems))
    wants_post = bool(args.get("post"))
    post = wants_post and ws.can_post
    with ws.editing(L):
        j = L.save_journal(j, fy, post=post)
    out = {"client": L.client.name, **_journal_out(L, j, fy), "where": _where(L, j.number)}
    if wants_post and not post:
        out["note"] = ("Saved as a draft: this firm has turned off posting by Claude (Fortis Ledger > Firm settings > "
                       "Claude). A person can check it and post it in Fortis Ledger.")
    elif not post:
        out["note"] = "Saved as a draft - it does not affect any balance until it is posted."
    return out


def t_post_journal(ws: Workspace, args: dict) -> dict:
    L = ws.ledger(args.get("client"))
    j, fy = _get_journal(L, args.get("number"), args.get("fy"))
    if not ws.can_post:
        raise ToolError("This firm has turned off posting by Claude (Fortis Ledger > Firm settings > Claude). "
                        f"A person can post {j.number} in Fortis Ledger.")
    if j.status != "draft":
        raise ToolError(f"{j.number} is {j.status}, not a draft.")
    if j.kind not in EDITABLE_KINDS:
        raise ToolError(f"{j.number} was made by an import or the year-end close - it is not posted from here.")
    try:
        with ws.editing(L):
            j = L.save_journal(j, fy, post=True)
    except ValidationError as e:
        raise ToolError(f"{j.number} was not posted: " + "; ".join(e.errors)) from None
    return {"client": L.client.name, **_journal_out(L, j, fy), "where": _where(L, j.number)}


def t_delete_draft(ws: Workspace, args: dict) -> dict:
    L = ws.ledger(args.get("client"))
    j, fy = _get_journal(L, args.get("number"), args.get("fy"))
    if j.status != "draft":
        raise ToolError(f"{j.number} is {j.status} - only drafts can be deleted (a posted journal is voided by a person).")
    if (j.created or {}).get("via") != AGENT:
        raise ToolError(f"{j.number} was drafted by {(j.created or {}).get('by') or 'a person'}, not Claude - "
                        f"only drafts Claude made can be deleted from here.")
    with ws.editing(L):
        L.delete_draft(fy, j.number)
    return {"client": L.client.name, "deleted": j.number, "fy": fy, "narration": j.narration}


def t_add_account(ws: Workspace, args: dict) -> dict:
    L = ws.ledger(args.get("client"))
    acct_type = str(args.get("type") or "").strip()
    if acct_type not in ACCOUNT_TYPES:
        raise ToolError(f"type is one of {', '.join(ACCOUNT_TYPES)}")
    rc = str(args.get("report_code") or "").strip().split(" ")[0] or default_report_code(acct_type)
    tax = str(args.get("tax_rate") or "").strip()
    if tax and tax not in bankmod.TAX_RATES:
        raise ToolError(f"tax_rate is one of {', '.join(bankmod.TAX_CHOICES)}")
    tax = tax or bankmod.tax_for_type(acct_type, L.client.gst_registered)
    with ws.editing(L):
        a = L.chart.add(str(args.get("code") or ""), str(args.get("name") or ""), rc, acct_type, tax_code=tax)
        L.store.audit(L.slug, "account.added", {"code": a.code, "name": a.name, "report_code": a.report_code,
                                                "type": a.type, "tax": a.tax_code})
    return {"client": L.client.name, "added": {"code": a.code, "name": a.name, "type": a.type,
                                               "report_code": a.report_code, "tax_rate": bankmod.default_tax(a)}}


@dataclass
class Tool:
    name: str
    title: str
    description: str
    properties: dict
    required: list
    handler: Callable[[Workspace, dict], dict]
    read_only: bool = True
    destructive: bool = False

    def spec(self) -> dict:
        schema = {"type": "object", "properties": self.properties, "additionalProperties": False}
        if self.required:
            schema["required"] = self.required
        return {"name": self.name, "title": self.title, "description": self.description, "inputSchema": schema,
                "annotations": {"title": self.title, "readOnlyHint": self.read_only,
                                "destructiveHint": self.destructive, "idempotentHint": self.read_only,
                                "openWorldHint": False}}


CLIENT = {"type": "string", "description": "The client: its id from list_clients, or its name (part of the name is "
                                           "enough when only one client matches)."}
FY = {"type": "integer", "description": "Financial year, named by the year it ends (2026 = the year ending in 2026). "
                                        "Default: the latest year started."}
DATE = "A date, 2026-06-30 or 30/06/2026"
AMOUNT = {"type": ["number", "string"], "description": "A positive amount, e.g. 1250.50"}

TOOLS = [
    Tool("list_clients", "List clients", "The firm's clients in Fortis Ledger: id, name, entity type, ABN, year end, the "
         "financial years started, whether registered for GST, and whether someone is editing the client now.",
         {"search": {"type": "string", "description": "Part of a client's name or ABN (optional)."}}, [], t_list_clients),
    Tool("get_chart_of_accounts", "Chart of accounts", "A client's chart of accounts: code, name, Xero account type, "
         "report code and, for GST-registered clients, the account's usual tax rate. Give fy to include each account's "
         "closing balance. Use it to find the right account code before creating a journal.",
         {"client": CLIENT, "search": {"type": "string", "description": "A code or words in the account name, type or "
                                                                        "report code (optional)."}, "fy": FY},
         ["client"], t_chart),
    Tool("get_trial_balance", "Trial balance", "A client's trial balance for a financial year (or to a date in it), "
         "with net profit after tax.", {"client": CLIENT, "fy": FY, "as_at": {"type": "string", "description": DATE}},
         ["client"], t_trial_balance),
    Tool("list_journals", "List journals", "A client's journals in a financial year, newest first: number, date, "
         "narration, type, status (draft, posted, void) and total. Bank transactions from cash coding are left out "
         "unless include_bank_transactions is true.",
         {"client": CLIENT, "fy": FY, "status": {"type": "string", "enum": ["all", "draft", "posted", "void"]},
          "search": {"type": "string", "description": "Words in the narration, reference or a line, a journal number, "
                                                      "or an account code."},
          "include_bank_transactions": {"type": "boolean"},
          "limit": {"type": "integer", "minimum": 1, "maximum": 500, "description": "Default 50."}},
         ["client"], t_list_journals),
    Tool("get_journal", "Get a journal", "One journal with its lines (accounts, debits, credits, tax rates and the GST "
         "lines), who made it and its status.",
         {"client": CLIENT, "number": {"type": "string", "description": "The journal number, e.g. J2026-0004 or "
                                                                        "B2026-0012 (a bank transaction)."}, "fy": FY},
         ["client", "number"], t_get_journal),
    Tool("get_account_transactions", "Account transactions", "Every transaction in one account for a period in a "
         "financial year (the general ledger detail), with the opening and closing balance.",
         {"client": CLIENT, "account": {"type": "string", "description": "The account code."}, "fy": FY,
          "from": {"type": "string", "description": DATE}, "to": {"type": "string", "description": DATE}},
         ["client", "account"], t_account_transactions),
    Tool("get_profit_and_loss", "Profit and loss", "A client's Profit and Loss for a period (default: the latest "
         "financial year), laid out like Xero's, with the same period a year earlier.",
         {"client": CLIENT, "from": {"type": "string", "description": DATE}, "to": {"type": "string", "description": DATE},
          "compare": {"type": "boolean", "description": "Include the year before (default true)."}},
         ["client"], t_profit_and_loss),
    Tool("get_balance_sheet", "Balance sheet", "A client's Balance Sheet at a date (default: the end of the latest "
         "financial year), with the same date a year earlier.",
         {"client": CLIENT, "as_at": {"type": "string", "description": DATE},
          "compare": {"type": "boolean", "description": "Include a year earlier (default true)."}},
         ["client"], t_balance_sheet),
    Tool("get_gst_report", "GST report", "The GST report for a period: the BAS labels (G1, G3, G4, G10, G11, 1A, 1B), "
         "GST by tax rate and the transactions behind them. Cash basis counts bank transactions; accrual basis also "
         "counts journals with a tax rate.",
         {"client": CLIENT, "from": {"type": "string", "description": DATE}, "to": {"type": "string", "description": DATE},
          "basis": {"type": "string", "enum": ["accrual", "cash"]}},
         ["client"], t_gst_report),
    Tool("create_journal", "Create a journal", "Create a manual journal in a client's ledger. Each line has an account "
         "code from the client's chart and a debit or a credit (positive amounts; debits must equal credits). The date "
         "must be in a financial year that has been started and is not locked. For a GST-registered client a line can "
         "have a tax_rate such as 'GST on Expenses': the GST is split out to the GST account automatically, from GST "
         "inclusive amounts unless amounts is 'exclusive'. Set post to true to post it straight away - when the user "
         "asks for the journal to be posted, entered or put through; leave it false to save a draft for a person to "
         "check first. If the firm has turned off posting by Claude, it is saved as a draft either way. Returns the "
         "journal and its number.",
         {"client": CLIENT, "date": {"type": "string", "description": DATE},
          "narration": {"type": "string", "description": "What the journal is for, e.g. 'Depreciation for FY2026'."},
          "reference": {"type": "string"},
          "amounts": {"type": "string", "enum": ["inclusive", "exclusive"],
                      "description": "Whether lines with a GST tax rate include the GST (default inclusive)."},
          "lines": {"type": "array", "minItems": 2, "items": {
              "type": "object", "additionalProperties": False, "required": ["account"],
              "properties": {"account": {"type": "string", "description": "Account code from the client's chart."},
                             "description": {"type": "string"}, "debit": AMOUNT, "credit": AMOUNT,
                             "tax_rate": {"type": "string", "enum": bankmod.TAX_CHOICES,
                                          "description": "GST-registered clients only; leave out for no GST."}}}},
          "post": {"type": "boolean", "description": "true: post it now. false (the default): save a draft. The firm "
                                                     "lets Claude post unless a partner has turned it off."}},
         ["client", "date", "narration", "lines"], t_create_journal, read_only=False),
    Tool("post_journal", "Post a draft journal", "Post a draft journal so it affects the balances (unless the firm has "
         "turned off posting by Claude). It is checked first (balanced, accounts, year open).",
         {"client": CLIENT, "number": {"type": "string", "description": "The draft's number, e.g. J2026-0004."}, "fy": FY},
         ["client", "number"], t_post_journal, read_only=False),
    Tool("delete_draft_journal", "Delete a draft journal", "Delete a draft journal that Claude made (for example one "
         "with a mistake, before creating it again). Posted journals and drafts made by people cannot be deleted here.",
         {"client": CLIENT, "number": {"type": "string", "description": "The draft's number."}, "fy": FY},
         ["client", "number"], t_delete_draft, read_only=False, destructive=True),
    Tool("add_account", "Add an account", "Add an account to a client's chart of accounts: Xero account type, account "
         "code (number) and name. The report code (where it prints in the Financial Reports) follows the type unless "
         "given; the tax rate follows the type for GST-registered clients unless given.",
         {"client": CLIENT, "type": {"type": "string", "enum": ACCOUNT_TYPES},
          "code": {"type": "string", "description": "Up to 10 letters, digits, '.' or '-', e.g. 61150."},
          "name": {"type": "string"}, "tax_rate": {"type": "string", "enum": bankmod.TAX_CHOICES},
          "report_code": {"type": "string", "description": "A Xero report code such as EXP.ADM (optional)."}},
         ["client", "type", "code", "name"], t_add_account, read_only=False),
]
BY_NAME = {t.name: t for t in TOOLS}


# ---------------------------------------------------------------------------------------- #
# the protocol: JSON-RPC 2.0, one message per line on stdin / stdout
# ---------------------------------------------------------------------------------------- #
def _ok(rid, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": rid, "result": result}


def _error(rid, code: int, message: str, data=None) -> dict:
    err = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": rid, "error": err}


class Connector:
    """Answers MCP messages.  `settings` is for tests; otherwise this PC's Fortis Ledger settings are read each time."""

    def __init__(self, settings: Settings | None = None):
        self._settings = settings
        self.legacy_version: str | None = None      # set by an initialize handshake (earlier MCP revisions)

    def settings(self) -> Settings:
        return self._settings or Settings.load()

    def handle(self, msg):
        if isinstance(msg, list):                   # a JSON-RPC batch (allowed by the 2025-03-26 revision)
            out = [r for r in (self._one(m) for m in msg) if r is not None]
            return out or None
        return self._one(msg)

    def _one(self, msg) -> dict | None:
        if not isinstance(msg, dict):
            return _error(None, INVALID_REQUEST, "Invalid request")
        if "method" not in msg:
            return None                              # a response from the client - this server sends no requests
        rid = msg.get("id")
        if "id" not in msg:
            return None                              # notifications (initialized, cancelled ...) need no answer
        if msg.get("jsonrpc") != "2.0" or not isinstance(msg.get("method"), str) or rid is None \
                or isinstance(rid, bool) or not isinstance(rid, (str, int)):
            return _error(rid if isinstance(rid, (str, int)) and not isinstance(rid, bool) else None,
                          INVALID_REQUEST, "Invalid request")
        method = msg["method"]
        params = msg.get("params") if msg.get("params") is not None else {}
        if not isinstance(params, dict):
            return _error(rid, INVALID_PARAMS, "params must be an object")
        meta = params.get("_meta") if isinstance(params.get("_meta"), dict) else {}
        modern = META_VERSION in meta
        try:
            if method == "initialize":
                return _ok(rid, self._initialize(params))
            if modern:
                version = meta.get(META_VERSION)
                if version not in MODERN_VERSIONS:
                    return _error(rid, UNSUPPORTED_VERSION, "Unsupported protocol version",
                                  {"supported": list(SUPPORTED_VERSIONS), "requested": version})
                if not isinstance(meta.get(META_CAPABILITIES), dict):
                    return _error(rid, INVALID_PARAMS, f"_meta must include {META_CAPABILITIES}")
            elif method == "server/discover":
                return _error(rid, INVALID_PARAMS, f"server/discover needs _meta {META_VERSION} and {META_CAPABILITIES}")
            result = self._dispatch(method, params, modern)
        except RpcError as e:
            return _error(rid, e.code, e.message, e.data)
        except Exception:                            # never let one bad request stop the connector
            log.exception("request %s failed", method)
            return _error(rid, INTERNAL_ERROR, "Fortis Ledger hit an unexpected error - see claude-connector.log")
        if modern:
            result = {"resultType": "complete", **result, "_meta": {META_SERVER: SERVER_INFO}}
        return _ok(rid, result)

    def _initialize(self, params: dict) -> dict:
        wanted = params.get("protocolVersion")
        self.legacy_version = wanted if wanted in LEGACY_VERSIONS else LEGACY_VERSIONS[0]
        return {"protocolVersion": self.legacy_version, "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": SERVER_INFO, "instructions": INSTRUCTIONS}

    def _dispatch(self, method: str, params: dict, modern: bool) -> dict:
        # the 2026-07-28 revision requires caching directives on these results; the tools never change while
        # the connector runs, and "private" keeps any shared cache from reusing them
        cache = {"ttlMs": CACHE_MS, "cacheScope": "private"} if modern else {}
        if method == "server/discover":
            return {"supportedVersions": list(SUPPORTED_VERSIONS), "capabilities": {"tools": {}},
                    "instructions": INSTRUCTIONS, **cache}
        if method == "ping":
            return {}
        if method == "tools/list":
            return {"tools": [t.spec() for t in TOOLS], **cache}
        if method == "tools/call":
            tool = BY_NAME.get(params.get("name"))
            if tool is None:
                raise RpcError(INVALID_PARAMS, f"Unknown tool: {params.get('name')}")
            args = params.get("arguments") if params.get("arguments") is not None else {}
            if not isinstance(args, dict):
                raise RpcError(INVALID_PARAMS, "arguments must be an object")
            return self._call(tool, args, structured=modern or (self.legacy_version or "") >= "2025-06-18")
        raise RpcError(METHOD_NOT_FOUND, f"Method not found: {method}")

    def _call(self, tool: Tool, args: dict, structured: bool) -> dict:
        try:
            data = tool.handler(Workspace(self.settings()), args)
        except ToolError as e:
            return {"content": [{"type": "text", "text": str(e)}], "isError": True}
        except (LedgerError, ChartError, StoreError, statements.StatementError, bankmod.BankError) as e:
            return {"content": [{"type": "text", "text": f"Fortis Ledger did not do that: {e}"}], "isError": True}
        except Exception as e:
            log.exception("tool %s failed", tool.name)
            return {"content": [{"type": "text", "text": f"Fortis Ledger could not do that ({type(e).__name__}: {e})."}],
                    "isError": True}
        log.info("%s for %s", tool.name, str(args.get("client") or "-")[:80])
        out = {"content": [{"type": "text", "text": json.dumps(data, ensure_ascii=False, indent=1, default=str)}],
               "isError": False}
        if structured:
            out["structuredContent"] = data
        return out


def _setup_logging() -> None:
    try:
        d = config_dir()
        d.mkdir(parents=True, exist_ok=True)
        h = logging.handlers.RotatingFileHandler(d / "claude-connector.log", maxBytes=500_000, backupCount=2,
                                                 encoding="utf-8")
        h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        root = logging.getLogger()
        root.setLevel(logging.INFO)
        root.addHandler(h)
    except OSError:
        pass


def serve(stdin=None, stdout=None, settings: Settings | None = None) -> int:
    """Answer MCP messages on stdin / stdout until Claude closes stdin."""
    inp = stdin if stdin is not None else (sys.stdin.buffer if sys.stdin else None)
    out = stdout if stdout is not None else (sys.stdout.buffer if sys.stdout else None)
    if inp is None or out is None:
        return 2                                  # started without a channel (e.g. from a desktop icon)
    if stdout is None:
        sys.stdout = sys.stderr                   # a stray print must never reach Claude's channel
    _setup_logging()
    log.info("Fortis Ledger %s Claude connector started", __version__)
    conn = Connector(settings)
    for raw in inp:
        line = (raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw).strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            reply = _error(None, PARSE_ERROR, "Parse error")
        else:
            reply = conn.handle(msg)
        if reply is not None:
            data = json.dumps(reply, ensure_ascii=False, separators=(",", ":"), default=str) + "\n"
            out.write(data.encode("utf-8") if not hasattr(out, "encoding") else data)
            out.flush()
    log.info("Claude connector stopped")
    return 0
