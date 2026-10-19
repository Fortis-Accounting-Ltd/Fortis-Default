"""
Server settings - read once at start-up from environment variables (or a forwork.conf file in the data
folder, for an office PC).  Secrets come only from here, never from the database, so a backup of the
database never carries them.

  FORWORK_DATA              data folder (default ./forwork-data)
  FORWORK_BASE_URL          the address people open, e.g. https://forwork.fortisap.com.au
  FORWORK_SECRET_KEY        signs cookies; made and kept in <data>/secret.key if not set
  FORWORK_MS_TENANT_ID      Microsoft Entra Directory (tenant) ID
  FORWORK_MS_CLIENT_ID      Microsoft Entra Application (client) ID
  FORWORK_MS_CLIENT_SECRET  the app's client secret (Certificates & secrets)
  FORWORK_ALLOWED_DOMAINS   email domains allowed to sign in (default fortisap.com.au)
  FORWORK_ADMINS            emails made admins at first sign-in (comma separated)
  FORWORK_GRAPH_CLIENT_SECRET   secret for OneDrive backups through Microsoft Graph (default: the sign-in secret)
  FORWORK_MONDAY_TOKEN      monday.com API token, only for importing from monday.com
  FORWORK_DEV_LOGIN         "1" = sign in without Microsoft, as any user - only allowed on 127.0.0.1
  FORWORK_TRUST_PROXY       "1" behind Azure App Service / a reverse proxy (reads X-Forwarded-Proto/For)
  FORWORK_JOURNAL_MODE      WAL (default) or DELETE (for a data folder on a network share such as Azure /home)
"""
from __future__ import annotations

import json
import os
import re
import secrets
from dataclasses import dataclass, field
from pathlib import Path

ENV_PREFIX = "FORWORK_"


def _list(text: str) -> list[str]:
    return [p.strip().lower().lstrip("@") for p in re.split(r"[\s,;]+", text or "") if p.strip()]


def _bool(text) -> bool:
    return str(text or "").strip().lower() in ("1", "true", "yes", "on")


@dataclass
class Config:
    data_dir: Path
    base_url: str = ""
    secret_key: str = ""
    ms_tenant_id: str = ""
    ms_client_id: str = ""
    ms_client_secret: str = ""
    allowed_domains: list[str] = field(default_factory=lambda: ["fortisap.com.au"])
    admins: list[str] = field(default_factory=list)
    graph_client_secret: str = ""
    monday_token: str = ""
    dev_login: bool = False
    trust_proxy: bool = False
    journal_mode: str = "WAL"
    ms_authority: str = "https://login.microsoftonline.com"
    graph_base: str = "https://graph.microsoft.com/v1.0"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "forwork.db"

    @property
    def files_dir(self) -> Path:
        return self.data_dir / "files"

    @property
    def backups_dir(self) -> Path:
        return self.data_dir / "backups"

    @property
    def microsoft_ready(self) -> bool:
        return bool(self.ms_tenant_id and self.ms_client_id and self.ms_client_secret)

    @property
    def secure_cookies(self) -> bool:
        return self.base_url.lower().startswith("https://")

    @classmethod
    def load(cls, data_dir: str | Path | None = None, env: dict | None = None) -> "Config":
        env = dict(os.environ if env is None else env)
        folder = Path(data_dir or env.get("FORWORK_DATA") or "forwork-data").resolve()
        conf = folder / "forwork.conf"
        if conf.is_file():
            # a file for an office PC: {"FORWORK_BASE_URL": "...", ...}; the environment wins
            for k, v in json.loads(conf.read_text(encoding="utf-8")).items():
                if k.startswith(ENV_PREFIX) and k not in env:
                    env[k] = str(v)
        folder.mkdir(parents=True, exist_ok=True)
        c = cls(
            data_dir=folder,
            base_url=env.get("FORWORK_BASE_URL", "").rstrip("/"),
            secret_key=env.get("FORWORK_SECRET_KEY", ""),
            ms_tenant_id=env.get("FORWORK_MS_TENANT_ID", "").strip(),
            ms_client_id=env.get("FORWORK_MS_CLIENT_ID", "").strip(),
            ms_client_secret=env.get("FORWORK_MS_CLIENT_SECRET", "").strip(),
            allowed_domains=_list(env.get("FORWORK_ALLOWED_DOMAINS", "fortisap.com.au")),
            admins=_list(env.get("FORWORK_ADMINS", "")),
            graph_client_secret=env.get("FORWORK_GRAPH_CLIENT_SECRET", "").strip(),
            monday_token=env.get("FORWORK_MONDAY_TOKEN", "").strip(),
            dev_login=_bool(env.get("FORWORK_DEV_LOGIN")),
            trust_proxy=_bool(env.get("FORWORK_TRUST_PROXY")),
            journal_mode=(env.get("FORWORK_JOURNAL_MODE") or "WAL").upper(),
        )
        if env.get("FORWORK_MS_AUTHORITY"):
            c.ms_authority = env["FORWORK_MS_AUTHORITY"].rstrip("/")
        if env.get("FORWORK_GRAPH_BASE"):
            c.graph_base = env["FORWORK_GRAPH_BASE"].rstrip("/")
        if c.journal_mode not in ("WAL", "DELETE", "TRUNCATE"):
            c.journal_mode = "WAL"
        if not c.graph_client_secret:
            c.graph_client_secret = c.ms_client_secret
        if not c.secret_key:
            c.secret_key = _stored_secret(folder / "secret.key")
        return c


def _stored_secret(path: Path) -> str:
    try:
        s = path.read_text(encoding="utf-8").strip()
        if len(s) >= 32:
            return s
    except FileNotFoundError:
        pass
    s = secrets.token_hex(32)
    path.write_text(s + "\n", encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return s
