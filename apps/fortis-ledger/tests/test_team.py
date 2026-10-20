"""The team: who may use the shared data folder and open which client (on the screens and in Claude), the live notes
that tell an open screen about a colleague's change, and sharing the data folder (where it is, moving it into a
SharePoint library, the invite email).  The people are made up."""
import html
import json
import re
from urllib.parse import parse_qs, urlsplit

import pytest

from fortis_ledger.connector import Connector
from fortis_ledger.settings import Settings
from fortis_ledger.store import FolderStore
from fortis_ledger.team import Team
from fortis_ledger.web import create_app
from fortis_ledger.web.teamviews import folder_status

TOKEN = "tok"
ADMIN, STAFF, OTHER, NEW = ("pat.admin@example.com.au", "sam.staff@example.com.au", "lee.other@example.com.au",
                            "kim.new@example.com.au")


@pytest.fixture
def lib(tmp_path, monkeypatch):
    monkeypatch.setenv("FORTIS_LEDGER_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))           # Path.home() on Windows
    for var in ("OneDrive", "OneDriveCommercial"):
        monkeypatch.delenv(var, raising=False)
    d = tmp_path / "library"
    d.mkdir()
    st = FolderStore(d, user="Tester")
    st.initialise()
    for name in ("Alpha Pty Ltd", "Beta Trust", "Gamma Partners"):
        st.create_client(name, "company")
    return d


@pytest.fixture
def app(tmp_path, lib):
    a = create_app(Settings(data_folder=str(lib), user_name="Tester"), token=TOKEN, work_dir=tmp_path / "work")
    a.config.update(TESTING=True, UPDATE_CHECKS=False)
    a.opened = []
    a.config["BROWSER"] = a.opened.append
    return a


def require_signin(lib):
    FolderStore(lib).save_firm_settings({"microsoft": {"tenant_id": "11111111-2222-3333-4444-555555555555",
                                                       "client_id": "66666666-7777-8888-9999-000000000000",
                                                       "allowed_domains": ["example.com.au"], "required": True}})


def person(app, email=None, name=""):
    """A PC with Fortis Ledger open, signed in with this office email (None: nobody signed in)."""
    c = app.test_client()
    c.get(f"/?token={TOKEN}")
    with c.session_transaction() as s:
        c.csrf = s["csrf"]
        if email:
            s["ms_user"] = {"email": email, "name": name}
    return c


def post(c, url, data=None, **kw):
    return c.post(url, data={"_csrf": c.csrf, **(data or {})}, **kw)


def flashes(c):
    with c.session_transaction() as s:
        return [m for _, m in s.get("_flashes", [])]


def text(r):
    return html.unescape(re.sub(r"\s+", " ", r.get_data(as_text=True)))


def claude(settings, tool, **args):
    """A tool call from Claude on that person's PC: (result, is_error)."""
    res = Connector(settings).handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                      "params": {"name": tool, "arguments": args}})["result"]
    out = res["content"][0]["text"]
    return (out if res["isError"] else json.loads(out)), res["isError"]


def team_file(lib) -> dict:
    return json.loads((lib / "team.json").read_text(encoding="utf-8"))


def slug(lib, name) -> str:
    return next(c.slug for c in FolderStore(lib).list_clients() if c.name == name)


def listed(c, lib) -> set[str]:
    """The clients on the client list."""
    names = {x.slug: x.name for x in FolderStore(lib).list_clients()}
    return {names[s] for s in re.findall(r'href="/c/([a-z0-9-]+)/"', c.get("/").get_data(as_text=True))}


