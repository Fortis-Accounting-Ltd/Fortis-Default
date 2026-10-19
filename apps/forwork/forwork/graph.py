"""
Microsoft Graph, for the backups - just enough of it to put files in a OneDrive or a SharePoint document
library, list them, fetch one back and delete old ones.

ForWork talks to Graph as itself (app-only, the OAuth client credentials grant), not as a person, so the
hourly backup needs nobody signed in.  The firm's app in Microsoft Entra needs the application permission
Files.ReadWrite.All (or Sites.Selected, granted on the one site) with admin consent, and a client secret.
The access token is kept in memory only and never written to a log or an error message.

Only the standard library is used.  Requests go through the system proxy settings, except to localhost
(the tests' fake Graph server), which is always reached directly.
"""
from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

SCOPE = "https://graph.microsoft.com/.default"
SIMPLE_UPLOAD_MAX = 4 * 1024 * 1024      # larger files go through an upload session
CHUNK_UNIT = 320 * 1024                  # upload session chunks must be a multiple of 320 KiB
CHUNK_SIZE = 16 * CHUNK_UNIT             # 5 MiB
RETRY_STATUSES = (429, 500, 502, 503, 504)
LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1")


class GraphError(Exception):
    def __init__(self, message: str, status: int = 0):
        super().__init__(message)
        self.status = status


_DIRECT = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def http(method: str, url: str, headers: dict | None = None, data: bytes | None = None, token: str = "",
         timeout: float = 60, sink=None, service: str = "OneDrive") -> tuple[int, dict, bytes]:
    """
    One request.  The token goes in an "unredirected" header, so urllib does not pass it on when Graph
    redirects a download to its storage servers.  With sink (an open file), a successful body is
    streamed into it instead of being returned.
    """
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    if token:
        req.add_unredirected_header("Authorization", f"Bearer {token}")
    host = urllib.parse.urlsplit(url).hostname or ""
    opener = _DIRECT if host in LOCAL_HOSTS else urllib.request.build_opener()
    try:
        with opener.open(req, timeout=timeout) as r:
            if sink is not None:
                while block := r.read(1024 * 1024):
                    sink.write(block)
                return r.status, dict(r.headers), b""
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        reason = getattr(e, "reason", e)
        raise GraphError(f"could not reach {service} ({reason}) - check the internet connection") from None


def quote_path(path: str) -> str:
    """'ForWork Backups/a&b #1.json' -> 'ForWork%20Backups/a%26b%20%231.json' (each name encoded on its own)."""
    parts = [p for p in str(path).replace("\\", "/").split("/") if p]
    if any(p in (".", "..") for p in parts):
        raise GraphError(f"'{path}' is not a usable OneDrive path")
    return "/".join(urllib.parse.quote(p, safe="") for p in parts)


def _header(headers: dict, name: str) -> str:
    for k, v in headers.items():
        if k.lower() == name.lower():
            return v
    return ""


def _graph_message(body: bytes) -> tuple[str, str]:
    """(code, message) from a Graph error body."""
    try:
        err = json.loads(body).get("error") or {}
        if isinstance(err, dict):
            return str(err.get("code") or ""), str(err.get("message") or "")
        return str(err), ""
    except (ValueError, AttributeError):
        return "", body.decode("utf-8", "replace")[:200]


def describe_target(target: dict) -> str:
    kind = (target or {}).get("kind")
    if kind == "user":
        return f"{target.get('user', '')}'s OneDrive"
    if kind == "site":
        return f"the '{target.get('library') or 'Documents'}' library of {target.get('site_url', '')}"
    if kind == "drive":
        return "the chosen OneDrive drive"
    return "OneDrive"


