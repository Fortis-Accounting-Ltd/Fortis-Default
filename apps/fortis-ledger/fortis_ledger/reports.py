"""
Reports.

* Financial Reports pack - the firm's Xero report template look, produced by the report
  engine (fortis_ledger/reportengine) from the ledger's balances (current and prior year).
* Trial Balance, General Ledger (account transactions) and Journal Report - Xero-style tables,
  as PDF or Excel.
"""
from __future__ import annotations

import copy
import datetime as dt
from decimal import Decimal
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, Side
from openpyxl.utils import get_column_letter
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas as rl_canvas
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from .engine import BOILERPLATE, REPORT_MAP, fonts, fr_engine, render_pack
from .ledger import Ledger
from .money import fmt

BLUE = colors.HexColor("#06B3E8")
RULE = colors.HexColor("#D9D9D9")
DARK = colors.HexColor("#8C8C8C")
NUM_FMT = '#,##0.00;(#,##0.00);"-"'


# ---------------------------------------------------------------------------------------- #
# Financial Reports pack
# ---------------------------------------------------------------------------------------- #
def financial_reports(ledger: Ledger, fy: int, out_path: str | Path, final: bool = False,
                      upto: dt.date | None = None, date_signed: str = "", order: list[str] | None = None) -> dict:
    data = ledger.pack(fy, upto, date_signed)
    pack = fr_engine.Pack(data, REPORT_MAP)
    boiler = copy.deepcopy(BOILERPLATE)
    for k, v in (ledger.client.options.get("boilerplate_overrides") or {}).items():
        if isinstance(v, dict) and isinstance(boiler.get(k), dict):
            boiler[k].update(v)
        else:
            boiler[k] = v
    draft = (not final) or pack.has_errors()
    render_pack.build_pdf(pack, out_path, boiler, fonts(), order, draft=draft)
    ledger.store.audit(ledger.slug, "report.generated", {"fy": fy, "report": "financial_reports", "final": not draft,
                                                         "file": Path(out_path).name})
    return {"path": str(out_path), "draft": draft, "validation": pack.validation, "summary": pack.summary(),
            "has_errors": pack.has_errors()}


# ---------------------------------------------------------------------------------------- #
# table reports - common PDF / Excel builders
# ---------------------------------------------------------------------------------------- #
class _Canvas(rl_canvas.Canvas):
    """Footer 'Report | Entity' and 'Page x of y' on every page."""

    def __init__(self, *a, footer_left: str = "", font: str = "Helvetica", **k):
        super().__init__(*a, **k)
        self._pages = []
        self._footer_left = footer_left
        self._font = font

    def showPage(self):
        self._pages.append(dict(self.__dict__))
        self._startPage()

    def save(self):
        total = len(self._pages)
        for st in self._pages:
            self.__dict__.update(st)
            w = self._pagesize[0]
            self.setFont(self._font, 7)
            self.setFillColor(colors.black)
            self.drawString(17 * mm, 12 * mm, self._footer_left)
            self.drawRightString(w - 17 * mm, 12 * mm, f"Page {self._pageNumber} of {total}")
            self.setStrokeColor(RULE)
            self.line(17 * mm, 16 * mm, w - 17 * mm, 16 * mm)
            rl_canvas.Canvas.showPage(self)
        rl_canvas.Canvas.save(self)


def _styles():
    f = fonts()
    return {
        "title": ParagraphStyle("t", fontName=f["SB"], fontSize=20, leading=24, textColor=BLUE, spaceAfter=2),
        "sub": ParagraphStyle("s", fontName=f["SB"], fontSize=12, leading=15),
        "cell": ParagraphStyle("c", fontName=f["R"], fontSize=7.5, leading=9),
        "cellb": ParagraphStyle("cb", fontName=f["SB"], fontSize=7.5, leading=9),
        "head": ParagraphStyle("h", fontName=f["SB"], fontSize=7, leading=9),
    }


