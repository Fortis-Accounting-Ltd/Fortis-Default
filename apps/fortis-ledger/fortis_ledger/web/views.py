"""Routes, one section per screen."""
from __future__ import annotations

import calendar
import datetime as dt
import gc
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
import webbrowser
from collections import Counter
from pathlib import Path

from flask import (Blueprint, abort, current_app, flash, g, jsonify, redirect, render_template, request,
                   send_file, session, url_for)
from werkzeug.utils import secure_filename

from .. import bank as bankmod, claude_desktop, imports, reports, shortcut, statements, xero
from ..chartbook import ACCOUNT_TYPES, ChartError, ClientChart, default_report_code
from ..engine import chartlib, describe, report_code_options
from ..ledger import Ledger, LedgerError, ValidationError, journal_from_form
from ..model import ENTITY_TYPES, Client, Journal, Line, now_iso, parse_date
from ..money import ZERO
from ..msauth import MicrosoftConfig, MicrosoftSignIn, domains
from ..oauth import SignInError
from ..settings import suggest_folders
from ..store import FolderStore, LockedError, StoreError
from ..team import Team, norm
from ..xero_connect import DEFAULT_PORT as XERO_PORT, SCOPES as XERO_SCOPES, XeroConfig, XeroSession, has_balances, year_ends
from . import ENTITY_LABELS, release_locks

bp = Blueprint("ui", __name__)

PID_RE = re.compile(r"^[0-9a-f]{16}$")
NO_SETUP_NEEDED = {"ui.setup", "ui.quit", "ui.alive"}
SIGNIN_OPEN = {"ui.signin", "ui.signin_start", "ui.signin_status", "ui.signin_cancel", "ui.signin_start_get"}
TEAM_OPEN = SIGNIN_OPEN | {"ui.signout"}            # open to people the team has not given access
ADMIN_ONLY = {"ui.firm_settings", "ui.client_archive", "ui.team_access", "ui.team_move",
              "ui.updates_share"}                    # when the firm manages access
LOCK_ENDPOINTS = {"ui.lock_heartbeat", "ui.lock_take", "ui.lock_release"}
EDITABLE_KINDS = {"manual", "reversal", "opening"}
HEARTBEAT_WRITE_SECONDS = 60
REPORT_TITLES = {"financial_reports": "Financial Reports", "trial_balance": "Trial Balance",
                 "profit_and_loss": "Profit and Loss", "balance_sheet": "Balance Sheet",
                 "general_ledger": "General Ledger Detail", "gst_report": "GST Report",
                 "journal_report": "Journal Report"}
CLIENT_TEXT_FIELDS = ("legal_name", "abn", "trustee", "trustee_type", "business_name", "proprietor",
                      "compiled_by", "notes")
# report options with their own control on the Client details screen; the rest are edited as JSON
OPTION_CHOICES = {
    "decimals": {"": "Entity default", "0": "Whole dollars", "2": "Dollars and cents"},
    "contents_page_numbers": {"": "Entity default", "yes": "Yes", "no": "No"},
    "cos_lines": {"": "Entity default", "accounts": "Each account", "grouped": "Opening stock / purchases / closing stock"},
    "single_column": {"": "Automatic (when there is no prior year)", "yes": "Current year only", "no": "Always show prior year"},
    "keep_nil_accounts": {"": "Entity default", "yes": "Show nil accounts", "no": "Hide nil accounts"},
}
OPTION_TEXT = ("cost_of_sales_label",)


# ---------------------------------------------------------------------------------------- #
# helpers
# ---------------------------------------------------------------------------------------- #
def cfg(key):
    return current_app.config[key]


def settings():
    return cfg("SETTINGS")


def store() -> FolderStore:
    if "store" not in g:
        s = settings()
        signed = session.get("ms_user") or {}
        g.store = FolderStore(s.data_folder, user=signed.get("email") or s.user_name)
    return g.store


def firm() -> dict:
    """Firm-wide settings kept in the data folder (Microsoft sign-in, the Xero app)."""
    if "firm" not in g:
        g.firm = store().firm_settings() if settings().configured and store().is_initialised() else {}
    return g.firm


def ms_config(m: dict | None = None) -> MicrosoftConfig | None:
    m = m if m is not None else (firm().get("microsoft") or {})
    if not m.get("tenant_id") or not m.get("client_id"):
        return None
    return MicrosoftConfig(m["tenant_id"], m["client_id"], list(m.get("allowed_domains") or []), cfg("MS_AUTHORITY"))


def signin_required() -> bool:
    return bool((firm().get("microsoft") or {}).get("required")) and ms_config() is not None


def team() -> Team:
    """Who may open which client (team.json in the data folder)."""
    if "team" not in g:
        g.team = Team.load(store().read_team()) if settings().configured and store().is_initialised() else Team()
    return g.team


def me() -> str:
    """The signed-in office email ('' when nobody is signed in)."""
    return norm((session.get("ms_user") or {}).get("email") or "")


def managing() -> bool:
    """The firm manages access and can apply it: office sign-in is required, so everyone is known."""
    return team().managed and signin_required()


def is_admin() -> bool:
    return not managing() or team().is_admin(me())


def can_open(slug: str) -> bool:
    return not managing() or team().can_open(me(), slug)


def admin_names() -> str:
    t = team()
    return ", ".join(t.name(e) for e in t.admins()) or "an admin"


def xero_config() -> XeroConfig | None:
    x = firm().get("xero") or {}
    if not x.get("client_id"):
        return None
    return XeroConfig(x["client_id"], int(x.get("port") or XERO_PORT), x.get("scopes") or XERO_SCOPES,
                      cfg("XERO_ENDPOINTS"))


def open_in_browser(url: str) -> None:
    (current_app.config.get("BROWSER") or webbrowser.open)(url)


def back(default: str | None = None):
    ref = request.referrer or ""
    if ref.startswith(request.host_url) and ref != request.url:
        return redirect(ref)
    return redirect(default or url_for("ui.clients"))


def client_home() -> str:
    return url_for("ui.overview", slug=g.slug)


def arg_date(name: str, default: dt.date | None = None) -> dt.date | None:
    v = (request.values.get(name) or "").strip()
    if not v:
        return default
    try:
        return parse_date(v)
    except ValueError:
        flash(f"'{v}' is not a date - use 30/06/2026", "error")
        return default


def clamp(d: dt.date | None, lo: dt.date, hi: dt.date) -> dt.date | None:
    return None if d is None else min(max(d, lo), hi)


def work_dir() -> Path:
    return cfg("WORK_DIR")


def account_choices(L: Ledger) -> list:
    return [(a.code, a.name, a.report_code) for a in L.chart.usable()]


def used_accounts(L: Ledger) -> set[str]:
    used = set(store().list_bank_accounts(L.slug))          # an account with a bank statement is in use
    for fy in L.years():
        for j in L.journals(fy, statuses=None):
            used.update(ln.account for ln in j.lines)
    return used


def new_account_ctx(L: Ledger) -> dict:
    """What the 'New account' pop-up needs: account types, each type's report code and tax rate, the codes."""
    gst = bool(L.client.gst_registered)
    return {"acct_types": ACCOUNT_TYPES, "tax_choices": bankmod.TAX_CHOICES, "codes": report_code_options(),
            "type_defaults": {t: {"report_code": default_report_code(t), "tax": bankmod.tax_for_type(t, gst)}
                              for t in ACCOUNT_TYPES}}


def account_info(L: Ledger) -> dict:
    """code -> [name, the tax rate it is coded with by default] for the account inputs."""
    return {a.code: [a.name, bankmod.default_tax(a)] for a in L.chart.accounts}


def open_path(p: Path, folder: bool = False) -> None:
    """Open a generated file (or its folder) with the PC's default app."""
    opener = current_app.config.get("OPENER")
    if opener:
        opener(p, folder)
        return
    if sys.platform.startswith("win"):
        if folder:
            subprocess.Popen(["explorer", "/select,", str(p)])
        else:
            os.startfile(str(p))  # noqa: S606 - a file the app wrote into the client's reports folder
    elif sys.platform == "darwin":
        subprocess.Popen(["open", "-R", str(p)] if folder else ["open", str(p)])
    else:
        subprocess.Popen(["xdg-open", str(p.parent if folder else p)])


def remember(result: dict) -> str:
    rid = secrets.token_hex(8)
    res = cfg("RESULTS")
    res[rid] = result
    while len(res) > 50:
        res.pop(next(iter(res)))
    return rid


def fy_end_from_form(form) -> tuple[int, int]:
    month = int(form.get("fy_end_month") or 6)
    day = int(form.get("fy_end_day") or calendar.monthrange(2001, month)[1])
    if not (1 <= month <= 12) or not (1 <= day <= calendar.monthrange(2000, month)[1]):
        raise ValueError("year-end day is not a date in that month")
    return month, day


def client_fields_from_form(form) -> dict:
    f = {k: (form.get(k) or "").strip() for k in CLIENT_TEXT_FIELDS}
    f["directors"] = [x.strip() for x in (form.get("directors") or "").splitlines() if x.strip()]
    if f["abn"]:
        f["abn"] = xero.format_abn(f["abn"])
    return f


# ---------------------------------------------------------------------------------------- #
# request set-up: settings, the client, its edit lock and the selected year
# ---------------------------------------------------------------------------------------- #
@bp.url_value_preprocessor
def _pull_slug(endpoint, values):
    g.slug = (values or {}).get("slug")


def _team_gate():
    """The firm manages access: let in the people it allows, and only to what they may open."""
    t, email = team(), me()
    if t.role(email) is None and t.join == "auto" and email:
        name = (session.get("ms_user") or {}).get("name") or ""
        t.set_person(email, name, "staff", by="joined at first sign-in", at=now_iso())
        store().save_team(t.to_dict(), "team.joined", {"email": email, "name": name})
    if not t.can_use(email):
        return render_template("error.html", code=403, message=(
            f"{email or 'This account'} cannot use the firm's Fortis Ledger clients yet. Ask {admin_names()} to give "
            f"you access, then open Fortis Ledger again.")), 403
    if request.endpoint in ADMIN_ONLY and not t.is_admin(email):
        abort(403, f"Only an admin can do that - ask {admin_names()}.")
    if g.slug and not t.can_open(email, g.slug):
        abort(403, f"You do not have access to this client. Ask {admin_names()} if you need it.")
    return None


