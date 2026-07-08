# Metamorphosis Go-Live — status (2026-07-08)

Executed the actionable slice of the FINAL GO-LIVE prompt against the live org
(906246204, "Kingdom Foods", .com). What follows is the honest per-phase state:
✅ done by code · 📋 UI-only (runbook ready, human must click) · ⛔ gated/blocked.

## PHASE A — Verify org state ✅ COMPLETE
| Check | Result |
|-------|--------|
| A1 OAuth / CRM | ✅ `.com`, org "Kingdom Foods" |
| A2 Inventory DC | ✅ **`.com` confirmed** (`.in` → 401). Prompt's ".in maybe" is wrong. ⚠️ token spans multiple orgs — `.com/inventory/organizations[0]` = **"Agro Nexus Private Limited"**; safe only because we always pin `organization_id=906246204` |
| A3 Items | 513 total, **386 active** (incl. 15 demo, since cleaned) |
| A4 CRM fields | ✅ **10/10 present** (incl. SKU_Interest, Missed_Call_Flag, Next_Action_Date, Source_Payload) |
| A5 Scoring | ⚠️ **0/37 scored** → workflow not deployed (now backfilled, see B1) |
| A6 GSTIN | ⚠️ **UNSET** (`gst=False`). Set `09AFJPB3153M1ZC` in UI before invoices |

## PHASE B — Deploy automations (B1 ✅ code · B2–B6 📋 UI)
- **B1 Lead scoring** — ✅ **backfilled 37/37 leads** via `lead_score_backfill.py` (same
  `crm_setup.score_lead` oracle). Scores 0–36, **all "Cold"** — because `Business_Type`
  is empty on every lead (30-pt driver). Real fix: capture Business_Type at intake.
  Workflow deploy is 📋 UI — `RUNBOOK_CRM_Workflows.md` §A–B.
- **B2 Deal→SO** — 📋 `deluge/deal_won_to_so.dg` + `RUNBOOK_GoLive_Builds_20260708.md`.
- **B3 Collections** — 📋 `deluge/collections_reminder.dg` + `RUNBOOK_Collections.md`.
- **B4 Blueprint** — 📋 `RUNBOOK_CRM_Blueprint.md`.
- **B5 Follow-up rules (D0/D3/D7/14d)** — 📋 `RUNBOOK_CRM_Workflows.md`.
- **B6 Bank rules (7)** — 📋 `RUNBOOK_GoLive_Builds_20260708.md` (account IDs included).

> Deploying workflow rules / custom functions / Blueprint / bank rules is **UI-only** —
> the Zoho API cannot create them. These are click-paths for Divyanshu/Babli, not code.

## PHASE C — Cleanup ✅ (C1 done · C2/C3 flagged)
- **C1** ✅ demo invoice/SO/PO/customer/vendor **deleted**; 15 `DEMO-*` items **inactivated**;
  **0 active [DEMO] items**. `K24-TEST-001` intentionally kept (SAFE-MODE dummy until E).
- **C2** ⚠️ **do NOT delete the 4 synced Rashi leads yet** (ANKUR, PRIME, Cosmos, Divyan).
  They carry CRM IDs in sheet col L — deleting now breaks the write-back (re-sync would PUT
  to a dead id, then create duplicates). Correct order: re-run the corrected `bulkSyncAllTabs`
  first (fixes ANKUR/PRIME to real data), THEN delete the Cosmos/Divyan test **rows + leads** together.
- **C3** ⚠️ 2 orphan frozen SKUs (`K24-FRZ-SPRLL-1KG`, `K24-FRZ-MOMO-1KG`, inactive, hold
  stock) — human decision pending (`item_reconciliation_20260708.md`).

## PHASE D — Production item load ⛔ GATED (no data)
Blocked: the fill sheet (`K24_Zoho_Item_Import_Template.xlsx`, ② Item List) with real
cost + opening stock + CA HSN hasn't arrived (Trilok/Babli/CA). Note the reframe already
proven: it's an **upsert of cost/stock onto the 370 existing SKUs**, NOT a create of 379.
`load_items.py` is ready (idempotent on SKU). Run D0 gate-check → dry-run → `--commit`
when the sheet lands.

## PHASE E — Flip LIVE_MODE ⛔ GATED (depends on D) + not my surface
`LIVE_MODE` is a Render env var on the FastAPI service — I can't flip it (no Render creds,
dir not git). Do it after D loads ≥350 items, then verify one order end-to-end.

## PHASE F — E2E validation ⛔ mostly gated
Needs the B-phase workflows deployed (UI) + real SKUs (D) + a sheet edit (browser) + Render
live. Re-runnable once those land; the sheet→CRM and IndiaMART paths were already tested
live this week (sheet sync round-trip; IndiaMART webhook create/update/delete).

## PHASE G — Handover docs (G4 ✅, G1–G3 pending)
- **G4 Known Issues** ✅ `KNOWN_ISSUES.md`.
- **G1 Admin guide / G2 Ops runbook / G3 Architecture** — not yet written; best done once the
  B-phase deploys + D load are complete so they document the real end state (say the word).

## The gate, plainly
Everything I can do **headlessly** is done: org verified, DC ambiguity settled, scores
backfilled, demo data cleaned. Everything remaining is one of: **UI clicks** (B deploys,
GSTIN, Blueprint — runbooks ready), **waiting on data** (D), or **Render access** (E). None
are code I can run. Saturday readiness now depends on those human/UI steps, not on more code.
