# RUNBOOK — WhatsApp → Zoho CRM Leads (Source 3)

**Audience:** Zoho CRM admin + whoever holds the WhatsApp number
**Preferred path:** Zoho's **native WhatsApp channel** (no code). **Fallback:** the Cloud-API webhook
already built in `app.py` (`/webhook/whatsapp`). Build the fallback only if the native channel can't fit.

K24 number **+91 88601 11090** · WABA **789633577087490** · Phone Number ID **986442791212513**.

---

## STEP 0 — Decide how the number is held (do NOT assume)
A WhatsApp number is either on a **BSP** *or* **Meta Cloud-API direct** — **not both**. Find out which:
- **On a BSP** (e.g. TailorTalk and similar) → use the **Zoho native channel** with the BSP's creds, or
  have the BSP push inbound messages to us. (You cannot also connect it Cloud-API-direct.)
- **Free on Meta Cloud API direct** → connect the **Zoho native channel** with the WABA ID, **or** use
  the **Cloud-API webhook fallback** (§B).

Confirm with whoever set up the number before configuring anything.

---

## A. PREFERRED — Zoho native WhatsApp channel (no code)
1. **Zoho CRM → Setup → Channels → WhatsApp → Get Started / Connect.**
2. Connect via your **BSP** (enter BSP credentials) **or** **Meta** (authorize the WABA `789633577087490`,
   Phone Number ID `986442791212513`).
3. **Inbound from a new number → auto-create Lead:** enable lead auto-creation; set
   **`Inbound_Source` = "WhatsApp"** (via the channel's field-mapping, or a create workflow that stamps it).
4. **Routing:** round-robin / territory (mirrors `assign_lead.dg`).
5. **Out-of-hours auto-reply:** configure an approved WhatsApp **template** message.
6. Done — **no code**. The native channel logs the full conversation on the lead/contact.

> If native lead-creation can't stamp `Inbound_Source`, add a CRM workflow: on Lead create where the
> WhatsApp channel is the origin → Field Update `Inbound_Source="WhatsApp"`.

---

## B. FALLBACK — Cloud API webhook (already coded in `app.py`)
Use only if the native channel can't be used (e.g. number is Cloud-API-direct and you want full control).

1. **Render env:** set `META_APP_SECRET` (Meta App → Settings → Basic → App Secret) and
   `META_VERIFY_TOKEN` (any long random string you choose).
2. **Meta App → WhatsApp → Configuration → Webhooks:**
   - **Callback URL:** `https://<your-render-app>.onrender.com/webhook/whatsapp`
   - **Verify token:** the same `META_VERIFY_TOKEN`. Meta calls `GET /webhook/whatsapp` and our endpoint
     echoes `hub.challenge` when the token matches.
   - **Subscribe** to the **`messages`** field.
3. Inbound flow: `POST /webhook/whatsapp` verifies **`X-Hub-Signature-256`** (HMAC-SHA256 of the raw body
   with `META_APP_SECRET`; 401 on mismatch), parses `entry[].changes[].value.messages[]`, takes the name
   from `value.contacts[].profile.name` and the number from `wa_id`, then upserts a Lead:
   **`Inbound_Source="WhatsApp"`**, first message → note. **Idempotent on message id**, **dedupe on mobile**.
4. Returns `200` within the 5s Meta requires.

> **Phone CALLS are out of scope** — the Meta number cannot receive PSTN calls (confirmed). See
> `PHASE1_EXTENSION_NOTES.md` (needs a cloud-telephony layer — procurement).

---
**Linked:** [../PHASE1_EXTENSION_NOTES.md](../PHASE1_EXTENSION_NOTES.md) · [RUNBOOK_Shoopy_Webhook.md](RUNBOOK_Shoopy_Webhook.md)
