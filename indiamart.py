"""
indiamart.py — IndiaMART Lead Manager -> Zoho CRM, as a mountable FastAPI router.

Reuses leads.upsert_lead (the shared dedupe + scoring + idempotency path every source
uses) instead of re-implementing lead creation — so IndiaMART leads get the same
mobile/email dedupe, K24_Lead_Score oracle, and Source_Record_Id idempotency as
Shoopy/WhatsApp. Async httpx throughout (never blocks the event loop).

Two ingest paths (build both — the runbook wanted redundancy):
  * PUSH  POST /webhook/indiamart   — IndiaMART posts each enquiry in real time.
  * PULL  GET  /indiamart/pull      — pull a recent window on demand / via cron.
Historical backfill is a SEPARATE CLI (indiamart_backfill.py) because IndiaMART's
Pull API throttles to ~1 request / 5 minutes — a multi-window backfill cannot run
inside a single HTTP request without tripping the throttle or the Render timeout.

Wire into app.py:
    from indiamart import router as indiamart_router
    app.include_router(indiamart_router)

Env:
  INDIAMART_API_KEY       (from seller.indiamart.com/leadmanager/crmapi)
  INDIAMART_WEBHOOK_KEY   (optional shared secret; if set, push must send ?key=...)
  INDIAMART_PULL_URL      (default https://mapi.indiamart.com/wservce/enquiry/listing/)
"""
from __future__ import annotations

import hmac
import os
from datetime import datetime, timedelta
from typing import Any

import httpx
import structlog
from fastapi import APIRouter, HTTPException, Query, Request

import leads as leadsvc
from zoho_client import ZohoClient

log = structlog.get_logger("indiamart")
router = APIRouter(tags=["indiamart"])

INDIAMART_API_KEY = os.getenv("INDIAMART_API_KEY", "").strip()
INDIAMART_WEBHOOK_KEY = os.getenv("INDIAMART_WEBHOOK_KEY", "").strip()
# v2 Pull API (the legacy /enquiry/listing/ v1 endpoint 503s "overload"). v2 caps each
# request to a 7-day window and expects date-only start/end.
PULL_URL = os.getenv("INDIAMART_PULL_URL", "https://mapi.indiamart.com/wservce/crm/crmListing/v2/").strip()

INBOUND_SOURCE = "IndiaMart"  # exact live picklist value (verified)
_IM_TIME_FMT = "%d-%b-%Y"  # v2 date format, e.g. 08-Jul-2026 (max 7-day range per call)


def _first(d: dict[str, Any], *keys: str) -> Any:
    """First non-empty value among keys (IndiaMART field names vary by API version)."""
    for k in keys:
        v = d.get(k)
        if v not in (None, ""):
            return v
    return None


def indiamart_to_lead_kwargs(im: dict[str, Any]) -> dict[str, Any]:
    """Map one IndiaMART enquiry to leads.upsert_lead kwargs. Everything optional."""
    name = _first(im, "SENDER_NAME", "SENDERNAME") or "IndiaMART Buyer"
    parts = str(name).strip().split(" ", 1)
    first = parts[0] if len(parts) > 1 else None
    last = parts[1] if len(parts) > 1 else parts[0]

    product = _first(im, "QUERY_PRODUCT_NAME", "PRODUCT_NAME", "QUERY_MCAT_NAME")
    subject = _first(im, "SUBJECT", "QUERY_MESSAGE", "MESSAGE") or ""
    enquiry_id = _first(im, "UNIQUE_QUERY_ID", "QUERY_ID", "ENQ_ID")
    when = _first(im, "QUERY_TIME", "DATE_TIME", "ENQ_DATE") or ""

    note = (
        f"IndiaMART enquiry {enquiry_id or '?'} ({_first(im, 'QUERY_TYPE') or 'enquiry'}) "
        f"@ {when}: {subject}"
        + (f" | product: {product}" if product else "")
    )
    return {
        "external_id": f"IM:{enquiry_id}" if enquiry_id else None,
        "first_name": first,
        "last_name": last,
        "company": _first(im, "SENDER_COMPANY", "COMPANY_NAME", "SENDER_COMPANY_NAME"),
        # dedupe key — leadsvc normalises to +91…; alt mobile kept only in the note/payload
        "mobile": _first(im, "SENDER_MOBILE", "MOB", "MOBILE", "SENDER_MOBILE_ALT", "ALT_MOB"),
        "email": _first(im, "SENDER_EMAIL", "EMAIL", "SENDER_EMAIL_ALT"),
        "city": _first(im, "SENDER_CITY", "CITY"),
        "product_interest": str(product)[:255] if product else None,
        "note": note,
    }


async def _ingest_one(z: ZohoClient, im: dict[str, Any]) -> dict[str, Any]:
    kw = indiamart_to_lead_kwargs(im)
    result = await leadsvc.upsert_lead(z, inbound_source=INBOUND_SOURCE, raw_payload=im, **kw)
    return {"enquiry_id": _first(im, "UNIQUE_QUERY_ID", "QUERY_ID"), **result}


