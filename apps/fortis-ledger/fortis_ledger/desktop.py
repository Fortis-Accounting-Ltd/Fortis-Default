"""
Desktop launcher.  Starts the app's server on 127.0.0.1 (any free port) and shows it in a desktop
window - pywebview, which uses Microsoft Edge WebView2 on Windows.  Without a window toolkit it opens
the default web browser instead.

    python -m fortis_ledger              desktop window
    python -m fortis_ledger --browser    in the web browser
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

from werkzeug.serving import make_server

from . import APP_NAME, __version__
from .settings import Settings, config_dir
from .store import FolderStore
from .web import create_app

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


def say(text: str) -> None:
    if sys.stdout:            # a windowed (no console) build has no stdout
        print(text, flush=True)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="fortis-ledger", description=f"{APP_NAME} {__version__}")
    ap.add_argument("--browser", action="store_true", help="use the web browser instead of a desktop window")
    ap.add_argument("--port", type=int, default=0, help="port on 127.0.0.1 (default: any free port)")
    ap.add_argument("--no-open", action="store_true", help="start the server and print its address only")
    ap.add_argument("--data", help="data folder for this session (overrides the saved setting)")
    ap.add_argument("--self-test", action="store_true", help="check this copy end to end and exit (0 = ok)")
    ap.add_argument("--version", action="version", version=f"{APP_NAME} {__version__}")
    args = ap.parse_args(argv)
    if args.self_test:
        from .selftest import run
        return run()

    log_path = setup_logging()
    settings = Settings.load()
    if args.data:
        settings.data_folder = args.data
    token = secrets.token_urlsafe(24)
    app = create_app(settings, token=token)
    server = make_server("127.0.0.1", args.port, app, threaded=True)
    url = f"http://127.0.0.1:{server.server_port}/?token={token}"
    threading.Thread(target=server.serve_forever, name="fortis-ledger-server", daemon=True).start()
    logging.info("%s %s listening on 127.0.0.1:%s (log %s)", APP_NAME, __version__, server.server_port, log_path)

    def release_locks():
        if settings.data_folder and settings.user_name:
            st = FolderStore(settings.data_folder, user=settings.user_name)
            for slug in list(app.config["OPEN_CLIENTS"]):
                try:
                    st.release_lock(slug)
                except Exception:
                    logging.exception("could not release the edit lock on %s", slug)
        app.config["OPEN_CLIENTS"].clear()

    atexit.register(release_locks)

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
                webview.start()                    # returns when the window is closed
                release_locks()
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
    release_locks()
    server.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
