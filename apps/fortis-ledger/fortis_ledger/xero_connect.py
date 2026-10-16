"""
Connect to Xero - sign in with a Xero login, choose an organisation, and read its chart of accounts and
year-end trial balances to set the client up in the ledger.  Read only: nothing is ever written to Xero.

The firm registers one Xero app at developer.xero.com (integration type "Mobile or desktop app", which
uses PKCE and has no client secret) with the redirect URI http://localhost:8749/callback, and enters its
client ID once in Fortis Ledger.  The access token lasts 30 minutes and is kept in memory only.
"""
from __future__ import annotations

import datetime as dt
import json
import time
import urllib.parse
from dataclasses import dataclass, field

from .oauth import LoopbackSignIn, SignInError, http

DEFAULT_PORT = 8749
SCOPES = "openid profile email accounting.settings.read accounting.reports.trialbalance.read"


@dataclass
class Endpoints:
    authorize: str = "https://login.xero.com/identity/connect/authorize"
    token: str = "https://identity.xero.com/connect/token"
    api: str = "https://api.xero.com"


@dataclass
class XeroConfig:
    client_id: str
    port: int = DEFAULT_PORT
    scopes: str = SCOPES
    endpoints: Endpoints = field(default_factory=Endpoints)

    @property
    def redirect_uri(self) -> str:
        return f"http://localhost:{self.port}/callback"


XeroError = SignInError


class XeroSession(LoopbackSignIn):
    provider = "Xero"
    callback_path = "/callback"

    def __init__(self, cfg: XeroConfig):
        super().__init__(cfg.client_id, cfg.endpoints.authorize, cfg.endpoints.token, cfg.scopes, cfg.port)
        self.cfg = cfg
        self.tenants: list[dict] = []

    @property
    def user(self) -> dict:
        return {k: self.claims.get(k, "") for k in ("email", "name", "given_name", "family_name")}

    def after_sign_in(self) -> str:
        self.tenants = self.connections()
        n = len(self.tenants)
        return (f"Connected to Xero ({n} organisation{'s' if n != 1 else ''}). "
                f"Go back to Fortis Ledger - you can close this tab.")

    def explain(self, status: int, body: bytes, what: str) -> str:
        text = body.decode("utf-8", "replace")[:300]
        if status == 401 and "insufficient_scope" in text.lower():
            return (f"Xero refused {what}: the firm's Xero app is missing a permission. In developer.xero.com, "
                    f"check the app allows these scopes: {self.cfg.scopes}")
        if status == 401:
            return "the Xero sign-in has expired (it lasts 30 minutes) - connect to Xero again"
        if status == 403:
            return f"your Xero login is not allowed to read {what} for this organisation"
        if status == 404:
            return f"Xero has no {what} for this organisation"
        if status == 429:
            return "Xero's limit on requests was reached - wait a minute and try again"
        return super().explain(status, body, what)

    # ---- reading ---------------------------------------------------------------------------- #
    def _headers(self, tenant_id: str | None = None) -> dict:
        if not self.access_token or time.time() > self.expires_at:
            raise XeroError("the Xero sign-in has expired (it lasts 30 minutes) - connect to Xero again")
        h = {"Authorization": f"Bearer {self.access_token}", "Accept": "application/json"}
        if tenant_id:
            h["xero-tenant-id"] = tenant_id
        return h

    def connections(self) -> list[dict]:
        status, _, body = http("GET", f"{self.cfg.endpoints.api}/connections", headers=self._headers())
        if status != 200:
            raise XeroError(self.explain(status, body, "the list of organisations"))
        return [c for c in json.loads(body) if (c.get("tenantType") or "ORGANISATION") == "ORGANISATION"]

    def tenant(self, tenant_id: str) -> dict:
        t = next((t for t in self.tenants if t.get("tenantId") == tenant_id), None)
        if t is None:
            raise XeroError("that organisation is not part of this Xero sign-in")
        return t

    def get(self, tenant_id: str, path: str, params: dict | None = None, what: str = "") -> dict:
        url = f"{self.cfg.endpoints.api}/api.xro/2.0/{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        for attempt in range(3):
            status, headers, body = http("GET", url, headers=self._headers(tenant_id))
            if status == 429 and attempt < 2:
                time.sleep(min(float(headers.get("Retry-After") or 5), 30))
                continue
            if status != 200:
                raise XeroError(self.explain(status, body, what or path))
            return json.loads(body)
        raise XeroError(self.explain(429, b"", what or path))

    def organisation(self, tenant_id: str) -> dict:
        return self.get(tenant_id, "Organisation", what="the organisation details")

    def accounts(self, tenant_id: str) -> dict:
        return self.get(tenant_id, "Accounts", what="the chart of accounts")

    def trial_balance(self, tenant_id: str, on: dt.date) -> dict:
        return self.get(tenant_id, "Reports/TrialBalance", {"date": on.isoformat()},
                        what=f"the trial balance at {on:%d/%m/%Y}")

    def disconnect(self, tenant_id: str) -> None:
        """Remove the organisation from the Xero app's connections (frees one of the free connections)."""
        conn = self.tenant(tenant_id)
        status, _, body = http("DELETE", f"{self.cfg.endpoints.api}/connections/{conn['id']}", headers=self._headers())
        if status not in (200, 204):
            raise XeroError(self.explain(status, body, "disconnecting"))
        self.tenants = [t for t in self.tenants if t.get("tenantId") != tenant_id]


def year_ends(fy_end_month: int, fy_end_day: int, today: dt.date | None = None, count: int = 4) -> list[dt.date]:
    """The last `count` completed financial year ends, oldest first."""
    from .model import _safe_date
    today = today or dt.date.today()
    end = _safe_date(today.year, fy_end_month, fy_end_day)
    if end >= today:
        end = _safe_date(today.year - 1, fy_end_month, fy_end_day)
    out = [end]
    while len(out) < count:
        out.append(_safe_date(out[-1].year - 1, fy_end_month, fy_end_day))
    return sorted(out)


def has_balances(tb_json: dict) -> bool:
    """Whether a trial balance holds any non-nil line (an organisation that had not started has none)."""
    report = (tb_json.get("Reports") or [tb_json])[0]

    def walk(rows) -> bool:
        for r in rows or []:
            if r.get("RowType") == "Section" and walk(r.get("Rows")):
                return True
            if r.get("RowType") == "Row":
                for c in (r.get("Cells") or [])[1:]:
                    v = str(c.get("Value") or "").replace(",", "").strip()
                    try:
                        if v and float(v) != 0:
                            return True
                    except ValueError:
                        continue
        return False

    return walk(report.get("Rows"))
