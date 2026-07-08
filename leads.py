"""
leads.py — shared CRM Lead upsert for the lead-source integrations (Shoopy, WhatsApp, …).

ONE place for the dedupe + idempotency + scoring discipline every source reuses:
  - idempotency on the source's external id (stored in `Source_Record_Id`)
  - dedupe on mobile (E.164-normalised) then email — same customer => one lead, enriched not duplicated
  - score via the existing crm_setup.score_lead oracle (mirrors score_lead.dg); assignment is left to
    the server-side assign_lead.dg workflow (we never call /crm/v6/users — scope-blocked by design)
  - never hard-deletes; cancellations/deletes flag the lead via a note + Pipeline_Stage

LEADS ONLY — this module never touches invoices or stock.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any

import structlog

from crm_setup import score_label, score_lead
from zoho_client import ZohoClient

log = structlog.get_logger("leads")

MODULE = "Leads"
_PAYLOAD_MAX = 32000  # Source_Payload is a 'large' textarea; keep well under the limit

# In-process dedupe cache: maps external_id / mobile / email -> lead_id. Zoho's search index
# lags a few seconds after a write, so two webhooks arriving in quick succession (or a test) can
# race and create duplicates. This cache makes dedupe deterministic within the process; the CRM
# /search below is the cross-process / post-restart backstop. (Render runs a single worker.)
_idx_external: dict[str, str] = {}
_idx_mobile: dict[str, str] = {}
_idx_email: dict[str, str] = {}


def _cache_put(lead_id: str, external_id: str | None, mobile: str | None, email: str | None) -> None:
    if external_id:
        _idx_external[external_id] = lead_id
    if mobile:
        _idx_mobile[mobile] = lead_id
    if email:
        _idx_email[email] = lead_id


def _cache_clear() -> None:  # test helper
    _idx_external.clear()
    _idx_mobile.clear()
    _idx_email.clear()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def normalize_mobile(raw: Any) -> str | None:
    """Canonicalise to +<cc><number>. Indian 10-digit numbers -> +91XXXXXXXXXX.

    Handles '9876543210', '+91 98765-43210', '0919876543210', 919876543210 (int), None.
    Returns None if there aren't enough digits to be a phone number.
    """
    if raw is None:
        return None
    digits = re.sub(r"\D", "", str(raw))
    if not digits:
        return None
    digits = digits.lstrip("0")              # drop trunk/leading zeros
    if len(digits) == 10:                    # bare Indian mobile
        return "+91" + digits
    if len(digits) == 12 and digits.startswith("91"):
        return "+" + digits
    if len(digits) < 7:
        return None
    return "+" + digits


def _criteria(field: str, value: str) -> str:
    # CRM search criteria; escape parentheses-breaking chars defensively
    safe = value.replace("(", "").replace(")", "")
    return f"({field}:equals:{safe})"


async def _search(z: ZohoClient, criteria: str) -> dict[str, Any] | None:
    try:
        resp = await z.get(z.crm(f"/{MODULE}/search"), params={"criteria": criteria}, with_org=False)
    except Exception as exc:  # search returns 204/empty -> treat as no match
        log.debug("lead_search_miss", criteria=criteria, error=str(exc))
        return None
    data = resp.get("data") or []
    return data[0] if data else None


async def _get_by_id(z: ZohoClient, lead_id: str) -> dict[str, Any] | None:
    try:
        resp = await z.get(z.crm(f"/{MODULE}/{lead_id}"), with_org=False)
        data = resp.get("data") or []
        return data[0] if data else None
    except Exception:
        return None


async def find_lead(z: ZohoClient, *, external_id: str | None, mobile: str | None, email: str | None) -> tuple[dict[str, Any] | None, str]:
    """Return (lead_or_none, matched_by). Priority: external id -> mobile -> email.

    Checks the in-process cache first (beats Zoho's search-index lag), then CRM /search.
    """
    # fast path — process cache
    for key, store, label in ((external_id, _idx_external, "external_id"), (mobile, _idx_mobile, "mobile"), (email, _idx_email, "email")):
        if key and key in store:
            rec = await _get_by_id(z, store[key])
            if rec:
                return rec, label
            store.pop(key, None)  # stale (deleted) — drop it
    # backstop — CRM search
    if external_id:
        hit = await _search(z, _criteria("Source_Record_Id", external_id))
        if hit:
            return hit, "external_id"
    if mobile:
        hit = await _search(z, f"(({_criteria('Mobile', mobile)[1:-1]})or({_criteria('Phone', mobile)[1:-1]}))")
        if hit:
            return hit, "mobile"
    if email:
        hit = await _search(z, _criteria("Email", email))
        if hit:
            return hit, "email"
    return None, "none"


def _truncate_payload(raw: Any) -> str:
    try:
        s = json.dumps(raw, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        s = str(raw)
    return s[:_PAYLOAD_MAX]


async def upsert_lead(
    z: ZohoClient,
    *,
    inbound_source: str,
    external_id: str | None,
    first_name: str | None,
    last_name: str,
    company: str | None = None,
    mobile: str | None = None,
    email: str | None = None,
    city: str | None = None,
    gstin: str | None = None,
    est_value: float | None = None,
    product_interest: str | None = None,
    sku_interest: str | None = None,
    note: str | None = None,
    raw_payload: Any | None = None,
    stage: str | None = None,
) -> dict[str, Any]:
    """Create or enrich a Lead. Idempotent on external_id, dedupes on mobile then email.

    Returns {'action': 'created'|'updated'|'noop', 'lead_id': str, 'matched_by': str, 'score': int}.
    """
    mobile = normalize_mobile(mobile)
    email = (email or "").strip().lower() or None

    existing, matched_by = await find_lead(z, external_id=external_id, mobile=mobile, email=email)

    # Build the score from the same oracle the Deluge uses.
    scored = score_lead({
        "Business_Type": "",
        "Estimated_Order_Value": est_value or 0,
        "City": city or "",
        "Phone": mobile or "",
        "Email": email or "",
        "Product_Interest": product_interest or "",
        "Company": company or "",
    })

    stamp = f"[{now_iso()}] {inbound_source}: {note or 'lead event'}"

    if existing:
        lead_id = existing["id"]
        # idempotency: same external id already processed -> only append the note if new
        prior_desc = existing.get("Description") or ""
        if external_id and matched_by == "external_id" and note and stamp.split('] ', 1)[1] in prior_desc:
            log.info("lead_noop_duplicate", lead_id=lead_id, external_id=external_id)
            return {"action": "noop", "lead_id": lead_id, "matched_by": matched_by, "score": int(existing.get("K24_Lead_Score") or 0)}

        upd: dict[str, Any] = {"Description": (stamp + "\n" + prior_desc)[:60000]}
        # enrich only empty fields (don't clobber rep edits), but always refresh source + payload + score
        upd["Inbound_Source"] = inbound_source
        if external_id:
            upd["Source_Record_Id"] = external_id
        if raw_payload is not None:
            upd["Source_Payload"] = _truncate_payload(raw_payload)
        if est_value:
            upd["Estimated_Order_Value"] = est_value
        if city and not existing.get("City"):
            upd["City"] = city
        if product_interest and not existing.get("Product_Interest"):
            upd["Product_Interest"] = product_interest
        if sku_interest and not existing.get("SKU_Interest"):
            upd["SKU_Interest"] = sku_interest
        if mobile and not existing.get("Mobile"):
            upd["Mobile"] = mobile
        if email and not existing.get("Email"):
            upd["Email"] = email
        if stage:
            upd["Pipeline_Stage"] = stage
        upd["K24_Lead_Score"] = scored["score"]
        await z.put(z.crm(f"/{MODULE}/{lead_id}"), json={"data": [upd]}, with_org=False)
        _cache_put(lead_id, external_id, mobile, email)
        log.info("lead_updated", lead_id=lead_id, matched_by=matched_by, source=inbound_source, score=scored["score"])
        return {"action": "updated", "lead_id": lead_id, "matched_by": matched_by, "score": scored["score"]}

    # create
    payload: dict[str, Any] = {
        "Last_Name": last_name or "Lead",
        "Inbound_Source": inbound_source,
        "Pipeline_Stage": stage or "New",
        "K24_Lead_Score": scored["score"],
        "Description": stamp,
    }
    if first_name:
        payload["First_Name"] = first_name[:40]
    if company:
        payload["Company"] = company
    if mobile:
        payload["Mobile"] = mobile
        payload["Phone"] = mobile
    if email:
        payload["Email"] = email
    if city:
        payload["City"] = city
    if est_value:
        payload["Estimated_Order_Value"] = est_value
    if product_interest:
        payload["Product_Interest"] = product_interest
    if sku_interest:
        payload["SKU_Interest"] = sku_interest
    if external_id:
        payload["Source_Record_Id"] = external_id
    if raw_payload is not None:
        payload["Source_Payload"] = _truncate_payload(raw_payload)

    resp = await z.post(z.crm(f"/{MODULE}"), json={"data": [payload]}, with_org=False)
    rec = (resp.get("data") or [{}])[0]
    if rec.get("code") != "SUCCESS":
        raise RuntimeError(f"lead create failed: {rec}")
    lead_id = rec["details"]["id"]
    _cache_put(lead_id, external_id, mobile, email)
    log.info("lead_created", lead_id=lead_id, source=inbound_source, score=scored["score"], label=score_label(scored["score"]))
    return {"action": "created", "lead_id": lead_id, "matched_by": "none", "score": scored["score"]}