def _pdf_table(out_path, title: str, entity: str, period: str, header: list[str], rows: list[dict],
               widths: list[float], num_cols: set[int], landscape_page: bool = False) -> str:
    """rows: {'cells': [...], 'kind': 'line' | 'total' | 'heading' | 'spacer'}"""
    st = _styles()
    size = landscape(A4) if landscape_page else A4
    doc = SimpleDocTemplate(str(out_path), pagesize=size, leftMargin=17 * mm, rightMargin=17 * mm,
                            topMargin=18 * mm, bottomMargin=22 * mm, title=f"{title} - {entity}")
    story = [Paragraph(title, st["title"]), Paragraph(_esc(entity), st["sub"]), Paragraph(_esc(period), st["sub"]),
             Spacer(1, 6 * mm)]
    data = [[Paragraph(h.upper(), st["head"]) for h in header]]
    styles = [("LINEBELOW", (0, 0), (-1, 0), 0.8, DARK), ("VALIGN", (0, 0), (-1, -1), "TOP"),
              ("TOPPADDING", (0, 0), (-1, -1), 2), ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
              ("LEFTPADDING", (0, 0), (-1, -1), 2), ("RIGHTPADDING", (0, 0), (-1, -1), 2)]
    for ci in num_cols:
        styles.append(("ALIGN", (ci, 0), (ci, -1), "RIGHT"))
    for r in rows:
        i = len(data)
        kind = r.get("kind", "line")
        sty = st["cellb"] if kind in ("total", "heading") else st["cell"]
        cells = []
        for ci, v in enumerate(r["cells"]):
            if ci in num_cols:
                cells.append(Paragraph(v, ParagraphStyle("n", parent=sty, alignment=2)))
            else:
                cells.append(Paragraph(_esc(v), sty))
        data.append(cells)
        if kind == "line":
            styles.append(("LINEBELOW", (0, i), (-1, i), 0.3, RULE))
        elif kind == "total":
            styles.append(("LINEABOVE", (0, i), (-1, i), 0.6, DARK))
            styles.append(("LINEBELOW", (0, i), (-1, i), 0.6, DARK))
        elif kind == "heading":
            styles.append(("TOPPADDING", (0, i), (-1, i), 8))
    t = Table(data, colWidths=widths, repeatRows=1)
    t.setStyle(TableStyle(styles))
    story.append(t)
    f = fonts()
    doc.build(story, canvasmaker=lambda *a, **k: _Canvas(*a, footer_left=f"{title}  |  {entity}", font=f["R"], **k))
    return str(out_path)


def _xlsx(out_path, title: str, entity: str, period: str, header: list[str], rows: list[dict],
          num_cols: set[int], widths: list[int]) -> str:
    wb = Workbook()
    ws = wb.active
    ws.title = title[:31]
    ws.append([title])
    ws.append([entity])
    ws.append([period])
    ws.append([])
    ws.append(header)
    ws["A1"].font = Font(bold=True, size=14, color="06B3E8")
    for c in ws[5]:
        c.font = Font(bold=True)
        c.border = Border(bottom=Side(style="thin"))
    for r in rows:
        vals = []
        for ci, v in enumerate(r.get("values", r["cells"])):
            vals.append(float(v) if isinstance(v, Decimal) else v)
        ws.append(vals)
        if r.get("kind") in ("total", "heading"):
            for c in ws[ws.max_row]:
                c.font = Font(bold=True)
    for ci in num_cols:
        col = get_column_letter(ci + 1)
        for cell in ws[col][5:]:
            cell.number_format = NUM_FMT
            cell.alignment = Alignment(horizontal="right")
    for i, w in enumerate(widths):
        ws.column_dimensions[get_column_letter(i + 1)].width = w
    ws.freeze_panes = "A6"
    wb.save(out_path)
    return str(out_path)


def _esc(s) -> str:
    return (str(s or "")).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _n(d) -> str:
    return fmt(d) if d else "-"


# ---------------------------------------------------------------------------------------- #
# Trial Balance
# ---------------------------------------------------------------------------------------- #
def trial_balance(ledger: Ledger, fy: int, out_path: str | Path, upto: dt.date | None = None,
                  as_excel: bool = False) -> str:
    tb = ledger.trial_balance(fy, upto)
    header = ["Code", "Account", "Account type", "Report code", "Debit", "Credit"]
    rows = [{"cells": [r["code"], r["name"], r["type"], r["report_code"], _n(r["debit"]), _n(r["credit"])],
             "values": [r["code"], r["name"], r["type"], r["report_code"], r["debit"], r["credit"]]}
            for r in tb["rows"]]
    rows.append({"kind": "total", "cells": ["", "Total", "", "", _n(tb["total_debit"]), _n(tb["total_credit"])],
                 "values": ["", "Total", "", "", tb["total_debit"], tb["total_credit"]]})
    period = f"As at {tb['as_at'].day} {tb['as_at']:%B %Y}"
    ledger.store.audit(ledger.slug, "report.generated", {"fy": fy, "report": "trial_balance", "file": Path(out_path).name})
    if as_excel:
        return _xlsx(out_path, "Trial Balance", ledger.client.name, period, header, rows, {4, 5}, [12, 44, 20, 22, 16, 16])
    return _pdf_table(out_path, "Trial Balance", ledger.client.name, period, header, rows,
                      [16 * mm, 68 * mm, 28 * mm, 30 * mm, 17 * mm, 17 * mm], {4, 5})


