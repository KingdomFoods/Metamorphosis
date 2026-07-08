# RUNBOOK — IndiaMART → Zoho CRM (Source 2, official plugin — NO custom code)

**Audience:** Zoho CRM admin + IndiaMART seller-panel admin
**Why no code:** IndiaMART has an **official Zoho Marketplace plugin** (real-time Push API). Building a
custom integration would be redundant and unsupported. This is **config only**.

---

## 1. Pre-check: edition
- The real-time extension requires **Zoho CRM Enterprise edition or above**. The paid Zoho One plan
  should qualify — **confirm in CRM** (Setup → Subscription / your plan). If on a lower edition, use the
  Pull-API fallback (§4).

## 2. Install the extension
1. **Zoho Marketplace** → search **"IndiaMART Official Real-Time Leads Extension for Zoho CRM"** → **Install**.
2. Choose users/profiles that can see IndiaMART leads. Authorize.
3. The extension exposes a **webhook listener URL** (Push API endpoint) inside the IndiaMART setup screen
   — copy it for the next step.

## 3. Activate Push API in the IndiaMART seller panel
1. IndiaMART **Seller Panel → Lead Manager → Import/Export Leads (CRM/API) → Push API**.
2. Select **Zoho CRM** / paste the plugin's **webhook listener URL**.
3. **Confirm via OTP** (sent to the registered IndiaMART mobile/email).
4. **Activate.** Leads now flow in real time: buy-leads, enquiries, and **PNS (call) enquiries** all
   arrive tagged source **"IndiaMART"**.

## 4. Fallback: Pull API (if Push unavailable on the plan)
- The plugin's **Pull API** mode fetches leads on a schedule. Zoho's minimum poll is **~every 2 hours**.
- Same setup screen → choose **Pull** → provide the IndiaMART **CRM key** (Seller Panel → Lead Manager →
  API key). Leads land the same way, just batched.

## 5. ONE-TIME uniformity workflow (the only "code" consideration)
The plugin writes its source into the **standard `Lead_Source`** field (value "IndiaMART"), **not** our
`Inbound_Source`. So all sources stay uniform, add a CRM workflow to copy it:

**Setup → Automation → Workflow Rules → + Create Rule → "Unify IndiaMART Source"**, Module **Leads**:
- **When:** Create.
- **Condition:** `Lead Source` = `IndiaMART` (and/or record created by the IndiaMART extension user).
- **Instant action → Field Update:** set `Inbound_Source` = **"IndiaMart"** (this value already exists in
  the picklist).
- Save. Now IndiaMART leads are scored/assigned/deduped identically to Shoopy & WhatsApp leads (the
  `score_lead.dg` / `assign_lead.dg` workflows fire on create regardless of source).

> Dedupe note: Zoho's standard duplicate-check on Email/Phone, plus our `assign_lead`/`score_lead`
> workflows, handle IndiaMART leads the same as coded sources. No extra dedupe code needed.

---
**Linked:** [RUNBOOK_Shoopy_Webhook.md](RUNBOOK_Shoopy_Webhook.md) · [../README.md](../README.md)
