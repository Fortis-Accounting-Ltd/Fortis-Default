# Fortis Ledger

A desktop ledger for the clients we keep in Xero only for year-end work. It does what we use the Xero
Ledger plan for: a chart of accounts with report codes, manual journals, trial balance imports,
year-end roll-forward, and Xero-style reports. The data lives in a SharePoint document library, so
the whole firm works from one copy.

The Financial Reports come from the app's own copy of the report engine (`fortis_ledger/reportengine`),
taken unchanged from the financial-report-builder skill on 16 October 2026. The app never reads or
changes the skill. The two are separate from now on: a change to one does not reach the other.

## What it does

| | |
|---|---|
| **Clients** | Company, trust, partnership or sole trader. Each starts with the firm's chart for its type, or with its own Xero chart. |
| **Chart of accounts** | Add, rename or re-code accounts. The report code (Xero's, e.g. `LIA.CUR.PAY.TRA`) decides where an account prints and which note groups it. Exports in Xero's CSV format. |
| **Manual journals** | Debits must equal credits, the date must fall in the year, and only chart accounts can be used. Journals can be drafts (unbalanced allowed, never in the balances), posted, edited, copied, reversed (e.g. accruals on 1 July) or voided with a reason. |
| **Imports** (each one is previewed before anything posts) | **Trial balance / classified account list** (CSV or Excel): code, name and either debit/credit columns, one balance column, or CY and PY columns; Xero's Trial Balance export works as it is. **Xero manual-journal CSV**, or a plain journal file. **Chart of accounts** (Xero export). **Fortis workpaper** (P&L and Balance Sheet tabs, or an ITR Business tab). |
| **Balances** | Like Xero: balance-sheet accounts carry forward, P&L accounts start each year at nil, and last year's profit goes to retained earnings. An import *sets* each account to the file's figure: it posts only the difference from what the ledger already holds. So you can re-import a corrected file, and loading each year's TB never double counts. |
| **Year end** | Roll forward opens the next year and posts one closing journal on its first day. Company dividends go into retained earnings. Each beneficiary's or partner's drawings, contributions and share of profit go into their opening balance. A sole trader's drawings go into retained earnings. If the year changes later, the app flags the closing journal for a refresh. Lock a year once its reports are signed; unlocking needs a reason. |
| **Reports** | **Financial Reports** pack (PDF, the firm's Xero template, with comparatives from the ledger; DRAFT stamp until marked final). **Trial Balance**, **General Ledger** and **Journal Report** (PDF or Excel). All are saved in the client's folder in SharePoint. The trial balance and account transactions can also be viewed on screen, with drill-down to each journal. |
| **Change log** | Every change records who made it, when, on which PC, and what was there before. SharePoint also keeps each file's version history. |
| **Edit lock** | One person edits a client at a time; others can view it read only. The lock frees itself after 15 minutes if a PC goes away, and someone can take it over. |
| **Move a client off Xero** | **Connect to Xero** signs in to Xero from the app and brings a client's chart and year-end trial balances straight across (read only), then disconnects it. Or give the app the client's Xero chart export and trial balance exports. Either way, every year's closing balances match Xero. |
| **Office sign-in** | Optional: everyone signs in with their Fortis Microsoft 365 email. The change log and the edit lock then show the verified email instead of a typed name. |
| **Installer** | One file, `Fortis-Ledger-Setup-<version>.exe`, to share with staff. It installs without administrator rights and puts a *Fortis Ledger* icon on the desktop, plus a *Fortis Ledger - Connect to Xero* icon that opens straight at Connect to Xero. |

## Setting up the SharePoint library (once, for the firm)

1. In SharePoint, create a document library for the ledger (e.g. site *Fortis*, library *Fortis Ledger*).
   Keep version history on. It is SharePoint's backup of every file.
2. Each person opens the library in SharePoint and clicks **Sync**. OneDrive puts it on their PC,
   usually at `C:\Users\<you>\Fortis Accounting Partners\Fortis Ledger - Documents`.
3. The first person to start the app chooses that folder and ticks **Set up a new data folder here**.
   Everyone else chooses their own synced copy of the same library; the app finds it if it is in the
   usual place.

Library permissions control who can see and change the ledger.

## Signing in with the office email (optional, once, for the firm)

This needs a Microsoft 365 admin and takes about 10 minutes. There is no password or client secret to look after.

1. Go to **entra.microsoft.com** > Applications > App registrations > **New registration**.
2. Name it `Fortis Ledger`. For supported account types choose **Accounts in this organizational directory only**.
3. Under Redirect URI choose the platform **Public client/native (mobile & desktop)** and enter `http://localhost`.
   Then click Register. Microsoft accepts any port on `http://localhost` for desktop apps, which is what the app uses.
4. Optional: to allow only some staff, go to Enterprise applications > Fortis Ledger > Properties and set
   **Assignment required** to Yes. Then add those people under Users and groups.
5. In Fortis Ledger, open **Firm settings** (the menu under your name, top right). Paste the
   **Directory (tenant) ID** and the **Application (client) ID** from the app's Overview page, add the email
   domain (`fortisap.com.au`), and click **Test sign-in**.
6. Once the test sign-in works, tick **Require sign-in on every PC** and click **Save**. The app lets you require
   sign-in only after a successful test with the same IDs, so a typing mistake cannot lock everyone out.

From then on the app opens at a *Sign in with Microsoft* page. Its button opens the browser, where it is usually
one click because Microsoft remembers the account. The app checks that the account is in the firm's directory and
in an allowed domain. Then the change log, the edit lock and the Overview show that email. No password or token is
saved on the PC, only the last email used, so it can be offered next time. Each time the app opens, it signs in
again. **Firm settings > Stop requiring sign-in** turns it off again for everyone.

## Installing it

The installer is one file, `Fortis-Ledger-Setup-<version>.exe`. Share it with staff, for example from
SharePoint or Teams. Each of the *Fortis Ledger* workflow's runs on GitHub (Actions) also keeps a copy,
as **Fortis-Ledger-Setup**.

Each person installs it like this:

1. Double-click `Fortis-Ledger-Setup-<version>.exe`.
2. The installer is not code-signed, so Windows may show *Windows protected your PC*. Click **More info**,
   then **Run anyway**.
3. Click **Next**, choose the desktop icons (*Fortis Ledger*, and *Fortis Ledger - Connect to Xero*), and
   click **Install**.
4. Leave *Open Fortis Ledger now* ticked and click **Finish**. The first time, the app asks for your name
   and the data folder (the synced SharePoint library).

It installs for the person running it, in `%LOCALAPPDATA%\Programs\Fortis Ledger`, so no administrator
rights are needed. It adds a Start menu entry. The program opens in its own window, which uses the Edge
WebView2 runtime that comes with Windows 10 and 11.

- **Updating:** run the newer `Fortis-Ledger-Setup` file. It replaces the old version, and everything else
  is kept: the ledger, your settings and your icons.
- **Uninstalling:** go to Settings > Apps > Fortis Ledger > Uninstall. This removes the program and its
  icons only. The ledger in SharePoint and your settings stay.
- **For IT:**
  - To install for everyone on a PC, run the setup from an administrator prompt with `/ALLUSERS`.
  - To install silently, e.g. through Intune, use
    `Fortis-Ledger-Setup-<version>.exe /VERYSILENT /SUPPRESSMSGBOXES /TASKS=desktopicon,xeroicon`.
    Leave out either task name to skip that icon.

**Settings > Desktop icons** adds an icon again if one was deleted. To open the app at a particular screen,
use `"Fortis Ledger.exe" --start xero` or `--start settings`.

## Running it without installing

The workflow's **Fortis-Ledger-windows** download is the same program as a plain folder. Unzip the
`Fortis Ledger` folder anywhere and run `Fortis Ledger.exe`. The first time it runs on a PC, it puts a
**Fortis Ledger** icon on the desktop, including a desktop that OneDrive has moved. If you move the folder,
make the icons again from Settings.

To check a copy end to end, run `"Fortis Ledger.exe" --self-test`. The exit code is 0 when it
passes, and the details are written to `%APPDATA%\Fortis Ledger\self-test.txt`.

From the source code (any OS, Python 3.11+):

```
cd apps/fortis-ledger
python -m pip install -r requirements.txt
python -m fortis_ledger            # desktop window (on Windows), or the browser elsewhere
python -m fortis_ledger --browser  # in the default web browser
```

To build the Windows app and its installer yourself, install [Inno Setup](https://jrsoftware.org/isinfo.php)
6.7 or later, then run `powershell -ExecutionPolicy Bypass -File packaging\build_windows.ps1`. The installer
is made by `packaging\installer.iss`.

`packaging\test_installer.ps1 dist\Fortis-Ledger-Setup-<version>.exe` checks the installer the way staff
will use it: it installs Fortis Ledger, checks the icons, self-tests the installed copy, installs again
over the top, and uninstalls it. CI runs it on every build.

## Moving a client off Xero

Do this before the client's Xero subscription is cancelled. There are two ways.

### Connect to Xero (no export files)

Once, for the firm (5 minutes, free):

1. Sign in at **developer.xero.com** > My Apps > **New app**.
2. App name `Fortis Ledger`. Integration type **Mobile or desktop app** (it has no client secret).
   Company URL `https://fortisap.com.au`.
3. Redirect URI `http://localhost:8749/callback`. Create the app.
4. Copy the app's **Client id** into Fortis Ledger's **Firm settings > Xero connection** and click Save. If another
   program on the PCs already uses port 8749, choose another port there, and change the redirect URI to match.

For each client:

1. Click **Connect to Xero** (in the sidebar, on the Clients page, or on the desktop icon). The browser opens Xero's
   sign-in. Sign in with your own Xero login, tick the client's organisation, and click Allow.
2. The app lists the organisations you connected. Click **Bring across**, check the client's name, type, ABN
   and signatories, and tick the year ends to bring in. The last two are ticked to start with.
3. The app reads the organisation's details, its chart of accounts with Xero's report codes, and the trial
   balance at each year end you ticked. It then builds the client so that each year's closing balances match
   Xero. Years with no balances in Xero are skipped. If something fails part way, the half-made client is
   archived rather than left in the list.
4. The organisation is disconnected from the firm's Xero app afterwards (untick *Disconnect* to keep it). That
   keeps the firm inside the 5 organisations Xero's free tier allows at a time. Disconnecting changes nothing in
   the Xero file.

The app only asks Xero for `accounting.settings.read` and `accounting.reports.trialbalance.read`, plus the
user's name and email. It never writes to Xero. The sign-in lasts 30 minutes and is kept in memory, never on
disk. Individual transactions don't come across, because Xero offers its Journals API only on its paid
Advanced tier. Keep the General Ledger Detail and Journal Report exports (step 3 below) in the client's folder.

### Xero export files

Before cancelling a client's Xero subscription, export:

1. **Chart of accounts**: Accounting > Chart of accounts > Export.
2. **Trial Balance at each year end** you need (at least last year and this year): Reports > Trial Balance >
   date = year end > Export > Excel.
3. For the file, not for the app: General Ledger Detail and Journal Report for each year, plus any attachments.
   Keep them in the client's SharePoint folder.

Then click **Move a client from Xero** in the app and give it the chart and all the trial balances. It
reads the year from each report's "As at" date. Check each year's trial balance against Xero, look
at the accounts listed under *Check these accounts' report codes*, then generate the Financial Reports.

## How the data is stored

Plain files, so nothing is locked inside the app:

```
<library>/
  fortis-ledger.json              marks the folder as a Fortis Ledger data folder
  charts/<type>.csv               the firm's master charts
  clients/<client>/
    client.json                   entity details, report options
    chart.csv                     the chart of accounts, in Xero's import/export columns
    audit.jsonl                   change log (one line per change)
    lock.json                     who is editing now
    years/<FY>/year.json          locked or open, lock history
    years/<FY>/journals/J<FY>-0001.json ...
    years/<FY>/reports/           generated reports
  archived/                       archived clients (moved here, never deleted)
```

Files are written to a temporary name and then renamed, so OneDrive never uploads half a file. If two
PCs change the same file before OneDrive syncs, OneDrive keeps both copies. The client's Overview
lists any such copies so a person can keep the right one.

**Good practice**: wait for OneDrive to show *Up to date* before opening a client that someone else
has just changed, and close the client (go back to Clients) when you finish.

## What it does not do (yet)

Not built yet:
- Bank feeds and bank reconciliation
- Invoicing and contacts
- GST/BAS
- Payroll
- Tracking categories
- A fixed asset register: depreciation comes in through journals or the workpaper import
- Budgets
- Tax return schedules

The plan is to add the asset register next.

## For developers

- `fortis_ledger/`: `store.py` (folder storage, change log, edit lock, firm settings), `ledger.py` (journal
  rules, balances, year-end close), `imports.py`, `xero.py` (migration), `reports.py`, `web/` (the screens:
  Flask, served on 127.0.0.1 with a start-up token and CSRF checks) and `desktop.py` (pywebview window).
- Sign-ins: `oauth.py` is the shared part: OAuth 2.0 authorisation code with PKCE, returning to a one-off listener
  on localhost (RFC 8252). It uses no client secret, and tokens stay in memory. `msauth.py` is office sign-in
  (Microsoft Entra ID; it checks the tenant, the audience, the nonce and the email domain). `xero_connect.py`
  is Connect to Xero (Organisation, Accounts and Reports/TrialBalance, read only). `shortcut.py` makes the
  desktop icons with Windows' own shortcut maker.
- Firm-wide settings (the office sign-in IDs, the Xero client id) are kept in `fortis-ledger.json` in the
  data folder, so every PC uses the same ones. They are IDs, not secrets.
- Tests: `python -m pip install -r requirements-dev.txt && python -m pytest -q tests`.
  `tests/test_ui_browser.py` drives the screens in Chromium when Playwright is installed.
  `tests/fake_oauth.py` stands in for Xero and Microsoft on 127.0.0.1: like the real services, it checks the
  PKCE challenge, the client ID and the redirect URI. That way both sign-ins are tested end to end without
  touching either service.
- The report engine is `fortis_ledger/reportengine` (scripts, charts, report map, wording, fonts); see its
  `SOURCE.md`. To try another copy of the engine, set `FORTIS_REPORT_ENGINE` to its folder.
- `xero.migrate_from_api()` builds a client from Xero API JSON (Organisation, Accounts,
  Reports/TrialBalance). Connect to Xero uses it.