@bp.before_request
def _context():
    if request.endpoint in NO_SETUP_NEEDED:
        return None
    s = settings()
    if not s.configured:
        return redirect(url_for("ui.setup"))
    if not store().is_initialised():
        flash("The data folder is not set up as a Fortis Ledger folder - choose it again.", "error")
        return redirect(url_for("ui.setup"))
    g.ledger = None
    if request.endpoint not in SIGNIN_OPEN and signin_required() and not session.get("ms_user"):
        nxt = request.full_path if request.method == "GET" and request.path != "/" else None
        return redirect(url_for("ui.signin", next=nxt))
    if managing() and request.endpoint not in TEAM_OPEN:
        refused = _team_gate()
        if refused is not None:
            return refused
    if not g.slug:
        return None
    try:
        g.ledger = Ledger(store(), g.slug)
    except StoreError:
        abort(404, "That client is not in the data folder (it may have been archived, or OneDrive is still syncing).")
    st = store()
    info = st.lock_info(g.slug)
    if request.method == "GET" and (not info or info["stale"] or
                                    (info["mine"] and time.time() - float(info.get("heartbeat", 0)) > HEARTBEAT_WRITE_SECONDS)):
        ok, info = st.acquire_lock(g.slug)
        if ok:
            cfg("OPEN_CLIENTS")[g.slug] = st.user
    elif info and info["mine"]:
        cfg("OPEN_CLIENTS")[g.slug] = st.user
    if request.method == "POST" and request.endpoint not in LOCK_ENDPOINTS:
        st.require_lock(g.slug)
        cfg("OPEN_CLIENTS")[g.slug] = st.user
        info = st.lock_info(g.slug)
    g.lock = info
    g.readonly = bool(info and not info["mine"] and not info["stale"])
    g.years = g.ledger.years()
    fy = request.values.get("fy", type=int)
    if fy is None or not (1990 <= fy <= 2100):
        fy = session.get(f"fy:{g.slug}")
    if fy is None or (g.years and fy not in g.years and fy not in (min(g.years) - 1, max(g.years) + 1)):
        fy = g.ledger.default_fy()
    g.fy = fy
    session[f"fy:{g.slug}"] = fy
    return None


TAB_OF = {"ui.overview": "overview", "ui.journals": "journals", "ui.journal_new": "journals",
          "ui.journal_view": "journals", "ui.journal_save": "journals", "ui.accounts": "accounts",
          "ui.account_edit": "accounts", "ui.account_add": "accounts", "ui.import_start": "import",
          "ui.import_review": "import", "ui.trial_balance": "tb", "ui.account_transactions": "gl",
          "ui.profit_and_loss": "pl", "ui.balance_sheet": "bs", "ui.gst_page": "gst",
          "ui.reports_page": "reports", "ui.year_end": "year_end", "ui.change_log": "changes",
          "ui.client_details": "details", "ui.migrated": "overview", "ui.bank": "bank", "ui.bank_columns": "bank",
          "ui.bank_account": "bank"}


@bp.app_context_processor
def _client_globals():
    L = getattr(g, "ledger", None)
    if L is None:
        return {"client": None}
    started = g.fy in g.years
    return {"client": L.client, "L": L, "fy": g.fy, "years": g.years, "readonly": g.readonly, "lock": g.lock,
            "started": started, "year_locked": started and L.is_locked(g.fy), "fy_start": L.fy_range(g.fy)[0],
            "fy_end": L.fy_range(g.fy)[1], "tab": TAB_OF.get(request.endpoint or "", "")}


def _error_back(messages: list[str]):
    for m in messages:
        flash(m, "error")
    if request.method == "GET":
        return render_template("error.html", code=409, message=" ".join(messages)), 409
    return back(client_home() if g.get("slug") else None)


@bp.app_errorhandler(LockedError)
def _locked(e):
    return _error_back([str(e)])


@bp.app_errorhandler(ValidationError)
def _invalid(e):
    return _error_back(e.errors)


@bp.app_errorhandler(LedgerError)
def _ledger_error(e):
    return _error_back([str(e)])


@bp.app_errorhandler(StoreError)
def _store_error(e):
    return _error_back([str(e)])


@bp.app_errorhandler(ChartError)
def _chart_error(e):
    return _error_back([str(e)])


@bp.app_errorhandler(bankmod.BankError)
def _bank_error(e):
    return _error_back([str(e)])


@bp.app_errorhandler(statements.StatementError)
def _statement_error(e):
    return _error_back([str(e)])


# ---------------------------------------------------------------------------------------- #
# set up, clients, quitting
# ---------------------------------------------------------------------------------------- #
@bp.route("/setup", methods=["GET", "POST"])
def setup():
    s = settings()
    folder, user = s.data_folder, s.user_name
    need_create = False
    if request.method == "POST":
        folder = (request.form.get("data_folder") or "").strip().strip('"')
        user = (request.form.get("user_name") or "").strip()
        errors = []
        p = Path(folder).expanduser() if folder else None
        if not folder:
            errors.append("Choose the data folder.")
        elif not p.is_dir():
            errors.append(f"{folder} is not a folder on this PC.")
        if not user:
            errors.append("Enter your name - it is recorded against every change you make.")
        if not errors:
            st = FolderStore(p, user=user)
            if not st.is_initialised():
                if request.form.get("create") == "yes":
                    st.initialise()
                else:
                    need_create = True
                    other = any(not x.name.startswith(".") for x in p.iterdir())
                    errors.append("This folder is not a Fortis Ledger data folder yet. Tick 'Set up a new data "
                                  "folder here' to start one" + (" - it already holds other files, so an empty folder "
                                                                 "(e.g. a new SharePoint library) is better." if other else "."))
        if not errors:
            s.data_folder, s.user_name = str(p), user
            s.save()
            flash("Settings saved.", "ok")
            return redirect(url_for("ui.clients"))
        for e in errors:
            flash(e, "error")
    return render_template("setup.html", folder=folder, user=user, need_create=need_create,
                           suggestions=suggest_folders(1.0))


@bp.route("/")
def clients():
    st = store()
    release_locks(current_app)                 # leaving a client hands its edit lock back
    rows = []
    for c in st.list_clients():
        if not can_open(c.slug):
            continue
        info = st.lock_info(c.slug)
        rows.append({"c": c, "years": st.list_years(c.slug), "last": st.last_audit(c.slug),
                     "lock": info if info and not info["mine"] and not info["stale"] else None})
    q = (request.args.get("q") or "").strip().lower()
    if q:
        rows = [r for r in rows if q in r["c"].name.lower() or q in (r["c"].abn or "").replace(" ", "")
                or q in (r["c"].legal_name or "").lower()]
    by_slug = {r["c"].slug: r for r in rows}
    recent = [by_slug[s] for s in settings().recent if s in by_slug][:6]
    return render_template("clients.html", rows=rows, recent=recent, q=q)


@bp.route("/clients/new", methods=["GET", "POST"])
def client_new():
    today_fy = Client(id="", slug="", name="").fy_of(dt.date.today())
    if request.method == "POST":
        f = request.form
        try:
            month, day = fy_end_from_form(f)
            c = store().create_client((f.get("name") or "").strip(), f.get("type") or "", fy_end_month=month,
                                      fy_end_day=day, gst_registered=f.get("gst_registered") == "yes",
                                      **client_fields_from_form(f))
        except (StoreError, ValueError) as e:
            flash(str(e), "error")
            return render_template("client_form.html", new=True, form=f, default_fy=today_fy)
        first = f.get("first_fy", type=int)
        if first and 1990 <= first <= 2100:
            Ledger(store(), c.slug).ensure_year(first)
        flash(f"{c.name} set up with the firm's {ENTITY_LABELS[c.type].lower()} chart of accounts.", "ok")
        return redirect(url_for("ui.overview", slug=c.slug, fy=first))
    return render_template("client_form.html", new=True, form={"fy_end_month": "6", "fy_end_day": "30"},
                           default_fy=today_fy)


@bp.route("/alive", methods=["POST"])
def alive():
    """Browser mode: open pages ping this so the app knows it is still in use."""
    return ("", 204)


@bp.route("/quit", methods=["POST"], endpoint="quit")
def quit_app():
    release_locks(current_app)
    fn = cfg("SHUTDOWN")
    if fn:
        threading.Timer(0.3, fn).start()
    return render_template("error.html", code=200, message="Fortis Ledger has closed. You can close this tab.")



# ---------------------------------------------------------------------------------------- #
# client overview, details, years, edit lock
# ---------------------------------------------------------------------------------------- #
def key_figures(L: Ledger, fy: int) -> dict:
    out = {k: ZERO for k in ("income", "expenses", "appropriations", "assets", "liabilities", "equity")}
    for code, v in L.closing(fy).items():
        a = L.chart.get(code)
        rc = a.report_code if a else ""
        if L.is_appropriation(code):
            out["appropriations"] += v
        elif rc.startswith("REV"):
            out["income"] -= v
        elif rc.startswith("EXP"):
            out["expenses"] += v
        elif rc.startswith("ASS"):
            out["assets"] += v
        elif rc.startswith("LIA"):
            out["liabilities"] -= v
        elif rc.startswith("EQU"):
            out["equity"] -= v
    out["profit"] = out["income"] - out["expenses"]
    out["net_assets"] = out["assets"] - out["liabilities"]
    out["total_equity"] = out["equity"] + out["profit"] - out["appropriations"]
    return out


@bp.route("/c/<slug>/")
def overview(slug):
    L = g.ledger
    settings().touch_client(slug)
    years = []
    for fy in reversed(g.years):
        js = L.journals(fy, statuses=None, bank=False)
        tb = L.trial_balance(fy)
        cs = L.close_status(fy)
        years.append({"fy": fy, "range": L.fy_range(fy), "locked": L.is_locked(fy), "difference": tb["difference"],
                      "profit": tb["profit"], "posted": sum(j.status == "posted" for j in js),
                      "drafts": sum(j.status == "draft" for j in js), "close": cs})
    figures = key_figures(L, g.fy) if g.fy in g.years else None
    return render_template("overview.html", year_rows=years, figures=figures, conflicts=store().conflicts(slug),
                           activity=store().read_audit(slug, limit=8), banks=bankmod.summaries(L, g.fy))


@bp.route("/c/<slug>/years/start", methods=["POST"])
def year_start(slug):
    L = g.ledger
    fy = request.form.get("new_fy", type=int)
    if not fy or not (1990 <= fy <= 2100):
        flash("Enter the financial year, e.g. 2026 for the year ending 30 June 2026.", "error")
        return redirect(client_home())
    if g.years and fy > max(g.years):
        flash(f"Start FY{max(g.years) + 1} by rolling FY{max(g.years)} forward at Year end - that also posts "
              f"the year-end close.", "error")
        return redirect(url_for("ui.year_end", slug=slug, fy=max(g.years)))
    if g.years and fy != min(g.years) - 1:
        flash(f"Years run on from each other - the earliest you can add now is FY{min(g.years) - 1}.", "error")
        return redirect(client_home())
    L.ensure_year(fy)
    flash(f"FY{fy} ({L.fy_range(fy)[0]:%d/%m/%Y} - {L.fy_range(fy)[1]:%d/%m/%Y}) started.", "ok")
    return redirect(url_for("ui.overview", slug=slug, fy=fy))


