#!/usr/bin/env python3
"""
fr_engine.py - the "Xero ledger" behind the Financial Reports pack.

Given a list of accounts (code, name, type, report_code, cy, py) this module
derives everything the report pack needs, the same way Xero's report templates
derive a pack from account types + report codes:

  * Profit and Loss Statement sections and sub-totals
  * Balance Sheet lines (grouped by 3-segment report code) with note references
  * Movements in Equity (opening, profit, other movements, closing)
  * Notes to the Financial Statements (numbered, tiered by report code)
  * Depreciation schedule totals
  * A validation report (balance, roll-forward, ties, unmapped accounts)

Nothing in here knows about PDF layout - see render_pack.py for that.
The layout rules (labels, ordering, which code goes where) live in
assets/report-map.json so they can be edited without touching code.
"""
from __future__ import annotations

import json
import math
import re
from collections import OrderedDict
from pathlib import Path
from typing import Any

SKILL_DIR = Path(__file__).resolve().parent.parent
# Whole-dollar source figures (a rounded PDF or workpaper) can leave footing differences of a few
# dollars; cents-level Xero/workpaper exports should reconcile to $0.  Differences inside this
# tolerance are reported as INFO (rounding), anything larger is a real error.
ROUNDING_TOL = 5.0
DEFAULT_MAP = SKILL_DIR / "assets" / "report-map.json"
DEFAULT_BOILERPLATE = SKILL_DIR / "assets" / "boilerplate.json"


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def rnd(x: Any) -> int:
    """Round half away from zero to whole dollars (Xero report convention)."""
    if x is None or x == "":
        return 0
    x = float(x)
    if math.isnan(x):
        return 0
    return int(math.floor(abs(x) + 0.5)) * (1 if x >= 0 else -1)


def fmt(n: int | float | None, decimals: int = 0) -> str:
    """Xero number format: 1,234 / (1,234) / '-' for nil; decimals=2 gives 1,234.56 (some templates print cents)."""
    if decimals:
        x = float(n or 0)
        q = 10 ** decimals
        x = math.floor(abs(x) * q + 0.5) / q * (1 if x >= 0 else -1)
        if x == 0:
            return "-"
        s = f"{abs(x):,.{decimals}f}"
        return f"({s})" if x < 0 else s
    n = rnd(n)
    if n == 0:
        return "-"
    s = f"{abs(n):,}"
    return f"({s})" if n < 0 else s


def load_json(path: str | Path) -> dict:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def load_map(path: str | Path | None = None) -> dict:
    return load_json(path or DEFAULT_MAP)


def load_boilerplate(path: str | Path | None = None) -> dict:
    return load_json(path or DEFAULT_BOILERPLATE)


def segs(code: str) -> list[str]:
    return [s for s in (code or "").split(".") if s]


def strip_code(label: str) -> tuple[str | None, str]:
    """'200 - Sales' -> ('200', 'Sales'); 'Sales' -> (None, 'Sales')."""
    m = re.match(r"^\s*([0-9][0-9A-Za-z.\-/]{0,11})\s*[-\u2013:]\s+(.*\S)\s*$", label or "")
    if m:
        return m.group(1), m.group(2)
    return None, (label or "").strip()


# --------------------------------------------------------------------------- #
# report code -> layout slot  (Xero report codes as used in the firm charts)
# --------------------------------------------------------------------------- #
ACCUM_RE = re.compile(r"accum|\bless\b|amortis|deprec|written down|impair", re.I)


def slot_set(rmap: dict) -> set[str]:
    """Every layout position the engine understands (they can be used directly as report codes)."""
    out = set(rmap["bs_lines"])
    for key, info in rmap["bs_lines"].items():
        note = rmap["notes"].get(info.get("note") or "", {})
        for cls in (note.get("classes") or {}):
            out.add(f"{key}.{cls}")
        for l2 in (note.get("level2") or {}):
            out.add(f"{key}.{l2}")
    for secs in [rmap["pnl_sections"]] + [lay.get("pnl_sections") or [] for k, lay in
                                          (rmap.get("entity_layouts") or {}).items() if not k.startswith("_")]:
        for sec in secs:
            for c in [sec.get("code")] + list(sec.get("also_codes") or []):
                if c:
                    out.add(c)
    out.update({"EQU.CYE", "EQU.DIV", "EQU.SET", "EQU.PFT", "EXP.CFL", "EXP.DIS", "EXP.DIS.ADJ", "LIA.NCA.BEN"})
    return out


def _pnl_heading_for(slot: str, rmap: dict) -> str | None:
    for secs in [rmap["pnl_sections"]] + [lay.get("pnl_sections") or [] for k, lay in
                                          (rmap.get("entity_layouts") or {}).items() if not k.startswith("_")]:
        for sec in secs:
            if sec.get("code") == slot:
                return sec["heading"]
    return None


def ppe_class(name: str, parts: list[str], rmap: dict) -> tuple[str, bool]:
    """
    Class of a Property, Plant and Equipment account.  Xero groups by report code, so the class is the
    code's class segment (ASS.NCA.FIX.PLA -> PLA); ppe_class_refine splits a code by account name only
    where the firm chart lumps natures together (pools / staff amenities in BUC, a bare ASS.NCA.FIX).
    Returns (class, decided_by_name).
    """
    lname = (name or "").lower()
    seg = parts[3] if len(parts) > 3 else "FIX"
    for rx, cls in (rmap.get("ppe_class_refine") or {}).get(seg, []):
        if re.search(rx, lname):
            return cls, True
    by_code = rmap.get("ppe_class_by_code", {})
    return by_code.get(seg, rmap["notes"]["ppe"].get("default_class", "PLA")), seg != "FIX"


def resolve_slot(code: str, name: str, rmap: dict, _slots: set[str] | None = None) -> tuple[str | None, int | None, bool]:
    """
    Xero report code (+ account name) -> (layout slot, person number, ppe_class_inferred_from_code).
    Longest matching prefix in xero_code_map / the layout's own codes wins.  A trailing
    .1/.2/... on an equity or beneficiary code is the partner / beneficiary number.
    """
    code = (code or "").strip().upper()
    if not code:
        return None, None, False
    parts = code.split(".")
    person = None
    xmap = {k: v for k, v in (rmap.get("xero_code_map") or {}).items() if not k.startswith("_")}
    if len(parts) > 2 and parts[-1].isdigit() and code not in xmap:
        person = int(parts[-1])
        parts = parts[:-1]
    slots = _slots if _slots is not None else slot_set(rmap)
    for n in range(len(parts), 0, -1):
        pref = ".".join(parts[:n])
        if pref in xmap:
            entry = xmap[pref]
            slot = entry["slot"]
            lname = (name or "").lower()
            for rx, alt in entry.get("refine", []):
                if re.search(rx, lname):
                    slot = alt
                    break
            class_inferred = False
            if entry.get("ppe_class"):
                cls, decided = ppe_class(name, parts, rmap)
                slot = f"{slot}.{cls}"
                class_inferred = not decided
            return slot, (person if slot.startswith(("EQU", "LIA.NCA.BEN")) else None), class_inferred
        if pref in slots:
            return pref, (person if pref.startswith(("EQU", "LIA.NCA.BEN")) else None), False
    return None, None, False


