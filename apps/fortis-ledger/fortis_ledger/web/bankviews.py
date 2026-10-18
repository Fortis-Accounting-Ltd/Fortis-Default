"""Bank screens: import a statement, check its columns, cash code and reconcile, unreconcile."""
from __future__ import annotations

import math
import secrets
from pathlib import Path

from flask import abort, current_app, flash, g, redirect, render_template, request, url_for

from .. import bank as bankmod, imports
from ..money import ZERO, to_dec
from ..store import ACCOUNT_CODE_RE
from .views import (_finish, _load_meta, _save_meta, _save_upload, account_choices, account_info, bp, cfg,
                    new_account_ctx, remember, store, work_dir)

STATEMENT_FILES = {".csv", ".txt", ".xlsx", ".xlsm"}
PAGE_SIZE = 100
DATE_ORDERS = {"dmy": "Day first - 30/06/2026", "mdy": "Month first - 06/30/2026", "ymd": "Year first - 2026-06-30"}


def _account_or_404(code: str):
    if not ACCOUNT_CODE_RE.match(code or ""):
        abort(404)
    a = g.ledger.chart.get(code)
    if a is None and not store().bank_path(g.slug, code).exists():
        abort(404, f"There is no account {code}.")
    return a


def _matches(ln: dict, q: str) -> bool:
    if not q:
        return True
    c = ln.get("coded") or {}
    hay = " ".join(str(x) for x in (ln.get("payee"), ln.get("description"), ln.get("reference"), ln["date"],
                                    ln["amount"], c.get("number"), c.get("account"),
                                    (c.get("match") or {}).get("number"))).lower()
    return q in hay or q.replace(",", "") in hay


def _page(items: list, page: int) -> tuple[list, int, int]:
    pages = max(1, math.ceil(len(items) / PAGE_SIZE))
    page = min(max(page, 1), pages)
    return items[(page - 1) * PAGE_SIZE: page * PAGE_SIZE], page, pages


# ---------------------------------------------------------------------------------------- #
# bank accounts and importing a statement
# ---------------------------------------------------------------------------------------- #
@bp.route("/c/<slug>/bank")
def bank(slug):
    L = g.ledger
    banks, others = bankmod.statement_accounts(L)
    return render_template("bank.html", banks=bankmod.summaries(L, g.fy), bank_accounts=banks, other_accounts=others,
                           picked=request.args.get("account") or "", **new_account_ctx(L))


@bp.route("/c/<slug>/bank/upload", methods=["POST"])
def bank_upload(slug):
    L = g.ledger
    code = (request.form.get("account") or "").strip().split(" ")[0]
    up = request.files.get("file")
    back_to = url_for("ui.bank", slug=slug, account=code)
    a = L.chart.get(code) if ACCOUNT_CODE_RE.match(code or "") else None
    if a is None:
        flash("Choose the bank account the statement is for.", "error")
        return redirect(back_to)
    if L.is_pl(code):
        flash(f"{code} {a.name} is a profit and loss account - a statement goes into a bank, credit card or loan account.",
              "error")
        return redirect(back_to)
    if not up or not up.filename:
        flash("Choose the statement file.", "error")
        return redirect(back_to)
    if Path(up.filename).suffix.lower() not in STATEMENT_FILES:
        flash(f"{up.filename}: use the CSV (or Excel) file your internet banking exports.", "error")
        return redirect(back_to)
    pid = secrets.token_hex(8)
    d = work_dir() / pid
    d.mkdir(parents=True)
    path = _save_upload(up, d)
    try:
        rows = imports.read_table(path)
        if not any(any(bankmod._text(c) for c in r) for r in rows):
            raise ValueError("the file is empty")
        layout = bankmod.detect_layout(rows)
        saved = bankmod.BankFeed(store(), slug, code).data.get("layout")
        if saved:                      # the same bank's file as last time: keep the choices made then
            last = bankmod.Layout.from_dict(saved)
            if last.cols == layout.cols and last.header_row == layout.header_row:
                layout.flip, layout.date_order = last.flip, last.date_order
    except Exception as e:     # an unreadable file: say so and let the user try another
        current_app.logger.exception("statement %s could not be read", up.filename)
        _finish(pid)
        flash(f"Could not read {up.filename}: {e}", "error")
        return redirect(back_to)
    _save_meta(pid, {"kind": "bank", "slug": slug, "code": code, "file": up.filename, "path": str(path),
                     "layout": layout.to_dict()})
    return redirect(url_for("ui.bank_columns", slug=slug, pid=pid))


