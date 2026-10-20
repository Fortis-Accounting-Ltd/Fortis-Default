#!/usr/bin/env python3
"""
render_pack.py - render a ledger pack (pack.json) into the Fortis "Financial
Reports" PDF, mimicking the Xero report-template look:

  cover -> contents -> the entity type's reports in order, e.g.
    company:      P&L -> Balance Sheet -> Movements in Equity -> Notes -> Depreciation -> Directors Declaration -> Compilation
    trust:        Income Statement -> Balance Sheet -> Trust Income Distribution -> Beneficiary Accounts -> Notes
                  -> Rental Schedule -> Depreciation -> Trustee Declaration -> Compilation
    partnership:  P&L -> Rental Schedule -> Partner Accounts -> Balance Sheet -> Notes -> Depreciation
                  -> Partner Declaration -> Compilation
    sole trader:  P&L -> Movements in Equity -> Balance Sheet -> Notes -> Depreciation
  (report-map.json entity_layouts; pack options.report_order overrides)

Usage:
    python render_pack.py pack.json out.pdf [--map report-map.json]
                          [--boilerplate boilerplate.json] [--font-dir DIR]
                          [--order pnl,bs,equity,notes,depreciation,declaration,compilation]
                          [--draft] [--allow-errors]

Requires: reportlab (pip install reportlab).  Fonts: assets/fonts/SourceSans3-*.ttf
(falls back to Helvetica if missing).
"""
from __future__ import annotations

import argparse
import io
import re
import sys
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas as rl_canvas
from reportlab.platypus import (BaseDocTemplate, Flowable, Frame, KeepTogether, NextPageTemplate,
                                PageBreak, PageTemplate, Paragraph, Spacer, Table, TableStyle)

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fr_engine import Pack, fmt, fy_labels, load_boilerplate, load_json, load_map  # noqa: E402

SKILL_DIR = Path(__file__).resolve().parent.parent
BLUE = colors.HexColor("#06B3E8")
RULE_DARK = colors.HexColor("#8C8C8C")
RULE_LIGHT = colors.HexColor("#D9D9D9")
BLACK = colors.black

PAGE_W, PAGE_H = A4
MARGIN_L = MARGIN_R = 17 * mm
MARGIN_T = 22 * mm
MARGIN_B = 22 * mm


# --------------------------------------------------------------------------- #
# fonts
# --------------------------------------------------------------------------- #
def register_fonts(font_dir: Path | None) -> dict:
    font_dir = font_dir or (SKILL_DIR / "assets" / "fonts")
    faces = {"R": "SourceSans3-Regular.ttf", "SB": "SourceSans3-SemiBold.ttf",
             "B": "SourceSans3-Bold.ttf", "I": "SourceSans3-Italic.ttf"}
    names = {}
    ok = True
    for key, fn in faces.items():
        p = font_dir / fn
        if not p.exists():
            ok = False
            break
        name = fn[:-4]
        pdfmetrics.registerFont(TTFont(name, str(p)))
        names[key] = name
    if not ok:
        names = {"R": "Helvetica", "SB": "Helvetica-Bold", "B": "Helvetica-Bold", "I": "Helvetica-Oblique"}
    else:
        from reportlab.pdfbase.pdfmetrics import registerFontFamily
        registerFontFamily("SourceSans3-Regular", normal=names["R"], bold=names["SB"], italic=names["I"], boldItalic=names["B"])
    return names


# --------------------------------------------------------------------------- #
# canvas with "Page x of y" + per-page metadata
# --------------------------------------------------------------------------- #
class NumberedCanvas(rl_canvas.Canvas):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._saved_page_states = []
        self._fr_nofooter = False
        self._fr_footnote = None
        self._fr_title = None
        self._fr_title_page = None
        self._fr_entity = ""
        self._fr_footer_left = "Financial Reports"
        self._fr_fonts = {}
        self._fr_draft = False

    def showPage(self):
        self._saved_page_states.append(dict(self.__dict__))
        self._startPage()
        self._fr_nofooter = False  # only cover/contents opt out, one page at a time

    def save(self):
        total = len(self._saved_page_states)
        for state in self._saved_page_states:
            self.__dict__.update(state)
            if not state.get("_fr_nofooter"):
                self._draw_page_number(total)
            if self._fr_draft:
                self._draw_draft()
            rl_canvas.Canvas.showPage(self)
        rl_canvas.Canvas.save(self)

    def _draw_draft(self):
        """Pale diagonal DRAFT stamp - used until every mapping line has been reviewed."""
        w, h = self._pagesize
        self.saveState()
        self.setFillColor(colors.Color(0.85, 0.1, 0.1, alpha=0.12))
        self.setFont(self._fr_fonts.get("B", "Helvetica-Bold"), 96)
        self.translate(w / 2, h / 2)
        self.rotate(40)
        self.drawCentredString(0, -30, "DRAFT")
        self.restoreState()

    def _draw_page_number(self, total: int):
        w = self._pagesize[0]
        f = self._fr_fonts
        self.setFont(f.get("R", "Helvetica"), 7)
        self.setFillColor(BLACK)
        self.drawRightString(w - MARGIN_R, 13 * mm, f"Page {self._pageNumber} of {total}")


class Meta(Flowable):
    """Zero-height flowable that stamps page metadata onto the canvas."""

    def __init__(self, footnote=None, title=None, nofooter=False, mark_title_page=False):
        super().__init__()
        self.footnote, self.title, self.nofooter, self.mark = footnote, title, nofooter, mark_title_page
        self.width = self.height = 0

    def wrap(self, aw, ah):
        return 0, 0

    def draw(self):
        c = self.canv
        if self.footnote is not None:
            c._fr_footnote = self.footnote or None
        if self.title is not None:
            c._fr_title = self.title
        if self.mark:
            c._fr_title_page = c.getPageNumber()
        if self.nofooter:
            c._fr_nofooter = True


