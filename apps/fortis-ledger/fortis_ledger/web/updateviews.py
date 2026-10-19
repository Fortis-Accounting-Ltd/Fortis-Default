"""Updates: what this PC runs, installing an update file (opened from an email, chosen here, or shared by an admin in
the data folder), sharing it with everyone who uses the folder, and restarting into it."""
from __future__ import annotations

import logging
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

from flask import abort, current_app, flash, redirect, render_template, request, session, url_for

from .. import updates
from ..updates import EXTENSION, UpdateError
from . import release_locks
from .views import admin_names, bp, cfg, is_admin, open_path, settings, store, work_dir

CHECK_EVERY = 15 * 60          # seconds between looks in the shared folder for an update


def trust():
    return cfg("UPDATE_TRUST")          # None: GitHub's keys and the firm's repository (tests set their own)


def _refresh_shared(app) -> None:
    try:
        folder = app.config["SETTINGS"].data_folder
        found, problems = updates.newest_shared(folder, app.config["UPDATE_TRUST"]) if folder else (None, [])
    except Exception:
        logging.exception("looking for a shared update failed")
        found, problems = None, []
    app.config.update(SHARED_UPDATE=found, SHARED_PROBLEMS=problems, SHARED_CHECKED=time.time(), SHARED_CHECKING=False)


def shared_update(wait: bool = False):
    """The newest update an admin shared in the data folder that this PC can install (checked in the background,
    every so often; at once with wait=True)."""
    app = current_app._get_current_object()
    if wait:
        _refresh_shared(app)
    elif app.config["UPDATE_CHECKS"] and not app.config.get("SHARED_CHECKING") \
            and time.time() - app.config.get("SHARED_CHECKED", 0) > CHECK_EVERY:
        app.config["SHARED_CHECKING"] = True
        threading.Thread(target=_refresh_shared, args=(app,), daemon=True, name="fortis-ledger-updates").start()
    return app.config.get("SHARED_UPDATE")


@bp.app_context_processor
def _update_globals():
    try:
        if not settings().configured or request.endpoint in (None, "static"):
            return {}
        st = updates.state()
        if st["restart"]:
            return {"update_banner": {"restart": st["installed"]["version"]}}
        shared = shared_update()
        if shared is not None and not updates.can_install(shared):
            return {"update_banner": {"available": shared.version}}
    except Exception:
        logging.exception("update banner")
    return {}


def _review_path() -> Path | None:
    """The update file being looked at: chosen on this page, or opened with Fortis Ledger (double-click, email)."""
    name = session.get("update_review")
    if name:
        p = work_dir() / "updates" / name
        if p.is_file():
            return p
    opened = cfg("UPDATE_FILE")
    return Path(opened) if opened and Path(opened).is_file() else None


@bp.route("/updates")
def updates_page():
    review, review_error, path = None, "", _review_path()
    if path:
        try:
            review = updates.read_update(path, trust())
        except UpdateError as e:
            review_error = str(e)
    st = updates.state()
    shared = shared_update(wait=True)
    version = st["installed"].get("version") if st["restart"] else st["running"]
    chosen = path is not None and path.parent == work_dir() / "updates"
    return render_template("updates.html", st=st, review=review, review_error=review_error,
                           review_name="the file you chose" if chosen else (path.name if path else ""),
                           review_why=updates.can_install(review) if review else "",
                           shared=shared, shared_why=updates.can_install(shared) if shared else "",
                           problems=cfg("SHARED_PROBLEMS") or [], admin=is_admin(), admins=admin_names(),
                           saved=updates.saved_file(version or ""), saved_version=version, ext=EXTENSION)


@bp.route("/updates/choose", methods=["POST"])
def updates_choose():
    f = request.files.get("file")
    if not f or not f.filename or not f.filename.lower().endswith(EXTENSION):
        flash(f"Choose a Fortis Ledger update file (its name ends in {EXTENSION}).", "error")
        return redirect(url_for("ui.updates_page"))
    d = work_dir() / "updates"
    d.mkdir(parents=True, exist_ok=True)
    name = uuid.uuid4().hex + EXTENSION
    f.save(d / name)
    session["update_review"] = name
    return redirect(url_for("ui.updates_page"))


@bp.route("/updates/install", methods=["POST"])
def updates_install():
    shared = request.form.get("which") == "shared"
    found = shared_update(wait=True) if shared else None
    path = found.path if found else (None if shared else _review_path())
    if path is None:
        flash("There is no update to install.", "error")
        return redirect(url_for("ui.updates_page"))
    try:
        u = updates.read_update(path, trust())
        updates.install(u, source="the shared folder" if shared else path.name)
    except UpdateError as e:
        flash(f"Not installed: {e}", "error")
        return redirect(url_for("ui.updates_page"))
    msg = f"Fortis Ledger {u.version} is installed. Restart Fortis Ledger to start using it."
    if not shared and request.form.get("share") == "yes":
        if is_admin():
            updates.share(u, settings().data_folder)
            store().log_firm("update.shared", {"version": u.version, "built": u.source})
            msg += " Everyone else who uses the shared folder is offered it too."
        else:
            msg += f" Only an admin can share it with everyone - ask {admin_names()}."
    session.pop("update_review", None)
    current_app.config["UPDATE_FILE"] = None
    flash(msg, "ok")
    return redirect(url_for("ui.updates_page"))


def _saved_update():
    p = updates.saved_file(request.form.get("version") or "")
    if p is None:
        abort(404, "That update file is no longer on this PC.")
    return p


@bp.route("/updates/share", methods=["POST"])
def updates_share():
    if not is_admin():
        abort(403, f"Only an admin can share an update with everyone - ask {admin_names()}.")
    try:
        u = updates.read_update(_saved_update(), trust())
    except UpdateError as e:
        flash(f"Not shared: {e}", "error")
        return redirect(url_for("ui.updates_page"))
    updates.share(u, settings().data_folder)
    store().log_firm("update.shared", {"version": u.version, "built": u.source})
    flash(f"Fortis Ledger {u.version} is in the shared folder: everyone who uses it is offered the update.", "ok")
    return redirect(url_for("ui.updates_page"))


@bp.route("/updates/show", methods=["POST"])
def updates_show():
    p = _saved_update()
    open_path(p, folder=True)
    flash(f"{p.name} is shown in its folder. Attach it to an email: whoever opens it on a PC with Fortis Ledger is "
          f"asked to install it.", "ok")
    return redirect(url_for("ui.updates_page"))


@bp.route("/updates/use-installed", methods=["POST"])
def updates_use_installed():
    updates.use_installed_program()
    flash("Fortis Ledger will start with the version installed with the program. Restart it to switch.", "ok")
    return redirect(url_for("ui.updates_page"))


@bp.route("/updates/restart", methods=["POST"])
def updates_restart():
    if not getattr(sys, "frozen", False):
        flash("Close Fortis Ledger and open it again to start the new version.", "ok")
        return redirect(url_for("ui.updates_page"))
    release_locks(current_app)
    flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    subprocess.Popen([sys.executable], close_fds=True, creationflags=flags)    # noqa: S603 - this program, again
    window, stop = cfg("WINDOW"), cfg("SHUTDOWN")
    if window is not None:
        threading.Timer(0.8, window.destroy).start()
    elif stop:
        threading.Timer(0.8, stop).start()
    return render_template("error.html", code=200, message="Fortis Ledger is restarting with the new version...")
