"""
tailortalk.py — TailorTalk WhatsApp AI agent -> Zoho CRM, as a mountable FastAPI router.

TailorTalk fires an outbound webhook on lead events (First Message / On Warm / On Hot /
On Converted / On Escalated / Every Message). This receives it and upserts a CRM Lead via
the shared leads.upsert_lead path (dedup on mobile/email + K24_Lead_Score + idempotency),
exactly like Shoopy / IndiaMART.

⚠️ SCHEMA NOTE: TailorTalk's guide (tailortalk.ai/guide/webhook/webhook) documents the
setup + triggers but NOT the exact JSON field names. So the mapper is intentionally
tolerant (tries many aliases) AND logs the raw payload. After you hit "View Sample
Response" / "Test Trigger" in TailorTalk, share the real JSON and we tighten field names
here (the _first(...) alias lists).

Wire into app.py:
    from tailortalk import router as tailortalk_router
    app.include_router(tailortalk_router)

Configure in TailorTalk: Agent page -> Webhook -> URL
  https://<render-app>.onrender.com/webhook/tailortalk   (append ?key=<secret> if
  TAILORTALK_WEBHOOK_KEY is set). Pick triggers: at minimum "First Message"; add
  "On Warm/Hot/Converted" to keep the CRM lead's status fresh.

Env: TAILORTALK_WEBHOOK_KEY (optional shared secret).
"""
from __future__ import annotations

import hmac
import os
from typing import Any

import structlog
from fastapi import APIRouter, HTTPException, Query, Request, Response

import leads as leadsvc
from zoho_client import ZohoClient

log = structlog.get_logger("tailortalk")
router = APIRouter(tags=["tailortalk"])

WEBHOOK_KEY = os.getenv("TAILORTALK_WEBHOOK_KEY", "").strip()
INBOUND_SOURCE = "WhatsApp"  # exact live Inbound_Source picklist value (verified)

# TailorTalk trigger -> optional Pipeline_Stage nudge. Unknown/most events keep "New"
# (never auto-advance on an event we can't fully trust). "converted" is the clear one.
STAGE_BY_EVENT = {
    "on converted": "Deal",
    "converted": "Deal",
}


def _first(d: dict[str, Any], *keys: str) -> Any:
    """First non-empty value among keys/paths (TailorTalk field names are undocumented,
    so we try many). Supports one level of nesting via 'a.b'."""
    for k in keys:
        if "." in k:
            a, b = k.split(".", 1)
            v = (d.get(a) or {}) if isinstance(d.get(a), dict) else {}
            v = v.get(b) if isinstance(v, dict) else None
        else:
            v = d.get(k)
        if v not in (None, "", [], {}):
            return v
    return None


def tailortalk_to_lead_kwargs(p: dict[str, Any]) -> dict[str, Any]:
    """Map a TailorTalk webhook payload to leads.upsert_lead kwargs. Tolerant of unknown
    field names; refine the alias lists once the real sample response is known."""
    name = _first(p, "name", "lead_name", "contact_name", "customer_name", "sender_name",
                  "full_name", "user_name", "contact.name", "lead.name") or "WhatsApp Lead"
    parts = str(name).strip().split(" ", 1)
    first = parts[0] if len(parts) > 1 else None
    last = parts[1] if len(parts) > 1 else parts[0]

    phone = _first(p, "phone", "mobile", "whatsapp", "whatsapp_number", "wa_id", "from",
                   "phone_number", "contact_number", "msisdn", "contact.phone", "lead.phone")
    email = _first(p, "email", "email_address", "contact.email")
    message = _first(p, "message", "text", "body", "query", "last_message", "content",
                     "message_body", "user_message") or ""
    event = str(_first(p, "event", "trigger", "type", "event_type", "status", "stage") or "message")
    city = _first(p, "city", "location", "contact.city")

    note = f"TailorTalk WhatsApp ({event}): {message}".strip()
    kw = {
        "external_id": (lambda i: f"TT:{i}" if i else None)(
            _first(p, "id", "lead_id", "conversation_id", "session_id", "contact_id", "wa_id")),
        "first_name": first,
        "last_name": last,
        "mobile": phone,
        "email": email,
        "city": city,
        "note": note,
    }
    stage = STAGE_BY_EVENT.get(event.strip().lower())
    if stage:
        kw["stage"] = stage
    return kw


@router.post("/webhook/tailortalk")
async def tailortalk_webhook(request: Request, key: str | None = Query(default=None)) -> dict[str, Any]:
    if WEBHOOK_KEY and not (key and hmac.compare_digest(key, WEBHOOK_KEY)):
        raise HTTPException(status_code=401, detail="invalid or missing key")

    raw = await request.body()
    try:
        body: Any = await request.json()
    except Exception:
        form = await request.form()
        body = dict(form)

    # log the raw payload — this is how we discover TailorTalk's real field names
    log.info("tailortalk_webhook", raw=(raw[:2000].decode("utf-8", "replace") if raw else ""))

    # payload may be a single object or {"data": {...}} / {"lead": {...}}
    obj = body
    if isinstance(body, dict):
        obj = body.get("data") or body.get("lead") or body.get("payload") or body
    if not isinstance(obj, dict):
        return {"status": "ignored", "reason": "non-object payload"}

    kw = tailortalk_to_lead_kwargs(obj)
    if not (kw.get("mobile") or kw.get("email")):
        # no contact key yet — accept (don't error the webhook) but flag for schema fix
        log.warning("tailortalk_no_contact_key", keys=list(obj.keys()))
        return {"status": "ok", "note": "no phone/email found — check field mapping vs sample response"}

    z = ZohoClient()
    async with z:
        result = await leadsvc.upsert_lead(z, inbound_source=INBOUND_SOURCE, raw_payload=obj, **kw)
    return {"status": "ok", **result}


@router.get("/webhook/tailortalk")
async def tailortalk_verify(request: Request) -> Response:
    """Some providers GET the URL to verify it / echo a challenge. Echo it defensively."""
    challenge = request.query_params.get("challenge") or request.query_params.get("hub.challenge")
    return Response(content=challenge or "ok", media_type="text/plain")


@router.get("/tailortalk/health")
async def tailortalk_health() -> dict[str, Any]:
    return {"status": "ok", "webhook_key_set": bool(WEBHOOK_KEY), "inbound_source": INBOUND_SOURCE}
