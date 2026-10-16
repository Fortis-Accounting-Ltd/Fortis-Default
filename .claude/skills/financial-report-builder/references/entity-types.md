# Entity types - what each pack looks like

Each layout copies the firm's own Xero report packs found in FYI (cabinet Final
Reports & Returns): 14 company packs for FY2026, 33 trust packs, 4 partnership
packs, and 1 sole-trader Xero pack plus 1 legacy pack. The machine copy is
`assets/report-map.json › entity_layouts` (report set, P&L sections, equity
lines, partner and beneficiary statements, options) and
`assets/boilerplate.json` (wording). `fr_engine.py` lays the entity layout over
the company defaults.

## 1. Summary

| | Company | Trust | Partnership | Sole trader |
|---|---|---|---|---|
| Reports, in order | P&L, BS, Movements in Equity, Notes, [Depreciation], Directors Declaration, Compilation | Income Statement, BS, Trust Income Distribution, [Beneficiary Accounts], Notes, [Rental], [Depreciation], Trustee Declaration, Compilation | P&L, [Rental], Partner Accounts, BS, Notes, [Depreciation], Partner Declaration, Compilation | P&L, Movements in Equity, BS, Notes, [Depreciation] |
| P&L middle | Gross Profit from Trading | no Gross Profit line; computed Total Income | as trust | Gross Profit from Trading |
| P&L end | Profit/(Loss) before Taxation → Income Tax Expense → Net Profit After Tax → Dividend Paid → Net Profit After Tax & Dividend | Profit/(Loss) Before Distribution → [tax] → Distribution of Trust Income › Trust Income Distributed → Undistributed Income | Profit/(Loss) Before Distribution → Net Income Distributed › Distribution of Net Income → Undistributed Income | Net Profit/(Loss) |
| Cost of sales heading | Cost of Goods Sold | Cost of Sales | Cost of Goods Sold, grouped Opening inventory / Purchases / Closing inventory | Cost of Sales |
| BS equity | Retained Earnings, Reserves, Share Capital | Retained Earnings, Reserves, settled sum / unit capital by account name, Beneficiary Accounts | one line "Owner/Partner Accounts" (+ Undistributed Income / Reserves when non-nil) | Retained Earnings (+ Funds Introduced / Drawings when there are equity-typed owner accounts) |
| Amounts | whole dollars | whole dollars | **cents** | whole dollars |
| Inventories policy | only with stock | always | always | only with stock |
| Signatories | directors | directors of the trustee company, or the trustee(s) | partners | none (no declaration) |

[ ] = printed only when there is something to show: a register for
Depreciation, rental accounts for the Rental Schedule, per-beneficiary equity
accounts for Beneficiary Accounts.

Common to all four types:

* The cover reads "Financial Reports", the entity, "ABN …" (omitted when there
  is no ABN), the year and "Prepared by Fortis Accounting Partners".
* The Contents page has the footer and lists each report with its start page.
* The footer "Financial Reports | <Entity> | Page x of y" runs from page 2.
* Notes are for the Balance Sheet only, numbered from 2: Cash, Receivables,
  Inventory, Prepayments, PPE, Financial Assets, Intangibles, Investments, Other
  Assets, Payables, Provisions, Financial Liabilities, Loan from
  Shareholders/Associates, Beneficiary Entitlements, Other Liabilities,
  Reserves. Only notes with a non-nil account print. A Reserves note prints
  when there are two or more reserve accounts.
* Footnote placement: "The accompanying notes form part of these financial
  statements…" goes on the Balance Sheet, Trust Income Distribution, Partner or
  Beneficiary Accounts and Depreciation pages. It does not go on the P&L,
  Rental Schedule or Movements in Equity. "These notes should be read in
  conjunction with the attached compilation report." goes on the notes pages.
  Without a Compilation Report (sole trader) the footnote stops after the first
  sentence.
* First year (no PY figures at all): every statement prints a single
  current-year column. A WARNING asks for last year's comparatives if it is not
  really the first year.

## 2. Company

The original five fixtures (`assets/examples/`) and the FY2026 Xero packs
(IHDC, RSJ, Ongoing Concern, AirEnv, Argo, JL Auto, GSP and others).

* Retained Earnings printed = RE + Current Year Earnings − dividends booked to
  equity. Dividends paid through the P&L print in "Dividend Paid". Current Year
  Earnings is checked against NPAT less P&L dividends.
* Division 7A: a debit loan to a director or shareholder prints in Receivables
  (current or non-current, as the workpaper section says). An INFO reminds the
  reviewer to confirm the loan agreement and minimum yearly repayment.
* The Cost of Goods Sold section varies by client. IHDC, Argo, JL Auto and
  Fluenta print a single "Purchases" line (`--cos-lines grouped`). Complex
  Communicators and Premier Fire print one line per account (the default).
  Follow last year's pack.