@bp.route("/c/<slug>/details", methods=["GET", "POST"])
def client_details(slug):
    L = g.ledger
    c = L.client
    has_journals = any(L.journals(fy, statuses=None) for fy in g.years)
    equity_accounts = [a for a in L.chart.usable() if a.report_code.startswith("EQU")]
    rounding_accounts = [a for a in L.chart.usable() if a.report_code.startswith(("REV", "EXP"))]
    opts_known = set(OPTION_CHOICES) | set(OPTION_TEXT)
    if request.method == "POST":
        f = request.form
        before = c.to_dict()
        errors = []
        name = (f.get("name") or "").strip()
        if not name:
            errors.append("Client name is required.")
        new_type = f.get("type") or c.type
        if new_type not in ENTITY_TYPES:
            errors.append("Choose the entity type.")
        elif new_type != c.type and has_journals:
            errors.append("The entity type cannot change once journals are posted - set up a new client instead.")
        try:
            month, day = fy_end_from_form(f)
        except ValueError as e:
            errors.append(str(e))
            month, day = c.fy_end_month, c.fy_end_day
        if (month, day) != (c.fy_end_month, c.fy_end_day) and g.years:
            errors.append("The year-end date cannot change once a financial year has been started.")
        try:
            extra = json.loads(f.get("options_json") or "{}")
            if not isinstance(extra, dict):
                raise ValueError("must be a JSON object { ... }")
        except ValueError as e:
            errors.append(f"Advanced report options: {e}")
            extra = None
        if errors:
            for e in errors:
                flash(e, "error")
            return render_template("client_form.html", new=False, form=f, has_journals=has_journals,
                                   equity_accounts=equity_accounts, rounding_accounts=rounding_accounts,
                                   option_choices=OPTION_CHOICES)
        type_changed = new_type != c.type
        c.name, c.type, c.fy_end_month, c.fy_end_day = name, new_type, month, day
        for k, v in client_fields_from_form(f).items():
            setattr(c, k, v)
        c.retained_earnings_account = f.get("retained_earnings_account") or ""
        c.rounding_account = f.get("rounding_account") or ""
        c.gst_registered = f.get("gst_registered") == "yes"
        opts = {k: v for k, v in extra.items() if k not in opts_known}
        for key in OPTION_CHOICES:
            v = f.get(f"opt_{key}") or ""
            if key == "decimals" and v:
                opts[key] = int(v)
            elif v in ("yes", "no"):
                opts[key] = v == "yes"
            elif v:
                opts[key] = v
        for key in OPTION_TEXT:
            v = (f.get(f"opt_{key}") or "").strip()
            if v:
                opts[key] = v
        c.options = opts
        after = c.to_dict()
        changes = {k: [before.get(k), v] for k, v in after.items() if before.get(k) != v}
        if changes:
            store().save_client(c, "client.updated", {"changes": changes})
            flash("Client details saved.", "ok")
            if type_changed:
                # nothing is posted yet, so the client simply starts again on the firm's chart for the new type
                shutil.copyfile(store().firm_chart_path(new_type), store().chart_path(slug))
                store().audit(slug, "chart.replaced", {"with": f"firm {new_type} chart", "why": "entity type changed"})
                flash(f"The chart of accounts is now the firm's {ENTITY_LABELS[new_type].lower()} chart.", "ok")
        else:
            flash("Nothing changed.", "ok")
        return redirect(url_for("ui.client_details", slug=slug))
    form = c.to_dict()
    form["directors"] = "\n".join(c.directors)
    for key in OPTION_CHOICES:
        v = c.options.get(key)
        form[f"opt_{key}"] = ("yes" if v else "no") if isinstance(v, bool) else ("" if v is None else str(v))
    for key in OPTION_TEXT:
        form[f"opt_{key}"] = c.options.get(key, "")
    form["options_json"] = json.dumps({k: v for k, v in c.options.items() if k not in opts_known}, indent=2)
    return render_template("client_form.html", new=False, form=form, has_journals=has_journals,
                           equity_accounts=equity_accounts, rounding_accounts=rounding_accounts,
                           option_choices=OPTION_CHOICES)


@bp.route("/c/<slug>/archive", methods=["POST"])
def client_archive(slug):
    L = g.ledger
    if (request.form.get("confirm_name") or "").strip().lower() != L.client.name.strip().lower():
        flash("Type the client's name exactly to archive it.", "error")
        return redirect(url_for("ui.client_details", slug=slug))
    dest = store().archive_client(slug)
    cfg("OPEN_CLIENTS").pop(slug, None)
    flash(f"{L.client.name} archived - its files are in {dest}.", "ok")
    return redirect(url_for("ui.clients"))


@bp.route("/c/<slug>/lock/heartbeat", methods=["POST"])
def lock_heartbeat(slug):
    st = store()
    info = st.lock_info(slug)
    if not info or info["mine"] or info["stale"]:
        ok, info = st.acquire_lock(slug)
        if ok:
            cfg("OPEN_CLIENTS")[slug] = st.user
    other = bool(info and not info["mine"])
    return jsonify({"readonly": other, "user": info.get("user", "") if other else "",
                    "machine": info.get("machine", "") if other else ""})


@bp.route("/c/<slug>/lock/take", methods=["POST"])
def lock_take(slug):
    store().acquire_lock(slug, force=True)
    cfg("OPEN_CLIENTS")[slug] = store().user
    flash("You are now editing this client. The other person can no longer save changes to it.", "warn")
    return redirect(client_home())


@bp.route("/c/<slug>/lock/release", methods=["POST"])
def lock_release(slug):
    store().release_lock(slug)
    cfg("OPEN_CLIENTS").pop(slug, None)
    return redirect(url_for("ui.clients"))


# ---------------------------------------------------------------------------------------- #
# journals
# ---------------------------------------------------------------------------------------- #
def _amount_text(d) -> str:
    return f"{d:,.2f}" if d else ""


def _form_lines(j: Journal, minimum: int = 4, extra: int = 2) -> list[dict]:
    """The journal's lines as typed: without the automatic GST lines, GST-inclusive amounts back to gross."""
    lines = []
    for ln in j.lines:
        if ln.auto:
            continue
        add = ln.gst if ln.gst and j.amounts != "exclusive" else ZERO
        lines.append({"account": ln.account, "description": ln.description, "tax": ln.tax,
                      "debit": _amount_text(ln.debit + add if ln.debit else ln.debit),
                      "credit": _amount_text(ln.credit + add if ln.credit else ln.credit)})
    target = max(minimum, len(lines) + extra)
    while len(lines) < target:
        lines.append({"account": "", "description": "", "debit": "", "credit": "", "tax": ""})
    return lines


def _raw_lines(form) -> list[dict]:
    idx = sorted({int(k.split("_")[1]) for k in form if re.fullmatch(r"(account|debit|credit|desc|tax)_\d+", k)})
    rows = [{"account": form.get(f"account_{i}", ""), "description": form.get(f"desc_{i}", ""),
             "debit": form.get(f"debit_{i}", ""), "credit": form.get(f"credit_{i}", ""), "tax": form.get(f"tax_{i}", "")}
            for i in idx]
    rows = [r for r in rows if any(v.strip() for k, v in r.items() if k != "tax")]
    while len(rows) < 4:
        rows.append({"account": "", "description": "", "debit": "", "credit": "", "tax": ""})
    return rows


def _editor(j: Journal, jfy: int, new: bool, errors=None, lines=None, date_text=None):
    L = g.ledger
    return render_template("journal_edit.html", j=j, jfy=jfy, new=new, errors=errors or [],
                           lines=lines or _form_lines(j), accounts=account_choices(L),
                           date_text=date_text if date_text is not None else f"{j.date:%d/%m/%Y}",
                           names={a.code: a.name for a in L.chart.accounts}, info=account_info(L),
                           gst_on=bool(L.client.gst_registered or any(ln.tax for ln in j.lines)),
                           rates=bankmod.TAX_RATES, **new_account_ctx(L))


@bp.route("/c/<slug>/journals")
def journals(slug):
    L = g.ledger
    status = request.args.get("status") or "all"
    kind = request.args.get("kind") or ""
    q = (request.args.get("q") or "").strip().lower()
    every = L.journals(g.fy, statuses=None, bank=kind == "bank") if g.fy in g.years else []
    js = [j for j in every if status == "all" or j.status == status]
    if kind:
        js = [j for j in js if j.kind == kind]
    if q:
        js = [j for j in js if q in j.narration.lower() or q in j.number.lower() or q in (j.reference or "").lower()
              or any(q == ln.account.lower() or q in ln.description.lower() for ln in j.lines)]
    js.sort(key=lambda j: (j.date, j.number), reverse=True)
    return render_template("journals.html", journals=js, status=status, kind=kind, q=q,
                           counts=Counter(j.status for j in every))


@bp.route("/c/<slug>/journals/new")
def journal_new(slug):
    L = g.ledger
    if g.fy not in g.years:
        flash(f"FY{g.fy} has not been started.", "error")
        return redirect(client_home())
    j = Journal(date=L.fy_range(g.fy)[1], narration="")
    src = request.args.get("copy") or ""
    m = re.fullmatch(r"(\d{4})/([A-Za-z0-9-]{1,30})", src)
    if m:
        o = L.get_journal(int(m.group(1)), m.group(2))
        j.narration, j.reference, j.amounts = o.narration, o.reference, o.amounts
        j.lines = [Line(ln.account, debit=ln.debit, credit=ln.credit, description=ln.description, tax=ln.tax,
                        gst=ln.gst) for ln in o.lines if not ln.auto]
        j.date = min(max(o.date, L.fy_range(g.fy)[0]), L.fy_range(g.fy)[1])
    return _editor(j, g.fy, new=True)


@bp.route("/c/<slug>/journals/<int:jfy>/<number>")
def journal_view(slug, jfy, number):
    L = g.ledger
    try:
        j = L.get_journal(jfy, number)
    except StoreError:
        abort(404, f"There is no journal {number} in FY{jfy}.")
    editable = (not g.readonly and not L.is_locked(jfy) and j.status != "void" and j.kind in EDITABLE_KINDS)
    if editable and (j.status == "draft" or request.args.get("edit") == "1"):
        return _editor(j, jfy, new=False)
    accounts = {a.code: a for a in L.chart.accounts}
    reverse_on = L.fy_range(jfy + 1)[0] if j.date == L.fy_range(jfy)[1] else j.date
    return render_template("journal_view.html", j=j, jfy=jfy, editable=editable, accounts=accounts,
                           reverse_on=reverse_on, has_tax=any(ln.tax or ln.auto for ln in j.lines))