class GraphClient:
    """
    A Graph client for one Entra app.  Remote paths are relative to the drive's root, with forward
    slashes: "ForWork Backups/forwork-20261018-233000Z.db.gz".
    """
    retries = 5                  # attempts for a request Graph answers with 429 or 5xx
    max_wait = 120.0             # longest Retry-After honoured, in seconds

    def __init__(self, tenant_id: str, client_id: str, client_secret: str,
                 authority: str = "https://login.microsoftonline.com",
                 graph_base: str = "https://graph.microsoft.com/v1.0", timeout: float = 60):
        self.tenant_id = (tenant_id or "").strip()
        self.client_id = (client_id or "").strip()
        self._secret = (client_secret or "").strip()
        self.authority = authority.rstrip("/")
        self.graph_base = graph_base.rstrip("/")
        self.timeout = timeout
        self.sleep = time.sleep
        self._token = ""
        self._token_until = 0.0
        self._lock = threading.Lock()
        self._drives: dict[str, str] = {}

    def __repr__(self) -> str:                  # never shows the secret
        return f"GraphClient(tenant_id={self.tenant_id!r}, client_id={self.client_id!r})"

    # ---- sign-in -------------------------------------------------------------------------- #
    def token(self) -> str:
        """An app-only access token, reused until 2 minutes before it expires."""
        with self._lock:
            if self._token and time.time() < self._token_until:
                return self._token
            if not (self.tenant_id and self.client_id and self._secret):
                raise GraphError("backups to OneDrive need the firm's Microsoft 365 app set up first "
                                 "(FORWORK_MS_TENANT_ID, FORWORK_MS_CLIENT_ID and a client secret)")
            form = urllib.parse.urlencode({"grant_type": "client_credentials", "client_id": self.client_id,
                                           "client_secret": self._secret, "scope": SCOPE}).encode()
            url = f"{self.authority}/{urllib.parse.quote(self.tenant_id, safe='')}/oauth2/v2.0/token"
            status, _, body = http("POST", url, data=form, timeout=self.timeout, service="Microsoft sign-in",
                                   headers={"Content-Type": "application/x-www-form-urlencoded",
                                            "Accept": "application/json"})
            if status != 200:
                raise GraphError(self._sign_in_problem(status, body), status)
            try:
                tok = json.loads(body)
                self._token = str(tok["access_token"])
                self._token_until = time.time() + float(tok.get("expires_in", 3600)) - 120
            except (ValueError, KeyError, TypeError):
                raise GraphError("Microsoft sign-in did not return a usable token for OneDrive - "
                                 "try again later") from None
            return self._token

    def _sign_in_problem(self, status: int, body: bytes) -> str:
        try:
            j = json.loads(body)
            text = str(j.get("error_description") or j.get("error") or "")
        except (ValueError, AttributeError):
            text = body.decode("utf-8", "replace")
        if "AADSTS7000222" in text:
            return ("the Microsoft 365 app's client secret has expired - make a new one in Microsoft Entra "
                    "(App registrations > Certificates & secrets) and update FORWORK_GRAPH_CLIENT_SECRET")
        if "AADSTS7000215" in text or "invalid_client" in text:
            return ("Microsoft did not accept the app's client secret - check FORWORK_GRAPH_CLIENT_SECRET "
                    "(copy the secret's Value, not its Secret ID)")
        if "AADSTS700016" in text:
            return "the Application (client) ID was not found in the firm's Microsoft 365 - check FORWORK_MS_CLIENT_ID"
        if "AADSTS90002" in text or "AADSTS900023" in text:
            return "the Directory (tenant) ID was not found - check FORWORK_MS_TENANT_ID"
        first = text.strip().splitlines()[0][:200] if text.strip() else "no details"
        return f"Microsoft sign-in for OneDrive failed (HTTP {status}): {self._scrub(first)}"

    def _scrub(self, text: str) -> str:
        for s in (self._secret, self._token):
            if s:
                text = text.replace(s, "***")
        return text

    # ---- requests -------------------------------------------------------------------------- #
    def call(self, method: str, url: str, what: str, ok: tuple = (200,), allow: tuple = (),
             body=None, data: bytes | None = None, headers: dict | None = None, auth: bool = True,
             sink=None) -> tuple[int, dict, bytes]:
        """
        A Graph request, retried when Graph is busy (429/5xx, honouring Retry-After) and once with a
        fresh token on 401.  Raises GraphError unless the status is in ok or allow.
        """
        if url.startswith("/"):
            url = self.graph_base + url
        hdrs = {"Accept": "application/json", **(headers or {})}
        if body is not None:
            data = json.dumps(body).encode()
            hdrs["Content-Type"] = "application/json"
        renewed = False
        for attempt in range(self.retries):
            token = self.token() if auth else ""
            status, rh, rb = http(method, url, headers=hdrs, data=data, token=token, timeout=self.timeout, sink=sink)
            if status in ok or status in allow:
                return status, rh, rb
            if status == 401 and auth and not renewed:
                renewed = True
                with self._lock:
                    self._token = ""
                continue
            if status in RETRY_STATUSES and attempt < self.retries - 1:
                self.sleep(self._wait(rh, attempt))
                continue
            raise GraphError(self.explain(status, rb, what), status)
        raise GraphError(f"OneDrive kept refusing {what} - try again later")

    def _wait(self, headers: dict, attempt: int) -> float:
        try:
            return min(max(float(_header(headers, "Retry-After")), 0.0), self.max_wait)
        except ValueError:
            return min(2.0 ** attempt, self.max_wait)

    def explain(self, status: int, body: bytes, what: str) -> str:
        code, message = _graph_message(body)
        if status == 401:
            return ("Microsoft did not accept ForWork's sign-in for OneDrive - check the app's client secret "
                    "has not expired (Microsoft Entra > App registrations > Certificates & secrets)")
        if status == 403:
            return (f"ForWork is not allowed {what} - an administrator needs to give the Microsoft 365 app the "
                    f"Files.ReadWrite.All application permission (with admin consent) in Microsoft Entra")
        if status == 404:
            return f"OneDrive could not find the folder or file it needed {what}"
        if status == 507 or code == "quotaLimitReached":
            return "the OneDrive is full - free up space or choose another place for the backups"
        if status in RETRY_STATUSES:
            return f"OneDrive was too busy {what} (HTTP {status}) - the backup will try again next time"
        detail = self._scrub(message or code or "no details")[:200]
        return f"OneDrive returned an error when asked {what} (HTTP {status}): {detail}"

    def _item(self, drive_id: str, path: str, suffix: str = "") -> str:
        """URL of a drive item by path: /drives/{id}/root:/{path}:{suffix}, or the root itself."""
        drive = f"/drives/{urllib.parse.quote(drive_id, safe='!')}"
        p = quote_path(path)
        if not p:
            return f"{drive}/root{suffix}"
        return f"{drive}/root:/{p}:{suffix}" if suffix else f"{drive}/root:/{p}"

    # ---- where the backups go ------------------------------------------------------------- #
    def resolve_drive(self, target: dict) -> str:
        """
        The drive id for a backup target:
          {"kind": "user", "user": "backups@fortisap.com.au"}                  that person's OneDrive
          {"kind": "site", "site_url": "https://x.sharepoint.com/sites/Fortis", "library": "Documents"}
          {"kind": "drive", "drive_id": "b!..."}
        """
        target = target or {}
        kind = target.get("kind")
        key = json.dumps(target, sort_keys=True)
        if key in self._drives:
            return self._drives[key]
        if kind == "drive":
            drive_id = str(target.get("drive_id") or "").strip()
            if not drive_id:
                raise GraphError("the OneDrive drive ID is blank - fill it in on the backup settings page")
        elif kind == "user":
            upn = str(target.get("user") or "").strip()
            if "@" not in upn:
                raise GraphError("enter the email address of the person whose OneDrive holds the backups")
            status, _, body = self.call("GET", f"/users/{urllib.parse.quote(upn, safe='@')}/drive?$select=id",
                                        f"to open {upn}'s OneDrive", allow=(404,))
            if status == 404:
                raise GraphError(f"no OneDrive was found for {upn} - check the email address, and that the person "
                                 f"has opened OneDrive at least once (Microsoft makes it on first use)", 404)
            drive_id = json.loads(body)["id"]
        elif kind == "site":
            drive_id = self._site_drive(str(target.get("site_url") or "").strip(),
                                        str(target.get("library") or "").strip())
        else:
            raise GraphError("the OneDrive target is not set up - choose a person's OneDrive, a SharePoint "
                             "library or a drive on the backup settings page")
        self._drives[key] = drive_id
        return drive_id

    def _site_drive(self, site_url: str, library: str) -> str:
        parts = urllib.parse.urlsplit(site_url)
        if parts.scheme != "https" or not parts.hostname:
            raise GraphError("the SharePoint site address should look like "
                             "https://fortisap.sharepoint.com/sites/Fortis")
        path = urllib.parse.quote(urllib.parse.unquote(parts.path).strip("/"), safe="/")   # "Fortis%20Team" as copied
        url = f"/sites/{parts.hostname}:/{path}" if path else f"/sites/{parts.hostname}"
        status, _, body = self.call("GET", url + "?$select=id", "to open the SharePoint site", allow=(404,))
        if status == 404:
            raise GraphError(f"the SharePoint site {site_url} was not found - copy its address from the browser "
                             f"while the site's home page is open", 404)
        site = urllib.parse.quote(json.loads(body)["id"], safe=",")
        if not library:
            _, _, body = self.call("GET", f"/sites/{site}/drive?$select=id", "to open the site's documents")
            return json.loads(body)["id"]
        _, _, body = self.call("GET", f"/sites/{site}/drives?$select=id,name,webUrl", "to list the site's libraries")
        drives = json.loads(body).get("value", [])
        want = library.casefold()
        for d in drives:
            url_name = urllib.parse.unquote(str(d.get("webUrl") or "").rstrip("/").rsplit("/", 1)[-1])
            if want in (str(d.get("name") or "").casefold(), url_name.casefold()):
                return d["id"]
        names = ", ".join(sorted(str(d.get("name")) for d in drives)) or "none"
        raise GraphError(f"the site has no document library called '{library}' (it has: {names})")

    # ---- files ---------------------------------------------------------------------------- #
    def upload(self, drive_id: str, local_path: str | Path, remote_path: str) -> dict:
        """Upload a file, replacing any file of that name.  Returns the new drive item."""
        local = Path(local_path)
        size = local.stat().st_size
        what = f"to save {local.name}"
        if size <= SIMPLE_UPLOAD_MAX:
            _, _, body = self.call("PUT", self._item(drive_id, remote_path, "/content"), what, ok=(200, 201),
                                   data=local.read_bytes(), headers={"Content-Type": "application/octet-stream"})
            return json.loads(body)
        _, _, body = self.call("POST", self._item(drive_id, remote_path, "/createUploadSession"), what,
                               body={"item": {"@microsoft.graph.conflictBehavior": "replace"}})
        upload_url = json.loads(body).get("uploadUrl")
        if not upload_url:
            raise GraphError(f"OneDrive did not start the upload of {local.name} - the backup will try again next time")
        try:
            with local.open("rb") as f:
                start = 0
                while start < size:
                    chunk = f.read(CHUNK_SIZE)
                    if not chunk:
                        raise GraphError(f"{local.name} changed while it was being sent - the backup will try again "
                                         f"next time")
                    end = start + len(chunk) - 1
                    # the upload URL carries its own authorisation: Graph asks that no token is sent to it
                    status, _, body = self.call("PUT", upload_url, what, ok=(200, 201, 202), auth=False, data=chunk,
                                                headers={"Content-Type": "application/octet-stream",
                                                         "Content-Length": str(len(chunk)),
                                                         "Content-Range": f"bytes {start}-{end}/{size}"})
                    start = end + 1
                    if status in (200, 201):
                        return json.loads(body)
        except BaseException:
            try:
                http("DELETE", upload_url, timeout=self.timeout)       # let OneDrive discard the partial file
            except GraphError:
                pass
            raise
        raise GraphError(f"OneDrive did not confirm the upload of {local.name} - the backup will try again next time")

    def list_folder(self, drive_id: str, folder: str) -> list[dict]:
        """The folder's contents: [{"name", "size", "id", "lastModifiedDateTime", "folder"}].  [] if it is missing."""
        url = (self._item(drive_id, folder, "/children")
               + "?$select=id,name,size,lastModifiedDateTime,folder,file&$top=500")
        out: list[dict] = []
        while url:
            status, _, body = self.call("GET", url, f"to list {folder or 'the drive'}", allow=(404,))
            if status == 404:
                return []
            page = json.loads(body)
            for it in page.get("value", []):
                out.append({"name": it.get("name", ""), "size": int(it.get("size") or 0), "id": it.get("id", ""),
                            "lastModifiedDateTime": it.get("lastModifiedDateTime", ""), "folder": "folder" in it})
            url = page.get("@odata.nextLink") or ""
            if url and not url.startswith(self.graph_base + "/"):
                # the token must only ever go to Graph itself
                raise GraphError("OneDrive sent a page link to an unexpected address - listing stopped")
        return out

    def delete(self, drive_id: str, remote_path: str) -> None:
        """Delete a file (to the OneDrive recycle bin).  A file that is already gone is not an error."""
        if not quote_path(remote_path):
            raise GraphError("will not delete the whole drive")
        self.call("DELETE", self._item(drive_id, remote_path), f"to delete {remote_path}", ok=(204, 200), allow=(404,))

    def download(self, drive_id: str, remote_path: str, local_path: str | Path) -> Path:
        local = Path(local_path)
        local.parent.mkdir(parents=True, exist_ok=True)
        tmp = local.with_name(f".{local.name}.{uuid.uuid4().hex[:8]}.tmp")
        try:
            with tmp.open("wb") as f:
                status, _, _ = self.call("GET", self._item(drive_id, remote_path, "/content"),
                                         f"to fetch {remote_path}", allow=(404,), sink=f)
            if status == 404:
                raise GraphError(f"{remote_path} is not in the OneDrive", 404)
            tmp.replace(local)
        finally:
            tmp.unlink(missing_ok=True)
        return local

    def ensure_folder(self, drive_id: str, folder: str) -> None:
        """Create the folder and any folders above it.  Folders that already exist are left as they are."""
        quote_path(folder)                       # rejects "." and ".."
        names = [p for p in str(folder).replace("\\", "/").split("/") if p]
        if not names:
            return
        status, _, _ = self.call("GET", self._item(drive_id, folder) + "?$select=id,folder",
                                 f"to open {folder}", allow=(404,))
        if status == 200:
            return
        for i, name in enumerate(names):
            parent = "/".join(names[:i])
            self.call("POST", self._item(drive_id, parent, "/children"), f"to make the folder {name}",
                      ok=(200, 201), allow=(409,),
                      body={"name": name, "folder": {}, "@microsoft.graph.conflictBehavior": "fail"})