class StartMark(Flowable):
    """Zero-size flowable that records the page a report starts on (Contents page numbers)."""

    def __init__(self, key: str, sink: dict):
        super().__init__()
        self.key, self.sink = key, sink
        self.width = self.height = 0

    def wrap(self, aw, ah):
        return 0, 0

    def draw(self):
        self.sink.setdefault(self.key, self.canv.getPageNumber())


def on_page_end(canv: NumberedCanvas, doc):
    """Footer (left), footnote and continuation title - page number is added at save()."""
    if getattr(canv, "_fr_nofooter", False):
        return
    w, h = canv._pagesize
    f = canv._fr_fonts
    canv.saveState()
    # footer left: "Financial Reports | Entity"
    canv.setFont(f["R"], 7)
    canv.setFillColor(BLACK)
    x = MARGIN_L
    canv.drawString(x, 13 * mm, canv._fr_footer_left)
    x += pdfmetrics.stringWidth(canv._fr_footer_left, f["R"], 7) + 6
    canv.setStrokeColor(RULE_DARK)
    canv.setLineWidth(0.4)
    canv.line(x, 12 * mm, x, 16 * mm)
    canv.drawString(x + 6, 13 * mm, canv._fr_entity)
    # rule above footer
    canv.setStrokeColor(RULE_LIGHT)
    canv.line(MARGIN_L, 19 * mm, w - MARGIN_R, 19 * mm)
    # footnote sits just above the rule
    if canv._fr_footnote:
        style = ParagraphStyle("fn", fontName=f["R"], fontSize=7.5, leading=9.5)
        p = Paragraph(canv._fr_footnote, style)
        pw, ph = p.wrap(w - MARGIN_L - MARGIN_R, 40)
        p.drawOn(canv, MARGIN_L, 21 * mm)
    # continuation header
    if canv._fr_title and canv._fr_title_page != canv.getPageNumber():
        canv.setFont(f["R"], 9)
        canv.setFillColor(BLACK)
        canv.drawString(MARGIN_L, h - 15 * mm, canv._fr_title)
    canv.restoreState()


