# Fortis Ledger

A desktop ledger for the clients we keep in Xero only for year-end work. It does what we use the Xero
Ledger plan for: a chart of accounts with report codes, manual journals, bank statements with cash
coding, GST, trial balance imports, year-end roll-forward, and Xero-style reports. The data lives in a SharePoint document library, so
the whole firm works from one copy.

The Financial Reports come from the app's own copy of the report engine (`fortis_ledger/reportengine`),
taken unchanged from the financial-report-builder skill on 16 October 2026. The app never reads or
changes the skill. The two are separate from now on: a change to one does not reach the other.

## What it does

| | |
|---|---|
| **Clients** | Company, trust, partnership or sole trader. Each starts with the firm's chart for its type, or with its own Xero chart. |
| **Chart of accounts** | Add, rename or re-code accounts. The report code (Xero's, e.g. `LIA.CUR.PAY.TRA`) decides where an account prints and which note groups it. Exports in Xero's CSV format. A **New account** pop-up on the journal and cash coding screens adds an account (type, number, name and, for GST clients, tax rate) without leaving the screen. |
| **Bank statements and cash coding** | Import the CSV (or Excel) statement from internet banking into a bank, card or loan account; the columns are found for you and checked on screen first, and lines already imported are skipped. Code each line to an account - suggested from how the same payee was coded before - and reconcile: it posts as a bank transaction, with the GST split out. Transfers between two accounts with statements post once. The Overview shows each bank account's lines to reconcile and the statement balance beside the ledger balance. |
| **GST** | Tick *Registered for GST* on Client details. Bank lines and journal lines then take a tax rate (Xero's: GST on Income, GST on Expenses, GST Free ..., BAS Excluded), and the GST goes to the GST account. Journals can be entered GST inclusive or exclusive. |
| **Manual journals** | Debits must equal credits, the date must fall in the year, and only chart accounts can be used. Journals can be drafts (unbalanced allowed, never in the balances), posted, edited, copied, reversed (e.g. accruals on 1 July) or voided with a reason. |
| **Imports** (each one is previewed before anything posts) | **Trial balance / classified account list** (CSV or Excel): code, name and either debit/credit columns, one balance column, or CY and PY columns; Xero's Trial Balance export works as it is. **Xero manual-journal CSV**, or a plain journal file. **Chart of accounts** (Xero export). **Fortis workpaper** (P&L and Balance Sheet tabs, or an ITR Business tab). |
| **Balances** | Like Xero: balance-sheet accounts carry forward, P&L accounts start each year at nil, and last year's profit goes to retained earnings. An import *sets* each account to the file's figure: it posts only the difference from what the ledger already holds. So you can re-import a corrected file, and loading each year's TB never double counts. |
| **Year end** | Roll forward opens the next year and posts one closing journal on its first day. Company dividends go into retained earnings. Each beneficiary's or partner's drawings, contributions and share of profit go into their opening balance. A sole trader's drawings go into retained earnings. If the year changes later, the app flags the closing journal for a refresh. Lock a year once its reports are signed; unlocking needs a reason. |
| **Reports** | **Financial Reports** pack (PDF, the firm's Xero template, with comparatives from the ledger; DRAFT stamp until marked final). **Profit and Loss** and **Balance Sheet** on their own, for any period or date, with the year before beside them. **General Ledger Detail**, **Trial Balance**, **Journal Report**, and the **GST Report** on the cash or accrual basis with the BAS labels (G1, G3, G4, G10, G11, 1A, 1B). PDF or Excel, saved in the client's folder in SharePoint; all but the pack can also be viewed on screen, with drill-down to each transaction. |
| **Change log** | Every change records who made it, when, on which PC, and what was there before. SharePoint also keeps each file's version history. |
| **Edit lock** | One person edits a client at a time; others can view it read only. The lock frees itself after 15 minutes if a PC goes away, and someone can take it over. |
| **Move a client off Xero** | **Connect to Xero** signs in to Xero from the app and brings a client's chart and year-end trial balances straight across (read only), then disconnects it. Or give the app the client's Xero chart export and trial balance exports. Either way, every year's closing balances match Xero. |
| **Claude** | Claude (the Claude desktop app, or Claude Code) can read a client's ledger and prepare journals on a PC where Fortis Ledger is installed - "journal depreciation of $4,500 for ... at 30 June". It works through the same rules as the screens; it posts the journals you ask it to post (a partner can turn that off, and Claude then saves drafts for a person to post), and every change is marked *via Claude* in the change log. |
| **Office sign-in** | Optional: everyone signs in with their Fortis Microsoft 365 email. The change log and the edit lock then show the verified email instead of a typed name. |
| **Team** | Everyone works on the same clients through the SharePoint library. With office sign-in required, admins choose who may use Fortis Ledger and limit a client to the people who work on it - on every screen and in Claude. An open screen says when a colleague (or Claude) has changed what it shows. |
| **Installer** | One file, `Fortis-Ledger-Setup-<version>.exe`, to share with staff. It installs without administrator rights and puts a *Fortis Ledger* icon on the desktop, plus a *Fortis Ledger - Connect to Xero* icon that opens straight at Connect to Xero. |
| **Updates** | After that, changes come as a small update file (`.fortisupdate`, under 1 MB): email it, or an admin shares it with everyone from inside the app. GitHub signs each one for the firm's build, and Fortis Ledger checks it before it goes in. |

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

## Working as a team

**Sharing the client files.** Everyone syncs the same SharePoint library (above), so a client set up on one PC
is on every PC a few seconds later. **Team** (in the side bar) shows where this PC's data folder is: in
SharePoint, in your own OneDrive, or only on this PC. If it is not in SharePoint yet, *Move the data folder into
SharePoint* copies every client into a library synced to this PC, checks the copy and switches to it (the old
folder is left as it was). *Invite a colleague* writes the email with the three steps: install, sync the
library, open Fortis Ledger.

**Working at the same time.** One person changes a client at a time (the edit lock); others can open it read
only. Each open screen checks every 10 seconds (the client list every 30) whether a colleague or Claude has
changed what it shows, and then says who changed what, with a *Refresh* button. A read-only screen also says
when the person editing has finished.

**Who can open which client.** Access is by office email, so office sign-in must be required first (above).
Then, on **Team**:

1. Click **Manage access**. You become the first admin. Choose whether new people *join as staff when they
   first sign in* or *need an admin to add them*.
2. Add people as **Admin**, **Staff** or **No access**. Admins can open every client, and manage the team, the
   firm settings, client archiving, moving the data folder and sharing updates with everyone. Staff can open
   every client that is not limited to others. No access shuts someone out.
3. To limit a client, open it and go to **Client details > Access > Only these people**. Admins can always
   open it.

The rules are kept in the data folder (`team.json`), so every PC applies the same ones, and they apply to
Claude too: Claude only sees the clients the person using it may open. The Team page lists every change to
the team (`firm-log.jsonl`). While access is managed, office sign-in cannot be turned off; *Stop managing
access* first.

Fortis Ledger applies these rules, but the files themselves are in the SharePoint library, so anyone who
syncs it can still open them outside the app. For a client that must stay private, also limit its folder
(`clients\<client>`) in SharePoint to the same people (the folder's *Manage access*, stop inheriting
permissions).

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

- **Updating:** install the update files (see *Updates* below). A newer `Fortis-Ledger-Setup` file also
  works: it replaces the old version, and everything else is kept: the ledger, your settings and your icons.
- **Uninstalling:** go to Settings > Apps > Fortis Ledger > Uninstall. This removes the program and its
  icons only. The ledger in SharePoint and your settings stay.
- **For IT:**
  - To install for everyone on a PC, run the setup from an administrator prompt with `/ALLUSERS`.
  - To install silently, e.g. through Intune, use
    `Fortis-Ledger-Setup-<version>.exe /VERYSILENT /SUPPRESSMSGBOXES /TASKS=desktopicon,xeroicon`.
    Leave out either task name to skip that icon.

**Settings > Desktop icons** adds an icon again if one was deleted. To open the app at a particular screen,
use `"Fortis Ledger.exe" --start xero` or `--start settings`.

## Updates

From 0.2.0 on, changes come as an update file, `Fortis-Ledger-Update-<version>.fortisupdate` (under 1 MB,
just Fortis Ledger's own code), instead of a new installer. Each run of the *Fortis Ledger* workflow on
GitHub (Actions) keeps one, as **Fortis-Ledger-Update**.

- **By email:** attach the file to an email. Whoever double-clicks it on a PC with Fortis Ledger sees the
  **Updates** screen: the version, and the branch, commit and build it came from. **Install**, then
  **Restart now**.
- **For everyone at once:** an admin ticks *Also share it with everyone* when installing it (or clicks
  **Updates > Share with everyone**). The file goes into the data folder (`updates/`), and every PC that uses
  the folder shows *Fortis Ledger X is available*.
- An update is installed for the person (no administrator rights), in `%LOCALAPPDATA%\Fortis Ledger\updates`,
  beside the program rather than over it. It starts the next time Fortis Ledger opens. **Updates > Send it by
  email > Show the file** finds the file again to send it on.

Each update is signed by GitHub for the firm's build: the build asks GitHub Actions for an identity token
naming the update's list of files and their SHA-256 hashes, and that token, signed with GitHub's key, travels
in the file. Fortis Ledger installs an update only if GitHub's signature checks out, the token was issued to
this repository's *Fortis Ledger* workflow, and every file matches the list. So an update that arrives by
email, or that someone puts in the shared folder, can only carry what the firm's build made. (Anyone who can
change this repository's code can make an update, as they can make an installer.) No signing key or password
needs looking after.

If an update does not start, Fortis Ledger starts the version the installer put on the PC and says so on the
Updates screen; **Updates > Use the installed version** does the same by hand. When an update needs new parts
of the program itself (a new Python or library), it says so: install that version with its full installer
instead. For IT: `fortis-ledger-mcp.exe --install-update <file> --yes` installs one without asking.

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

`packaging\test_installer.ps1 dist\Fortis-Ledger-Setup-<version>.exe [update file]` checks the installer the
way staff will use it: it installs Fortis Ledger, checks the icons and that update files open in it,
self-tests the installed copy, installs the update file and self-tests again, checks that a changed update
file is refused and that an update which does not start is set aside, installs again over the top, and
uninstalls it. CI runs it on every build, with an update file GitHub signed for that build.

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

## Bank statements and cash coding

1. In internet banking, export the account's transactions as a **CSV** file (CommBank, Westpac, NAB, ANZ,
   St.George, Macquarie, Bendigo and most other banks offer one; an Excel file works too).
2. In the client, open **Bank** (or *Import a statement* on the Overview), choose the bank account - or
   make one with **New bank account** - and the file, then **Upload and check**.
3. Check the columns: the app shows which column it read as the date, the amount (or the spent and
   received columns), the payee, description, reference and the bank's running balance, with every line
   it will import. Change any of them, or tick *Money spent shows as positive* for credit card files.
   Lines already in the account (an overlapping statement imported before) are marked and skipped.
4. **Import**. The lines wait under *To reconcile*. Nothing is in the ledger yet.
5. Code each line: type or pick the account (the account's tax rate comes with it) - or tick several lines
   and *Code the ticked lines*. Lines from a payee coded before are filled in and marked *Suggested*.
   Coding a line ticks it; Enter moves to the next line. **New account** adds an account on the spot.
6. **Reconcile ticked lines**. Each line posts as a bank transaction (`B2026-0001` ...): the bank account
   against the account chosen, with the GST split out for a GST-registered client. A line that cannot be
   posted (e.g. its year is locked or not started) stays, with the reason beside it.

Good to know:
- A transfer between two accounts that both have statements is posted once: code the second side to the
  first account and it is matched to the transaction already posted there.
- To change a reconciled line, **Unreconcile** it on the *Reconciled* tab: it comes out of the ledger and
  goes back to be coded. Numbers are never reused.
- Lines that are not real transactions can be deleted (with a reason); they are kept under *Statements*
  and can be put back.
- Bank transactions show in the reports, the general ledger and *Journals* (choose *Bank transactions*),
  but they are changed only from the Bank page.

## GST and the GST report

Tick **Registered for GST** on Client details (Connect to Xero sets it from the Xero organisation). Then:
- cash coding offers a tax rate per line, starting from the account's own (the chart's tax code);
- the journal editor has a tax rate per line and *Amounts are GST inclusive / exclusive*; the GST lines
  to the GST account are made for you;
- the **GST report** (Reports > GST report) gives the BAS labels and the transactions behind them.
  *Cash basis* counts money received and paid - the bank transactions. *Accrual basis* also counts
  journals with a tax rate, e.g. invoices raised or bills received entered as journals.

The app does not lodge the BAS; the report gives the figures to lodge.

## Claude: journals from a conversation

Fortis Ledger has a built-in connector (an MCP server, the standard way Claude connects to other programs).
On a PC where Fortis Ledger is installed, Claude can then:

- find a client, its chart of accounts, trial balance, journals, account transactions, Profit and Loss,
  Balance Sheet and GST report;
- **create journals** (with GST for registered clients), **post** drafts, delete drafts it made itself, and
  add accounts.

It works through the same rules as the screens - the client's chart, the financial year, debits equal
credits, locked years, GST, the edit lock - and works as the person using that PC. Every change it makes is
marked **via Claude** in the client's change log, on the journal and in the journal list.

**Set it up:**
1. *Claude can post journals* is on (**Firm settings > Claude**): Claude posts the journals you ask it to post,
   and saves a draft when you ask for one. A partner can turn it off; Claude then only saves drafts, and a
   person checks and posts them (*Journals > Draft*).
2. On each PC: install Claude Desktop (claude.ai/download), then in Fortis Ledger click **Firm settings >
   Claude > Install in Claude Desktop**. Claude Desktop asks to install the *Fortis Ledger* extension: click
   **Install**. In a chat it is then under **+ > Connectors**, and Claude asks your permission before it uses it.
   If Claude Desktop does not open, drag the file (**Show the file**) into its window, or use Claude Desktop's
   **Settings > Extensions > Advanced settings > Install Extension...**. IT can make the file with
   `fortis-ledger-mcp.exe --claude-extension fortis-ledger.mcpb`.
   For Claude Code, run the command shown under *Other ways* instead (`claude mcp add fortis-ledger -- "...\fortis-ledger-mcp.exe"`).
3. Ask Claude, e.g. *"In Fortis Ledger, journal depreciation of $4,500 on the plant for [client] at 30 June
   2026"* or *"Here are the year-end accruals for [client] - draft them as one journal"*. Claude replies with
   the journal number; open it in Fortis Ledger to check and post it.

Good to know:
- The connector runs on the PC (`fortis-ledger-mcp.exe`, started by Claude). It is not reachable over the
  network, and it reads the same OneDrive copy of the data folder as the app.
- If someone else is editing the client, Claude waits like anyone else; if you have the client open
  yourself, Claude works alongside you.
- Claude in a web browser or on a phone cannot reach it - that would need a hosted connector, a separate
  project.
- The extension (`fortis-ledger.mcpb`, made in `%APPDATA%\Fortis Ledger`) holds no copy of the program: a
  small script, run by the Node.js that comes with Claude Desktop, starts the `fortis-ledger-mcp.exe` installed
  on that PC and relays Claude's messages to it, so updating Fortis Ledger updates what Claude uses. (Claude
  Desktop runs extension scripts in its own Node.js host, where stdin and stdout are streams it provides, so the
  script relays them rather than handing the connector its real input and output; version 0.1.1 did that, and
  Claude Desktop reported *Unable to connect to extension server*.) If Fortis Ledger is moved or reinstalled in
  another folder, install the extension again.
- *Other ways* on the same card can instead add Fortis Ledger to Claude Desktop's settings file
  (`claude_desktop_config.json`). Some versions of Claude Desktop for Windows rewrite that file when they start
  and drop entries added this way, which is why the extension is the main way.
- Its log is `%APPDATA%\Fortis Ledger\claude-connector.log`; Claude Desktop's logs are under
  Claude Desktop > Settings > Developer.

## How the data is stored

Plain files, so nothing is locked inside the app:

```
<library>/
  fortis-ledger.json              marks the folder as a Fortis Ledger data folder; the firm's settings
  team.json                       who may open which client (when the firm manages access)
  firm-log.jsonl                  the firm's change log: the team, updates shared with everyone, folder moves
  updates/                        update files an admin shared with everyone
  charts/<type>.csv               the firm's master charts
  clients/<client>/
    client.json                   entity details, report options
    chart.csv                     the chart of accounts, in Xero's import/export columns
    audit.jsonl                   change log (one line per change)
    lock.json                     who is editing now
    years/<FY>/year.json          locked or open, lock history
    years/<FY>/journals/J<FY>-0001.json ...
    years/<FY>/reports/           generated reports
    bank/<account code>.json      a bank account's statement lines, how each was coded, the imports
  archived/                       archived clients (moved here, never deleted)
```

Files are written to a temporary name and then renamed, so OneDrive never uploads half a file. If two
PCs change the same file before OneDrive syncs, OneDrive keeps both copies. The client's Overview
lists any such copies so a person can keep the right one.

**Good practice**: wait for OneDrive to show *Up to date* before opening a client that someone else
has just changed, and close the client (go back to Clients) when you finish.

## What it does not do (yet)

Not built yet:
- Live bank feeds: statements are imported from files. Lines are cash coded; there is no matching to
  invoices or bills
- Invoicing and contacts
- Lodging the BAS (the GST report gives the figures)
- Payroll
- Tracking categories
- A fixed asset register: depreciation comes in through journals or the workpaper import
- Budgets
- Tax return schedules

The plan is to add the asset register next.

## For developers

- `fortis_ledger/`: `store.py` (folder storage, change log, edit lock, firm settings), `ledger.py` (journal
  rules, balances, GST lines, year-end close), `bank.py` (reading bank statement files, duplicates, cash
  coding, transfers, suggestions; reconciled lines are the ledger's bank transactions), `statements.py`
  (Profit and Loss, Balance Sheet and GST report figures), `imports.py`, `xero.py` (migration), `reports.py`
  (PDF and Excel), `web/` (the screens: Flask, served on 127.0.0.1 with a start-up token and CSRF checks;
  `bankviews.py` holds the bank screens), `desktop.py` (pywebview window), `connector.py` (the Claude
  connector: MCP over stdin / stdout, both the 2026-07-28 per-request form and the earlier `initialize`
  handshake) and `claude_desktop.py` (makes the Claude Desktop extension - an MCP Bundle, `.mcpb` - and
  edits Claude Desktop's config). The build makes two programs from the same code: `Fortis Ledger.exe`
  (windowed) and `fortis-ledger-mcp.exe` (console, for Claude); `packaging/check_connector.py` checks a built
  connector the way Claude Desktop talks to it, directly or (`--extension`) through the extension and Node.js,
  and CI checks the extension's manifest with Anthropic's `mcpb validate`.
- Sign-ins: `oauth.py` is the shared part: OAuth 2.0 authorisation code with PKCE, returning to a one-off listener
  on localhost (RFC 8252). It uses no client secret, and tokens stay in memory. `msauth.py` is office sign-in
  (Microsoft Entra ID; it checks the tenant, the audience, the nonce and the email domain). `xero_connect.py`
  is Connect to Xero (Organisation, Accounts and Reports/TrialBalance, read only). `shortcut.py` makes the
  desktop icons with Windows' own shortcut maker.
- Firm-wide settings (the office sign-in IDs, the Xero client id) are kept in `fortis-ledger.json` in the
  data folder, so every PC uses the same ones. They are IDs, not secrets. `team.py` is who may open which
  client (`team.json`); `web/views.py` applies it to every request and `connector.py` to Claude.
  `web/teamviews.py` is the Team screen, the Access card, moving the data folder, and the live notes
  (`/pulse`: a screen is shown with a mark of the client's newest change-log entry, and asks what came after
  it - so PCs' clocks do not matter).
- Updates: the build bundles the app's own code (`fortis_ledger`) as plain files, not inside the program, and
  all of the standard library and the libraries the app uses, so an update can be just that code.
  `packaging/make_update.py` makes an update file and, in GitHub Actions, gets it signed (the job needs
  `id-token: write`). `updates.py` checks one (GitHub's RS256 signature in plain Python, the token's issuer,
  repository, workflow and audience, and each file's hash) and installs it; `packaging/launch.py` starts the
  newest installed update made for its `RUNTIME`, and falls back to the program's own copy if it does not
  load. Raise `RUNTIME` in `launch.py` when the Python version or a library changes: older programs then ask
  for the full installer. `github_oidc_keys.json` is a copy of GitHub's public keys; a key added since is
  fetched from GitHub. `web/updateviews.py` is the Updates screen.
- Tests: `python -m pip install -r requirements-dev.txt && python -m pytest -q tests`. `tests/test_updates.py`
  signs updates with a test key standing in for GitHub's; `tests/test_team.py` covers access, live notes and
  moving the data folder.
  `tests/test_ui_browser.py` drives the screens in Chromium when Playwright is installed.
  `tests/fake_oauth.py` stands in for Xero and Microsoft on 127.0.0.1: like the real services, it checks the
  PKCE challenge, the client ID and the redirect URI. That way both sign-ins are tested end to end without
  touching either service.
- The report engine is `fortis_ledger/reportengine` (scripts, charts, report map, wording, fonts); see its
  `SOURCE.md`. To try another copy of the engine, set `FORTIS_REPORT_ENGINE` to its folder.
- `xero.migrate_from_api()` builds a client from Xero API JSON (Organisation, Accounts,
  Reports/TrialBalance). Connect to Xero uses it.
