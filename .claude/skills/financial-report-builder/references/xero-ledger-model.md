# How the "Xero ledger" model works

The Financial Reports pack Fortis issues (P&L, Balance Sheet, Movements in
Equity, Notes, Depreciation Schedule, Directors Declaration, Compilation Report)
is Xero's **Report Templates** output. Xero never lays that pack out by hand: it
derives every line from two attributes on each account in the chart of accounts.

| Attribute | What it drives |
|---|---|
| **Account type** (Revenue, Other Income, Direct Costs, Expense, Bank, Current Asset, Fixed Asset, Current Liability, Non-current Liability, Equity ...) | Which statement the account lands on and which broad block (income vs expense, current vs non-current, asset vs liability). |
| **Report code** (`REV.TRA`, `EXP.COS`, `ASS.CUR.CAS.BAN`, `LIA.CUR.TAX` ...) | The exact line on the P&L / Balance Sheet, the Note it is disclosed in, and the sub-heading inside that Note. |

This skill reproduces that mechanism for entities that have **no Xero file**.
The workpaper's P&L and Balance Sheet tabs are treated as a trial balance; each
line is "posted" to an account with a type and a report code; the pack is then
generated from the codes, exactly as Xero would.

```
workpaper P&L + BS tabs ──► build_pack.py ──► pack.json (the ledger) ──► render_pack.py ──► PDF
  (or ITR Business tab)      │  chart.py: firm chart for the entity type  │  fr_engine.py + report-map.json
                             │  (match by section, name, code, rules)     │  (xero_code_map, entity_layouts)
                             └  mapping.md, ledger.csv                    └  boilerplate.json, fonts
```

`make_report.py` runs both steps into one folder and stamps the PDF DRAFT until
nothing needs review.

## The ledger file (`pack.json`)

```json
{
  "entity": {"name": "Gersbach Family Trust", "abn": "39 364 106 853", "type": "trust",
             "fy_end": "2026-06-30", "directors": ["Ashley Gersbach", "Sharon Gersbach"],
             "trustee": "…", "trustee_type": "corporate", "legal_name": "…", "date_signed": "…"},
  "options": {"sort": "alpha", "cost_of_sales_label": "Cost of Sales", "cos_lines": "accounts",
              "decimals": 0, "include_inventory_policy": null, "depreciation_layout": "simple",
              "report_order": null, "fx_rates": null, "extra_policies": [], "bs_line_labels": {},
              "contents_page_numbers": true, "single_column": null},
  "overrides": {"report_codes": {"46020": "EQU.DRA.2"}, "accounts": {}, "labels": {}},
  "equity": {"opening_py": 85089},
  "equity_movements": [{"label": "Share Capital", "py": 100}],
  "accounts": [
    {"code": "46020", "name": "Drawings - Sharon Gersbach", "type": "Equity", "report_code": "EQU.DRA.2",
     "chart_code": "46020", "chart_name": "Drawings - Beneficiary 2", "cy": -90472.96, "py": -83212.22,
     "source": "bs:row32", "person": 2,
     "mapping": {"confidence": "chart-code", "reason": "same code as chart account 46020 …",
                 "section": "Equity", "prints_as": "Equity: Drawings (person #2)", "review": false}}
  ],
  "depreciation": {"layout": "simple", "groups": [{"name": "Motor Vehicle", "assets": [ ... ]}]}
}
```

`build_pack.py` writes every field above. The fixtures in `assets/examples/`
are hand-made ledgers with only `name`, `report_code`, `cy` and `py`.
Entity-type defaults for `options` come from `report-map.json ›
entity_layouts.<type>.options`, and the pack's own `options` win.

* `accounts[].cy / py` are the **closing balances** for the current and prior
  year in their natural sign (see below). Keep cents when you have them.
* `report_code` is the only thing the engine reads to place an account. It may
  be a Xero code from the firm chart (resolved through `xero_code_map`) or a
  layout code. `chart_code` / `chart_name` record the ledger posting; `mapping`
  records why. A `.n` suffix on `EQU.ADV/DRA/PFT` is the partner / beneficiary
  number, and the person's name is read from the account name.