# --------------------------------------------------------------------------- #
# renderer
# --------------------------------------------------------------------------- #
class Renderer:
    def __init__(self, pack: Pack, boiler: dict, fonts: dict, order: list[str] | None = None):
        self.p = pack
        self.b = boiler
        self.f = fonts
        self.fy = fy_labels(pack.entity)
        self.etype = pack.etype
        self.order = order or pack.reports()
        self.rt = dict(boiler["report_titles"])
        self.rt.update((boiler.get("report_titles_by_type") or {}).get(self.etype) or {})
        self.rt.update(pack.layout.get("titles") or {})
        self.foot = pack.map.get("footnotes") or {}
        self.has_compilation = "compilation" in self.order
        self.page_of: dict[str, int] = {}               # report -> first page, recorded while building
        self.contents_pages: dict[str, int] | None = None  # page numbers from the first pass
        R, SB, B = fonts["R"], fonts["SB"], fonts["B"]
        self.s = {
            "title": ParagraphStyle("title", fontName=SB, fontSize=22, leading=26, textColor=BLUE, spaceAfter=4),
            "entity": ParagraphStyle("entity", fontName=SB, fontSize=14, leading=17),
            "period": ParagraphStyle("period", fontName=SB, fontSize=14, leading=17, spaceAfter=2),
            "cover_title": ParagraphStyle("ct", fontName=B, fontSize=30, leading=36, textColor=BLUE, spaceAfter=10),
            "cover_line": ParagraphStyle("cl", fontName=R, fontSize=16, leading=21),
            "cover_small": ParagraphStyle("cs", fontName=R, fontSize=8, leading=11, spaceBefore=12),
            "contents_title": ParagraphStyle("cot", fontName=SB, fontSize=22, leading=26, textColor=BLUE, spaceAfter=18),
            "contents_item": ParagraphStyle("coi", fontName=R, fontSize=11, leading=15, spaceAfter=10),
            "contents_row": ParagraphStyle("cor", fontName=R, fontSize=9.5, leading=12),
            "contents_page": ParagraphStyle("cop", fontName=B, fontSize=9.5, leading=12),
            "colhead": ParagraphStyle("ch", fontName=SB, fontSize=7, leading=9, alignment=TA_RIGHT),
            "colhead_l": ParagraphStyle("chl", fontName=SB, fontSize=7, leading=9, alignment=TA_LEFT),
            "section": ParagraphStyle("sec", fontName=SB, fontSize=10, leading=12),
            "subsection": ParagraphStyle("sub", fontName=SB, fontSize=8, leading=10),
            "line": ParagraphStyle("line", fontName=R, fontSize=8, leading=10),
            "total": ParagraphStyle("tot", fontName=SB, fontSize=8, leading=10),
            "grand": ParagraphStyle("grand", fontName=SB, fontSize=10, leading=12),
            "num": ParagraphStyle("num", fontName=R, fontSize=8, leading=10, alignment=TA_RIGHT),
            "num_b": ParagraphStyle("numb", fontName=SB, fontSize=8, leading=10, alignment=TA_RIGHT),
            "body": ParagraphStyle("body", fontName=R, fontSize=8.5, leading=11.5, spaceAfter=7),
            "body_h": ParagraphStyle("bodyh", fontName=SB, fontSize=8.5, leading=11.5, spaceBefore=4, spaceAfter=4),
            "note_h": ParagraphStyle("noteh", fontName=SB, fontSize=10, leading=12, spaceBefore=6, spaceAfter=6),
            "sig_name": ParagraphStyle("sig", fontName=R, fontSize=8.5, leading=11),
            "sig_label": ParagraphStyle("sigl", fontName=R, fontSize=8.5, leading=11),
            "sig_bold": ParagraphStyle("sigb", fontName=SB, fontSize=8.5, leading=11),
            "dep_head": ParagraphStyle("dh", fontName=SB, fontSize=6, leading=7.5, alignment=TA_RIGHT),
            "dep_head_l": ParagraphStyle("dhl", fontName=SB, fontSize=6, leading=7.5, alignment=TA_LEFT),
            "dep_line": ParagraphStyle("dl", fontName=R, fontSize=6.8, leading=8.2),
            "dep_num": ParagraphStyle("dn", fontName=R, fontSize=6.8, leading=8.2, alignment=TA_RIGHT),
            "dep_num_b": ParagraphStyle("dnb", fontName=SB, fontSize=6.8, leading=8.2, alignment=TA_RIGHT),
            "dep_tot": ParagraphStyle("dt", fontName=SB, fontSize=6.8, leading=8.2),
            "dep_group": ParagraphStyle("dg", fontName=SB, fontSize=9, leading=11),
        }
        self.usable_w = PAGE_W - MARGIN_L - MARGIN_R
        self.decimals = int(pack.options.get("decimals") or 0)
        self.one_col = pack.single_column

    def fmt(self, v) -> str:
        return fmt(v, self.decimals)

    # ---- small helpers -------------------------------------------------- #
    def P(self, text, style):
        return Paragraph(text, self.s[style])

    def footnote(self, key: str) -> str:
        """The statements footnote prints only on the reports the firm's Xero packs put it on."""
        if key == "notes":
            return self.foot.get("notes", "") if self.has_compilation else ""
        if key not in (self.foot.get("statement_reports") or []):
            return ""
        return self.foot.get("statements", "") if self.has_compilation else self.foot.get("statements_no_compilation", "")

    def cols(self, bs: bool = False) -> list[str]:
        if bs:
            return [self.fy["col_cy"]] if self.one_col else [self.fy["col_cy"], self.fy["col_py"]]
        return [self.fy["cy"]] if self.one_col else [self.fy["cy"], self.fy["py"]]

    def header(self, title: str, period: str | None = None, mark=True) -> list:
        ent = self.p.entity["name"]
        return [Meta(title=title, mark_title_page=mark), self.P(title, "title"), self.P(ent, "entity"),
                self.P(period or self.fy["period"], "period"), Spacer(1, 2)]

    def _num(self, v, bold=False):
        return Paragraph(self.fmt(v), self.s["num_b" if bold else "num"])

    @staticmethod
    def _keep_together(rows: list[dict]) -> list:
        """
        NOSPLIT ranges so a page never starts with an orphaned total or computed line, and a
        heading is never left at the foot of a page: short blocks (heading .. total, <= 20 rows)
        stay whole; a long block keeps its heading with the first two lines and its last two
        lines with the total and any computed line straight after it.  Table row = rows index + 1.
        """
        cmds = []
        n = len(rows)
        heads = [i for i, r in enumerate(rows) if r["kind"] in ("section", "subsection")]
        for h in heads:
            end = h
            j = h + 1
            while j < n and rows[j]["kind"] not in ("section", "grand"):
                if rows[j]["kind"] == "subsection" and rows[h]["kind"] == "subsection":
                    break
                end = j
                j += 1
            if end - h <= 20:
                cmds.append(("NOSPLIT", (0, h + 1), (-1, end + 1)))
            else:
                cmds.append(("NOSPLIT", (0, h + 1), (-1, min(h + 2, end) + 1)))
        for i, r in enumerate(rows):
            if r["kind"] in ("total", "grand"):
                start = i
                k = i - 1
                taken = 0
                while k >= 0 and taken < 2 and rows[k]["kind"] in ("line", "total", "spacer"):
                    if rows[k]["kind"] != "spacer":
                        taken += 1
                    start = k
                    k -= 1
                stop = i
                if r["kind"] == "total":
                    m = i + 1
                    while m < n and rows[m]["kind"] == "spacer":
                        m += 1
                    if m < n and rows[m]["kind"] == "grand":
                        stop = m
                if stop > start:
                    cmds.append(("NOSPLIT", (0, start + 1), (-1, stop + 1)))
        return cmds

    def _rows_table(self, rows: list[dict], col_labels: list[str], with_notes=False) -> Table:
        """
        rows: dicts with kind in {section, subsection, line, total, grand, spacer}
        Builds the Xero-style ruled table with one or two amount columns.
        """
        ncols = len(col_labels)
        num_w = 72
        note_w = 36 if with_notes else 0
        label_w = self.usable_w - 2 * num_w - note_w
        data, style = [], []
        head = [Paragraph("", self.s["colhead_l"])]
        if with_notes:
            head.append(Paragraph("NOTES", self.s["colhead"]))
        head += [Paragraph(lab, self.s["colhead"]) for lab in col_labels]
        if ncols == 1:
            head.append("")
        data.append(head)
        style += [("LINEBELOW", (0, 0), (-1, 0), 0.6, RULE_DARK), ("BOTTOMPADDING", (0, 0), (-1, 0), 3)]
        blank = [""] * (len(head))
        for r in rows:
            i = len(data)
            kind = r["kind"]
            if kind == "spacer":
                data.append(list(blank))
                style.append(("TOPPADDING", (0, i), (-1, i), 0))
                style.append(("BOTTOMPADDING", (0, i), (-1, i), r.get("h", 2)))
                continue
            indent = r.get("indent", 0) * 9
            if kind == "section":
                lab, nums = self.P(r["label"], "section"), ["", ""]
                style.append(("LINEBELOW", (0, i), (-1, i), 0.6, RULE_DARK))
                style.append(("TOPPADDING", (0, i), (-1, i), 4))
            elif kind == "subsection":
                lab, nums = self.P(r["label"], "subsection"), ["", ""]
                style.append(("LINEBELOW", (0, i), (-1, i), 0.3, RULE_LIGHT))
            elif kind == "line":
                lab = self.P(r["label"], "line")
                nums = [self._num(r["cy"]), self._num(r["py"])]
                style.append(("LINEBELOW", (0, i), (-1, i), 0.3, RULE_LIGHT))
            elif kind == "total":
                lab = self.P(r["label"], "total")
                nums = [self._num(r["cy"], True), self._num(r["py"], True)]
                style.append(("LINEBELOW", (0, i), (-1, i), 0.3, RULE_LIGHT))
            elif kind == "grand":
                lab = self.P(r["label"], "grand")
                nums = [self._num(r["cy"], True), self._num(r["py"], True)]
                style.append(("LINEBELOW", (0, i), (-1, i), 0.6, RULE_DARK))
                style.append(("TOPPADDING", (0, i), (-1, i), 3))
            else:
                continue
            if ncols == 1:
                nums = [nums[0], ""]
            row = [lab]
            if with_notes:
                row.append(Paragraph(str(r.get("note", "") or ""), self.s["num"]))
            row += nums
            data.append(row)
            style.append(("LEFTPADDING", (0, i), (0, i), 2 + indent))
        widths = [label_w] + ([note_w] if with_notes else []) + [num_w, num_w]
        style += self._keep_together(rows)
        t = Table(data, colWidths=widths, repeatRows=1)
        base = [("VALIGN", (0, 0), (-1, -1), "BOTTOM"), ("TOPPADDING", (0, 0), (-1, -1), 2.2),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 1.8), ("LEFTPADDING", (0, 0), (-1, -1), 2),
                ("RIGHTPADDING", (0, 0), (-1, -1), 2)]
        t.setStyle(TableStyle(base + style))
        return t

    # ---- cover & contents ---------------------------------------------- #
    def cover(self) -> list:
        e = self.p.entity
        out = [Meta(nofooter=True, footnote=""), Spacer(1, PAGE_H * 0.40),
               self.P(self.b["firm"]["pack_title"], "cover_title"), self.P(e["name"], "cover_line")]
        if e.get("abn"):
            out.append(self.P(f"ABN {e['abn']}", "cover_line"))
        out += [self.P(self.fy["period"], "cover_line"), self.P(self.b["firm"]["prepared_by"], "cover_small"), PageBreak()]
        return out

    def _present(self, key: str) -> bool:
        return {"depreciation": bool(self.p.depn), "rental": bool(self.p.rental), "persons": bool(self.p.persons),
                "distribution": bool(self.p.distribution)}.get(key, True)

    def contents(self) -> list:
        nofooter = not self.foot.get("contents_footer", True)
        out = [Meta(nofooter=nofooter, footnote=""), self.P(self.rt["contents"], "contents_title"), Spacer(1, 6)]
        keys = [k for k in self.order if self._present(k)]
        if not self.p.options.get("contents_page_numbers", True):
            out += [self.P(self._report_title(k), "contents_item") for k in keys]
            out.append(PageBreak())
            return out
        pages = self.contents_pages or {}
        rows = [[self.P(str(pages.get(k, "")), "contents_page"), self.P(self._report_title(k), "contents_row")]
                for k in keys]
        t = Table(rows, colWidths=[10 * mm, None], hAlign="LEFT")
        t.setStyle(TableStyle([("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                               ("TOPPADDING", (0, 0), (-1, -1), 0), ("BOTTOMPADDING", (0, 0), (-1, -1), 11),
                               ("VALIGN", (0, 0), (-1, -1), "TOP")]))
        out += [t, PageBreak()]
        return out

    def _report_title(self, key: str) -> str:
        if key == "depreciation" and self.p.depn:
            return self.p.depn["title"]
        if key == "declaration":
            return self._declaration_cfg().get("title", self.rt["declaration"])
        if key == "persons" and self.p.persons:
            return self.p.persons["title"]
        if key == "distribution" and self.p.distribution:
            return self.p.distribution["title"]
        if key == "rental" and self.p.rental:
            return self.p.rental["title"]
        return self.rt[key]

    # ---- profit and loss ----------------------------------------------- #
    def pnl(self) -> list:
        rows: list[dict] = []
        for sec in self.p.pnl["sections"]:
            if sec.get("computed"):
                if not sec.get("show", True):
                    continue
                rows.append({"kind": "grand", "label": sec["label"], "cy": sec["cy"], "py": sec["py"]})
                rows.append({"kind": "spacer"})
                continue
            if not sec.get("show"):
                continue
            rows.append({"kind": "section", "label": sec["heading"]})
            for l in sec["lines"]:
                rows.append({"kind": "line", "label": l["label"], "cy": l["cy"], "py": l["py"], "indent": 1})
            rows.append({"kind": "total", "label": sec["total_label"], "cy": sec["cy"], "py": sec["py"], "indent": 1})
            rows.append({"kind": "spacer"})
        out = [Meta(footnote=self.footnote("pnl"))]
        out += self.header(self._report_title("pnl"))
        out.append(self._rows_table(rows, self.cols()))
        out.append(PageBreak())
        return out

    # ---- rental schedule ------------------------------------------------ #
    def rental(self) -> list:
        r = self.p.rental
        if not r:
            return []
        rows = [{"kind": "section", "label": "Profit/(Loss)"}, {"kind": "subsection", "label": "Income", "indent": 1}]
        rows += [{"kind": "line", "label": a["display_name"], "cy": a["cy"], "py": a["py"], "indent": 2} for a in r["income"]]
        rows.append({"kind": "total", "label": "Total Income", "indent": 2, **r["income_total"]})
        rows.append({"kind": "spacer", "h": 3})
        rows.append({"kind": "subsection", "label": "Expenses", "indent": 1})
        rows += [{"kind": "line", "label": a["display_name"], "cy": a["cy"], "py": a["py"], "indent": 2} for a in r["expenses"]]
        rows.append({"kind": "total", "label": "Total Expenses", "indent": 2, **r["expenses_total"]})
        rows.append({"kind": "spacer"})
        rows.append({"kind": "grand", "label": "Total Profit/(Loss)", **r["net"]})
        out = [Meta(footnote=self.footnote("rental"))]
        out += self.header(self._report_title("rental"))
        out.append(self._rows_table(rows, self.cols()))
        out.append(PageBreak())
        return out

    # ---- partner / beneficiary accounts ---------------------------------- #
    def persons(self) -> list:
        ps = self.p.persons
        if not ps:
            return []
        rows = [{"kind": "section", "label": ps["title"]}, {"kind": "subsection", "label": ps["summary_title"], "indent": 1}]
        rows += [{"kind": "line", "label": r["label"], "cy": r["cy"], "py": r["py"], "indent": 2} for r in ps["summary"]]
        rows.append({"kind": "total", "label": f"Total {ps['summary_title']}", "cy": ps["cy"], "py": ps["py"], "indent": 2})
        rows.append({"kind": "spacer", "h": 6})
        rows.append({"kind": "subsection", "label": ps["detail_title"], "indent": 1})
        for person in ps["persons"]:
            rows.append({"kind": "subsection", "label": person["heading"], "indent": 2})
            rows += [{"kind": "line", "label": r["label"], "cy": r["cy"], "py": r["py"], "indent": 3} for r in person["rows"]]
            rows.append({"kind": "total", "label": f"Total {person['heading']}", "cy": person["cy"], "py": person["py"], "indent": 3})
            rows.append({"kind": "spacer", "h": 3})
        rows.append({"kind": "total", "label": f"Total {ps['detail_title']}", "cy": ps["cy"], "py": ps["py"], "indent": 2})
        out = [Meta(footnote=self.footnote("persons"))]
        out += self.header(self._report_title("persons"))
        out.append(self._rows_table(rows, self.cols()))
        out.append(PageBreak())
        return out

    # ---- trust income distribution ---------------------------------------- #
    def distribution(self) -> list:
        d = self.p.distribution
        if not d:
            return []
        rows = [{"kind": "section", "label": d["heading"]}]
        for r in d["rows"]:
            kind = {"heading": "subsection", "computed": "grand"}.get(r["kind"], r["kind"])
            row = {"kind": kind, "label": r["label"], "indent": r.get("indent", 1 if kind != "grand" else 0)}
            if kind != "subsection":
                row.update({"cy": r["cy"], "py": r["py"]})
            rows.append(row)
            if kind == "grand":
                rows.append({"kind": "spacer"})
        out = [Meta(footnote=self.footnote("distribution"))]
        out += self.header(self._report_title("distribution"))
        out.append(self._rows_table(rows, self.cols()))
        out.append(PageBreak())
        return out

    # ---- balance sheet ------------------------------------------------- #
    def bs(self) -> list:
        b = self.p.bs
        cfg = b["cfg"]
        rows: list[dict] = []

        def side(name):
            s = b[name]
            c = cfg[name]
            if not (s["current"] or s["non_current"]):
                return
            rows.append({"kind": "section", "label": c["heading"]})
            for part in ("current", "non_current"):
                lines = s[part]
                if not lines:
                    continue
                rows.append({"kind": "subsection", "label": c[part]["heading"], "indent": 1})
                for l in lines:
                    rows.append({"kind": "line", "label": l["label"], "note": l["note"], "cy": l["cy"], "py": l["py"], "indent": 2})
                tot = {k: sum(x[k] for x in lines) for k in ("cy", "py")}
                rows.append({"kind": "total", "label": c[part]["total"], "cy": tot["cy"], "py": tot["py"], "indent": 2})
                rows.append({"kind": "spacer", "h": 3})
            rows.append({"kind": "total", "label": c["total"], "cy": s["total"]["cy"], "py": s["total"]["py"], "indent": 1})
            rows.append({"kind": "spacer"})

        side("assets")
        side("liabilities")
        rows.append({"kind": "grand", "label": cfg["net_assets"], "cy": b["net_assets"]["cy"], "py": b["net_assets"]["py"]})
        rows.append({"kind": "spacer"})
        rows.append({"kind": "section", "label": cfg["equity"]["heading"]})
        for l in b["equity"]["rows"]:
            rows.append({"kind": "line", "label": l["label"], "note": l.get("note", ""), "cy": l["cy"], "py": l["py"], "indent": 1})
        rows.append({"kind": "total", "label": cfg["equity"]["total"], "cy": b["equity"]["total"]["cy"], "py": b["equity"]["total"]["py"], "indent": 1})
        out = [Meta(footnote=self.footnote("bs"))]
        out += self.header(self._report_title("bs"), self.fy["as_at"])
        out.append(self._rows_table(rows, self.cols(bs=True), with_notes=True))
        out += self.fx_block()
        out.append(PageBreak())
        return out

    def fx_block(self) -> list:
        fx = self.p.options.get("fx_rates")
        if not fx:
            return []
        out = [Spacer(1, 14), self.P(self.b["fx_note"], "body")]
        for blk in fx:
            out.append(Paragraph(f"&bull; <b>{blk['date']}</b>", self.s["body"]))
            for r in blk["rates"]:
                out.append(Paragraph(f"&nbsp;&nbsp;&nbsp;&nbsp;{r}", self.s["body"]))
        return out

    # ---- movements in equity ------------------------------------------- #
    def equity(self) -> list:
        e = self.p.equity
        es = self.p.equity_labels
        rows = [{"kind": "section", "label": es["heading"]},
                {"kind": "line", "label": es["opening"], "cy": e["opening"]["cy"], "py": e["opening"]["py"], "indent": 1},
                {"kind": "spacer", "h": 3},
                {"kind": "subsection", "label": es["increases"], "indent": 1}]
        for m in e["movements"]:
            rows.append({"kind": "line", "label": m["label"], "cy": m["cy"], "py": m["py"], "indent": 2})
        rows.append({"kind": "total", "label": es["total_increases"], "cy": e["total_increases"]["cy"], "py": e["total_increases"]["py"], "indent": 2})
        rows.append({"kind": "spacer"})
        rows.append({"kind": "grand", "label": es["total"], "cy": e["total"]["cy"], "py": e["total"]["py"]})
        out = [Meta(footnote=self.footnote("equity"))]
        out += self.header(self._report_title("equity"))
        out.append(self._rows_table(rows, self.cols()))
        out.append(PageBreak())
        return out

    # ---- notes --------------------------------------------------------- #
    def _subs(self) -> dict:
        e = self.p.entity
        return {"entity": e["name"], "legal_name": e.get("legal_name") or e["name"], "fy_long": self.fy["long"],
                "proprietor": e.get("proprietor") or ", ".join(e.get("directors") or []) or e["name"],
                "trustee_ref": e.get("trustee") if (e.get("trustee") and self._trustee_kind() == "corporate") else "the trustee company"}

    def notes(self) -> list:
        pol = self.b["policies"]
        subs = self._subs()
        out = [Meta(footnote=self.footnote("notes"))]
        out += self.header(self._report_title("notes"))
        out.append(self.P(pol["heading"], "note_h"))
        for para in (pol.get("basis_by_type") or {}).get(self.etype) or pol["basis"]:
            out.append(self.P(para.format(**subs), "body"))
        letters = "bcdefghijk"
        idx = 0
        out.append(self.P(pol["ppe"]["heading"], "body_h"))
        out.append(self.P((pol["ppe"].get("text_by_type") or {}).get(self.etype) or pol["ppe"]["text"], "body"))
        idx += 1
        inc_inv = self.p.options.get("include_inventory_policy")
        if inc_inv is None:
            inc_inv = any(n["key"] == "inventory" for n in self.p.notes)
        if inc_inv:
            out.append(self.P(pol["inventories"]["heading"], "body_h"))
            out.append(self.P(pol["inventories"]["text"], "body"))
            idx += 1
        for key in self.p.options.get("extra_policies") or []:
            extra = pol["optional"].get(key)
            if not extra:
                continue
            out.append(self.P(f"{letters[idx]}. {extra['heading']}", "body_h"))
            out.append(self.P(extra["text"], "body"))
            idx += 1
        out.append(Spacer(1, 10))

        for n in self.p.notes:
            rows = [{"kind": "section", "label": f"{n['number']}. {n['title']}"}]
            for r in n["rows"]:
                if r["kind"] == "heading":
                    rows.append({"kind": "subsection", "label": r["label"], "indent": r["indent"]})
                elif r["kind"] == "line":
                    rows.append({"kind": "line", "label": r["label"], "cy": r["cy"], "py": r["py"], "indent": r["indent"]})
                elif r["kind"] == "total":
                    rows.append({"kind": "total", "label": r["label"], "cy": r["cy"], "py": r["py"], "indent": r["indent"]})
                    rows.append({"kind": "spacer", "h": 3})
            rows.append({"kind": "grand", "label": n["total_label"], "cy": n["cy"], "py": n["py"]})
            t = self._rows_table(rows, self.cols())
            block = [t, Spacer(1, 12)]
            out.append(KeepTogether(block) if len(rows) <= 22 else t)
            if len(rows) > 22:
                out.append(Spacer(1, 12))
        out += self.fx_block()
        out.append(PageBreak())
        return out

    # ---- depreciation schedule ----------------------------------------- #
    LAYOUTS = {
        "simple": [("name", "NAME", "l"), ("cost", "COST", "n"), ("opening_value", "OPENING VALUE", "n"),
                   ("purchases", "PURCHASES", "n"), ("disposals", "DISPOSALS", "n"),
                   ("depreciation", "DEPRECIATION", "n"), ("closing_value", "CLOSING VALUE", "n")],
        "standard": [("name", "NAME", "l"), ("rate", "RATE", "l"), ("method", "METHOD", "l"), ("purchased", "PURCHASED", "l"),
                     ("cost", "COST", "n"), ("opening_value", "OPENING VALUE", "n"), ("purchases", "PURCHASES", "n"),
                     ("disposals", "DISPOSALS", "n"), ("depreciation", "DEPRECIATION", "n"), ("closing_value", "CLOSING VALUE", "n")],
        "register": [("name", "NAME", "l"), ("asset_number", "ASSET NUMBER", "l"), ("asset_type", "ASSET TYPE", "l"),
                     ("purchased", "PURCHASED", "l"), ("cost", "COST", "n"), ("opening_accum", "OPENING ACCUM DEP", "n"),
                     ("opening_value", "OPENING VALUE", "n"), ("purchases", "PURCHASES", "n"), ("disposals", "DISPOSALS", "n"),
                     ("depreciation", "DEPRECIATION", "n"), ("closing_accum", "CLOSING ACCUM DEP", "n"),
                     ("closing_value", "CLOSING VALUE", "n"), ("effective_life", "EFFECTIVE LIFE", "l"), ("dep_start", "DEP START DATE", "l")],
    }

    def depreciation(self) -> list:
        d = self.p.depn
        if not d:
            return []
        cols = self.LAYOUTS[d["layout"]]
        is_land = self.depn_is_landscape()
        page_w = (landscape(A4)[0] if is_land else PAGE_W) - MARGIN_L - MARGIN_R
        out = [Meta(footnote=self.footnote("depreciation"))]
        out += self.header(d["title"])
        fixed = {"rate": 34, "method": 40, "purchased": 56, "asset_number": 42, "asset_type": 64,
                 "effective_life": 38, "dep_start": 54}
        num_w = 46 if d["layout"] == "register" else 52
        widths = []
        for key, _, kind in cols:
            if key == "name":
                widths.append(None)
            elif kind == "n":
                widths.append(num_w)
            else:
                widths.append(fixed.get(key, 50))
        name_w = page_w - sum(w for w in widths if w)
        widths = [name_w if w is None else w for w in widths]
        data = [[Paragraph(c[1], self.s["dep_head" if c[2] == "n" else "dep_head_l"]) for c in cols]]
        style = [("LINEBELOW", (0, 0), (-1, 0), 0.6, RULE_DARK), ("VALIGN", (0, 0), (-1, -1), "BOTTOM"),
                 ("TOPPADDING", (0, 0), (-1, -1), 2.5), ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
                 ("LEFTPADDING", (0, 0), (-1, -1), 2), ("RIGHTPADDING", (0, 0), (-1, -1), 2)]
        for g in d["groups"]:
            i = len(data)
            data.append([Paragraph(g["name"], self.s["dep_group"])] + [""] * (len(cols) - 1))
            style += [("SPAN", (0, i), (-1, i)), ("LINEBELOW", (0, i), (-1, i), 0.6, RULE_DARK), ("TOPPADDING", (0, i), (-1, i), 8)]
            for a in g["assets"]:
                i = len(data)
                row = []
                for key, _, kind in cols:
                    if kind == "n":
                        row.append(Paragraph(self.fmt(a.get(key, 0)), self.s["dep_num"]))
                    else:
                        row.append(Paragraph(str(a.get(key, "") or ""), self.s["dep_line"]))
                data.append(row)
                style.append(("LINEBELOW", (0, i), (-1, i), 0.3, RULE_LIGHT))
            i = len(data)
            row = []
            for key, _, kind in cols:
                if key == "name":
                    row.append(Paragraph(f"Total {g['name']}", self.s["dep_tot"]))
                elif kind == "n":
                    row.append(Paragraph(self.fmt(g["totals"].get(key, 0)), self.s["dep_num_b"]))
                else:
                    row.append("")
            data.append(row)
            style.append(("LINEBELOW", (0, i), (-1, i), 0.3, RULE_LIGHT))
        i = len(data)
        row = []
        for key, _, kind in cols:
            if key == "name":
                row.append(Paragraph("Total", self.s["dep_tot"]))
            elif kind == "n":
                row.append(Paragraph(self.fmt(d["totals"].get(key, 0)), self.s["dep_num_b"]))
            else:
                row.append("")
        data.append(row)
        style += [("LINEBELOW", (0, i), (-1, i), 0.6, RULE_DARK), ("TOPPADDING", (0, i), (-1, i), 6)]
        t = Table(data, colWidths=widths, repeatRows=1)
        t.setStyle(TableStyle(style))
        out.append(t)
        out += [NextPageTemplate("portrait"), PageBreak()]
        return out

    def depn_is_landscape(self) -> bool:
        d = self.p.depn
        if not d:
            return False
        return d.get("orientation", "landscape" if d["layout"] in ("register", "standard") else "portrait") == "landscape"

    # ---- declaration ---------------------------------------------------- #
    def _trustee_kind(self) -> str:
        e = self.p.entity
        kind = (e.get("trustee_type") or "").lower()
        if kind in ("corporate", "individual"):
            return kind
        t = (e.get("trustee") or "")
        if t and not re.search(r"\b(pty|ltd|limited|company|corporation|inc)\b", t, re.I):
            return "individual"
        return "corporate"

    def _declaration_cfg(self) -> dict:
        variants = self.b["declaration"].get(self.etype) or self.b["declaration"]["company"]
        if self.etype == "trust":
            variants = variants[self._trustee_kind()]
        n = len(self.p.entity.get("directors") or [])
        if n == 1 and "singular" in variants:
            return variants["singular"]
        return variants.get("plural") or variants.get("singular")

    def _dated(self, label: str) -> list:
        when = self.p.entity.get("date_signed") or ""
        dated = Table([[Paragraph(label, self.s["sig_bold"]), Paragraph(when, self.s["sig_name"])]], colWidths=[40, 120], rowHeights=[16])
        dated.setStyle(TableStyle([("LINEBELOW", (1, 0), (1, 0), 0.6, BLACK), ("VALIGN", (0, 0), (-1, -1), "BOTTOM"),
                                   ("LEFTPADDING", (0, 0), (-1, -1), 0)]))
        return [dated]

    def declaration(self) -> list:
        cfg = self._declaration_cfg()
        subs = self._subs()
        out = [Meta(footnote="")]
        out += self.header(cfg.get("title", self.rt["declaration"]))
        out.append(self.P(cfg["p1"].format(**subs), "body"))
        out.append(self.P(cfg["p2"].format(**subs), "body"))
        items = [[Paragraph(f"{i}.", self.s["body"]), Paragraph(txt.format(**subs), self.s["body"])]
                 for i, txt in enumerate(cfg["items"], 1)]
        t = Table(items, colWidths=[22, self.usable_w - 22])
        t.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0),
                               ("TOPPADDING", (0, 0), (-1, -1), 0), ("BOTTOMPADDING", (0, 0), (-1, -1), 0)]))
        out += [t, Spacer(1, 6)]
        if cfg.get("p3"):
            out.append(self.P(cfg["p3"].format(**subs), "body"))
        out.append(Spacer(1, 22))
        for name in self.p.entity.get("directors") or []:
            sig = Table([[Paragraph(cfg["signature_label"], self.s["sig_label"]), ""],
                         ["", Paragraph(name, self.s["sig_name"])]],
                        colWidths=[48, 170], rowHeights=[16, 14])
            sig.setStyle(TableStyle([("LINEBELOW", (1, 0), (1, 0), 0.6, BLACK), ("VALIGN", (0, 0), (-1, -1), "BOTTOM"),
                                     ("LEFTPADDING", (0, 0), (-1, -1), 0), ("BOTTOMPADDING", (0, 0), (-1, -1), 1)]))
            out += [sig, Spacer(1, 22)]
        out += self._dated(cfg["dated_label"])
        out.append(PageBreak())
        return out

    # ---- compilation report -------------------------------------------- #
    def compilation(self) -> list:
        c = self.b["compilation"]
        words = c.get("words") or {}
        n = len(self.p.entity.get("directors") or [])
        key = self.etype if self.etype in words else ("company_singular" if n == 1 else "company_plural")
        legal = self.p.entity.get("legal_name") or self.p.entity["name"]
        w = {k: v.format(entity=legal) for k, v in (words.get(key) or {}).items()}
        subs = {"entity": legal, "fy_long": self.fy["long"], **w}
        out = [Meta(footnote="")]
        out += self.header(self.rt["compilation"])
        out.append(self.P(c["title_line"].format(**subs), "body"))
        out.append(self.P(c["p1"].format(**subs), "body"))
        out.append(self.P(c["responsibility_heading"].format(**subs), "body_h"))
        out.append(self.P(c["p2"].format(**subs), "body"))
        out.append(self.P(c["our_heading"], "body_h"))
        out.append(self.P(c["p3"].format(**subs), "body"))
        out.append(self.P(c["p4"].format(**subs), "body"))
        out.append(self.P(c["disclaimer_heading"], "body_h"))
        out.append(self.P(c["p5"].format(**subs), "body"))
        out.append(self.P(c["p6"].format(**subs), "body"))
        out.append(Spacer(1, 40))
        firm = self.b["firm"]
        out.append(self.P(self.p.entity.get("compiled_by") or firm["signatory"], "body"))
        out.append(Spacer(1, 6))
        out.append(self.P(firm["name"], "sig_name"))
        out.append(self.P(firm["descriptor"], "sig_name"))
        for line in firm["address_lines"]:
            out.append(self.P(line, "sig_name"))
        out.append(Spacer(1, 18))
        out += self._dated(c["dated_label"])
        return out

    # ---- assemble ------------------------------------------------------ #
    def story(self) -> list:
        story = self.cover() + self.contents()
        builders = {"pnl": self.pnl, "bs": self.bs, "equity": self.equity, "notes": self.notes,
                    "depreciation": self.depreciation, "declaration": self.declaration, "compilation": self.compilation,
                    "rental": self.rental, "persons": self.persons, "distribution": self.distribution}
        for key in self.order:
            if key not in builders or not self._present(key):
                continue
            if key == "depreciation" and self.depn_is_landscape():
                # the template switch must sit before the page break that starts the schedule
                while story and isinstance(story[-1], PageBreak):
                    story.pop()
                story += [NextPageTemplate("landscape"), PageBreak()]
            story.append(StartMark(key, self.page_of))
            story += builders[key]()
        # drop a trailing PageBreak so we don't emit a blank last page
        while story and isinstance(story[-1], PageBreak):
            story.pop()
        return story


