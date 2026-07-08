# RUNBOOK — Sales Order Cycle (Order → Invoice) (Phase 3)

**Audience:** Zoho admin (Books/Inventory)
**Why UI-only:** Enabling the Sales Order Cycle and its auto-create/auto-send toggles is a
**Settings UI** action — no API. `app.py` creates the Sales Order (and, in safe mode, an explicit
DRAFT invoice to prove the chain). This runbook enables the **native** SO→Invoice automation that
takes over at go-live.

Org: Kingdom 24 · Zoho Inventory + Books · **.com** · **SAFE MODE caveats called out below**

---

## 1. Where stock decrements (read first)
- **Sales Order** = a commitment. It does **NOT** decrement stock.
- Stock decrements at **Shipment** (if you use packages/shipments) **or** at **Invoice** (if you
  invoice directly without shipping). For K24 finished goods, decide one model and keep it:
  - **Recommended:** decrement at **Invoice** (simpler; no separate shipment step).
- **app.py** posts finished-goods/RM movements as **Inventory Adjustments** (independent of the SO
  cycle) so production and RM flows are explicit and idempotent.

## 2. Enable the Sales Order workflow
**Settings → Preferences → Sales Orders:**
1. Turn **Sales Orders ON** (if not already).
2. Under **Sales Order Cycle** (a.k.a. *"Automate Sales Order workflow"*), enable
   **"Convert Sales Order to Invoice automatically"**.
3. Set the converted invoice to be created as **Draft** (NOT "Save and Send").

## 3. Keep auto-send / auto-email OFF while LIVE_MODE=false  ← CRITICAL
**Settings → Preferences → Invoices** (and the SO-cycle conversion settings):
- **Automatic invoice emailing: OFF.**
- **"Send a copy to customer on creation": OFF.**
- Do **NOT** enable any payment-link / Razorpay auto-attach on draft invoices yet.
- Rationale: the item master is non-compliant (no GST, org GSTIN unset). Sending an invoice now
  would issue a non-compliant tax document. SAFE MODE in `app.py` already blocks sends from code;
  this keeps the **UI** path equally silent.

## 4. Batch / expiry caveat (do NOT assume)
- Frozen-food SKUs may later be **batch/expiry tracked**. **If an item is batch-tracked, the SO
  cycle's auto-package/auto-shipment will NOT fire automatically** — Zoho requires you to pick the
  batch at shipment, which is a manual step.
- **Today** the live items (and the dummy `K24-TEST-001`) are **not** batch-tracked (verified), so
  auto-conversion works. **When you enable batch tracking on real SKUs at go-live, the shipment
  step becomes manual** — document the picker step for the dispatch team then.

## 5. Go-live handoff (when LIVE_MODE flips to true)
1. Enable steps 2–3 (auto-convert to **Draft** invoice; emailing still controlled per policy).
2. In `app.py`, the explicit `_create_draft_invoice` step becomes redundant with the native cycle —
   **disable it** (guard it behind `if not native_cycle_enabled:`) to avoid double invoices, OR keep
   `app.py` as the sole creator and leave native auto-convert OFF. **Pick ONE creator.** Recommended:
   keep the native cycle (UI) as the creator in production and turn off app.py's invoice step.
3. Only after CA signoff + GST mapped + GSTIN set may invoice **emailing/sending** be turned on
   (see GO_LIVE_CHECKLIST.md).

---
**Linked:** [RUNBOOK_Inventory_Setup.md](RUNBOOK_Inventory_Setup.md) · [../GO_LIVE_CHECKLIST.md](../GO_LIVE_CHECKLIST.md)