def _layout_from_form(form, rows: list[list]) -> tuple[bankmod.Layout, list[str]]:
    errors = []
    try:
        header_row = int(form.get("header_row") or 0) - 1
    except ValueError:
        header_row = -1
    header_row = min(max(header_row, -1), max(len(rows) - 1, -1))
    cols: dict[str, int] = {}
    for k in form:
        if k.startswith("role_") and form.get(k) in bankmod.ROLES:
            role, j = form.get(k), int(k.split("_")[1])
            if role in cols:
                errors.append(f"'{bankmod.ROLES[role]}' is chosen for two columns - choose it for one")
            cols[role] = j
    if "amount" in cols and ({"spent", "received"} & set(cols)):
        errors.append("choose either one amount column, or the spent and received columns - not both")
    order = form.get("date_order") if form.get("date_order") in DATE_ORDERS else "dmy"
    return bankmod.Layout(header_row, cols, order, form.get("flip") == "yes"), errors


@bp.route("/c/<slug>/bank/import/<pid>", methods=["GET", "POST"])
def bank_columns(slug, pid):
    L = g.ledger
    meta = _load_meta(pid)
    if meta.get("kind") != "bank":
        abort(404)
    code = meta["code"]
    a = L.chart.get(code)
    rows = imports.read_table(meta["path"])
    errors: list[str] = []
    if request.method == "POST":
        layout, errors = _layout_from_form(request.form, rows)
        meta["layout"] = layout.to_dict()
        _save_meta(pid, meta)
    else:
        layout = bankmod.Layout.from_dict(meta["layout"])
    pv = bankmod.preview(L, code, rows, layout)
    if request.method == "POST" and request.form.get("op") == "import" and not errors:
        if not pv["lines"]:
            errors.append("no statement lines were found - check the date and amount columns")
        elif not pv["new"]:
            errors.append(f"every line in the file is already in {code} - there is nothing new to import")
        else:
            imp = bankmod.import_lines(L, code, pv["lines"], meta["file"], layout)
            _finish(pid)
            msg = f"{imp['lines']} line(s) imported from {meta['file']}"
            if imp["duplicates"]:
                msg += f" - {imp['duplicates']} already in the account were skipped"
            flash(msg + ".", "ok")
            return redirect(url_for("ui.bank_account", slug=slug, code=code))
    return render_template("bank_columns.html", meta=meta, pid=pid, a=a, code=code, layout=layout, errors=errors,
                           columns=bankmod.column_titles(rows, layout), roles=bankmod.ROLES, orders=DATE_ORDERS,
                           pv=pv, sample=list(zip(pv["lines"], pv["duplicates"]))[:60],
                           title_rows=min(len(rows), 15))


@bp.route("/c/<slug>/bank/import/<pid>/cancel", methods=["POST"])
def bank_import_cancel(slug, pid):
    meta = _load_meta(pid)
    _finish(pid)
    return redirect(url_for("ui.bank", slug=slug, account=meta.get("code", "")))


# ---------------------------------------------------------------------------------------- #
# one bank account: cash coding, reconciled lines, statements
# ---------------------------------------------------------------------------------------- #
@bp.route("/c/<slug>/bank/<code>")
def bank_account(slug, code):
    L = g.ledger
    a = _account_or_404(code)
    feed = bankmod.BankFeed(store(), slug, code)
    tab = request.args.get("tab") if request.args.get("tab") in ("code", "done", "statements") else "code"
    q = (request.args.get("q") or "").strip().lower()
    page = request.args.get("page", type=int) or 1
    summary = next((s for s in bankmod.summaries(L, g.fy) if s["code"] == code), None)
    ctx = {"a": a, "code": code, "feed": feed, "tab": tab, "q": q, "summary": summary,
           "counts": {"code": len(feed.ordered("unreconciled")), "done": len(feed.ordered("reconciled")),
                      "deleted": len([ln for ln in feed.lines if ln["status"] == "deleted"])}}
    if tab == "code":
        lines = [ln for ln in feed.ordered("unreconciled") if _matches(ln, q)]
        shown, page, pages = _page(lines, page)
        res = cfg("RESULTS").get(request.args.get("r") or "") or {}
        errors, typed = res.get("errors") or {}, res.get("values") or {}
        sugg = bankmod.suggestions(bankmod.feeds(store(), slug), shown)
        rows = []
        for ln in shown:
            s = sugg.get(ln["id"]) or {}
            t = typed.get(ln["id"]) or {}
            rows.append({"ln": ln, "amount": to_dec(ln["amount"]), "account": t.get("account", s.get("account", "")),
                         "tax": t.get("tax", s.get("tax", "")), "suggested": bool(s) and not t,
                         "error": errors.get(ln["id"], "")})
        ctx.update(rows=rows, total=len(lines), page=page, pages=pages, accounts=account_choices(L),
                   info=account_info(L), gst_on=bool(L.client.gst_registered), **new_account_ctx(L))
    elif tab == "done":
        lines = [ln for ln in reversed(feed.ordered("reconciled")) if _matches(ln, q)]
        shown, page, pages = _page(lines, page)
        names = {x.code: x.name for x in L.chart.accounts}
        ctx.update(rows=[{"ln": ln, "amount": to_dec(ln["amount"]), "c": ln["coded"], "fy": L.client.fy_of(
            bankmod._date(ln["date"])), "name": names.get(ln["coded"]["account"], "")} for ln in shown],
            total=len(lines), page=page, pages=pages, locked={fy for fy in g.years if L.is_locked(fy)})
    else:
        deleted = sorted((ln for ln in feed.lines if ln["status"] == "deleted"), key=lambda ln: (ln["date"], ln["seq"]))
        ctx.update(imports=list(reversed(feed.data["imports"])), deleted=deleted)
    return render_template("bank_account.html", **ctx)