def describe_code(code: str, name: str, rmap: dict) -> str:
    """Plain-English 'where does this print' for a report code - used in mapping tables."""
    slot, person, _ = resolve_slot(code, name, rmap)
    if not slot:
        return "UNKNOWN report code - not in xero_code_map or the layout"
    s = segs(slot)
    who = f" (person #{person})" if person else ""
    if slot == "EXP.DIS":
        return "P&L: distributions to beneficiaries / partners (trust and partnership packs)"
    heading = _pnl_heading_for(slot, rmap)
    if heading:
        return f"P&L: {heading}"
    if slot == "EXP.DIS.ADJ":
        return "P&L: distribution section - reconciliation adjustment line"
    if s[0] == "EQU":
        labels = {"EQU.CYE": "Current year earnings (folded into Retained Earnings)",
                  "EQU.DIV": "Dividends (P&L Dividend Paid; deducted from Retained Earnings)",
                  "EQU.PFT": "Share of profit (partner / beneficiary account)", "EQU.SET": "Settled sum / unit capital"}
        lab = labels.get(slot) or rmap["bs_lines"].get(slot, {}).get("label", slot)
        return f"Equity: {lab}{who}"
    if slot == "EXP.CFL":
        return "P&L: losses applied (before distributions)"
    if slot == "LIA.NCA.BEN":
        return f"Non-Current Liabilities: beneficiary entitlements{who}"
    key = None
    if len(s) >= 4 and ".".join(s[:4]) in rmap["bs_lines"]:
        key = ".".join(s[:4])
    elif len(s) >= 3 and ".".join(s[:3]) in rmap["bs_lines"]:
        key = ".".join(s[:3])
    if not key:
        return f"layout slot {slot} (no Balance Sheet line)"
    info = rmap["bs_lines"][key]
    part = {"CUR": "Current", "NCA": "Non-Current"}.get(s[1], s[1])
    side = {"ASS": "Assets", "LIA": "Liabilities"}[s[0]]
    note = rmap["notes"].get(info.get("note") or "", {})
    tier = ""
    if note.get("levels") == "class":
        cls = s[3] if len(s) > 3 and s[3] in note.get("classes", {}) else note.get("default_class")
        tier = note["classes"].get(cls, "") if cls else ""
    elif note.get("levels") == "cur_ncu":
        tier = note["cur_label"] if s[1] == "CUR" else note["ncu_label"]
        if len(s) > 3 and s[3] in (note.get("level2") or {}):
            tier += f" > {note['level2'][s[3]]}"
    elif note.get("levels") == "payables":
        for k in sorted(note["subgroup_order"], key=len, reverse=True):
            if slot == k or slot.startswith(k + "."):
                tier = note["subgroups"][k]
                break
    where = f"Note {note.get('title', '-')}" + (f" > {tier}" if tier else "")
    return f"{part} {side}: {info['label']} - {where}"


# --------------------------------------------------------------------------- #
# the pack builder
# --------------------------------------------------------------------------- #
CY_PY = ("cy", "py")
ZERO = {"cy": 0.0, "py": 0.0}
# how the company P&L computed lines are formed when a layout gives no formula
LEGACY_FORMULAS = {
    "gross": {"add": ["income"], "sub": ["cos"]},
    "total_income": {"add": ["gross", "other"]},
    "pbt": {"add": ["total_income"], "sub": ["expenses"]},
    "npat": {"add": ["pbt"], "sub": ["tax"]},
    "npat_div": {"add": ["npat"], "sub": ["dividends"]},
    "npat_dist": {"add": ["npat_div"], "sub": ["distributions"]},
}
DEFAULT_BS_EQUITY = [
    {"value": "retained_display", "label": "Retained Earnings", "always": True},
    {"value": "reserves", "label": "Reserves"},
    {"value": "share_capital", "label": "Share Capital"},
    {"value": "capital", "label": "Capital Accounts"},
    {"value": "drawings", "label": "Drawings"},
    {"value": "other", "label": "Other Equity"},
    {"value": "settled", "label": "@accounts"},
]
PERSON_SLOTS = ("EQU.CAP", "EQU.DRA", "EQU.PFT")
GENERIC_WORDS = re.compile(r"^(#?\s*\d+|partner|beneficiary|beneficiaries|owner|proprietor|education(al)?|income tax|"
                           r"life (insurance|assurance)|medicare|medical|superannuation|salary|other|rates and taxes|"
                           r"clearing account.*|opening balance|funds contributed|drawings|share of profits?)$", re.I)


def _plus(a: dict, b: dict, k: float = 1.0) -> dict:
    return {c: a[c] + k * b[c] for c in CY_PY}


