"""
A stand-in for Xero and Microsoft 365 on 127.0.0.1, for testing the sign-ins end to end: it checks the
PKCE challenge, the client ID and the redirect URI like the real services, and serves the few Xero API
calls the app makes.
"""
import base64
import hashlib
import html
import json
import secrets
import socket
import threading
import urllib.parse
import urllib.request

from flask import Flask, jsonify, redirect, request
from werkzeug.serving import make_server

TENANT = "11111111-2222-3333-4444-555555555555"
MS_CLIENT = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
XERO_CLIENT = "ABCDEF0123456789ABCDEF0123456789"

DIRECT = urllib.request.build_opener(urllib.request.ProxyHandler({}), type("NoRedirect", (urllib.request.HTTPRedirectHandler,),
                                     {"redirect_request": lambda *a, **k: None})())


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def unsigned_jwt(claims: dict) -> str:
    def b64(d):
        return base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()
    return f"{b64({'alg': 'none', 'typ': 'JWT'})}.{b64(claims)}."


def follow(url: str) -> tuple[int, str]:
    """Act as the browser: open the sign-in URL and follow its redirect back to the app's listener."""
    try:
        r = DIRECT.open(url, timeout=10)
        return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        if e.code in (301, 302, 303, 307):
            back = e.headers["Location"]
            try:
                r = DIRECT.open(back, timeout=20)
                return r.status, html.unescape(r.read().decode())
            except urllib.error.HTTPError as e2:
                return e2.code, html.unescape(e2.read().decode())
        return e.code, e.read().decode()


class FakeProvider:
    def __init__(self, xero_data: dict | None = None):
        self.codes: dict[str, dict] = {}
        self.calls: list[tuple[str, str]] = []
        self.deleted: list[str] = []
        self.ms_claims = {"preferred_username": "alex.citizen@fortisap.com.au", "name": "Alex Citizen", "tid": TENANT}
        self.deny = False
        self.xero = xero_data or {}
        app = Flask("fake-oauth")
        self.app = app

        def authorize(kind):
            q = request.args
            self.calls.append(("authorize", kind))
            if self.deny:
                return redirect(f"{q['redirect_uri']}?error=access_denied&state={q['state']}")
            assert q["response_type"] == "code" and q["code_challenge_method"] == "S256"
            code = secrets.token_hex(8)
            self.codes[code] = {"kind": kind, "challenge": q["code_challenge"], "redirect_uri": q["redirect_uri"],
                                "client_id": q["client_id"], "nonce": q.get("nonce"), "scope": q["scope"]}
            return redirect(f"{q['redirect_uri']}?{urllib.parse.urlencode({'code': code, 'state': q['state']})}")

        def token(kind):
            f = request.form
            info = self.codes.pop(f.get("code", ""), None)
            self.calls.append(("token", kind))
            if (info is None or info["kind"] != kind or f.get("grant_type") != "authorization_code"
                    or f.get("redirect_uri") != info["redirect_uri"] or f.get("client_id") != info["client_id"]):
                return jsonify({"error": "invalid_grant"}), 400
            digest = base64.urlsafe_b64encode(hashlib.sha256(f["code_verifier"].encode()).digest()).rstrip(b"=").decode()
            if digest != info["challenge"]:
                return jsonify({"error": "invalid_grant", "error_description": "PKCE check failed"}), 400
            if kind == "ms":
                claims = {**self.ms_claims, "aud": info["client_id"], "nonce": info["nonce"]}
            else:
                claims = {"email": "staff@fortisap.com.au", "name": "Staff Member"}
            return jsonify({"access_token": f"at-{kind}", "expires_in": 1800, "token_type": "Bearer",
                            "id_token": unsigned_jwt(claims)})

        app.add_url_rule("/xero/authorize", "x_auth", lambda: authorize("xero"))
        app.add_url_rule("/xero/token", "x_token", lambda: token("xero"), methods=["POST"])
        app.add_url_rule(f"/{TENANT}/oauth2/v2.0/authorize", "m_auth", lambda: authorize("ms"))
        app.add_url_rule(f"/{TENANT}/oauth2/v2.0/token", "m_token", lambda: token("ms"), methods=["POST"])

        def bearer_ok():
            return request.headers.get("Authorization") == "Bearer at-xero"

        @app.route("/xeroapi/connections")
        def connections():
            if not bearer_ok():
                return jsonify({"Title": "Unauthorized"}), 401
            return jsonify([{"id": "conn-1", "tenantId": "org1", "tenantType": "ORGANISATION",
                             "tenantName": "Migrated Pty Ltd"}])

        @app.route("/xeroapi/connections/<cid>", methods=["DELETE"])
        def disconnect(cid):
            self.deleted.append(cid)
            return "", 204

        @app.route("/xeroapi/api.xro/2.0/<path:path>")
        def api(path):
            if not bearer_ok() or request.headers.get("xero-tenant-id") != "org1":
                return jsonify({"Title": "Unauthorized"}), 401
            self.calls.append(("api", f"{path}?{request.query_string.decode()}"))
            if path == "Organisation":
                return jsonify(self.xero["org"])
            if path == "Accounts":
                return jsonify(self.xero["accounts"])
            if path == "Reports/TrialBalance":
                tb = self.xero["tbs"].get(request.args["date"])
                return jsonify(tb or {"Reports": [{"ReportID": "TrialBalance", "Rows": []}]})
            return jsonify({}), 404

    def __enter__(self):
        self.server = make_server("127.0.0.1", 0, self.app, threaded=True)
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *a):
        self.server.shutdown()
