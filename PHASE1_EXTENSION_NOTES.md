# PHASE 1 EXTENSION NOTES — where the 5 deferred funnels plug in

**Phase 1 is NOT built now.** This documents exactly where each deferred lead funnel attaches, so
adding it later needs **no rework**. Two extension points already exist:

1. **CRM field `Inbound_Source`** (picklist) already contains every funnel value:
   `Phone, WhatsApp, Website, Instamart, Meta Ads, Missed Call, Manual, IndiaMart`.
   A new funnel just creates a CRM Lead with the right `Inbound_Source` — no schema change.
2. **Webhook stub**: the Phase-3 `POST /webhook/order` (in `app.py`) already accepts a `source` field
   and is secured by `X-Webhook-Secret`. A lead-intake endpoint follows the same pattern — add
   `POST /webhook/lead` that validates the secret, maps the payload to a CRM Lead, and returns. The
   auth + client + idempotency plumbing is all reusable.

| # | Funnel | Realistic mechanism (why) | Lands as | Maps to |
|---|--------|---------------------------|----------|---------|
| 1 | **Meta phone-call data** (missed/PSTN calls from ads) | Needs a **cloud-telephony layer (Exotel / MyOperator)**. The WhatsApp API **cannot** see missed PSTN calls, so a telephony provider must capture the call event and POST it. | CRM Lead via `POST /webhook/lead` | `Inbound_Source = "Missed Call"` (or `"Phone"`); set `Missed_Call_Flag = true` |
| 2 | **WhatsApp leads** | **WhatsApp Business API via a BSP** (e.g. Gupshup/Wati/360dialog). Inbound message webhook → lead. | CRM Lead via webhook | `Inbound_Source = "WhatsApp"` |
| 3 | **Website (Shoopy)** | Shoopy has **no public API/webhook**. Use the **Razorpay payment webhook** (already the Phase-3 order path) as the trigger — a paid order becomes a customer + order, and a started-but-unpaid checkout can become a lead. | `POST /webhook/order` (existing) for orders; `/webhook/lead` for abandoned carts | `Inbound_Source = "Website"` (orders) / `"Instamart"` if via that storefront |
| 4 | **IndiaMart** | **IndiaMart Lead Manager API** (pull). A scheduled job polls the API and creates leads. | scheduled pull → CRM Lead | `Inbound_Source = "IndiaMart"` |
| 5 | **Meta / Insta / FB Lead Ads** | **Meta Lead Ads** → Zoho native connector or Zoho Flow (no custom code needed for the happy path). | Zoho Flow → CRM Lead | `Inbound_Source = "Meta Ads"` |

## Implementation guidance (when Phase 1 starts)
- Add `POST /webhook/lead` to `app.py` mirroring `/webhook/order`: secret check → Pydantic model →
  create CRM Lead (reuse `ZohoClient`). The server-side `score_lead` / `assign_lead` Deluge already
  fire on lead create, so a new funnel's leads are scored + assigned with **zero** extra work.
- Each provider gets a thin adapter that normalizes its payload to the lead model. Keep secrets in env
  (`<PROVIDER>_TOKEN`), never hardcoded.
- Razorpay (#3) reuses the Phase-3 order webhook — verify the Razorpay signature in addition to (or
  instead of) `X-Webhook-Secret`.

**Do NOT build these now.** The Source values + the secured webhook pattern are the only contract the
funnels need; everything downstream (scoring, assignment, blueprint, order-to-cash) already works.

---

## UPDATE — what's now BUILT vs still DEFERRED (lead-source integration)

### BUILT (leads only; see runbooks)
- **Shoopy website** → `POST /webhook/shoopy` in `app.py` (Bearer + optional HMAC). Maps
  order.created/updated/cancelled + customer.* → CRM Lead, `Inbound_Source="Website (Shoopy)"`.
  Runbook: `RUNBOOK_Shoopy_Webhook.md`.
- **IndiaMART** → official Zoho Marketplace plugin (no code). Runbook: `RUNBOOK_IndiaMart_Plugin.md`.
- **WhatsApp** → prefer Zoho native channel; Cloud-API **fallback** coded at `POST /webhook/whatsapp`.
  Runbook: `RUNBOOK_WhatsApp_NativeChannel.md`.
- Shared upsert (`leads.py`): dedupe mobile→email, idempotent on external id, scoring via `score_lead`.
  Two CRM audit fields added: `Source_Record_Id` (external id) and `Source_Payload` (raw JSON).
  Picklist value `Website (Shoopy)` added to `Inbound_Source`.

### ⚠ Shoopy has NO payment-confirmation event — important for the GATED invoicing flow
`order.created` fires when an order is **PLACED, not PAID**. For **lead capture** (built) that's correct.
But the **Phase-3 order-to-cash flow (already built, gated behind LIVE_MODE)** must **NOT** treat
`order.created` as paid. When that flow is wired to the website later, the **payment trigger** must be
either Shoopy **`order.updated` with a paid status** OR the **Razorpay `payment.captured`** webhook —
never `order.created` alone. (We capture `payment_mode` and `due_amount` on the lead for context only.)

### STILL DEFERRED — documentation only (no build)
- **Phone calls (PSTN, missed + attended):** **NOT possible** via the Meta WhatsApp number — confirmed by
  Meta/Twilio/360dialog docs (WhatsApp cannot receive PSTN calls). Needs a **cloud-telephony layer
  (Exotel / MyOperator / Knowlarity)**: forward the line to a virtual number that logs every call and
  pushes to Zoho CRM natively. **Procurement decision.** `Inbound_Source` values `"Phone"` / `"Missed Call"`
  are ready; no code.
- **Meta / Instagram / Facebook Lead Ads:** Meta Lead Ads → Zoho CRM (native sync or Zoho Flow). Document;
  build later. `Inbound_Source="Meta Ads"`.
- **WhatsApp in-app Calling API:** needs the 2,000-conversation messaging tier + SIP/WebRTC. Defer.
