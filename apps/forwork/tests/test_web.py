"""The web app: Microsoft sign-in end to end, sessions and cookies, CSRF, permissions, and the board API."""
import gzip
import io
import json
import urllib.parse
import urllib.request

import pytest

from fake_microsoft import CLIENT, SECRET, TENANT, FakeMicrosoft
from forwork.auth import create_session
from forwork.config import Config
from forwork.db import Database
from forwork.service import Service
from forwork.web import create_app

NOREDIRECT = urllib.request.build_opener(urllib.request.ProxyHandler({}), type("NoRedirect", (urllib.request.HTTPRedirectHandler,),
                                         {"redirect_request": lambda *a, **k: None})())


@pytest.fixture
def ms():
    f = FakeMicrosoft()
    yield f
    f.stop()


def make_cfg(tmp_path, ms=None, **kw):
    c = Config(data_dir=tmp_path / "data", base_url=kw.pop("base_url", "http://localhost"), secret_key="k" * 64,
               ms_tenant_id=TENANT, ms_client_id=CLIENT, ms_client_secret=SECRET, allowed_domains=["fortisap.com.au"], **kw)
    if ms:
        c.ms_authority = ms.url
    c.data_dir.mkdir(parents=True, exist_ok=True)
    return c


@pytest.fixture
def env(tmp_path, ms):
    cfg = make_cfg(tmp_path, ms)
    db = Database(cfg.db_path)
    app = create_app(cfg, db=db)
    app.testing = True
    svc = Service(db)
    admin = svc.create_user(None, "alex@fortisap.com.au", "Alex Citizen", role="admin")
    ws = svc.create_workspace(admin, "Main workspace")
    bid = svc.create_board(admin, "Annual Compliance", ws)
    return {"app": app, "cfg": cfg, "db": db, "svc": svc, "admin": admin, "board": bid, "ms": ms}


def signed_in(env, user):
    client = env["app"].test_client()
    token, csrf = create_session(env["db"], user["id"])
    client.set_cookie("forwork_session", token)
    client.csrf = csrf
    return client


def api(client, method, url, body=None):
    return client.open(url, method=method, json=body, headers={"X-CSRF-Token": client.csrf})


def ms_sign_in(env, client):
    r = client.get("/auth/microsoft?next=/my-work")
    assert r.status_code == 302 and r.headers["Location"].startswith(env["ms"].url)
    try:
        NOREDIRECT.open(r.headers["Location"])
        raise AssertionError("the fake should redirect")
    except urllib.error.HTTPError as e:
        back = e.headers["Location"]
    parts = urllib.parse.urlsplit(back)
    assert parts.path == "/auth/callback"
    return client.get(parts.path + "?" + parts.query)


# ---- signing in -------------------------------------------------------------------------------------------- #
def test_pages_need_sign_in(env):
    c = env["app"].test_client()
    r = c.get("/boards/1")
    assert r.status_code == 302 and "/signin" in r.headers["Location"]
    assert c.get("/api/boards").status_code == 401
    assert c.get("/health").status_code == 200
    r = c.get("/signin")
    assert b"Sign in with Microsoft" in r.data


def test_microsoft_sign_in_adds_a_member_and_sets_a_safe_cookie(env):
    c = env["app"].test_client()
    r = ms_sign_in(env, c)
    assert r.status_code == 302 and r.headers["Location"] == "/my-work"
    cookie = [h for h in r.headers.getlist("Set-Cookie") if h.startswith("forwork_session=")][0]
    assert "HttpOnly" in cookie and "SameSite=Lax" in cookie
    u = env["svc"].user_by_email("jo@fortisap.com.au")
    assert u["role"] == "member" and u["ms_oid"] == "oid-jo"
    assert c.get("/my-work").status_code == 200
    # the database keeps only a hash of the cookie
    token = cookie.split(";")[0].split("=", 1)[1]
    assert not env["db"].one("SELECT 1 FROM sessions WHERE id = ?", (token,))


@pytest.mark.parametrize("change, message", [
    ({"tid": "99999999-2222-3333-4444-555555555555"}, "not in the firm"),
    ({"email": "jo@gmail.com"}, "not a firm email"),
])
def test_wrong_tenant_or_domain_is_refused(env, change, message):
    env["ms"].account.update(change)
    c = env["app"].test_client()
    r = ms_sign_in(env, c)
    assert r.status_code == 302 and "/signin?error=" in r.headers["Location"]
    assert message in urllib.parse.unquote_plus(r.headers["Location"])
    assert c.get("/api/boards").status_code == 401


