"""
A fake Microsoft sign-in and Microsoft Graph server for the backup tests: just the parts graph.py uses,
on 127.0.0.1, with drives kept in memory.  It refuses what the real one would (a wrong secret, a missing
or stale token, upload chunks out of order or of the wrong size, a token sent to an upload or download
address) and can be told to fail on purpose with fail().
"""
from __future__ import annotations

import json
import re
import threading
import urllib.parse
import uuid

from flask import Flask, Response, request
from werkzeug.serving import WSGIRequestHandler, make_server

TENANT = "11111111-2222-3333-4444-555555555555"
CLIENT_ID = "66666666-7777-8888-9999-000000000000"
SECRET = "fake-secret-Value~123"
USER = "backups@fortisap.com.au"
SITE_HOST = "fortisap.sharepoint.com"
SITE_PATH = "sites/Fortis"
SITE_ID = "fortisap.sharepoint.com,aaaa-1111,bbbb-2222"
CHUNK_UNIT = 320 * 1024
SIMPLE_MAX = 4 * 1024 * 1024
RANGE_RE = re.compile(r"^bytes (\d+)-(\d+)/(\d+)$")


class _Quiet(WSGIRequestHandler):
    def log_request(self, *a):
        pass


def _json(data, status: int = 200, headers: dict | None = None) -> Response:
    return Response(json.dumps(data), status, headers=headers or {}, content_type="application/json")


def _error(status: int, code: str, message: str = "") -> Response:
    return _json({"error": {"code": code, "message": message or code}}, status)


class Drive:
    def __init__(self, drive_id: str, name: str = "OneDrive", web_url: str = ""):
        self.id = drive_id
        self.name = name
        self.web_url = web_url
        self.files: dict[str, bytes] = {}       # "ForWork Backups/x.db.gz" -> content
        self.folders: set[str] = set()          # "ForWork Backups", "ForWork Backups/files", ...

    def add_parents(self, path: str) -> None:
        parts = path.split("/")[:-1]
        for i in range(1, len(parts) + 1):
            self.folders.add("/".join(parts[:i]))

    def item(self, path: str) -> dict | None:
        name = path.rsplit("/", 1)[-1]
        if path in self.files:
            return {"id": f"file:{path}", "name": name, "size": len(self.files[path]), "file": {},
                    "lastModifiedDateTime": "2026-10-18T00:00:00Z", "parentReference": {"driveId": self.id}}
        if path in self.folders:
            return {"id": f"folder:{path}", "name": name, "size": 0, "folder": {"childCount": len(self.children(path))},
                    "lastModifiedDateTime": "2026-10-18T00:00:00Z"}
        return None

    def children(self, folder: str) -> list[dict]:
        prefix = f"{folder}/" if folder else ""
        names = sorted({p[len(prefix):] for p in (*self.folders, *self.files)
                        if p.startswith(prefix) and p != folder and "/" not in p[len(prefix):]})
        return [self.item(prefix + n) for n in names]


