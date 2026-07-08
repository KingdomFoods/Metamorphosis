# RUNBOOK — Shoopy Website Webhook → Zoho CRM Leads (Source 1)

**Audience:** Shoopy admin + dev
**Code:** `app.py` → `POST /webhook/shoopy` (+ `GET /webhook/shoopy/health`). Reuses `leads.upsert_lead`
(dedupe mobile→email, idempotent on Shoopy `id`, scoring). **Leads only — never invoices/stock.**

---

## 0. What Shoopy confirmed (authoritative)
- Webhooks supported: **order.created** (order *placed*), **order.updated** (status/tracking),
  **order.cancelled**. Bearer-token auth, JSON body, expects a 2xx response.
- **No Order REST API, no CSV export** — the webhook push is the only path (and it's enough for leads).
- **No payment-confirmation event exists.** `order.created` fires on *placement, not payment*. For LEAD
  capture (this build) that's the correct trigger — we record `payment_mode` (ONLINE/COD) for context
  and never assume paid. (The separate gated invoicing flow must NOT treat order.created as paid — see
  `PHASE1_EXTENSION_NOTES.md`.)

## 1. Two things to confirm with Shoopy BEFORE going wide
1. **Is webhook config enabled on our plan, and where in the admin?** (Ask support; it may be under
   *Settings → Integrations/Developer/Webhooks* or enabled per-account.)
2. **Get one real `order.created` payload** (ask support for a sample, or fire a test order) so we can
   diff the real field names against our assumed shape and adjust the mapping in `_shoopy_order_to_lead`.

## 2. Configure the webhook in Shoopy admin
1. **Webhook URL:** `https://<your-render-app>.onrender.com/webhook/shoopy`
2. **Events:** select **order.created**, **order.updated**, **order.cancelled** (and customer.* if offered).
3. **Auth:** set the **Bearer token** = the value of `SHOOPY_WEBHOOK_TOKEN` in your Render env.
   Shoopy must send `Authorization: Bearer <token>`.
4. **HMAC (if Shoopy supports it):** set a signing secret = `SHOOPY_HMAC_SECRET`. Our endpoint then
   verifies `X-Shoopy-Signature` = HMAC-SHA256 of the **raw body**. If Shoopy can't sign, leave
   `SHOOPY_HMAC_SECRET` empty — Bearer auth still applies.
5. Save.

## 3. How events map to a Lead
| Shoopy event | Effect on CRM Lead |
|--------------|--------------------|
| `order.created` | upsert Lead · `Inbound_Source="Website (Shoopy)"` · name/mobile/email/company/city/GSTIN mapped · `Estimated_Order_Value=amount` · items (name/sku/qty/price) → note + `Product_Interest`/`SKU_Interest` · raw JSON → `Source_Payload` · runs scoring; assignment via `assign_lead.dg` |
| `order.updated` | enrich the same lead (status/tracking appended to note) |
| `order.cancelled` | flag `Pipeline_Stage="Not-Applicable"` + note — **never deletes** |
| `customer.created/updated/deleted` | upsert/flag the lead — **never deletes** |

- **Idempotent** on Shoopy `id` (stored in `Source_Record_Id`). Re-delivery = no duplicate.
- **Dedupe** on mobile (E.164-normalised) then email — same customer across orders = one lead, enriched.
- Returns `200 {"status":"ok", ...}`. Null/epoch-ms/ISO timestamps and null address fields are all tolerated.

## 4. Go-wide procedure (do this, don't skip)
1. Set `SHOOPY_WEBHOOK_TOKEN` (and `SHOOPY_HMAC_SECRET` if used) in Render env. Redeploy.
2. `GET /webhook/shoopy/health` → `{"ok":true,...}`.
3. Fire **ONE** real test order. Check the Render logs (`shoopy_webhook` line shows the raw length) and
   confirm a Lead appeared in CRM with the right fields.
4. **Diff** the real payload against the assumed shape; if a field name differs, tweak
   `_shoopy_order_to_lead` in `app.py` (it's the only mapping point) and redeploy.
5. Then enable the webhook for all orders.

## 5. Security notes
- Endpoint rejects wrong/absent Bearer with **401**; wrong HMAC (when a secret is set) with **401**.
- Verification runs on the **raw** body before JSON parsing.
- Secrets are env-only; never commit them.

---
**Linked:** [../PHASE1_EXTENSION_NOTES.md](../PHASE1_EXTENSION_NOTES.md) · [RUNBOOK_WhatsApp_NativeChannel.md](RUNBOOK_WhatsApp_NativeChannel.md)
