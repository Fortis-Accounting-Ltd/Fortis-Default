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
| **Move a client off Xero** | Give the app the client's Xero chart export and its year-end trial balances. It builds the client so that every year's closing balances match Xero. |

## Setting up the SharePoint library (once, for the firm)

1. In SharePoint, create a document library for the ledger (e.g. site *Fortis*, library *Fortis Ledger*).
   Keep version history on. It is SharePoint's backup of every file.
2. Each person opens the library in SharePoint and clicks **Sync**. OneDrive puts it on their PC,
   usually at `C:\Users\<you>\Fortis Accounting Partners\Fortis Ledger - Documents`.
3. The first person to start the app chooses that folder and ticks **Set up a new data folder here**.
   Everyone else chooses their own synced copy of the same library; the app finds it if it is in the
   usual place.

Library permissions control who can see and change the ledger.

## Running it

Download **Fortis-Ledger-windows** from the latest run of the *Fortis Ledger* workflow (GitHub >
Actions). Unzip the `Fortis Ledger` folder anywhere, e.g. `C:\Program Files\Fortis Ledger` or your
own Apps folder, then run `Fortis Ledger.exe`. It opens in its own window, which uses the Edge
WebView2 runtime that comes with Windows 10 and 11.

To check a copy end to end, run `"Fortis Ledger.exe" --self-test`. The exit code is 0 when it
passes, and the details are written to `%APPDATA%\Fortis Ledger\self-test.txt`.

From the source code (any OS, Python 3.11+):

```
cd apps/fortis-ledger
python -m pip install -r requirements.txt
python -m fortis_ledger            # desktop window (on Windows), or the browser elsewhere
python -m fortis_ledger --browser  # in the default web browser
```

To build the Windows app yourself, run `powershell -ExecutionPolicy Bypass -File packaging\build_windows.ps1`.

## Moving a client off Xero

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

- `fortis_ledger/`: `store.py` (folder storage, change log, edit lock), `ledger.py` (journal rules,
  balances, year-end close), `imports.py`, `xero.py` (migration), `reports.py`, `web/` (the screens:
  Flask, served on 127.0.0.1 with a start-up token and CSRF checks) and `desktop.py` (pywebview window).
- Tests: `python -m pip install -r requirements-dev.txt && python -m pytest -q tests`.
  `tests/test_ui_browser.py` drives the screens in Chromium when Playwright is installed.
- The report engine is `fortis_ledger/reportengine` (scripts, charts, report map, wording, fonts); see its
  `SOURCE.md`. To try another copy of the engine, set `FORTIS_REPORT_ENGINE` to its folder.
- `xero.migrate_from_api()` builds a client from Xero API JSON (Organisation, Accounts,
  Reports/TrialBalance) for files that are still connected.
