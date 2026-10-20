"""
Browser sign-in for a desktop app: OAuth 2.0 authorisation code with PKCE (RFC 7636), returning to a
one-off listener on localhost (RFC 8252).  Used for Microsoft 365 (sign in with your office email) and
for Xero.  There is no client secret, and tokens are kept in memory only - nothing about a sign-in is
written to disk.
"""
from __future__ import annotations

import base64
import hashlib
import html
import json
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

WAIT_SECONDS = 10 * 60           # how long the listener waits for the browser to come back


class SignInError(Exception):
    pass


def pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)[:96]
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def jwt_claims(token: str) -> dict:
    """
    The claims of an id_token.  The token comes straight from the provider's token endpoint over TLS in
    exchange for our own code and PKCE verifier, which OpenID Connect allows in place of checking the
    signature (OIDC Core 3.1.3.7).
    """
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload))
    except (IndexError, ValueError):
        return {}


_DIRECT = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def http(method: str, url: str, headers: dict | None = None, data: bytes | None = None,
         timeout: float = 60) -> tuple[int, dict, bytes]:
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    host = urllib.parse.urlsplit(url).hostname or ""
    opener = _DIRECT if host in ("localhost", "127.0.0.1", "::1") else urllib.request.build_opener()
    try:
        with opener.open(req, timeout=timeout) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        reason = getattr(e, "reason", e)
        raise SignInError(f"could not reach the sign-in service ({reason}) - check the internet connection") from None


