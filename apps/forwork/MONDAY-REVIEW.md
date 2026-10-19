# How Fortis uses monday.com - and what ForWork keeps

A review of the firm's monday.com account, read through monday's API on 18-19 October 2026, before the
first ForWork import. Client and staff names are left out because this repository is public.

## The account at a glance

| | |
|---|---|
| Plan | monday.com Standard (Work Management + CRM), 13 active members |
| Workspaces | 3: *Main workspace*, *SMSF* (with a *2025* folder), *CRM* |
| Boards | 126 in total: 17 active, 22 archived, the rest deleted (plus their subitem boards and a few docs) |
| Items | 7,996 on boards and 9 subitems, plus 140 archived items that updates still point to |
| Updates | 8,161 updates and 307 replies, 1,470 with @mentions, 4,004 attachments; 26 people have written them |
| Activity | Updates per year: 2018-19 about 500, 2020 921, 2021 831, 2022-24 400-500, 2025 1,300, 2026 2,804 so far |

monday.com is used more each year: 2026 already has more than twice 2025's updates.

## How the boards are built

**One board per job type per year.** The core of the firm's work is year-based:

- *Annual Compliance 2025-2026* (1,043 items) and *Annual Compliance 2026-2027* (1,257 items): one item per
  entity (individuals, companies, trusts, SMSFs), named in capitals the way the ATO lists them.
- *SMSF Workflow FY2025* / *FY2026* (about 100 funds each) in their own SMSF workspace.
- *Activity Statement* (760 items): BAS, IAS, super and TPAR, one group per lodgement period.
- A dedicated BAS/IAS board and an ad-hoc/FBT board for one large client group.
- *Pre-June Review*: tax-planning reviews, one group per year (2022 to 2026).
- The *JHK Workflow* boards (2018-19 to 2024-25), *Jobkeeper* and *NSW State Grant* boards are archived history.

Each new year is started by **duplicating last year's board** and resetting it, which is why the column ids,
groups and automations are the same from year to year. ForWork's *Duplicate board* (structure only,
structure and items, or structure, items and updates) does the same in one step.

**Groups are workflow stages or periods, not categories.** On the compliance boards the groups are the pipeline:
*IT List* (not started) → *Proposal* → *To Complete* → *WIP* → *With SR* → *In Query* → *Review* → *Out* →
*Completed*. On the Activity Statement board each group is a period with its due date in the name, e.g.
"Sep Qtr 2026 BAS (Due on 25/11/2026)", newest at the top, with master lists for TPAR, BAS and IAS clients.

**Status columns drive everything.** The main compliance status has 31 labels, for example *Not Started*,
*To Send Proposal*, *Proposal Sent*, *Working on it*, *Queries to Manager*, *Queries to Client*, *Queries Replied*,
*With SR*, *Waiting for Review*, *Reviewed, Changes Required*, *Cover Letter Drafted*, *Ready to Send*,
*Sent to Client*, *Signed, Waiting for Payment*, *Sent to ATO*, *Done*, *Disengaged* and *Hold off Lodgement*.
Seven of them are switched off ("deactivated") but still sit on older items. ForWork keeps every label with its
colour, order and "counts as done" flag, and keeps switched-off labels hidden rather than losing them.

The Activity Statement board has five status columns per item: lodgement status, STP finalisation, super,
the PayDay Super cycle, and who reviews it. The bookkeeping clientele board uses 12 status columns as a
service profile (BAS, IAS and payroll frequency, super, payroll tax, bank recs, bills, sales invoices, reports).

**The other columns:**

| What it's for | Column type | Examples |
|---|---|---|
| Who | people | Manager, Accountant, Team, Reviewer, Account Manager |
| When | date / timeline | ATO Due Date, Date In, Date To Complete, timeline |
| Money and time | numbers | Fees, Bill, Est. Billings, Hrs, Budgeted / Workable / Remaining Hours, Priority |
| Notes | text / long text | Notes, Group, Entity Type, TFN-type references, Query via, Client Instructions |
| Scope | dropdown | Scope of Work (e.g. "Ask info + BAS + Super Reminder"), Add-ons, Financial Reports |
| Links | link / connect boards / mirror | FYI workpaper links; the review register links back to the job on any of five boards |
| Small things | checkbox, email, phone, file, formula, creation log, button | |

Subitems are barely used (9 in total). Updates are where the work is discussed: questions to the manager,
"check queries with SR", file notes and approvals, with @mentions to hand work over.

**Automations** are simple and consistent: when the status changes to a label, move the item to the matching
group (e.g. *Waiting for Review* → *Review*, *Sent to Client* → *Out*, *Done* → *Completed*), and stamp *Date In*
when an item goes to *To Complete*. *Annual Compliance 2026-2027* has 15 of these and *SMSF Workflow FY2026* has 3.
There are none on the Activity Statement or the client-group boards. ForWork brings these across and runs them the
same way. One rule on monday ("when an item is created, link it to the portfolio project") was not brought across.

**Views** are mostly personal column layouts: one table view per manager (their initials) with a column order
and hidden columns; "RB" groups by status, and "Hours Allocation" groups by accountant, filtered to open work.
One view filters to "assigned to me". ForWork keeps these as saved views with the same order, hidden columns,
group-by, filters and sort. monday's app views ("Build Vibe", *Compliance Oversight*, *Accountant Workload*),
charts, timelines and the form view were not brought across; those are dashboards and can be rebuilt later.

**The CRM workspace** (Leads, Contacts, Accounts, Deals, Activities, Quotes & Invoices) is monday's template
with a handful of test items. It comes across, but isn't really in use.

## Things worth tidying (seen in the data)

- 22 archived and 44 deleted boards. The archived ones come across as archived (read only), ready to search.
- *Annual Compliance 2026-2027* exists twice: the real board and a one-item "portfolio" board made by a template.
- Old groups on *Pre-June Review* (2022-2025) keep open items assigned to people, so they show up in My work. Archive
  those groups, or set their status, once the year is closed.
- Several people's old accounts appear twice (a second email for the same person) and about 20 accounts are "Deleted member".
  ForWork keeps them as inactive people, so history still shows who did what.
- Files uploaded to monday (4,004 on updates, plus file columns) stay in monday.com: monday only lets a signed-in user
  download them. ForWork lists each file on its update with its name and size. Download them before the monday
  subscription ends if they matter (most are copies of what's already in FYI).

## What ForWork does differently

- **Sign-in is the firm's Microsoft 365**, with its MFA and conditional access, instead of separate monday logins.
- **The data is the firm's**: one database on the firm's server, with a full copy in OneDrive every hour and every night.
- **Every change is in the activity log**, including automation moves, with what the value was before.
- **"Former members" keep their names** on the work they did, instead of "Deleted member".
- **No per-seat cost.**
