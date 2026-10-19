"""
python -m forwork <command>

  serve [--host 0.0.0.0] [--port 8000]      run ForWork (waitress); settings come from FORWORK_* variables
  import-monday <export folder or .zip>      load a monday.com export into ForWork [--replace]
  pull-monday --out <folder> [--zip]         pull everything from monday.com with FORWORK_MONDAY_TOKEN
  backup [list]                              back up now / list backups
  restore <file or name> [--yes]             put ForWork back as it was in a backup
  add-admin <email> [name]                   add (or promote) an admin - for the very first sign-in
  check                                      show the settings ForWork has picked up (no secrets)
"""
from __future__ import annotations

import argparse
import sys

from . import APP_NAME, __version__
from .config import Config
from .db import Database
from .service import Service

LOOPBACK = {"127.0.0.1", "localhost", "::1"}


def _app_parts(data: str | None):
    cfg = Config.load(data)
    db = Database(cfg.db_path, journal_mode=cfg.journal_mode)
    return cfg, db, Service(db)


def cmd_serve(args) -> int:
    cfg = Config.load(args.data)
    if cfg.dev_login and args.host not in LOOPBACK:
        print("FORWORK_DEV_LOGIN lets anyone sign in as anyone, so ForWork only allows it on 127.0.0.1. "
              "Turn it off or use --host 127.0.0.1.", file=sys.stderr)
        return 2
    if not cfg.microsoft_ready and not cfg.dev_login:
        print("Note: Microsoft sign-in is not set up yet (FORWORK_MS_TENANT_ID / CLIENT_ID / CLIENT_SECRET), "
              "so nobody can sign in. See SETUP-GUIDE.md.", file=sys.stderr)
    from waitress import serve
    from .web import create_app
    app = create_app(cfg, start_background=not args.no_backups)
    print(f"{APP_NAME} {__version__} on http://{args.host}:{args.port}  (data: {cfg.data_dir})")
    # behind a proxy (FORWORK_TRUST_PROXY=1) the app itself reads X-Forwarded-* (werkzeug ProxyFix)
    serve(app, host=args.host, port=args.port, threads=args.threads, ident=APP_NAME)
    return 0


def cmd_import(args) -> int:
    from .monday_import import import_bundle
    _, _, svc = _app_parts(args.data)

    def progress(msg, done, total):
        print(f"  {msg} ({done}/{total})" if total else f"  {msg}")

    r = import_bundle(svc, args.bundle, replace=args.replace, progress=progress)
    print(f"Imported {r['boards']} boards, {r['items']} items, {r['subitems']} subitems, {r['updates']} updates, "
          f"{r['views']} views, {r['automations']} automations; {r['users']} people added.")
    for b in r["skipped_boards"]:
        print(f"  skipped: {b}")
    if r["warnings"]:
        print(f"{len(r['warnings'])} notes:")
        for w in r["warnings"]:
            print(f"  - {w}")
    return 0


def cmd_add_admin(args) -> int:
    _, _, svc = _app_parts(args.data)
    u = svc.user_by_email(args.email)
    if u:
        with svc.db.write() as c:
            c.execute("UPDATE users SET role = 'admin', active = 1 WHERE id = ?", (u["id"],))
        print(f"{args.email} is now an admin.")
    else:
        svc.create_user(None, args.email, args.name or args.email.split("@")[0], role="admin")
        print(f"Added {args.email} as an admin. They can sign in with Microsoft now.")
    return 0


def cmd_check(args) -> int:
    cfg = Config.load(args.data)
    print(f"{APP_NAME} {__version__}")
    print(f"  data folder        {cfg.data_dir}")
    print(f"  address            {cfg.base_url or '(not set - FORWORK_BASE_URL)'}")
    print(f"  Microsoft sign-in  {'ready' if cfg.microsoft_ready else 'NOT set up'}")
    print(f"  email domains      {', '.join(cfg.allowed_domains) or 'any'}")
    print(f"  admins at sign-in  {', '.join(cfg.admins) or '(none listed)'}")
    print(f"  test sign-in       {'ON' if cfg.dev_login else 'off'}")
    print(f"  behind a proxy     {'yes' if cfg.trust_proxy else 'no'}")
    print(f"  journal mode       {cfg.journal_mode}")
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "pull-monday":
        from .monday_pull import main_pull
        return main_pull(argv[1:])
    if argv and argv[0] in ("backup", "restore"):
        from .backup import main_backup
        from .export import export_all_json
        data = None
        if "--data" in argv:
            data = argv[argv.index("--data") + 1]
        svc = Service(Database(Config.load(data).db_path, journal_mode=Config.load(data).journal_mode))
        return main_backup(argv, extra_exports=lambda d: export_all_json(svc, d))
    p = argparse.ArgumentParser(prog="python -m forwork", description=f"{APP_NAME} - Fortis work boards")
    p.add_argument("--data", help="data folder (default: FORWORK_DATA or ./forwork-data)")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve", help="run the web app")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8000)
    s.add_argument("--threads", type=int, default=8)
    s.add_argument("--no-backups", action="store_true", help="do not run the backup schedule in this process")
    s.set_defaults(fn=cmd_serve)
    i = sub.add_parser("import-monday", help="load a monday.com export")
    i.add_argument("bundle")
    i.add_argument("--replace", action="store_true")
    i.set_defaults(fn=cmd_import)
    a = sub.add_parser("add-admin", help="add or promote an admin")
    a.add_argument("email")
    a.add_argument("name", nargs="?")
    a.set_defaults(fn=cmd_add_admin)
    c = sub.add_parser("check", help="show the settings ForWork has picked up")
    c.set_defaults(fn=cmd_check)
    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
