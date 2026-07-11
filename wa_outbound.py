"""wa_outbound.py — send business-initiated WhatsApp messages via the Meta Cloud API.

Business-initiated messages (messaging a lead who hasn't messaged us) are only allowed as
**pre-approved template messages** — free-form text to a cold number is blocked by WhatsApp and
risks a number ban. This module sends templates (and, for the 24h service window, free text).

Shared by indiamart.py (auto-welcome on new lead) and app.py. Guarded: if the WhatsApp env isn't
set, configured() is False and every send is a no-op that returns {"ok": False, "error": "..."}.
Never raises — callers stay best-effort so a messaging failure can't break lead ingestion.

Env:
    WHATSAPP_TOKEN            permanent System-User token (whatsapp_business_messaging)
    WHATSAPP_PHONE_NUMBER_ID  sender phone-number id
    WHATSAPP_GRAPH_VERSION    Graph API version (default v21.0)
"""
from __future__ import annotations

import os
import re

import httpx
import structlog

log = structlog.get_logger("wa_outbound")

TOKEN = os.getenv("WHATSAPP_TOKEN", "").strip()
PHONE_ID = os.getenv("WHATSAPP_PHONE_NUMBER_ID", "").strip()
GRAPH_VERSION = os.getenv("WHATSAPP_GRAPH_VERSION", "v21.0").strip()


def configured() -> bool:
    """True when both credentials are present, so sends can actually go out."""
    return bool(TOKEN and PHONE_ID)


def normalize_msisdn(raw: str | None) -> str | None:
    """Normalise an Indian mobile to Graph-API form (digits, country code, no '+').
    Returns None if it can't be made into a plausible number."""
    digits = re.sub(r"\D", "", raw or "")
    if not digits:
        return None
    if len(digits) == 10:                       # bare 10-digit -> prefix 91
        digits = "91" + digits
    elif len(digits) == 11 and digits[0] == "0":  # 0XXXXXXXXXX -> 91XXXXXXXXXX
        digits = "91" + digits[1:]
    if not (11 <= len(digits) <= 15):
        return None
    return digits


async def _post(payload: dict) -> dict:
    url = f"https://graph.facebook.com/{GRAPH_VERSION}/{PHONE_ID}/messages"
    try:
        async with httpx.AsyncClient(timeout=30.0) as c:
            r = await c.post(url, headers={"Authorization": f"Bearer {TOKEN}"}, json=payload)
        if r.status_code >= 300:
            log.error("wa_out_failed", to=payload.get("to"), status=r.status_code, body=r.text[:300])
            return {"ok": False, "to": payload.get("to"), "error": f"HTTP {r.status_code}: {r.text[:200]}"}
        wamid = (((r.json().get("messages") or [{}])[0]) or {}).get("id")
        log.info("wa_out_ok", to=payload.get("to"), type=payload.get("type"), wamid=wamid)
        return {"ok": True, "to": payload.get("to"), "wamid": wamid}
    except Exception as exc:  # noqa: BLE001
        log.error("wa_out_error", to=payload.get("to"), error=str(exc))
        return {"ok": False, "to": payload.get("to"), "error": str(exc)}


async def send_template(to: str, template: str, lang: str = "en",
                        body_params: list[str] | None = None) -> dict:
    """Send an approved template message. body_params fill {{1}}, {{2}}, … in the template body.
    Returns {"ok": bool, "to", "wamid"|"error"}."""
    if not configured():
        return {"ok": False, "error": "WhatsApp not configured (WHATSAPP_TOKEN/PHONE_NUMBER_ID)"}
    if not template:
        return {"ok": False, "error": "no template name"}
    msisdn = normalize_msisdn(to)
    if not msisdn:
        return {"ok": False, "error": f"unusable phone: {to!r}"}
    components = []
    if body_params:
        components = [{"type": "body",
                       "parameters": [{"type": "text", "text": str(p)[:1024]} for p in body_params]}]
    return await _post({
        "messaging_product": "whatsapp",
        "to": msisdn,
        "type": "template",
        "template": {"name": template, "language": {"code": lang}, "components": components},
    })


async def send_text(to: str, text: str) -> dict:
    """Free-form text — ONLY valid inside a 24h window opened by the buyer messaging first."""
    if not configured():
        return {"ok": False, "error": "WhatsApp not configured"}
    msisdn = normalize_msisdn(to)
    if not msisdn:
        return {"ok": False, "error": f"unusable phone: {to!r}"}
    return await _post({
        "messaging_product": "whatsapp",
        "to": msisdn,
        "type": "text",
        "text": {"body": text[:4096]},
    })
