"""
tailortalk.py — TailorTalk WhatsApp AI agent -> Zoho CRM, as a mountable FastAPI router.

TailorTalk fires an outbound webhook on lead events (First Message / On Warm / On Hot /
On Converted / On Escalated / Every Message). This receives it and upserts a CRM Lead via
the shared leads.upsert_lead path (dedup on mobile + K24_Lead_Score + idempotency), like
Shoopy / IndiaMART.

Field mapping locked to TailorTalk's REAL payload (confirmed 2026-07-09):
    { "webhook_trigger": "...", "event_type": "lead", "data": {
        "lead_name": "...", "lead_contact": "<phone>", "lead_status": "cold|warm|hot",
        "buyer_type": "...", "product_category": "...", "city": "...",
        "chat_summary": "...", "lead_source": "whatsapp_ad", "id": "...",
        "ad_data": {...}, "escalated": bool, ... } }
Note: TailorTalk leads have NO email -> dedup is on mobile (lead_contact) only.

Wire into app.py:  from tailortalk import router as tailortalk_router; app.include_router(tailortalk_router)
Configure in TailorTalk: Agent -> Webhook -> URL
  https://<render-app>.onrender.com/webhook/tailortalk?key=<TAILORTALK_WEBHOOK_KEY>

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
INBOUND_SOURCE = "WhatsApp"  # exact live Inbound_Source picklist value

# buyer_type -> Business_Type (a PICKLIST: Hotel/Restaurant/Cloud Kitchen/Caterer/QSR/
# Distributor/Institutional). Unknown values are dropped (never sent) to avoid INVALID_DATA.
_VALID_BUYER = {"Hotel", "Restaurant", "Cloud Kitchen", "Caterer", "QSR", "Distributor", "Institutional"}
_BUYER_ALIASES = {
    "hotel": "Hotel", "restaurant": "Restaurant", "cafe": "Restaurant",
    "cloud kitchen": "Cloud Kitchen", "cloudkitchen": "Cloud Kitchen", "dark kitchen": "Cloud Kitchen",
    "caterer": "Caterer", "catering": "Caterer", "qsr": "QSR", "quick service restaurant": "QSR",
    "distributor": "Distributor", "distribution": "Distributor", "wholesaler": "Distributor",
    "institutional": "Institutional", "institution": "Institutional",
}


def _norm_buyer(v: Any) -> str | None:
    if not v:
        return None
    s = str(v).strip()
    return s if s in _VALID_BUYER else _BUYER_ALIASES.get(s.lower())


# TailorTalk trigger -> optional Pipeline_Stage nudge. Only "converted" is unambiguous.
_STAGE_BY_EVENT = {"on converted": "Deal", "converted": "Deal", "on_converted": "Deal"}


def tailortalk_to_lead_kwargs(body: dict[str, Any]) -> dict[str, Any]:
    """Map a full TailorTalk webhook body to leads.upsert_lead kwargs."""
    d = body.get("data") if isinstance(body.get("data"), dict) else body
    event = str(body.get("webhook_trigger") or body.get("event_type") or "").strip()

    name = str(d.get("lead_name") or "WhatsApp Lead").strip()
    parts = name.split(" ", 1)
    first = parts[0] if len(parts) > 1 else None
    last = parts[1] if len(parts) > 1 else parts[0]

    # rich note — everything TailorTalk gives that isn't a first-class field
    bits = []
    if d.get("chat_summary"):
        bits.append("Summary: " + str(d["chat_summary"]))
    for label, key in (("Buyer", "buyer_type"), ("Status", "lead_status"), ("Supply", "supply_mode"),
                       ("Qty", "quantity"), ("Timeline", "timeline"), ("Purchase", "purchase_type"),
                       ("Use", "use_case"), ("Followups", "total_followups")):
        v = d.get(key)
        if v not in (None, "", 0):
            bits.append(f"{label}: {v}")
    if d.get("lead_source"):
        bits.append("Src: " + str(d["lead_source"]))
    ad = d.get("ad_data") or {}
    if isinstance(ad, dict) and ad.get("title"):
        bits.append("Ad: " + str(ad["title"]))
    if d.get("escalated"):
        bits.append("ESCALATED")
    note = ("TailorTalk WhatsApp" + (f" [{event}]" if event else "") + " | " + " | ".join(bits)).strip(" |")

    kw: dict[str, Any] = {
        "external_id": (lambda i: f"TT:{i}" if i else None)(d.get("id") or d.get("lead_id")),
        "first_name": first,
        "last_name": last,
        "mobile": d.get("lead_contact") or d.get("phone") or d.get("mobile"),  # leadsvc normalises
        "city": d.get("city"),
        "product_interest": (str(d["product_category"])[:255] if d.get("product_category") else None),
        "business_type": _norm_buyer(d.get("buyer_type")),
        "note": note,
    }
    st = _STAGE_BY_EVENT.get(event.lower())
    if st:
        kw["stage"] = st
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

    log.info("tailortalk_webhook", raw=(raw[:2000].decode("utf-8", "replace") if raw else ""))
    if not isinstance(body, dict):
        return {"status": "ignored", "reason": "non-object payload"}

    kw = tailortalk_to_lead_kwargs(body)
    if not kw.get("mobile"):
        log.warning("tailortalk_no_contact", data_keys=list((body.get("data") or body).keys()))
        return {"status": "ok", "note": "no lead_contact/phone in payload — nothing to create"}

    z = ZohoClient()
    async with z:
        result = await leadsvc.upsert_lead(z, inbound_source=INBOUND_SOURCE, raw_payload=body, **kw)
    log.info("tailortalk_lead", action=result.get("action"), lead_id=result.get("lead_id"), score=result.get("score"))
    return {"status": "ok", **result}


@router.get("/webhook/tailortalk")
async def tailortalk_verify(request: Request) -> Response:
    """Some providers GET the URL to verify / echo a challenge. Echo it defensively."""
    challenge = request.query_params.get("challenge") or request.query_params.get("hub.challenge")
    return Response(content=challenge or "ok", media_type="text/plain")


@router.get("/tailortalk/health")
async def tailortalk_health() -> dict[str, Any]:
    return {"status": "ok", "webhook_key_set": bool(WEBHOOK_KEY), "inbound_source": INBOUND_SOURCE}