def test_team_rules():
    t = Team.load({"managed": True, "join": "nonsense", "people": {
        " Pat.Admin@Example.com.au ": {"name": "Pat", "role": "admin"}, STAFF: {"name": "Sam", "role": "staff"},
        OTHER: {"role": "boss"}, "": {"role": "staff"}}, "clients": {"beta": [STAFF.upper(), ""], "bad": "x"}})
    assert t.join == "auto" and set(t.people) == {ADMIN, STAFF} and t.clients == {"beta": [STAFF]}
    assert t.is_admin(ADMIN) and not t.is_admin(STAFF) and t.can_use(STAFF) and not t.can_use(OTHER)
    t.limit_client("gamma", [ADMIN])
    assert t.can_open(STAFF, "alpha") and t.can_open(STAFF, "beta") and not t.can_open(STAFF, "gamma")
    assert t.can_open(ADMIN, "gamma") and not t.can_open(OTHER, "alpha") and not t.can_open("", "alpha")
    assert t.summary(STAFF, ["alpha", "beta", "gamma"]) == "every client except 1 limited to others"
    assert t.summary(ADMIN, ["gamma"]) == "every client" and t.summary(OTHER, []) == "nothing"
    with pytest.raises(ValueError, match="at least one admin"):
        t.set_person(ADMIN, "", "staff", by=ADMIN, at="now")
    with pytest.raises(ValueError, match="at least one admin"):
        t.remove_person(ADMIN)
    with pytest.raises(ValueError, match="email"):
        t.set_person("not an email", "", "staff", by=ADMIN, at="now")
    t.remove_person(STAFF)
    assert t.clients["beta"] == [] and not t.can_open(STAFF, "alpha")
    t.limit_client("beta", None)
    assert "beta" not in t.clients
    # access that is not managed lets everyone open everything
    assert Team().can_open("anyone", "alpha") and Team().is_admin("") and Team().can_use("")


def test_admins_manage_who_opens_which_client(app, lib):
    admin = person(app, ADMIN, "Pat Admin")
    # access can only be managed when everyone is known by their office email
    assert "office sign-in must be required first" in text(admin.get("/team"))
    post(admin, "/team", {"op": "on", "name": "Pat Admin"})
    assert not (lib / "team.json").exists() and "Make office sign-in required first" in flashes(admin)[-1]

    require_signin(lib)
    page = text(admin.get("/team"))
    assert "Manage access" in page and "You (pat.admin@example.com.au) become the first admin" in page
    post(admin, "/team", {"op": "on", "name": "Pat Admin", "join": "auto"})
    t = team_file(lib)
    assert t["managed"] and t["people"][ADMIN]["role"] == "admin" and t["updated"]["by"] == ADMIN

    # a colleague who signs in joins as staff and sees every client
    staff = person(app, STAFF, "Sam Staff")
    assert listed(staff, lib) == {"Alpha Pty Ltd", "Beta Trust", "Gamma Partners"}
    assert team_file(lib)["people"][STAFF]["role"] == "staff"
    assert "Sam Staff joined as staff at their first sign-in" in text(admin.get("/team"))

    # the admin adds Lee and limits Beta Trust to Lee
    beta = slug(lib, "Beta Trust")
    post(admin, "/team", {"op": "person", "email": OTHER, "name": "Lee Other", "role": "staff"})
    assert "Lee Other is added as staff." in flashes(admin)[-1]
    page = text(admin.get(f"/c/{beta}/details"))
    assert 'id="access"' in page and "Admins (Pat Admin) can always open it" in page
    post(admin, f"/team/access/{beta}", {"mode": "named", "people": [OTHER, ADMIN, "stranger@example.com.au"]})
    assert team_file(lib)["clients"] == {beta: [OTHER]}
    assert "Beta Trust is now open only to Lee Other and the admins." in flashes(admin)[-1]

    # Sam no longer sees it, on any screen - and nor does Claude working for Sam
    assert listed(staff, lib) == {"Alpha Pty Ltd", "Gamma Partners"}
    for url in (f"/c/{beta}/", f"/c/{beta}/journals", f"/pulse/{beta}"):
        r = staff.get(url)
        assert r.status_code == 403, url
    assert "You do not have access to this client. Ask Pat Admin" in text(staff.get(f"/c/{beta}/"))
    assert post(staff, f"/c/{beta}/details", {"name": "Hacked"}).status_code == 403
    assert listed(person(app, OTHER, "Lee Other"), lib) == {"Alpha Pty Ltd", "Beta Trust", "Gamma Partners"}

    sams_pc = Settings(data_folder=str(lib), user_name="Sam", last_email=STAFF)
    out, err = claude(sams_pc, "list_clients")
    assert not err and {c["name"] for c in out["clients"]} == {"Alpha Pty Ltd", "Gamma Partners"}
    out, err = claude(sams_pc, "get_trial_balance", client="Beta Trust")
    assert err and "No client matches" in out

    # only admins change the team, firm settings and access
    assert post(staff, "/team", {"op": "person", "email": STAFF, "role": "admin"}).status_code == 403
    assert post(staff, f"/team/access/{beta}", {"mode": "everyone"}).status_code == 403
    assert staff.get("/firm").status_code == 403
    assert team_file(lib)["people"][STAFF]["role"] == "staff"
    staff_page = text(staff.get("/team"))
    assert "Pat Admin" in staff_page and "Stop managing access" not in staff_page and "Copy and switch" not in staff_page
    assert "Firm settings" not in staff_page and "Firm settings" in text(admin.get("/team"))

    # the last admin stays an admin, and sign-in stays required while access is managed
    post(admin, "/team", {"op": "person", "email": ADMIN, "role": "staff"})
    assert "keep at least one admin" in flashes(admin)[-1] and team_file(lib)["people"][ADMIN]["role"] == "admin"
    post(admin, "/firm", {"op": "ms_off"})
    assert "must stay required" in flashes(admin)[-1]
    assert FolderStore(lib).firm_settings()["microsoft"]["required"] is True

    # new people wait for an admin to add them; "No access" shuts someone out
    post(admin, "/team", {"op": "join", "join": "admin"})
    r = person(app, NEW, "Kim New").get("/")
    assert r.status_code == 403 and "cannot use the firm's Fortis Ledger clients yet. Ask Pat Admin" in text(r)
    assert NEW not in team_file(lib)["people"]
    post(admin, "/team", {"op": "person", "email": STAFF, "role": "none"})
    assert person(app, STAFF, "Sam Staff").get("/").status_code == 403
    out, err = claude(sams_pc, "list_clients")
    assert err and "cannot use this firm's Fortis Ledger clients - ask Pat Admin" in out

    # everything is in the firm's change log
    log = text(admin.get("/team"))
    for line in ("started managing access", "Lee Other: Staff (added)", "Beta Trust limited to Lee Other",
                 "new people now need an admin to add them", "Sam Staff: No access (was Staff)"):
        assert line in log, line

    # stop managing: everyone opens everything again
    post(admin, "/team", {"op": "off"})
    assert not team_file(lib)["managed"]
    assert listed(person(app, STAFF, "Sam Staff"), lib) == {"Alpha Pty Ltd", "Beta Trust", "Gamma Partners"}
    assert claude(sams_pc, "list_clients")[0]["count"] == 3


