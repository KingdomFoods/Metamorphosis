# GO-LIVE CHECKLIST — flipping `LIVE_MODE=true`

`LIVE_MODE` is the **single** go-live switch. There is **no bypass** in the code. While it is
`false`, all order-to-cash automation touches only the dummy SKU `K24-TEST-001`, invoices are DRAFT,
and nothing is emailed or charged. Flipping it to `true` activates real SKUs, real invoices and
(once enabled) real sends.

**Do the steps IN ORDER. Each has an owner. Do not skip ahead — the gate exists because the live item
master is non-compliant today.**

| # | Step | Owner | How / verify |
|---|------|-------|--------------|
| 1 | **CA signs off HSN/GST mapping** for all SKUs | **CA — Amandeep Singh & Associates (FRN 028635N)** | Signed HSN/GST sheet (source: `data/reports/hsn_gst_proposal_for_CA.csv`). Until signed, do **nothing** below. |
| 2 | **Map GST to every item** (rate + HSN) | Zoho admin (UI / Books) | CA-gated. Done in the Zoho UI, **not** by this bundle's code. Verify: no item left at 0% / unmapped. |
| 3 | **Set the org GSTIN** = `09AAJCK4455F1ZC` | **Babli Kumari (Super Admin)** | Settings → Organization Profile → GSTIN. Currently **UNSET** (verified). UI-only, CA-gated. |
| 4 | **Load cost prices** on the 96.2% of items missing them | Purchase team | source: `data/reports/missing_costs_for_purchase_team.csv`. Verify: every active SKU has a purchase rate. |
| 5 | **Grant the integration user access to `K24 Sector 68 - MMR`** location | Zoho admin | Settings → Users → integration user → Locations → tick MMR. **Verified blocker** — without it, real stock-in fails. |
| 6 | **Swap dummy SKU → real SKUs** | Ops + dev | Real orders already carry real SKUs; safe-mode substitution stops automatically when `LIVE_MODE=true`. Confirm `app.py` resolves real SKUs (it will, once GST is mapped so SO/invoice lines validate). |
| 7 | **Decide the single invoice creator** (native SO-cycle **or** app.py) | Dev | Avoid double invoices: keep the native Sales Order Cycle as creator and disable `app.py`'s `_create_draft_invoice`, OR vice-versa. See RUNBOOK_SalesOrderCycle.md §5. |
| 8 | **Enable invoice auto-send / emailing** (and payment links if used) | Finance | Settings → Preferences → Invoices. Only after steps 1–4. This is the last thing turned on. |
| 9 | **Flip `LIVE_MODE=true`** in the Render env and redeploy | Dev (with sign-off from Babli + CA) | Render → Environment → `LIVE_MODE=true`. Set `liveMode=true` in `collections_reminder.dg` and `production_workorder.dg` too. |
| 10 | **Smoke test in production** with ONE small real order | Ops + Finance | One real customer, small value, confirm correct GST on the invoice, then proceed. |

## Pending separate decision (NOT part of this checklist)
A **.com → .in data-center migration** (for Indian GST e-invoicing / IRN) is pending at Director
level. This bundle runs on the current **.com** org; all GST/e-invoicing specifics are isolated so
they can be repointed after that migration. **Do not** attempt the migration as part of go-live.

## Rollback
Set `LIVE_MODE=false` and redeploy — automation immediately returns to dummy-SKU / draft / no-send.
Nothing in the code persists a "live" state outside this env var.
