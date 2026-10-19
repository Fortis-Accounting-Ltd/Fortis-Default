"""The team: sharing the data folder with colleagues (where it is, moving it into SharePoint, the invite email), who may
open which client, and the live notes that tell an open screen when someone else has changed something."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
from pathlib import Path
from urllib.parse import quote

from flask import abort, current_app, flash, g, jsonify, redirect, render_template, request, session, url_for

from .. import __version__
from ..model import now_iso
from ..store import MARKER, FolderStore, StoreError
from ..team import JOIN, ROLES, norm
from . import release_locks
from .views import (admin_names, back, bp, can_open, firm, is_admin, managing, me, open_in_browser, settings,
                    signin_required, store, team)


# ---- where the data folder is ---------------------------------------------------------------------- #
def _under(p: Path, root: Path) -> bool:
    try:
        p.resolve().relative_to(root.resolve())
        return True
    except (ValueError, OSError):
        return False


def folder_status(folder: str) -> dict:
    """Whether colleagues can share the data folder: a SharePoint library OneDrive syncs to this PC (under
    C:\\Users\\<you>\\<organisation>\\<site> - <library>), a folder in the person's own OneDrive, or only this PC."""
    p = Path(folder)
    business = os.environ.get("OneDriveCommercial")
    org = Path.home() / Path(business).name[len("OneDrive - "):] \
        if business and Path(business).name.startswith("OneDrive - ") else None
    where = {"org": org.name, "synced": str(org)} if org else {}       # where OneDrive puts synced SharePoint libraries
    if org and _under(p, org):
        rel = p.resolve().relative_to(org.resolve()).parts
        return {"kind": "sharepoint", "library": rel[0] if rel else org.name, **where}
    if any(d and _under(p, Path(d)) for d in (business, os.environ.get("OneDrive"))):
        return {"kind": "onedrive", **where}
    return {"kind": "local", **where}


def invite(status: dict) -> dict:
    """The email that tells a colleague how to start (subject, body, and a mailto: link)."""
    where = (f"the {status['library']} library in SharePoint" if status.get("kind") == "sharepoint"
             else "the firm's Fortis Ledger SharePoint library")
    body = (f"Hi,\n\nYou can now use Fortis Ledger, the firm's ledger for client year-end work. To start:\n\n"
            f"1. Install it: run Fortis-Ledger-Setup-{__version__}.exe (attached). No administrator rights are needed.\n"
            f"2. Sync the client files to your PC: open {where} and click Sync. OneDrive keeps them up to date.\n"
            f"3. Open Fortis Ledger from the desktop icon. It finds the synced folder by itself - choose it, and "
            f"sign in with your Fortis email if it asks.\n\n"
            f"Updates come as a small file by email or are offered inside Fortis Ledger - open them to install.\n")
    subject = "Fortis Ledger - getting started"
    return {"subject": subject, "body": body, "mailto": f"mailto:?subject={quote(subject)}&body={quote(body)}"}


# ---- the team page --------------------------------------------------------------------------------- #
def log_text(entry: dict, t) -> str:
    """One line of the firm's change log, in words."""
    d = entry.get("detail") or {}
    who = t.name(d.get("email") or "") if d.get("email") else ""
    people = ", ".join(t.name(e) for e in d.get("people") or []) or "the admins only"
    return {
        "team.managed": "started managing access - new people " + JOIN.get(d.get("join"), ""),
        "team.unmanaged": "stopped managing access - everyone can open every client",
        "team.join": "new people now " + JOIN.get(d.get("join"), ""),
        "team.person": f"{who}: {ROLES.get(d.get('role'), d.get('role'))}"
                       + (f" (was {ROLES.get(d.get('was'), d.get('was'))})" if d.get("was") else " (added)"),
        "team.joined": f"{who} joined as staff at their first sign-in",
        "team.removed": f"removed {who}",
        "team.client_limited": f"{d.get('name') or d.get('client')} limited to {people}",
        "team.client_open": f"{d.get('name') or d.get('client')} opened to everyone in the team",
        "update.shared": f"shared Fortis Ledger {d.get('version')} with everyone",
        "data.moved": f"moved the data folder to {d.get('to')}",
    }.get(entry.get("action"), str(entry.get("action") or ""))