def test_live_notes_about_colleagues_changes(app, lib):
    alpha, beta = slug(lib, "Alpha Pty Ltd"), slug(lib, "Beta Trust")
    me = person(app)
    page = me.get(f"/c/{alpha}/").get_data(as_text=True)
    since = re.search(r'data-pulse-since="([^"]+)"', page).group(1)
    assert f'data-pulse="/pulse/{alpha}"' in page and 'id="live-banner"' in page and "data-pulse-ro" not in page

    def note(**q):
        return me.get(f"/pulse/{alpha}", query_string={"since": since, **q}).get_json()["message"]

    assert note() == ""
    FolderStore(lib, user="Tester").audit(alpha, "client.updated", {"name": "Alpha Pty Ltd"})
    assert note() == ""                                              # my own change on this PC
    FolderStore(lib, user="Robin Colleague", machine="pc2").audit(alpha, "journal.posted",
                                                                  {"number": "J0007", "narration": "Accruals"})
    assert note().startswith("Robin Colleague has just changed this client: journal posted")
    FolderStore(lib, user="Tester", agent="claude").audit(alpha, "journal.draft", {"number": "J0008"})
    assert note().startswith("Claude (for Tester) has just changed this client") and "(and 1 more change)" in note()

    # read only while a colleague edits: the screen is told when they have finished
    colleague = FolderStore(lib, user="Robin Colleague", machine="pc2")
    assert colleague.acquire_lock(beta)[0]
    page = me.get(f"/c/{beta}/").get_data(as_text=True)
    assert 'data-pulse-ro="1"' in page
    since = re.search(r'data-pulse-since="([^"]+)"', page).group(1)
    assert me.get(f"/pulse/{beta}", query_string={"since": since, "ro": "1"}).get_json()["message"] == ""
    colleague.release_lock(beta)
    assert "has finished - refresh to make changes" in me.get(f"/pulse/{beta}",
                                                              query_string={"since": since, "ro": "1"}).get_json()["message"]

    # the client list notices a new client, or someone starting work on one
    page = me.get("/").get_data(as_text=True)
    sig = re.search(r'data-pulse-since="([^"]+)"', page).group(1)
    assert 'data-pulse="/pulse"' in page and 'data-pulse-every="30"' in page
    assert me.get("/pulse", query_string={"since": sig}).get_json()["message"] == ""
    colleague.create_client("Delta Pty Ltd", "company")
    assert "The client list has changed" in me.get("/pulse", query_string={"since": sig}).get_json()["message"]