class LoopbackSignIn:
    """
    One sign-in.  status: waiting -> signed_in | error | cancelled.
    port 0 lets the system choose a free port (Microsoft accepts any port on http://localhost);
    a fixed port must match the redirect URI registered with the provider (Xero).
    """
    provider = "the sign-in service"
    callback_path = "/callback"
    use_nonce = False

    def __init__(self, client_id: str, authorize_url: str, token_url: str, scopes: str, port: int = 0,
                 extra_params: dict | None = None):
        if not (client_id or "").strip():
            raise SignInError(f"the {self.provider} app's client ID is not set up yet")
        self.client_id = client_id.strip()
        self.authorize_endpoint = authorize_url
        self.token_endpoint = token_url
        self.scopes = scopes
        self.port = int(port or 0)
        self.extra_params = dict(extra_params or {})
        self.id = secrets.token_hex(8)
        self.state = secrets.token_urlsafe(24)
        self.nonce = secrets.token_urlsafe(16)
        self.verifier, self.challenge = pkce_pair()
        self.status = "waiting"
        self.error = ""
        self.access_token = ""
        self.expires_at = 0.0
        self.claims: dict = {}
        self.created = time.time()
        self._server: ThreadingHTTPServer | None = None
        self._lock = threading.Lock()

    # ---- the browser round trip ------------------------------------------------------------- #
    @property
    def redirect_uri(self) -> str:
        path = "" if self.callback_path == "/" else self.callback_path
        return f"http://localhost:{self.port}{path}"

    def authorize_url(self) -> str:
        q = {"response_type": "code", "client_id": self.client_id, "redirect_uri": self.redirect_uri,
             "scope": self.scopes, "state": self.state, "code_challenge": self.challenge,
             "code_challenge_method": "S256", **self.extra_params}
        if self.use_nonce:
            q["nonce"] = self.nonce
        return f"{self.authorize_endpoint}?{urllib.parse.urlencode(q)}"

    def listen(self) -> None:
        """Start the listener the provider's sign-in page returns to (call before authorize_url)."""
        session = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):          # keep codes out of any log
                pass

            def do_GET(self):
                parts = urllib.parse.urlsplit(self.path)
                if parts.path.rstrip("/") != session.callback_path.rstrip("/"):
                    self._reply(404, "Not found.")
                    return
                ok, message = session._callback(dict(urllib.parse.parse_qsl(parts.query)))
                self._reply(200 if ok else 400, message)
                if session.status != "waiting":
                    threading.Thread(target=session.stop_listening, daemon=True).start()

            def _reply(self, code: int, message: str):
                colour = "#0050c8" if code == 200 else "#b42318"
                page = ("<!doctype html><html lang='en-AU'><head><meta charset='utf-8'><title>Fortis Ledger</title></head>"
                        "<body style='font-family:Segoe UI,Arial,sans-serif;margin:80px auto;max-width:520px;color:#1d2733'>"
                        f"<h2 style='color:{colour}'>Fortis Ledger</h2><p>{html.escape(message)}</p></body></html>").encode()
                self.send_response(code)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(page)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(page)

        try:
            self._server = ThreadingHTTPServer(("127.0.0.1", self.port), Handler)
        except OSError:
            raise SignInError(f"signing in to {self.provider} needs port {self.port} on this PC and another program "
                              f"is using it - close that program and try again") from None
        self.port = self._server.server_address[1]
        self._server.daemon_threads = True
        threading.Thread(target=self._server.serve_forever, kwargs={"poll_interval": 0.25}, daemon=True,
                         name=f"sign-in-{self.id}").start()
        t = threading.Timer(WAIT_SECONDS, self._time_out)
        t.daemon = True
        t.start()

    def _time_out(self) -> None:
        if self.status == "waiting":
            self.status, self.error = "error", "the sign-in was not finished within 10 minutes - try again"
        self.stop_listening()

    def stop_listening(self) -> None:
        srv, self._server = self._server, None
        if srv:
            try:
                srv.shutdown()
                srv.server_close()
            except OSError:
                pass

    def cancel(self) -> None:
        if self.status == "waiting":
            self.status = "cancelled"
        self.stop_listening()
        self.access_token = ""

    def _callback(self, q: dict) -> tuple[bool, str]:
        with self._lock:
            if self.status != "waiting":
                return False, "This sign-in has already been used. Go back to Fortis Ledger."
            if not secrets.compare_digest(q.get("state", ""), self.state):
                return False, "This sign-in link does not belong to the open Fortis Ledger window."
            if q.get("error"):
                self.status = "error"
                self.error = (f"you did not allow Fortis Ledger to use {self.provider}" if q["error"] == "access_denied"
                              else f"{self.provider} sign-in failed: {q.get('error_description') or q['error']}")
                return False, f"Not signed in: {self.error}. You can close this tab."
            try:
                self._exchange(q.get("code", ""))
                if self.use_nonce and self.claims.get("nonce") != self.nonce:
                    raise SignInError("the sign-in reply did not match this sign-in - try again")
                message = self.after_sign_in()
            except SignInError as e:
                self.status, self.error, self.access_token = "error", str(e), ""
                return False, f"Not signed in: {e}. You can close this tab."
            self.status = "signed_in"
            return True, message

    def _exchange(self, code: str) -> None:
        if not code:
            raise SignInError(f"{self.provider} did not send a sign-in code")
        form = urllib.parse.urlencode({"grant_type": "authorization_code", "client_id": self.client_id,
                                       "code": code, "redirect_uri": self.redirect_uri,
                                       "code_verifier": self.verifier}).encode()
        status, _, body = http("POST", self.token_endpoint, data=form,
                               headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"})
        if status != 200:
            raise SignInError(self.explain(status, body, "the sign-in"))
        tok = json.loads(body)
        self.access_token = tok.get("access_token", "")
        self.expires_at = time.time() + float(tok.get("expires_in", 3600)) - 30
        self.claims = jwt_claims(tok.get("id_token", ""))
        if not self.access_token and not self.claims:
            raise SignInError(f"{self.provider} did not return a sign-in token")

    # ---- for subclasses ------------------------------------------------------------------ #
    def after_sign_in(self) -> str:
        """Check the signed-in identity; return the message shown in the browser tab."""
        return "Signed in. Go back to Fortis Ledger - you can close this tab."

    def explain(self, status: int, body: bytes, what: str) -> str:
        text = body.decode("utf-8", "replace")[:300]
        try:
            j = json.loads(body)
            text = j.get("error_description") or j.get("error") or j.get("Detail") or j.get("Message") or text
        except ValueError:
            pass
        return f"{self.provider} returned an error for {what} (HTTP {status}): {text}"