* Directors Declaration and Compilation Report use singular wording for one
  director and plural for two or more.

## 3. Trust

Gersbach, Premier Fire, Chawla, Hassarati, BM, Stalley, La Bella Vita, Zolot and
25 others.

* **Income Statement** (title): Income, Cost of Sales, Other Income (rental
  accounts collapse into one "Rental income/(loss)" line when there is a Rental
  Schedule), Total Income, Expenses, **Profit/(Loss) Before Distribution**. An
  Income Tax Expense block and Net Profit After Tax print only when there is
  tax. Then **Distribution of Trust Income** › "Trust Income Distributed"
  (`EXP.DIS` plus losses applied `EXP.CFL` and reconciliation adjustments
  `EXP.UDE.1`), and **Undistributed Income**.
* **Trust Income Distribution** always prints, even when nil:
  Current Year Earnings → [Carried Forward Losses Applied] → Net Trust Income
  for Distribution → Distributions to Beneficiaries (each distribution account
  by name) → Undistributed Trust Income.
* **Beneficiary Accounts** prints when there are per-beneficiary equity
  accounts (Drawings / Funds contributed / Share of profits - <name>). It has a
  summary (Funds Introduced, Funds Drawn, Profit Distributed), then one block
  per beneficiary ("Beneficiary 1", "Beneficiary 2" in workpaper order), each
  with Opening Balance when an "Opening balance - <name>" account exists. An
  INFO notes when there are no opening-balance accounts.
* Equity on the BS: Retained Earnings (incl. capital/revenue loss reserves coded
  as reserves → Reserves), Settlement capital / Unit capital by account name,
  Beneficiary Accounts.
* Liabilities: beneficiary loans and current "Loan - <person>" accounts →
  Payables › Current. Non-current beneficiary loans → Financial Liabilities ›
  Non Current › Unsecured. Unpaid present entitlements go to Payables when
  current. When non-current they go to Financial Liabilities › Non Current ›
  Unsecured, as every trust pack shows. The layout code `LIA.NCA.BEN` gives
  them a separate "Beneficiary Entitlements" line and note if a client wants
  one.
* **Trustee Declaration**:
  * Corporate trustee: "The directors of <trustee company> declare that …",
    signed "Director:" / "Date:". Without `--trustee` it says "the trustee
    company".
  * Individual trustee(s) (`--trustee-type individual`, or a trustee name
    without Pty/Ltd): "The trustee(s) declare(s) …", signed "Trustee:".
  * Singular wording for one signatory.
* Compilation: "The Responsibility of the Trustee". "The Trustee is solely
  responsible …"
* Note 1 (a) is the Xero trust paragraph ("…requirements of the trust deed…").
  The PPE policy ends "useful lives to the trust". The Inventories policy always
  prints.

## 4. Partnership

B Liu & X Wang (2025, 2026), Happy Inn (2025), Trinity Projects (2025, 2026),
Fortune Palace.

* Amounts print **with cents**.
* P&L: Income, Cost of Goods Sold (Opening inventory → Purchases → Closing
  inventory), Other Income (+ "Rental income/(loss)"), Total Income, Expenses,
  Profit/(Loss) Before Distribution, **Net Income Distributed** › one line
  "Distribution of Net Income" (the sum of the distribution accounts; a profit
  prints in brackets, a loss as positive), Undistributed Income (nil). B Liu 2026
  listed one line per partner; that variant is not reproduced.
* **Partner Accounts** comes before the Balance Sheet. It has a summary
  (Opening Balance, Funds Introduced, Profit Distributed), then "Partner 1",
  "Partner 2" … each with Opening Balance, Funds contributed - <name>, Profit
  distributions / Share of profits - <name>, and the total. Partner numbering
  follows the ledger, not the declaration.
* BS equity is one line, **Owner/Partner Accounts**, equal to the Partner
  Accounts total.
* Notes: Financial Liabilities has no Secured / Unsecured tier. The Payables
  sub-groups run Current → Personnel-related items → Tax liabilities → Non
  Current.
