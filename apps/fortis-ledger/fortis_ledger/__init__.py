"""Fortis Ledger - a small double-entry ledger for annual accounts, with Xero-style reports.

Data lives in a folder (normally a SharePoint document library synced by OneDrive), one
folder per client: the client's chart of accounts, its journals (one file each), a change
log and the year status.  The Financial Reports pack is produced by the app's own copy of the
report engine (fortis_ledger/reportengine), so it matches the firm's Xero report templates.
"""

__version__ = "0.2.0"
APP_NAME = "Fortis Ledger"
