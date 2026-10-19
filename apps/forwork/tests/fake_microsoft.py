"""
A stand-in for Microsoft's sign-in (Entra ID) on 127.0.0.1, for testing the whole sign-in round trip: it
checks the client ID and secret, the redirect URI and the PKCE challenge like the real service, and returns
an ID token for whichever account the test chose.
"""
import base64
import hashlib
import json
import secrets
import threading
import time
import urllib.parse

from flask import Flask, jsonify, redirect, request
from werkzeug.serving import make_server

TENANT = "11111111-2222-3333-4444-555555555555"
CLIENT = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
SECRET = "test-secret-value"


def unsigned_jwt(claims: dict) -> str:
    def b64(d):
        return base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()
    return f"{b64({'alg': 'none', 'typ': 'JWT'})}.{b64(claims)}."


class FakeMicrosoft:
    def __init__(self):
        self.account = {"email": "jo@fortisap.com.au", "name": "Jo Citizen", "oid": "oid-jo", "tid": TENANT}
        self.codes: dict[str, dict] = {}
        self.fail_token = None
        app = Flask("fake-ms")

        @app.get("/<tenant>/oauth2/v2.0/authorize")
        def authorize(tenant):
            q = request.args
            assert tenant == TENANT and q["client_id"] == CLIENT and q["code_challenge_method"] == "S256"
            code = secrets.token_urlsafe(12)
            self.codes[code] = {"challenge": q["code_challenge"], "redirect_uri": q["redirect_uri"], "nonce": q["nonce"],
                                "account": dict(self.account)}
            return redirect(q["redirect_uri"] + "?" + urllib.parse.urlencode({"code": code, "state": q["state"]}))

        @app.post("/<tenant>/oauth2/v2.0/token")
        def token(tenant):
            f = request.form
            if self.fail_token:
                return jsonify(error="invalid_client", error_description=self.fail_token), 401
            if f.get("client_id") != CLIENT or f.get("client_secret") != SECRET:
                return jsonify(error="invalid_client", error_description="AADSTS7000215: Invalid client secret provided."), 401
            c = self.codes.pop(f.get("code"), None)
            if not c or f.get("redirect_uri") != c["redirect_uri"]:
                return jsonify(error="invalid_grant"), 400
            challenge = base64.urlsafe_b64encode(hashlib.sha256(f["code_verifier"].encode()).digest()).rstrip(b"=").decode()
            if challenge != c["challenge"]:
                return jsonify(error="invalid_grant", error_description="PKCE check failed"), 400
            a = c["account"]
            claims = {"aud": CLIENT, "iss": f"https://login.microsoftonline.com/{a['tid']}/v2.0", "tid": a["tid"],
                      "oid": a["oid"], "preferred_username": a["email"], "name": a["name"], "nonce": c["nonce"],
                      "exp": time.time() + 3600}
            return jsonify(id_token=unsigned_jwt(claims), access_token="x", token_type="Bearer", expires_in=3600)

        self.server = make_server("127.0.0.1", 0, app, threaded=True)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def stop(self):
        self.server.shutdown()
