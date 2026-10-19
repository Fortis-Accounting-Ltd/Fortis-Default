"""ForWork - Fortis Accounting Partners' work boards (our own version of monday.com).

A browser app the whole firm signs in to with their Microsoft 365 email.  Work is kept on boards:
groups of items (a client's job, a BAS, an SMSF audit), with typed columns (status, people, dates,
numbers ...), subitems, updates (comments with @mentions), automations and saved views.  The data
lives in one SQLite database on the server and a complete copy goes to OneDrive every hour.
"""

__version__ = "0.1.0"
APP_NAME = "ForWork"