* `overrides` holds the preparer's per-client choices. Pass the pack back with
  `--overrides` to keep them on the next run.
* `equity.opening_py` is the prior-year *opening* equity (the closing equity two
  years back) so the PY column of Movements in Equity can be completed.
* `equity_movements` are movements the engine cannot derive from two Balance
  Sheet columns (anything in the PY column other than profit; or a label you
  want to force).

## Sign conventions (Xero's)

| Item | Stored as | Printed as |
|---|---|---|
| Income, other income | positive | positive |
| Cost of sales, expenses, income tax | positive | positive (a credit-balance expense such as Closing stock or a currency gain prints in brackets) |
| Dividends paid (`EQU.DIV`) | positive = amount paid | positive in the P&L "Dividend Paid" section; deducted from Retained Earnings |
| Assets | debit positive | positive; accumulated depreciation negative in brackets |
| Liabilities | credit positive | positive; a debit balance (GST refund due, ITA in credit) prints in brackets as a negative liability |
| Equity | credit positive | positive; accumulated losses in brackets |

`build_pack.py` auto-detects a workpaper that stores expenses as negatives
(`--expenses-negative auto`) and converts dividends taken from the equity
section (debit) into the positive "paid" figure.

## Rounding

Partnership packs print cents (`options.decimals: 2`); the others print whole
dollars. Xero calculates every total from unrounded ledger balances and rounds
only when printing, so a column can be out by $1 (Wisdom IT: Total Current Liabilities
8,143 vs 9,691 - 1,547 = 8,144). The engine does the same: amounts stay unrounded,
`fmt()` rounds half-away-from-zero at print time. Validation treats differences
up to `ROUNDING_TOL` ($5) as INFO because whole-dollar inputs (a re-keyed PDF)
cannot foot perfectly; cents-level inputs from a workpaper should reconcile to $0.

## What is printed and what is suppressed

