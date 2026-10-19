"""
Make an update file (.fortisupdate) from this source folder: the app's own code and files (fortis_ledger), a list of
them with their SHA-256 (update.json), and - in GitHub Actions - an identity token GitHub issues to the build for
that list, which is the update's signature (see fortis_ledger/updates.py).

    python packaging/make_update.py dist --require-signature      dist/Fortis-Ledger-Update-<version>.fortisupdate
    python packaging/make_update.py OUT --version 0.2.0.1        a test update: the version inside it is changed

The job needs "permissions: id-token: write".  Elsewhere the file is made unsigned, and Fortis Ledger will not
install it.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import sys
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

APP = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(APP))

from fortis_ledger.updates import AUDIENCE, EXTENSION, FORMAT, VERSION_RE  # noqa: E402


def runtime() -> str:
    m = re.search(r'^RUNTIME = "([^"]+)"', (APP / "packaging" / "launch.py").read_text(encoding="utf-8"), re.M)
    if not m:
        raise SystemExit("packaging/launch.py does not say its RUNTIME")
    return m.group(1)


def payload(version: str | None) -> dict[str, bytes]:
    files = {}
    for f in sorted((APP / "fortis_ledger").rglob("*")):
        if not f.is_file() or "__pycache__" in f.parts or f.suffix in (".pyc", ".pyo") or f.name.startswith("."):
            continue
        name, data = f.relative_to(APP).as_posix(), f.read_bytes()
        if version and name == "fortis_ledger/__init__.py":
            data = VERSION_RE.sub(f'__version__ = "{version}"', data.decode("utf-8"), count=1).encode("utf-8")
        files[name] = data
    return files


def github_token(audience: str) -> str | None:
    url, bearer = os.environ.get("ACTIONS_ID_TOKEN_REQUEST_URL"), os.environ.get("ACTIONS_ID_TOKEN_REQUEST_TOKEN")
    if not url or not bearer:
        return None
    req = urllib.request.Request(f"{url}&audience={urllib.parse.quote(audience, safe='')}",
                                 headers={"Authorization": f"bearer {bearer}", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode("utf-8"))["value"]


def make(out: Path, version: str | None = None, token_for=github_token) -> tuple[Path, bool]:
    """Write the update file to `out` (a folder: named after the version in it)."""
    files = payload(version)
    version = version or VERSION_RE.search(files["fortis_ledger/__init__.py"].decode("utf-8")).group(1)
    if out.is_dir():
        out = out / f"Fortis-Ledger-Update-{version}{EXTENSION}"
    manifest = {"format": FORMAT, "app": "Fortis Ledger", "version": version, "runtime": runtime(),
                "commit": os.environ.get("GITHUB_SHA", ""), "built": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                "files": {n: hashlib.sha256(d).hexdigest() for n, d in files.items()}}
    raw = (json.dumps(manifest, indent=1, sort_keys=True) + "\n").encode("utf-8")
    token = token_for(AUDIENCE + hashlib.sha256(raw).hexdigest())
    out.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        z.writestr("update.json", raw)
        if token:
            z.writestr("provenance.jwt", token)
        for name, data in files.items():
            z.writestr(name, data)
    return out, bool(token)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("out", help="the .fortisupdate file to write, or a folder to write it in")
    ap.add_argument("--version", help="make a test update with this version number inside it")
    ap.add_argument("--require-signature", action="store_true", help="fail unless GitHub signs it")
    args = ap.parse_args()
    out, signed = make(Path(args.out), args.version)
    if args.require_signature and not signed:
        print("FAILED - no GitHub identity token: run this in GitHub Actions with 'permissions: id-token: write'")
        return 1
    print(f"{out} ({out.stat().st_size:,} bytes, {'signed by GitHub' if signed else 'UNSIGNED - Fortis Ledger will not install it'})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
