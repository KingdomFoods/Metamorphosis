# RUNBOOK — Zoho Creator "Production Batch" App (Phase 3)

**Audience:** Zoho admin + production floor lead
**Why UI-built:** A Zoho Creator app/form is built on the Creator canvas (no API). This is the
spec the admin builds; on submit it calls `app.py` (which holds the safe-mode guard), so no stock
moves outside the single guarded path.

Purpose: the floor team records each production batch (finished-goods IN + the RM batches consumed
for **traceability** — NO consumption costing yet, per the K24 decision).

---

## 1. Create the app + form
**Zoho Creator → + Create App → "K24 Production"** → add a form **"Production Batch"** with fields:

| Field (label)        | Type            | Notes |
|----------------------|-----------------|-------|
| Batch ID             | Single Line     | unique; idempotency key. Auto-number prefix `BATCH-` recommended |
| SKU                  | Single Line / Lookup | finished-good SKU. SAFE MODE: the on-submit script forces the dummy SKU |
| Planned Qty          | Number          | |
| Actual Qty           | Number          | posted as finished-goods IN |
| Unit                 | Single Line     | pcs / kg |
| Start Time           | Date-Time       | |
| End Time             | Date-Time       | |
| Batch Code           | Single Line     | printed code on the pack |
| Mfg Date             | Date            | |
| Expiry Date          | Date            | |
| QC Status            | Dropdown        | PASS / HOLD / FAIL |
| RM Batches Consumed  | Multi Line / subform | one line per `RM_SKU:RM_BATCH:QTY` (traceability) |
| Posted to Inventory  | Checkbox (read-only) | set true by the on-submit script |

> Keep **Batch ID** unique — `app.py /production/stockin` is idempotent on it, so a re-submit
> won't double-count stock.

## 2. On-Submit Deluge (paste `deluge/production_workorder.dg`)
**Form → Edit → Workflow → On Successful Submit → Run Custom Function:**
1. Paste `deluge/production_workorder.dg`.
2. Map form fields → the script inputs (`batchId`, `sku`, `actualQty`, `mfgDate`, `expiryDate`,
   `qcStatus`, `rmBatches`).
3. Set `appBaseUrl` (your Render URL) and `webhookSecret` (= `WEBHOOK_SHARED_SECRET`).
4. Keep `liveMode = false` until go-live (the script then forces the dummy SKU).
5. Save & **Publish** the app.

## 3. What happens on submit
- Calls `POST /production/stockin` → finished-goods **IN** as an Inventory Adjustment (idempotent).
- For each `RM Batches Consumed` line → calls `POST /rm/issue` → RM **stock-out** linked to `Batch_ID`
  (traceability only; no costing).
- All stock movement goes through `app.py`'s safe-mode guard — in SAFE MODE everything lands on the
  dummy SKU and nothing customer-facing fires.

## 4. QC gate (recommended)
Add a Creator validation: if **QC Status = FAIL**, block the IN posting (don't stock failed batches).
Easiest: only call `/production/stockin` when `qcStatus == "PASS"` (wrap the invokeurl in an `if`).

## 5. Go-live
After go-live, set `liveMode = true` in the on-submit function so the **real** finished-good SKU is
used and real RM batches are issued. No other change needed.

---
**Linked:** [RUNBOOK_Inventory_Setup.md](RUNBOOK_Inventory_Setup.md) · [RUNBOOK_SalesOrderCycle.md](RUNBOOK_SalesOrderCycle.md)