class FakeGraph:
    def __init__(self, page_size: int = 100, expires_in: int = 3600):
        self.page_size = page_size
        self.expires_in = expires_in
        self.drives = {
            "drive-user": Drive("drive-user"),
            "drive-docs": Drive("drive-docs", "Documents", f"https://{SITE_HOST}/{SITE_PATH}/Shared%20Documents"),
            "drive-archive": Drive("drive-archive", "Archive", f"https://{SITE_HOST}/{SITE_PATH}/Archive"),
        }
        self.users = {USER: "drive-user"}
        self.tokens: set[str] = set()
        self.token_requests = 0
        self.log: list[tuple[str, str]] = []            # (method, decoded path) of every Graph request
        self.chunks: list[tuple[int, int, int]] = []    # accepted upload chunks: (start, end, total)
        self.auth_leaks: list[str] = []                 # upload/download requests that carried a token
        self.sessions: dict[str, dict] = {}
        self.downloads: dict[str, tuple[str, str]] = {}
        self.failures: list[dict] = []
        self.lock = threading.RLock()
        self.app = self._make_app()
        self.url = ""
        self._server = None

    # ---- control -------------------------------------------------------------------------- #
    def start(self) -> "FakeGraph":
        self._server = make_server("127.0.0.1", 0, self.app, threaded=True, request_handler=_Quiet)
        self.url = f"http://127.0.0.1:{self._server.server_port}"
        threading.Thread(target=self._server.serve_forever, daemon=True, name="fake-graph").start()
        return self

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()
            self._server.server_close()

    @property
    def authority(self) -> str:
        return self.url

    @property
    def graph_base(self) -> str:
        return f"{self.url}/v1.0"

    def fail(self, kind: str, status: int, times: int = 1, skip: int = 0, headers: dict | None = None,
             body: dict | None = None) -> None:
        """
        Answer the next `times` requests of a kind with `status`, after letting `skip` through.
        Kinds: token, graph (any Graph call), simple_upload, create_session, chunk, list.
        """
        self.failures.append({"kind": kind, "status": status, "times": times, "skip": skip,
                              "headers": headers or {}, "body": body or {"error": {"code": "injected",
                                                                                     "message": "injected failure"}}})

    def _failure(self, kind: str) -> Response | None:
        with self.lock:
            for f in self.failures:
                if f["kind"] == kind and f["times"] > 0:
                    if f["skip"] > 0:
                        f["skip"] -= 1
                        return None
                    f["times"] -= 1
                    return _json(f["body"], f["status"], f["headers"])
        return None

    def requests(self, method: str, contains: str = "") -> list[str]:
        return [p for m, p in self.log if m == method and contains in p]

    # ---- the app -------------------------------------------------------------------------- #
    def _make_app(self) -> Flask:
        app = Flask("fake_graph")

        @app.post("/<tenant>/oauth2/v2.0/token")
        def token(tenant):
            with self.lock:
                self.token_requests += 1
            failed = self._failure("token")
            if failed:
                return failed
            form = request.form
            if tenant != TENANT:
                return _json({"error": "invalid_request",
                              "error_description": f"AADSTS90002: Tenant '{tenant}' not found."}, 400)
            if (form.get("grant_type") != "client_credentials"
                    or form.get("scope") != "https://graph.microsoft.com/.default"):
                return _json({"error": "invalid_request", "error_description": "AADSTS900144: bad request"}, 400)
            if form.get("client_id") != CLIENT_ID:
                return _json({"error": "unauthorized_client",
                              "error_description": "AADSTS700016: Application not found in the directory."}, 400)
            if form.get("client_secret") != SECRET:
                return _json({"error": "invalid_client",
                              "error_description": "AADSTS7000215: Invalid client secret provided."}, 401)
            tok = f"tok-{uuid.uuid4().hex}"
            with self.lock:
                self.tokens.add(tok)
            return _json({"token_type": "Bearer", "expires_in": self.expires_in, "access_token": tok})

        @app.route("/v1.0/<path:rest>", methods=["GET", "POST", "PUT", "DELETE"])
        def graph(rest):
            raw = (request.environ.get("RAW_URI") or request.environ.get("REQUEST_URI") or "").split("?", 1)[0]
            raw = raw[len("/v1.0/"):]
            with self.lock:
                self.log.append((request.method, urllib.parse.unquote(raw)))
                auth = request.headers.get("Authorization", "")
                if not auth.startswith("Bearer ") or auth[7:] not in self.tokens:
                    return _error(401, "InvalidAuthenticationToken", "Access token is empty or expired.")
                failed = self._failure("graph")
                if failed:
                    return failed
                return self._dispatch(raw)

        @app.route("/upload/<sid>", methods=["PUT", "DELETE"])
        def upload(sid):
            with self.lock:
                self.log.append((request.method, f"upload/{sid}"))
                if "Authorization" in request.headers:
                    self.auth_leaks.append(f"upload/{sid}")
                    return _error(401, "unauthenticated", "do not send a token to the upload URL")
                session = self.sessions.get(sid)
                if session is None:
                    return _error(404, "itemNotFound", "upload session not found")
                if request.method == "DELETE":
                    del self.sessions[sid]
                    return Response(status=204)
                failed = self._failure("chunk")
                if failed:
                    return failed
                m = RANGE_RE.match(request.headers.get("Content-Range", ""))
                data = request.get_data()
                if not m:
                    return _error(400, "invalidRange", "Content-Range missing or malformed")
                start, end, total = (int(x) for x in m.groups())
                if session["total"] is None:
                    session["total"] = total
                if total != session["total"] or start != len(session["data"]):
                    return _error(416, "invalidRange", f"expected bytes {len(session['data'])}-")
                if len(data) != end - start + 1 or int(request.headers.get("Content-Length", -1)) != len(data):
                    return _error(400, "invalidRange", "Content-Length does not match the range")
                if end != total - 1 and len(data) % CHUNK_UNIT:
                    return _error(400, "invalidRange", "chunks must be a multiple of 320 KiB")
                session["data"] += data
                self.chunks.append((start, end, total))
                if end == total - 1:
                    drive = self.drives[session["drive"]]
                    new = session["path"] not in drive.files
                    drive.files[session["path"]] = bytes(session["data"])
                    drive.add_parents(session["path"])
                    del self.sessions[sid]
                    return _json(drive.item(session["path"]), 201 if new else 200)
                return _json({"nextExpectedRanges": [f"{end + 1}-"], "expirationDateTime": "2026-10-19T00:00:00Z"}, 202)

        @app.get("/download/<did>")
        def download(did):
            with self.lock:
                if "Authorization" in request.headers:
                    self.auth_leaks.append(f"download/{did}")
                drive_id, path = self.downloads[did]
                return Response(self.drives[drive_id].files[path], 200, content_type="application/octet-stream")

        return app

    def _dispatch(self, raw: str) -> Response:
        method = request.method
        if raw.startswith("users/"):
            parts = raw.split("/")
            upn = urllib.parse.unquote(parts[1])
            if parts[2:] == ["drive"] and upn.lower() in self.users:
                return _json({"id": self.users[upn.lower()], "driveType": "business"})
            return _error(404, "itemNotFound", "user or drive not found")
        if raw.startswith("sites/"):
            rest = raw[len("sites/"):]
            if ":" in rest:
                host, path = rest.split(":", 1)
                if (host, urllib.parse.unquote(path.strip("/"))) == (SITE_HOST, SITE_PATH):
                    return _json({"id": SITE_ID, "displayName": "Fortis"})
                return _error(404, "itemNotFound", "site not found")
            site, _, tail = rest.partition("/")
            if urllib.parse.unquote(site) != SITE_ID:
                return _error(404, "itemNotFound", "site not found")
            docs = [d for d in self.drives.values() if d.web_url.startswith(f"https://{SITE_HOST}/")]
            if tail == "drives":
                return _json({"value": [{"id": d.id, "name": d.name, "webUrl": d.web_url} for d in docs]})
            if tail == "drive":
                return _json({"id": docs[0].id})
            return _error(400, "invalidRequest", "unknown site request")
        if not raw.startswith("drives/"):
            return _error(400, "invalidRequest", "unknown request")
        drive_id, _, after = raw[len("drives/"):].partition("/")
        drive = self.drives.get(urllib.parse.unquote(drive_id))
        if drive is None:
            return _error(404, "itemNotFound", "drive not found")
        if after == "root/children":
            path, suffix = "", "/children"
        elif after == "root":
            path, suffix = "", ""
        elif after.startswith("root:/"):
            tail = after[len("root:/"):]
            enc, _, suffix = tail.partition(":")
            path = "/".join(urllib.parse.unquote(p) for p in enc.split("/") if p)
        else:
            return _error(400, "invalidRequest", f"unknown drive request {after}")

        if suffix == "" and method == "GET":
            item = drive.item(path) if path else {"id": "root", "name": "root", "folder": {}}
            return _json(item) if item else _error(404, "itemNotFound", "The resource could not be found.")
        if suffix == "" and method == "DELETE":
            if path in drive.files:
                del drive.files[path]
            elif path in drive.folders:
                drive.folders = {f for f in drive.folders if f != path and not f.startswith(path + "/")}
                drive.files = {f: v for f, v in drive.files.items() if not f.startswith(path + "/")}
            else:
                return _error(404, "itemNotFound", "The resource could not be found.")
            return Response(status=204)
        if suffix == "/children" and method == "GET":
            failed = self._failure("list")
            if failed:
                return failed
            if path and path not in drive.folders:
                return _error(404, "itemNotFound", "The resource could not be found.")
            items = drive.children(path)
            skip = int(request.args.get("$skiptoken", 0))
            page = {"value": items[skip:skip + self.page_size]}
            if skip + self.page_size < len(items):
                raw_path = request.environ["RAW_URI"].split("?", 1)[0]
                page["@odata.nextLink"] = f"{request.host_url.rstrip('/')}{raw_path}?$skiptoken={skip + self.page_size}"
            return _json(page)
        if suffix == "/children" and method == "POST":
            body = request.get_json()
            if path and path not in drive.folders:
                return _error(404, "itemNotFound", "parent folder not found")
            new = f"{path}/{body['name']}" if path else body["name"]
            if new in drive.folders or new in drive.files:
                return _error(409, "nameAlreadyExists", "An item with the same name already exists under the parent")
            drive.folders.add(new)
            return _json(drive.item(new), 201)
        if suffix == "/content" and method == "PUT":
            failed = self._failure("simple_upload")
            if failed:
                return failed
            data = request.get_data()
            if len(data) > SIMPLE_MAX:
                return _error(413, "requestTooLarge", "use an upload session for files over 4 MB")
            new = path not in drive.files
            drive.files[path] = data
            drive.add_parents(path)
            return _json(drive.item(path), 201 if new else 200)
        if suffix == "/content" and method == "GET":
            if path not in drive.files:
                return _error(404, "itemNotFound", "The resource could not be found.")
            did = uuid.uuid4().hex
            self.downloads[did] = (drive.id, path)
            return Response(status=302, headers={"Location": f"{request.host_url}download/{did}"})
        if suffix == "/createUploadSession" and method == "POST":
            failed = self._failure("create_session")
            if failed:
                return failed
            sid = uuid.uuid4().hex
            self.sessions[sid] = {"drive": drive.id, "path": path, "total": None, "data": bytearray()}
            return _json({"uploadUrl": f"{request.host_url}upload/{sid}", "expirationDateTime": "2026-10-19T00:00:00Z"})
        return _error(400, "invalidRequest", f"unsupported {method} {suffix}")