@bp.route("/c/<slug>/bank/<code>/reconcile", methods=["POST"])
def bank_reconcile(slug, code):
    L = g.ledger
    _account_or_404(code)
    f = request.form
    picked = [x for x in dict.fromkeys(f.getlist("pick")) if bankmod.LINE_ID_RE.match(x)]
    codings, blank = [], 0
    for lid in picked:
        acct = (f.get(f"acct_{lid}") or "").strip().split(" ")[0]
        if not acct:
            blank += 1
            continue
        codings.append(bankmod.Coding(lid, acct, (f.get(f"tax_{lid}") or "").strip()))
    nav = {"q": f.get("q") or None, "page": f.get("page") or None}
    if not picked:
        flash("Tick the lines to reconcile (filling in an account ticks a line).", "error")
        return redirect(url_for("ui.bank_account", slug=slug, code=code, **nav))
    res = bankmod.reconcile(L, code, codings) if codings else {"done": [], "errors": {}}
    done = res["done"]
    if done:
        matched = sum(1 for d in done if d["matched"])
        msg = f"{len(done)} line(s) reconciled"
        if matched:
            msg += f" ({matched} matched to the other side of a transfer already in the ledger)"
        flash(msg + ".", "ok")
    if blank:
        flash(f"{blank} ticked line(s) had no account, so they were left to code.", "warn")
    rid = None
    if res["errors"]:
        flash(f"{len(res['errors'])} line(s) could not be reconciled - see the reasons below.", "error")
        rid = remember({"errors": res["errors"],
                        "values": {cd.line: {"account": cd.account, "tax": cd.tax} for cd in codings
                                   if cd.line in res["errors"]}})
    return redirect(url_for("ui.bank_account", slug=slug, code=code, r=rid, **nav))


@bp.route("/c/<slug>/bank/<code>/delete", methods=["POST"])
def bank_delete(slug, code):
    _account_or_404(code)
    picked = [x for x in request.form.getlist("pick") if bankmod.LINE_ID_RE.match(x)]
    n = bankmod.delete_lines(g.ledger, code, picked, (request.form.get("reason") or "").strip())
    flash(f"{n} line(s) deleted - they are kept under Statements if you need them back." if n else
          "Tick the lines to delete.", "ok" if n else "error")
    return redirect(url_for("ui.bank_account", slug=slug, code=code, q=request.form.get("q") or None))


@bp.route("/c/<slug>/bank/<code>/restore", methods=["POST"])
def bank_restore(slug, code):
    _account_or_404(code)
    picked = [x for x in request.form.getlist("pick") if bankmod.LINE_ID_RE.match(x)]
    n = bankmod.restore_lines(g.ledger, code, picked)
    flash(f"{n} line(s) put back to be coded." if n else "Tick the lines to put back.", "ok" if n else "error")
    return redirect(url_for("ui.bank_account", slug=slug, code=code, tab="statements"))


@bp.route("/c/<slug>/bank/<code>/unreconcile", methods=["POST"])
def bank_unreconcile(slug, code):
    _account_or_404(code)
    lid = request.form.get("line") or ""
    if not bankmod.LINE_ID_RE.match(lid):
        abort(400)
    res = bankmod.unreconcile(g.ledger, code, lid, (request.form.get("reason") or "").strip())
    msg = (f"{res['number']} taken out of the ledger" if res["number"] else "The transfer match was undone")
    msg += " - the line is back in To reconcile."
    if res["also"]:
        msg += f" The other side of the transfer (in {res['also']['feed']}) went back to be coded too."
    flash(msg, "ok")
    return redirect(url_for("ui.bank_account", slug=slug, code=code, tab="done", q=request.form.get("q") or None))


@bp.app_template_filter("signed")
def signed_money(v) -> str:
    v = to_dec(v)
    return f"{v:,.2f}" if v >= 0 else f"-{-v:,.2f}"


@bp.app_template_global("bank_amounts")
def bank_amounts(amount) -> tuple[str, str]:
    """(spent, received) for a statement amount."""
    v = to_dec(amount)
    return (f"{-v:,.2f}", "") if v < ZERO else ("", f"{v:,.2f}")
