# Kingdom 24 — Metamorphosis Phase 2 & 3 (Zoho)

Operational automation for K24 on the **paid Zoho One** plan, **.com** data center (verified — not .in).
**Phase 2** = the CRM lead→deal pipeline the sales team works in. **Phase 3** = order → production →
raw-material → inventory automation, built in **SAFE MODE** behind the compliance gate.

> **Phase 1 (lead funnels) is intentionally NOT built** — only clean extension points are left.
> See `PHASE1_EXTENSION_NOTES.md`.

---

## The single switch: `LIVE_MODE` (default `false`)
While `false`: all order-to-cash automation points at ONE dummy SKU `K24-TEST-001`, invoices are
**DRAFT** and never sent, no customer email / payment link fires, no real SKU is touched. There is
**no bypass**. Going live = a human completes `GO_LIVE_CHECKLIST.md` and flips `LIVE_MODE=true`.

---

## What's CODE vs what's a RUNBOOK (human clicks)

### Code (in this bundle)
| File | Purpose |
|------|---------|
| `zoho_client.py` | Shared async Zoho client — .com base, org_id injection, token refresh (+disk cache), retry, rate-limit, **scope-error fail-fast** |
| `crm_setup.py` | **Phase 2** — idempotently creates the Lead custom fields (+ `Source_Record_Id`/`Source_Payload` audit fields, + `Website (Shoopy)` picklist value); defines `score_lead` / `assign_lead` oracle logic |
| `app.py` | **Phase 3 + lead webhooks** — FastAPI: `/webhook/order`, `/production/stockin`, `/rm/stockin`, `/rm/issue`, `/production/workorder`, `/health`, **`/webhook/shoopy` (+health)**, **`/webhook/whatsapp` (GET verify + POST)**. Single-file (Render-style). SAFE-MODE guarded |
| `leads.py` | Shared CRM-Lead upsert for all sources — dedupe mobile→email (+ in-process cache for index-lag), idempotent on external id, scoring via `score_lead`. **Leads only — never invoices/stock** |
| `deluge/score_lead.dg` | Rule-based lead scoring (runs server-side on lead create) |
| `deluge/assign_lead.dg` | Round-robin assignment + Day-0 first-touch task |
| `deluge/production_workorder.dg` | Finished-goods IN + RM traceability (calls `app.py`) |
| `deluge/collections_reminder.dg` | Overdue buckets + >₹10L flag + call task (**SAFE MODE = log only**) |
| `test_crm.py` | Phase 2 live smoke test (creates a lead, scores/assigns/tasks, deletes it) |
| `test_flow.py` | Phase 3 SAFE-MODE e2e (draft SO + draft invoice on dummy, zero sends, adjustments posted) |
| `test_integration.py` | Lead-source e2e (Shoopy order → lead; idempotency; mobile dedupe; cross-source dedupe; WhatsApp; cleanup) |

### Runbooks (UI-only — the human operator clicks these)
| File | Covers |
|------|--------|
| `runbooks/RUNBOOK_CRM_Fields.md` | Lead field spec + manual fallback |
| `runbooks/RUNBOOK_CRM_Workflows.md` | Install Deluge functions, workflow rules, Day-0/3/7 cadence, 14-day stale reassign, Zia note |
| `runbooks/RUNBOOK_CRM_Blueprint.md` | Blueprint on `Pipeline_Stage` (New→…→Deal + common exits) |
| `runbooks/RUNBOOK_SalesOrderCycle.md` | Enable SO→Invoice (DRAFT, no auto-send); stock-decrement point; batch caveat |
| `runbooks/RUNBOOK_Creator_Production.md` | Zoho Creator "Production Batch" form spec + on-submit Deluge |
| `runbooks/RUNBOOK_Inventory_Setup.md` | Locations, zones, reorder, FIFO/expiry + the **MMR-access go-live blocker** |
| `runbooks/RUNBOOK_Collections.md` | Schedule the daily sweep; turn on sends only at go-live |
| `runbooks/RUNBOOK_Shoopy_Webhook.md` | **Source 1** — configure the Shoopy webhook (Bearer/HMAC), go-wide procedure |
| `runbooks/RUNBOOK_IndiaMart_Plugin.md` | **Source 2** — install the official IndiaMART plugin (no code) + uniformity workflow |
| `runbooks/RUNBOOK_WhatsApp_NativeChannel.md` | **Source 3** — native Zoho WhatsApp channel (preferred) or the Cloud-API fallback |
| `GO_LIVE_CHECKLIST.md` | The ordered switch to `LIVE_MODE=true`, each step with an owner |
| `PHASE1_EXTENSION_NOTES.md` | Built lead sources + still-deferred funnels (phone/Meta ads) + the Shoopy payment-event caveat |