def test_turned_off_people_cannot_sign_in_and_are_signed_out(env):
    jo = env["svc"].create_user(env["admin"], "jo@fortisap.com.au", "Jo Citizen")
    client = signed_in(env, jo)
    assert client.get("/api/boards").status_code == 200
    env["svc"].update_user(env["admin"], jo["id"], active=False)
    assert client.get("/api/boards").status_code == 401
    r = ms_sign_in(env, env["app"].test_client())
    assert "turned off" in urllib.parse.unquote_plus(r.headers["Location"])


def test_a_bad_client_secret_is_explained(env):
    env["ms"].fail_token = "AADSTS7000215: Invalid client secret provided."
    r = ms_sign_in(env, env["app"].test_client())
    assert "client secret" in urllib.parse.unquote_plus(r.headers["Location"])


def test_callback_without_a_started_sign_in_is_refused(env):
    r = env["app"].test_client().get("/auth/callback?code=abc&state=xyz")
    assert "sign in again" in urllib.parse.unquote_plus(r.headers["Location"])


def test_only_firm_admins_listed_become_admins(env, tmp_path):
    env["cfg"].admins = ["jo@fortisap.com.au"]
    ms_sign_in(env, env["app"].test_client())
    assert env["svc"].user_by_email("jo@fortisap.com.au")["role"] == "admin"


def test_https_uses_secure_host_cookie(tmp_path, ms):
    cfg = make_cfg(tmp_path, ms, base_url="https://forwork.example.com.au")
    app = create_app(cfg, db=Database(cfg.db_path))
    c = app.test_client()
    r = c.get("/auth/microsoft", base_url="https://forwork.example.com.au")
    assert "redirect_uri=https%3A%2F%2Fforwork.example.com.au%2Fauth%2Fcallback" in r.headers["Location"]
    try:
        NOREDIRECT.open(r.headers["Location"])
    except urllib.error.HTTPError as e:
        back = urllib.parse.urlsplit(e.headers["Location"])
    r = c.get(back.path + "?" + back.query, base_url="https://forwork.example.com.au")
    cookie = [h for h in r.headers.getlist("Set-Cookie") if h.startswith("__Host-forwork=")][0]
    assert "Secure" in cookie and "Path=/" in cookie
    assert "Strict-Transport-Security" in r.headers


def test_dev_sign_in_only_when_on_and_only_from_this_pc(env):
    c = env["app"].test_client()
    assert c.post("/auth/dev", data={"user": env["admin"]["id"]}).status_code in (400, 404)
    env["cfg"].dev_login = True
    r = c.post("/auth/dev", data={"user": env["admin"]["id"]}, environ_base={"REMOTE_ADDR": "10.1.2.3"})
    assert r.status_code == 404
    r = c.post("/auth/dev", data={"user": env["admin"]["id"]}, environ_base={"REMOTE_ADDR": "127.0.0.1"})
    assert r.status_code == 302 and c.get("/api/boards").status_code == 200


def test_serve_refuses_dev_login_on_a_network_address(tmp_path, monkeypatch):
    from forwork.__main__ import main
    monkeypatch.setenv("FORWORK_DEV_LOGIN", "1")
    assert main(["--data", str(tmp_path / "d"), "serve", "--host", "0.0.0.0"]) == 2


def test_security_headers(env):
    r = signed_in(env, env["admin"]).get("/")
    csp = r.headers["Content-Security-Policy"]
    assert "script-src 'self'" in csp and "frame-ancestors 'none'" in csp and "unsafe-inline" not in csp
    assert r.headers["X-Frame-Options"] == "DENY" and r.headers["X-Content-Type-Options"] == "nosniff"


def test_changes_need_the_csrf_token(env):
    c = signed_in(env, env["admin"])
    r = c.post(f"/api/boards/{env['board']}/items", json={"name": "X"})
    assert r.status_code == 400 and "expired" in r.get_json()["error"]
    r = c.post(f"/api/boards/{env['board']}/items", json={"name": "X"}, headers={"X-CSRF-Token": "wrong"})
    assert r.status_code == 400
    assert api(c, "POST", f"/api/boards/{env['board']}/items", {"name": "X"}).status_code == 200