| Rule | Detail |
|---|---|
| Account nil in both years | Dropped (set `"keep": true` on the account to force it, as Frontier's "Discounts given" line). |
| P&L section with no accounts | Section omitted, but the computed lines (Gross Profit from Trading, Total Income, Profit/(Loss) before Taxation, Net Profit After Tax, Net Profit After Tax & Dividend) always print. |
| Income Tax Expense / Dividend Paid sections | Printed only when non-nil in either year. |
| Balance Sheet line nil in both years | Suppressed, even if its Note prints. |
| Note whose accounts are all nil | Suppressed. A Note prints if **any account** in it is non-nil, so a fully written-down asset (cost 489, accumulated (489)) still gets a Note with a nil total - Wisdom IT Note 4, Complex Communicators Note 6, Capstone Notes 5-6. |
| Balance Sheet sub-section (e.g. Non-Current Assets) with no lines | Omitted together with its total. |

## Ordering

* Accounts inside a section print **alphabetically by name** (Xero default,
  `options.sort = "alpha"`). Use `"source"` to keep workpaper order (Wisdom IT's
  pack shows Wages, Superannuation, Telephone out of alphabetical order, so its
  template was manually ordered).
* Inside a PPE / Intangibles Note the cost line prints before its accumulated
  depreciation line.
* Notes are numbered from 2 in the order in `report-map.json` (`notes_order`):
  Cash, Receivables, Inventory, Prepayments, PPE, Financial Assets, Intangibles,
  Investments, Other Assets, Payables, Provisions, Financial Liabilities, Loan
  from Shareholders/Associates, Beneficiary Entitlements, Other Liabilities,
  Reserves. Note 1 is always the accounting policies. `options.notes_order`
  pins a client's own order (Frontier puts Provisions before Payables).
* Inside notes, lines are alphabetical; `options.note_line_order: "source"`
  keeps workpaper order. PPE notes print "<Class> at Cost" / "Accumulated
  Depreciation of <Class>" per class, or the account names with
  `options.ppe_note_lines: "accounts"` (the five fixtures).
* Cost of sales prints opening stock first and closing stock last;
  `options.cos_lines: "grouped"` prints Opening inventory / Purchases / Closing
  inventory (partnership default).
* Reports print in `options.report_order`, else the entity layout's order
  (`references/entity-types.md`). Frontier Travel's pack puts Movements in
  Equity before the Balance Sheet.

## Equity mechanics

Xero keeps three separate equity balances that the pack collapses into one
"Retained Earnings" line:

```
Retained Earnings (printed) = EQU.RET (prior years)  +  EQU.CYE (current year earnings)  -  EQU.DIV (dividends)
```

* If the Balance Sheet export has a **Current Year Earnings** line, code it
  `EQU.CYE`; the engine checks it equals Net Profit After Tax from the P&L.
  If absent, the engine uses the computed NPAT.
* **Movements in Equity** is derived: Opening = PY Total Equity; Increases =
  Profit for the Period (after dividends) + change in Share Capital + change in
  Reserves + any residual movement in `EQU.RET` (a direct posting, shown as a
  "Retained Earnings" line - Frontier's (125,000)) + user-supplied
  `equity_movements`; Total Equity = CY Total Equity.
* The PY column needs `equity.opening_py`; without it the engine backs the
  opening figure out (PY closing less PY profit less supplied PY movements) and
  flags it INFO so you check it against last year's pack.

## Trust and partnership mechanics

* Distributions (`EXP.DIS`, plus losses applied `EXP.CFL` and reconciliation
  adjustments) close the P&L to **Undistributed Income** = Profit/(Loss) Before
  Distribution − distributions. Partnerships print the distribution with the sign
  that offsets the result.
* The Balance Sheet's Current Year Earnings must equal NPAT − dividends paid
  through the P&L − distributions. This is nil for a fully distributed trust or
  partnership.
* **Trust Income Distribution**: Current Year Earnings (= Profit/(Loss) Before
  Distribution), losses applied (sign reversed), Net Trust Income for
  Distribution, each distribution account, Undistributed Trust Income.
* **Partner / Beneficiary Accounts**: per-person equity accounts are grouped by
  the person's name, then by the code's number. Each person's block is Opening
  Balance ("Opening balance - <name>" accounts), Funds contributed, Drawings,
  Share of profits and the total. The summary adds the categories across people.
  For partnerships the BS shows the grand total as one "Owner/Partner Accounts"
  line.
* Validation ties the profit shares to the distributions and checks
  that a partnership's Undistributed Income is nil.

## Validation checks (`Pack.validation`)

| Level | Check | Meaning |
|---|---|---|
| ERROR | `mapping` | An account has no report code, or a code not in `report-map.json`. |
| ERROR | `balance_sheet_balances` | Net Assets ≠ Total Equity beyond rounding tolerance (CY or PY). |
| ERROR | `current_year_earnings` | BS Current Year Earnings ≠ NPAT − P&L dividends − distributions. |
| ERROR | `entity` | Missing name / FY end. |
| WARNING | `retained_earnings_rollforward` | RE moved beyond profit and dividends; a movement line was added - confirm the label. |
| WARNING | `equity_opening_py` | Supplied PY opening equity does not roll to PY closing. |
| WARNING | `depreciation_ties_pl` / `depreciation_ties_ppe` | Schedule totals do not tie to the P&L depreciation lines / PPE note. |
| WARNING | `comparatives` | No prior-year figures at all - the pack prints one column (first-year layout). |
| WARNING | `entity` | No ABN (the cover omits the ABN line); no signatories while a declaration is printed. |
| WARNING | `distribution_ties` | Partner / beneficiary profit shares ≠ the P&L distribution. |
| WARNING | `undistributed_income` | A partnership with undistributed income. |
| INFO | `division_7a` | A company has Division 7A loan accounts - confirm the agreement and minimum yearly repayment. |
| INFO | `person_accounts` | No "Opening balance - <name>" accounts - the Partner / Beneficiary Accounts show closing balances only. |
| INFO | `gst_refund`, `income_tax`, rounding notes | Presentation reminders; no action unless the firm template says otherwise. |

`render_pack.py` refuses to render while any ERROR remains. `--allow-errors`
renders anyway, stamped DRAFT. `make_report.py` stamps DRAFT while any mapping
line needs review, until it is run with `--final`.