---

## Lead sources (Phase 1 funnels — leads only, never invoices/stock)
| Source | How | Channel value (`Inbound_Source`) |
|--------|-----|-----------------------------------|
| **Shoopy website** | **CODE** — `POST /webhook/shoopy` (Bearer + optional HMAC). order.created/updated/cancelled + customer.* | `Website (Shoopy)` |
| **IndiaMART** | **CONFIG** — official Zoho Marketplace plugin (Push API real-time; Pull every ~2h fallback). One workflow copies its `Lead_Source` → `Inbound_Source` | `IndiaMart` |
| **WhatsApp** | **CONFIG preferred** — native Zoho WhatsApp channel. **CODE fallback** — `POST /webhook/whatsapp` (HMAC-256) if native can't fit | `WhatsApp` |

**WhatsApp path decision (do step 0 of its runbook first):** a number is on a **BSP** *or* **Meta
Cloud-API direct**, not both. BSP → native Zoho channel with BSP creds. Cloud-API direct → native channel
*or* the coded fallback. K24 number +91 88601 11090 (WABA 789633577087490).

All sources funnel through `leads.upsert_lead`: **dedupe mobile→email**, **idempotent on the source's
external id**, **scored** via `score_lead`; assignment runs server-side in `assign_lead.dg` (we never call
the scope-blocked `/crm/v6/users`). **Phone (PSTN) calls are NOT buildable via the Meta number** — deferred
to a cloud-telephony procurement (see `PHASE1_EXTENSION_NOTES.md`).

> ⚠ **Shoopy has no payment-confirmation event.** `order.created` = *placed, not paid* — fine for leads.
> The gated invoicing flow must use `order.updated` (paid) or Razorpay `payment.captured`, never
> `order.created`. Documented in `PHASE1_EXTENSION_NOTES.md`.

## Run it

```bash
cd metamorphosis
pip install -r requirements.txt
cp .env.example .env          # fill in ZOHO_* + WEBHOOK_SHARED_SECRET ; keep LIVE_MODE=false

python zoho_client.py         # 1. validate auth on .com (prints the org)
python crm_setup.py           # 2. Phase 2: create Lead fields (idempotent)
python test_crm.py            # 3. Phase 2 smoke test (must pass)
python test_flow.py           # 4. Phase 3 SAFE-MODE e2e (must pass, zero sends)
python test_integration.py    # 5. lead-source e2e (Shoopy + WhatsApp, dedupe proven)

uvicorn app:app --port 8000   # run the API locally (Phase-3 + lead webhooks)
# deploy: Render web service, start command `uvicorn app:app --host 0.0.0.0 --port $PORT`
```

All POST endpoints require header `X-Webhook-Secret: <WEBHOOK_SHARED_SECRET>`.

---

## Verified live facts (from this build)
- Auth works on **.com**; org = *Kingdom 24 Private Limited* (`906246204`).
- **Org GSTIN is UNSET**; 100% of items have **no GST** → compliance gate is real → SAFE MODE required.
- Dummy SKU `K24-TEST-001` created (tracked, **non-taxable** so safe-mode SO/invoice validate).
- Locations: `Head Office` (API-primary, integration user can write — used in safe mode) and
  `K24 Sector 68 - MMR` (holds real stock — **integration user lacks access; grant at go-live**).
- CRM: scope to **create fields** works; `/crm/v6/users` is scope-blocked by design (assignment runs
  in Deluge server-side, so that's fine).
- Items are **not** batch-tracked today (so the SO cycle auto-converts) — revisit if you enable it.

## Notes
- `zoho_client.py` caches the short-lived **access** token to a temp file so repeated runs don't hit
  Zoho's refresh-token throttle (which returns `Access Denied` after a few refreshes per window).
- A `.com → .in` migration (Indian GST e-invoicing/IRN) is a **pending Director decision** — not done
  here; GST/e-invoicing specifics are isolated so they can be repointed later.
```
