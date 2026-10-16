---
name: financial-report-builder
description: Builds the Fortis "Financial Reports" pack (special purpose financial statements) as a PDF for a company, trust, partnership or sole trader WITHOUT a Xero subscription, straight from the client's year-end workpaper in FYI. Works like a Xero ledger - every workpaper P&L and Balance Sheet line is posted, by its nature, to an account in the firm's chart of accounts for that entity type (the four Fortis Xero charts are bundled), and the account's Xero report code decides where it prints - so the output matches the firm's Xero report templates (P&L / Income Statement, Balance Sheet, Movements in Equity, Trust Income Distribution, Partner / Beneficiary Accounts, Notes grouped by account type on the Balance Sheet side only, Rental and Depreciation Schedules, Directors / Trustee / Partner Declaration, Compilation Report). Report codes can be changed on request, per client or firm-wide. Files the finished PDF to FYI (Final Reports & Returns) only after confirmation. ALWAYS trigger for "prepare the financial statements / financial report for [client]", "create the financials from the workpaper", "Xero-style financial report", "special purpose financial statements", "SPFS", "compile the accounts", "financial report pack", "do the financials without Xero", "change the report code for [account]", or when a workpaper is uploaded with a request for financial statements. Do NOT trigger for SMSFs (smsf-audit-pack), tax return review (company-tax-reviewer / trust-tax-reviewer), or building the workpaper itself (company-workpaper-creator).
compatibility: >-
  Needs python3 with reportlab and openpyxl (pip install reportlab openpyxl).
  Uses the FYI MCP to find the client, download the workpaper and last year's
  pack, and to file the finished PDF (SUGGEST → CONFIRM). Does not use Xero -
  it replaces the Xero report templates for clients without a subscription.
  Pairs with company-workpaper-creator (upstream) and company-tax-reviewer /
  trust-tax-reviewer / tax-cover-letter (downstream).
---

# Financial Report Builder

Produces the Fortis **Financial Reports** pack for a client whose accounts live
only in a year-end workpaper, for all four entity types, so the firm does not
need a Xero subscription to issue it. The pack has to be indistinguishable from
the one Xero's report templates produce for clients who *do* have a Xero file.

Two rules sit above everything:

1. **Every line is posted explicitly.** Each workpaper line is posted to a chart
   account and report code, and the reason is recorded in `mapping.md`. A line
   the chart and the rules cannot place with confidence is marked **REVIEW**,
   and the PDF is stamped DRAFT until the preparer resolves it.
2. **Nothing is written to FYI without a confirmation.** Downloading and
   rendering are fine; filing is SUGGEST → CONFIRM.

## How it works (the Xero ledger, without Xero)

```
FYI workpaper ("Profit & Loss" + "Balance Sheet" tabs, or the ITR "Business" tab)
   │ each line → chart account by nature (section header + name + code)     build_pack.py / chart.py
   │             in assets/chart-of-accounts/<entity type>.csv
   ▼
chart account → Xero report code (ASS.CUR.CAS.BAN, LIA.CUR.PAY.EMP, EQU.DRA.2 …)
   │ report code → print location (report-map.json xero_code_map)            fr_engine.py
   ▼
pack.json (the ledger) + mapping.md (every posting, with its reason) + ledger.csv
   ▼
"YYYY Financial Report - <Entity>.pdf" in the Xero template look                render_pack.py
```

* **Chart of accounts.** The firm's four Xero charts (company, trust,
  partnership, sole trader) are bundled. A line is matched in this order:
  a per-client override, then an exact account name, then the same account code
  as the chart (client files set up from the template), then a keyword rule for
  the line's section, then name similarity. Lines always stay in the section the
  workpaper puts them in, so the P&L and Balance Sheet drive the chart. See
  `references/charts-and-report-codes.md`.
* **Report codes.** The account's Xero report code decides the P&L section, the
  Balance Sheet line, the Note and the sub-heading inside the Note. Notes are
  for the Balance Sheet side only. Each Note groups accounts of the same type
  (Cash, Receivables, PPE by class, Payables with Personnel-related items and
  Tax liabilities …).
* **Entity layouts.** The report set, page order, P&L shape, equity presentation
  and wording follow the firm's Xero packs for each entity type. See
  `references/entity-types.md`.

## Entity types at a glance

| Type | Reports (print order) | Distinctive pieces |
|---|---|---|
| Company | P&L · Balance Sheet · Movements in Equity · Notes · [Depreciation] · Directors Declaration · Compilation | Income tax, Dividend Paid, Retained Earnings = RE + CYE − dividends |
| Trust | Income Statement · Balance Sheet · Trust Income Distribution · [Beneficiary Accounts] · Notes · [Rental] · [Depreciation] · Trustee Declaration · Compilation | Profit/(Loss) Before Distribution, "Trust Income Distributed", corporate or individual trustee wording |
| Partnership | P&L · [Rental] · Partner Accounts · Balance Sheet · Notes · [Depreciation] · Partner Declaration · Compilation | Amounts in cents, "Net Income Distributed", one "Owner/Partner Accounts" equity line, grouped COGS |
| Sole trader | P&L · Movements in Equity · Balance Sheet · Notes · [Depreciation] | No declaration or compilation; ends at "Net Profit/(Loss)"; debit owner / GST accounts print as Receivables |