class Pack:
    def __init__(self, data: dict, rmap: dict | None = None):
        self.src = data
        self.map = rmap or load_map(data.get("report_map"))
        self.entity = data["entity"]
        self.etype = (self.entity.get("type") or "company").strip().lower().replace(" ", "_").replace("-", "_")
        self.layout = self._layout()
        self.options = {
            "sort": "alpha",                  # alpha | source
            "cost_of_sales_label": "Cost of Sales",
            "keep_nil_accounts": False,
            "show_current_heading": True,
            "include_inventory_policy": None,  # None = auto (only when inventory exists)
            "depreciation_layout": None,       # simple | standard | register
            "report_order": None,
            "decimals": 0,
            "bs_line_labels": {},              # e.g. {"ASS.CUR.REC": "Trade and Other Receivables"}
            "note_titles": {},                 # e.g. {"receivables": "Trade and Other Receivables"}
            "contents_page_numbers": True,     # Xero prints the start page of each report on the Contents page
            "cos_lines": "accounts",           # accounts | grouped (Opening inventory / Purchases / Closing inventory)
            "cos_group_labels": {},            # e.g. {"opening": "Opening stock", "closing": "Closing stock"}
        }
        self.options.update(self.layout.get("options") or {})
        self.options.update(data.get("options") or {})
        self.accounts = [dict(a) for a in data.get("accounts", [])]
        self.extra_equity_movements = data.get("equity_movements") or []
        self.depreciation = data.get("depreciation")
        self.validation: list[dict] = []
        self._normalise_accounts()
        sc = self.options.get("single_column")
        self.single_column = bool(sc) if sc is not None else (bool(self.accounts) and all(rnd(a["py"]) == 0 for a in self.accounts))
        self.rental = self._build_rental()
        self.pnl = self._build_pnl()
        self.equity_lines = self._equity_lines()
        self.persons = self._build_persons()
        self.distribution = self._build_distribution_statement()
        self.notes = self._build_notes()
        self.bs = self._build_bs()
        self.equity = self._build_equity_statement()
        self.depn = self._build_depreciation()
        self._validate()

    # ---- entity layout -------------------------------------------------- #
    def _layout(self) -> dict:
        """report-map.json entity_layouts.<type> laid over the company defaults."""
        layouts = {k: v for k, v in (self.map.get("entity_layouts") or {}).items() if not k.startswith("_")}
        lay = {"reports": None, "pnl_sections": self.map["pnl_sections"], "notes_order": self.map["notes_order"],
               "bs_equity": DEFAULT_BS_EQUITY, "persons": None, "distribution_statement": None,
               "rental_schedule": None, "options": {}}
        lay.update(layouts.get(self.etype) or {})
        return lay

    def reports(self) -> list[str]:
        """Reports in print order (options.report_order wins, then the entity layout, then the company default)."""
        return list(self.options.get("report_order") or self.layout.get("reports")
                    or ["pnl", "bs", "equity", "notes", "depreciation", "declaration", "compilation"])

    # ---- accounts ------------------------------------------------------- #
    def _normalise_accounts(self):
        slots = slot_set(self.map)
        last_ppe = None  # slot of the last PPE cost line, in ledger (workpaper) order
        for i, a in enumerate(self.accounts):
            a["_seq"] = i
            a["cy"] = float(a.get("cy") or 0)
            a["py"] = float(a.get("py") or 0)
            a["report_code"] = (a.get("report_code") or "").upper()
            a.setdefault("name", "")
            a["display_name"] = a.get("display_name") or a["name"]
            a["slot"] = ""
            if not a["report_code"]:
                self.validation.append({"level": "error", "check": "mapping",
                                        "detail": f"Account '{a['name']}' has no report code - map it before rendering."})
                continue
            slot, person, class_inferred = resolve_slot(a["report_code"], a["name"], self.map, slots)
            if slot is None:
                self.validation.append({"level": "error", "check": "mapping",
                                        "detail": f"Account '{a['name']}' report code {a['report_code']} is not a known Xero report code or layout code (report-map.json xero_code_map)."})
                continue
            if slot.startswith("ASS.NCA.PPE"):
                # an accumulated depreciation line whose name gives no class follows its cost line
                if ACCUM_RE.search(a["name"]) and class_inferred and last_ppe:
                    slot = last_ppe
                elif not ACCUM_RE.search(a["name"]):
                    last_ppe = slot
            a["slot"] = slot
            if person is not None:
                a.setdefault("person", person)
        if not self.options["keep_nil_accounts"]:
            self.accounts = [a for a in self.accounts if rnd(a["cy"]) != 0 or rnd(a["py"]) != 0 or a.get("keep")]

    def _accts(self, prefix: str) -> list[dict]:
        rows = [a for a in self.accounts if a["slot"] == prefix or a["slot"].startswith(prefix + ".")]
        if self.options["sort"] == "alpha":
            rows.sort(key=lambda a: a["display_name"].lower())
        return rows

    @staticmethod
    def _sum(rows: list[dict]) -> dict:
        return {"cy": sum(r["cy"] for r in rows), "py": sum(r["py"] for r in rows)}

    # ---- rental schedule -------------------------------------------------- #
    def _build_rental(self) -> dict | None:
        """Rental property accounts (REV.INV.REN / EXP.INV.REN) taken out of the P&L body and shown in a
        Rental Schedule, with one net 'Rental income/(loss)' line in Other Income (partnership / trust packs)."""
        cfg = self.options.get("rental_schedule", self.layout.get("rental_schedule"))
        if not cfg:
            return None
        cfg = cfg if isinstance(cfg, dict) else {}
        prefixes = cfg.get("codes") or ["REV.INV.REN", "EXP.INV.REN"]
        rows = [a for a in self.accounts if a["slot"].startswith(("REV", "EXP"))
                and any(a["report_code"] == p or a["report_code"].startswith(p + ".") for p in prefixes)]
        if not rows:
            return None
        for a in rows:
            a["rental"] = True
        key = (lambda a: a["display_name"].lower()) if self.options["sort"] == "alpha" else (lambda a: a["_seq"])
        inc = sorted([a for a in rows if a["slot"].startswith("REV")], key=key)
        exp = sorted([a for a in rows if not a["slot"].startswith("REV")], key=key)
        ti, te = self._sum(inc), self._sum(exp)
        return {"title": cfg.get("title", "Rental Schedule"), "label": cfg.get("line_label", "Rental income/(loss)"),
                "income": inc, "expenses": exp, "income_total": ti, "expenses_total": te, "net": _plus(ti, te, -1)}

    # ---- profit and loss ---------------------------------------------- #
    def _cos_groups(self, rows: list[dict]) -> list[dict]:
        """Cost of sales as Xero's grouped trading account (partnership packs): one line each for
        opening inventory, purchases (every other cost-of-sales account) and closing inventory."""
        labels = {"opening": "Opening inventory", "purchases": "Purchases", "closing": "Closing inventory",
                  **(self.options.get("cos_group_labels") or {})}
        groups: dict[str, list] = {"opening": [], "purchases": [], "closing": []}
        for r in rows:
            rc = str(r.get("report_code") or "").upper()
            if rc.startswith("EXP.COS.OPE") or re.search(r"opening", r["display_name"], re.I):
                groups["opening"].append(r)
            elif rc.startswith("EXP.COS.CLO") or re.search(r"closing", r["display_name"], re.I):
                groups["closing"].append(r)
            else:
                groups["purchases"].append(r)
        return [{"label": labels[g], **self._sum(groups[g]), "code": None, "slot": "EXP.COS"}
                for g in ("opening", "purchases", "closing") if groups[g]]

    def _build_pnl(self) -> dict:
        out = {"sections": [], "totals": {}}
        t: dict[str, dict] = {}
        for sec in self.layout["pnl_sections"]:
            key = sec["key"]
            if sec.get("computed"):
                f = sec.get("formula") or LEGACY_FORMULAS.get(key, {})
                val = {c: sum(t.get(k, ZERO)[c] for k in f.get("add", [])) - sum(t.get(k, ZERO)[c] for k in f.get("sub", []))
                       for c in CY_PY}
                t[key] = val
                show = not sec.get("hidden")
                dep = sec.get("hide_if_nil_section") or sec.get("show_with")
                if dep:
                    dep_sec = next((x for x in out["sections"] if x["key"] == dep), None)
                    show = show and bool(dep_sec and dep_sec.get("show"))
                out["sections"].append({"key": key, "computed": True, "label": sec["label"], "show": show, **val})
                continue
            codes = [sec["code"]] + list(sec.get("also_codes") or [])
            rows, seen = [], set()
            for code in codes:
                for a in self._accts(code):
                    if id(a) not in seen and not a.get("rental"):
                        seen.add(id(a))
                        rows.append(a)
            if self.options["sort"] == "alpha" and len(codes) > 1:
                rows.sort(key=lambda a: a["display_name"].lower())
            if key == "cos" and self.options.get("cos_stock_order", True):
                # opening stock first, closing stock last (Xero prints the trading account that way)
                rows.sort(key=lambda a: 0 if re.search(r"opening", a["display_name"], re.I)
                          else 2 if re.search(r"closing", a["display_name"], re.I) else 1)
            total = self._sum(rows)
            lines = [{"label": r["display_name"], "cy": r["cy"], "py": r["py"], "code": r.get("code"), "slot": r["slot"]}
                     for r in rows]
            if key == "cos" and self.options.get("cos_lines") == "grouped" and rows:
                lines = self._cos_groups(rows)
            if sec.get("rental_line") and self.rental:
                net = self.rental["net"]
                lines.append({"label": self.rental["label"], "cy": net["cy"], "py": net["py"], "code": None, "slot": "RENTAL"})
                if self.options["sort"] == "alpha":
                    lines.sort(key=lambda l: l["label"].lower())
                total = _plus(total, net)
            if sec.get("collapse_label") and lines:
                main = sec["code"]
                keep = [l for l in lines if l["slot"] != main]
                coll = [l for l in lines if l not in keep]
                lines = keep + ([{"label": sec["collapse_label"], **self._sum(coll), "code": None, "slot": main}] if coll else [])
            sign = float(sec.get("sign", 1))
            if sign != 1:
                lines = [{**l, "cy": l["cy"] * sign, "py": l["py"] * sign} for l in lines]
            t[key] = total
            heading = sec["heading"]
            total_label = sec["total"]
            if key == "cos":
                heading = self.options["cost_of_sales_label"] or heading
                total_label = sec.get("alt_headings", {}).get(heading, total_label)
            show = bool(lines) and not (sec.get("hide_if_nil") and rnd(total["cy"]) == 0 and rnd(total["py"]) == 0)
            out["sections"].append({
                "key": key, "computed": False, "heading": heading, "total_label": total_label, "lines": lines,
                "show": show, "sign": sign, "cy": total["cy"] * sign, "py": total["py"] * sign,
            })
        for k in ("income", "cos", "other", "expenses", "tax", "dividends", "distributions"):
            t.setdefault(k, dict(ZERO))
        if "npat" not in t:
            t["npat"] = _plus(t.get("pbt", ZERO), t["tax"], -1)
        t.setdefault("npat_div", _plus(t["npat"], t["dividends"], -1))
        t.setdefault("total_income", _plus(_plus(t["income"], t["cos"], -1), t["other"]))
        # what stays in the entity after tax, dividends and distributions (= Profit for the Period)
        t["retained_profit"] = _plus(t["npat_div"], t["distributions"], -1)
        out["totals"] = t
        return out

    # ---- equity helper -------------------------------------------------- #
    def _equity_lines(self) -> dict:
        """
        Xero keeps Retained Earnings (prior years), Current Year Earnings and dividends apart; the
        pack prints them as one Retained Earnings line.  Current Year Earnings is whatever the P&L
        bottom line is in the ledger: NPAT, less any dividends and distributions that were posted
        through the P&L (a workpaper '506 - Dividend paid' line, trust / partnership distributions).
        """
        ret = self._sum(self._accts("EQU.RET"))
        cye = self._accts("EQU.CYE")
        div_rows = self._accts("EQU.DIV")
        div = self._sum(div_rows)
        div_pl = self._sum([a for a in div_rows if str(a.get("source", "")).startswith("pl")])
        div_bs = _plus(div, div_pl, -1)
        dist = self.pnl["totals"]["distributions"]
        npat = self.pnl["totals"]["npat"]
        expected = {c: npat[c] - div_pl[c] - dist[c] for c in CY_PY}
        if cye:
            cye_tot = self._sum(cye)
            for c in CY_PY:
                if abs(cye_tot[c] - expected[c]) > 0.5:
                    self.validation.append({"level": "error", "check": "current_year_earnings",
                                            "detail": f"Current Year Earnings on the Balance Sheet ({c.upper()}: {fmt(cye_tot[c])}) does not equal the P&L result it should carry ({fmt(expected[c])} = Net Profit After Tax {fmt(npat[c])}"
                                                      f"{' less dividends in the P&L ' + fmt(div_pl[c]) if rnd(div_pl[c]) else ''}{' less distributions ' + fmt(dist[c]) if rnd(dist[c]) else ''})."})
        else:
            cye_tot = expected
        person_rows = [a for a in self.accounts if a["slot"].startswith(PERSON_SLOTS)]
        return {
            "retained_prior": ret, "cye": cye_tot, "dividends": div, "dividends_bs": div_bs, "distributions": dist,
            "retained_display": {c: ret[c] + cye_tot[c] - div_bs[c] for c in CY_PY},
            "share_capital": self._sum(self._accts("EQU.SHA")),
            "reserves": self._sum(self._accts("EQU.RES")),
            "drawings": self._sum(self._accts("EQU.DRA")),
            "capital": self._sum(self._accts("EQU.CAP")),
            "other": self._sum(self._accts("EQU.OTH")),
            "settled": self._sum(self._accts("EQU.SET")),
            "profit_share": self._sum(self._accts("EQU.PFT")),
            "persons": self._sum(person_rows),
        }

    # ---- partner / beneficiary accounts ---------------------------------- #
    @staticmethod
    def _person_name(a: dict) -> str:
        if a.get("person_name"):
            return a["person_name"]
        parts = [p.strip() for p in re.split(r"\s+[-–]\s+", a.get("name") or "") if p.strip()]
        for cand in reversed(parts[1:]):
            cand = re.sub(r"^(partner|beneficiary|owner|proprietor)\s*", "", cand, flags=re.I).strip(" #")
            if cand and not GENERIC_WORDS.match(cand):
                return cand
        return f"#{a['person']}" if a.get("person") else ""

    def _build_persons(self) -> dict | None:
        """
        Partner Accounts / Beneficiary Accounts: one block per person from the per-person equity accounts
        (funds contributed EQU.ADV.n, drawings EQU.DRA.n, share of profits EQU.PFT.n), Xero style.
        The person is read from the account name ('Drawings - Jane Citizen') or the code's number.
        An account whose name says 'opening' is that person's brought-forward balance.
        """
        cfg = self.layout.get("persons")
        if not cfg:
            return None
        accts = sorted([a for a in self.accounts if a["slot"].startswith(PERSON_SLOTS)], key=lambda a: a["_seq"])
        if not accts:
            return None
        groups: list[dict] = []
        for a in accts:
            name = self._person_name(a)
            toks = set(re.findall(r"[a-z0-9#]+", name.lower()))
            g = next((g for g in groups if toks and g["toks"] and (toks <= g["toks"] or g["toks"] <= toks)), None)
            if g is None:
                g = {"name": name, "toks": toks, "accounts": []}
                groups.append(g)
            elif len(name) > len(g["name"]):
                g["name"], g["toks"] = name, toks
            g["accounts"].append(a)

        def cat(a):
            if re.search(r"opening", a["name"], re.I):
                return "opening"
            return {"EQU.CAP": "introduced", "EQU.DRA": "drawn", "EQU.PFT": "profit"}.get(".".join(segs(a["slot"])[:2]), "other")

        persons = []
        for i, g in enumerate(groups, 1):
            rows = []
            opening = [a for a in g["accounts"] if cat(a) == "opening"]
            if opening:
                rows.append({"label": cfg.get("opening_label", "Opening Balance"), "cat": "opening", **self._sum(opening)})
            for a in g["accounts"]:
                if cat(a) != "opening":
                    rows.append({"label": a["display_name"], "cat": cat(a), "cy": a["cy"], "py": a["py"]})
            heading = g["name"] if (cfg.get("heading") == "name" and g["name"] and not g["name"].startswith("#")) \
                else f"{cfg.get('person_label', 'Partner')} {i}"
            persons.append({"number": i, "name": g["name"], "heading": heading, "rows": rows, **self._sum(g["accounts"])})
        summary = []
        for catkey, label in cfg.get("summary_rows", [["opening", "Opening Balance"], ["introduced", "Funds Introduced"],
                                                       ["drawn", "Funds Drawn"], ["profit", "Profit Distributed"],
                                                       ["other", "Other Movements"]]):
            v = self._sum([r for p in persons for r in p["rows"] if r["cat"] == catkey])
            if rnd(v["cy"]) or rnd(v["py"]):
                summary.append({"label": label, **v})
        if not any(r["cat"] == "opening" for p in persons for r in p["rows"]) and any(rnd(a["py"]) for a in accts):
            self.validation.append({"level": "info", "check": "person_accounts",
                                    "detail": f"No 'Opening balance' account per {cfg.get('person_label', 'partner').lower()} - the {cfg.get('title', 'accounts')} statement lists each account's balance as it stands in the workpaper. Post the brought-forward balance to an 'Opening balance - <name>' account if the statement should show opening balance + movements."})
        return {"title": cfg.get("title", "Partner Accounts"), "summary_title": cfg.get("summary_title", "Partner Accounts Summary"),
                "detail_title": cfg.get("detail_title", "Detailed Partner Accounts"), "persons": persons,
                "summary": summary, **self._sum(accts)}

    # ---- trust income distribution ---------------------------------------- #
    def _build_distribution_statement(self) -> dict | None:
        cfg = self.layout.get("distribution_statement")
        if not cfg:
            return None
        cfl = sorted(self._accts("EXP.CFL"), key=lambda a: a["_seq"])
        dist = sorted([a for a in self._accts("EXP.DIS")], key=lambda a: a["_seq"])
        if not (cfl or dist) and not cfg.get("always"):
            return None
        cye = self.pnl["totals"]["npat"]
        rows = [{"kind": "line", "label": cfg.get("cye_label", "Current Year Earnings"), **cye}]
        cfl_tot = self._sum(cfl)
        if cfl:
            rows.append({"kind": "heading", "label": cfg.get("cfl_heading", "Carried Forward Losses Applied")})
            rows += [{"kind": "line", "label": a["display_name"], "cy": -a["cy"], "py": -a["py"], "indent": 1} for a in cfl]
            rows.append({"kind": "total", "label": f"Total {cfg.get('cfl_heading', 'Carried Forward Losses Applied')}",
                         "cy": -cfl_tot["cy"], "py": -cfl_tot["py"], "indent": 1})
        net = _plus(cye, cfl_tot, -1)
        rows.append({"kind": "computed", "label": cfg.get("net_label", "Net Trust Income for Distribution"), **net})
        dist_tot = self._sum(dist)
        if dist:
            rows.append({"kind": "heading", "label": cfg.get("dist_heading", "Distributions to Beneficiaries")})
            rows += [{"kind": "line", "label": a["display_name"], "cy": a["cy"], "py": a["py"], "indent": 1} for a in dist]
            rows.append({"kind": "total", "label": f"Total {cfg.get('dist_heading', 'Distributions to Beneficiaries')}",
                         "indent": 1, **dist_tot})
        rows.append({"kind": "computed", "label": cfg.get("undistributed_label", "Undistributed Trust Income"),
                     **_plus(net, dist_tot, -1)})
        return {"title": cfg.get("title", "Trust Income Distribution"), "heading": cfg.get("heading", "Trust Income Distribution"),
                "rows": rows}

    # ---- notes ---------------------------------------------------------- #
    def _note_key_for(self, code: str) -> str | None:
        s = segs(code)
        if len(s) < 3 or s[0] not in ("ASS", "LIA"):
            return None
        info = self._bs_line_info(code)
        return info["note"] if info else None

    def _bs_line_key(self, code: str) -> str | None:
        """4-segment override if report-map has one, else the 3-segment line."""
        s = segs(code)
        if len(s) >= 4 and ".".join(s[:4]) in self.map["bs_lines"]:
            return ".".join(s[:4])
        if len(s) >= 3 and ".".join(s[:3]) in self.map["bs_lines"]:
            return ".".join(s[:3])
        return None

    def _bs_line_info(self, code: str) -> dict | None:
        k = self._bs_line_key(code)
        return self.map["bs_lines"].get(k) if k else None

    def _build_notes(self) -> list[dict]:
        # an entity layout may override parts of a note (e.g. partnerships print no Secured / Unsecured tier)
        notes_cfg = {k: {**v, **((self.layout.get("notes") or {}).get(k) or {})} if isinstance(v, dict) else v
                     for k, v in self.map["notes"].items()}
        # client wording, e.g. {"receivables": "Trade and Other Receivables"} (pair with options.bs_line_labels)
        for k, title in (self.options.get("note_titles") or {}).items():
            if isinstance(notes_cfg.get(k), dict):
                notes_cfg[k] = {**notes_cfg[k], "title": title, "total": f"Total {title}"}
        order = self.options.get("notes_order") or self.layout.get("notes_order") or self.map["notes_order"]
        by_note: dict[str, list[dict]] = {k: [] for k in order}
        for a in self.accounts:
            k = self._note_key_for(a["slot"])
            if k is None:
                if a["slot"].startswith(("ASS", "LIA")):
                    self.validation.append({"level": "error", "check": "mapping",
                                            "detail": f"Account '{a['name']}' report code {a['report_code']} (layout {a['slot']}) is not a Balance Sheet line in report-map.json."})
                continue
            by_note.setdefault(k, []).append(a)
        # a Reserves note when there are two or more reserve accounts (one reserve prints on the face only)
        reserves = [a for a in self.accounts if a["slot"].startswith("EQU.RES")]
        if len(reserves) >= 2 and self.options.get("reserves_note", True) and "reserves" in notes_cfg:
            by_note["reserves"] = reserves

        notes: list[dict] = []
        number = 2
        for key in order:
            rows = by_note.get(key) or []
            if not any(rnd(r["cy"]) or rnd(r["py"]) for r in rows):
                continue
            cfg = notes_cfg[key]
            tiers = self._tier_rows(key, cfg, rows)
            total = self._sum(rows)
            notes.append({"key": key, "number": number, "title": cfg["title"], "total_label": cfg["total"],
                          "rows": tiers, **total})
            number += 1
        return notes

    def _tier_rows(self, key: str, cfg: dict, rows: list[dict]) -> list[dict]:
        """Turn accounts into display rows: heading / line / total tiers."""
        levels = cfg.get("levels", "flat")
        out: list[dict] = []

        line_order = self.options.get("note_line_order") or self.options["sort"]

        def lines_for(rs: list[dict], indent: int) -> list[dict]:
            rs = sorted(rs, key=lambda a: a["display_name"].lower()) if line_order == "alpha" else sorted(rs, key=lambda a: a["_seq"])
            # cost lines before their accumulated depreciation, Xero style
            rs = sorted(rs, key=lambda a: 1 if re.search(r"accum|amortis|deprec", a["display_name"].lower()) else 0) if key in ("ppe", "intangibles") else rs
            return [{"kind": "line", "label": a["display_name"], "cy": a["cy"], "py": a["py"], "indent": indent} for a in rs]

        def ppe_class_lines(label: str, cls: str, rs: list[dict], indent: int) -> list[dict]:
            """Xero prints a PPE class as '<Class> at Cost' and 'Accumulated Depreciation of <Class>'."""
            if cls == "OTH" or (self.options.get("ppe_note_lines") or cfg.get("line_style")) != "class":
                return lines_for(rs, indent)
            acc = [a for a in rs if ACCUM_RE.search(a["name"])]
            cost = [a for a in rs if a not in acc]
            out_ = []
            for lab, grp in ((f"{label} at Cost", cost), (f"Accumulated Depreciation of {label}", acc)):
                tot = self._sum(grp)
                if grp and (rnd(tot["cy"]) or rnd(tot["py"])):
                    out_.append({"kind": "line", "label": lab, "indent": indent, **tot})
            return out_

        if levels == "flat":
            out += lines_for(rows, 1)
            return out

        if levels == "class":
            groups: OrderedDict[str, list[dict]] = OrderedDict()
            for label in cfg["classes"].values():
                groups[label] = []
            code_of = {v: k for k, v in cfg["classes"].items()}
            for a in rows:
                s = segs(a["slot"])
                cls = s[3] if len(s) > 3 and s[3] in cfg["classes"] else cfg.get("default_class")
                groups[cfg["classes"][cls]].append(a)
            for label, rs in groups.items():
                if not rs:
                    continue
                out.append({"kind": "heading", "label": label, "indent": 1})
                out += ppe_class_lines(label, code_of[label], rs, 2) if key == "ppe" else lines_for(rs, 2)
                st = self._sum(rs)
                out.append({"kind": "total", "label": f"Total {label}", "indent": 2, **st})
            return out

        if levels == "cur_ncu":
            if not self.options["show_current_heading"]:
                out += lines_for(rows, 1)
                return out
            for seg, label in (("CUR", cfg["cur_label"]), ("NCA", cfg["ncu_label"])):
                rs = [a for a in rows if segs(a["slot"])[1] == seg]
                if not rs:
                    continue
                out.append({"kind": "heading", "label": label, "indent": 1})
                l2 = cfg.get("level2") or {}
                sub: OrderedDict[str, list[dict]] = OrderedDict()
                for a in rs:
                    s = segs(a["slot"])
                    sub.setdefault(l2.get(s[3]) if len(s) > 3 else None, []).append(a)
                for l2label, l2rows in sub.items():
                    if l2label:
                        out.append({"kind": "heading", "label": l2label, "indent": 2})
                        out += lines_for(l2rows, 3)
                        out.append({"kind": "total", "label": f"Total {l2label}", "indent": 3, **self._sum(l2rows)})
                    else:
                        out += lines_for(l2rows, 2)
                out.append({"kind": "total", "label": f"Total {label}", "indent": 2, **self._sum(rs)})
            return out

        if levels == "payables":
            order = cfg["subgroup_order"]
            groups: OrderedDict[str, list[dict]] = OrderedDict((k, []) for k in order)
            for a in rows:
                code = a["slot"]
                match = None
                for k in sorted(order, key=len, reverse=True):  # longest prefix first
                    if code == k or code.startswith(k + "."):
                        match = k
                        break
                groups[match or "LIA.CUR.PAY"].append(a)
            for k, rs in groups.items():
                if not rs:
                    continue
                label = cfg["subgroups"][k]
                out.append({"kind": "heading", "label": label, "indent": 1})
                out += lines_for(rs, 2)
                out.append({"kind": "total", "label": f"Total {label}", "indent": 2, **self._sum(rs)})
            return out

        out += lines_for(rows, 1)
        return out

    # ---- balance sheet -------------------------------------------------- #
    def _build_bs(self) -> dict:
        note_no = {n["key"]: n["number"] for n in self.notes}
        cfg = self.map["balance_sheet"]

        def lines(prefix: str) -> list[dict]:
            groups: OrderedDict[str, list[dict]] = OrderedDict()
            for a in self.accounts:
                s = segs(a["slot"])
                if len(s) >= 3 and ".".join(s[:2]) == prefix:
                    k = self._bs_line_key(a["slot"])
                    if k:
                        groups.setdefault(k, []).append(a)
            # keep report-map order
            ordered = [k for k in self.map["bs_lines"] if k in groups]
            out = []
            relabel = self.options.get("bs_line_labels") or {}
            for k in ordered:
                info = dict(self.map["bs_lines"][k])
                if k in relabel or ".".join(segs(k)[:3]) in relabel:
                    info["label"] = relabel.get(k) or relabel[".".join(segs(k)[:3])]
                tot = self._sum(groups[k])
                if rnd(tot["cy"]) == 0 and rnd(tot["py"]) == 0:
                    continue
                # merge same-label lines (e.g. two codes both labelled Financial Liabilities)
                existing = next((o for o in out if o["label"] == info["label"]), None)
                note = note_no.get(info.get("note"))
                if existing:
                    existing["cy"] += tot["cy"]; existing["py"] += tot["py"]
                    if note and str(note) not in existing["note"].split(", "):
                        existing["note"] = ", ".join(filter(None, [existing["note"], str(note)]))
                else:
                    out.append({"label": info["label"], "note": str(note) if note else "", **tot})
            return out

        assets_cur, assets_ncu = lines("ASS.CUR"), lines("ASS.NCA")
        liab_cur, liab_ncu = lines("LIA.CUR"), lines("LIA.NCA")
        ta = {c: sum(x[c] for x in assets_cur + assets_ncu) for c in ("cy", "py")}
        tl = {c: sum(x[c] for x in liab_cur + liab_ncu) for c in ("cy", "py")}
        na = {c: ta[c] - tl[c] for c in ("cy", "py")}

        eq = self.equity_lines
        equity_rows = []
        slot_of = {"share_capital": "EQU.SHA", "reserves": "EQU.RES", "capital": "EQU.CAP", "drawings": "EQU.DRA",
                   "other": "EQU.OTH", "settled": "EQU.SET", "profit_share": "EQU.PFT"}
        for spec in self.layout.get("bs_equity") or DEFAULT_BS_EQUITY:
            key, lab = spec["value"], spec["label"]
            if lab == "@accounts" and key in slot_of:
                for a in sorted(self._accts(slot_of[key]), key=lambda a: a["_seq"]):
                    equity_rows.append({"label": a["display_name"], "note": "", "cy": a["cy"], "py": a["py"]})
                continue
            v = eq[key]
            if rnd(v["cy"]) or rnd(v["py"]) or spec.get("always"):
                nref = str(note_no["reserves"]) if key == "reserves" and "reserves" in note_no else ""
                equity_rows.append({"label": lab, "note": nref, **v})
        te = {c: sum(x[c] for x in equity_rows) for c in ("cy", "py")}
        return {
            "assets": {"current": assets_cur, "non_current": assets_ncu, "total": ta},
            "liabilities": {"current": liab_cur, "non_current": liab_ncu, "total": tl},
            "net_assets": na, "equity": {"rows": equity_rows, "total": te}, "cfg": cfg,
        }

    # ---- movements in equity -------------------------------------------- #
    def _build_equity_statement(self) -> dict:
        eq = self.equity_lines
        te = self.bs["equity"]["total"]
        profit = self.pnl["totals"]["retained_profit"]
        es = dict(self.map["equity_statement"])
        es.update(self.layout.get("equity_statement") or {})
        self.equity_labels = es
        moves: list[dict] = [{"label": es["profit"], "cy": profit["cy"], "py": profit["py"]}]
        # movements derived from the balance sheet (CY only - PY needs prior-prior data)
        labels = es.get("movement_labels") or {}
        for key, label in (("share_capital", "Share Capital"), ("reserves", "Reserves"), ("settled", "Settled Sum"),
                           ("capital", "Capital Accounts"), ("drawings", "Drawings"), ("other", "Other Equity")):
            delta = eq[key]["cy"] - eq[key]["py"]
            if rnd(delta):
                names = [a["display_name"] for a in self._accts("EQU." + {"share_capital": "SHA", "reserves": "RES", "capital": "CAP", "drawings": "DRA", "other": "OTH", "settled": "SET"}[key])]
                moves.append({"label": labels.get(key) or (names[0] if len(names) == 1 and key in ("reserves", "other", "settled") else label),
                              "cy": delta, "py": 0, "derived": True})
        # retained earnings roll-forward residual (direct postings to RE)
        expected_ret_cy = eq["retained_prior"]["py"] + eq["cye"]["py"] - eq["dividends_bs"]["py"]
        residual = eq["retained_prior"]["cy"] - expected_ret_cy
        if 0.5 < abs(residual) <= ROUNDING_TOL:
            self.validation.append({"level": "info", "check": "retained_earnings_rollforward",
                                    "detail": f"Retained Earnings roll-forward differs by {fmt(residual)} - rounding of whole-dollar source figures; no movement line added."})
        elif abs(residual) > ROUNDING_TOL:
            moves.append({"label": "Retained Earnings", "cy": residual, "py": 0, "derived": True})
            self.validation.append({"level": "warning", "check": "retained_earnings_rollforward",
                                    "detail": f"Retained Earnings moved by {fmt(residual)} beyond profit and dividends (direct posting / prior period adjustment). It is shown as a 'Retained Earnings' movement - confirm the reason and label."})
        # user-supplied movements (e.g. PY share issue) - merge by label
        for m in self.extra_equity_movements:
            ex = next((x for x in moves if x["label"] == m.get("label")), None)
            if ex:
                ex["py"] = rnd(m.get("py", ex["py"])) if "py" in m else ex["py"]
                if "cy" in m:
                    ex["cy"] = rnd(m["cy"])
            else:
                moves.append({"label": m["label"], "cy": rnd(m.get("cy", 0)), "py": rnd(m.get("py", 0))})
        moves = [m for m in moves if rnd(m["cy"]) or rnd(m["py"]) or m["label"] == es["profit"]]
        total_inc = {c: sum(m[c] for m in moves) for c in ("cy", "py")}
        opening = {"cy": te["py"], "py": te["py"] - total_inc["py"]}
        if "opening_py" in self.src.get("equity", {}):
            opening["py"] = float(self.src["equity"]["opening_py"])
            diff = rnd(opening["py"] + total_inc["py"] - te["py"])
            if diff and abs(diff) <= ROUNDING_TOL:
                self.validation.append({"level": "info", "check": "equity_opening_py",
                                        "detail": f"Prior-year opening equity rolls to PY closing equity within rounding ({fmt(diff)})."})
            elif diff:
                self.validation.append({"level": "warning", "check": "equity_opening_py",
                                        "detail": f"Prior-year opening equity supplied ({fmt(opening['py'])}) plus PY movements does not roll to PY closing equity ({fmt(te['py'])}); difference {fmt(diff)}. Add the missing PY movement to equity_movements."})
        else:
            self.validation.append({"level": "info", "check": "equity_opening_py",
                                    "detail": "Prior-year opening equity was derived as PY closing equity less PY profit and supplied PY movements. Check it against the prior-year report's Movements in Equity."})
        return {"opening": opening, "movements": moves, "total_increases": total_inc, "total": te}

    # ---- depreciation --------------------------------------------------- #
    def _build_depreciation(self) -> dict | None:
        d = self.depreciation
        if not d or not d.get("groups"):
            return None
        num_cols = ["cost", "opening_accum", "opening_value", "purchases", "disposals", "depreciation",
                    "closing_accum", "closing_value"]
        groups = []
        grand = {k: 0 for k in num_cols}
        for g in d["groups"]:
            assets = []
            gt = {k: 0 for k in num_cols}
            for a in g.get("assets", []):
                row = dict(a)
                for k in num_cols:
                    row[k] = float(a.get(k) or 0)
                    gt[k] += row[k]
                if "closing_value" not in a and ("opening_value" in a or "purchases" in a):
                    row["closing_value"] = row["opening_value"] + row["purchases"] - row["disposals"] - row["depreciation"]
                    gt["closing_value"] += row["closing_value"]
                assets.append(row)
            for k in num_cols:
                grand[k] += gt[k]
            groups.append({"name": g["name"], "assets": assets, "totals": gt})
        layout = d.get("layout") or self.options["depreciation_layout"] or "simple"
        return {"layout": layout, "title": d.get("title") or ("Accounting Depreciation Schedule" if layout == "simple" else "Depreciation Schedule"),
                "groups": groups, "totals": grand}

    # ---- validation ----------------------------------------------------- #
    def _validate(self):
        v = self.validation
        for c in ("cy", "py"):
            na, te = self.bs["net_assets"][c], self.bs["equity"]["total"][c]
            if abs(na - te) > ROUNDING_TOL:
                v.append({"level": "error", "check": "balance_sheet_balances",
                          "detail": f"{c.upper()}: Net Assets {fmt(na)} does not equal Total Equity {fmt(te)} (difference {fmt(na - te)}). Check the P&L ties to Current Year Earnings, and dividends/RE coding."})
            elif abs(na - te) > 0.005:
                v.append({"level": "info", "check": "balance_sheet_balances",
                          "detail": f"{c.upper()}: Net Assets and Total Equity differ by {fmt(na - te)} - within rounding tolerance for whole-dollar source figures (Xero shows the same small footing differences)."})
        # PY comparatives present?
        if all(rnd(a["py"]) == 0 for a in self.accounts):
            v.append({"level": "warning", "check": "comparatives",
                      "detail": "No prior-year figures found - the pack prints a single current-year column (Xero's first-year layout). If this is not the entity's first year, add the comparatives from last year's pack."})
        # depreciation ties
        if self.depn:
            pl_depn = sum(l["cy"] for s in self.pnl["sections"] if not s.get("computed") for l in s["lines"]
                          if re.search(r"deprec|amortis|write.?off", l["label"].lower()))
            if abs(pl_depn - self.depn["totals"]["depreciation"]) > ROUNDING_TOL:
                v.append({"level": "warning", "check": "depreciation_ties_pl",
                          "detail": f"Depreciation schedule total {fmt(self.depn['totals']['depreciation'])} vs P&L depreciation/amortisation/write-off lines {fmt(pl_depn)}."})
            ppe = next((n for n in self.notes if n["key"] == "ppe"), None)
            if ppe and abs(ppe["cy"] - self.depn["totals"]["closing_value"]) > ROUNDING_TOL:
                v.append({"level": "warning", "check": "depreciation_ties_ppe",
                          "detail": f"Depreciation schedule closing value {fmt(self.depn['totals']['closing_value'])} vs Property, Plant and Equipment note {fmt(ppe['cy'])}. Land or intangibles may explain the gap."})
        # GST in refund position
        for a in self._accts("LIA.CUR.TAX"):
            if rnd(a["cy"]) < 0 and "gst" in a["name"].lower():
                v.append({"level": "info", "check": "gst_refund",
                          "detail": f"'{a['name']}' is a debit balance {fmt(a['cy'])} - printed as a negative liability, the Xero convention. Reclassify to Receivables only if the firm template requires it."})
        # income tax expense with no tax liability / vice versa
        tax_exp = rnd(self.pnl["totals"]["tax"]["cy"])
        tax_liab = [a for a in self._accts("LIA.CUR.TAX") if re.search(r"income tax|provision for", a["name"].lower())]
        if tax_exp and not tax_liab:
            v.append({"level": "info", "check": "income_tax", "detail": "Income tax expense is booked but no Income Tax Payable / Provision account was found on the Balance Sheet."})
        # entity fields
        for f_ in ("name", "fy_end"):
            if not self.entity.get(f_):
                v.append({"level": "error", "check": "entity", "detail": f"entity.{f_} is missing."})
        if not self.entity.get("abn"):
            v.append({"level": "warning", "check": "entity",
                      "detail": "No ABN supplied - the cover prints no ABN line (correct only if the entity has no ABN)."})
        if "declaration" in self.reports() and not self.entity.get("directors"):
            who = {"trust": "directors of the trustee company / trustees", "partnership": "partners",
                   "sole_trader": "proprietor"}.get(self.etype, "directors")
            v.append({"level": "warning", "check": "entity", "detail": f"No signatories listed ({who}) - the declaration will have no signature blocks."})
        # Division 7A loans
        d7a = [a["name"] for a in self.accounts if re.search(r"div(ision)?\.?\s*7a", a["name"], re.I)]
        if d7a and self.etype == "company":
            v.append({"level": "info", "check": "division_7a",
                      "detail": f"Division 7A loan(s) on the Balance Sheet: {', '.join(d7a)}. Confirm a complying loan agreement and the minimum yearly repayment."})
        # distributions credited to partners / beneficiaries
        dist = self.pnl["totals"]["distributions"]
        if self.persons and (rnd(dist["cy"]) or rnd(dist["py"])):
            share = self.equity_lines["profit_share"]
            for c in ("cy", "py"):
                if abs(share[c] - dist[c]) > ROUNDING_TOL and rnd(share[c]):
                    v.append({"level": "warning", "check": "distribution_ties",
                              "detail": f"{c.upper()}: distributions in the P&L {fmt(dist[c])} vs share of profits credited to {self.persons['title'].lower()} {fmt(share[c])}."})
        if self.etype == "partnership":
            und = self.pnl["totals"].get("undistributed") or self.pnl["totals"]["retained_profit"]
            if rnd(und["cy"]):
                v.append({"level": "warning", "check": "undistributed_income",
                          "detail": f"Undistributed Income {fmt(und['cy'])} - a partnership's net income is normally fully distributed to the partners."})

    # ---- output --------------------------------------------------------- #
    def has_errors(self) -> bool:
        return any(x["level"] == "error" for x in self.validation)

    def validation_markdown(self) -> str:
        if not self.validation:
            return "All checks passed."
        order = {"error": 0, "warning": 1, "info": 2}
        rows = sorted(self.validation, key=lambda x: order[x["level"]])
        out = ["| Level | Check | Detail |", "|---|---|---|"]
        for r in rows:
            out.append(f"| {r['level'].upper()} | {r['check']} | {r['detail']} |")
        return "\n".join(out)

    def summary(self) -> dict:
        t = self.pnl["totals"]
        return {
            "entity": self.entity.get("name"),
            "fy_end": self.entity.get("fy_end"),
            "type": self.etype, "reports": self.reports(),
            "total_income": t["total_income"], "expenses": t["expenses"], "npat": t["npat"],
            "dividends": t["dividends"], "npat_after_div": t["npat_div"], "distributions": t["distributions"],
            "total_assets": self.bs["assets"]["total"], "total_liabilities": self.bs["liabilities"]["total"],
            "net_assets": self.bs["net_assets"], "total_equity": self.bs["equity"]["total"],
            "notes": [(n["number"], n["title"]) for n in self.notes],
            "errors": sum(1 for x in self.validation if x["level"] == "error"),
            "_note": "amounts are unrounded; fmt() rounds at display",
            "warnings": sum(1 for x in self.validation if x["level"] == "warning"),
        }


def fy_labels(entity: dict) -> dict:
    """'2026-06-30' -> {'cy': '2026', 'py': '2025', 'long': '30 June 2026', 'long_py': '30 June 2025'}"""
    import datetime as dt
    end = dt.date.fromisoformat(entity["fy_end"])
    try:
        py_end = end.replace(year=end.year - 1)
    except ValueError:  # 29 Feb
        py_end = end.replace(year=end.year - 1, day=28)
    return {
        "cy": str(end.year), "py": str(py_end.year),
        "long": f"{end.day} {end.strftime('%B %Y')}", "long_py": f"{py_end.day} {py_end.strftime('%B %Y')}",
        "period": entity.get("period_label") or f"For the year ended {end.day} {end.strftime('%B %Y')}",
        "as_at": f"As at {end.day} {end.strftime('%B %Y')}",
        "col_cy": f"{end.day} {end.strftime('%B %Y')}".upper(), "col_py": f"{py_end.day} {py_end.strftime('%B %Y')}".upper(),
    }


if __name__ == "__main__":  # quick smoke test:  python fr_engine.py pack.json
    import sys
    p = Pack(load_json(sys.argv[1]))
    print(json.dumps(p.summary(), indent=2, default=str))
    print(p.validation_markdown())
