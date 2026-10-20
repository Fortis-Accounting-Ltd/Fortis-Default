# Report pack structure

Anatomy of the Fortis "Financial Reports" pack as issued from Xero report
templates. It was first captured from the five 2025/2026 company packs and then
checked against the FY2026 company packs (IHDC, RSJ, JL Auto and others).
`render_pack.py` reproduces it page for page; `assets/boilerplate.json` holds
the wording. Trust, partnership and sole-trader packs use the same page
anatomy with their own report set. See `references/entity-types.md`.

## Page order (company default)

| # | Page | Notes |
|---|---|---|
| 1 | **Cover** | "Financial Reports" (blue, bold) · entity name · "ABN xx xxx xxx xxx" · "For the year ended 30 June 20XX" · "Prepared by Fortis Accounting Partners". No footer. |
| 2 | **Contents** | One line per report, in print order, with its start page in bold on the left (`options.contents_page_numbers`). Carries the footer. |
| 3 | **Profit and Loss Statement** | Columns `2026 | 2025`. |
| 4 | **Balance Sheet** | "As at 30 June 2026"; columns `NOTES | 30 JUNE 2026 | 30 JUNE 2025`. |
| 5 | **Movements in Equity** | Columns `2026 | 2025`. (Frontier prints this *before* the Balance Sheet - `options.report_order`.) |
| 6+ | **Notes to the Financial Statements** | Note 1 policies, then numbered Notes each with its own `2026 | 2025` header. |
| … | **Depreciation Schedule** / **Accounting Depreciation Schedule** | Only when a schedule is supplied. Landscape for the register / standard layouts. |
| … | **Directors Declaration** | Signature block per director + Dated. |
| last | **Compilation Report** | APES 315 wording, signed John Kalachian. |

Every page from the Contents page onwards carries the footer
`Financial Reports | <Entity>` (left) and `Page x of y` (right). Continuation
pages repeat the report title in small type at the top left and the column
header row of the table.

Footnotes above the footer rule (`report-map.json › footnotes`):

* Balance Sheet, Trust Income Distribution, Partner / Beneficiary Accounts and
  Depreciation Schedule: *"The accompanying notes form part of these financial
  statements. These statements should be read in conjunction with the attached
  compilation report."* Only the first sentence prints when the pack has no
  Compilation Report.
* Notes pages: *"These notes should be read in conjunction with the attached
  compilation report."* This is dropped when there is no Compilation Report.
* P&L / Income Statement, Rental Schedule, Movements in Equity, Declaration,
  Compilation Report: none. This was checked against the FY2026 Xero packs.
  The original company skill printed it on the P&L as well.

While any mapping line still needs review, or the render was forced with
`--allow-errors`, every page carries a pale diagonal **DRAFT** stamp.

## Typography (Xero report template look)

| Element | Face | Size | Colour |
|---|---|---|---|
| Report title | Source Sans 3 SemiBold | 22 pt | #06B3E8 (Xero blue) |
| Entity / period lines | SemiBold | 14 pt | black |
| Column headers (2026 / 2025 / NOTES) | SemiBold | 7 pt, upper case for BS dates | black |
| Section heading (Income, Assets, Note title) | SemiBold | 10 pt | black, rule below |
| Sub-heading (Current Assets, Bank Accounts) | SemiBold | 8 pt | |
| Account line | Regular | 8 pt | light grey rule below |
| Total line | SemiBold | 8 pt | |
| Computed line (Gross Profit, Net Assets, Total Equity, note totals) | SemiBold | 10 pt label / 8 pt figures | rule below |
| Body text (policies, declaration, compilation) | Regular | 8.5 pt | |
| Footer | Regular | 7 pt | |

Numbers: whole dollars with thousands separators, negatives in brackets, nil as
"-". A4 portrait, ~17 mm side margins.

## Profit and Loss Statement layout

```
Income                                  ← REV.TRA accounts
  Total Income
Cost of Sales | Cost of Goods Sold      ← EXP.COS (only if any)
  Total Cost of Sales
Gross Profit from Trading               ← always
Other Income                            ← REV.OTH
  Total Other Income
Total Income                            ← always (Gross Profit + Other Income)
Expenses                                ← EXP.OPE, alphabetical
  Total Expenses
Profit/(Loss) before Taxation           ← always
Income Tax Expense                      ← EXP.TAX (only if non-nil)
  Total Income Tax Expense
Net Profit After Tax                    ← always
Dividend Paid                           ← EQU.DIV (only if non-nil)
  Total Dividend Paid
Net Profit After Tax & Dividend         ← always
```

## Balance Sheet layout

```
Assets
  Current Assets            lines with note refs → Total Current Assets
  Non-Current Assets        (omitted when empty)  → Total Non-Current Assets
  Total Assets
Liabilities
  Current Liabilities       → Total Current Liabilities
  Non-Current Liabilities   → Total Non-Current Liabilities
  Total Liabilities
Net Assets
Equity
  Retained Earnings | Reserves | Share Capital   (nil lines suppressed except Retained Earnings)
  Total Equity
[Exchange-rate footnote when options.fx_rates is set - Padel]
```

Several Balance Sheet lines can point at the **same Note** (Payables,
Personnel-related items and Taxation all reference the Payables note).

## Movements in Equity layout

```
Equity
  Opening Balance
  Increases
    Profit for the Period            ← Net Profit After Tax & Dividend
    Share Capital                    ← Δ EQU.SHA (derived) or supplied
    <Reserve name>                   ← Δ EQU.RES
    Retained Earnings                ← residual direct posting (Frontier (125,000))
    Other Increases / any supplied movement
    Total Increases
Total Equity
```