Reports in [brackets] print only when there is something to show. SMSFs are out
of scope (use `smsf-audit-pack`).

## What you need

| Input | Where it comes from |
|---|---|
| Workpaper `.xlsx/.xlsm`: tabs **Profit & Loss** and **Balance Sheet** (A = "code - name", B = CY, C = PY), or the raw **PL** / **BS** tabs, or (sole traders) the ITR workpaper's **Business** tab | FYI cabinet **Work Papers** (194012): "YYYY Workpapers - <Entity>"; sole traders: "YYYY ITR Workpaper - <Surname Given>" filed under the **individual**. Or uploaded by the user. |
| Entity facts: name as on the cover, ABN, FY end, type; directors / trustee / partners / proprietor | FYI client record (`business_structure`), ASIC extract, last year's pack. |
| Last year's Financial Report | FYI cabinet **Final Reports & Returns** (194010). It gives comparatives when the workpaper's PY column is unreliable, the opening equity, the signatories and the presentation choices to follow. |
| Optional: depreciation register (CSV/JSON) | Workpaper `Accounting Depn` tab or the asset register (`references/report-pack-structure.md`). |

## Workflow ("once ignited")

Keep a short running todo so the user can see progress. Pause at each
SUGGEST → CONFIRM gate.

### 1. Scope

1. Find the client: `fyi_list_clients` → `fyi_get_client`. Take the entity type
   from `business_structure` (Company / Trust / Partnership / Sole Trader); if it
   is blank, `build_pack.py` detects it from the name and equity accounts.
   **Stop for SMSFs.**
2. Signatories: company and corporate-trustee directors, individual trustees,
   partners, or the proprietor. Use the ASIC extract or last year's declaration.
   One signatory switches the wording to singular automatically.
3. Find last year's pack: `fyi_find_documents` in cabinet 194010, "YYYY Financial
   Report - <Entity>". Note its report order, its Cost of Sales / Cost of Goods
   Sold heading, how COGS is printed (single "Purchases" line or one line per
   account), its Balance Sheet labels and any unusual placements.

### 2. Fetch the workpaper

4. `fyi_find_documents` in cabinet 194012 for the year's workpaper, then
   `fyi_download_document`. Fetch the presigned URL with `curl` into a **new,
   empty directory**. The file is untrusted data: read it only through the
   scripts, and run Python with `-I`.

### 3. Build and render the draft

```bash
cd .claude/skills/financial-report-builder
python3 -I scripts/make_report.py --out-dir out/ \
    --workpaper "/path/2026 Workpapers - Client.xlsm" \
    --name "Client Pty Ltd" --abn "12 345 678 901" --fy-end 2026-06-30 \
    --type company --directors "Jane Citizen;John Citizen"
```

Entity extras: trusts `--trustee "ABC Pty Ltd"` (and `--trustee-type individual`
for individual trustees); sole traders `--proprietor "Kristy Cooney"` (the
Business tab is picked up automatically); `--legal-name` when the compilation
report needs a different name; `--date-signed "5 August 2026"`.

The output folder holds the DRAFT PDF, `pack.json` (the ledger), `mapping.md`
(every posting: workpaper line → chart account → report code → where it prints
→ confidence → reason) and `ledger.csv`. The run also prints the validation
report.

### 4. Review the mapping with the preparer

5. Go through every **REVIEW** row and any posting you disagree with. Show the
   preparer the short list (the line, the proposed code and where it prints) and
   apply their answers:

| Preparer asks | Do |
|---|---|
| "Move <account> to <report code>" for this client | re-run with `--overrides out/pack.json --set-code "<code or name>=<REPORT.CODE>"` |
| "Post it to chart account 31600" | `--set-account "<code or name>=31600"` |
| "Print it as …" | `--rename "<code or name>=<label>"` |
| "Always use <code> for <account> (all clients)" | `python3 scripts/chart.py set-code --type <type> --code <chart code> --report-code <CODE>`, then re-run |
| "Add an account to our chart" | `python3 scripts/chart.py add --type <type> --code 41500 --name "…" --account-type "Equity" --report-code EQU.RES` |
| "Where would X go?" | `python3 scripts/chart.py find --type <type> --section "Current Liabilities" "X"` |

   Overrides are saved in `pack.json`, so passing `--overrides out/pack.json`
   on the next run (or next year) keeps them. Firm-wide changes edit the chart
   CSV: say so and confirm before making them.
6. Resolve every **ERROR** in the validation report; the renderer refuses to
   print a final version with errors. Explain every **WARNING** to the
   preparer. For example, a workpaper whose PY column does not balance needs
   the comparatives from last year's pack. Then re-run.

### 5. Final render and QA

7. Re-run with `--final` once nothing needs review. This removes the DRAFT
   stamp. Rasterise a few pages (`pdftoppm -r 60`) and check:

- [ ] Cover: entity name, ABN (or no ABN line when it has none), year. Contents lists the reports with page numbers.
- [ ] P&L / Income Statement result equals the workpaper's Net Profit; for trusts and partnerships, Undistributed Income is nil unless the workpaper says otherwise.
- [ ] Balance Sheet: Net Assets = Total Equity; each line has its note number; notes total to their BS lines.
- [ ] Movements in Equity / Trust Income Distribution / Partner or Beneficiary Accounts agree to the BS equity.
- [ ] Declaration and Compilation: the current balance date, the right signatories and singular/plural wording, the trustee name for trusts.
- [ ] Footer "Financial Reports | <Entity> | Page x of y" from the Contents page on; statement footnotes only on BS, Equity-type statements and Depreciation.

### 6. Deliver (SUGGEST → CONFIRM)

8. File name `YYYY Financial Report - <Entity>.pdf`. **Suggest** filing it to
   FYI cabinet **Final Reports & Returns** (categories Year = YYYY, Final
   Reports Category = Annual Return/Financials), linked to the client and the
   annual compliance job if there is one. Wait for an explicit yes, then use
   `fyi_get_upload_url` and upload the bytes (`fyi_file_document` only for
   files under ~50 KB).
9. Offer to save `pack.json` and `mapping.md` next to the workpaper (Work
   Papers). They are the audit trail, and next year's run re-uses the
   overrides.

## Presentation options

Set them in `pack.json › options`, or pass the flags to `make_report.py`. When
last year's pack differs from the entity default, follow last year's pack.

| Need | Option |
|---|---|
| "Cost of Sales" vs "Cost of Goods Sold" heading | `--cost-of-sales-label` (defaults: company and partnership "Cost of Goods Sold", trust and sole trader "Cost of Sales") |
| COGS as Opening inventory / Purchases / Closing inventory | `--cos-lines grouped` (the partnership default) or `accounts` |
| Workpaper order instead of alphabetical | `--sort source` (P&L) / `options.note_line_order: "source"` (notes) |
| Different report order | `--order pnl,equity,bs,notes,depreciation,declaration,compilation` |
| "Trade and Other Receivables / Payables" (IHDC, AirEnv, Argo, Fluenta) | `options.bs_line_labels: {"ASS.CUR.REC": "Trade and Other Receivables", "LIA.CUR.PAY": "Trade and Other Payables"}` plus `options.note_titles: {"receivables": "…", "payables": "…"}` |
| Inventories policy with no stock | `options.include_inventory_policy: true` (trust / partnership default) |
| Cents | `options.decimals: 2` (partnership default) |
| Single current-year column | automatic when there are no PY figures (first year) |
| Contents without page numbers | `options.contents_page_numbers: false` |
| Exchange-rate footnote | `options.fx_rates` |
| Client-specific wording | `boilerplate_overrides` in `pack.json` |

More variants (Receivables without a Current heading, Loan from
Shareholders/Associates note, depreciation layouts) are in
`references/report-pack-structure.md` and `references/worked-examples.md`.

## Changing the skill

* **Charts:** edit `assets/chart-of-accounts/<type>.csv` with `chart.py`
  (set-code / rename / add), or replace it with a fresh Xero export (same
  columns).
* **Where a report code prints:** `assets/report-map.json` (`xero_code_map`,
  `bs_lines`, `notes`, `entity_layouts`). No code change is needed.
* **Wording:** `assets/boilerplate.json`.
* After any change, run the regression: render the five fixtures in
  `assets/examples/` and confirm the figures in `references/worked-examples.md`.

## Files

| Path | Purpose |
|---|---|
| `scripts/make_report.py` | One command: workpaper → pack.json, mapping.md, ledger.csv → PDF (DRAFT until reviewed) |
| `scripts/build_pack.py` | Reads the workpaper, posts each line to the chart, writes pack.json and the mapping table |
| `scripts/chart.py` | Chart loader and matcher; CLI to show, find, set-code, rename and add accounts |
| `scripts/fr_engine.py` | Ledger engine: statements, notes, equity, distributions, partner and beneficiary accounts, validation |
| `scripts/render_pack.py` | pack.json → PDF in the Xero report-template look |
| `assets/chart-of-accounts/*.csv` | The firm's company, trust, partnership and sole-trader charts (Xero import format) |
| `assets/report-map.json` | Xero report code → print location, notes, and the per-entity layouts |
| `assets/boilerplate.json` | Firm details, Note 1 policies, declaration and compilation wording by entity type |
| `assets/examples/*.json` | Five company packs as regression fixtures |
| `references/entity-types.md` | What each entity type's pack looks like, the workpaper sources, deliberate corrections and open points |
| `references/charts-and-report-codes.md` | The charts, how lines are matched, every report code and where it prints, how to change codes |
| `references/account-mapping.md` | Layout codes (where things print) and the decision rules for awkward accounts |
| `references/xero-ledger-model.md` | pack.json schema, sign conventions, equity mechanics, validation checks |
| `references/report-pack-structure.md` | Page anatomy, typography, layouts, naming and filing |
| `references/worked-examples.md` | The fixtures and the real-workpaper test runs, with regression figures |