def test_sign_out_ends_the_session(env):
    c = signed_in(env, env["admin"])
    c.post("/signout", data={"_csrf": c.csrf})
    assert c.get("/api/boards").status_code == 401


# ---- permissions ------------------------------------------------------------------------------------------- #
def test_viewers_read_but_cannot_change(env):
    v = env["svc"].create_user(env["admin"], "sam@fortisap.com.au", "Sam Viewer", role="viewer")
    c = signed_in(env, v)
    assert c.get(f"/api/boards/{env['board']}").get_json()["can_edit"] is False
    r = api(c, "POST", f"/api/boards/{env['board']}/items", {"name": "X"})
    assert r.status_code == 403


def test_admin_pages_are_for_admins(env):
    m = env["svc"].create_user(env["admin"], "max@fortisap.com.au", "Max Member")
    assert signed_in(env, m).get("/admin/people").status_code == 403
    a = signed_in(env, env["admin"])
    for page in ("/admin/people", "/admin/settings", "/admin/backups", "/admin/import", "/admin/bin"):
        assert a.get(page).status_code == 200, page


# ---- the board API ------------------------------------------------------------------------------------------ #
def test_board_round_trip(env):
    c = signed_in(env, env["admin"])
    bid = env["board"]
    p = c.get(f"/api/boards/{bid}").get_json()
    status = next(col for col in p["columns"] if col["title"] == "Status")
    group = p["groups"][0]["id"]
    r = api(c, "POST", f"/api/boards/{bid}/items", {"name": "Example Pty Ltd", "group_id": group}).get_json()
    iid = r["id"]
    seq = c.get(f"/api/boards/{bid}").get_json()["board"]["seq"]
    r = api(c, "POST", f"/api/items/{iid}/values", {"column": status["id"], "value": 1})
    assert r.status_code == 200 and r.get_json()["items"][0]["values"][str(status["id"])] == 1
    r = api(c, "POST", f"/api/items/{iid}/values", {"column": status["id"], "value": "Nope"})
    assert r.status_code == 400 and "not one of" in r.get_json()["error"]
    ch = c.get(f"/api/boards/{bid}/changes?since={seq}").get_json()
    assert [i["id"] for i in ch["items"]] == [iid]
    g2 = api(c, "POST", f"/api/boards/{bid}/groups", {"title": "Review", "where": "bottom"}).get_json()["id"]
    api(c, "POST", f"/api/items/{iid}/move", {"group_id": g2})
    assert env["svc"].item(iid)["group_id"] == g2
    col = api(c, "POST", f"/api/boards/{bid}/columns", {"type": "numbers", "title": "Fee"}).get_json()["id"]
    api(c, "POST", f"/api/items/{iid}/values", {"column": col, "value": "1,250"})
    detail = c.get(f"/api/items/{iid}").get_json()
    assert detail["item"]["values"][str(col)] == 1250
    assert any("Fee" in a["text"] for a in detail["activity"])
    v = api(c, "POST", f"/api/boards/{bid}/views", {"name": "Done only", "settings": {"filters": {"rules": [
        {"column": status["id"], "values": [1]}]}}}).get_json()
    assert v["views"][0]["name"] == "Done only"
    assert c.get(f"/api/boards/{bid}/changes?since={seq}").get_json()["reload"]      # groups/columns/views changed
    seq = c.get(f"/api/boards/{bid}").get_json()["board"]["seq"]
    assert api(c, "POST", f"/api/items/{iid}/state", {"state": "deleted"}).status_code == 200
    assert iid in c.get(f"/api/boards/{bid}/changes?since={seq}").get_json()["removed"]


def test_big_board_json_is_compressed(env):
    c = signed_in(env, env["admin"])
    for n in range(150):
        env["svc"].create_item(env["admin"], env["board"], f"Client number {n} Pty Ltd")
    r = c.get(f"/api/boards/{env['board']}", headers={"Accept-Encoding": "gzip"})
    assert r.headers.get("Content-Encoding") == "gzip"
    assert len(json.loads(gzip.decompress(r.data))["items"]) == 150


