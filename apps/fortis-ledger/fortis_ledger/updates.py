"""
App updates: small files with just the app's own code, made and signed by the firm's GitHub build, that can be
emailed or put in the shared folder for everyone.

The installed program carries a Python runtime and the libraries the app uses, plus the app's own code (the
fortis_ledger package) as plain files.  An update (.fortisupdate) is a zip with a new copy of that code:

  update.json      what it is: the version, the runtime it needs, the commit it was built from, and the SHA-256 of
                   every file in it
  provenance.jwt   an identity token GitHub Actions issued to the firm's build workflow, whose audience is
                   "fortis-ledger-update:" + the SHA-256 of update.json - GitHub's RS256 signature over the token
                   is the update's signature
  fortis_ledger/   the app's code and files

An update is installed only if GitHub's signature checks out (against GitHub's published keys), the token was
issued to the firm's own repository and build workflow, it names this update.json, and every file matches it -
so an update that comes by email, or that someone puts in the shared folder, can only carry what the firm's
build made.  It is unpacked under the person's local app data, beside the program rather than over it;
packaging/launch.py loads the newest one made for the installed runtime (new libraries need the full installer)
and falls back to the installed copy if it does not start.
"""
from __future__ import annotations

import base64
import datetime as dt
import hashlib
import hmac
import json
import os
import re
import shutil
import uuid
import zipfile
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from urllib.request import Request, urlopen

from . import __version__
from .settings import config_dir

FORMAT = 1
EXTENSION = ".fortisupdate"
ISSUER = "https://token.actions.githubusercontent.com"
JWKS_URL = ISSUER + "/.well-known/jwks"
REPOSITORY = "Fortisap-Git/Fortis-Default"
WORKFLOW = ".github/workflows/fortis-ledger.yml"
EVENTS = ("push", "pull_request", "workflow_dispatch")
AUDIENCE = "fortis-ledger-update:"
KEYS_FILE = Path(__file__).resolve().parent / "github_oidc_keys.json"
SHARED_DIR = "updates"                      # in the data folder: updates shared with everyone who uses it
MAX_FILES = 5000
MAX_BYTES = 80 * 1024 * 1024
SHA256_INFO = bytes.fromhex("3031300d060960864801650304020105000420")      # DigestInfo prefix, RFC 8017 9.2
NAME_RE = re.compile(r"(update\.json|provenance\.jwt|fortis_ledger(/[A-Za-z0-9_.\- ]+)+)")
VERSION_RE = re.compile(r'^__version__ = "([^"]+)"', re.M)


class UpdateError(Exception):
    pass


@dataclass
class Trust:
    """Whose updates are installed: GitHub identity tokens issued to this repository's build workflow, checked with
    GitHub's keys (the saved ones, then GitHub's current list for a key added since)."""
    repository: str = REPOSITORY
    workflow: str = WORKFLOW
    keys: list[dict] | None = None          # None: GitHub's keys
    fetch: bool = True


@dataclass
class Update:
    path: Path
    version: str
    runtime: str
    commit: str
    built: str
    files: dict[str, str]
    provenance: dict = field(default_factory=dict)
    size: int = 0

    @property
    def source(self) -> str:
        """Where GitHub says it was built: the branch, the commit and the build run."""
        p = self.provenance
        ref = str(p.get("ref") or "").removeprefix("refs/heads/")
        bits = [f"branch {ref}" if ref else "", f"commit {str(p.get('sha') or self.commit)[:7]}",
                f"build #{p['run_number']}" if p.get("run_number") else ""]
        return ", ".join(b for b in bits if b)


def version_key(v: str) -> tuple[int, ...]:
    if not isinstance(v, str) or not re.fullmatch(r"\d+(\.\d+){1,4}", v):
        raise UpdateError(f"{v!r} is not a version number")
    return tuple(int(x) for x in v.split("."))


def newer(a: str, b: str) -> bool:
    return version_key(a) > version_key(b)


def runtime() -> str:
    """The runtime of the installed program (set by packaging/launch.py); empty when running from source."""
    return os.environ.get("FORTIS_LEDGER_RUNTIME", "")


