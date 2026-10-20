# Charts of accounts and report codes

The skill works like a Xero file set up from the firm's template. Every
workpaper line is **posted to an account in the firm's chart for the entity
type**. The account's **Xero report code** then decides where the line prints.
This file covers the charts, how a line finds its account, every report code
and where it prints, and how to change a code when someone asks.

## 1. The four charts

`assets/chart-of-accounts/` holds the firm's Xero charts in Xero's
import/export format (`*Code, Report Code, *Name, Reporting Name, *Type,
*Tax Code, Description, Dashboard, Expense Claims, Enable Payments`). A chart
exported from Xero can replace a file unchanged, and the bundled files can be
imported into Xero.

| File | Entity type | Accounts | Report codes |
|---|---|---|---|
| `company.csv` | company | 385 | 101 |
| `trust.csv` | trust | 395 | 116 |
| `partnership.csv` | partnership | 413 | 108 |
| `sole-trader.csv` | sole trader | 378 | 96 |

The bundled copies are the four uploads with these clean-ups:

* Names are trimmed and rows are sorted by numeric code.
* Partnership: the second "Distribution to Partner 3" (90013) became 90014
  "Distribution to Partner 4".
* Trust: the 99310-99340 "Beneficiary/Shareholder profit distribution" accounts
  had no usable tax code. They are now BAS Excluded.
* Accounts named "(DO NOT USE)" (Xero system accounts: Accounts Receivable /
  Payable, Unpaid Expense Claims, Historical Adjustment, Rounding, Tracking
  Transfers) are never chosen by the matcher.

**Points to raise with the firm** (the skill works either way):

| Chart | Point |
|---|---|
| all | `LIA.CUR.PAY.PAY` holds PAYG withholding / instalment payable and wages payable. The skill prints the code under **Personnel-related items**, as the firm's Happy Inn pack does. IHDC's own Xero file printed PAYG withholding / instalments under **Taxation**. To follow IHDC firm-wide, run `chart.py set-code --type company --code 34300 --report-code LIA.CUR.TAX` (and 35510). |
| trust | No "Share of profits - Beneficiary n" accounts (`EQU.PFT.n`), although trust packs with Beneficiary Accounts use them. The skill falls back to the layout code `EQU.PFT.n` without asking for review. Add the accounts with `chart.py add` if the ledger needs them. |
| partnership, sole trader | Client-specific accounts left in the template: 13911 "Loans - Sushi Musketeers", 31603 "Loan from Shuhuachen". |
| trust | 44050-44070 "Opening balance" carry `EQU.UDE.2`, a user-defined equity code that prints as Other Equity. |

## 2. How a workpaper line is posted

`build_pack.py` reads each line (`code - name`, CY, PY) and the section header
it sits under (Trading Income, Cost of Sales, Other Income, Operating Expenses;
Bank, Current Assets, Fixed Assets, Non-current Assets, Current Liabilities,
Non-current Liabilities, Equity). `chart.py` then picks the account:

| Step | Rule | Confidence shown |
|---|---|---|
| 1 | Per-client override (`--set-account`, `--set-code`, overrides file) | `override` |
| 2 | Exact account name inside the accounts the section allows | `chart-exact` |
| 3 | The workpaper code is a chart code in the same section, and the names fit (client files set up from the template, e.g. 46020 Drawings - Beneficiary 2) | `chart-code` |
| 4 | Keyword rule for the section (`RULES` in `chart.py`) → report-code family → most similar account in it, or the account the rule names (`prefer`) | `rule` |
| 5 | Strong name similarity (≥ 0.80) | `chart` |
| 6 | Section catch-all (trading income → `REV`; cost of sales → `EXP.COS`; expenses → `EXP.OPR`) | `rule` |
| 7 | Moderate similarity (≥ 0.45) | `similar` |
| 8 | Section default | `type` - **REVIEW** |

What keeps the postings honest:

* **The section restricts the families.** A line under Non-current Liabilities can
  only land on a `LIA.NCL` account; a line under Operating Expenses only on an
  expense account. A rule may name a deliberate exception: income tax,
  dividends, distributions, a loss on disposal, a debit owner account for a sole
  trader.