def test_sharing_the_data_folder(app, lib, tmp_path, monkeypatch):
    home = tmp_path / "home"
    org = home / "Example Firm"
    library = org / "Ledger - Documents"
    library.mkdir(parents=True)
    (home / "OneDrive - Example Firm" / "Mine").mkdir(parents=True)
    monkeypatch.setenv("OneDriveCommercial", str(home / "OneDrive - Example Firm"))
    assert folder_status(str(lib)) == {"kind": "local", "org": "Example Firm", "synced": str(org)}
    assert folder_status(str(home / "OneDrive - Example Firm" / "Mine"))["kind"] == "onedrive"
    assert folder_status(str(library / "Clients"))["library"] == "Ledger - Documents"

    c = person(app)
    page = text(c.get("/team"))
    assert "Only on this PC" in page and "Move the data folder into SharePoint" in page and str(org) in page

    # the invite opens in the email program
    post(c, "/team/invite")
    q = parse_qs(urlsplit(app.opened[-1]).query)
    assert app.opened[-1].startswith("mailto:?") and q["subject"] == ["Fortis Ledger - getting started"]
    assert "Install it: run Fortis-Ledger-Setup-" in q["body"][0]

    # moving: not into the data folder itself, nor a folder that is not there
    post(c, "/team/move", {"dest": str(lib / "clients")})
    assert "outside the current data folder" in flashes(c)[-1]
    post(c, "/team/move", {"dest": str(tmp_path / "nowhere")})
    assert "it must already exist" in flashes(c)[-1]
    FolderStore(tmp_path / "another", user="Tester").initialise()
    post(c, "/team/move", {"dest": str(tmp_path / "another")})
    assert "already a Fortis Ledger data folder" in flashes(c)[-1]
    gamma = slug(lib, "Gamma Partners")
    FolderStore(lib, user="Robin Colleague", machine="pc2").acquire_lock(gamma)

    post(c, "/team/move", {"dest": str(library)})
    assert "3 client(s) copied" in flashes(c)[-1]
    assert app.config["SETTINGS"].data_folder == str(library) and Settings.load().data_folder == str(library)
    moved = FolderStore(library)
    assert sorted(x.name for x in moved.list_clients()) == ["Alpha Pty Ltd", "Beta Trust", "Gamma Partners"]
    assert not (library / "clients" / gamma / "lock.json").exists()          # edit locks stay behind
    assert (lib / "clients" / gamma / "client.json").exists()                  # the old folder is left
    assert moved.read_firm_log()[0]["action"] == "data.moved"
    page = text(c.get("/team"))
    assert "In SharePoint: Ledger - Documents" in page and "moved the data folder to" in page
    assert "open the Ledger - Documents library in SharePoint" in parse_qs(urlsplit(
        (post(c, "/team/invite"), app.opened[-1])[1]).query)["body"][0]

    # a non-empty folder gets a "Fortis Ledger" folder of its own
    other = tmp_path / "home" / "Example Firm" / "Shared - Documents"
    (other / "Other files").mkdir(parents=True)
    post(c, "/team/move", {"dest": str(other)})
    assert app.config["SETTINGS"].data_folder == str(other / "Fortis Ledger")