def build_pdf(pack: Pack, out_path: str | Path, boiler: dict, fonts: dict, order=None, draft: bool = False):
    # pass 1 (in memory) finds the page each report starts on; pass 2 prints them on the Contents page
    first = Renderer(pack, boiler, fonts, order)
    _build(first, io.BytesIO(), pack, boiler, fonts, draft)
    r = Renderer(pack, boiler, fonts, order)
    r.contents_pages = dict(first.page_of)
    _build(r, str(out_path), pack, boiler, fonts, draft)
    return out_path


def _build(r: "Renderer", target, pack: Pack, boiler: dict, fonts: dict, draft: bool):
    doc = BaseDocTemplate(target, pagesize=A4, leftMargin=MARGIN_L, rightMargin=MARGIN_R,
                          topMargin=MARGIN_T, bottomMargin=MARGIN_B,
                          title=f"{boiler['firm']['pack_title']} - {pack.entity['name']}",
                          author=boiler["firm"]["name"])
    fr_p = Frame(MARGIN_L, MARGIN_B + 8 * mm, PAGE_W - MARGIN_L - MARGIN_R, PAGE_H - MARGIN_T - MARGIN_B - 8 * mm, id="p",
                 leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0)
    lw, lh = landscape(A4)
    fr_l = Frame(MARGIN_L, MARGIN_B + 8 * mm, lw - MARGIN_L - MARGIN_R, lh - MARGIN_T - MARGIN_B - 8 * mm, id="l",
                 leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0)
    doc.addPageTemplates([PageTemplate(id="portrait", frames=[fr_p], onPageEnd=on_page_end, pagesize=A4),
                          PageTemplate(id="landscape", frames=[fr_l], onPageEnd=on_page_end, pagesize=landscape(A4))])

    def canvasmaker(*a, **k):
        c = NumberedCanvas(*a, **k)
        c._fr_entity = pack.entity["name"]
        c._fr_footer_left = boiler["firm"]["footer_left"]
        c._fr_fonts = fonts
        c._fr_draft = draft
        return c

    doc.build(r.story(), canvasmaker=canvasmaker)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("pack")
    ap.add_argument("out")
    ap.add_argument("--map")
    ap.add_argument("--boilerplate")
    ap.add_argument("--font-dir")
    ap.add_argument("--order", help="comma-separated report keys")
    ap.add_argument("--allow-errors", action="store_true", help="render even if validation has errors (stamped DRAFT)")
    ap.add_argument("--draft", action="store_true", help="stamp every page DRAFT")
    args = ap.parse_args()

    data = load_json(args.pack)
    pack = Pack(data, load_map(args.map or data.get("report_map")))
    boiler = load_boilerplate(args.boilerplate or data.get("boilerplate"))
    if data.get("boilerplate_overrides"):
        # shallow merge so a client-specific wording tweak can live in the pack
        for k, v in data["boilerplate_overrides"].items():
            if isinstance(v, dict) and isinstance(boiler.get(k), dict):
                boiler[k].update(v)
            else:
                boiler[k] = v
    print(pack.validation_markdown())
    if pack.has_errors() and not args.allow_errors:
        print("\nValidation errors - fix the pack or pass --allow-errors to render a draft.", file=sys.stderr)
        sys.exit(2)
    fonts = register_fonts(Path(args.font_dir) if args.font_dir else None)
    order = args.order.split(",") if args.order else None
    build_pdf(pack, args.out, boiler, fonts, order, draft=args.draft or (args.allow_errors and pack.has_errors()))
    print(f"\nWrote {args.out}")


if __name__ == "__main__":
    main()