* **Nature decides the sign.** Amounts are stored in the natural sign of the
  account they land on. A credit line printed in a debit position (e.g. a GST
  refund printed as a receivable) is reversed and the reason is recorded.
  Workpapers that show expenses as negatives are detected automatically.
* **Uncoded subtotals are skipped.** Totals, Gross Profit, Net Profit and
  similar lines are recomputed by the engine. An uncoded line whose figures
  equal the running result (for example a mistyped "Opearting Profit") is
  treated as a subtotal.
* **REVIEW** is raised for a section default, for a rule whose reason says
  REVIEW (an owner's account held as a liability in a company or trust), and for
  a rule code that no chart account carries, unless that code's presentation is
  settled in the firm's packs (`SETTLED_LAYOUT_CODES`: non-current Division 7A
  receivable, share of profits, drawings, funds contributed, non-current
  payables, losses applied, distributions).

### Entity-specific rules

| Entity | Rule | Evidence |
|---|---|---|
| trust | A current "Loan - <person or related party>" (no bank or finance words) → `LIA.CUR.PAY.TRA`: Payables › Current | Gersbach, Chawla, Leembruggen, AIF, Premier Fire packs |
| sole trader | An owner / drawings / funds-contributed account or GST held under liabilities with a **debit** balance → Receivables › Current (`ASS.CUR.LOA` / `ASS.CUR.REC`), sign reversed, no review | True Talkers FY2025 pack |
| trust, partnership | Per-person equity accounts are numbered by person in workpaper order: Funds contributed `EQU.ADV.n`, Drawings `EQU.DRA.n`, Share of profits `EQU.PFT.n` | Partner / Beneficiary Accounts statements |
| all | A currency gain listed under income stays Other Income (`REV.OTH`); under expenses it is `EXP.FOR` | Premier Fire, Padel |

## 3. Report code → where it prints

There are two layers:

1. **Xero report code** on the chart account (`LIA.CUR.PAY.EMP`, `ASS.NCA.FIX.VEH.ACC`, `EQU.DRA.2` …).
2. `report-map.json › xero_code_map` turns it into a **layout code** (`LIA.CUR.EMP`,
   `ASS.NCA.PPE.MOT`, `EQU.DRA` + person 2). The layout code drives the P&L section,
   BS line and Note (`references/account-mapping.md`). The longest matching prefix
   wins. A few codes hold accounts of different natures, so a `refine` rule on the
   account name splits them (cards in `LIA.CUR.LOA` → Payables; term debtors in
   `ASS.CUR.TER` → Receivables). Property, Plant and Equipment takes its class
   from the code (`LAC`/`BUC` Land and Buildings, `LEA` Leasehold Improvements,
   `VEH` Motor Vehicles, `PLA` Plant and Equipment, `FIX` Other Fixed Assets).
   Pools and staff amenities inside `BUC` are refined by name. A trailing `.1`
   `.2` … is the partner / beneficiary number.

Layout codes are also valid report codes. `--set-code "Loan - Director=LIA.NCA.SHA"`
places a line on the "Loan from Shareholders/Associates" note directly.

Every code in the four charts (C = company, T = trust, P = partnership,
S = sole trader). "Prints as" is the Balance Sheet line or P&L section, then
the Note › sub-heading:

| Report code | Charts | Prints as | Example accounts |
|---|---|---|---|
| `ASS.CUR` | CTPS | Current Assets: Other Current Assets - Note Other Assets > Current | Suspense |
| `ASS.CUR.CAS` | CTPS | Current Assets: Cash and Cash Equivalents - Note Cash and Cash Equivalents > Other Cash Items | Cash on hand; Petty cash |
| `ASS.CUR.CAS.BAN` | CTPS | Current Assets: Cash and Cash Equivalents - Note Cash and Cash Equivalents > Bank Accounts | Bank currencies; Cheque account; Cheque account 1 ... |
| `ASS.CUR.INY` | CTPS | Current Assets: Inventory - Note Inventory > Inventories | Finished goods; Inventory; Primo inventory ... |
| `ASS.CUR.INY.WIP` | CTPS | Current Assets: Inventory - Note Inventory > Work in Progress | Work in progress |
| `ASS.CUR.LOA` | CTPS | Current Assets: Receivables - Note Receivables > Current | Loans - Associates; Loans - Other; Loans - Related party ... |
| `ASS.CUR.REC` | CTPS | Current Assets: Receivables - Note Receivables > Current | Bills receivable; Leases |
| `ASS.CUR.REC.ADA` | CTPS | Current Assets: Receivables - Note Receivables > Current | Less provision for doubtful debts; Provision for unearned revenue |
| `ASS.CUR.REC.PRE` | CTPS | Current Assets: Prepayments - Note Prepayments | Prepayments |
| `ASS.CUR.REC.TRA` | CTPS | Current Assets: Receivables - Note Receivables > Current | Sundry debtors; Trade debtors |
| `ASS.CUR.REL` | CTPS | Current Assets: Receivables - Note Receivables > Current | Payments made - loans to related parties (Clearing); Proceeds from repayments - loans to related parties (Clrg) |
| `ASS.CUR.TER` | CTPS | Current Assets: Financial Assets - Note Financial Assets > Current; by name: /debtor/ -> Current Assets: Receivables | Short term deposits; Term debtors; Term deposits |
| `ASS.NCA.FIX` | CTPS | Non-Current Assets: Property, Plant and Equipment - Note Property Plant and Equipment > Other Fixed Assets | Tangible Assets |
| `ASS.NCA.FIX.BUC` | CTPS | Non-Current Assets: Property, Plant and Equipment - Note Property Plant and Equipment > Land and Buildings | Buildings at cost; Less accumulated depreciation staff amenities; Less accumulated depreciation Staff amenities ... |
| `ASS.NCA.FIX.BUC.ACC` | CTPS | Non-Current Assets: Property, Plant and Equipment - Note Property Plant and Equipment > Land and Buildings | Less accumulated amortisation property improvement; Less accumulated amortisation Property improvement; Less accumulated depreciation building ... |
| `ASS.NCA.FIX.LAC` | CTPS | Non-Current Assets: Property, Plant and Equipment - Note Property Plant and Equipment > Land and Buildings | Freehold land at cost; Leasehold land at cost; Less accumulated amortisation - Freehold land ... |
| `ASS.NCA.FIX.LEA` | CTPS | Non-Current Assets: Property, Plant and Equipment - Note Property Plant and Equipment > Leasehold Improvements | Leasehold improvements at cost |
| `ASS.NCA.FIX.LEA.ACC` | CTPS | Non-Current Assets: Property, Plant and Equipment - Note Property Plant and Equipment > Leasehold Improvements | Less accumulated amortisation leasehold improvements; Less accumulated amortisation Leasehold improvements |
| `ASS.NCA.FIX.PLA` | CTPS | Non-Current Assets: Property, Plant and Equipment - Note Property Plant and Equipment > Plant and Equipment | Furniture and fittings at cost; Office equipment at cost; Plant and equipment at cost ... |
| `ASS.NCA.FIX.PLA.ACC` | CTPS | Non-Current Assets: Property, Plant and Equipment - Note Property Plant and Equipment > Plant and Equipment | Less accumulated depreciation furniture & fitting; Less accumulated depreciation Furniture & fitting; Less accumulated depreciation office equipment ... |
| `ASS.NCA.FIX.VEH` | CTPS | Non-Current Assets: Property, Plant and Equipment - Note Property Plant and Equipment > Motor Vehicles | Motor vehicles at cost |
| `ASS.NCA.FIX.VEH.ACC` | CTPS | Non-Current Assets: Property, Plant and Equipment - Note Property Plant and Equipment > Motor Vehicles | Less accumulated depreciation motor vehicles; Less accumulated depreciation Motor Vehicles |
| `ASS.NCA.INT` | CTPS | Non-Current Assets: Intangibles - Note Intangibles > Other Intangible Assets | Debenture issue expenses; Exploration expenditure - Phases; Formation cost ... |
| `ASS.NCA.INT.GOO` | CTPS | Non-Current Assets: Intangibles - Note Intangibles > Goodwill | Goodwill; Goodwill and consolidation |
| `ASS.NCA.INT.GOO.IMP` | CTPS | Non-Current Assets: Intangibles - Note Intangibles > Goodwill | Less accumulated amortisation Goodwill |
| `ASS.NCA.INV` | CTPS | Non-Current Assets: Investments - Note Investments | Purchase of investment (Clearing) |
| `ASS.NCA.INV.SHA` | CTPS | Non-Current Assets: Investments - Note Investments | Change in market value of Government securities; Change in market value of Managed investments; Change in market value of Shares in associated companies ... |
| `ASS.NCA.INV.TER` | CTPS | Non-Current Assets: Receivables - Note Receivables > Non Current; by name: /term deposit|investment/ -> Non-Current Assets: Financial Assets | Deposit paid |
| `EQU` | C | Equity: Other Equity | Shareholder's Equity |
| `EQU.ADV` | CS | Equity: Capital Accounts | Deferred taxes; Funds contributed |
| `EQU.ADV.1` | TP | Equity: Capital Accounts (person #1) | Funds contributed - Beneficiary #1; Funds contributed - partner #1; Opening balance - partner #1 |
| `EQU.ADV.2` | TP | Equity: Capital Accounts (person #2) | Funds contributed - Beneficiary #2; Funds contributed - partner #2; Opening balance - partner #2 |
| `EQU.ADV.3` | TP | Equity: Capital Accounts (person #3) | Funds contributed - Beneficiary #3; Funds contributed - partner #3; Opening balance - partner #3 |
| `EQU.ADV.4` | TP | Equity: Capital Accounts (person #4) | Funds contributed - Beneficiary #4; Funds contributed - partner #4; Opening balance - partner #4 |
| `EQU.DRA` | CS | Equity: Drawings | Drawings; Drawings - Educational; Drawings - Income tax ... |
| `EQU.DRA.1` | TP | Equity: Drawings (person #1) | Drawings - Beneficiary 1; Drawings - Education - partner #1; Drawings - Income tax - partner #1 ... |
| `EQU.DRA.2` | TP | Equity: Drawings (person #2) | Drawings - Beneficiary 2; Drawings - Education - partner #2; Drawings - Income tax - partner #2 ... |
| `EQU.DRA.3` | TP | Equity: Drawings (person #3) | Drawings - Beneficiary #3; Drawings - Education - partner #3; Drawings - Income tax - partner #3 ... |
| `EQU.DRA.4` | TP | Equity: Drawings (person #4) | Drawings - Beneficiary #4; Drawings - Education - partner #4; Drawings - Income tax - partner #4 ... |
| `EQU.DRA.5` | T | Equity: Drawings (person #5) | Drawings #5 |
| `EQU.DRA.6` | T | Equity: Drawings (person #6) | Drawings #6 |
| `EQU.DRA.7` | T | Equity: Drawings (person #7) | Drawings #7 |
| `EQU.PFT` | P | Equity: Share of profit (partner / beneficiary account) | Share of profits - Clearing account (DR) |
| `EQU.PFT.1` | P | Equity: Share of profit (partner / beneficiary account) (person #1) | Share of profits - partner #1 |
| `EQU.PFT.2` | P | Equity: Share of profit (partner / beneficiary account) (person #2) | Share of profits - partner #2 |
| `EQU.PFT.3` | P | Equity: Share of profit (partner / beneficiary account) (person #3) | Share of profits - partner #3 |
| `EQU.PFT.4` | P | Equity: Share of profit (partner / beneficiary account) (person #4) | Share of profits - partner #4 |
| `EQU.RES` | CT | Equity: Reserves | Capital redemption reserve; Forfeited shares reserve; General reserve ... |
| `EQU.RES.LRR` | C | Equity: Reserves | Livestock revaluation reserve |
| `EQU.RES.REV` | CT | Equity: Reserves | Asset revaluation reserve; Stock revaluation reserve |
| `EQU.RET` | CTPS | Equity: Retained Earnings | Opening balance; Retained Earnings; Retained profits (Accumulated losses) |
| `EQU.RET.CUR` | CTPS | Equity: Current year earnings (folded into Retained Earnings) | Net income for the year; Profit (Loss) earned this year |
| `EQU.RET.DIV.ORD` | C | P&L: Dividend Paid | Fully franked dividends paid; Unfranked dividends paid |
| `EQU.SET` | T | Equity: Settled sum / unit capital | Settlement capital; Unit capital |
| `EQU.SHA` | C | Equity: Share Capital | Issued capital; Share capital; Shareholder's capital |
| `EQU.UDE.2` | T | Equity: Other Equity (person #2) | Opening balance (44050); Opening balance (44060); Opening balance (44070) |
| `EXP.ADM` | CTPS | P&L: Expenses | Accounting fees; Bank charges; Computer expenses ... |
| `EXP.ADV` | CTPS | P&L: Expenses | Advertising |
| `EXP.AMO` | CTPS | P&L: Expenses | Amortisation - Debenture issue expenses; Amortisation - Exploration expenditure; Amortisation - Formation ... |
| `EXP.BAD` | CTPS | P&L: Expenses | Bad debt expenses; Debt collection expenses |
| `EXP.CFL` | T | P&L: losses applied (before distributions) | Carry Forward Losses Applied |
| `EXP.COS` | CTPS | P&L: Cost of Sales | Delivery expenses - Raw material; Delivery expenses - Retail; Discount received - Retail ... |
| `EXP.COS.CLO` | CTPS | P&L: Cost of Sales | Closing inventories; Closing stock adjustment |
| `EXP.COS.OPE` | CTPS | P&L: Cost of Sales | Opening inventories |
| `EXP.DEP` | CTPS | P&L: Expenses | Adjustment for private use on tangible assets; Capital works deductions; Depreciation - Buildings ... |
| `EXP.DIS` | TP | P&L: distributions to beneficiaries / partners (trust and partnership packs) | Distribution to Beneficiaries; Distribution to Beneficiaries 1; Distribution to Beneficiaries 2 ... |
| `EXP.DIV` | S | P&L: Dividend Paid | Fully franked dividends paid; Unfranked dividends paid |
| `EXP.EMP` | CTPS | P&L: Expenses | Consultant services; Contractors and sub-contractors; Holiday pay ... |
| `EXP.EMP.SHA` | CTPS | P&L: Expenses | Directors fees |
| `EXP.EMP.SUP` | CTPS | P&L: Expenses | Superannuation contributions - Associates; Superannuation contributions - Directors; Superannuation contributions - Employees |
| `EXP.EMP.WAG` | CTPS | P&L: Expenses | PAYG withholding; Payroll taxes; Salaries ... |
| `EXP.ENT` | CTPS | P&L: Expenses | Entertainment |
| `EXP.FBT` | CTPS | P&L: Expenses | Fringe benefit tax |
| `EXP.FOR` | CTPS | P&L: Expenses | Consulting fees; Foreign exchange gain/loss |
| `EXP.FOR.RGL` | CTPS | P&L: Expenses | Bank Revaluations; Realised Currency Gains; Realised gains ... |
| `EXP.FOR.UGL` | CTPS | P&L: Expenses | Unrealised Currency Gains; Unrealised gains; Unrealised losses |
| `EXP.INC` | CTPS | P&L: Income Tax Expense | Income tax |
| `EXP.INS` | CTPS | P&L: Expenses | Insurance; Insurance (76460); Insurance - other |
| `EXP.INT` | CTPS | P&L: Expenses | Interest - ATO; Interest on bank loans; Interest on car loan ... |
| `EXP.INV.REN` | CTPS | P&L: Expenses | Advertising - Rental; Bank charges - Rental; Body corporate fees - Rental ... |
| `EXP.NON` | CTPS | P&L: Expenses | Fines and penalties; Non deductable expenses |
| `EXP.OCC` | CTPS | P&L: Expenses | Cleaning - Home office; Council rates - Home office; Electricity - Home office ... |
| `EXP.OPR` | CTPS | P&L: Expenses | Agistments; Borrowing costs; Brokerage ... |
| `EXP.REN` | CTPS | P&L: Expenses | Rental expenses |
| `EXP.REP` | TPS | P&L: Expenses | Repairs and maintenance; Repairs and maintenance (76620) |
| `EXP.TRA.INT` | CTPS | P&L: Expenses | Travel - International |
| `EXP.TRA.NAT` | CTPS | P&L: Expenses | Travel - National |
| `EXP.UDE` | CTPS | P&L: Expenses | Terms charges |
| `EXP.UDE.1` | T | P&L: Expenses; by name: /reconciliation/ -> P&L: distribution section | Reconciliation Adjustments |
| `EXP.UDE.3` | CTPS | P&L: Expenses | Donation; Extraordinary expenses; Finance charges to vendors ... |
| `EXP.VEH` | CTPS | P&L: Expenses | MV - Depreciation; MV - Fuel and motor oil; MV - Interest ... |
| `LIA.CUR` | CTPS | Current Liabilities: Payables - Note Payables > Current | (Xero system accounts only - do not use) |
| `LIA.CUR.HPA` | CTPS | Current Liabilities: Financial Liabilities - Note Financial Liabilities > Current | Hire purchases liability |
| `LIA.CUR.HPA.UEI` | CTPS | Current Liabilities: Financial Liabilities - Note Financial Liabilities > Current | Less unexpired hp liability |
| `LIA.CUR.LOA` | CTPS | Current Liabilities: Financial Liabilities - Note Financial Liabilities > Current; by name: /visa|master ?card|amex|american express|diners|bank ?card|credit card/ -> Current Liabilities: Payables | Bank card; Bills payable; Diners card ... |
| `LIA.CUR.PAY.EMP` | CTPS | Current Liabilities: Personnel-related items - Note Payables > Personnel-related items | Directors fees payable; Employee entitlements; Provision for holiday pay ... |
| `LIA.CUR.PAY.PAY` | CTPS | Current Liabilities: Personnel-related items - Note Payables > Personnel-related items | Instalment taxes payable; PAYG instalment payable; PAYG withholding payable ... |
| `LIA.CUR.PAY.TRA` | CTPS | Current Liabilities: Payables - Note Payables > Current | Accrued charges; Beneficiary loans; Income in advance ... |
| `LIA.CUR.PRO` | CTPS | Current Liabilities: Provisions - Note Provisions | Provision for dividends; Provision for income tax; Provisions |
| `LIA.CUR.TAX` | CTPS | Current Liabilities: Taxation - Note Payables > Tax liabilities; by name: /dividend/ -> Current Liabilities: Provisions | Corporate taxes payable; Dividends for the fiscal year; Luxury car tax payable ... |
| `LIA.CUR.TAX.GST` | CTPS | Current Liabilities: Taxation - Note Payables > Tax liabilities | GST collected; GST payments / refunds |
| `LIA.CUR.TAX.INC` | CTPS | Current Liabilities: Taxation - Note Payables > Tax liabilities | Import duty payable; Income tax payable; Income tax payable - prior year |
| `LIA.CUR.TAX.INC.WTP` | CTPS | Current Liabilities: Taxation - Note Payables > Tax liabilities | No ABN withholding payable; No TFN withholding payable; Supplementary taxes payable ... |
| `LIA.NCL` | CTPS | Non-Current Liabilities: Other Non-Current Liabilities - Note Other Liabilities > Non Current | Proceeds from borrowings (Clearing); Repayment of borrowings (Clearing) |
| `LIA.NCL.CHM` | CTPS | Non-Current Liabilities: Financial Liabilities - Note Financial Liabilities > Non Current > Secured | Mortgage |
| `LIA.NCL.HPA` | CTPS | Non-Current Liabilities: Financial Liabilities - Note Financial Liabilities > Non Current > Secured | Hire purchase liability |
| `LIA.NCL.HPA.UEI` | CTPS | Non-Current Liabilities: Financial Liabilities - Note Financial Liabilities > Non Current > Secured | Less unexpired hire purchase liability |
| `LIA.NCL.LOA` | CTPS | Non-Current Liabilities: Financial Liabilities - Note Financial Liabilities > Non Current | Debentures; Interest Bearing Loans - Bank; Lease liability |
| `LIA.NCL.LOA.DIR` | CTPS | Non-Current Liabilities: Financial Liabilities - Note Financial Liabilities > Non Current > Unsecured | Shareholders loans |
| `LIA.NCL.LOA.SEC` | CTPS | Non-Current Liabilities: Financial Liabilities - Note Financial Liabilities > Non Current > Secured | Interest Bearing Loans - Secured |
| `LIA.NCL.LOA.SET` | CTPS | Non-Current Liabilities: Financial Liabilities - Note Financial Liabilities > Non Current > Unsecured | Loans - Beneficiary |
| `LIA.NCL.LOA.UNS` | CTPS | Non-Current Liabilities: Financial Liabilities - Note Financial Liabilities > Non Current > Unsecured | Interest Bearing Loans - Unsecured |
| `LIA.NCL.PFT.1` | T | Non-Current Liabilities: Financial Liabilities - Note Financial Liabilities > Non Current > Unsecured | Beneficiary/Shareholder profit distribution 1 |
| `LIA.NCL.PFT.2` | T | Non-Current Liabilities: Financial Liabilities - Note Financial Liabilities > Non Current > Unsecured | Beneficiary/Shareholder profit distribution 2 |
| `LIA.NCL.PFT.3` | T | Non-Current Liabilities: Financial Liabilities - Note Financial Liabilities > Non Current > Unsecured | Beneficiary/Shareholder profit distribution 3 |
| `LIA.NCL.PFT.4` | T | Non-Current Liabilities: Financial Liabilities - Note Financial Liabilities > Non Current > Unsecured | Beneficiary/Shareholder profit distribution 4 |
| `LIA.NCL.REL` | CTPS | Non-Current Liabilities: Financial Liabilities - Note Financial Liabilities > Non Current > Unsecured | Interest Bearing Loans - related parties; Interest Bearing Loans - Related parties |
| `LIA.NCL.UDL.1` | CTPS | Non-Current Liabilities: Provisions - Note Provisions | Aggregate employee entitlement liability; Provision for deferred exchange gain; Provision for long term service leave ... |
| `REV` | CTP | P&L: Income | Sale of retail; sale of retail; Sales of retail |
| `REV.GRA.GOV` | CTPS | P&L: Other Income | Government subsidies |
| `REV.INV.DIV` | CTPS | P&L: Other Income | Dividends received |
| `REV.INV.INT` | CTPS | P&L: Other Income | Interest received; Interest received- ATO |
| `REV.INV.REN` | CTPS | P&L: Other Income | Other rental income; Rental income; Rental income - Property name |
| `REV.OTH` | CTPS | P&L: Other Income | Accounting fees collected; Administration fees; Application rounding ... |
| `REV.OTH.CAP` | CTPS | P&L: Other Income | Net gain/loss on disposal of plant and equipment |
| `REV.OTH.INV` | CTPS | P&L: Other Income | Change in net market value of investments; Net gain or (loss) on sale of investments |
| `REV.TRA.GOO` | CTPS | P&L: Income | sale adjustments, retail; Sales adjustments, retail; Sales of retail |
| `REV.TRA.SER` | CTPS | P&L: Income | sale - Job name; Sales - Job name |

Codes a chart does not carry still work: a code not listed prints by its
longest known prefix (e.g. `LIA.NCL.ABC` → Other Non-Current Liabilities).
A code with no known prefix is an ERROR in validation.

## 4. Changing a report code on request

| Scope | How | Where it is kept |
|---|---|---|
| This client, this year | `make_report.py … --set-code "<wp code, name or 'code - name'>=<REPORT.CODE>"` | `pack.json › overrides.report_codes` |
| This client, a different chart account | `--set-account "<key>=<chart code>"` | `overrides.accounts` |
| This client, a different printed name | `--rename "<key>=<label>"` | `overrides.labels` |
| Keep the client's choices next year | `--overrides <last year's pack.json>` (or an overrides JSON) | carried forward |
| Every client of this entity type | `python3 scripts/chart.py set-code --type trust --code 31600 --report-code LIA.CUR.PAY.TRA` | the chart CSV |
| A new chart account | `python3 scripts/chart.py add --type company --code 41500 --name "Asset revaluation reserve" --account-type Equity --report-code EQU.RES.REV` | the chart CSV |
| The printed name of a chart account | `python3 scripts/chart.py rename --type company --code 21700 --reporting-name "Plant and Equipment"` | chart "Reporting Name" |
| Where a code prints (all clients) | edit `assets/report-map.json › xero_code_map` | the map |

`--set-code` also moves the ledger posting to a chart account that carries the
new code, so `ledger.csv` stays consistent. Before a firm-wide change, check
which lines move: `python3 scripts/chart.py show --type company --report-code LIA.CUR.PAY.PAY`.
After any firm-wide change, re-run the regression in `references/worked-examples.md`.

## 5. Useful commands

```bash
python3 scripts/chart.py show  --type trust --report-code EQU.DRA          # accounts carrying a code
python3 scripts/chart.py codes --type partnership                          # every code in a chart
python3 scripts/chart.py find  --type company --section "Current Liabilities" "Loan - G Stalley"
```