def updates_dir() -> Path:
    """Where updates are unpacked on this PC - packaging/launch.py looks in the same place."""
    env = os.environ.get("FORTIS_LEDGER_UPDATES")
    if env:
        return Path(env)
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "Fortis Ledger" / "updates"
    return config_dir() / "updates"


# ---- GitHub's signature ------------------------------------------------------------------------------ #
def _b64url(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _rs256(jwk: dict, signed: bytes, signature: bytes) -> bool:
    """RSASSA-PKCS1-v1_5 with SHA-256: the signature, raised to e mod n, must be exactly the padded digest."""
    if jwk.get("kty") != "RSA":
        return False
    try:
        n = int.from_bytes(_b64url(jwk["n"]), "big")
        e = int.from_bytes(_b64url(jwk["e"]), "big")
    except (KeyError, ValueError, TypeError):
        return False
    if n.bit_length() < 2048 or e < 3 or e % 2 == 0:
        return False
    k = (n.bit_length() + 7) // 8
    s = int.from_bytes(signature, "big")
    if len(signature) != k or s >= n:
        return False
    t = SHA256_INFO + hashlib.sha256(signed).digest()
    expected = b"\x00\x01" + b"\xff" * (k - len(t) - 3) + b"\x00" + t
    return hmac.compare_digest(pow(s, e, n).to_bytes(k, "big"), expected)


_github_keys: list[dict] | None = None


def _key(trust: Trust, kid: str) -> dict | None:
    if trust.keys is not None:
        return next((k for k in trust.keys if k.get("kid") == kid), None)
    saved = json.loads(KEYS_FILE.read_text(encoding="utf-8")).get("keys") or []
    found = next((k for k in saved if k.get("kid") == kid), None)
    if found or not trust.fetch:
        return found
    global _github_keys
    if _github_keys is None:
        try:
            req = Request(JWKS_URL, headers={"Accept": "application/json", "User-Agent": f"Fortis Ledger/{__version__}"})
            with urlopen(req, timeout=15) as r:
                _github_keys = json.loads(r.read().decode("utf-8")).get("keys") or []
        except (OSError, ValueError) as e:
            raise UpdateError(f"GitHub could not be reached to check the update's signature ({e}) - check the "
                              f"internet connection and try again") from None
    return next((k for k in _github_keys if k.get("kid") == kid), None)


def verify_token(token: str, audience: str, trust: Trust) -> dict:
    """Check a GitHub Actions identity token: GitHub's signature, and that it names this update and was issued to
    the firm's build workflow.  Returns its claims."""
    parts = token.strip().split(".")
    if len(parts) != 3 or not all(re.fullmatch(r"[A-Za-z0-9_-]+", p) for p in parts):
        raise UpdateError("the update's signature is not a GitHub identity token")
    try:
        header, claims = json.loads(_b64url(parts[0])), json.loads(_b64url(parts[1]))
    except ValueError:
        raise UpdateError("the update's signature is not a GitHub identity token") from None
    if not isinstance(header, dict) or not isinstance(claims, dict) or header.get("alg") != "RS256":
        raise UpdateError("the update is not signed the way GitHub signs")
    key = _key(trust, str(header.get("kid") or ""))
    if key is None:
        raise UpdateError("the update is signed with a key GitHub does not use")
    if not _rs256(key, f"{parts[0]}.{parts[1]}".encode("ascii"), _b64url(parts[2])):
        raise UpdateError("the update's signature does not match - it was changed after the firm's build made it")
    aud = claims.get("aud")
    if claims.get("iss") != ISSUER or audience not in (aud if isinstance(aud, list) else [aud]):
        raise UpdateError("the signature is for something else, not this update")
    repo = str(claims.get("repository") or "")
    if repo.lower() != trust.repository.lower():
        raise UpdateError(f"the update was not made by the firm's build ({trust.repository}) but by {repo or 'someone else'}")
    if not str(claims.get("workflow_ref") or "").lower().startswith(f"{trust.repository}/{trust.workflow}@".lower()) \
            or claims.get("event_name") not in EVENTS:
        raise UpdateError("the update was not made by the Fortis Ledger build")
    return claims


# ---- reading and installing ------------------------------------------------------------------------- #
def _safe(name: str) -> bool:
    parts = name.split("/")
    return bool(NAME_RE.fullmatch(name)) and not any(p in ("", ".", "..") for p in parts)


DAMAGED = (OSError, EOFError, RuntimeError, NotImplementedError, zipfile.BadZipFile, zlib.error)   # reading a zip


def read_update(path: str | Path, trust: Trust | None = None) -> Update:
    """Open an update file and check it completely; raises UpdateError saying what is wrong."""
    path, trust = Path(path), trust or Trust()
    try:
        z = zipfile.ZipFile(path)
    except (OSError, zipfile.BadZipFile):
        raise UpdateError(f"{path.name} is not a Fortis Ledger update") from None
    try:
        with z:
            return _check(path, z, trust)
    except DAMAGED as e:
        raise UpdateError(f"{path.name} is damaged ({e}) - get a fresh copy") from None


def _check(path: Path, z: zipfile.ZipFile, trust: Trust) -> Update:
    """Everything read_update checks, on the open file."""
    infos = [i for i in z.infolist() if not i.is_dir()]
    if len(infos) > MAX_FILES or sum(i.file_size for i in infos) > MAX_BYTES:
        raise UpdateError(f"{path.name} is too large to be a Fortis Ledger update")
    names = [i.filename for i in infos]
    if len(set(names)) != len(names) or not all(_safe(n) for n in names):
        raise UpdateError(f"{path.name} holds files a Fortis Ledger update may not have")
    if "update.json" not in names or "provenance.jwt" not in names:
        raise UpdateError(f"{path.name} is not a signed Fortis Ledger update")
    raw = z.read("update.json")
    claims = verify_token(z.read("provenance.jwt").decode("ascii", "replace"),
                          AUDIENCE + hashlib.sha256(raw).hexdigest(), trust)
    try:
        m = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise UpdateError(f"{path.name} is not a Fortis Ledger update") from None
    if not isinstance(m, dict) or m.get("format") != FORMAT or m.get("app") != "Fortis Ledger":
        raise UpdateError(f"{path.name} is not an update this version of Fortis Ledger can install")
    version, files = str(m.get("version")), m.get("files")
    version_key(version)
    payload = set(names) - {"update.json", "provenance.jwt"}
    if not isinstance(files, dict) or set(files) != payload or "fortis_ledger/__init__.py" not in payload:
        raise UpdateError(f"the files in {path.name} do not match its list")
    for name in sorted(payload):
        if hashlib.sha256(z.read(name)).hexdigest() != files[name]:
            raise UpdateError(f"{name} in {path.name} was changed after the update was made")
    code = VERSION_RE.search(z.read("fortis_ledger/__init__.py").decode("utf-8", "replace"))
    if not code or code.group(1) != version:
        raise UpdateError(f"{path.name} says it is version {version} but holds another version")
    return Update(path=path, version=version, runtime=str(m.get("runtime") or ""), commit=str(m.get("commit") or ""),
                  built=str(m.get("built") or ""), files=dict(files), provenance=claims, size=path.stat().st_size)


def can_install(update: Update) -> str:
    """Why this PC cannot install the update ("" when it can)."""
    if not runtime():
        return "updates go into the installed program - this copy of Fortis Ledger runs from its source folder"
    if update.runtime != runtime():
        return (f"Fortis Ledger {update.version} needs a newer installation of the program - install it with the full "
                f"installer (Fortis-Ledger-Setup-{update.version}.exe), which brings the update with it")
    if not newer(update.version, __version__):
        return f"this PC already has Fortis Ledger {__version__}"
    return ""


def _write_json(path: Path, data: dict) -> None:
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex[:8]}.tmp")
    tmp.write_text(json.dumps(data, indent=1) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def install(update: Update, source: str = "") -> Path:
    """Unpack the update for this PC (each file checked again against its verified list) and make it the one
    Fortis Ledger starts with next time.  Returns where it went."""
    why = can_install(update)
    if why:
        raise UpdateError(why)
    base = updates_dir()
    base.mkdir(parents=True, exist_ok=True)
    tmp = base / f".install-{uuid.uuid4().hex[:8]}"
    try:
        with zipfile.ZipFile(update.path) as z:
            for name, digest in update.files.items():
                data = z.read(name)
                if hashlib.sha256(data).hexdigest() != digest:
                    raise UpdateError(f"{name} in {update.path.name} changed while it was being installed")
                target = tmp / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
        keep = base / "files"
        keep.mkdir(exist_ok=True)
        shutil.copyfile(update.path, keep / f"Fortis-Ledger-Update-{update.version}{EXTENSION}")
        final = base / update.version
        if final.exists():
            shutil.rmtree(final)
        os.replace(tmp, final)
    except UpdateError:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    except (*DAMAGED, KeyError) as e:
        shutil.rmtree(tmp, ignore_errors=True)
        raise UpdateError(f"the update could not be installed: {e}") from None
    _write_json(base / "current.json", {"version": update.version, "dir": update.version, "runtime": update.runtime,
                                        "commit": update.commit, "installed": dt.datetime.now().isoformat(timespec="seconds"),
                                        "source": source})
    for old in base.iterdir():                        # keep this update and the one running now
        if old.is_dir() and old.name not in (update.version, __version__, "files") and re.fullmatch(r"\d+(\.\d+)+", old.name):
            shutil.rmtree(old, ignore_errors=True)
    return final


def use_installed_program() -> None:
    """Start with the copy installed with the program next time, not the update."""
    cur = updates_dir() / "current.json"
    if cur.exists():
        data = json.loads(cur.read_text(encoding="utf-8-sig"))
        data["bad"] = "set aside by hand"
        _write_json(cur, data)


def state() -> dict:
    """This PC: the version running, the program's runtime, and an installed update waiting for a restart."""
    try:
        cur = json.loads((updates_dir() / "current.json").read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        cur = {}
    try:
        waiting = not cur.get("bad") and cur.get("runtime") == runtime() and newer(str(cur.get("version")), __version__)
    except UpdateError:
        waiting = False
    return {"running": __version__, "runtime": runtime(), "installed": cur, "restart": waiting,
            "set_aside": cur.get("bad") or ""}


def saved_file(version: str) -> Path | None:
    """The update file an installed update came from (kept so it can be emailed on)."""
    p = updates_dir() / "files" / f"Fortis-Ledger-Update-{version}{EXTENSION}"
    return p if p.is_file() else None


# ---- the shared folder ---------------------------------------------------------------------------- #
def shared_dir(data_folder: str | Path) -> Path:
    return Path(data_folder) / SHARED_DIR


def share(update: Update, data_folder: str | Path) -> Path:
    """Put a checked update in the data folder, where every PC that uses it is offered it."""
    d = shared_dir(data_folder)
    d.mkdir(parents=True, exist_ok=True)
    dst = d / f"Fortis-Ledger-Update-{update.version}{EXTENSION}"
    tmp = d / f".{dst.name}.{uuid.uuid4().hex[:8]}.tmp"
    shutil.copyfile(update.path, tmp)
    os.replace(tmp, dst)
    return dst


def newest_shared(data_folder: str | Path, trust: Trust | None = None) -> tuple[Update | None, list[str]]:
    """The newest update in the shared folder that this PC can install, and what is wrong with any others."""
    d = shared_dir(data_folder)
    if not d.is_dir():
        return None, []
    found = []
    for p in d.glob("*" + EXTENSION):
        m = re.search(r"(\d+(?:\.\d+)+)" + re.escape(EXTENSION) + "$", p.name)
        try:
            if m and newer(m.group(1), __version__):
                found.append((version_key(m.group(1)), p))
        except UpdateError:
            continue
    problems = []
    for _, p in sorted(found, reverse=True):
        try:
            u = read_update(p, trust)
        except UpdateError as e:
            problems.append(f"{p.name}: {e}")
            continue
        why = can_install(u)
        if not why:
            return u, problems
        problems.append(f"{p.name}: {why}")
    return None, problems
