"""Admin screens: people and roles, firm settings, backups and restore, importing from monday.com, and the bin."""
from __future__ import annotations

import logging
import os
import secrets
import shutil
import tempfile
import threading
from pathlib import Path

from flask import Blueprint, abort, current_app, flash, g, jsonify, redirect, render_template, request, send_file, url_for

from ..auth import end_all_sessions
from ..service import ROLES, WorkError

bp = Blueprint("admin", __name__, url_prefix="/admin")


def svc():
    return current_app.config["FW_SVC"]


def cfg():
    return current_app.config["FW_CONFIG"]


@bp.before_request
def _admins_only():
    if g.user is None or g.user["role"] != "admin":
        abort(403, "Only an admin can open this page.")


def backup_manager():
    mgr = current_app.config.get("FW_BACKUP")
    if mgr is None:
        from ..backup import BackupManager
        from ..export import export_all_json
        s = svc()
        mgr = BackupManager(current_app.config["FW_DB"], cfg(), extra_exports=lambda d: export_all_json(s, d))
        current_app.config["FW_BACKUP"] = mgr
    return mgr


# ---- people -------------------------------------------------------------------------------------------- #
@bp.get("/")
def index():
    return redirect(url_for("admin.people"))


@bp.get("/people")
def people():
    s = svc()
    users = s.users(include_inactive=True)
    sessions = {r[0]: r[1] for r in s.db.q("SELECT user_id, COUNT(*) FROM sessions GROUP BY user_id")}
    return render_template("admin_people.html", users=users, roles=ROLES, sessions=sessions, teams=s.teams())


@bp.post("/people")
def people_add():
    try:
        svc().create_user(g.user, request.form.get("email", ""), request.form.get("name", ""), request.form.get("role", "member"))
        flash("Added. They can sign in with their Microsoft 365 account now.", "ok")
    except WorkError as e:
        flash(str(e), "error")
    return redirect(url_for("admin.people"))


@bp.post("/people/<int:uid>")
def people_change(uid):
    s = svc()
    f = request.form
    try:
        if f.get("op") == "signout":
            n = end_all_sessions(s.db, uid)
            flash(f"Signed out of {n} browser{'s' if n != 1 else ''}.", "ok")
        else:
            s.update_user(g.user, uid, name=f.get("name") or None, role=f.get("role") or None,
                          active=None if f.get("active") is None else f.get("active") == "1")
            flash("Saved.", "ok")
    except WorkError as e:
        flash(str(e), "error")
    return redirect(url_for("admin.people"))


# ---- firm settings ---------------------------------------------------------------------------------------- #
@bp.get("/settings")
def settings():
    c = cfg()
    return render_template("admin_settings.html", cfg=c, auto_add=current_app.config["FW_DB"].get_setting("auto_add_people", True),
                           counts={k: svc().db.scalar(q, default=0) for k, q in (
                               ("boards", "SELECT COUNT(*) FROM boards WHERE state != 'deleted'"),
                               ("items", "SELECT COUNT(*) FROM items WHERE state = 'active'"),
                               ("updates", "SELECT COUNT(*) FROM updates WHERE state = 'active'"),
                               ("people", "SELECT COUNT(*) FROM users WHERE active = 1"))},
                           db_size=os.path.getsize(c.db_path) if c.db_path.exists() else 0)


@bp.post("/settings")
def settings_save():
    db = current_app.config["FW_DB"]
    db.set_setting("auto_add_people", request.form.get("auto_add") == "1")
    if request.form.get("op") == "signout_all":
        n = end_all_sessions(db)
        flash(f"Everyone has been signed out ({n} sessions).", "ok")
        return redirect(url_for("ui.signin"))
    flash("Saved.", "ok")
    return redirect(url_for("admin.settings"))


# ---- backups ------------------------------------------------------------------------------------------------ #
@bp.get("/backups")
def backups():
    from ..backup import load_settings
    mgr = backup_manager()
    try:
        listing = mgr.list_backups()
    except Exception as e:                                   # OneDrive unreachable: still show the page
        listing = []
        flash(f"Could not list the OneDrive backups: {e}", "warn")
    return render_template("admin_backups.html", settings=load_settings(current_app.config["FW_DB"]), status=mgr.status(),
                           backups=listing, running=mgr.running, graph_ready=bool(cfg().ms_tenant_id and cfg().ms_client_id
                                                                                   and cfg().graph_client_secret))


@bp.post("/backups/settings")
def backups_settings():
    from ..backup import BackupError, save_settings
    f = request.form
    target = {"kind": f.get("graph_kind") or "user"}
    if target["kind"] == "user":
        target["user"] = f.get("graph_user", "").strip()
    elif target["kind"] == "site":
        target["site_url"] = f.get("graph_site", "").strip()
        target["library"] = f.get("graph_library", "").strip() or "Documents"
    else:
        target["drive_id"] = f.get("graph_drive", "").strip()
    raw = {"folder": f.get("folder", "").strip(),
           "graph": {"enabled": f.get("graph_enabled") == "1", "target": target, "folder": f.get("graph_folder", "").strip()},
           "interval_minutes": f.get("interval_minutes"),
           "keep": {"all_hours": f.get("keep_hours"), "daily_days": f.get("keep_days"), "monthly_months": f.get("keep_months")}}
    try:
        save_settings(current_app.config["FW_DB"], raw)
        flash("Backup settings saved. Use 'Back up now' to check they work.", "ok")
    except BackupError as e:
        flash(str(e), "error")
    return redirect(url_for("admin.backups"))