@bp.route("/c/<slug>/journals/save", methods=["POST"])
def journal_save(slug):
    L = g.ledger
    f = request.form
    post = f.get("op") in ("post", "post_new")
    number = (f.get("number") or "").strip()
    jfy = f.get("jfy", type=int) or g.fy
    raw = _raw_lines(f)
    try:
        j = journal_from_form(f.to_dict())
    except ValueError as e:
        stub = Journal(date=L.fy_range(jfy)[1], narration=(f.get("narration") or "").strip(),
                       reference=(f.get("reference") or "").strip(), number=number)
        return _editor(stub, jfy, new=not number, errors=[str(e)], lines=raw, date_text=f.get("date", ""))
    if number:
        try:
            existing = L.get_journal(jfy, number)
        except StoreError:
            abort(404)
        if existing.kind not in EDITABLE_KINDS:
            flash(f"{number} was made by an import or the year-end close - re-run that instead of editing it.", "error")
            return redirect(url_for("ui.journal_view", slug=slug, jfy=jfy, number=number))
        j.kind, j.source = existing.kind, existing.source
        j.reverses, j.reversed_by = existing.reverses, existing.reversed_by
        fy = jfy
    else:
        fy = L.client.fy_of(j.date)
        if fy not in g.years:
            later = bool(g.years) and fy > max(g.years)
            msg = (f"{j.date:%d/%m/%Y} falls in FY{fy}, which has not been started"
                   + (f" - roll FY{max(g.years)} forward at Year end first." if later else "."))
            return _editor(j, jfy, new=True, errors=[msg], lines=raw)
    try:
        if any(ln.tax for ln in j.lines):
            L.apply_gst(j)              # GST lines to the GST account; inclusive amounts cut back to net
        j = L.save_journal(j, fy, post=post)
    except ValidationError as e:
        return _editor(j, fy, new=not number, errors=e.errors, lines=raw)
    flash(f"{j.number} {'posted' if post else 'saved as a draft'}.", "ok")
    if f.get("op") == "post_new":
        return redirect(url_for("ui.journal_new", slug=slug, fy=fy))
    return redirect(url_for("ui.journal_view", slug=slug, jfy=fy, number=j.number))


@bp.route("/c/<slug>/journals/<int:jfy>/<number>/void", methods=["POST"])
def journal_void(slug, jfy, number):
    g.ledger.void_journal(jfy, number, request.form.get("reason", ""))
    flash(f"{number} voided - it no longer affects any balance.", "ok")
    return redirect(url_for("ui.journal_view", slug=slug, jfy=jfy, number=number))


@bp.route("/c/<slug>/journals/<int:jfy>/<number>/reverse", methods=["POST"])
def journal_reverse(slug, jfy, number):
    L = g.ledger
    on = arg_date("on")
    had = set(L.years())
    r = L.reverse_journal(jfy, number, on)
    rfy = L.client.fy_of(r.date)
    msg = f"{number} reversed by {r.number} on {r.date:%d/%m/%Y}."
    if rfy not in had:
        msg += f" FY{rfy} was started for it - run Year end for FY{jfy} when the year is final."
    flash(msg, "ok")
    return redirect(url_for("ui.journal_view", slug=slug, jfy=rfy, number=r.number))


@bp.route("/c/<slug>/journals/<int:jfy>/<number>/delete", methods=["POST"])
def journal_delete(slug, jfy, number):
    g.ledger.delete_draft(jfy, number)
    flash(f"Draft {number} deleted.", "ok")
    return redirect(url_for("ui.journals", slug=slug, fy=jfy))


# ---------------------------------------------------------------------------------------- #
# chart of accounts
# ---------------------------------------------------------------------------------------- #
def _accounts_page(form=None, status=200):
    L = g.ledger
    bal = L.closing(g.fy) if g.fy in g.years else {}
    used = used_accounts(L)
    show = request.args.get("show") or "all"
    rows = []
    for a in L.chart.accounts:
        b = bal.get(a.code, ZERO)
        if show == "used" and a.code not in used:
            continue
        if show == "balances" and not b:
            continue
        rows.append({"a": a, "balance": b, "prints_as": describe(a.report_code, a.name), "used": a.code in used})
    return render_template("accounts.html", rows=rows, show=show, types=ACCOUNT_TYPES, codes=report_code_options(),
                           form=form or {}), status


@bp.route("/c/<slug>/accounts")
def accounts(slug):
    return _accounts_page()


@bp.route("/c/<slug>/accounts/add", methods=["POST"])
def account_add(slug):
    L = g.ledger
    f = request.form
    rc = (f.get("report_code") or "").split(" ")[0]
    try:
        a = L.chart.add(f.get("code", ""), f.get("name", ""), rc, f.get("type") or "", f.get("reporting_name") or "")
    except ChartError as e:
        flash(str(e), "error")
        return _accounts_page(form=f, status=400)
    store().audit(slug, "account.added", {"code": a.code, "name": a.name, "report_code": a.report_code, "type": a.type})
    flash(f"Account {a.code} {a.name} added - it prints in {describe(a.report_code, a.name)}.", "ok")
    return redirect(url_for("ui.accounts", slug=slug) + f"#a-{a.code}")


@bp.route("/c/<slug>/accounts/quick-add", methods=["POST"])
def account_quick_add(slug):
    """The 'New account' pop-up: type, number and name (and tax rate) - answered as JSON for the page's script."""
    L = g.ledger
    f = request.form
    as_json = request.headers.get("X-Requested-With") == "fetch"
    acct_type = (f.get("type") or "").strip()
    rc = (f.get("report_code") or "").strip().split(" ")[0] or default_report_code(acct_type)
    tax = f.get("tax") or ""
    if tax not in bankmod.TAX_RATES:
        tax = bankmod.tax_for_type(acct_type, L.client.gst_registered)
    try:
        if acct_type not in ACCOUNT_TYPES:
            raise ChartError("choose the account type")
        a = L.chart.add(f.get("code", ""), f.get("name", ""), rc, acct_type, tax_code=tax)
    except ChartError as e:
        if as_json:
            return jsonify({"ok": False, "error": str(e)}), 400
        flash(f"The account was not added: {e}", "error")
        return redirect(_safe_next(f.get("next")))
    store().audit(slug, "account.added", {"code": a.code, "name": a.name, "report_code": a.report_code,
                                          "type": a.type, "tax": a.tax_code})
    if as_json:
        return jsonify({"ok": True, "account": {"code": a.code, "name": a.name, "type": a.type,
                                                "report_code": a.report_code, "tax": bankmod.default_tax(a)}})
    flash(f"Account {a.code} {a.name} added.", "ok")
    return redirect(_safe_next(f.get("next")))


@bp.route("/c/<slug>/accounts/<code>", methods=["GET", "POST"])
def account_edit(slug, code):
    L = g.ledger
    a = L.chart.get(code)
    if a is None:
        abort(404, f"There is no account {code}.")
    if request.method == "POST":
        f = request.form
        if f.get("op") == "delete":
            if code in used_accounts(L):
                flash(f"{code} has journals in it, so it cannot be deleted.", "error")
                return redirect(url_for("ui.account_edit", slug=slug, code=code))
            L.chart.remove(code)
            store().audit(slug, "account.deleted", {"code": code, "name": a.name, "report_code": a.report_code})
            flash(f"Account {code} {a.name} deleted.", "ok")
            return redirect(url_for("ui.accounts", slug=slug))
        try:
            changes = L.chart.update(code, name=f.get("name", a.name), report_code=(f.get("report_code") or "").split(" ")[0],
                                     reporting_name=f.get("reporting_name", ""), acct_type=f.get("type", a.type))
        except ChartError as e:
            flash(str(e), "error")
            return redirect(url_for("ui.account_edit", slug=slug, code=code))
        if changes:
            store().audit(slug, "account.updated", {"code": code, "changes": changes})
            flash(f"Account {code} saved.", "ok")
        return redirect(url_for("ui.accounts", slug=slug) + f"#a-{code}")
    history = [(fy, L.closing(fy).get(code, ZERO)) for fy in g.years]
    return render_template("account_edit.html", a=a, types=ACCOUNT_TYPES, codes=report_code_options(),
                           prints_as=describe(a.report_code, a.name), history=history,
                           used=code in used_accounts(L))


@bp.route("/c/<slug>/accounts/export.csv")
def accounts_export(slug):
    name = secure_filename(g.ledger.client.name) or "client"
    return send_file(store().chart_path(slug), as_attachment=True, mimetype="text/csv",
                     download_name=f"{name} chart of accounts.csv")


# ---------------------------------------------------------------------------------------- #
# imports
# ---------------------------------------------------------------------------------------- #
UPLOAD_TYPES = {"balances": {".csv", ".txt", ".xlsx", ".xlsm"}, "workpaper": {".xlsx", ".xlsm"},
                "journals": {".csv", ".txt", ".xlsx", ".xlsm"}, "chart": {".csv"}}


def _save_upload(file, pid_dir: Path) -> Path:
    name = secure_filename(file.filename or "") or "upload"
    if "." not in name:
        name += Path(file.filename or "").suffix.lower()
    p = pid_dir / name
    file.save(p)
    return p


def _meta_path(pid: str) -> Path:
    if not PID_RE.match(pid or ""):
        abort(404)
    return work_dir() / pid / "meta.json"


def _load_meta(pid: str) -> dict:
    p = _meta_path(pid)
    if not p.exists():
        abort(404, "That import has expired - upload the file again.")
    meta = json.loads(p.read_text(encoding="utf-8"))
    if meta.get("slug") != g.slug:
        abort(404)
    return meta


def _save_meta(pid: str, meta: dict) -> None:
    _meta_path(pid).write_text(json.dumps(meta, default=str), encoding="utf-8")


def _finish(pid: str) -> None:
    gc.collect()        # let go of any workbook still holding the upload open (Windows cannot delete open files)
    shutil.rmtree(work_dir() / pid, ignore_errors=True)


@bp.route("/c/<slug>/import")
def import_start(slug):
    return render_template("import.html")


