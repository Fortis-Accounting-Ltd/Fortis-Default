# Account mapping reference - layout codes

Two kinds of code are in play (see `references/charts-and-report-codes.md`):

* the **Xero report code** on the firm's chart account (`LIA.CUR.PAY.EMP`,
  `ASS.NCA.FIX.VEH.ACC`, `EQU.DRA.2` …), which is what a preparer changes; and
* the **layout code** it resolves to through `report-map.json › xero_code_map`.
  This file is about the layout code. It decides where an account prints in
  each report of the pack.

Layout codes are valid report codes too. `--set-code "Loan - Director=LIA.NCA.SHA"`
uses one directly. The machine copy is `assets/report-map.json` (`pnl_sections`,
`bs_lines`, `notes`, labels and order). The trust, partnership and sole-trader
P&L sections are in `entity_layouts`.

## 1. Report code hierarchy

Segment 1 = statement side, 2 = current/non-current, 3 = Balance Sheet line /
P&L section, 4 = class or sub-heading inside the Note.

### Profit and Loss Statement

| Report code | P&L section printed | Total line | Typical accounts |
|---|---|---|---|
| `REV.TRA` | **Income** | Total Income | Sales, Sale of goods, Commissions collected, Fees, "Test" (Complex), BAC Wage Subsidies (Frontier) |
| `EXP.COS` | **Cost of Sales** (or "Cost of Goods Sold" via `options.cost_of_sales_label`) | Total Cost of Sales | Opening stock, Purchases, Closing stock (credit), Consultant, Therapy Resource, Assessment Program |
| *(computed)* | **Gross Profit from Trading** | | Income less Cost of Sales - always printed |
| `REV.OTH` | **Other Income** | Total Other Income | Interest received, FBT employee contribution, Consulting Fees Collected (Wisdom), Overrides, Other income & refunds, dividends/distributions received, gains |
| *(computed)* | **Total Income** | | Gross Profit + Other Income |
| `EXP.OPE` | **Expenses** | Total Expenses | every operating expense incl. Depreciation, Amortisation, Instant Write-Off Assets, Interest Expense, PSI Attribution, Realised/Unrealised Currency Gains, Bad Debts, Donation, Fines |
| *(computed)* | **Profit/(Loss) before Taxation** | | |
| `EXP.TAX` | **Income Tax Expense** (hidden when nil) | Total Income Tax Expense | Income Tax Expense, Income tax |
| *(computed)* | **Net Profit After Tax** | | |
| `EQU.DIV` | **Dividend Paid** (hidden when nil) | Total Dividend Paid | Fully Franked Dividends Paid, Unfranked Dividends Paid |
| *(computed)* | **Net Profit After Tax & Dividend** | | = "Profit for the Period" in Movements in Equity |
| `EXP.DIS` | **Distribution of Trust Income** › "Trust Income Distributed" (trust) / **Net Income Distributed** › "Distribution of Net Income" (partnership) | Total … | Distribution to Beneficiaries / Partner n; per-person distribution accounts are summed into the one line |
| `EXP.CFL` | trust distribution section (losses applied before distribution) | | Carry Forward Losses Applied |
| `EXP.DIS.ADJ` | distribution section, "Reconciliation Adjustment" line | | Reconciliation Adjustments (Happy Inn) |
| *(computed)* | **Profit/(Loss) Before Distribution**, **Undistributed Income** (trust, partnership); **Net Profit/(Loss)** (sole trader) | | see `references/entity-types.md` |

### Balance Sheet + Notes

