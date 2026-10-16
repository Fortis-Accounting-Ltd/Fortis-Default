# Worked examples - the five reference packs

Each example is transcribed as a ledger fixture in `assets/examples/`. Render one
with `python scripts/render_pack.py assets/examples/<file>.json out.pdf` and
compare with the source PDF. Figures below are the regression targets (whole
dollars as printed; totals can differ by $1-3 from re-keyed rounded lines).

| Fixture | Entity | FY | Pages (source) | What it exercises |
|---|---|---|---|---|
| `wisdom-it-2026.json` | Wisdom IT Pty Ltd | 2026 | 10 | Dividends (franked + unfranked), director loan inside Payables note, nil Intangibles note, simple depreciation layout, single director, source ordering |
| `capstone-brokers-2025.json` | Capstone Brokers Pty Ltd | 2025 | 9 | Opening/closing stock, "Cost of Goods Sold", receivables without Current heading, Loan from Shareholders/Associates note, negative equity, no depreciation schedule |
| `complex-communicators-2026.json` | Complex Communicators Pty Ltd | 2026 | 10 | Cost of Sales, income tax expense, PSI company, PY share issue movement, nil PPE note, single director wording |
| `padel-point-2026.json` | Padel Point Pty Ltd | 2026 | 14 | Five PPE classes, inventory, intangibles, FX footnote, three unsecured family-trust loans, PY capital loss reserve movement, standard depreciation layout (landscape) |
| `frontier-travel-2026.json` | Frontier Travel Pty Ltd | 2026 | 13 | Trust monies, provisions, personnel-related items, current related-party loan note, tax + franked dividends, reserves, direct RE adjustment, P&L → Equity → BS order, asset-register depreciation layout |

## Wisdom IT Pty Ltd - FY2026

| Statement | Key figures (2026 / 2025) |
|---|---|
| Total Income (after other income) | - / 206,244 |
| Total Expenses | 16,699 / 250,507 |
| Net Profit After Tax | (16,699) / (44,263) |
| Total Dividend Paid | 11,478 / 12,500 |
| Net Profit After Tax & Dividend | (28,177) / (56,763) |
| Total Assets / Liabilities | 8,293 / 55,296 · 8,143 / 26,969 |
| Net Assets = Total Equity | 150 / 28,327 |
| Movements in Equity opening | 28,327 / 85,089 |
| Notes | 2 Cash · 3 PPE · 4 Intangibles (nil) · 5 Payables |

Mapping highlights

| Account | Code | Prints as |
|---|---|---|
| Consulting Fees Collected, FBT Employee Contributions | `REV.OTH` | Other Income |
| Business Transaction Account***4799, Mastercard***0401 (debit/credit) | `ASS.CUR.CAS.BAN` | Cash › Bank Accounts |
| Plant and Equipment at Cost / Accumulated Depreciation | `ASS.NCA.PPE.PLA` | PPE › Plant and Equipment |
| Motor Vehicles at Cost / Accumulated Depreciation (disposed) | `ASS.NCA.PPE.MOT` | PPE › Motor Vehicles (nil CY) |
| Formation Costs / Less Accumulated Amortisation | `ASS.NCA.INT.OTH` | Intangibles › Other Intangible Assets (nil total, note still prints; BS line suppressed) |
| Loan - Frank Grippi | `LIA.CUR.PAY.FIN` | BS "Financial Liabilities 5"; Payables note › Financial Liabilities |
| GST (1,547), PAYG Withholdings Payable | `LIA.CUR.TAX` | BS "Taxation 5"; Payables note › Tax liabilities |
| Fully Franked / Unfranked Dividends Paid | `EQU.DIV` | P&L Dividend Paid; netted into Retained Earnings (-) |

Retained Earnings printed nil = RET 28,177 + CYE (16,699) − DIV 11,478.

## Capstone Brokers Pty Ltd - FY2025

| Statement | Key figures (2025 / 2024) |
|---|---|
| Total Income (trading) | 901,355 / 726,494 |
| Total Cost of Goods Sold | 910,412 / 724,058 |
| Gross Profit from Trading | (9,058) / 2,437 |
| Net Profit After Tax | (33,870) / (24,792) |
| Total Assets / Liabilities | 270,244 / 158,703 · 337,092 / 191,681 |
| Net Assets = Total Equity | (66,848) / (32,978) |
| Movements in Equity opening | (32,978) / (8,186) |
| Notes | 2 Cash · 3 Receivables · 4 Inventory · 5 PPE (nil) · 6 Intangibles (nil) · 7 Payables · 8 Loan from Shareholders/Associates |