@bp.route("/team", methods=["GET", "POST"])
def team_page():
    t = team()
    if request.method == "POST":
        if not is_admin():
            abort(403, f"Only an admin can change the team - ask {admin_names()}.")
        op = request.form.get("op")
        who = me()
        try:
            if op == "on":
                if not signin_required() or not who:
                    flash("Make office sign-in required first (Firm settings > Sign in with office email), and sign in - "
                          "Fortis Ledger needs to know who everyone is to manage access.", "error")
                    return redirect(url_for("ui.team_page"))
                t.turn_on(who, request.form.get("name") or "", now_iso())
                t.join = request.form.get("join") if request.form.get("join") in JOIN else "auto"
                store().save_team(t.to_dict(), "team.managed", {"admin": who, "join": t.join})
                flash("Access is now managed. You are an admin: add your colleagues below, and limit any client "
                      "to the people who work on it from its Client details page.", "ok")
            elif op == "off":
                t.managed = False
                store().save_team(t.to_dict(), "team.unmanaged", {})
                flash("Access is no longer managed: everyone who uses the data folder can open every client.", "warn")
            elif op == "join" and request.form.get("join") in JOIN:
                t.join = request.form["join"]
                store().save_team(t.to_dict(), "team.join", {"join": t.join})
                flash(f"New people now {JOIN[t.join]}.", "ok")
            elif op == "person":
                email, role = norm(request.form.get("email")), request.form.get("role") or ""
                was = t.role(email)
                t.set_person(email, request.form.get("name") or "", role, by=who, at=now_iso())
                store().save_team(t.to_dict(), "team.person", {"email": email, "role": role, "was": was})
                flash(f"{t.name(email)} is now {ROLES[role].lower()}." if was else
                      f"{t.name(email)} is added as {ROLES[role].lower()}.", "ok")
            elif op == "remove":
                email = norm(request.form.get("email"))
                name = t.name(email)
                t.remove_person(email)
                store().save_team(t.to_dict(), "team.removed", {"email": email})
                flash(f"{name} is removed" + (" - they can join again when they sign in." if t.join == "auto" else "."), "ok")
            else:
                abort(400)
        except ValueError as e:
            flash(str(e), "error")
        return redirect(url_for("ui.team_page"))
    clients = store().list_clients()
    slugs = [c.slug for c in clients]
    people = sorted(t.people.items(), key=lambda kv: ((kv[1].get("name") or kv[0]).lower()))
    limited = [{"c": c, "people": [t.name(e) for e in t.limited_to(c.slug)]} for c in clients if t.limited_to(c.slug) is not None]
    status = folder_status(settings().data_folder)
    log = [{**r, "text": log_text(r, t), "who": t.name(r.get("user") or "")} for r in store().read_firm_log(30)]
    return render_template("team.html", t=t, people=people, summary={e: t.summary(e, slugs) for e, _ in people},
                           limited=limited, roles=ROLES, joins=JOIN, me=me(), signin=signin_required(), status=status,
                           invite=invite(status), log=log, admin=is_admin(), managing=managing(),
                           my_name=(session.get("ms_user") or {}).get("name") or settings().user_name or "",
                           domain=((firm().get("microsoft") or {}).get("allowed_domains") or [""])[0])


@bp.route("/team/invite", methods=["POST"])
def team_invite():
    """Open the invite in the PC's email program (a mailto: link, which the desktop window cannot open itself)."""
    open_in_browser(invite(folder_status(settings().data_folder))["mailto"])
    flash("Your email program has the invite ready: add the colleague's address, attach the installer, and send it.", "ok")
    return redirect(url_for("ui.team_page"))


@bp.route("/team/access/<client>", methods=["POST"])
def team_access(client):
    """Who may open a client: everyone in the team, or only the people ticked (admins can always open it)."""
    t = team()
    try:
        c = store().load_client(client)
    except StoreError:
        abort(404)
    if not t.managed:
        abort(400)
    if request.form.get("mode") == "named":
        chosen = [norm(e) for e in request.form.getlist("people") if t.role(e) == "staff"]
        t.limit_client(client, chosen)
        store().save_team(t.to_dict(), "team.client_limited", {"client": client, "name": c.name, "people": chosen})
        flash(f"{c.name} is now open only to " + (", ".join(t.name(e) for e in chosen) + " and the admins." if chosen
                                                 else "the admins."), "ok")
    else:
        t.limit_client(client, None)
        store().save_team(t.to_dict(), "team.client_open", {"client": client, "name": c.name})
        flash(f"Everyone in the team can open {c.name}.", "ok")
    return back(url_for("ui.client_details", slug=client))


@bp.app_context_processor
def _access_globals():
    """The Access card on Client details (admins, when the firm manages access)."""
    if request.endpoint != "ui.client_details" or not g.get("slug"):
        return {}
    try:
        if not managing() or not is_admin():
            return {}
        t = team()
        limited = t.limited_to(g.slug)
        staff = [{"email": e, "name": p.get("name") or e, "checked": limited is not None and e in limited}
                 for e, p in sorted(t.people.items(), key=lambda kv: (kv[1].get("name") or kv[0]).lower())
                 if p.get("role") == "staff"]
        return {"access": {"limited": limited is not None, "staff": staff, "admins": [t.name(e) for e in t.admins()]}}
    except StoreError:
        return {}