| Report code | BS section | BS line label | Note | Sub-heading inside the Note |
|---|---|---|---|---|
| `ASS.CUR.CAS.BAN` | Current Assets | Cash and Cash Equivalents | Cash and Cash Equivalents | Bank Accounts |
| `ASS.CUR.CAS.OTH` | Current Assets | Cash and Cash Equivalents | Cash and Cash Equivalents | Other Cash Items (cash in hand, petty cash; Capstone put its CBA account here) |
| `ASS.CUR.CAS.TRM` | Current Assets | Cash and Cash Equivalents | Cash and Cash Equivalents | Term Deposits |
| `ASS.CUR.REC` | Current Assets | Receivables | Receivables | Current (heading suppressed with `show_current_heading:false` - Capstone) |
| `ASS.NCA.REC` | Non-Current Assets | Receivables | Receivables | Non Current |
| `ASS.CUR.INV.INV` | Current Assets | Inventory | Inventory | Inventories |
| `ASS.CUR.INV.WIP` | Current Assets | Inventory | Inventory | Work in Progress |
| `ASS.CUR.PRE` | Current Assets | Prepayments | Prepayments | *(flat)* |
| `ASS.CUR.FIN` | Current Assets | Financial Assets | Financial Assets | Current (Frontier's bank-guarantee term deposit) |
| `ASS.NCA.FIN` | Non-Current Assets | Financial Assets | Financial Assets | Non Current |
| `ASS.CUR.OTH` / `ASS.NCA.OTH` | Current / Non-Current | Other Current / Non-Current Assets | Other Assets | Current / Non Current |
| `ASS.NCA.PPE.LAN` | Non-Current Assets | Property, Plant and Equipment | Property Plant and Equipment | Land and Buildings |
| `ASS.NCA.PPE.LEA` | " | " | " | Leasehold Improvements |
| `ASS.NCA.PPE.PLA` | " | " | " | Plant and Equipment |
| `ASS.NCA.PPE.MOT` | " | " | " | Motor Vehicles |
| `ASS.NCA.PPE.FUR` | " | " | " | Furniture & Fixtures |
| `ASS.NCA.PPE.OFF` | " | " | " | Office Equipment |
| `ASS.NCA.PPE.COM` | " | " | " | Computer Equipment |
| `ASS.NCA.PPE.LVP` | " | " | " | Low Value Pool |
| `ASS.NCA.INT.GOO` | Non-Current Assets | Intangibles | Intangibles | Goodwill |
| `ASS.NCA.INT.OTH` | Non-Current Assets | Intangibles | Intangibles | Other Intangible Assets (formation costs + amortisation, trademarks, Padel's bank guarantees) |
| `ASS.NCA.INV` | Non-Current Assets | Investments | Investments | *(flat)* |
| `LIA.CUR.PAY` | Current Liabilities | Payables | Payables | Current (trade creditors, credit cards, accruals, client control account, ATO ICA when a liability) |
| `LIA.CUR.PAY.FIN` | Current Liabilities | **Financial Liabilities** | Payables | Financial Liabilities (Wisdom IT: director loan inside the Payables note) |
| `LIA.CUR.EMP` | Current Liabilities | Personnel-related items | Payables | Personnel-related items (super payable, wages payable) |
| `LIA.CUR.TAX` | Current Liabilities | Taxation | Payables | Tax liabilities (GST, PAYG W, PAYG I, Income tax payable) |
| `LIA.CUR.PRO` | Current Liabilities | Provisions | Provisions | *(flat)* |
| `LIA.CUR.FIN` | Current Liabilities | Financial Liabilities | Financial Liabilities | Current (Frontier: Loans - Related party) |
| `LIA.NCA.FIN.UNS` | Non-Current Liabilities | Financial Liabilities | Financial Liabilities | Non Current › Unsecured (director / family-trust loans - Padel, Complex) |
| `LIA.NCA.FIN.SEC` | Non-Current Liabilities | Financial Liabilities | Financial Liabilities | Non Current › Secured (bank loans, HP, chattel mortgage) |
| `LIA.NCA.SHA` | Non-Current Liabilities | Financial Liabilities | **Loan from Shareholders/Associates** | *(flat)* (Capstone) |
| `LIA.NCA.PRO` | Non-Current Liabilities | Provisions | Provisions | *(flat)* (long service leave) |
| `LIA.NCA.PAY` | Non-Current Liabilities | Payables | Payables | Non Current |
| `EQU.SHA` | Equity | Share Capital | - | - |
| `EQU.RES` | Equity | Reserves | - | - |
| `EQU.RET` | Equity | Retained Earnings (combined with CYE and DIV) | - | - |
| `EQU.CYE` | Equity | *(folded into Retained Earnings)* | - | must equal NPAT |
| `EQU.DIV` | Equity | *(folded into Retained Earnings; printed in the P&L)* | - | - |
| `EQU.DRA` / `EQU.CAP` / `EQU.OTH` | Equity | Drawings / Capital Accounts / Other Equity | - | non-company entities; a `.n` suffix is the partner / beneficiary number |
| `EQU.PFT` | Equity | Share of profits (per person) | - | Partner / Beneficiary Accounts statement |
| `EQU.SET` | Equity | Settlement capital / Unit capital (printed by account name) | - | trusts |
| `LIA.NCA.BEN` | Non-Current Liabilities | Beneficiary Entitlements | Beneficiary Entitlements | optional: unpaid entitlements on their own line (the packs use `LIA.NCA.FIN.UNS`) |
| `EQU.RES` (two or more accounts) | Equity | Reserves | **Reserves** | flat list (trust packs) |

## 2. Section defaults

When neither the chart nor a keyword rule places a line, the section header it
sits under decides, and the line is flagged **REVIEW**:

| Workpaper section | Default Xero code | Prints as |
|---|---|---|
| Trading Income / Income / Sales | `REV.TRA` | Income |
| Other Income | `REV.OTH` | Other Income |
| Cost of Sales / Direct Costs | `EXP.COS` | Cost of Sales |
| Operating Expenses / Expenses | `EXP.OPR` | Expenses |
| Bank | `ASS.CUR.CAS.BAN` | Cash › Bank Accounts |
| Current Assets | `ASS.CUR.REC` | Receivables › Current |
| Fixed Assets | `ASS.NCA.FIX.PLA` | PPE › Plant and Equipment |
| Non-current Assets | `ASS.NCA.REC` | Receivables › Non Current |
| Current Liabilities | `LIA.CUR.PAY.TRA` | Payables › Current |
| Non-current Liabilities | `LIA.NCL.LOA.UNS` | Financial Liabilities › Non Current › Unsecured |
| Equity | `EQU` | Other Equity |

A P&L line with no section is income when its name says received / income /
revenue / earned / sales, otherwise an expense.

## 3. Where does this account go? (decision rules)

| Account (as it appears in the workpaper) | Code | Why / when to choose differently |
|---|---|---|
| GST (credit) | `LIA.CUR.TAX` | Tax liabilities inside the Payables note. |
| GST (debit / refund due) | `LIA.CUR.TAX` | Xero prints it as a negative liability (Wisdom, Complex, Frontier). Capstone reclassified "GST payments/refunds" to `ASS.CUR.REC` - only do that if the preparer asks. Sole traders: Receivables automatically (True Talkers). |
| PAYG Withholding Payable, PAYG Instalment Payable, Income Tax Payable, Provision for Income Tax (credit) | `LIA.CUR.TAX` | |
| Provision for income tax in **debit** (refund) | `LIA.CUR.TAX` (negative) or `ASS.CUR.REC` | Capstone shows it in Receivables. Follow the prior-year pack for the client. |
| ATO Integrated Client Account / running balance account, SGC account | `LIA.CUR.TAX` (firm chart "Tax liabilities") | The FY2026 Xero packs (IHDC) print them under Taxation. The five older fixtures pin `LIA.CUR.PAY` (Padel, Frontier) or `ASS.CUR.REC` (Capstone) - follow the client's prior pack. |
| Superannuation Payable, Wages Payable, payroll clearing | `LIA.CUR.EMP` | Personnel-related items. |
| Provision for holiday pay / annual leave / bonuses / rent / make-good | `LIA.CUR.PRO` | Long service leave → `LIA.NCA.PRO`. |
| Credit cards (AMEX, Mastercard) in credit | `LIA.CUR.PAY` | Frontier. If the card is in debit and set up as a Bank account, `ASS.CUR.CAS.BAN` (Wisdom's Mastercard***0401). |
| Client control / trust monies liability | `LIA.CUR.PAY` | Frontier. |
| Revenue received in advance / unearned income | `LIA.CUR.PAY` | |
| Director / shareholder / family-trust loan, credit, no fixed repayment | `LIA.NCA.FIN.UNS` | Padel, Complex. Use `LIA.NCA.SHA` if the client's prior pack used the "Loan from Shareholders/Associates" note (Capstone). Use `LIA.CUR.FIN` if repayable within 12 months (Frontier's "Loans - Related party"). `LIA.CUR.PAY.FIN` reproduces Wisdom's presentation inside the Payables note. |
| Director loan in **debit** (company owed money) | `ASS.CUR.REC` | Padel "Loan to Directors" - flag Division 7A to the reviewer. |
| Loan to a related company | `ASS.CUR.REC` | Padel "Loan - Padel Point Rosehill Pty Ltd". Non-current if not repayable within 12 months → `ASS.NCA.REC`. |
| Bank loan, HP, chattel mortgage, equipment finance | `LIA.NCA.FIN.SEC` (current portion `LIA.CUR.FIN`) | |
| Bank guarantee / term deposit held as security | `ASS.CUR.FIN` | Frontier. Padel coded its bank guarantees to Intangibles (`ASS.NCA.INT.OTH`) - keep whatever the prior year did. |
| Deposits / bonds paid | `ASS.CUR.REC` | |
| Trade debtors / Accounts Receivable | `ASS.CUR.REC` | |
| Inventory / stock on hand | `ASS.CUR.INV.INV` | Also triggers the Inventories accounting policy. |
| "... at Cost" and "Accumulated Depreciation ..." pairs | same `ASS.NCA.PPE.<class>` | Both lines in the same class so the class total is the written-down value. Class from the name: leasehold/fit-out → LEA, motor/vehicle → MOT, furniture/fixtures → FUR, office equipment → OFF, computer → COM, else PLA. |
| Formation costs (+ accumulated amortisation) | `ASS.NCA.INT.OTH` | Prints even when fully amortised. |
| Trademark, brand deposit, website cost | `ASS.NCA.INT.OTH` | |
| Goodwill | `ASS.NCA.INT.GOO` | |
| Cash in hand, petty cash | `ASS.CUR.CAS.OTH` | |
| Depreciation, Amortisation, Instant Write-Off Assets | `EXP.OPE` | They are ordinary expenses in the pack; the depreciation schedule tie-out finds them by name. |
| Realised / Unrealised Currency Gains | `EXP.OPE` | Xero default codes 498/499 are expense-type; a gain prints in brackets. Padel. |
| Interest received / income | `REV.OTH` | |
| FBT employee contribution | `REV.OTH` | |
| Consulting fees **collected** | `REV.OTH` if the client's prior pack did so (Wisdom); otherwise `REV.TRA` | |
| Commissions collected / Overrides | `REV.TRA` / `REV.OTH` respectively | Frontier convention. |
| Government subsidies / grants | `REV.OTH` (Frontier coded BAC Wage Subsidies to `REV.TRA` - follow prior year) | |
| Income Tax Expense | `EXP.TAX` | |
| Dividends paid (franked / unfranked) | `EQU.DIV` | Enter as a positive "paid" amount. |
| Current Year Earnings | `EQU.CYE` | Only if the BS export shows it; validated against NPAT. |
| Retained Earnings | `EQU.RET` | Prior-years balance (Xero system account). |
| Capital loss / capital profits reserve | `EQU.RES` | Padel's PY movement was posted to RE; supply it in `equity_movements`. |
| Share Capital | `EQU.SHA` | |
| Suspense, Clearing, Historical Adjustment, Rounding, Tracking Transfers | `LIA.CUR.PAY` | Should be nil at year end - raise a query if not. |

## 4. Keyword rules (summary)

`chart.py › RULES` is scanned top to bottom for the line's section. The first
pattern that matches the (normalised) name wins and gives a Xero report-code
family; the most similar chart account in that family is the posting. The
important ones, in priority order:

* **Assets:** cash on hand → term deposits → card and bank names → doubtful
  debts → prepayments → WIP / inventory → GST / ATO / withholding refunds →
  debtors → loans receivable → deposits and bonds → owner accounts → goodwill →
  leasehold, land, buildings, vehicles, pools, plant (PPE classes) → intangibles
  → investments → suspense.
* **Liabilities:** unexpired HP interest → chattel mortgage → hire purchase and
  vehicle finance → GST → PAYG / income tax / ATO / SGC → parental leave and
  super → owner accounts (REVIEW) → wages → leave and entitlements → dividends
  and provisions → beneficiary entitlements and loans → director / shareholder /
  partner loans → (trusts) loans from persons → related-party loans →
  mortgages, cards, bank loans → other loans → payables and accruals →
  suspense.
* **Equity:** dividends → current year earnings → retained earnings → share
  capital → reserves → settled sum / units → drawings → share of profits →
  capital / funds introduced / opening balances.
* **P&L:** interest, dividends and distributions received, rent, grants, gains
  on disposal, currency gains, FBT contributions, other income; trading income
  (under a trading header only); opening / closing stock, cost of sales; income
  tax, dividends paid, distributions, losses applied, losses on disposal,
  currency losses, amortisation, depreciation, interest, super, wages, motor
  vehicle, travel, insurance, rent, repairs, administration, advertising and
  other expense families.

Rules never move a line across statements. A P&L line can only become `REV.*`,
`EXP.*` or a dividend / distribution code; a Balance Sheet line only `ASS.*`,
`LIA.*` or `EQU.*`. Current and non-current follow the workpaper section. The
entity-specific rules are listed in `references/charts-and-report-codes.md § 2`.

## 5. Other entity types

Trusts, partnerships and sole traders are fully supported. Their P&L
sections, equity lines, partner and beneficiary statements and wording are in
`references/entity-types.md`. SMSFs are out of scope (`smsf-audit-pack`).