Mapping highlights

| Account | Code | Prints as |
|---|---|---|
| Opening stock, Purchases, Closing stock (credit) | `EXP.COS` | Cost of Goods Sold (`options.cost_of_sales_label`) |
| CBA bank account***2575 | `ASS.CUR.CAS.OTH` | Cash › Other Cash Items (as the source pack did) |
| ATO Integrated Client Account, Deposit paid, GST payments/refunds, Provision for income tax | `ASS.CUR.REC` | Receivables - flat list (`show_current_heading: false`) |
| PAYG instalment payable | `LIA.CUR.TAX` | BS "Taxation 7"; Payables › Tax liabilities |
| Loan from director | `LIA.NCA.SHA` | BS "Financial Liabilities 8" (non-current); Note 8 "Loan from Shareholders/Associates" |

## Complex Communicators Pty Ltd - FY2026

| Statement | Key figures (2026 / 2025) |
|---|---|
| Total Income (trading) | 260,069 / 117,661 |
| Total Cost of Sales | 34,901 / 21,748 |
| Total Expenses | 225,402 / 95,914 |
| Profit/(Loss) before Taxation | - / - |
| Income Tax Expense | 109 / 1,986 |
| Net Profit After Tax | (109) / (1,986) |
| Net Assets = Total Equity | (1,995) / (1,886) |
| Movements in Equity | opening (1,886) / -; Share Capital PY 100 |
| Notes (source) | 2 Cash · 3 Receivables · 4 Payables · 5 Financial Liabilities · 6 PPE (nil) |
| Notes (engine) | 2 Cash · 3 Receivables · 4 PPE (nil) · 5 Payables · 6 Financial Liabilities - canonical order |

Mapping highlights

| Account | Code | Prints as |
|---|---|---|
| Sales, Test | `REV.TRA` | Income |
| Assessment Program, Consultant, Therapy Resource | `EXP.COS` | Cost of Sales |
| PSI Attribution, Instant Write-Off Assets | `EXP.OPE` | Expenses |
| Income Tax Expense | `EXP.TAX` | Income Tax Expense section |
| Business Account, Westpac AU …#001, Westpac Business Cash Reserve | `ASS.CUR.CAS.BAN` | Cash › Bank Accounts |
| Cash in hand | `ASS.CUR.CAS.OTH` | Cash › Other Cash Items |
| Superannuation Payable | `LIA.CUR.EMP` | BS "Personnel-related items 4" |
| Income Tax Payable (1,955), PAYG Installment Payable, GST (1,551), PAYG Withholdings Payable | `LIA.CUR.TAX` | BS "Taxation 4" |
| Loan - Abi Williams | `LIA.NCA.FIN.UNS` | BS "Financial Liabilities 5" (non-current); Note › Non Current › Unsecured |

## Padel Point Pty Ltd - FY2026

| Statement | Key figures (2026 / 2025) |
|---|---|
| Total Income (trading) | 621,805 / - |
| Total Cost of Goods Sold | 95,187 / - |
| Total Other Income | 8,735 / - |
| Total Expenses | 733,594 / 34,758 |
| Net Profit After Tax | (198,241) / (34,758) |
| Total Assets / Liabilities | 2,163,812 / 111,983 · 2,443,392 / 193,323 |
| Net Assets = Total Equity | (279,580) / (81,339) |
| Movements in Equity | opening (81,339) / (4,937); PY Share Capital 100; PY Capital Loss Reserve (41,745) |
| Notes | 2 Cash · 3 Receivables · 4 Inventory · 5 PPE · 6 Intangibles · 7 Payables · 8 Financial Liabilities |
| Depreciation schedule | cost 1,153,131 · opening 52,945 · purchases 1,094,183 · depreciation 123,965 · closing 1,023,163 (= PPE note) |

Mapping highlights

