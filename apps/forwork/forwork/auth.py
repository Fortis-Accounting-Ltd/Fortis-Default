"""
Signing in - with the firm's Microsoft 365 accounts, and nothing else.

ForWork is registered once in Microsoft Entra as a web app (see SETUP-GUIDE.md).  "Sign in with
Microsoft" sends the browser to Microsoft, which checks the password and the firm's multi-factor rules,
and comes back with a one-time code.  ForWork swaps the code for an ID token at Microsoft's token
endpoint (authorisation code flow with PKCE and the app's client secret) and checks that the account
is in the firm's directory and has a firm email address.  ForWork never sees a password.

A signed-in browser holds one random cookie.  The database keeps only its SHA-256, with the person, a
CSRF token, and when it expires: after 10 hours without use, or 7 days at most.  Signing out, turning
someone's access off, or an admin's "sign everyone out" deletes the session on the server.

For trying ForWork on a PC without Microsoft set up, FORWORK_DEV_LOGIN=1 shows a list of people to
sign in as - only to browsers on the same PC (127.0.0.1), and the server refuses to start with it on
any other address.
"""
from __future__ import annotations

import base64
import datetime as dt
import hashlib
import json
import re
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request

from .config import Config
from .db import Database, now_iso

IDLE_HOURS = 10
MAX_DAYS = 7
SCOPES = "openid profile email"
GUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)


class SignInError(Exception):
    pass


def pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)[:96]
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def jwt_claims(token: str) -> dict:
    """
    The claims of an ID token.  It comes straight from Microsoft's token endpoint over TLS, in exchange
    for our own code, PKCE verifier and client secret, which OpenID Connect accepts in place of checking
    the signature (OIDC Core 3.1.3.7).
    """
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload))
    except (IndexError, ValueError):
        return {}


_DIRECT = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _post(url: str, form: dict, timeout: float = 30) -> tuple[int, dict]:
    data = urllib.parse.urlencode(form).encode()
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"})
    host = urllib.parse.urlsplit(url).hostname or ""
    opener = _DIRECT if host in ("localhost", "127.0.0.1", "::1") else urllib.request.build_opener()
    try:
        with opener.open(req, timeout=timeout) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except ValueError:
            return e.code, {}
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        raise SignInError("ForWork could not reach Microsoft to finish signing you in - try again in a minute") from None


class MicrosoftSignIn:
    """The two halves of a browser sign-in: start() gives the Microsoft URL, finish() checks what comes back."""

    def __init__(self, cfg: Config):
        if not cfg.microsoft_ready:
            raise SignInError("Microsoft sign-in is not set up yet - an admin needs to follow SETUP-GUIDE.md")
        if not GUID_RE.match(cfg.ms_tenant_id) or not GUID_RE.match(cfg.ms_client_id):
            raise SignInError("the Microsoft tenant or client ID in the server settings is not right")
        self.cfg = cfg

    def redirect_uri(self, host_url: str) -> str:
        base = self.cfg.base_url or host_url.rstrip("/")
        return f"{base}/auth/callback"

    def start(self, host_url: str, login_hint: str = "") -> tuple[str, dict]:
        """(URL to send the browser to, what to remember until it comes back)."""
        verifier, challenge = pkce_pair()
        pending = {"state": secrets.token_urlsafe(24), "nonce": secrets.token_urlsafe(16), "verifier": verifier,
                   "redirect_uri": self.redirect_uri(host_url), "started": int(time.time())}
        q = {"client_id": self.cfg.ms_client_id, "response_type": "code", "redirect_uri": pending["redirect_uri"],
             "response_mode": "query", "scope": SCOPES, "state": pending["state"], "nonce": pending["nonce"],
             "code_challenge": challenge, "code_challenge_method": "S256", "prompt": "select_account"}
        if login_hint:
            q["login_hint"] = login_hint
        url = f"{self.cfg.ms_authority}/{self.cfg.ms_tenant_id}/oauth2/v2.0/authorize?{urllib.parse.urlencode(q)}"
        return url, pending

    def finish(self, args: dict, pending: dict | None) -> dict:
        """Check Microsoft's reply and return {"email", "name", "oid"} of the person who signed in."""
        if not pending or time.time() - pending.get("started", 0) > 15 * 60:
            raise SignInError("that sign-in took too long or was started in another browser - please sign in again")
        if not secrets.compare_digest(str(args.get("state", "")), pending["state"]):
            raise SignInError("that sign-in reply did not match - please sign in again")
        if args.get("error"):
            if args["error"] == "access_denied":
                raise SignInError("Microsoft did not let you into ForWork - ask an admin to give you access")
            raise SignInError(f"Microsoft could not sign you in: {args.get('error_description') or args['error']}")
        code = args.get("code") or ""
        if not code:
            raise SignInError("Microsoft did not send a sign-in code - please sign in again")
        status, tok = _post(f"{self.cfg.ms_authority}/{self.cfg.ms_tenant_id}/oauth2/v2.0/token", {
            "grant_type": "authorization_code", "client_id": self.cfg.ms_client_id,
            "client_secret": self.cfg.ms_client_secret, "code": code, "redirect_uri": pending["redirect_uri"],
            "code_verifier": pending["verifier"], "scope": SCOPES})
        if status != 200:
            msg = str(tok.get("error_description") or tok.get("error") or f"HTTP {status}").split("\r\n")[0][:300]
            if "AADSTS7000215" in msg or "AADSTS7000222" in msg:
                msg = "the client secret in the server settings is wrong or has expired - an admin needs to renew it"
            raise SignInError(f"Microsoft would not finish the sign-in: {msg}")
        c = jwt_claims(tok.get("id_token", ""))
        tenant = self.cfg.ms_tenant_id.lower()
        if (c.get("tid") or "").lower() != tenant or c.get("aud") != self.cfg.ms_client_id:
            raise SignInError("that account is not in the firm's Microsoft 365")
        iss = str(c.get("iss") or "")
        if iss and tenant not in iss.lower():
            raise SignInError("that sign-in came from the wrong Microsoft directory")
        if c.get("nonce") != pending["nonce"]:
            raise SignInError("that sign-in reply did not match - please sign in again")
        if c.get("exp") and float(c["exp"]) < time.time() - 300:
            raise SignInError("that sign-in has expired - please sign in again")
        email = (c.get("preferred_username") or c.get("email") or c.get("upn") or "").strip().lower()
        if "@" not in email:
            raise SignInError("Microsoft did not say which account signed in")
        if self.cfg.allowed_domains and email.rsplit("@", 1)[1] not in self.cfg.allowed_domains:
            raise SignInError(f"{email} is not a firm email address")
        return {"email": email, "name": (c.get("name") or "").strip() or email.split("@")[0], "oid": c.get("oid", "")}


