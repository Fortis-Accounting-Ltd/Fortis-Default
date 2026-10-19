"""
The ForWork web app (Flask).  Every page and API call needs a signed-in session, except the sign-in
pages themselves and static files.  Every change (POST, PATCH, DELETE) must carry the session's CSRF
token, pages allow no inline script and cannot be framed, and cookies are HttpOnly, SameSite=Lax and,
over HTTPS, Secure with the __Host- prefix.
"""
from __future__ import annotations

import datetime as dt
import gzip
import hmac
import logging

from flask import Flask, abort, g, jsonify, redirect, render_template, request, url_for

from .. import APP_NAME, __version__
from ..auth import load_session
from ..config import Config
from ..db import Database
from ..model import date_text
from ..service import Service, WorkError, initials

PUBLIC_ENDPOINTS = {"static", "ui.signin", "ui.auth_start", "ui.auth_callback", "ui.dev_signin", "ui.health"}
LOOPBACK = {"127.0.0.1", "::1", "localhost"}


def cookie_name(cfg: Config) -> str:
    return "__Host-forwork" if cfg.secure_cookies else "forwork_session"


def create_app(cfg: Config, db: Database | None = None, start_background: bool = False) -> Flask:
    app = Flask(__name__)
    db = db or Database(cfg.db_path, journal_mode=cfg.journal_mode)
    svc = Service(db)
    app.config.update(
        SECRET_KEY=cfg.secret_key,
        SESSION_COOKIE_NAME="forwork_signin",          # only holds a sign-in in progress (state, nonce, PKCE verifier)
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=cfg.secure_cookies,
        PERMANENT_SESSION_LIFETIME=dt.timedelta(minutes=20),
        MAX_CONTENT_LENGTH=60 * 1024 * 1024,
        FW_CONFIG=cfg,
        FW_DB=db,
        FW_SVC=svc,
        FW_JOBS={},                                     # background jobs (imports) by id - memory only
        FW_BACKUP=None,
        JSON_SORT_KEYS=False,
    )
    if cfg.trust_proxy:
        from werkzeug.middleware.proxy_fix import ProxyFix
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

    @app.before_request
    def _guard():
        g.user = None
        g.session = None
        g.csrf = ""
        token = request.cookies.get(cookie_name(cfg))
        if token:
            found = load_session(db, token)
            if found:
                g.session, g.user = found
                g.csrf = g.session["csrf"]
        if request.endpoint in PUBLIC_ENDPOINTS:
            return None
        if g.user is None:
            if request.path.startswith("/api/") or request.method != "GET":
                return jsonify(error="You are signed out - reload the page to sign in again."), 401
            nxt = request.full_path if request.full_path != "/?" else "/"
            return redirect(url_for("ui.signin", next=nxt))
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            sent = request.headers.get("X-CSRF-Token") or request.form.get("_csrf") or ""
            if not sent or not hmac.compare_digest(sent, g.csrf):
                if request.path.startswith("/api/"):
                    return jsonify(error="This page has expired - reload it and try again."), 400
                abort(400, "This form has expired - reload the page and try again.")
        return None

    @app.after_request
    def _headers(resp):
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["Referrer-Policy"] = "same-origin"
        resp.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        if "Content-Security-Policy" not in resp.headers:
            resp.headers["Content-Security-Policy"] = (
                "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; connect-src 'self'; "
                "object-src 'none'; frame-ancestors 'none'; form-action 'self' https://login.microsoftonline.com; "
                "base-uri 'none'")
        if cfg.secure_cookies:
            resp.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        if resp.mimetype in ("text/html", "application/json"):
            resp.headers["Cache-Control"] = "no-store"
        # compress large JSON (a board can be several hundred KB)
        if (resp.mimetype == "application/json" and not resp.direct_passthrough and resp.status_code == 200
                and "gzip" in request.headers.get("Accept-Encoding", "") and "Content-Encoding" not in resp.headers):
            body = resp.get_data()
            if len(body) > 20000:
                resp.set_data(gzip.compress(body, 5))
                resp.headers["Content-Encoding"] = "gzip"
                resp.headers["Vary"] = "Accept-Encoding"
        return resp

    @app.errorhandler(WorkError)
    def _work_error(e):
        if request.path.startswith("/api/"):
            return jsonify(error=str(e)), e.status
        return render_template("error.html", code=e.status, message=str(e)), e.status

    for code in (400, 403, 404, 405, 413, 500):
        app.register_error_handler(code, _error_page(code))

    @app.template_filter("dmy")
    def _dmy(v):
        return date_text((v or "")[:10]) if v else ""

    @app.template_filter("when")
    def _when(v):
        return date_text(v) if v else ""

    @app.template_filter("initials")
    def _initials(v):
        return initials(v or "")

    @app.context_processor
    def _globals():
        u = g.get("user")
        return {"APP_NAME": APP_NAME, "VERSION": __version__, "me": u, "csrf": g.get("csrf", ""),
                "unread": svc.unread_count(u) if u else 0, "is_admin": bool(u and u["role"] == "admin"),
                "dev_login": cfg.dev_login}

    from .pages import bp as pages_bp
    from .api import bp as api_bp
    from .admin import bp as admin_bp
    app.register_blueprint(pages_bp)
    app.register_blueprint(api_bp)
    app.register_blueprint(admin_bp)

    if start_background:
        _start_backups(app)
    return app


def _start_backups(app: Flask) -> None:
    try:
        from ..backup import BackupManager
        from ..export import export_all_json
    except ImportError:
        logging.warning("backups are not available in this copy of ForWork")
        return
    svc = app.config["FW_SVC"]
    mgr = BackupManager(app.config["FW_DB"], app.config["FW_CONFIG"], extra_exports=lambda d: export_all_json(svc, d))
    app.config["FW_BACKUP"] = mgr
    mgr.start_scheduler()


def _error_page(code: int):
    def handler(e):
        message = getattr(e, "description", "") or "Something went wrong."
        if code == 404:
            message = "That page does not exist (it may have been moved or deleted)."
        if code == 413:
            message = "That file is too large (the limit is 60 MB)."
        if code == 500:
            message = "Something went wrong on the server. The details are in its log."
        if request.path.startswith("/api/"):
            return jsonify(error=message), code
        return render_template("error.html", code=code, message=message), code
    return handler
