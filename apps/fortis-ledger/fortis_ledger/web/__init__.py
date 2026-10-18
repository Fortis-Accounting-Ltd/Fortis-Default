"""
The app's screens - a Flask app served on 127.0.0.1 and shown in a desktop window (pywebview) or
the browser.

Only this PC can reach it: the server listens on the loopback address, rejects any other Host
header (DNS rebinding), and answers only a browser that opened it with the start-up token the
launcher passes in.  Every form carries a CSRF token, and the pages allow no inline script.
"""
from __future__ import annotations

import datetime as dt
import hmac
import logging
import secrets
import tempfile
import time
from pathlib import Path
from urllib.parse import urlencode

from flask import Flask, abort, redirect, render_template, request, session

from .. import APP_NAME, __version__
from ..money import fmt
from ..msauth import AUTHORITY
from ..settings import Settings
from ..store import FolderStore
from ..xero_connect import Endpoints

ENTITY_LABELS = {"company": "Company", "trust": "Trust", "partnership": "Partnership", "sole_trader": "Sole trader"}
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}


def release_locks(app: Flask) -> None:
    """
    Hand back every edit lock this copy of the app holds - each as the account that took it (the typed
    name, or the office email when signed in), so others can edit those clients straight away.
    """
    folder = app.config["SETTINGS"].data_folder
    held = app.config["OPEN_CLIENTS"]
    for slug, user in list(held.items()):
        if folder:
            try:
                FolderStore(folder, user=user).release_lock(slug)
            except Exception:
                logging.exception("could not release the edit lock on %s", slug)
        held.pop(slug, None)


def create_app(settings: Settings | None = None, token: str | None = None,
               work_dir: str | Path | None = None) -> Flask:
    app = Flask(__name__)
    app.config.update(
        SECRET_KEY=secrets.token_hex(32),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Strict",
        SESSION_COOKIE_NAME="fortis_ledger",
        MAX_CONTENT_LENGTH=60 * 1024 * 1024,
        SETTINGS=settings or Settings.load(),
        APP_TOKEN=token or secrets.token_urlsafe(24),
        WORK_DIR=Path(work_dir or tempfile.mkdtemp(prefix="fortis-ledger-")),
        OPEN_CLIENTS={},         # clients whose edit lock this process holds -> the account that took it
        RESULTS={},              # results shown after a redirect (report checks, migrations)
        SHUTDOWN=None,           # set by the launcher in browser mode
        DESKTOP=False,           # True inside the pywebview window
        LAST_SEEN=time.time(),   # last request from an open page (browser mode stops when idle)
        SIGNINS={},              # browser sign-ins in progress (Microsoft 365, Xero) - memory only
        MS_AUTHORITY=AUTHORITY,
        XERO_ENDPOINTS=Endpoints(),
        BROWSER=None,            # opens a URL in the PC's browser (webbrowser.open unless set)
        SHORTCUT=None,           # makes a desktop icon (shortcut.create unless set)
    )
    app.config["WORK_DIR"].mkdir(parents=True, exist_ok=True)

    @app.before_request
    def _guard():
        host = (request.host or "").rsplit(":", 1)[0].strip("[]").lower()
        if host not in LOCAL_HOSTS:
            abort(403)
        tok = request.args.get("token")
        if tok is not None:
            if not hmac.compare_digest(tok, app.config["APP_TOKEN"]):
                abort(403)
            session.clear()
            session["auth"] = True
            session["csrf"] = secrets.token_hex(16)
            args = request.args.to_dict(flat=False)
            args.pop("token", None)
            return redirect(request.path + ("?" + urlencode(args, doseq=True) if args else ""))
        if request.endpoint == "static":
            return None
        if not session.get("auth"):
            return render_template("error.html", code=403,
                                   message="Open Fortis Ledger from its desktop shortcut."), 403
        app.config["LAST_SEEN"] = time.time()
        if request.method == "POST":
            sent = request.form.get("_csrf") or request.headers.get("X-CSRF-Token", "")
            if not sent or not hmac.compare_digest(sent, session.get("csrf", "")):
                abort(400, "This form has expired - reload the page and try again.")
        return None

    @app.after_request
    def _headers(resp):
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["Referrer-Policy"] = "same-origin"
        resp.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
            "object-src 'none'; frame-ancestors 'none'; form-action 'self'; base-uri 'none'")
        if resp.mimetype == "text/html":
            resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.template_filter("money")
    def _money(v):
        return fmt(v) if v not in (None, "") else ""

    @app.template_filter("money0")
    def _money0(v):
        return fmt(v, blank_zero=False) if v not in (None, "") else ""

    @app.template_filter("dmy")
    def _dmy(v):
        if not v:
            return ""
        if isinstance(v, str):
            try:
                v = dt.date.fromisoformat(v[:10])
            except ValueError:
                return v
        return f"{v:%d/%m/%Y}"

    @app.template_filter("longdate")
    def _longdate(v):
        return f"{v.day} {v:%B %Y}" if v else ""

    @app.template_filter("when")
    def _when(v):
        """ISO timestamp (UTC) -> local '16 Oct 2026 10:38'."""
        if not v:
            return ""
        try:
            t = dt.datetime.fromisoformat(str(v).replace("Z", "+00:00")).astimezone()
        except ValueError:
            return str(v)
        return f"{t.day} {t:%b %Y %H:%M}"

    @app.template_filter("mtime")
    def _mtime(ts):
        t = dt.datetime.fromtimestamp(float(ts))
        return f"{t.day} {t:%b %Y %H:%M}"

    @app.template_filter("entity")
    def _entity(v):
        return ENTITY_LABELS.get(v, v)

    @app.template_filter("filesize")
    def _filesize(n):
        n = float(n or 0)
        for unit in ("bytes", "KB", "MB"):
            if n < 1024 or unit == "MB":
                return f"{n:.0f} {unit}" if unit == "bytes" else f"{n:.1f} {unit}"
            n /= 1024
        return f"{n:.1f} MB"

    app.jinja_env.globals["csrf_token"] = lambda: session.get("csrf", "")

    @app.context_processor
    def _globals():
        return {"APP_NAME": APP_NAME, "VERSION": __version__,
                "ENTITY_LABELS": ENTITY_LABELS, "settings": app.config["SETTINGS"],
                "desktop": app.config["DESKTOP"], "browser_mode": app.config["SHUTDOWN"] is not None}

    for code in (400, 403, 404, 405, 413, 500):
        app.register_error_handler(code, _error_page(code))

    from .bankviews import bp      # the screens' blueprint (views.py) with the bank screens added to it
    app.register_blueprint(bp)
    return app


def _error_page(code: int):
    def handler(e):
        message = getattr(e, "description", "") or "Something went wrong."
        if code == 413:
            message = "That file is too large (the limit is 60 MB)."
        if code == 500:
            message = "Something went wrong. The details are in the app's log file."
        return render_template("error.html", code=code, message=message), code
    return handler