@bp.route("/c/<slug>/import/upload", methods=["POST"])
def import_upload(slug):
    L = g.ledger
    f = request.form
    kind = f.get("kind") or ""
    up = request.files.get("file")
    if kind not in UPLOAD_TYPES:
        abort(400)
    if not up or not up.filename:
        flash("Choose a file to import.", "error")
        return redirect(url_for("ui.import_start", slug=slug))
    ext = Path(up.filename).suffix.lower()
    if ext not in UPLOAD_TYPES[kind]:
        flash(f"{up.filename}: use a {' or '.join(sorted(UPLOAD_TYPES[kind]))} file for this import.", "error")
        return redirect(url_for("ui.import_start", slug=slug))
    fy = f.get("fy", type=int) or g.fy
    if kind in ("balances", "workpaper"):
        if not g.years and 1990 <= fy <= 2100:
            L.ensure_year(fy)          # a new client's first year
            g.years = L.years()
        if fy not in g.years:
            flash(f"FY{fy} has not been started - start it on the Overview (or roll forward at Year end).", "error")
            return redirect(url_for("ui.import_start", slug=slug))
    pid = secrets.token_hex(8)
    d = work_dir() / pid
    d.mkdir(parents=True)
    path = _save_upload(up, d)
    meta = {"kind": kind, "slug": slug, "fy": fy, "file": up.filename, "path": str(path)}
    sheet = (f.get("sheet") or "").strip() or None
    try:
        if kind == "balances":
            plan = imports.plan_from_file(L, path, fy, mode=f.get("mode") or "year_end", sign=f.get("sign") or "auto",
                                          sheet=sheet)
            plan.source = up.filename
            plan.complete = f.get("complete", "yes") == "yes"
            if not plan.lines:
                raise imports.ImportError_("no account lines were found in the file")
            meta["plan"] = plan.to_dict()
        elif kind == "workpaper":
            plan = imports.plan_workpaper(L, path, fy)
            plan.source = up.filename
            if not plan.lines:
                raise imports.ImportError_("no Profit & Loss or Balance Sheet lines were found in the workpaper")
            meta["plan"] = plan.to_dict()
        elif kind == "journals":
            js, errs = imports.parse_journal_rows(imports.read_table(path, sheet))
            if not js:
                raise imports.ImportError_("no journal lines were found in the file")
            for j in js:
                j.source = {**j.source, "file": up.filename}
            meta["journals"] = [j.to_dict() for j in js]
            meta["parse_errors"] = errs
        elif kind == "chart":
            incoming = chartlib.Chart(path, L.client.type)
            if not [a for a in incoming.accounts if a.code]:
                raise imports.ImportError_("no accounts with codes were found - export the chart from Xero "
                                           "(Accounting > Chart of accounts > Export)")
            meta["replace"] = f.get("replace") == "yes"
    except Exception as e:     # any unreadable file: say so and let the user try another
        current_app.logger.exception("import of %s failed", up.filename)
        _finish(pid)
        flash(f"Could not import {up.filename}: {e}", "error")
        return redirect(url_for("ui.import_start", slug=slug))
    _save_meta(pid, meta)
    return redirect(url_for("ui.import_review", slug=slug, pid=pid))


def _plan_totals(plan: imports.Plan) -> dict:
    from ..money import to_dec
    out = {"dr": ZERO, "cr": ZERO, "py_dr": ZERO, "py_cr": ZERO}
    for pl in plan.lines:
        if pl.action == "skip":
            continue
        v = to_dec(pl.amount)
        out["dr" if v > 0 else "cr"] += abs(v)
        if pl.py is not None:
            p = to_dec(pl.py)
            out["py_dr" if p > 0 else "py_cr"] += abs(p)
    out["diff"], out["py_diff"] = out["dr"] - out["cr"], out["py_dr"] - out["py_cr"]
    return out


def _chart_preview(L: Ledger, path: Path, replace: bool) -> dict:
    tmp = work_dir() / f"chart-preview-{secrets.token_hex(4)}.csv"
    shutil.copyfile(store().chart_path(L.slug), tmp)
    try:
        res = ClientChart(tmp, L.client.type).merge(path, replace=replace)
    finally:
        tmp.unlink(missing_ok=True)
    incoming = {a.code: a for a in chartlib.Chart(path, L.client.type).accounts}
    used = used_accounts(L)
    res["added_rows"] = [incoming[c] for c in res["added"] if c in incoming]
    res["blocked"] = [c for c in res["removed"] if c in used]
    res["removed_rows"] = [L.chart.get(c) for c in res["removed"] if L.chart.get(c)]
    return res


@bp.route("/c/<slug>/import/<pid>", methods=["GET", "POST"])
def import_review(slug, pid):
    L = g.ledger
    meta = _load_meta(pid)
    kind = meta["kind"]
    if kind in ("balances", "workpaper"):
        return _review_plan(L, pid, meta)
    if kind == "journals":
        return _review_journals(L, pid, meta)
    return _review_chart(L, pid, meta)


def _review_plan(L: Ledger, pid: str, meta: dict):
    plan = imports.Plan.from_dict(meta["plan"])
    problems: list[str] = []
    if request.method == "POST":
        f = request.form
        for i, pl in enumerate(plan.lines):
            act = f.get(f"action_{i}")
            if act in ("post", "new", "skip"):
                pl.action = act
            if f"account_{i}" in f:
                pl.account = (f.get(f"account_{i}") or "").strip().split(" ")[0]
            if pl.action == "new":
                if f"name_{i}" in f:
                    pl.account_name = (f.get(f"name_{i}") or "").strip()
                if f"rc_{i}" in f:
                    pl.report_code = (f.get(f"rc_{i}") or "").strip().split(" ")[0].upper()
        if any(pl.py is not None for pl in plan.lines):
            plan.include_py = f.get("include_py") == "yes"
        problems = imports.recheck(L, plan)
        meta["plan"] = plan.to_dict()
        _save_meta(pid, meta)
        if f.get("op") == "apply" and not problems:
            try:
                res = imports.apply_plan(L, plan, allow_suspense=f.get("allow_suspense") == "yes")
            except ValidationError as e:
                problems = e.errors
            else:
                _finish(pid)
                flash(f"Imported {meta['file']}: {len(res['journals'])} journal(s) posted"
                      f"{', ' + str(len(res['created_accounts'])) + ' new account(s)' if res['created_accounts'] else ''}.", "ok")
                return redirect(url_for("ui.trial_balance", slug=g.slug, fy=plan.fy))
    return render_template("import_plan.html", meta=meta, plan=plan, problems=problems, totals=_plan_totals(plan),
                           accounts=account_choices(L), codes=report_code_options(), pid=pid,
                           has_py=any(pl.py is not None for pl in plan.lines))


def _review_journals(L: Ledger, pid: str, meta: dict):
    js = [Journal.from_dict(d) for d in meta["journals"]]
    checks = []
    for j in js:
        fy = L.client.fy_of(j.date)
        errs = L.validate(j, fy) if fy in g.years else [f"FY{fy} has not been started"]
        checks.append({"j": j, "fy": fy, "errors": errs})
    if request.method == "POST":
        post = request.form.get("op") == "post"
        missing = sorted({c["fy"] for c in checks if c["fy"] not in g.years})
        if missing:
            flash(f"Start {', '.join(f'FY{y}' for y in missing)} first.", "error")
        elif post and any(c["errors"] for c in checks):
            flash("Fix the journals marked below (or save them all as drafts and fix them one by one).", "error")
        else:
            numbers = imports.import_journals(L, js, meta["file"], post=post)
            _finish(pid)
            flash(f"{len(numbers)} journal(s) {'posted' if post else 'saved as drafts'} from {meta['file']}.", "ok")
            return redirect(url_for("ui.journals", slug=g.slug, fy=checks[0]["fy"] if checks else g.fy))
    return render_template("import_journals.html", meta=meta, checks=checks, pid=pid,
                           parse_errors=meta.get("parse_errors") or [], names={a.code: a.name for a in L.chart.accounts},
                           ok=not any(c["errors"] for c in checks))


def _review_chart(L: Ledger, pid: str, meta: dict):
    path = Path(meta["path"])
    replace = (request.form.get("replace") == "yes") if request.method == "POST" else meta.get("replace", False)
    preview = _chart_preview(L, path, replace)
    if request.method == "POST" and request.form.get("op") == "apply":
        if preview["blocked"]:
            flash(f"Accounts {', '.join(preview['blocked'])} have journals, so they cannot be removed - "
                  f"untick 'remove accounts that are not in the file'.", "error")
        else:
            res = L.chart.merge(path, replace=replace)
            store().audit(g.slug, "chart.imported", {"source": meta["file"], "added": len(res["added"]),
                                                     "changed": len(res["changed"]), "removed": len(res["removed"]),
                                                     "replace": replace})
            _finish(pid)
            flash(f"Chart updated from {meta['file']}: {len(res['added'])} added, {len(res['changed'])} changed"
                  f"{', ' + str(len(res['removed'])) + ' removed' if res['removed'] else ''}.", "ok")
            return redirect(url_for("ui.accounts", slug=g.slug))
    return render_template("import_chart.html", meta=meta, preview=preview, replace=replace, pid=pid)


# ---------------------------------------------------------------------------------------- #
# on-screen reports: trial balance, account transactions
# ---------------------------------------------------------------------------------------- #
@bp.route("/c/<slug>/trial-balance")
def trial_balance(slug):
    L = g.ledger
    if g.fy not in g.years:
        return render_template("not_started.html")
    start, end = L.fy_range(g.fy)
    upto = clamp(arg_date("upto", end), start, end)
    tb = L.trial_balance(g.fy, None if upto == end else upto)
    return render_template("tb.html", tb=tb, upto=upto)


@bp.route("/c/<slug>/account-transactions")
def account_transactions(slug):
    L = g.ledger
    if g.fy not in g.years:
        return render_template("not_started.html")
    s, e = L.fy_range(g.fy)
    start, end = clamp(arg_date("start", s), s, e), clamp(arg_date("end", e), s, e)
    code = (request.args.get("code") or "").strip().split(" ")[0]
    data = L.general_ledger(g.fy, start, end, [code] if code else None)
    return render_template("gl.html", data=data, code=code, start=start, end=end, accounts=account_choices(L))


def _period(default_start: dt.date, default_end: dt.date) -> tuple[dt.date, dt.date]:
    start, end = arg_date("start", default_start), arg_date("end", default_end)
    return (start, end) if start <= end else (default_start, default_end)


@bp.route("/c/<slug>/profit-and-loss")
def profit_and_loss(slug):
    L = g.ledger
    if g.fy not in g.years:
        return render_template("not_started.html")
    start, end = _period(*L.fy_range(g.fy))
    compare = request.args.get("compare", "yes") == "yes"
    try:
        data = statements.profit_and_loss(L, start, end, compare)
    except statements.StatementError as e:
        flash(str(e), "error")
        data = statements.profit_and_loss(L, *L.fy_range(g.fy), compare)
        start, end = data["start"], data["end"]
    return render_template("statement.html", data=data, report="profit_and_loss", start=start, end=end,
                           compare=compare)


@bp.route("/c/<slug>/balance-sheet")
def balance_sheet(slug):
    L = g.ledger
    if g.fy not in g.years:
        return render_template("not_started.html")
    as_at = arg_date("as_at", L.fy_range(g.fy)[1])
    compare = request.args.get("compare", "yes") == "yes"
    try:
        data = statements.balance_sheet(L, as_at, compare)
    except statements.StatementError as e:
        flash(str(e), "error")
        as_at = L.fy_range(g.fy)[1]
        data = statements.balance_sheet(L, as_at, compare)
    return render_template("statement.html", data=data, report="balance_sheet", as_at=as_at, compare=compare)