# ---- people and sessions ------------------------------------------------------------------------ #
def user_for_identity(svc, cfg: Config, ident: dict) -> dict:
    """The ForWork user for a Microsoft identity - added as a member the first time (an admin if listed in FORWORK_ADMINS)."""
    db = svc.db
    u = None
    if ident.get("oid"):
        u = db.one("SELECT * FROM users WHERE ms_oid = ?", (ident["oid"],))
    u = dict(u) if u else svc.user_by_email(ident["email"])
    if u and not u["active"]:
        raise SignInError("your ForWork access has been turned off - ask an admin")
    is_admin = ident["email"] in cfg.admins
    with db.write() as c:
        if u is None:
            if not db.get_setting("auto_add_people", True) and not is_admin:
                raise SignInError("you are not set up in ForWork yet - ask an admin to add you")
            n = c.execute("SELECT COUNT(*) FROM users").fetchone()[0]
            first = n == 0                  # the very first person on a new, empty ForWork; otherwise FORWORK_ADMINS
            from .service import USER_COLORS
            uid = c.execute("INSERT INTO users (email, name, role, active, color, ms_oid, created_at, last_seen) "
                            "VALUES (?, ?, ?, 1, ?, ?, ?, ?)",
                            (ident["email"], ident["name"], "admin" if (is_admin or first) else "member",
                             USER_COLORS[n % len(USER_COLORS)], ident.get("oid") or None, now_iso(), now_iso())).lastrowid
            svc._log(c, None, None, None, "user.join", new={"email": ident["email"]}, detail={"user": uid})
        else:
            uid = u["id"]
            c.execute("UPDATE users SET ms_oid = COALESCE(ms_oid, ?), last_seen = ?, role = CASE WHEN ? THEN 'admin' ELSE role END "
                      "WHERE id = ?", (ident.get("oid") or None, now_iso(), 1 if is_admin else 0, uid))
            if u["name"] in ("", u["email"]) and ident.get("name"):
                c.execute("UPDATE users SET name = ? WHERE id = ?", (ident["name"], uid))
    return svc.user(uid)


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _later(**kw) -> str:
    return (dt.datetime.now(dt.timezone.utc) + dt.timedelta(**kw)).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def create_session(db: Database, user_id: int, ip: str = "", agent: str = "") -> tuple[str, str]:
    """A new session: returns (cookie token, CSRF token)."""
    token = secrets.token_urlsafe(32)
    csrf = secrets.token_urlsafe(24)
    now = now_iso()
    with db.write() as c:
        c.execute("DELETE FROM sessions WHERE expires_at < ?", (now,))
        c.execute("INSERT INTO sessions (id, user_id, csrf, created_at, last_seen, expires_at, ip, agent) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                  (_hash(token), user_id, csrf, now, now, _later(days=MAX_DAYS), (ip or "")[:64], (agent or "")[:300]))
    return token, csrf


def load_session(db: Database, token: str) -> tuple[dict, dict] | None:
    """(session, user) for a cookie, or None if it is unknown, expired, idle too long or the person is turned off."""
    if not token or len(token) > 200:
        return None
    s = db.one("SELECT * FROM sessions WHERE id = ?", (_hash(token),))
    if s is None:
        return None
    now = now_iso()
    u = db.one("SELECT * FROM users WHERE id = ?", (s["user_id"],))
    if s["expires_at"] < now or s["last_seen"] < _later(hours=-IDLE_HOURS) or u is None or not u["active"]:
        with db.write() as c:
            c.execute("DELETE FROM sessions WHERE id = ?", (s["id"],))
        return None
    if s["last_seen"][:16] != now[:16]:                    # at most one write a minute per session
        with db.write() as c:
            c.execute("UPDATE sessions SET last_seen = ? WHERE id = ?", (now, s["id"]))
            c.execute("UPDATE users SET last_seen = ? WHERE id = ?", (now, s["user_id"]))
    return dict(s), dict(u)


def end_session(db: Database, token: str) -> None:
    with db.write() as c:
        c.execute("DELETE FROM sessions WHERE id = ?", (_hash(token or ""),))


def end_all_sessions(db: Database, user_id: int | None = None) -> int:
    with db.write() as c:
        if user_id:
            return c.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,)).rowcount
        return c.execute("DELETE FROM sessions").rowcount
