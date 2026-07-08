# Go-Live Builds — deploy runbook (2026-07-08)

Covers the three "build while waiting" items that are code/config, not live yet:
Deal Won → Sales Order (#4), HDFC Bank Rules (#5), Collections (#6). CRM Dashboard
views (#3) are already created live via API — see the four `K24 - *` views on Leads.

All account/bank IDs below are the **live** org 906246204 ("Kingdom Foods", .com DC),
pulled 2026-07-08.

---

## #4 — Deal Won → Sales Order  (the CRM→Inventory bridge)

**Artifact:** `metamorphosis/deluge/deal_won_to_so.dg` (safe-mode, idempotent).

**Why UI deploy:** Zoho's API cannot create workflow rules or custom functions — same
constraint as the scoring oracle. This is a click-path.

**Prereq (2 min):** create a Deals custom field **"Books SO Number"**
(`Books_SO_Number`, single line) — used for idempotency + write-back.

**Deploy:**
1. Setup → Functions → New: name `K24_deal_won_to_so`, module **Deals**, category
   **Automation**; argument `dealId` (Long) → `${Deals.Deal Id}`; paste
   `deal_won_to_so.dg`; **Save**.
2. Setup → Automation → Workflow Rules → Create, module **Deals**, rule
   "Deal Won → Sales Order", on **Edit**, condition **Stage is "Closed Won"**
   (use the org's actual won stage), instant action → Function `K24_deal_won_to_so`
   (pass Deal Id). Save.
3. Confirm the CRM↔Books connection exists (Zoho One): the function calls
   `zoho.books.createRecord(...)` — it needs the Books connection wired once.

**Test in SAFE MODE first** (`liveMode=false`, the default): win a test Deal → the
function logs the full SO payload but creates nothing. Read the function log. When the
payload looks right, flip `liveMode=true` and re-run.

**Known dependencies the SAFE-MODE log will expose:**
- Deal product subform api name — the script reads `Product_Details`. If K24's layout
  uses a different subform (e.g. `Ordered_Items`), change that one line.
- Each product must map to a Books item by **SKU** (Product_Code) or name — the 370
  catalog items are already in Books, so this works once Deals carry products.
- Per-line `tax_id` comes from the matched item's default tax (per-line-tax gotcha).

---

## #5 — HDFC Bank Rules  (auto-categorize the feed)

**Status:** the Books bank-rules API endpoint exists (`GET /bankaccounts/rules`, 0 rules
today) and accepts POST, but the exact bank-account binding field wasn't resolved in
this session's budget. Rules take ~20s each in the UI, so build them there. **All target
account IDs are below** — no lookup needed.

**Where:** Zoho Books → Banking → **HDFC Bank - Current (0996)** → Rules → **+ New Rule**.
Bank account id `7530276000000191002`.

For each rule: **Transactions in** = *Withdrawals* (money out) unless noted; **Criteria**
= *Description/Payee* **contains** the match text; **then Categorize as** the account below.

| # | Rule name | Match (description/payee contains) | Apply to | Categorize as → account | Account ID |
|---|-----------|-----------------------------------|----------|-------------------------|-----------|
| 1 | Bank charges | `CHRG`, `SERVICE CHG`, `CHARGES` | Withdrawals | **Bank Fees and Charges** | `7530276000000000409` |
| 2 | OD interest | `INT ON OD`, `INTEREST` | Withdrawals | **Interest on OD** | `7530276000000116058` |
| 3 | SS Caterers | `SS CATERERS` | Withdrawals | **Cost of Goods Sold** | `7530276000000034003` |
| 4 | Momentum/Organicut | `MOMENTUM`, `ORGANICUT` | Withdrawals | **Cost of Goods Sold** | `7530276000000034003` |
| 5 | Haldiram | `HALDIRAM` | Withdrawals | **Cost of Goods Sold** | `7530276000000034003` |
| 6 | Director remuneration ⚠ | `BABLI KUMARI` | Withdrawals | **Director Remuneration** *(flag: review each)* | `7530276000000116050` |
| 7 | Loan/EMI | `EMI`, `LOAN` | Withdrawals | **Secured Bank Loan - Term** | `7530276000000116046` |

**Caveats worth honoring:**
- Rules #3–5 (suppliers) flat-book to COGS. That's fine as a fallback, but once vendor
  **bills** exist in Books, prefer letting the feed **match to the bill** (accurate AP)
  over a blanket COGS rule — consider limiting these rules or reviewing before accept.
- Rule #6 (BABLI KUMARI) is a **related-party** outflow — keep it review-only, don't
  auto-accept. It could be remuneration, an advance (`Director Advance - Babli Kumari`,
  `7530276000000116034`), or a loan. Categorize per transaction.
- Rule #2 "INTEREST" is broad — order it **after** #1 so charges match first.

---

## #6 — Collections (D7/D15/D30)  — already built

**Artifact:** `metamorphosis/deluge/collections_reminder.dg` (safe-mode; buckets 7/15/30,
flags any customer > ₹10L overdue, creates internal call Tasks; sends NO customer email
until `liveMode=true`).

**Deploy (per `RUNBOOK_Collections.md`):** create it as a **scheduled** custom function
(daily). One edit needed: replace the `"ORG_ID"` placeholder in the
`zoho.books.getRecords("invoices", "ORG_ID", ...)` call with the Books connection's org
(`906246204`) or the connection default.

**Keep `liveMode=false` until real invoices exist** — otherwise it emails customers about
invoices that aren't there yet. Flip to true post go-live (owner: Finance).

**Note:** collections is a *lagging* system — it does nothing useful until Order-to-Cash
is live and invoices are flowing. Deploy it now, but it only earns its keep after S8.