@bp.route("/c/<slug>/gst-report")
def gst_page(slug):
    L = g.ledger
    if g.fy not in g.years:
        return render_template("not_started.html")
    start, end = _period(*L.fy_range(g.fy))
    basis = request.args.get("basis") if request.args.get("basis") in statements.BASES else "accrual"
    data = statements.gst_report(L, start, end, basis)
    return render_template("gst.html", data=data, start=start, end=end, basis=basis,
                           notes=reports.gst_notes(data), bases=statements.BASES)


# ---------------------------------------------------------------------------------------- #
# report files
# ---------------------------------------------------------------------------------------- #
def _report_path(slug: str, rfy: int, name: str) -> Path:
    d = (store().year_dir(slug, rfy) / "reports").resolve()
    p = (d / name).resolve()
    if p.parent != d or not p.is_file():
        abort(404, "That report file is not in the client's reports folder.")
    return p


@bp.route("/c/<slug>/reports")
def reports_page(slug):
    files = store().list_reports(slug, g.fy) if g.fy in g.years else []
    result = cfg("RESULTS").get(request.args.get("r") or "")
    return render_template("reports.html", files=files, result=result, new=request.args.get("new") or "",
                           folder=store().year_dir(slug, g.fy) / "reports")


@bp.route("/c/<slug>/reports/generate", methods=["POST"])
def report_generate(slug):
    L = g.ledger
    f = request.form
    kind = f.get("report") or ""
    if kind not in REPORT_TITLES:
        abort(400)
    fy = g.fy
    if fy not in g.years:
        flash(f"FY{fy} has not been started.", "error")
        return redirect(url_for("ui.reports_page", slug=slug))
    s, e = L.fy_range(fy)
    excel = f.get("format") == "xlsx" and kind != "financial_reports"
    stamp = dt.datetime.now().strftime("%Y-%m-%d %H%M%S")
    folder = store().reports_dir(slug, fy)
    basis = f.get("basis") if f.get("basis") in statements.BASES else "accrual"
    title = REPORT_TITLES[kind] + (f" ({basis} basis)" if kind == "gst_report" else "")
    out = folder / f"{title} FY{fy} {stamp}.{'xlsx' if excel else 'pdf'}"
    rid = None
    if kind == "financial_reports":
        upto = clamp(arg_date("upto", e), s, e)
        res = reports.financial_reports(L, fy, out, final=f.get("final") == "yes",
                                        upto=None if upto == e else upto,
                                        date_signed=(f.get("date_signed") or "").strip())
        if res["draft"]:
            draft = out.with_name(f"{REPORT_TITLES[kind]} FY{fy} DRAFT {stamp}.pdf")
            out.rename(draft)
            out = draft
        rid = remember({"file": out.name, "draft": res["draft"], "has_errors": res["has_errors"],
                        "validation": res["validation"], "summary": res["summary"]})
    elif kind == "trial_balance":
        upto = clamp(arg_date("upto", e), s, e)
        reports.trial_balance(L, fy, out, upto=None if upto == e else upto, as_excel=excel)
    elif kind == "profit_and_loss":
        start, end = _period(s, e)
        reports.profit_and_loss(L, out, start, end, compare=f.get("compare", "yes") == "yes", as_excel=excel)
    elif kind == "balance_sheet":
        reports.balance_sheet(L, out, arg_date("as_at", e), compare=f.get("compare", "yes") == "yes", as_excel=excel)
    elif kind == "gst_report":
        start, end = _period(s, e)
        reports.gst_report(L, out, start, end, basis=basis, as_excel=excel)
    elif kind == "general_ledger":
        codes = [c for c in re.split(r"[\s,;]+", f.get("codes") or "") if c]
        reports.general_ledger(L, fy, out, start=clamp(arg_date("start", s), s, e), end=clamp(arg_date("end", e), s, e),
                               codes=codes or None, as_excel=excel)
    else:
        reports.journal_report(L, fy, out, include_void=f.get("include_void") == "yes", as_excel=excel)
    flash(f"{out.name} saved in the client's reports folder.", "ok")
    if f.get("open") == "yes":
        try:
            open_path(out)
        except OSError as err:
            flash(f"Could not open it automatically ({err}) - use Open below.", "warn")
    return redirect(url_for("ui.reports_page", slug=slug, fy=fy, r=rid, new=out.name))


@bp.route("/c/<slug>/reports/<int:rfy>/file/<path:name>")
def report_file(slug, rfy, name):
    p = _report_path(slug, rfy, name)
    return send_file(p, as_attachment=request.args.get("dl") == "1", download_name=p.name)


@bp.route("/c/<slug>/reports/<int:rfy>/open/<path:name>", methods=["POST"])
def report_open(slug, rfy, name):
    p = _report_path(slug, rfy, name)
    try:
        open_path(p, folder=request.form.get("folder") == "yes")
    except OSError as e:
        flash(f"Could not open {p.name}: {e}", "error")
    return redirect(url_for("ui.reports_page", slug=slug, fy=rfy))


# ---------------------------------------------------------------------------------------- #
# year end
# ---------------------------------------------------------------------------------------- #
@bp.route("/c/<slug>/year-end")
def year_end(slug):
    L = g.ledger
    if g.fy not in g.years:
        return render_template("not_started.html")
    nxt = g.fy + 1
    return render_template("year_end.html", st=L.close_status(g.fy), tb=L.trial_balance(g.fy),
                           drafts=L.journals(g.fy, statuses=("draft",)), state=L.year_state(g.fy),
                           next_locked=nxt in g.years and L.is_locked(nxt), next_range=L.fy_range(nxt),
                           names={a.code: a.name for a in L.chart.accounts})


@bp.route("/c/<slug>/year-end/close", methods=["POST"])
def year_close(slug):
    L = g.ledger
    res = L.close_year(g.fy)
    nxt = res["next_fy"]
    msg = f"FY{g.fy} rolled forward - FY{nxt} is open."
    msg += (f" Year-end close journal {res['journal']} moved {len(res['moves'])} balance(s) on "
            f"{L.fy_range(nxt)[0]:%d/%m/%Y}." if res["journal"] else " No closing journal was needed.")
    if res["created"]:
        msg += " New accounts: " + ", ".join(f"{a['code']} {a['name']}" for a in res["created"]) + "."
    flash(msg, "ok")
    return redirect(url_for("ui.year_end", slug=slug, fy=g.fy))


@bp.route("/c/<slug>/year-end/lock", methods=["POST"])
def year_lock(slug):
    g.ledger.lock_year(g.fy, (request.form.get("reason") or "").strip())
    flash(f"FY{g.fy} locked - no journal in it can change until it is unlocked.", "ok")
    return redirect(url_for("ui.year_end", slug=slug, fy=g.fy))


@bp.route("/c/<slug>/year-end/unlock", methods=["POST"])
def year_unlock(slug):
    g.ledger.unlock_year(g.fy, (request.form.get("reason") or "").strip())
    flash(f"FY{g.fy} unlocked.", "warn")
    return redirect(url_for("ui.year_end", slug=slug, fy=g.fy))


# ---------------------------------------------------------------------------------------- #
# change log
# ---------------------------------------------------------------------------------------- #
@bp.route("/c/<slug>/changes")
def change_log(slug):
    q = (request.args.get("q") or "").strip().lower()
    rows = store().read_audit(slug, limit=5000)
    if q:
        rows = [r for r in rows if q in json.dumps(r, default=str).lower()]
    return render_template("audit.html", rows=rows[:1000], q=q, total=len(rows))


@bp.app_template_filter("audit_text")
def audit_text(entry: dict) -> str:
    d = entry.get("detail") or {}
    a = entry.get("action", "")
    if a.startswith(("journal.", "import.", "year.close")) and d.get("number"):
        t = f"{d.get('number')} {d.get('narration', '')}".strip()
        if d.get("total"):
            t += f" - {float(d['total']):,.2f}"
        if d.get("reason"):
            t += f" - reason: {d['reason']}"
        return t
    if a.startswith("account."):
        if d.get("changes"):
            return f"{d.get('code')}: " + "; ".join(f"{k} {v[0] or '(blank)'} -> {v[1]}" for k, v in d["changes"].items())
        return f"{d.get('code', '')} {d.get('name', '')} {d.get('report_code', '')}".strip()
    if a == "client.updated" and d.get("changes"):
        return "; ".join(sorted(d["changes"]))
    if a.startswith("year."):
        return " ".join(str(x) for x in (f"FY{d.get('fy')}" if d.get("fy") else "", d.get("reason", "")) if x)
    if a == "report.generated":
        return d.get("file", "")
    if a.startswith("bank."):
        acct = d.get("account", "")
        if a == "bank.imported":
            return f"{acct}: {d.get('file', '')} - {d.get('lines', 0)} new line(s), {d.get('duplicates', 0)} already there"
        if a == "bank.reconciled":
            nums = d.get("transactions") or []
            span = f" ({nums[0]}" + (f" to {nums[-1]})" if len(nums) > 1 else ")") if nums else ""
            return f"{acct}: {d.get('count', 0)} line(s) reconciled{span}"
        if a == "bank.unreconciled":
            return f"{acct}: {d.get('transaction', '')} on {d.get('date', '')}, {d.get('amount', '')}" + (
                f" - reason: {d['reason']}" if d.get("reason") else "")
        return f"{acct}: {d.get('count', 0)} line(s)" + (f" - reason: {d['reason']}" if d.get("reason") else "")
    return ", ".join(f"{k}: {v}" for k, v in d.items() if not isinstance(v, (list, dict)))[:300]


# ---------------------------------------------------------------------------------------- #
# moving a client off Xero
# ---------------------------------------------------------------------------------------- #
FY_IN_NAME = re.compile(r"(?<!\d)(?:FY\s*)?(20\d{2})(?!\d)", re.I)