@bp.post("/backups/run")
def backups_run():
    mgr = backup_manager()
    if mgr.running:
        flash("A backup is already running.", "warn")
    else:
        threading.Thread(target=_safe_backup, args=(mgr,), daemon=True, name="backup-now").start()
        flash("Backing up now - this page shows the result when it has finished (reload in a minute).", "ok")
    return redirect(url_for("admin.backups"))


def _safe_backup(mgr):
    try:
        mgr.run(reason="manual")                         # the outcome is recorded in the backup status
    except Exception:
        logging.exception("backup failed")


@bp.get("/backups/download/<path:name>")
def backups_download(name):
    mgr = backup_manager()
    if "/" in name or "\\" in name or name.startswith("."):
        abort(404)
    path = cfg().backups_dir / name
    if not path.is_file():
        try:
            path = mgr.fetch(name)
        except Exception:
            abort(404)
    return send_file(path, as_attachment=True, download_name=name)


@bp.post("/backups/restore")
def backups_restore():
    mgr = backup_manager()
    name = request.form.get("name", "")
    if request.form.get("confirm", "").strip().upper() != "RESTORE":
        flash("Type RESTORE to confirm - restoring replaces everything in ForWork with the backup.", "error")
        return redirect(url_for("admin.backups"))
    try:
        path = mgr.fetch(name)
        pre = mgr.restore(path)
    except Exception as e:
        flash(f"The restore did not happen: {e}", "error")
        return redirect(url_for("admin.backups"))
    flash(f"Restored from {name}. The data as it was just before is kept in {Path(pre).name}. "
          f"Everyone may need to sign in again.", "ok")
    return redirect(url_for("ui.signin"))


# ---- import from monday.com ------------------------------------------------------------------------------------ #
def _job(job_id: str) -> dict:
    return current_app.config["FW_JOBS"].setdefault(job_id, {})


@bp.get("/import")
def import_page():
    s = svc()
    imported = s.db.q("SELECT name, state, monday_id FROM boards WHERE monday_id IS NOT NULL ORDER BY name")
    return render_template("admin_import.html", imported=imported, token_set=bool(cfg().monday_token),
                           jobs=current_app.config["FW_JOBS"])


@bp.post("/import")
def import_start():
    """Pull from monday.com with an API token (typed here or in FORWORK_MONDAY_TOKEN), or load an uploaded export .zip."""
    app = current_app._get_current_object()
    replace = request.form.get("replace") == "1"
    token = (request.form.get("token") or "").strip() or cfg().monday_token
    upload = request.files.get("bundle")
    work = Path(tempfile.mkdtemp(prefix="forwork-import-"))
    job_id = secrets.token_hex(6)
    job = _job(job_id)
    job.update(status="running", message="Starting", done=0, total=0, report=None, error=None, by=g.user["name"])
    actor = g.user
    if upload and upload.filename:
        path = work / "export.zip"
        upload.save(path)
        source = ("file", path)
    elif token:
        source = ("token", token)
    else:
        shutil.rmtree(work, ignore_errors=True)
        flash("Paste a monday.com API token or choose an export .zip file.", "error")
        return redirect(url_for("admin.import_page"))
    threading.Thread(target=_run_import, args=(app, job, source, work, replace, actor), daemon=True,
                     name=f"import-{job_id}").start()
    return redirect(url_for("admin.import_page", job=job_id))


def _run_import(app, job, source, work: Path, replace: bool, actor) -> None:
    from ..monday_import import import_bundle

    def progress(msg, done, total):
        job.update(message=msg, done=done, total=total)

    with app.app_context():
        try:
            if source[0] == "token":
                from ..monday_pull import pull
                job["message"] = "Reading monday.com"
                pull(source[1], work / "export", progress=progress)
                path = work / "export"
            else:
                path = source[1]
            job.update(message="Loading into ForWork", done=0, total=0)
            report = import_bundle(app.config["FW_SVC"], path, actor=actor, replace=replace, progress=progress)
            job.update(status="done", message="Finished", report=report)
        except Exception as e:                                 # shown on the page as it is
            job.update(status="error", error=str(e) or e.__class__.__name__)
            app.logger.exception("import failed")
        finally:
            shutil.rmtree(work, ignore_errors=True)


@bp.get("/import/jobs/<job_id>")
def import_job(job_id):
    job = current_app.config["FW_JOBS"].get(job_id)
    if not job:
        abort(404)
    return jsonify({k: v for k, v in job.items() if k != "token"})


# ---- the bin ----------------------------------------------------------------------------------------------------- #
@bp.get("/bin")
def bin_page():
    s = svc()
    boards = s.db.q("SELECT id, name, updated_at FROM boards WHERE state = 'deleted' ORDER BY updated_at DESC")
    items = s.db.q("SELECT i.id, i.name, i.updated_at, b.name AS board, b.id AS board_id, i.parent_id FROM items i "
                   "JOIN boards b ON b.id = i.board_id WHERE i.state = 'deleted' AND b.state != 'deleted' "
                   "ORDER BY i.updated_at DESC LIMIT 300")
    return render_template("admin_bin.html", boards=boards, items=items)


@bp.post("/bin/restore")
def bin_restore():
    s = svc()
    try:
        if request.form.get("board"):
            s.set_board_state(g.user, int(request.form["board"]), "active")
        elif request.form.get("item"):
            s.set_item_state(g.user, int(request.form["item"]), "active")
        flash("Restored.", "ok")
    except WorkError as e:
        flash(str(e), "error")
    return redirect(url_for("admin.bin_page"))