## Notes layout

**Note 1 - Statement of Significant Accounting Policies**: paragraphs (a) basis
of preparation incl. AASB 1031 and AASB 110, (b) Property, Plant and Equipment,
(c) Inventories. Wisdom IT and Complex (no stock) still print (c); Frontier does
not - controlled by `options.include_inventory_policy` (default: only when an
Inventory note exists). Extra policies can be switched on via
`options.extra_policies` (income_tax, revenue, gst, employee_benefits,
foreign_currency).

**Notes 2+**: each note starts with a `2026 | 2025` header, then the title
(`3. Receivables`), tiers, and `Total <Note title>`.

| Note | Tier structure |
|---|---|
| Cash and Cash Equivalents | Bank Accounts / Other Cash Items → `Total Bank Accounts` … |
| Receivables, Financial Assets, Financial Liabilities, Other Assets/Liabilities | Current / Non Current (Financial Liabilities adds Secured / Unsecured under Non Current) |
| Inventory | Inventories |
| Property Plant and Equipment | one tier per class: cost line, accumulated depreciation line, `Total <class>` |
| Intangibles | Goodwill / Other Intangible Assets |
| Payables | Current → Personnel-related items → Tax liabilities (→ Financial Liabilities in the Wisdom variant), each with a total |
| Provisions, Loan from Shareholders/Associates, Prepayments, Investments | flat list |

## Depreciation schedule layouts

| `layout` | Title | Columns | Orientation |
|---|---|---|---|
| `simple` | Accounting Depreciation Schedule | NAME · COST · OPENING VALUE · PURCHASES · DISPOSALS · DEPRECIATION · CLOSING VALUE | portrait (Wisdom) |
| `standard` | Depreciation Schedule | NAME · RATE · METHOD · PURCHASED · COST · OPENING VALUE · PURCHASES · DISPOSALS · DEPRECIATION · CLOSING VALUE | landscape (Padel) |
| `register` | Depreciation Schedule | NAME · ASSET NUMBER · ASSET TYPE · PURCHASED · COST · OPENING ACCUM DEP · OPENING VALUE · PURCHASES · DISPOSALS · DEPRECIATION · CLOSING ACCUM DEP · CLOSING VALUE · EFFECTIVE LIFE · DEP START DATE | landscape (Frontier) |

Assets are grouped by class with `Total <class>` rows and a grand `Total`. Set
`depreciation.orientation` to force portrait/landscape.

**Input file** for `build_pack.py --depreciation`: CSV with a `group` column and
any of `name, asset_number, asset_type, rate, method, purchased, cost,
opening_accum, opening_value, purchases, disposals, depreciation, closing_accum,
closing_value, effective_life, dep_start`; the layout is inferred from the
columns present (or a JSON file in the pack format). Export it from the
workpaper's `Accounting Depn` tab or the client's asset register.

Ties checked: Σ depreciation = P&L depreciation/amortisation/write-off lines;
Σ closing value = Property, Plant and Equipment note (current year).

## Declaration (Directors / Trustee / Partner)

Plural wording (2+ signatories) or singular wording (1) from
`boilerplate.json › declaration.<type>`. Trusts use corporate-trustee or
individual-trustee wording, and partnerships the Partner Declaration. Sole
traders have no declaration unless it is added to the report order. The date
beside the signatures is blank unless `entity.date_signed` is given. Paragraph 1 refers to "30 June 20XX" - **derived from
`entity.fy_end`**, never typed. One `Director: ______` block per name in
`entity.directors`, then `Dated: ______`. Signing happens later through FuseSign
(the Frontier pack carries the FuseSign audit trail as extra pages).

## Compilation Report

"Compilation report to <Entity>" → paragraph 1 (balance sheet as at
<fy_long>) → *The Responsibility of the Directors* → *Our Responsibility*
(APES 315, APES 110) → *Assurance Disclaimer* → signature block:
John Kalachian / Fortis Accounting Partners / Chartered Accountants / Suite 9
Level 12 101 Bathurst Street / SYDNEY NSW 2000 / Dated. Singular "director"
wording for a sole director. Trusts: "The Responsibility of the Trustee";
partnerships: "…of the Partners"; sole traders: "…of the Proprietor" (only when
added). The report is addressed to `entity.legal_name` when given (a
partnership's legal name can differ from its trading name).

## Errors in the source packs the generator must not repeat

These slipped through in the example packs because the templates were edited by
hand. The engine derives every date and name from `entity`, so they cannot recur:

| Pack | Slip |
|---|---|
| Complex Communicators 2026 | Directors Declaration says "as at 30 June **2025**"; "Board of Director". |
| Frontier Travel 2026 | Compilation Report says "balance sheet as at 30 June **2025**"; Note 1 heading "Accounting Policie"; asset type "Office Equiepement". |
| Padel Point 2026 | Declaration starts "he directors"; "Capital Loss Resreve"; entity printed as "Padel Point Pty Ltd." on some pages and without the full stop on others. |
| Wisdom IT 2026 | Payables note prints "Total Financial Liabilities" for what is the note total. |

## Naming and filing

File name: `YYYY Financial Report - <Entity Name>.pdf` (FY year, e.g.
`2026 Financial Report - Padel Point Pty Ltd.pdf`). FYI: cabinet **Final Reports
& Returns**, categories **Year = 20XX** and **Final Reports Category = Annual
Return/Financials**, linked to the entity and, where one exists, the annual
compliance job.
