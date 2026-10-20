"""
Sign in with your office email - Microsoft 365 (Microsoft Entra ID).

The firm registers one app in Microsoft Entra (App registrations > New registration: "Accounts in this
organizational directory only", platform "Mobile and desktop applications", redirect URI
http://localhost) and enters its Directory (tenant) ID and Application (client) ID in Fortis Ledger's
firm settings.  Only accounts in the firm's Microsoft 365 can then open the app, and the change log
and edit lock record the signed-in email.  Nothing is stored on the PC: each time the app opens, the
browser signs in (usually one click, as Microsoft remembers the account).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .oauth import LoopbackSignIn, SignInError

GUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
AUTHORITY = "https://login.microsoftonline.com"
SCOPES = "openid profile email User.Read"


@dataclass
class MicrosoftConfig:
    tenant_id: str
    client_id: str
    allowed_domains: list[str] = field(default_factory=list)   # e.g. ["fortisap.com.au"]; empty = any account in the tenant
    authority: str = AUTHORITY

    def check(self) -> None:
        if not GUID_RE.match(self.tenant_id or ""):
            raise SignInError("the Directory (tenant) ID looks like 8-4-4-4-12 letters and digits - copy it from the "
                              "app's Overview page in Microsoft Entra")
        if not GUID_RE.match(self.client_id or ""):
            raise SignInError("the Application (client) ID looks like 8-4-4-4-12 letters and digits - copy it from the "
                              "app's Overview page in Microsoft Entra")

    @property
    def authorize_url(self) -> str:
        return f"{self.authority}/{self.tenant_id}/oauth2/v2.0/authorize"

    @property
    def token_url(self) -> str:
        return f"{self.authority}/{self.tenant_id}/oauth2/v2.0/token"


def domains(text: str) -> list[str]:
    return sorted({d.strip().lower().lstrip("@") for d in re.split(r"[\s,;]+", text or "") if d.strip()})


class MicrosoftSignIn(LoopbackSignIn):
    provider = "Microsoft 365"
    callback_path = "/"            # Microsoft accepts http://localhost on any port
    use_nonce = True

    def __init__(self, cfg: MicrosoftConfig, login_hint: str = ""):
        cfg.check()
        extra = {"prompt": "select_account"}
        if login_hint:
            extra["login_hint"] = login_hint
        super().__init__(cfg.client_id, cfg.authorize_url, cfg.token_url, SCOPES, port=0, extra_params=extra)
        self.cfg = cfg
        self.user: dict = {}

    def after_sign_in(self) -> str:
        c = self.claims
        tid = (c.get("tid") or "").lower()
        if tid != self.cfg.tenant_id.lower() or c.get("aud") != self.cfg.client_id:
            raise SignInError("that account is not in the firm's Microsoft 365")
        email = (c.get("preferred_username") or c.get("email") or c.get("upn") or "").strip().lower()
        if "@" not in email:
            raise SignInError("Microsoft did not say which account signed in")
        if self.cfg.allowed_domains and email.rsplit("@", 1)[1] not in self.cfg.allowed_domains:
            raise SignInError(f"{email} is not a firm email address ({', '.join(self.cfg.allowed_domains)})")
        self.user = {"email": email, "name": (c.get("name") or "").strip() or email, "oid": c.get("oid", ""),
                     "tid": tid}
        self.access_token = ""           # only the identity is needed
        return f"Signed in as {email}. Go back to Fortis Ledger - you can close this tab."
