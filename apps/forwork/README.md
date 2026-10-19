# ForWork

Fortis Accounting Partners' own work boards: the firm's version of monday.com. It's a browser app the whole firm
signs in to with their Microsoft 365 email. It is built the way Fortis Ledger was (Python, Flask, plain HTML,
CSS and JavaScript, no outside frameworks, everything tested), but it runs on one server so everyone works on the
same boards at the same time.

- **To put it online, follow `SETUP-GUIDE.md`.** It takes about an hour and needs no coding.
- **To see how the firm uses monday.com, and what came across, read `MONDAY-REVIEW.md`.**

## What it does

| | |
|---|---|
| **Boards** | Workspaces and folders of boards, like monday.com. A board has groups (pipeline stages or periods) of items (a client's job, a BAS, an SMSF audit). Favourites, archived boards, a bin, and *Duplicate board* (structure, items, updates) for next year's board. |
| **Columns** | Status (coloured labels, "done" labels, hidden labels), people and teams, date, timeline, numbers (with a unit), text, long text, dropdown, checkbox, link, email, phone, location, files, connect boards (links to items on other boards), mirror (shows a column of the linked items), formula, subitems progress, creation log and last updated. Every type the firm uses on monday.com. |
| **Editing** | Click a cell to change it: a label picker, a people picker, a date picker and so on. Rename items and groups in place, drag items between groups, add items and subitems at the bottom of a group, resize, reorder, hide and add columns, edit labels and colours. |
| **Views** | The main table plus saved views: column order, hidden columns, filters (people, status, dropdown, "assigned to me"), sort, **group by** a column (status, person ...), and **kanban** (cards by status, drag to change). A search box and a person filter on every board. |
| **Updates** | Each item's updates: rich text, @mentions, replies, files, edit and delete. Mentions, replies and being added to an item land in the person's **Inbox**. |
| **My work** | Everything on open boards where you are in a people column, by due date: overdue, today, this week, next week, later. |
| **Automations** | "When Status changes to X, move the item to group Y / set a date to today / set another status / notify people / add the creator / archive it", and "when an item is created ...". The monday.com rules come across and keep running. |
| **Live** | Changes others make appear on your screen within about five seconds, without reloading. |
| **Activity log** | Every change: who, when, what it was before. Per board and per item. |
| **Export** | Any board to Excel. Every night a readable JSON copy of all boards goes into the backup. |
| **Admin** | People and roles (admin, member, viewer), turning access off, signing people out, backup settings and restore, importing from monday.com, the bin. |

## How it's built

```
browser ──HTTPS──> ForWork (Flask on waitress, one process) ──> forwork.db (SQLite, server disk)
   │                     │                                         │
   │                     └─ sign-in ─> Microsoft Entra ID          └─ every hour / night ─> OneDrive or SharePoint
   └─ boards load once, then ask "what changed since #n?" every 5 s              (Microsoft Graph, or a synced folder)
```

- **One SQLite database** (`forwork.db`) in the data folder on the server. That suits a firm of our size: no database
  server to look after, and one file to back up. Writes are queued through one lock, so two people saving at the same
  moment just wait a few milliseconds.
- **Why the live database isn't kept in OneDrive:** a sync app copying a database while it's in use can corrupt it.
  Instead, ForWork makes a consistent snapshot (`forwork-YYYYMMDD-HHMMSSZ.db.gz`) every hour when something has
  changed and every night at 2 am regardless. It also writes a readable `*.json.gz` copy of every board and uploads
  them to OneDrive or a SharePoint library through Microsoft Graph, or into a OneDrive-synced folder on an office PC.
  Attached files go up once each. Retention: everything for 48 hours, then one a day for 60 days, then one a month
  for 2 years. *Admin > Backups* shows the last result, can back up now, and can restore (the data as it was just
  before a restore is kept, so a restore can itself be undone).
- **Files** attached to items are stored by their SHA-256 under `files/` and are only ever served as downloads
  (images and PDFs open in the browser, in a sandbox).

### Security

| Control | Where |
|---|---|
| Sign-in only with the firm's Microsoft 365 (OIDC code flow with PKCE and a client secret); tenant, audience, nonce and email domain checked | `forwork/auth.py` |
| Optional: only people assigned to the app in Entra, or only people an admin has added (Admin > Settings) | Entra / `auth.py` |
| Session cookie: random, HttpOnly, SameSite=Lax, `__Host-` and Secure over HTTPS; only its SHA-256 is stored; ends after 10 idle hours or 7 days | `auth.py` |
| Every change needs the session's CSRF token | `web/__init__.py` |
| No inline script or style allowed (Content-Security-Policy), no framing, HSTS, nosniff | `web/__init__.py` |
| Update text is cleaned to a short list of tags; links only http(s)/mailto; files are downloads in a sandbox | `sanitize.py`, `web/pages.py` |
| Roles: viewers can't change anything; only admins see people, backups, imports and the bin | `service.py`, `web/admin.py` |
| Turning someone off signs them out at once; "Sign everyone out" for emergencies | Admin |
| Test sign-in (`FORWORK_DEV_LOGIN=1`) only answers 127.0.0.1, and the server refuses to start with it on any other address | `web/pages.py`, `__main__.py` |
| Secrets only in server settings (environment), never in the database or its backups | `config.py` |
| Every change in the activity log | `service.py` |

## Bringing monday.com across

*Admin > Import from monday.com* pulls everything with a monday API token: boards (active and archived),
groups, columns and labels, items and subitems with every value, connected items and mirrors, updates with replies
and @mentions, saved views, and status automations. People are matched by email. You can also load an export file,
from `python -m forwork pull-monday --out export --zip`, or the one made with Claude's monday.com connector.
Running it again skips boards already imported, or with *Replace* loads them fresh.

## Running it on a PC (to try it or to work on it)

Python 3.11 or later:

```
cd apps/forwork
python -m pip install -r requirements-dev.txt
set FORWORK_DEV_LOGIN=1                      (PowerShell: $env:FORWORK_DEV_LOGIN=1; macOS/Linux: export ...)
python -m forwork import-monday <export folder or .zip>     (optional)
python -m forwork serve                       # http://127.0.0.1:8000, pick anyone to sign in as
```

Other commands: `python -m forwork check` (the settings it picked up), `add-admin <email>`, `backup`,
`backup list`, `restore <file>`, `pull-monday`.

```
python -m pyflakes forwork tests
python -m pytest -q tests          # tests/test_ui_browser.py needs Playwright and Chromium, and is skipped without them
```

## Layout

| | |
|---|---|
| `forwork/db.py` | the database schema and connections |
| `forwork/model.py` | column types: checking, showing and sorting values; the formula calculator |
| `forwork/service.py` | boards, groups, columns, items, updates, views, notifications, search, My work, with the activity log |
| `forwork/automations.py` | board automations |
| `forwork/auth.py` | Microsoft 365 sign-in and sessions |
| `forwork/backup.py`, `graph.py` | snapshots, OneDrive/SharePoint upload, retention, restore |
| `forwork/monday_bundle.py`, `monday_pull.py`, `monday_import.py` | the monday.com export format, pulling it, loading it |
| `forwork/export.py` | Excel and JSON exports |
| `forwork/web/` | the Flask app: pages, JSON API, admin, templates, `static/board.js` (the board screen) |
| `tests/` | unit, web, import, backup and browser tests, with stand-ins for Microsoft, Graph and monday.com |
| `deploy/` | the Azure App Service start-up command and a Docker file |