def _extract_leads(body: Any) -> list[dict[str, Any]]:
    """IndiaMART push shapes: a single enquiry dict, a bare list, or {'RESPONSE': [...]}."""
    if isinstance(body, list):
        return [x for x in body if isinstance(x, dict)]
    if isinstance(body, dict):
        resp = body.get("RESPONSE")
        if isinstance(resp, list):
            return [x for x in resp if isinstance(x, dict)]
        if isinstance(resp, dict):
            return [resp]
        return [body]
    return []


# ─── PUSH: real-time webhook ────────────────────────────────────────────────
@router.post("/webhook/indiamart")
async def indiamart_webhook(request: Request, key: str | None = Query(default=None)) -> dict[str, Any]:
    """IndiaMART Push API target. If INDIAMART_WEBHOOK_KEY is set, the URL must carry
    ?key=<secret> (IndiaMART lets you bake a token into the push URL)."""
    if INDIAMART_WEBHOOK_KEY and not (key and hmac.compare_digest(key, INDIAMART_WEBHOOK_KEY)):
        raise HTTPException(status_code=401, detail="invalid or missing key")

    raw = await request.body()
    try:
        body: Any = await request.json()
    except Exception:  # IndiaMART sometimes posts form-encoded
        form = await request.form()
        body = dict(form)

    leads_in = _extract_leads(body)
    log.info("indiamart_push", count=len(leads_in), raw_len=len(raw))
    if not leads_in:
        return {"status": "ok", "processed": 0, "results": []}

    z = ZohoClient()
    async with z:
        results = []
        for im in leads_in:
            try:
                results.append(await _ingest_one(z, im))
            except Exception as exc:  # one bad enquiry must not drop the batch
                log.error("indiamart_push_lead_failed", error=str(exc))
                results.append({"enquiry_id": _first(im, "UNIQUE_QUERY_ID"), "action": "error", "error": str(exc)})
    return {"status": "ok", "processed": len(results), "results": results}


# ─── PULL: fetch a recent window on demand / via cron ───────────────────────
async def fetch_window(start: datetime, end: datetime) -> tuple[int, list[dict[str, Any]]]:
    """Call IndiaMART Pull API for [start, end]. Returns (code, leads). Raises on transport error.
    NOTE: IndiaMART throttles to ~1 request / 5 minutes per key — space calls accordingly."""
    if not INDIAMART_API_KEY:
        raise HTTPException(status_code=503, detail="INDIAMART_API_KEY not configured")
    params = {
        "glusr_crm_key": INDIAMART_API_KEY,
        "start_time": start.strftime(_IM_TIME_FMT),
        "end_time": end.strftime(_IM_TIME_FMT),
    }
    async with httpx.AsyncClient(timeout=60.0) as http:
        resp = await http.get(PULL_URL, params=params)
    data = resp.json() if resp.content else {}
    code = int(data.get("CODE", resp.status_code) or 0)
    if code != 200:
        # 429 / "too many requests" => the 5-min throttle; surface it, don't treat as empty
        log.warning("indiamart_pull_nonok", code=code, message=data.get("MESSAGE"))
        return code, []
    leads = data.get("RESPONSE") or []
    return code, [x for x in leads if isinstance(x, dict)]


@router.get("/indiamart/pull")
async def indiamart_pull(hours: int = Query(default=2, ge=1, le=168)) -> dict[str, Any]:
    """Pull enquiries from the last `hours` (default 2). Run via cron every few hours.
    IndiaMART's 5-minute throttle means: don't call this more than ~once per 5 min."""
    end = datetime.now()
    start = end - timedelta(hours=hours)
    code, leads = await fetch_window(start, end)
    if code == 429:
        raise HTTPException(status_code=429, detail="IndiaMART throttle (max 1 request / 5 min) — retry later")
    if code != 200:
        raise HTTPException(status_code=502, detail=f"IndiaMART returned CODE {code}")

    z = ZohoClient()
    async with z:
        results = []
        for im in leads:
            try:
                results.append(await _ingest_one(z, im))
            except Exception as exc:
                results.append({"enquiry_id": _first(im, "UNIQUE_QUERY_ID"), "action": "error", "error": str(exc)})
    created = sum(1 for r in results if r.get("action") == "created")
    log.info("indiamart_pull_done", hours=hours, fetched=len(leads), created=created)
    return {"status": "ok", "window_hours": hours, "fetched": len(leads), "created": created, "results": results}


@router.get("/indiamart/health")
async def indiamart_health() -> dict[str, Any]:
    return {
        "status": "ok",
        "api_key_set": bool(INDIAMART_API_KEY),
        "webhook_key_set": bool(INDIAMART_WEBHOOK_KEY),
        "inbound_source": INBOUND_SOURCE,
    }