| Account | Code | Prints as |
|---|---|---|
| Realised / Unrealised Currency Gains | `EXP.OPE` | Expenses (positive = loss) |
| Loan - Padel Point Rosehill Pty Ltd, Other, Loan to Directors | `ASS.CUR.REC` | Receivables › Current |
| Furniture & Fixture at Cost / Accum. | `ASS.NCA.PPE.FUR` | PPE › Furniture & Fixtures |
| Leasehold Improvements / Accum. | `ASS.NCA.PPE.LEA` | PPE › Leasehold Improvements |
| Office Equipment at Cost / Accum. | `ASS.NCA.PPE.OFF` | PPE › Office Equipment |
| Brand Deposit (Trademark), Bank Guarantees | `ASS.NCA.INT.OTH` | Intangibles › Other Intangible Assets |
| Accounts Payable, ATO Integrated Client Account (364) | `LIA.CUR.PAY` | BS "Payables 7"; Payables › Current |
| Director Loan - F & F Nami Family Trust / E & L Nicholas Family Trust / Hoss Family Trust | `LIA.NCA.FIN.UNS` | Financial Liabilities › Non Current › Unsecured |
| `options.fx_rates` | - | Exchange-rate footnote under the Balance Sheet and after the Notes |

## Frontier Travel Pty Ltd - FY2026

| Statement | Key figures (2026 / 2025) |
|---|---|
| Total Income (trading) | 1,568,893 / 1,567,150 |
| Total Other Income | 812,362 / 721,887 |
| Total Expenses | 1,712,803 / 1,923,102 |
| Profit/(Loss) before Taxation | 668,452 / 365,935 |
| Income Tax Expense | 176,513 / 86,593 |
| Net Profit After Tax | 491,939 / 279,342 |
| Total Dividend Paid | 135,000 / 95,000 |
| Net Profit After Tax & Dividend | 356,939 / 184,342 |
| Total Assets / Liabilities | 4,696,388 / 4,005,735 · 3,322,454 / 2,863,740 |
| Net Assets = Total Equity | 1,373,934 / 1,141,995 |
| Movements in Equity | opening 1,141,995 / 962,653; Retained Earnings (125,000) CY; Other Increases (5,000) PY |
| Notes | 2 Cash · 3 Receivables · 4 PPE · 5 Financial Assets · 6 Provisions · 7 Payables · 8 Financial Liabilities |
| Depreciation register | cost 455,375 · opening accum 223,137 · opening 101,572 · purchases 130,667 · depreciation 94,419 · closing accum 317,555 · closing 137,820 (= PPE note) |

Mapping highlights

| Account | Code | Prints as |
|---|---|---|
| BAC Wage Subsidies, Commissions collected | `REV.TRA` | Income |
| Overrides, Other income & refunds, Interest received, FBT employee contribution | `REV.OTH` | Other Income |
| Holiday pay 15,673 / (51,548), Parental Leave Payment, Capital Loss carry forward | `EXP.OPE` | Expenses (credits in brackets) |
| Discounts given (nil both years, `keep: true`) | `EXP.OPE` | printed as "- / -" |
| Trust account - Booking report, ANZ Account, General accounts, Virtuoso Credits, Trust Account Loan | `ASS.CUR.CAS.BAN` | Cash › Bank Accounts |
| Short term deposits - Mount St Bank Guarantee | `ASS.CUR.FIN` | BS "Financial Assets 5" (current) |
| Leasehold Improvements at Cost / Accumulated Amortisation | `ASS.NCA.PPE.LEA` | PPE › Leasehold Improvements (source: "Land and Buildings at Fair Value › Leasehold Improvements") |
| Provision for holiday pay / Employee Bonuses / Rent Expense | `LIA.CUR.PRO` | BS "Provisions 6" |
| Client control account, Revenue Owned - Luxury Brand, Bankwest World Mastercard, AMEX platinum credit card, Trade creditors, ATO Integrated Client account | `LIA.CUR.PAY` | BS "Payables 7"; Payables › Current |
| Superannuation payable | `LIA.CUR.EMP` | BS "Personnel-related items 7" |
| GST payments / refunds (15,021), Income tax payable, PAYG instalment payable, PAYG withholding payable | `LIA.CUR.TAX` | BS "Taxation 7" |
| Loans - Related party 26,350 / (105,548) | `LIA.CUR.FIN` | BS "Financial Liabilities 8" (current); Note 8 › Current |
| Reserves (38,000) | `EQU.RES` | Equity line "Reserves" |
| Retained Earnings 1,004,995 (after a 125,000 direct debit) | `EQU.RET` | printed 1,361,934 = RET + NPAT − dividends; the 125,000 appears as a "Retained Earnings" movement |

## Regression run

```bash
cd .claude/skills/financial-report-builder
for f in assets/examples/*.json; do python3 scripts/render_pack.py "$f" "/tmp/$(basename "$f" .json).pdf"; done
```