@bp.route("/migrate", methods=["GET", "POST"])
def migrate():
    form = request.form if request.method == "POST" else {"fy_end_month": "6", "fy_end_day": "30", "type": "company"}
    if request.method == "GET":
        return render_template("migrate.html", form=form)
    f = request.form
    errors = []
    name = (f.get("name") or "").strip()
    etype = f.get("type") or ""
    if not name:
        errors.append("Enter the client's name.")
    if etype not in ENTITY_TYPES:
        errors.append("Choose the entity type.")
    try:
        month, day = fy_end_from_form(f)
    except ValueError as e:
        errors.append(str(e))
        month, day = 6, 30
    chart = request.files.get("chart")
    tbs = [t for t in request.files.getlist("tbs") if t and t.filename]
    if not chart or not chart.filename:
        errors.append("Add the chart of accounts exported from Xero (Accounting > Chart of accounts > Export).")
    if not tbs:
        errors.append("Add at least one trial balance exported from Xero (Reports > Trial Balance at a year end).")
    if errors:
        for e in errors:
            flash(e, "error")
        return render_template("migrate.html", form=f), 400
    pid = secrets.token_hex(8)
    d = work_dir() / pid
    d.mkdir(parents=True)
    probe = Client(id="", slug="", name=name, type=etype, fy_end_month=month, fy_end_day=day)
    try:
        chart_path = _save_upload(chart, d)
        tb_files: dict[int, Path] = {}
        for t in tbs:
            sub = d / secrets.token_hex(3)
            sub.mkdir()
            p = _save_upload(t, sub)
            as_at = imports.detect_as_at(imports.read_table(p))
            m = FY_IN_NAME.search(t.filename)
            fy = probe.fy_of(as_at) if as_at else (int(m.group(1)) if m else None)
            if fy is None:
                raise ValueError(f"{t.filename}: cannot tell which year it is - export it from Xero with its "
                                 f"'As at' title, or put the year in the file name (e.g. 'TB FY2025.xlsx')")
            if as_at and as_at != probe.fy_range(fy)[1]:
                raise ValueError(f"{t.filename} is as at {as_at:%d/%m/%Y}, not a year end - export the trial "
                                 f"balance at {probe.fy_range(fy)[1]:%d/%m/%Y}")
            if fy in tb_files:
                raise ValueError(f"two trial balances for FY{fy}")
            tb_files[fy] = p
        fields = {"name": name, "type": etype, "fy_end_month": month, "fy_end_day": day,
                  **client_fields_from_form(f), "source": {"migrated_from": "xero-files"}}
        client, results = xero.migrate_from_files(store(), fields, chart_path, tb_files)
    except Exception as e:     # bad files or a trial balance that does not balance: nothing is kept
        current_app.logger.exception("migration failed")
        shutil.rmtree(d, ignore_errors=True)
        flash(f"Migration stopped: {e}", "error")
        return render_template("migrate.html", form=f), 400
    shutil.rmtree(d, ignore_errors=True)
    summary = []
    for r in results:
        plan = r["plan"]
        summary.append({"fy": r["fy"], "journals": r["result"]["journals"], "created": r["result"]["created_accounts"],
                        "warnings": plan.warnings,
                        "review": [{"name": pl.source_name, "account": pl.account, "report_code": pl.report_code,
                                    "reason": pl.reason} for pl in plan.lines if pl.review and pl.action != "skip"]})
    rid = remember({"years": summary})
    flash(f"{client.name} moved across from Xero: {len(results)} year(s) of balances loaded.", "ok")
    return redirect(url_for("ui.migrated", slug=client.slug, r=rid))


@bp.route("/c/<slug>/migrated")
def migrated(slug):
    res = cfg("RESULTS").get(request.args.get("r") or "")
    if not res:
        return redirect(client_home())
    return render_template("migrated.html", res=res)


# ---------------------------------------------------------------------------------------- #
# who is using the app (office sign-in), shown on every screen
# ---------------------------------------------------------------------------------------- #
@bp.app_context_processor
def _user_globals():
    signed = session.get("ms_user") or {}
    name = signed.get("name") or settings().user_name or ""
    initials = "".join(p[0] for p in name.replace(".", " ").split()[:2]).upper() or "?"
    try:
        required = settings().configured and store().is_initialised() and signin_required()
        admin = is_admin()
    except StoreError:
        required, admin = False, True
    nav_tab = TAB_OF.get(request.endpoint or "") or GLOBAL_TAB.get(request.endpoint or "", "")
    return {"user_name": name, "user_email": signed.get("email", ""), "user_initials": initials,
            "signed_in": bool(signed), "signin_required": required, "icons_possible": shortcut.available(),
            "nav_tab": nav_tab, "user_is_admin": admin}


GLOBAL_TAB = {"ui.clients": "clients", "ui.client_new": "clients", "ui.xero_home": "xero", "ui.xero_org": "xero",
              "ui.migrate": "xero", "ui.setup": "settings", "ui.firm_settings": "settings", "ui.team_page": "team",
              "ui.updates_page": "settings"}


# ---------------------------------------------------------------------------------------- #
# sign in with the office email (Microsoft 365)
# ---------------------------------------------------------------------------------------- #
def _safe_next(nxt: str | None) -> str:
    """A page of this app to go on to: a plain path, never //host, /\\host or one with spaces or control characters."""
    if nxt and nxt.startswith("/") and not nxt.startswith("//") and not re.search(r"[\\\s\x00-\x1f\x7f]", nxt):
        return nxt
    return url_for("ui.clients")


def _current(kind: str):
    sid = session.get(f"{kind}_id")
    return cfg("SIGNINS").get(sid) if sid else None


def _remember_signin(kind: str, s) -> None:
    pool = cfg("SIGNINS")
    old = _current(kind)
    if old is not None:
        old.cancel()
        pool.pop(old.id, None)
    pool[s.id] = s
    session[f"{kind}_id"] = s.id


@bp.route("/signin")
def signin():
    if session.get("ms_user") and request.args.get("again") != "1":
        return redirect(_safe_next(request.args.get("next")))
    s = _current("ms")
    return render_template("signin.html", s=s, configured=ms_config() is not None, next=request.args.get("next") or "")


@bp.route("/signin/start", methods=["POST"])
def signin_start():
    pending = session.pop("ms_test", None)            # firm settings: try details before saving them
    conf = ms_config(pending) if pending else ms_config()
    if conf is None:
        flash("Office sign-in is not set up yet - see Settings > Firm settings.", "error")
        return redirect(url_for("ui.signin"))
    try:
        s = MicrosoftSignIn(conf, login_hint=settings().last_email)
        s.listen()
    except SignInError as e:
        flash(str(e), "error")
        return redirect(url_for("ui.signin"))
    s.next = _safe_next(request.form.get("next"))
    s.test_config = pending
    _remember_signin("ms", s)
    open_in_browser(s.authorize_url())
    return redirect(url_for("ui.signin", next=s.next, again="1"))


@bp.route("/signin/status")
def signin_status():
    s = _current("ms")
    if s is None:
        return jsonify({"status": "none"})
    if s.status == "signed_in":
        testing = bool(getattr(s, "test_config", None))
        if testing:
            session["ms_tested"] = {"tenant_id": s.cfg.tenant_id, "client_id": s.cfg.client_id, "email": s.user["email"]}
        if not (testing and session.get("ms_user")):      # a test from Firm settings does not change who is signed in
            session["ms_user"] = s.user
            st = settings()
            st.last_email, st.user_name = s.user["email"], s.user["name"]
            try:
                st.save()
            except OSError:
                pass
        cfg("SIGNINS").pop(s.id, None)
        session.pop("ms_id", None)
        target = url_for("ui.firm_settings") if testing else s.next
        if request.args.get("go") == "1":
            return redirect(target)
        return jsonify({"status": "signed_in", "next": target, "email": s.user["email"]})
    if request.args.get("go") == "1":
        if s.status == "error":
            flash(s.error, "error")
        return redirect(url_for("ui.signin", again="1"))
    return jsonify({"status": s.status, "error": s.error})


@bp.route("/signin/cancel", methods=["POST"])
def signin_cancel():
    s = _current("ms")
    if s is not None:
        s.cancel()
        cfg("SIGNINS").pop(s.id, None)
    session.pop("ms_id", None)
    return redirect(url_for("ui.signin", again="1"))


@bp.route("/signout", methods=["POST"])
def signout():
    release_locks(current_app)                    # the edit locks belong to the account signing out
    session.pop("ms_user", None)
    session.pop("ms_tested", None)
    flash("Signed out.", "ok")
    return redirect(url_for("ui.signin", again="1") if signin_required() else url_for("ui.clients"))


# ---------------------------------------------------------------------------------------- #
# firm settings: office sign-in, the Xero app, desktop icons
# ---------------------------------------------------------------------------------------- #
@bp.route("/firm", methods=["GET", "POST"])
def firm_settings():
    st = store()
    f = firm()
    if request.method == "POST":
        op = request.form.get("op") or ""
        if op in ("ms_test", "ms_save"):
            m = {"tenant_id": (request.form.get("tenant_id") or "").strip(),
                 "client_id": (request.form.get("ms_client_id") or "").strip(),
                 "allowed_domains": domains(request.form.get("allowed_domains") or "")}
            try:
                MicrosoftConfig(m["tenant_id"], m["client_id"]).check()
            except SignInError as e:
                flash(str(e), "error")
                return _firm_page(f, request.form)
            if op == "ms_test":
                session["ms_test"] = m
                return redirect(url_for("ui.signin_start_get"))
            tested = session.get("ms_tested") or {}
            required = request.form.get("required") == "yes"
            if team().managed and not required:
                flash("The firm manages who can open which client (Team), so office sign-in must stay required - "
                      "stop managing access on the Team page first.", "error")
                return _firm_page(f, request.form)
            same = tested.get("tenant_id") == m["tenant_id"] and tested.get("client_id") == m["client_id"]
            if required and not same:
                flash("Sign in once with these details (Test sign-in) before making sign-in required - "
                      "so nobody is locked out by a typing mistake.", "error")
                return _firm_page(f, request.form)
            st.save_firm_settings({"microsoft": {**m, "required": required}})
            flash("Office sign-in " + ("is now required on every PC that uses this data folder." if required
                                       else "details saved (sign-in is not required yet)."), "ok")
            return redirect(url_for("ui.firm_settings"))
        if op == "ms_off":
            if team().managed:
                flash("The firm manages who can open which client (Team), so office sign-in must stay required - "
                      "stop managing access on the Team page first.", "error")
                return redirect(url_for("ui.firm_settings"))
            m = dict(f.get("microsoft") or {})
            m["required"] = False
            st.save_firm_settings({"microsoft": m})
            flash("Office sign-in is no longer required.", "warn")
            return redirect(url_for("ui.firm_settings"))
        if op == "claude_save":
            can_post = request.form.get("can_post") == "yes"
            st.save_firm_settings({"claude": {"can_post": can_post}})
            flash("Claude can now post journals." if can_post else
                  "Claude now saves journals as drafts for a person to post.", "ok")
            return redirect(url_for("ui.firm_settings") + "#claude")
        if op in ("claude_connect", "claude_disconnect"):
            try:
                changed = claude_desktop.connect() if op == "claude_connect" else claude_desktop.disconnect()
            except (claude_desktop.ClaudeDesktopError, OSError) as e:
                flash(f"Claude Desktop was not changed: {e}", "error")
            else:
                if op == "claude_connect":
                    flash("Fortis Ledger is added to Claude Desktop's settings file on this PC. Quit Claude Desktop completely "
                          "(right-click its icon by the clock > Quit) and open it again. If Fortis Ledger is gone once Claude "
                          "Desktop has started, use Install in Claude Desktop instead.", "ok")
                else:
                    flash("Fortis Ledger is removed from Claude Desktop's settings file on this PC." if changed else
                          "Claude Desktop's settings file on this PC did not have Fortis Ledger.", "ok")
            return redirect(url_for("ui.firm_settings") + "#claude")
        if op in ("claude_extension", "claude_extension_file"):
            try:
                path = claude_desktop.build_extension()
            except OSError as e:
                flash(f"The Claude Desktop extension could not be made: {e}", "error")
                return redirect(url_for("ui.firm_settings") + "#claude")
            manual = ("drag it into the Claude Desktop window, or in Claude Desktop go to Settings > Extensions > "
                      "Advanced settings > Install Extension... and choose it")
            if op == "claude_extension_file":
                open_path(path, folder=True)
                flash(f"The extension is {path}. To install it, {manual}.", "ok")
                return redirect(url_for("ui.firm_settings") + "#claude")
            try:
                claude_desktop.disconnect()      # also in the settings file, Claude would list Fortis Ledger twice
            except (claude_desktop.ClaudeDesktopError, OSError):
                pass
            if claude_desktop.opens_with_claude():
                open_path(path)
                flash("Claude Desktop should now ask to install Fortis Ledger: click Install. Then, in a chat, it is under "
                      f"+ > Connectors. If Claude Desktop did not open, {manual} (Show the file finds it).", "ok")
            else:
                open_path(path, folder=True)
                flash(f"The extension is ready: {path.name}, shown in its folder. To install it, {manual}."
                      + ("" if claude_desktop.status()["installed"] else
                         " Install Claude Desktop first (claude.ai/download)."), "ok")
            return redirect(url_for("ui.firm_settings") + "#claude")
        if op == "xero_save":
            cid = (request.form.get("xero_client_id") or "").strip()
            port = request.form.get("xero_port", type=int) or XERO_PORT
            if cid and not re.fullmatch(r"[A-Za-z0-9]{16,64}", cid):
                flash("The Xero client ID is the long code on the app's Configuration page in developer.xero.com.", "error")
                return _firm_page(f, request.form)
            if not (1024 <= port <= 65535):
                flash("The port must be between 1024 and 65535.", "error")
                return _firm_page(f, request.form)
            st.save_firm_settings({"xero": {"client_id": cid, "port": port} if cid else None})
            flash("Xero connection saved." if cid else "Xero connection removed.", "ok")
            return redirect(url_for("ui.firm_settings"))
        abort(400)
    m = f.get("microsoft") or {}
    x = f.get("xero") or {}
    form = {"tenant_id": m.get("tenant_id", ""), "ms_client_id": m.get("client_id", ""),
            "allowed_domains": ", ".join(m.get("allowed_domains") or []), "required": "yes" if m.get("required") else "",
            "xero_client_id": x.get("client_id", ""), "xero_port": x.get("port", XERO_PORT)}
    if not form["allowed_domains"] and (session.get("ms_user") or {}).get("email"):
        form["allowed_domains"] = session["ms_user"]["email"].rsplit("@", 1)[1]
    return _firm_page(f, form, tested=session.get("ms_tested") or {})


