"""
Ledger - the accounting rules for one client.

* Journals are double entry: debits equal credits to the cent, every line posts to an account in
  the client's chart, the date falls inside the financial year, and a locked year cannot change.
* Balances work like Xero: balance-sheet accounts carry forward from year to year, profit and loss
  accounts start each year at nil and last year's result is added to retained earnings.
* Year-end close posts one journal on the first day of the next year that moves the year's
  movement accounts into the accounts that accumulate them: each partner's / beneficiary's
  Drawings, Funds contributed and Share of profits into their Opening balance account, a
  company's dividends (equity) into retained earnings, a sole trader's drawings and funds
  introduced into retained earnings.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
from collections import defaultdict
from decimal import Decimal

from .chartbook import ChartError, ClientChart, code_sort_key, is_pl_account
from .engine import nature, slot_of
from .model import Journal, Line, now_iso, parse_date
from .money import ZERO, as_str, q2
from .store import FolderStore, StoreError

PERSON_RE = re.compile(r"^(EQU\.(?:ADV|DRA|PFT|CAP|UDE))\.(\d+)$")


class LedgerError(Exception):
    pass


class ValidationError(LedgerError):
    def __init__(self, errors: list[str]):
        super().__init__("; ".join(errors))
        self.errors = errors


class Ledger:
    def __init__(self, store: FolderStore, slug: str):
        self.store = store
        self.slug = slug
        self.client = store.load_client(slug)
        self.chart = ClientChart(store.chart_path(slug), self.client.type)
        self._closing_cache: dict = {}

    # ---- accounts ----------------------------------------------------------------------- #
    def account(self, code: str):
        return self.chart.get(code)

    def is_pl(self, code: str) -> bool:
        a = self.chart.get(code)
        return bool(a) and is_pl_account(a.report_code, a.type)

    def is_appropriation(self, code: str) -> bool:
        """Dividends and trust / partnership distributions coded to the P&L - they print below Net Profit After Tax."""
        a = self.chart.get(code)
        if not a or not is_pl_account(a.report_code, a.type):
            return False
        return (slot_of(a.report_code, a.name) or "").startswith(("EQU.DIV", "EXP.DIS"))

    def retained_earnings_code(self, create: bool = True) -> str:
        c = self.client.retained_earnings_account
        if c and self.chart.get(c):
            return c
        cands = [a for a in self.chart.usable() if a.report_code == "EQU.RET"]
        cands.sort(key=lambda a: (0 if re.search(r"retained|accumulated", a.name, re.I) else 1,
                                  0 if not re.search(r"opening|prior", a.name, re.I) else 1, code_sort_key(a.code)))
        if cands:
            return cands[0].code
        if not create:
            return ""
        code = "43000" if not self.chart.get("43000") else self.chart.next_code("43000")
        self.chart.add(code, "Retained Earnings", "EQU.RET", "Equity")
        self.store.audit(self.slug, "account.added", {"code": code, "name": "Retained Earnings", "report_code": "EQU.RET",
                                                      "why": "needed to carry profit forward"})
        return code

    def rounding_code(self) -> str:
        c = self.client.rounding_account
        if c and self.chart.get(c):
            return c
        cands = [a for a in self.chart.usable() if re.search(r"rounding", a.name, re.I)]
        if cands:
            return sorted(cands, key=lambda a: code_sort_key(a.code))[0].code
        code = "81710" if not self.chart.get("81710") else self.chart.next_code("81710")
        self.chart.add(code, "Rounding", "REV.OTH", "Other Income")
        self.store.audit(self.slug, "account.added", {"code": code, "name": "Rounding", "report_code": "REV.OTH"})
        return code

    # ---- years -------------------------------------------------------------------------- #
    def years(self) -> list[int]:
        return self.store.list_years(self.slug)

    def fy_range(self, fy: int) -> tuple[dt.date, dt.date]:
        return self.client.fy_range(fy)

    def default_fy(self) -> int:
        ys = self.years()
        return ys[-1] if ys else self.client.fy_of(dt.date.today())

    def ensure_year(self, fy: int) -> None:
        self.store.ensure_year(self.slug, fy)

    def year_state(self, fy: int) -> dict:
        return self.store.year_state(self.slug, fy)

    def is_locked(self, fy: int) -> bool:
        return bool(self.year_state(fy).get("locked"))

    def lock_year(self, fy: int, reason: str = "") -> None:
        self.store.require_lock(self.slug)
        st = self.year_state(fy)
        st["locked"] = True
        st.setdefault("history", []).append({"action": "locked", "by": self.store.user, "at": now_iso(), "reason": reason})
        self.store.save_year_state(self.slug, fy, st)
        self.store.audit(self.slug, "year.locked", {"fy": fy, "reason": reason})

    def unlock_year(self, fy: int, reason: str) -> None:
        if not (reason or "").strip():
            raise LedgerError("give a reason for unlocking a finalised year")
        self.store.require_lock(self.slug)
        st = self.year_state(fy)
        st["locked"] = False
        st.setdefault("history", []).append({"action": "unlocked", "by": self.store.user, "at": now_iso(), "reason": reason})
        self.store.save_year_state(self.slug, fy, st)
        self.store.audit(self.slug, "year.unlocked", {"fy": fy, "reason": reason})

    # ---- journals ----------------------------------------------------------------------- #
    def journals(self, fy: int, statuses=("posted",)) -> list[Journal]:
        js = self.store.list_journals(self.slug, fy)
        return [j for j in js if statuses is None or j.status in statuses]

    def get_journal(self, fy: int, number: str) -> Journal:
        return self.store.load_journal(self.slug, fy, number)

    def validate(self, j: Journal, fy: int) -> list[str]:
        errors = []
        start, end = self.fy_range(fy)
        if not (start <= j.date <= end):
            errors.append(f"date {j.date:%d/%m/%Y} is outside FY{fy} ({start:%d/%m/%Y} - {end:%d/%m/%Y})")
        if not (j.narration or "").strip():
            errors.append("narration is required")
        lines = [ln for ln in j.lines if ln.account or ln.debit or ln.credit]
        if len(lines) < 2:
            errors.append("a journal needs at least two lines")
        for i, ln in enumerate(lines, 1):
            a = self.chart.get(ln.account)
            if not ln.account:
                errors.append(f"line {i}: choose an account")
            elif a is None:
                errors.append(f"line {i}: account {ln.account} is not in the chart")
            elif a.system:
                errors.append(f"line {i}: {a.code} {a.name} is a Xero system account - post to another account")
            if ln.debit < 0 or ln.credit < 0:
                errors.append(f"line {i}: amounts cannot be negative - use the other column")
            if ln.debit and ln.credit:
                errors.append(f"line {i}: enter a debit or a credit, not both")
            if not ln.debit and not ln.credit:
                errors.append(f"line {i}: amount is nil")
        dr = sum((ln.debit for ln in lines), ZERO)
        cr = sum((ln.credit for ln in lines), ZERO)
        if dr != cr:
            errors.append(f"debits {dr:,.2f} and credits {cr:,.2f} differ by {abs(dr - cr):,.2f}")
        if self.is_locked(fy):
            errors.append(f"FY{fy} is locked (finalised) - unlock it first")
        return errors

    def save_journal(self, j: Journal, fy: int | None = None, post: bool = True, action: str | None = None) -> Journal:
        """Save a draft (post=False) or post a journal.  Raises ValidationError when posting fails the rules."""
        self.store.require_lock(self.slug)
        fy = fy or self.client.fy_of(j.date)
        j.lines = [ln for ln in j.lines if ln.account or ln.debit or ln.credit]
        for ln in j.lines:
            ln.debit, ln.credit = q2(ln.debit), q2(ln.credit)
        if self.is_locked(fy):
            raise ValidationError([f"FY{fy} is locked (finalised) - unlock it first"])
        existing = None
        if j.number:
            try:
                existing = self.get_journal(fy, j.number)
            except StoreError:
                existing = None
        if existing and existing.status == "void":
            raise ValidationError([f"{j.number} is void and cannot be changed"])
        if post:
            errors = self.validate(j, fy)
            if errors:
                raise ValidationError(errors)
        stamp = {"by": self.store.user, "at": now_iso()}
        if existing:
            j.uid, j.created = existing.uid, existing.created
            j.updated = stamp
        else:
            j.created = stamp
        j.status = "posted" if post else "draft"
        self.store.save_journal(self.slug, fy, j)
        self._closing_cache.clear()
        act = action or ("journal.updated" if existing else ("journal.posted" if post else "journal.draft_saved"))
        detail = {"fy": fy, "number": j.number, "date": j.date.isoformat(), "narration": j.narration,
                  "status": j.status, "kind": j.kind, "total": as_str(j.total_debit),
                  "lines": [[ln.account, as_str(ln.debit), as_str(ln.credit)] for ln in j.lines]}
        if existing:
            detail["before"] = {"date": existing.date.isoformat(), "narration": existing.narration,
                                "status": existing.status,
                                "lines": [[ln.account, as_str(ln.debit), as_str(ln.credit)] for ln in existing.lines]}
        self.store.audit(self.slug, act, detail)
        return j

    def void_journal(self, fy: int, number: str, reason: str) -> Journal:
        if not (reason or "").strip():
            raise LedgerError("give a reason for voiding the journal")
        self.store.require_lock(self.slug)
        if self.is_locked(fy):
            raise ValidationError([f"FY{fy} is locked (finalised) - unlock it first"])
        j = self.get_journal(fy, number)
        if j.status == "void":
            return j
        j.status = "void"
        j.voided = {"by": self.store.user, "at": now_iso(), "reason": reason}
        self.store.save_journal(self.slug, fy, j)
        self._closing_cache.clear()
        self.store.audit(self.slug, "journal.voided", {"fy": fy, "number": number, "reason": reason})
        return j

    def delete_draft(self, fy: int, number: str) -> None:
        self.store.require_lock(self.slug)
        j = self.get_journal(fy, number)
        if j.status != "draft":
            raise LedgerError("only drafts can be deleted - void a posted journal instead")
        (self.store.journal_dir(self.slug, fy) / f"{number}.json").unlink()
        self.store.audit(self.slug, "journal.draft_deleted", {"fy": fy, "number": number, "narration": j.narration})

    def reverse_journal(self, fy: int, number: str, on: dt.date | None = None) -> Journal:
        orig = self.get_journal(fy, number)
        if orig.status != "posted":
            raise LedgerError("only posted journals can be reversed")
        on = on or (self.fy_range(fy + 1)[0] if orig.date == self.fy_range(fy)[1] else orig.date)
        rev = Journal(date=on, narration=f"Reversal of {orig.number}: {orig.narration}", kind="reversal",
                      lines=[Line(ln.account, debit=ln.credit, credit=ln.debit, description=ln.description)
                             for ln in orig.lines], reverses=orig.number)
        rfy = self.client.fy_of(on)
        self.ensure_year(rfy)
        rev = self.save_journal(rev, rfy, post=True, action="journal.reversed")
        orig.reversed_by = rev.number
        self.store.save_journal(self.slug, fy, orig)
        return rev

    # ---- balances ----------------------------------------------------------------------- #
    def movements(self, fy: int, upto: dt.date | None = None, since: dt.date | None = None) -> dict[str, Decimal]:
        out: dict[str, Decimal] = defaultdict(lambda: ZERO)
        for j in self.journals(fy):
            if upto and j.date > upto:
                continue
            if since and j.date < since:
                continue
            for ln in j.lines:
                out[ln.account] += ln.amount
        return {k: v for k, v in out.items() if v != 0}

    def opening(self, fy: int) -> dict[str, Decimal]:
        """Balances at the start of `fy`: balance-sheet accounts carried forward, profit to retained earnings."""
        earlier = [y for y in self.years() if y < fy]
        if not earlier:
            return {}
        prev = max(earlier)
        out: dict[str, Decimal] = defaultdict(lambda: ZERO)
        re_code = None
        for code, bal in self.closing(prev).items():
            if self.is_pl(code):
                re_code = re_code or self.retained_earnings_code()
                out[re_code] += bal
            else:
                out[code] += bal
        return {k: v for k, v in out.items() if v != 0}

    def closing(self, fy: int, upto: dt.date | None = None) -> dict[str, Decimal]:
        key = (fy, upto)
        if key not in self._closing_cache:
            out: dict[str, Decimal] = defaultdict(lambda: ZERO)
            for src in (self.opening(fy), self.movements(fy, upto)):
                for k, v in src.items():
                    out[k] += v
            self._closing_cache[key] = {k: v for k, v in out.items() if v != 0}
        return dict(self._closing_cache[key])

    def trial_balance(self, fy: int, upto: dt.date | None = None) -> dict:
        bal = self.closing(fy, upto)
        rows = []
        for code in sorted(bal, key=code_sort_key):
            a = self.chart.get(code)
            v = bal[code]
            rows.append({"code": code, "name": a.name if a else "(not in chart)", "type": a.type if a else "",
                         "report_code": a.report_code if a else "", "debit": v if v > 0 else ZERO,
                         "credit": -v if v < 0 else ZERO, "balance": v})
        total_dr = sum((r["debit"] for r in rows), ZERO)
        total_cr = sum((r["credit"] for r in rows), ZERO)
        pl = sum((r["balance"] for r in rows if self.is_pl(r["code"])), ZERO)
        appropriations = sum((r["balance"] for r in rows if self.is_appropriation(r["code"])), ZERO)
        # profit = Net Profit After Tax, as the Financial Reports show it (before dividends / distributions);
        # retained = what goes to retained earnings at the year end
        return {"rows": rows, "total_debit": total_dr, "total_credit": total_cr,
                "difference": total_dr - total_cr, "profit": -(pl - appropriations),
                "appropriations": appropriations, "retained": -pl,
                "as_at": upto or self.fy_range(fy)[1]}

    def general_ledger(self, fy: int, start: dt.date | None = None, end: dt.date | None = None,
                       codes: list[str] | None = None) -> list[dict]:
        fy_start, fy_end = self.fy_range(fy)
        start, end = max(start or fy_start, fy_start), min(end or fy_end, fy_end)
        before = self.opening(fy)
        if start > fy_start:
            for k, v in self.movements(fy, upto=start - dt.timedelta(days=1)).items():
                before[k] = before.get(k, ZERO) + v
        entries: dict[str, list] = defaultdict(list)
        for j in self.journals(fy):
            if not (start <= j.date <= end):
                continue
            for ln in j.lines:
                entries[ln.account].append({"date": j.date, "number": j.number, "narration": j.narration,
                                            "description": ln.description, "debit": ln.debit, "credit": ln.credit,
                                            "kind": j.kind})
        accounts = set(entries) | {k for k, v in before.items() if v != 0}
        if codes:
            accounts &= set(codes)
        out = []
        for code in sorted(accounts, key=code_sort_key):
            a = self.chart.get(code)
            run = before.get(code, ZERO)
            opening = run
            rows = []
            for e in sorted(entries.get(code, []), key=lambda e: (e["date"], e["number"])):
                run += e["debit"] - e["credit"]
                rows.append({**e, "balance": run})
            out.append({"code": code, "name": a.name if a else "(not in chart)",
                        "report_code": a.report_code if a else "", "opening": opening, "entries": rows,
                        "closing": run, "debits": sum((e["debit"] for e in rows), ZERO),
                        "credits": sum((e["credit"] for e in rows), ZERO)})
        return out

    # ---- year-end close ----------------------------------------------------------------- #
    def _person_groups(self) -> dict[int, list]:
        groups: dict[int, list] = defaultdict(list)
        for a in self.chart.usable():
            m = PERSON_RE.match(a.report_code or "")
            if m and m.group(1) != "EQU.UDE":
                groups[int(m.group(2))].append(a)
        return groups

    def _person_label(self, accts) -> str:
        for a in accts:
            parts = [p.strip() for p in re.split(r"\s+[-–]\s+", a.name) if p.strip()]
            if len(parts) > 1 and not re.match(r"^(partner|beneficiary)?\s*#?\s*\d+$", parts[-1], re.I):
                return parts[-1]
        kind = "Partner" if self.client.type == "partnership" else "Beneficiary"
        m = PERSON_RE.match(accts[0].report_code)
        return f"{kind} {m.group(2)}" if m else kind

    def close_plan(self, fy: int) -> dict:
        """What year-end close would post for `fy` (into the first day of fy+1)."""
        bal = self.closing(fy)
        moves: list[tuple[str, str, Decimal, str]] = []   # (from account, to account, balance moved, why)
        create: list[dict] = []
        warnings: list[str] = []
        et = self.client.type
        taken: set[str] = set()
        if et in ("trust", "partnership"):
            for n, accts in sorted(self._person_groups().items()):
                opening = [a for a in accts if re.search(r"opening", a.name, re.I) and
                           (a.report_code.startswith(("EQU.ADV", "EQU.CAP")))]
                movers = [a for a in accts if a not in opening and bal.get(a.code)]
                if not movers:
                    continue
                if opening:
                    target = opening[0].code
                else:
                    label = self._person_label(accts)
                    target = self._free_code([str(44000 + 10 * n), f"440{n}5", accts[0].code], taken)
                    taken.add(target)
                    create.append({"code": target, "name": f"Opening balance - {label}",
                                   "report_code": f"EQU.ADV.{n}"})
                for a in movers:
                    moves.append((a.code, target, bal[a.code], f"person #{n} movements to opening balance"))
        re_code = self.retained_earnings_code(create=False) or "(retained earnings)"
        for a in self.chart.usable():
            rc = a.report_code or ""
            b = bal.get(a.code)
            if not b:
                continue
            if et == "company" and rc.startswith("EQU.RET.DIV"):
                moves.append((a.code, re_code, b, "dividends to retained earnings"))
            elif et == "sole_trader" and (rc.startswith("EQU.DRA") or rc.startswith("EQU.ADV")) and not PERSON_RE.match(rc):
                moves.append((a.code, re_code, b, "owner's drawings / funds introduced to retained earnings"))
        basis = hashlib.sha1(json.dumps(sorted((k, as_str(v)) for k, v in bal.items())).encode()).hexdigest()[:16]
        return {"fy": fy, "next_fy": fy + 1, "moves": moves, "create": create, "warnings": warnings, "basis": basis}

    def _free_code(self, candidates: list[str], taken: set[str]) -> str:
        for c in candidates:
            if c and not self.chart.get(c) and c not in taken:
                return c
        base = candidates[-1]
        for i in range(1, 1000):
            c = f"{base}.{i}"[:10]
            if not self.chart.get(c) and c not in taken:
                return c
        raise LedgerError("no free account code for the opening balance account")

    def close_journal(self, fy: int) -> Journal | None:
        """The year-end close journal already posted for `fy`, if any."""
        if fy + 1 not in self.years():
            return None
        for j in self.journals(fy + 1, statuses=("posted", "draft")):
            if j.kind == "close" and j.source.get("closed_fy") == fy:
                return j
        return None

    def close_status(self, fy: int) -> dict:
        j = self.close_journal(fy)
        plan = self.close_plan(fy)
        needed = bool(plan["moves"])
        stale = bool(j) and j.source.get("basis") != plan["basis"]
        return {"rolled_forward": fy + 1 in self.years(), "journal": j.number if j else "", "needed": needed,
                "stale": stale, "plan": plan}

    def close_year(self, fy: int) -> dict:
        """Roll forward: open fy+1 and post (or refresh) the year-end close journal on its first day."""
        self.store.require_lock(self.slug)
        nxt = fy + 1
        if self.is_locked(nxt):
            raise ValidationError([f"FY{nxt} is locked - unlock it to refresh the year-end close"])
        self.ensure_year(nxt)
        plan = self.close_plan(fy)
        created = []
        for acc in plan["create"]:
            if not self.chart.get(acc["code"]):
                self.chart.add(acc["code"], acc["name"], acc["report_code"], "Equity")
                self.store.audit(self.slug, "account.added", {**acc, "why": "year-end close"})
                created.append(acc)
        if plan["moves"] and "(retained earnings)" in {m[1] for m in plan["moves"]}:
            re_code = self.retained_earnings_code()
            plan["moves"] = [(f, re_code if t == "(retained earnings)" else t, b, w) for f, t, b, w in plan["moves"]]
        existing = self.close_journal(fy)
        if not plan["moves"]:
            if existing:
                self.void_journal(nxt, existing.number, f"no balances left to close for FY{fy}")
            self.store.audit(self.slug, "year.rolled_forward", {"fy": fy, "next_fy": nxt, "close_journal": ""})
            return {"next_fy": nxt, "journal": "", "moves": [], "created": created}
        lines = []
        totals: dict[str, Decimal] = defaultdict(lambda: ZERO)
        for frm, to, b, why in plan["moves"]:
            # clear the movement account and put the same balance into the accumulating account
            lines.append(Line(frm, debit=-b if b < 0 else ZERO, credit=b if b > 0 else ZERO, description=why))
            totals[to] += b
        for to, b in totals.items():
            if b:
                lines.append(Line(to, debit=b if b > 0 else ZERO, credit=-b if b < 0 else ZERO,
                                  description=f"year-end close FY{fy}"))
        start = self.fy_range(nxt)[0]
        j = existing or Journal(date=start, narration="", kind="close")
        j.date, j.kind, j.lines = start, "close", lines
        j.narration = f"Year-end close FY{fy} - movement accounts to opening balances / retained earnings"
        j.source = {"closed_fy": fy, "basis": plan["basis"]}
        j = self.save_journal(j, nxt, post=True, action="year.closed" if not existing else "year.close_refreshed")
        return {"next_fy": nxt, "journal": j.number, "moves": plan["moves"], "created": created}

    # ---- figures for the Financial Reports engine --------------------------------------- #
    def report_accounts(self, fy: int, upto: dt.date | None = None) -> list[dict]:
        cy = self.closing(fy, upto)
        py = self.closing(fy - 1) if any(y < fy for y in self.years()) else {}
        out = []
        for code in sorted(set(cy) | set(py), key=code_sort_key):
            a = self.chart.get(code)
            if a is None:
                continue
            rc = a.report_code or ""
            if rc.startswith("EQU.RET.CUR"):
                continue   # current year earnings is computed from the P&L
            sign = 1 if nature(rc, a.name) == "dr" else -1
            c, p = cy.get(code, ZERO) * sign, py.get(code, ZERO) * sign
            if c == 0 and p == 0:
                continue
            label = self.client.labels.get(code) or a.reporting_name or a.name
            out.append({"code": code, "name": a.name, "display_name": label, "type": a.type,
                        "report_code": rc, "cy": float(c), "py": float(p)})
        return out

    def pack(self, fy: int, upto: dt.date | None = None, date_signed: str = "") -> dict:
        start, end = self.fy_range(fy)
        c = self.client
        entity = {"name": c.name, "legal_name": c.legal_name, "abn": c.abn, "type": c.type,
                  "fy_end": (upto or end).isoformat(), "directors": list(c.directors), "trustee": c.trustee,
                  "trustee_type": c.trustee_type, "business_name": c.business_name, "proprietor": c.proprietor,
                  "compiled_by": c.compiled_by, "date_signed": date_signed}
        if upto and upto != end:
            entity["period_label"] = f"For the period {start.day} {start:%B %Y} to {upto.day} {upto:%B %Y}"
        entity = {k: v for k, v in entity.items() if v not in ("", None, [])}
        return {"_generated_by": "Fortis Ledger", "entity": entity, "options": dict(c.options),
                "accounts": self.report_accounts(fy, upto)}


def journal_from_form(form: dict, default_date: dt.date | None = None) -> Journal:
    """Build a Journal from posted form fields (narration, date, account_N, debit_N, credit_N, desc_N)."""
    from .money import to_dec
    lines = []
    idx = sorted({int(k.split("_")[1]) for k in form if re.fullmatch(r"(account|debit|credit|desc)_\d+", k)})
    for i in idx:
        acct = (form.get(f"account_{i}") or "").strip()
        acct = acct.split(" ")[0].strip() if acct else ""
        lines.append(Line(account=acct, debit=to_dec(form.get(f"debit_{i}")), credit=to_dec(form.get(f"credit_{i}")),
                          description=(form.get(f"desc_{i}") or "").strip()))
    d = parse_date(form.get("date")) if form.get("date") else (default_date or dt.date.today())
    return Journal(date=d, narration=(form.get("narration") or "").strip(), reference=(form.get("reference") or "").strip(),
                   lines=lines, number=(form.get("number") or "").strip())


__all__ = ["Ledger", "LedgerError", "ValidationError", "ChartError", "journal_from_form"]
