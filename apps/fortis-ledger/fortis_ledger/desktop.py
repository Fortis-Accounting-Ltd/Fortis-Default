"""
Desktop launcher.  Starts the app's server on 127.0.0.1 (any free port) and shows it in a desktop
window - pywebview, which uses Microsoft Edge WebView2 on Windows.  Without a window toolkit it opens
the default web browser instead.

    python -m fortis_ledger              desktop window
    python -m fortis_ledger --browser    in the web browser
    python -m fortis_ledger --mcp        the Claude connector (MCP over stdin / stdout - Claude starts it)
    python -m fortis_ledger --claude-extension FILE
                                         write the Claude Desktop extension for this PC (a .mcpb file)
    Fortis Ledger.exe --install-update FILE        open an update file (.fortisupdate) to install it
    fortis-ledger-mcp.exe --install-update FILE --yes   install it without asking (IT, CI)
"""
from __future__ import annotations

import argparse
import atexit
import logging
import logging.handlers
import secrets
import sys
import threading
import time
import webbrowser
from pathlib import Path

from werkzeug.serving import make_server

from . import APP_NAME, __version__
from . import shortcut
from .settings import Settings, config_dir
from .web import create_app, release_locks

START_PAGES = {"clients": "/", "xero": "/xero", "settings": "/setup", "updates": "/updates"}

IDLE_LIMIT_SECONDS = 15 * 60     # browser mode: stop when no page has been open for this long


class DesktopApi:
    """Methods the page's JavaScript can call inside the desktop window (window.pywebview.api)."""

    def __init__(self):
        self._window = None      # underscore: pywebview does not expose it to the page

    def choose_folder(self) -> str:
        import webview
        if self._window is None:
            return ""
        kind = webview.FileDialog.FOLDER if hasattr(webview, "FileDialog") else webview.FOLDER_DIALOG
        res = self._window.create_file_dialog(kind)
        return res[0] if res else ""


def setup_logging():
    d = config_dir()
    d.mkdir(parents=True, exist_ok=True)
    h = logging.handlers.RotatingFileHandler(d / "fortis-ledger.log", maxBytes=1_000_000, backupCount=3,
                                             encoding="utf-8")
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(h)
    logging.getLogger("werkzeug").setLevel(logging.WARNING)
    return d / "fortis-ledger.log"


def installed_by_setup() -> bool:
    """Whether the Windows installer put this copy here - it leaves install.ini next to the program."""
    return (Path(sys.executable).resolve().parent / "install.ini").is_file()


def first_run_icon(settings: Settings) -> None:
    """
    A copy of the app that was simply unzipped puts its icon on the desktop the first time it runs on a PC
    (once only).  An installed copy leaves that to the installer, which makes the icons the person ticked.
    """
    if not (getattr(sys, "frozen", False) and shortcut.available()) or "app" in settings.icons_made:
        return
    if installed_by_setup():
        return
    try:
        path = shortcut.create("app")
    except shortcut.ShortcutError:
        logging.exception("could not put the desktop icon")
        return
    settings.icons_made.append("app")
    try:
        settings.save()
    except OSError:
        pass
    logging.info("desktop icon made: %s", path)


def say(text: str) -> None:
    if sys.stdout:            # a windowed (no console) build has no stdout
        print(text, flush=True)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="fortis-ledger", description=f"{APP_NAME} {__version__}")
    ap.add_argument("--browser", action="store_true", help="use the web browser instead of a desktop window")
    ap.add_argument("--port", type=int, default=0, help="port on 127.0.0.1 (default: any free port)")
    ap.add_argument("--no-open", action="store_true", help="start the server and print its address only")
    ap.add_argument("--data", help="data folder for this session (overrides the saved setting)")
    ap.add_argument("--start", choices=sorted(START_PAGES), default="clients", help="screen to open first")
    ap.add_argument("--self-test", action="store_true", help="check this copy end to end and exit (0 = ok)")
    ap.add_argument("--mcp", action="store_true", help="run the Claude connector on stdin / stdout (Claude starts it)")
    ap.add_argument("--claude-extension", metavar="FILE",
                    help="write the Claude Desktop extension for this PC to FILE (a .mcpb) and exit")
    ap.add_argument("--install-update", metavar="FILE", help="open an update file (.fortisupdate) to install it")
    ap.add_argument("--yes", action="store_true", help="with --install-update: install it without asking, then exit")
    ap.add_argument("--version", action="version", version=f"{APP_NAME} {__version__}")
    args = ap.parse_args(argv)
    if args.self_test:
        from .selftest import run
        return run()
    # these come before --mcp: the connector program adds --mcp to whatever it is given
    if args.claude_extension:
        from .claude_desktop import build_extension
        say(str(build_extension(args.claude_extension)))
        return 0
    if args.install_update and args.yes:
        from .updates import UpdateError, install, read_update
        try:
            update = read_update(args.install_update)
            install(update, source=str(args.install_update))
        except UpdateError as e:
            say(f"Not installed: {e}")
            return 1
        say(f"Installed Fortis Ledger {update.version} ({update.source}). It starts the next time Fortis Ledger opens.")
        return 0
    if args.mcp:
        from .connector import serve
        return serve()

    log_path = setup_logging()
    settings = Settings.load()
    if args.data:
        settings.data_folder = args.data
    token = secrets.token_urlsafe(24)
    app = create_app(settings, token=token)
    start = args.start
    if args.install_update:                  # an update file was opened (double-clicked, or from an email)
        app.config["UPDATE_FILE"] = str(Path(args.install_update).resolve())
        start = "updates"
    server = make_server("127.0.0.1", args.port, app, threaded=True)
    url = f"http://127.0.0.1:{server.server_port}{START_PAGES[start]}?token={token}"
    threading.Thread(target=server.serve_forever, name="fortis-ledger-server", daemon=True).start()
    logging.info("%s %s listening on 127.0.0.1:%s (log %s)", APP_NAME, __version__, server.server_port, log_path)

    atexit.register(release_locks, app)
    first_run_icon(settings)

    if not args.browser and not args.no_open:
        try:
            import webview
        except ImportError:
            webview = None
            logging.info("pywebview is not installed - using the browser")
        if webview is not None:
            api = DesktopApi()
            app.config["DESKTOP"] = True
            try:
                if isinstance(getattr(webview, "settings", None), dict):
                    webview.settings["ALLOW_DOWNLOADS"] = True
                window = webview.create_window(APP_NAME, url, js_api=api, width=1320, height=880,
                                               min_size=(980, 640), text_select=True)
                api._window = window
                app.config["WINDOW"] = window        # so Restart (after an update) can close it
                webview.start()                    # returns when the window is closed
                release_locks(app)
                server.shutdown()
                return 0
            except Exception:
                logging.exception("the desktop window could not start - using the browser")
                app.config["DESKTOP"] = False

    stop = threading.Event()
    app.config["SHUTDOWN"] = stop.set
    if not args.no_open:
        webbrowser.open(url)
    say(f"{APP_NAME} is running at {url}\nClose it with Quit (top right of the app) or Ctrl+C here.")
    try:
        while not stop.wait(1.0):
            if not args.no_open and time.time() - app.config["LAST_SEEN"] > IDLE_LIMIT_SECONDS:
                logging.info("no page open for %s minutes - stopping", IDLE_LIMIT_SECONDS // 60)
                break
    except KeyboardInterrupt:
        pass
    release_locks(app)
    server.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
