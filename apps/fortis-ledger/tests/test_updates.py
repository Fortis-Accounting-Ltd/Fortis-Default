"""App updates: small files with the app's own code, signed by GitHub for the firm's build, checked completely
before they are installed, installed beside the program, and loaded by the launcher - with the program's own copy
to fall back on.  GitHub's part is played by a test key here; CI checks a real GitHub-signed update."""
import base64
import html
import io
import json
import os
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from fortis_ledger import __version__, desktop, updates
from fortis_ledger.settings import Settings
from fortis_ledger.store import FolderStore
from fortis_ledger.updates import ISSUER, Trust, UpdateError
from fortis_ledger.web import create_app

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP / "packaging"))
import launch  # noqa: E402
import make_update  # noqa: E402

NEXT, LATER = f"{__version__}.1", f"{__version__}.2"
REPO = "Fortisap-Git/Fortis-Default"
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OTHER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def jwk(key, kid="test-key") -> dict:
    n = key.public_key().public_numbers().n
    return {"kty": "RSA", "kid": kid, "alg": "RS256", "use": "sig", "n": b64(n.to_bytes((n.bit_length() + 7) // 8, "big")),
            "e": b64((65537).to_bytes(3, "big"))}


TRUST = Trust(keys=[jwk(KEY)], fetch=False)


def token(audience: str, key=KEY, kid="test-key", **claims) -> str:
    """An identity token the way GitHub Actions issues one to a workflow run."""
    body = {"iss": ISSUER, "aud": audience, "repository": REPO, "event_name": "push", "ref": "refs/heads/main",
            "workflow_ref": f"{REPO}/.github/workflows/fortis-ledger.yml@refs/heads/main", "sha": "0123456789abcdef",
            "run_number": "42", "exp": 1, **claims}
    signed = b64(json.dumps({"alg": "RS256", "typ": "JWT", "kid": kid}).encode()) + "." + b64(json.dumps(body).encode())
    return signed + "." + b64(key.sign(signed.encode("ascii"), padding.PKCS1v15(), hashes.SHA256()))


def make(folder: Path, version=NEXT, **claims) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    out, signed = make_update.make(folder, version, token_for=lambda aud: token(aud, **claims))
    assert signed and out.name == f"Fortis-Ledger-Update-{version}.fortisupdate"
    return out


def rewrite(src: Path, dst: Path, change=lambda name, data: data, add=()) -> Path:
    """A copy of an update file with its entries changed (change returns None to leave one out)."""
    with zipfile.ZipFile(src) as zin, zipfile.ZipFile(dst, "w") as zout:
        for name in zin.namelist():
            data = change(name, zin.read(name))
            if data is not None:
                zout.writestr(name, data)
        for name, data in add:
            zout.writestr(name, data)
    return dst


@pytest.fixture
def pc(tmp_path, monkeypatch):
    """An installed copy of Fortis Ledger (runtime 1) with its own updates folder."""
    monkeypatch.setenv("FORTIS_LEDGER_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("FORTIS_LEDGER_UPDATES", str(tmp_path / "pc1-updates"))
    monkeypatch.setenv("FORTIS_LEDGER_RUNTIME", launch.RUNTIME)
    return tmp_path / "pc1-updates"


def test_github_signature_check():
    signed = b"header.claims"
    good = KEY.sign(signed, padding.PKCS1v15(), hashes.SHA256())
    assert updates._rs256(jwk(KEY), signed, good)
    assert not updates._rs256(jwk(KEY), b"header.claimz", good)
    assert not updates._rs256(jwk(OTHER_KEY), signed, good)
    assert not updates._rs256(jwk(KEY), signed, good[:-1])
    assert not updates._rs256(jwk(KEY), signed, KEY.sign(signed, padding.PKCS1v15(), hashes.SHA1()))
    assert not updates._rs256(jwk(KEY), signed, KEY.sign(signed, padding.PSS(padding.MGF1(hashes.SHA256()), 32),
                                                         hashes.SHA256()))
    small = rsa.generate_private_key(public_exponent=65537, key_size=1024)
    assert not updates._rs256(jwk(small), signed, small.sign(signed, padding.PKCS1v15(), hashes.SHA256()))
    # the copy of GitHub's keys that comes with the app
    keys = json.loads(updates.KEYS_FILE.read_text(encoding="utf-8"))["keys"]
    assert len(keys) >= 2 and all(k["kty"] == "RSA" and len(base64.urlsafe_b64decode(k["n"] + "==")) >= 256 for k in keys)


def test_reading_an_update(tmp_path, monkeypatch):
    path = make(tmp_path / "made")
    u = updates.read_update(path, TRUST)
    assert (u.version, u.runtime) == (NEXT, launch.RUNTIME) and u.source == "branch main, commit 0123456, build #42"
    assert {"fortis_ledger/__init__.py", "fortis_ledger/web/templates/base.html", "fortis_ledger/github_oidc_keys.json"} <= set(u.files)
    assert not any("__pycache__" in f or f.endswith(".pyc") for f in u.files)
    with zipfile.ZipFile(path) as z:
        assert f'__version__ = "{NEXT}"' in z.read("fortis_ledger/__init__.py").decode()

    def refused(p, match):
        with pytest.raises(UpdateError, match=match):
            updates.read_update(p, TRUST)

    bad = tmp_path / "bad.fortisupdate"
    refused(rewrite(path, bad, lambda n, d: d + b"\n# changed" if n == "fortis_ledger/desktop.py" else d),
            "fortis_ledger/desktop.py in bad.fortisupdate was changed after the update was made")
    refused(rewrite(path, bad, add=[("fortis_ledger/extra.py", b"print('hi')")]), "do not match its list")
    refused(rewrite(path, bad, lambda n, d: None if n == "fortis_ledger/team.py" else d), "do not match its list")
    refused(rewrite(path, bad, lambda n, d: d.replace(b'"runtime": "1"', b'"runtime": "9"') if n == "update.json" else d),
            "the signature is for something else")
    refused(rewrite(path, bad, lambda n, d: None if n == "provenance.jwt" else d), "is not a signed Fortis Ledger update")
    refused(rewrite(path, bad, add=[("fortis_ledger/../../evil.py", b"x")]), "holds files a Fortis Ledger update may not have")
    refused(rewrite(path, bad, add=[("fortis_ledger\\evil.py", b"x")]),       # Windows' zipfile reads \ as /
            "holds files a Fortis Ledger update may not have|do not match its list")
    bad.write_bytes(b"MZ not a zip")
    refused(bad, "is not a Fortis Ledger update")
    damaged = bytearray(path.read_bytes())
    damaged[damaged.index(b"fortis_ledger/store.py") + 300] ^= 0xFF          # inside that file's packed bytes
    bad.write_bytes(bytes(damaged))
    refused(bad, "bad.fortisupdate is damaged|store.py in bad.fortisupdate was changed")

    def resigned(**kw):
        return rewrite(path, bad, lambda n, d: token(_audience(path), **kw).encode() if n == "provenance.jwt" else d)

    refused(resigned(key=OTHER_KEY), "signature does not match - it was changed after the firm's build made it")
    refused(resigned(kid="someone-elses"), "signed with a key GitHub does not use")
    refused(resigned(repository="someone/fork"), "not made by the firm's build .* but by someone/fork")
    refused(resigned(workflow_ref=f"{REPO}/.github/workflows/other.yml@refs/heads/main"), "not made by the Fortis Ledger build")
    refused(resigned(event_name="schedule"), "not made by the Fortis Ledger build")
    refused(resigned(iss="https://example.com"), "the signature is for something else")
    refused(rewrite(path, bad, lambda n, d: b"not.a.token!" if n == "provenance.jwt" else d), "not a GitHub identity token")

    # the version in the list must be the version of the code
    real = make_update.payload
    monkeypatch.setattr(make_update, "payload", lambda v: real(NEXT))
    refused(make(tmp_path / "mixed", version=LATER), f"says it is version {LATER} but holds another version")


def _audience(path: Path) -> str:
    import hashlib
    with zipfile.ZipFile(path) as z:
        return updates.AUDIENCE + hashlib.sha256(z.read("update.json")).hexdigest()


def test_installing_and_choosing_what_starts(tmp_path, pc, monkeypatch):
    u = updates.read_update(make(tmp_path / "made"), TRUST)
    monkeypatch.delenv("FORTIS_LEDGER_RUNTIME")
    assert "runs from its source folder" in updates.can_install(u)
    monkeypatch.setenv("FORTIS_LEDGER_RUNTIME", "2")
    assert "needs a newer installation of the program" in updates.can_install(u)
    monkeypatch.setenv("FORTIS_LEDGER_RUNTIME", launch.RUNTIME)
    same = updates.read_update(make(tmp_path / "same", version=__version__), TRUST)
    assert updates.can_install(same) == f"this PC already has Fortis Ledger {__version__}"
    with pytest.raises(UpdateError, match="already has"):
        updates.install(same)

    assert updates.can_install(u) == "" and not updates.state()["restart"]
    where = updates.install(u, source="an email")
    assert where == pc / NEXT and f'__version__ = "{NEXT}"' in (where / "fortis_ledger" / "__init__.py").read_text()
    cur = json.loads((pc / "current.json").read_text())
    assert (cur["version"], cur["dir"], cur["runtime"], cur["source"]) == (NEXT, NEXT, launch.RUNTIME, "an email")
    assert updates.state()["restart"] and updates.saved_file(NEXT).read_bytes() == u.path.read_bytes()
    assert not list(pc.glob(".install-*"))

    # the launcher starts the newest update made for its runtime, else the program's own copy
    assert launch.updates_dir() == pc and launch.bundled_version(APP) == __version__
    assert launch.chosen_update(APP) == pc / NEXT
    cur["runtime"] = "2"
    (pc / "current.json").write_text(json.dumps(cur))
    assert launch.chosen_update(APP) is None
    cur["runtime"] = launch.RUNTIME
    (pc / "current.json").write_text(json.dumps(cur))
    updates.use_installed_program()
    assert launch.chosen_update(APP) is None and updates.state()["set_aside"] == "set aside by hand"

    # a later update replaces it; one that changes while it is installed is not installed
    later_file = make(tmp_path / "later", version=LATER)
    later = updates.read_update(later_file, TRUST)
    rewrite(later_file, tmp_path / "swap", lambda n, d: d + b"#" if n == "fortis_ledger/store.py" else d)
    os.replace(tmp_path / "swap", later_file)
    with pytest.raises(UpdateError, match="changed while it was being installed"):
        updates.install(later)
    assert json.loads((pc / "current.json").read_text())["version"] == NEXT and not list(pc.glob(".install-*"))
    later = updates.read_update(make(tmp_path / "later2", version=LATER), TRUST)
    updates.install(later)
    assert launch.chosen_update(APP) == pc / LATER and not (pc / NEXT).exists()
    assert {p.name for p in (pc / "files").iterdir()} == {f"Fortis-Ledger-Update-{v}.fortisupdate" for v in (NEXT, LATER)}


def run_launcher(tmp_path, updates_dir, *args):
    env = {**os.environ, "FORTIS_LEDGER_UPDATES": str(updates_dir), "FORTIS_LEDGER_HOME": str(tmp_path / "config"),
           "PYTHONPATH": str(APP)}
    env.pop("FORTIS_LEDGER_RUNTIME", None)
    return subprocess.run([sys.executable, str(APP / "packaging" / "launch.py"), *args], capture_output=True, text=True,
                          env=env, timeout=120, cwd=tmp_path)


def test_the_launcher_starts_an_update_and_falls_back(tmp_path, pc):
    updates.install(updates.read_update(make(tmp_path / "made"), TRUST))
    r = run_launcher(tmp_path, pc, "--version")
    assert r.returncode == 0 and r.stdout.strip() == f"Fortis Ledger {NEXT}", r.stderr

    # an update that does not start: the program's own copy starts, and the update is set aside
    broken = pc / LATER / "fortis_ledger"
    broken.mkdir(parents=True)
    (broken / "__init__.py").write_text('raise RuntimeError("this update is broken")\n')
    (pc / "current.json").write_text(json.dumps({"version": LATER, "dir": LATER, "runtime": launch.RUNTIME}))
    r = run_launcher(tmp_path, pc, "--version")
    assert r.returncode == 0 and r.stdout.strip() == f"Fortis Ledger {__version__}", r.stderr
    assert json.loads((pc / "current.json").read_text())["bad"] == "RuntimeError: this update is broken"
    r = run_launcher(tmp_path, pc, "--version")
    assert r.stdout.strip() == f"Fortis Ledger {__version__}"


@pytest.fixture
def lib(tmp_path):
    d = tmp_path / "library"
    d.mkdir()
    FolderStore(d, user="Tester").initialise()
    return d


def screen(tmp_path, lib, name="pc1"):
    a = create_app(Settings(data_folder=str(lib), user_name="Tester"), token="tok", work_dir=tmp_path / name)
    a.config.update(TESTING=True, UPDATE_TRUST=TRUST, UPDATE_CHECKS=False)
    a.opened = []
    a.config["OPENER"] = lambda p, folder: a.opened.append((p, folder))
    c = a.test_client()
    c.get("/?token=tok")
    with c.session_transaction() as s:
        c.csrf = s["csrf"]
    return a, c


def post(c, url, data=None, **kw):
    return c.post(url, data={"_csrf": c.csrf, **(data or {})}, **kw)


def text(r):
    """The words on a screen."""
    return html.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", r.get_data(as_text=True))))


def flashes(c):
    with c.session_transaction() as s:
        return [m for _, m in s.get("_flashes", [])]


def choose(c, path: Path, name=None):
    return post(c, "/updates/choose", {"file": (io.BytesIO(path.read_bytes()), name or path.name)},
                content_type="multipart/form-data")


def test_the_updates_screen(tmp_path, pc, lib, monkeypatch):
    a, c = screen(tmp_path, lib)
    page = text(c.get("/updates"))
    assert f"Fortis Ledger {__version__} on this PC" in page and f"Nothing newer than {__version__} has been shared" in page
    path = make(tmp_path / "made")

    choose(c, path, "notes.txt")
    assert "its name ends in .fortisupdate" in flashes(c)[-1]
    bad = rewrite(path, tmp_path / "Fortis-Ledger-Update-x.fortisupdate",
                  lambda n, d: d + b"#" if n == "fortis_ledger/web/views.py" else d,
                  add=[("fortis_ledger/stowaway.py", b"import os")])
    choose(c, bad)
    page = text(c.get("/updates"))
    assert "This file can't be installed" in page and "The files in" in page and "do not match its list." in page
    assert post(c, "/updates/install", {"which": "file"}).status_code == 302 and "Not installed" in flashes(c)[-1]

    # a good one: checked, installed, shared with everyone, and offered for emailing
    choose(c, path)
    page = text(c.get("/updates"))
    assert f"Install Fortis Ledger {NEXT}" in page and "Made by the firm's build" in page
    assert "branch main, commit 0123456, build #42" in page and "Also share it with everyone" in page
    post(c, "/updates/install", {"which": "file", "share": "yes"})
    assert flashes(c)[-1] == (f"Fortis Ledger {NEXT} is installed. Restart Fortis Ledger to start using it. Everyone "
                              f"else who uses the shared folder is offered it too.")
    assert (lib / "updates" / path.name).read_bytes() == path.read_bytes()
    assert FolderStore(lib).read_firm_log()[0]["action"] == "update.shared"
    assert f"Fortis Ledger {NEXT} is installed - restart Fortis Ledger to start using it" in text(c.get("/"))
    page = text(c.get("/updates"))
    assert f"Fortis-Ledger-Update-{NEXT}.fortisupdate" in page and "Restart now" in page
    post(c, "/updates/show", {"version": NEXT})
    assert a.opened[-1] == (pc / "files" / path.name, True) and "Attach it to an email" in flashes(c)[-1]
    post(c, "/updates/restart")
    assert "Close Fortis Ledger and open it again" in flashes(c)[-1]

    # another PC that uses the data folder is offered it, and installs it from there
    monkeypatch.setenv("FORTIS_LEDGER_UPDATES", str(tmp_path / "pc2-updates"))
    a2, c2 = screen(tmp_path, lib, "pc2")
    assert "is available" not in text(c2.get("/"))
    page = text(c2.get("/updates"))
    assert f"Fortis Ledger {NEXT} is ready to install" in page
    assert f"Fortis Ledger {NEXT} is available - an admin shared it with everyone" in text(c2.get("/"))
    post(c2, "/updates/install", {"which": "shared"})
    assert f"Fortis Ledger {NEXT} is installed" in flashes(c2)[-1]
    assert json.loads((tmp_path / "pc2-updates" / "current.json").read_text())["source"] == "the shared folder"

    # an update that needs the full installer says so
    monkeypatch.setenv("FORTIS_LEDGER_UPDATES", str(tmp_path / "pc3-updates"))
    monkeypatch.setattr(make_update, "runtime", lambda: "2")
    shutil.copy(make(tmp_path / "runtime2", version=LATER), lib / "updates")
    a3, c3 = screen(tmp_path, lib, "pc3")
    page = text(c3.get("/updates"))
    assert f"Fortis Ledger {LATER} needs a newer installation of the program" in page
    assert f"Fortis Ledger {NEXT} is ready to install" in page

    # opened by double-clicking the file (or from an email)
    a3.config["UPDATE_FILE"] = str(path)
    page = text(c3.get("/updates"))
    assert f"From {path.name}" in page and f"Install Fortis Ledger {NEXT}" in page


def test_installing_from_the_command_line(tmp_path, pc, monkeypatch, capsys):
    monkeypatch.setattr(updates, "Trust", lambda: TRUST)
    path = make(tmp_path / "made")
    assert desktop.main(["--install-update", str(path), "--yes"]) == 0
    assert f"Installed Fortis Ledger {NEXT} (branch main, commit 0123456, build #42)" in capsys.readouterr().out
    assert json.loads((pc / "current.json").read_text())["version"] == NEXT
    bad = rewrite(path, tmp_path / "bad.fortisupdate", lambda n, d: d + b"#" if n == "fortis_ledger/store.py" else d)
    assert desktop.main(["--install-update", str(bad), "--yes"]) == 1
    assert "Not installed: fortis_ledger/store.py in bad.fortisupdate was changed" in capsys.readouterr().out


def test_making_an_update_needs_github(tmp_path):
    env = {k: v for k, v in os.environ.items() if not k.startswith("ACTIONS_ID_TOKEN")}
    r = subprocess.run([sys.executable, str(APP / "packaging" / "make_update.py"), str(tmp_path), "--require-signature"],
                       capture_output=True, text=True, env=env, timeout=120)
    assert r.returncode == 1 and "no GitHub identity token" in r.stdout
    r = subprocess.run([sys.executable, str(APP / "packaging" / "make_update.py"), str(tmp_path)],
                       capture_output=True, text=True, env=env, timeout=120)
    assert r.returncode == 0 and "UNSIGNED - Fortis Ledger will not install it" in r.stdout
    with pytest.raises(UpdateError, match="is not a signed Fortis Ledger update"):
        updates.read_update(tmp_path / f"Fortis-Ledger-Update-{__version__}.fortisupdate", TRUST)


def test_the_installer_and_the_build_carry_updates():
    iss = (APP / "packaging" / "installer.iss").read_text(encoding="utf-8")
    assert 'Subkey: "Software\\Classes\\.fortisupdate"' in iss and "ChangesAssociations=yes" in iss
    assert '--install-update ""%1""' in iss and updates.EXTENSION == ".fortisupdate"
    spec = (APP / "packaging" / "fortis-ledger.spec").read_text(encoding="utf-8")
    assert "a.pure = [m for m in a.pure if m[0] != \"fortis_ledger\"" in spec
    ci = (APP.parents[1] / ".github" / "workflows" / "fortis-ledger.yml").read_text(encoding="utf-8")
    assert "id-token: write" in ci and "make_update.py" in ci and updates.WORKFLOW == ".github/workflows/fortis-ledger.yml"