Expected: no ERROR rows; Frontier shows the WARNING for the (125,000) retained
earnings movement; page counts Wisdom 11 / Capstone 9 / Complex 10 /
Frontier 14 / Padel 13 (the generator's tables run slightly longer than Xero's).

The fixtures pin the presentation their source packs used, so that later
changes to the entity defaults cannot move them:

| Fixture | Pinned options |
|---|---|
| all five | `ppe_note_lines: "accounts"` (PPE note lines by account name, as these packs print them) |
| Capstone | `cost_of_sales_label: "Cost of Goods Sold"`, `show_current_heading: false` |
| Complex | `cost_of_sales_label: "Cost of Sales"`, `include_inventory_policy: true` |
| Frontier | `report_order` (Equity before BS), `notes_order` (Provisions before Payables), register depreciation |
| Padel | `cost_of_sales_label: "Cost of Goods Sold"`, `fx_rates`, standard depreciation |
| Wisdom | `sort: "source"`, `include_inventory_policy: true`, simple depreciation |

Changes accepted when the entity-type work landed (all match the Xero packs):

* Capstone COGS prints Opening stock → Purchases → Closing stock.
* Frontier current liabilities print in the Xero-observed line order.
* The Contents page carries the footer and page numbers.
* The P&L no longer carries the statements footnote.
* Single-director compilation reports read "the director who is responsible".

To compare engine output between two versions of the skill, dump every printed
figure with a small script over `fr_engine.Pack` (P&L sections, BS lines, notes,
equity, validation) and diff the dumps. The PDFs themselves differ in metadata.

## Real workpapers (FYI, FY2025-26)

`make_report.py` was run on these FYI workpapers. Use them as end-to-end
regression targets: same lines posted, no REVIEW, same totals.

| Client (type) | Workpaper source | Lines posted | Result | Notes |
|---|---|---|---|---|
| Isaac Houghton Design & Construct Pty Ltd (company) | 2026 Workpapers, P&L / BS tabs | 95, 0 REVIEW | NPAT 139,661 (PY 705); Net Assets 298,030 (PY 163,762); 12 pages | Div 7A loans → non-current Receivables (INFO); ATO RBA and SGC → Taxation; paid parental leave → Personnel-related items; PAYG W/I → Personnel-related items (IHDC's own pack: Taxation - see entity-types § 7 #1). Xero printed COGS as one "Purchases" line: `--cos-lines grouped` |
| Gersbach Family Trust (trust) | 2026 Workpapers | 19, 0 REVIEW | Profit Before Distribution 180,947; distributed in full; 10 pages | Beneficiary Accounts (2 beneficiaries, numbered by name; codes 46010 / 46020 matched by code). PY balance sheet in the workpaper does not balance → ERROR, as it should (Xero PY differs: the 740,000 shares were still held) |
| Premier Fire NZ Trust (trust) | 2025 Workpapers | 49, 0 REVIEW | Profit Before Distribution 122,490 = distribution | PY column of the workpaper is inconsistent (FX gain sign; PY Net Profit excludes Other Income) → ERROR; use the 2024 pack for comparatives. "Loan - G Stalley" → Payables (trust rule) |
| Chawla Family Trust (trust) | 2026 Workpapers | 14, 0 REVIEW | Loss (324) (PY (3,413)); Net Assets (158,734) | Mistyped "Opearting Profit" subtotal skipped; withholding tax → Receivables; matches the Xero pack line for line apart from deliberate wording |
| Hassarati Family Trust (trust) | 2026 Workpapers | 13, 0 REVIEW | Loss (2,697); first year | Single-column layout |
| True Talkers Speech Therapy (sole trader) | 2025 ITR Workpaper, Business tab (A-C / F-H) | 21, 0 REVIEW | Net Profit 27,745; Net Assets 27,745; 6 pages | Matches the FY2025 Xero pack line for line (GST 261 and owner account 19,062 under Receivables) |
| True Talkers Speech Therapy (sole trader) | 2026 ITR Workpaper, Business tab (A-D / K-N) | 34, 0 REVIEW | Net Profit 138,778; Net Assets 166,523 | Tax adjustments below Net Profit ignored; owner account 148,930 under Receivables |
| B Liu & X Wang (partnership) | test workpaper built from the 2026 Xero pack (no Fortis partnership workpaper has a BS) | 30, 0 REVIEW | Loss (268,588.72) distributed; Owner/Partner Accounts 361,398.51 | Partner Accounts statement identical to Xero; cents; "Loans from LBQ" print under Financial Liabilities (Xero: Payables › Non Current - set the code per client) |