def test_updates_with_mentions_notify(env):
    jo = env["svc"].create_user(env["admin"], "jo@fortisap.com.au", "Jo Citizen")
    c = signed_in(env, env["admin"])
    iid = env["svc"].create_item(env["admin"], env["board"], "Example Pty Ltd")
    body = f'<p>Can you look <span class="mention" data-user="{jo["id"]}">@Jo</span>? <img src=x onerror=alert(1)></p>'
    r = api(c, "POST", f"/api/items/{iid}/updates", {"body": body}).get_json()
    assert "onerror" not in r["updates"][0]["body"] and "<img" not in r["updates"][0]["body"]
    jc = signed_in(env, jo)
    n = jc.get("/api/notifications").get_json()
    assert n["unread"] == 1 and "mentioned you" in n["notifications"][0]["text"]
    assert b"mentioned you" in jc.get("/inbox").data


def test_files_upload_and_download_safely(env):
    c = signed_in(env, env["admin"])
    iid = env["svc"].create_item(env["admin"], env["board"], "Example Pty Ltd")
    r = c.post(f"/api/items/{iid}/files", data={"file": (io.BytesIO(b"<script>alert(1)</script>"), "notes.html")},
               headers={"X-CSRF-Token": c.csrf}, content_type="multipart/form-data")
    fid = r.get_json()["file"]["id"]
    d = c.get(f"/files/{fid}")
    assert d.status_code == 200 and d.data == b"<script>alert(1)</script>"
    assert d.headers["Content-Type"] == "application/octet-stream" and "attachment" in d.headers["Content-Disposition"]
    assert "sandbox" in d.headers["Content-Security-Policy"]


def test_pages_render_with_data(env):
    c = signed_in(env, env["admin"])
    svc, bid = env["svc"], env["board"]
    owner = next(col for col in svc.columns(bid) if col["title"] == "Owner")
    iid = svc.create_item(env["admin"], bid, "Example Pty Ltd", values={owner["id"]: {"users": [env["admin"]["id"]]}})
    for url in ("/", "/my-work", f"/boards/{bid}", f"/boards/{bid}/activity", f"/boards/{bid}/automations",
                "/search?q=example", "/inbox", "/archive", f"/items/{iid}"):
        r = c.get(url, follow_redirects=True)
        assert r.status_code == 200, url
    assert b"Example Pty Ltd" in c.get("/my-work").data
    x = c.get(f"/boards/{bid}/export.xlsx")
    assert x.status_code == 200 and x.data[:2] == b"PK"


def test_automation_form(env):
    c = signed_in(env, env["admin"])
    svc, bid = env["svc"], env["board"]
    status = next(col for col in svc.columns(bid) if col["title"] == "Status")
    review = svc.add_group(env["admin"], bid, "Review")
    r = c.post(f"/boards/{bid}/automations", data={"_csrf": c.csrf, "trigger": "status_change", "t_column": status["id"],
                                                    "t_to": "1", "action": "move_to_group", "a_group": review})
    assert r.status_code == 302
    iid = svc.create_item(env["admin"], bid, "X")
    api(c, "POST", f"/api/items/{iid}/values", {"column": status["id"], "value": 1})
    assert svc.item(iid)["group_id"] == review


def test_first_admin_only_on_an_empty_forwork(tmp_path, ms):
    cfg = make_cfg(tmp_path, ms)
    db = Database(cfg.db_path)
    app = create_app(cfg, db=db)
    env = {"app": app, "db": db, "ms": ms}
    ms_sign_in(env, app.test_client())                     # nobody yet: the first person is the admin
    assert Service(db).user_by_email("jo@fortisap.com.au")["role"] == "admin"
    cfg2 = make_cfg(tmp_path / "second", ms)
    db2 = Database(cfg2.db_path)
    Service(db2).create_user(None, "imported@fortisap.com.au", "Imported Person")      # e.g. after a monday import
    env2 = {"app": create_app(cfg2, db=db2), "db": db2, "ms": ms}
    ms_sign_in(env2, env2["app"].test_client())
    assert Service(db2).user_by_email("jo@fortisap.com.au")["role"] == "member"


@pytest.mark.parametrize("nxt, ok", [("/boards/3?item=4", True), ("//evil.example", False), ("/\\evil.example", False),
                                     ("/\t/evil.example", False), ("https://evil.example", False), ("javascript:x", False)])
def test_sign_in_only_returns_to_this_site(env, nxt, ok):
    from forwork.web.pages import safe_next
    with env["app"].test_request_context():
        assert (safe_next(nxt) == nxt) is ok
