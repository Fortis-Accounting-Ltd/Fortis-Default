"""The launcher as people run it: `python -m fortis_ledger` (self-test, and the server on 127.0.0.1)."""
import http.cookiejar
import os
import re
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

APP = Path(__file__).resolve().parents[1]


def env_for(tmp_path):
    return {**os.environ, "FORTIS_LEDGER_HOME": str(tmp_path / "config"), "HOME": str(tmp_path / "home"),
            "PYTHONPATH": str(APP)}


def test_self_test_passes(tmp_path):
    r = subprocess.run([sys.executable, "-m", "fortis_ledger", "--self-test"], cwd=APP, env=env_for(tmp_path),
                       capture_output=True, text=True, timeout=300)
    assert r.returncode == 0, r.stdout + r.stderr
    report = (tmp_path / "config" / "self-test.txt").read_text(encoding="utf-8")
    assert report.strip().endswith("PASSED") and report.count(": ok") == 5 and "\nscreens: ok" in report


def test_server_answers_only_with_the_token(tmp_path):
    lib = tmp_path / "library"
    lib.mkdir()
    p = subprocess.Popen([sys.executable, "-m", "fortis_ledger", "--no-open", "--data", str(lib)], cwd=APP,
                         env=env_for(tmp_path), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        url, deadline = None, time.time() + 60
        while time.time() < deadline and not url:
            line = p.stdout.readline()
            m = re.search(r"(http://127\.0\.0\.1:\d+)/\?token=([\w-]+)", line or "")
            if m:
                url = m
        assert url, "the server did not start"
        base, token = url.group(1), url.group(2)
        try:
            urllib.request.urlopen(base + "/")
            raise AssertionError("answered without the token")
        except urllib.error.HTTPError as e:
            assert e.code == 403
        jar = http.cookiejar.CookieJar()
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
        with opener.open(f"{base}/?token={token}") as r:      # new data folder -> the settings screen
            assert r.status == 200 and "<h1>Settings</h1>" in r.read().decode()
        with opener.open(f"{base}/static/app.js") as r:
            assert r.status == 200
    finally:
        p.terminate()
        p.wait(timeout=20)
