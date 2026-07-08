# RUNBOOK — Inventory Setup: Locations, Zones, Reorder, FIFO/Expiry (Phase 3)

**Audience:** Zoho Inventory admin
**Why UI-mostly:** Reorder points, storage zones, FIFO/first-expiry valuation and low-stock alerts
are Settings UI actions. This runbook lists them with the **verified live facts** the integration
discovered, including a **go-live blocker** you must clear.

Org: Kingdom 24 · Zoho Inventory · **.com** · Org ID 906246204

---

## 0. VERIFIED LIVE FACTS (from this build)
- **Locations (multi-branch):**
  - `Head Office` — `7530276000000093251` — **API-primary**, holds no real stock. **The integration
    user can write here** → SAFE MODE posts dummy stock to this location.
  - `K24 Sector 68 - MMR` — `7530276000000132001` — **holds all real stock**; the GO-LIVE target.
- **⚠ GO-LIVE BLOCKER (verified):** the integration/OAuth user **does NOT have access to the MMR
  location**. Posting stock there returns *"you do not have access to the associated location."*
  → **Before go-live, grant the integration user access to `K24 Sector 68 - MMR`** (Settings → Users
  → open the integration user → **Locations** → tick MMR), then set `ZOHO_PRIMARY_LOCATION_ID` as the
  active location (it already is for LIVE_MODE).
- **Batch tracking:** live items are currently **NOT** batch-tracked. If you enable it (recommended
  for frozen/expiry SKUs), the SO-cycle auto-shipment becomes manual — see RUNBOOK_SalesOrderCycle.md.
- **GST:** org has **no GST configured** and **GSTIN unset** (compliance gate). The dummy SKU is
  marked **non-taxable** so safe-mode flows work; real items need CA-mapped GST before go-live.

## 1. Storage zones (4)
Model the cold-chain zones as **sub-locations** under `K24 Sector 68 - MMR` (Inventory supports
storage locations within a location), or as an item **custom field** `Storage_Zone` if sub-locations
aren't on your plan:
- **Ambient** (dry store) · **Chilled** (0–4 °C) · **Frozen** (−18 °C) · **Quarantine** (QC hold / FAIL)
Route every GRN to a zone (the `/rm/stockin` payload carries `zone`); quarantine stock should not be
issuable to production until QC releases it.

## 2. Reorder points + low-stock alerts
**Inventory → Items → (item) → Edit → Reorder Point.** For each fast-mover set:
- **Reorder point** = avg daily consumption × lead-time days × safety factor.
- **Settings → Preferences → Reminders / Notifications → enable "Reorder level reached" email** to
  the purchase team. (For an automated PO draft, add a scheduled function that lists items below
  reorder point and emails the buyer — keep it a DRAFT PO, no auto-send, while not LIVE_MODE.)

## 3. Valuation: FIFO / first-expiry
**Settings → Preferences → Inventory → Inventory Valuation Method → FIFO.** For perishables, when
batch tracking is on, Zoho can pick **first-expiry-first-out** at shipment — enforce it as the
dispatch rule (pick the nearest-expiry batch). Document this for the dispatch team.

## 4. The dummy SKU (safe mode)
`K24-TEST-001` — tracked, non-taxable, created by `app.py`/`crm? no` (by app startup). It is the ONLY
SKU Phase-3 automation touches while LIVE_MODE=false. **Do not delete it; do not sell it.** At go-live
it is swapped out (see GO_LIVE_CHECKLIST.md) but can remain as a permanent test SKU.

## 5. Reconcile opening stock (pre-existing work)
Opening stock for real SKUs is handled by the repo-root migration toolkit (`data/imports/…`), not this
bundle. Ensure that reconciliation is complete and stock sits in **MMR** before go-live.

---
**Linked:** [RUNBOOK_SalesOrderCycle.md](RUNBOOK_SalesOrderCycle.md) · [RUNBOOK_Creator_Production.md](RUNBOOK_Creator_Production.md) · [../GO_LIVE_CHECKLIST.md](../GO_LIVE_CHECKLIST.md)