def _firm_page(f: dict, form, **extra):
    return render_template("firm.html", f=f, form=form, xero_scopes=XERO_SCOPES, xero_port=XERO_PORT,
                           claude=claude_desktop.status(), claude_code=claude_desktop.claude_code_command(), **extra)


@bp.route("/signin/test")
def signin_start_get():
    """Firm settings 'Test sign-in': start a sign-in with the details not saved yet."""
    pending = session.get("ms_test")
    if not pending:
        return redirect(url_for("ui.firm_settings"))
    return render_template("signin.html", s=None, configured=True, next=url_for("ui.firm_settings"), testing=True)


@bp.route("/desktop-icon", methods=["POST"])
def desktop_icon():
    kind = request.form.get("kind") or "app"
    if kind not in shortcut.ICONS:
        abort(400)
    try:
        path = (current_app.config.get("SHORTCUT") or shortcut.create)(kind)
    except shortcut.ShortcutError as e:
        flash(str(e), "error")
    else:
        st = settings()
        if kind not in st.icons_made:
            st.icons_made.append(kind)
            st.save()
        flash(f"Icon put on the desktop: {path}", "ok")
    return back(url_for("ui.firm_settings"))


# ---------------------------------------------------------------------------------------- #
# connect to Xero
# ---------------------------------------------------------------------------------------- #
def _xero() -> XeroSession | None:
    s = _current("xero")
    return s if isinstance(s, XeroSession) else None


def _existing_by_tenant() -> dict[str, object]:
    out = {}
    for c in store().list_clients():
        tid = (c.source or {}).get("xero_organisation_id")
        if tid:
            out[tid] = c
    return out


@bp.route("/xero")
def xero_home():
    s = _xero()
    return render_template("xero.html", conf=xero_config(), s=s, existing=_existing_by_tenant(),
                           redirect_uri=xero_config().redirect_uri if xero_config() else
                           f"http://localhost:{XERO_PORT}/callback", scopes=XERO_SCOPES)


@bp.route("/xero/connect", methods=["POST"])
def xero_connect():
    conf = xero_config()
    if conf is None:
        flash("Set up the firm's Xero app first (Settings > Firm settings).", "error")
        return redirect(url_for("ui.xero_home"))
    try:
        s = XeroSession(conf)
        s.listen()
    except SignInError as e:
        flash(str(e), "error")
        return redirect(url_for("ui.xero_home"))
    _remember_signin("xero", s)
    open_in_browser(s.authorize_url())
    return redirect(url_for("ui.xero_home"))


@bp.route("/xero/status")
def xero_status():
    s = _xero()
    if request.args.get("go") == "1":
        if s is not None and s.status == "error":
            flash(s.error, "error")
        return redirect(url_for("ui.xero_home"))
    if s is None:
        return jsonify({"status": "none"})
    return jsonify({"status": s.status, "error": s.error, "organisations": len(s.tenants)})


@bp.route("/xero/cancel", methods=["POST"])
def xero_cancel():
    s = _xero()
    if s is not None:
        s.cancel()
        cfg("SIGNINS").pop(s.id, None)
    session.pop("xero_id", None)
    return redirect(url_for("ui.xero_home"))


def _signed_in_xero() -> XeroSession:
    s = _xero()
    if s is None or s.status != "signed_in":
        raise SignInError("connect to Xero first")
    return s


@bp.route("/xero/org/<tenant_id>", methods=["GET", "POST"])
def xero_org(tenant_id):
    try:
        s = _signed_in_xero()
        t = s.tenant(tenant_id)
        org_json = s.organisation(tenant_id)
    except SignInError as e:
        flash(str(e), "error")
        return redirect(url_for("ui.xero_home"))
    fields = xero.client_fields_from_org(org_json) if (org_json.get("Organisations") or [None])[0] else {}
    ends = year_ends(fields.get("fy_end_month", 6), fields.get("fy_end_day", 30), count=5)
    existing = _existing_by_tenant().get(tenant_id)
    if request.method == "GET":
        form = {"name": fields.get("name") or t.get("tenantName", ""), "type": fields.get("type") or "company",
                "legal_name": fields.get("legal_name", ""), "abn": fields.get("abn", "")}
        return render_template("xero_org.html", t=t, org=(org_json.get("Organisations") or [{}])[0], fields=fields,
                               ends=ends, picked={e.isoformat() for e in ends[-2:]}, form=form, existing=existing)
    f = request.form
    picked = sorted(e for e in ends if e.isoformat() in f.getlist("years"))
    errors = []
    if not picked:
        errors.append("Tick at least one year end to bring across.")
    if existing and f.get("again") != "yes":
        errors.append(f"{t.get('tenantName')} is already in the ledger as {existing.name} - tick "
                      f"'bring it across again as a new client' to go ahead.")
    etype = f.get("type") or ""
    if etype not in ENTITY_TYPES:
        errors.append("Choose the entity type.")
    if errors:
        for e in errors:
            flash(e, "error")
        return render_template("xero_org.html", t=t, org=(org_json.get("Organisations") or [{}])[0], fields=fields,
                               ends=ends, picked={e.isoformat() for e in picked}, form=f, existing=existing), 400
    try:
        accounts_json = s.accounts(tenant_id)
        tbs, skipped = {}, []
        for end in picked:
            tb = s.trial_balance(tenant_id, end)
            fy = end.year            # a year end falls in the financial year it ends (FY2026 ends in 2026)
            if has_balances(tb):
                tbs[fy] = tb
            else:
                skipped.append(end)
        if not tbs:
            raise SignInError("Xero has no balances at the chosen year ends")
        overrides = {"name": (f.get("name") or "").strip(), "type": etype, "legal_name": (f.get("legal_name") or "").strip(),
                     "abn": xero.format_abn(f.get("abn") or ""),
                     "directors": [x.strip() for x in (f.get("directors") or "").splitlines() if x.strip()]}
        work = work_dir() / f"xero-{secrets.token_hex(4)}"
        work.mkdir(parents=True, exist_ok=True)
        try:
            client, results = xero.migrate_from_api(store(), org_json, accounts_json, tbs, work, overrides)
        finally:
            shutil.rmtree(work, ignore_errors=True)
    except (SignInError, LedgerError, StoreError, ValueError) as e:
        flash(f"Nothing was brought across: {e}", "error")
        return redirect(url_for("ui.xero_org", tenant_id=tenant_id))
    store().audit(client.slug, "xero.imported", {"organisation": t.get("tenantName"), "tenant_id": tenant_id,
                                                 "years": sorted(tbs), "xero_user": s.user.get("email", "")})
    disconnected = False
    if f.get("disconnect") == "yes":
        try:
            s.disconnect(tenant_id)
            disconnected = True
        except SignInError as e:
            flash(f"Brought across, but could not disconnect it from Xero: {e}", "warn")
    summary = []
    for r in results:
        plan = r["plan"]
        summary.append({"fy": r["fy"], "journals": r["result"]["journals"], "created": r["result"]["created_accounts"],
                        "warnings": plan.warnings,
                        "review": [{"name": pl.source_name, "account": pl.account, "report_code": pl.report_code,
                                    "reason": pl.reason} for pl in plan.lines if pl.review and pl.action != "skip"]})
    rid = remember({"years": summary, "skipped": [f"{d:%d/%m/%Y}" for d in skipped], "disconnected": disconnected,
                    "organisation": t.get("tenantName")})
    flash(f"{client.name} brought across from Xero: {len(results)} year(s)"
          + (" - and disconnected from Xero." if disconnected else "."), "ok")
    return redirect(url_for("ui.migrated", slug=client.slug, r=rid))


@bp.route("/xero/org/<tenant_id>/disconnect", methods=["POST"])
def xero_disconnect(tenant_id):
    try:
        s = _signed_in_xero()
        name = s.tenant(tenant_id).get("tenantName", "")
        s.disconnect(tenant_id)
        flash(f"{name} disconnected from Fortis Ledger's Xero app.", "ok")
    except SignInError as e:
        flash(str(e), "error")
    return redirect(url_for("ui.xero_home"))