# ---------------------------------------------------------------------------------------- #
# General Ledger (account transactions)
# ---------------------------------------------------------------------------------------- #
def general_ledger(ledger: Ledger, fy: int, out_path: str | Path, start: dt.date | None = None,
                   end: dt.date | None = None, codes: list[str] | None = None, as_excel: bool = False) -> str:
    data = ledger.general_ledger(fy, start, end, codes)
    s, e = ledger.fy_range(fy)
    start, end = max(start or s, s), min(end or e, e)
    header = ["Date", "Journal", "Narration / description", "Debit", "Credit", "Balance"]
    rows = []
    for acc in data:
        rows.append({"kind": "heading", "cells": ["", "", f"{acc['code']} {acc['name']}", "", "", ""],
                     "values": ["", "", f"{acc['code']} {acc['name']}", None, None, None]})
        rows.append({"cells": ["", "", "Opening balance", "", "", _n(acc["opening"])],
                     "values": ["", "", "Opening balance", None, None, acc["opening"]]})
        for x in acc["entries"]:
            text = x["narration"] + (f" - {x['description']}" if x["description"] else "")
            rows.append({"cells": [f"{x['date']:%d/%m/%Y}", x["number"], text, _n(x["debit"]), _n(x["credit"]),
                                   _n(x["balance"])],
                         "values": [x["date"], x["number"], text, x["debit"], x["credit"], x["balance"]]})
        rows.append({"kind": "total", "cells": ["", "", f"Total {acc['code']} {acc['name']}", _n(acc["debits"]),
                                                _n(acc["credits"]), _n(acc["closing"])],
                     "values": ["", "", f"Total {acc['code']} {acc['name']}", acc["debits"], acc["credits"], acc["closing"]]})
    period = f"{start.day} {start:%B %Y} to {end.day} {end:%B %Y}"
    ledger.store.audit(ledger.slug, "report.generated", {"fy": fy, "report": "general_ledger", "file": Path(out_path).name})
    if as_excel:
        return _xlsx(out_path, "General Ledger", ledger.client.name, period, header, rows, {3, 4, 5}, [12, 13, 60, 16, 16, 16])
    return _pdf_table(out_path, "General Ledger", ledger.client.name, period, header, rows,
                      [18 * mm, 20 * mm, 76 * mm, 21 * mm, 21 * mm, 21 * mm], {3, 4, 5})


# ---------------------------------------------------------------------------------------- #
# Journal Report
# ---------------------------------------------------------------------------------------- #
def journal_report(ledger: Ledger, fy: int, out_path: str | Path, include_void: bool = True,
                   as_excel: bool = False) -> str:
    statuses = ("posted", "void") if include_void else ("posted",)
    header = ["Date", "Journal", "Account", "Description", "Debit", "Credit"]
    rows = []
    for j in ledger.journals(fy, statuses=statuses):
        status = " (VOID)" if j.status == "void" else ""
        rows.append({"kind": "heading", "cells": [f"{j.date:%d/%m/%Y}", j.number, j.narration + status, j.kind, "", ""],
                     "values": [j.date, j.number, j.narration + status, j.kind, None, None]})
        for ln in j.lines:
            a = ledger.account(ln.account)
            name = f"{ln.account} {a.name}" if a else ln.account
            rows.append({"cells": ["", "", name, ln.description, _n(ln.debit), _n(ln.credit)],
                         "values": ["", "", name, ln.description, ln.debit, ln.credit]})
        rows.append({"kind": "total", "cells": ["", "", "", "Total", _n(j.total_debit), _n(j.total_credit)],
                     "values": ["", "", "", "Total", j.total_debit, j.total_credit]})
    s, e = ledger.fy_range(fy)
    period = f"{s.day} {s:%B %Y} to {e.day} {e:%B %Y}"
    ledger.store.audit(ledger.slug, "report.generated", {"fy": fy, "report": "journal_report", "file": Path(out_path).name})
    if as_excel:
        return _xlsx(out_path, "Journal Report", ledger.client.name, period, header, rows, {4, 5}, [12, 13, 44, 40, 16, 16])
    return _pdf_table(out_path, "Journal Report", ledger.client.name, period, header, rows,
                      [18 * mm, 20 * mm, 58 * mm, 46 * mm, 17 * mm, 17 * mm], {4, 5})


REPORTS = {
    "financial_reports": "Financial Reports pack",
    "trial_balance": "Trial Balance",
    "general_ledger": "General Ledger",
    "journal_report": "Journal Report",
}