# ---- moving the data folder into SharePoint -------------------------------------------------------- #
@bp.route("/team/move", methods=["POST"])
def team_move():
    """Copy the data folder into a SharePoint library synced to this PC and switch to it (the old one is left)."""
    src = Path(settings().data_folder)
    dest = Path((request.form.get("dest") or "").strip().strip('"')).expanduser()
    if not str(dest) or not dest.is_dir():
        flash("Choose the SharePoint folder on this PC (OneDrive's sync of the library) - it must already exist.", "error")
        return redirect(url_for("ui.team_page"))
    if _under(dest, src):
        flash("Choose a folder outside the current data folder.", "error")
        return redirect(url_for("ui.team_page"))
    if (dest / MARKER).exists():
        flash("That folder is already a Fortis Ledger data folder - to use it, choose it in Settings.", "error")
        return redirect(url_for("ui.team_page"))
    target = dest if not any(x for x in dest.iterdir() if not x.name.startswith(".")) else dest / "Fortis Ledger"
    if target.exists() and target != dest:
        flash(f"{target} already exists - choose an empty folder.", "error")
        return redirect(url_for("ui.team_page"))
    release_locks(current_app)
    before = len(store().list_clients())
    try:
        shutil.copytree(src, target, dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns("lock.json", ".*.tmp", "~$*"))
    except OSError as e:
        flash(f"The data folder could not be copied: {e}", "error")
        return redirect(url_for("ui.team_page"))
    moved = FolderStore(target, user=store().user)
    if not moved.is_initialised() or len(moved.list_clients()) != before:
        flash(f"The copy in {target} does not match the data folder - nothing was switched. Check it, or try again.", "error")
        return redirect(url_for("ui.team_page"))
    s = settings()
    s.data_folder = str(target)
    s.save()
    moved.log_firm("data.moved", {"from": str(src), "to": str(target), "clients": before})
    g.pop("store", None)
    g.pop("team", None)
    g.pop("firm", None)
    flash(f"The data folder is now {target}: {before} client(s) copied. OneDrive uploads it to SharePoint - give it a "
          f"few minutes before colleagues sync it. The old folder ({src}) is left as it was; delete it once you have "
          f"checked everything is there.", "ok")
    return redirect(url_for("ui.team_page"))


# ---- live notes: has someone else changed something since this screen was shown? -------------------- #
def _who(entry: dict) -> str:
    who = team().name(entry.get("user") or "") or "Someone"
    return f"Claude (for {who})" if entry.get("via") else who


def audit_mark(entry: dict | None) -> str:
    """Marks the newest change-log entry when a screen is shown: later entries are the news (PCs' clocks may differ)."""
    return hashlib.sha256(json.dumps(entry, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:16] if entry else "-"


@bp.route("/pulse/<client>")
def pulse_client(client):
    if not can_open(client):
        abort(403)
    st = store()
    try:
        st.client_dir(client)
    except StoreError:
        abort(404)
    since = request.args.get("since") or "-"
    news = []
    for e in st.audit_tail(client):                  # newest first, back to the one the screen was shown with
        if audit_mark(e) == since:
            break
        news.append(e)
    else:
        if since != "-":                             # the log was replaced (OneDrive): just say it changed
            news = news[:1]
    news = [e for e in news if e.get("via") or (e.get("user"), e.get("machine")) != (st.user, st.machine)]
    from .views import audit_text
    if news:
        e = news[0]
        what = f"{str(e.get('action', '')).replace('.', ' ').replace('_', ' ')} {audit_text(e)}".strip()
        more = f" (and {len(news) - 1} more change{'s' if len(news) > 2 else ''})" if len(news) > 1 else ""
        return jsonify(message=f"{_who(e)} has just changed this client: {what}{more}. Refresh to see it.")
    lock = st.lock_info(client)
    if request.args.get("ro") and (not lock or lock["stale"] or lock["mine"]):
        return jsonify(message="The person who was working on this client has finished - refresh to make changes.")
    return jsonify(message="")


def clients_signature() -> str:
    """Changes when a client is added or removed, someone starts or stops working on one, or one is changed."""
    st = store()
    parts = []
    for c in st.list_clients():
        if not can_open(c.slug):
            continue
        lock = st.lock_info(c.slug)
        last = st.last_audit(c.slug) or {}
        parts.append(f"{c.slug}|{c.name}|{lock.get('user') if lock and not lock['stale'] else ''}|{last.get('at', '')}")
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()[:16]


@bp.route("/pulse")
def pulse_clients():
    if request.args.get("since") and request.args["since"] != clients_signature():
        return jsonify(message="The client list has changed (a colleague added a client, or started or stopped work "
                               "on one). Refresh to see it.")
    return jsonify(message="")


@bp.app_context_processor
def _pulse_globals():
    """Screens that watch for colleagues' changes: a client's pages, and the client list."""
    try:
        if g.get("ledger") is not None and g.get("slug"):
            return {"pulse": {"url": url_for("ui.pulse_client", client=g.slug), "since": audit_mark(store().last_audit(g.slug)),
                              "ro": bool(g.get("readonly")), "every": 10}}
        if request.endpoint == "ui.clients" and settings().configured:      # every client's files: ask less often
            return {"pulse": {"url": url_for("ui.pulse_clients"), "since": clients_signature(), "ro": False, "every": 30}}
    except StoreError:
        pass
    return {}
