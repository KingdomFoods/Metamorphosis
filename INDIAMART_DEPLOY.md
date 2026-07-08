# IndiaMART → Zoho CRM — approach (REVERSED 2026-07-08: self-hosted webhook)

**Decision history:** first custom webhook → then "use official plugin, skip Render" →
**now back to the self-hosted webhook**, because the plugin failed in practice:
- Plugin/Flow UI **function editor is bugged** (argument panel broken, saves fail).
- Plugin's **connected-app auth expired** → `INVALID_TOKEN` (not fixable via API:
  `/settings/connectedapps` returns `INVALID_REQUEST` — it's a UI/OAuth-consent op).
- API function-create isn't a clean path either: `POST /crm/v8/settings/functions` wants a
  `metadata` param, but exposing a standalone function as an IndiaMART-postable REST URL
  (with zapikey) is a **UI step** — the same bugged UI.

So live capture = **our `/webhook/indiamart`** (in `app.py` via `indiamart.py`, re-mounted).

## Verified working (local, live Zoho, 2026-07-08)
Tested with IndiaMART's exact shape `{"CODE":200,"STATUS":"SUCCESS","RESPONSE":{…}}`:
lead **created with real data** (name split, mobile → `+919811122334`, City, Product,
`Inbound_Source=IndiaMart`, `Pipeline_Stage=New`, `K24_Lead_Score=40`), `Lead_Source`
correctly **unset**, `Source_Record_Id=IM:…`, **idempotent** (re-POST updates, no dupe),
then deleted. Handles `RESPONSE` as a single dict OR a list.

## Why this beats the bare webhook draft
Reuses `leads.upsert_lead` → mobile/email dedup + `K24_Lead_Score` scoring + idempotency
shared with Shoopy/WhatsApp. Only sets fields that exist (the draft's `Subject`,
`Mobile_Alternate`, `Phone_Alternate` aren't standard Lead fields → would `INVALID_DATA`).
Async httpx, not blocking `requests`. No `Lead_Source` (avoids the picklist trap).

## Deploy (YOUR step — I can't push to Render: no creds, dir not git)
1. Deploy the metamorphosis bundle to the existing Render service (the one running
   Shoopy/WhatsApp). Changed files: `app.py` (router include), `indiamart.py`,
   `indiamart_backfill.py`. **No start-command change, no new dependencies.**
2. (recommended) set env `INDIAMART_WEBHOOK_KEY=<secret>` → the push URL must then end
   with `?key=<secret>`. Without it the webhook is open.
3. Verify: `GET https://<app>.onrender.com/indiamart/health` → `{status: ok}`.
4. **IndiaMART Seller Panel → Lead Manager → Push API**: set Webhook Listener URL to
   `https://<app>.onrender.com/webhook/indiamart` (append `?key=<secret>` if set) →
   **Test webhook** button → confirm the lead lands in Zoho → **Activate**.
   *(This is the switch off TeleCRM — confirm with Man Mohan first.)*

## Endpoints
| Method | Path | Use |
|--------|------|-----|
| POST | `/webhook/indiamart` | IndiaMART Push API target (real-time) |
| GET | `/indiamart/pull?hours=2` | backup polling (cron, ≥5 min apart) |
| GET | `/indiamart/health` | key/config check |

## Historical backfill (optional, no Render — local CLI)
`INDIAMART_API_KEY=<pull key> python indiamart_backfill.py --days 365 --dry-run` then
without `--dry-run`. Self-throttles to IndiaMART's ~1-req/5-min limit; idempotent.