* Partner Declaration ("The partners declare …", "Partner:", "Dated:"). The
  Compilation Report uses `--legal-name` when the partnership trades under a
  display name (Happy Inn: "The C.Y.Lau Family Trust & The V and J Wong Family
  Trust T/A Happy Inn Restaurant").
* **Workpaper gap.** The partnership workpapers in FYI are tax workpapers. They
  have a P&L and tax reconciliation but no Balance Sheet, and partner equity
  lives only in Xero. To use this skill, the preparer adds the Fortis
  "Profit & Loss" and "Balance Sheet" tabs, with per-partner equity accounts
  named "Opening balance - <name>", "Funds contributed - <name>",
  "Drawings - <name>" and "Share of profits - <name>". Put the distribution
  lines after Net Profit in the P&L tab, as the trust workpapers do.

## 5. Sole trader

True Talkers Speech Therapy (Kristy Cooney) FY2025 is the only Xero-made
sole-trader pack; Chiro for Kids 2017 is a legacy pack.

* Reports: P&L, Movements in Equity, Balance Sheet, Notes, [Depreciation]. The
  firm's Xero pack has **no declaration and no compilation report**. Add them
  with `--order …,declaration,compilation` (Proprietor Declaration and "The
  Responsibility of the Proprietor" wording are in the boilerplate).
* The P&L ends at **Net Profit/(Loss)**. The Xero pack, built on the company
  template, printed "Profit/(Loss) before Taxation", "Net Profit After Tax" and
  "Net Profit After Tax & Dividend", all with the same figure.
* The owner's account and GST held under liabilities print under **Receivables
  › Current** when they are debit balances. A credit owner account stays in
  Payables (REVIEW). Equity-typed Funds contributed / Drawings accounts print as
  Funds Introduced / Drawings lines in equity and in Movements in Equity.
* **Workpaper**: the ITR workpaper "YYYY ITR Workpaper - <Surname Given>" sits
  under the **individual's** FYI entity. It has no P&L / BS tabs. The
  **Business** tab holds the Xero P&L and Balance Sheet pasted side by side.
  FY2025 has P&L in A-C and BS in F-H; FY2026 has P&L in A-D and BS in K-N.
  `build_pack.py` finds the "Balance Sheet" title, reads both blocks, stops the
  P&L at "Net Profit" (tax adjustments follow it), and strips apportionment
  suffixes such as "Cleaning (11.3% Office Area)".
* Note 1 is written for the proprietor ("…prepared for use by the proprietor of
  <business>…"). The PPE policy ends "useful lives to the business".

## 6. Deliberate corrections to the Xero templates

The Xero templates carry slips that the firm's packs repeat. The skill prints
the corrected text. Revert any of them in `boilerplate.json` if the firm prefers
the Xero text verbatim.

| Where | Xero prints | The skill prints |
|---|---|---|
| Trustee Declaration | "The directors of trustee company declare…", "the directors of Trustee declare", "In the directors opinion" | "The directors of <trustee company> / the trustee company…", "In the directors' opinion" |
| Any declaration / compilation, one signatory | plural wording ("directors … who are responsible") | singular ("director … who is responsible") |
| Partnership Note 1 | the trust paragraph ("…requirements of the trust deed… The trustees have determined that the trust…") | partnership wording ("…for use by the partners… the partnership is not a reporting entity") |
| PPE policy, non-company | "directors' valuation … useful lives to the company" | trustees' / partners' / proprietor's valuation, "to the trust / partnership / business" |
| Sole trader | company Note 1, company P&L ending, footnote referring to an absent compilation report | proprietor Note 1, "Net Profit/(Loss)", footnote without the compilation sentence |
| Any pack | stale years and hand-typed names (Complex 2026 "30 June 2025", Frontier "Accounting Policie") | dates and names derived from the entity facts |
| Trinity / Happy Inn | an empty trailing "Compilation Report" page | no blank pages |

## 7. Decisions for the firm

The skill works today with the defaults below. Each one is a one-line change
if the firm prefers the alternative.

| # | Question | Default now | Change with |
|---|---|---|---|
| 1 | PAYG withholding / instalments: Personnel-related items or Taxation? | Personnel-related items (firm chart `LIA.CUR.PAY.PAY`) | `chart.py set-code … --report-code LIA.CUR.TAX` for 34300 / 35510 |
| 2 | Add "Share of profits - Beneficiary n" accounts to the trust chart? | layout code `EQU.PFT.n`, no review | `chart.py add --type trust …` |
| 3 | Remove the client-specific accounts from the partnership / sole-trader charts (Sushi Musketeers, Shuhuachen)? | kept | `chart.py rename` or edit the CSV |
| 4 | Partnership Note 1: Xero's trust wording or partnership wording? | partnership wording | `boilerplate.json › policies.basis_by_type.partnership` |
| 5 | Sole trader: Xero's company-template P&L ending and Note 1, or tailored? | tailored | `entity_layouts.sole_trader.pnl_sections`, `basis_by_type.sole_trader` |
| 6 | Sole trader: add a Proprietor Declaration and Compilation Report? | not printed (as Xero) | `--order pnl,equity,bs,notes,depreciation,declaration,compilation` |
| 7 | First year: one column (as trust packs) or a "-" comparative column (as True Talkers)? | one column | `options.single_column: false` |
| 8 | Partnership workpaper template with BS and partner equity tabs | needed for partnerships | firm template change |
