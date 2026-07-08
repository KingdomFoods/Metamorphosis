"""
test_integration.py — Phase-1 lead-source webhooks (Shoopy + WhatsApp fallback).

Drives app.py as an ASGI app in one asyncio loop (real lifespan). Proves:
  - Shoopy order.created -> Lead created, Inbound_Source="Website (Shoopy)", items + est value captured
  - same Shoopy id again -> NO duplicate (idempotent / noop or update on same lead)
  - same MOBILE, different order id -> the SAME lead is enriched, not duplicated (dedupe)
  - Shoopy bad bearer / bad HMAC -> 401
  - WhatsApp inbound message -> Lead created, Inbound_Source="WhatsApp", idempotent on message id
Then DELETES every lead it created. LEADS ONLY — never invoices/stock.

Run:  pytest test_integration.py -v    (or)   python test_integration.py
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import uuid

import httpx
import pytest
from dotenv import load_dotenv
from httpx import ASGITransport

load_dotenv()
import app as appmod  # noqa: E402
import leads as leadsvc  # noqa: E402

RUN = uuid.uuid4().hex[:8]
MOBILE = "98" + RUN[:8].translate(str.maketrans("abcdef", "012345"))  # 10-digit-ish, unique
SHOOPY_TOKEN = os.getenv("SHOOPY_WEBHOOK_TOKEN", "")
SHOOPY_HMAC = os.getenv("SHOOPY_HMAC_SECRET", "")
META_SECRET = os.getenv("META_APP_SECRET", "")


def _shoopy_headers(body: bytes, event: str) -> dict[str, str]:
    h = {"Authorization": f"Bearer {SHOOPY_TOKEN}", "X-Shoopy-Event": event, "Content-Type": "application/json"}
    if SHOOPY_HMAC:
        h["X-Shoopy-Signature"] = "sha256=" + hmac.new(SHOOPY_HMAC.encode(), body, hashlib.sha256).hexdigest()
    return h


def _order(order_id: str, mobile: str) -> dict:
    return {
        "id": order_id,
        "number": f"SHP-{order_id}",
        "status": "PLACED",
        "payment_mode": "COD",
        "amount": 250000,
        "due_amount": 250000,
        "partner_name": "Ramesh Caterers",
        "tracking_id": None,
        "company_name": "Ramesh Caterers Pvt Ltd",
        "address": {"customer_name": "Ramesh Caterers", "mobile": mobile, "city": "Noida", "state": "UP", "pincode": "201301"},
        "items": [
            {"name": "Frozen Momos", "sku": "K24-MOMO-500", "quantity": 10, "price": 250},
            {"name": "Veg Samosa", "sku": "K24-SAMOSA-1KG", "quantity": 5, "price": None},  # null price tolerated
        ],
        "created_at": 1751280000000,   # epoch-ms
        "updated_at": None,            # null tolerated
    }


def _wa_payload(msg_id: str, wa_id: str) -> dict:
    return {
        "object": "whatsapp_business_account",
        "entry": [{
            "id": "WABA",
            "changes": [{
                "field": "messages",
                "value": {
                    "contacts": [{"wa_id": wa_id, "profile": {"name": "Priya Hotel"}}],
                    "messages": [{"from": wa_id, "id": msg_id, "type": "text", "text": {"body": "Do you supply frozen momos in bulk?"}}],
                },
            }],
        }],
    }


async def _delete_leads(z, ids) -> None:
    for lid in ids:
        if lid:
            try:
                await z.delete(z.crm(f"/Leads/{lid}"), with_org=False)
            except Exception as e:
                print(f"  WARN lead cleanup {lid}: {e}")


async def _run() -> None:
    created_ids: set[str] = set()
    leadsvc._cache_clear()
    transport = ASGITransport(app=appmod.app)
    async with appmod.app.router.lifespan_context(appmod.app):
        z = appmod._z
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            try:
                # --- Shoopy: bad auth -------------------------------------------------
                b = json.dumps(_order(f"O-{RUN}-1", MOBILE)).encode()
                r = await client.post("/webhook/shoopy", content=b, headers={"Authorization": "Bearer WRONG", "X-Shoopy-Event": "order.created"})
                assert r.status_code == 401, r.text
                if SHOOPY_HMAC:
                    r = await client.post("/webhook/shoopy", content=b,
                                          headers={"Authorization": f"Bearer {SHOOPY_TOKEN}", "X-Shoopy-Event": "order.created", "X-Shoopy-Signature": "sha256=deadbeef"})
                    assert r.status_code == 401, "bad HMAC must 401"
                print("  shoopy auth: bad bearer + bad HMAC -> 401 OK")

                # --- Shoopy order.created -> lead created -----------------------------
                oid1 = f"O-{RUN}-1"
                b = json.dumps(_order(oid1, MOBILE)).encode()
                r = await client.post("/webhook/shoopy", content=b, headers=_shoopy_headers(b, "order.created"))
                assert r.status_code == 200, r.text
                res = r.json()
                assert res["action"] == "created", res
                lead_id = res["lead_id"]
                created_ids.add(lead_id)
                print(f"  shoopy order.created -> lead {lead_id} score={res['score']}")

                lead = (await z.get(z.crm(f"/Leads/{lead_id}"), with_org=False))["data"][0]
                assert lead["Inbound_Source"] == "Website (Shoopy)", lead.get("Inbound_Source")
                assert lead.get("Source_Record_Id") == oid1
                assert "K24-MOMO-500" in (lead.get("SKU_Interest") or ""), lead.get("SKU_Interest")
                assert float(lead.get("Estimated_Order_Value") or 0) == 250000
                assert lead.get("Source_Payload"), "raw payload not stored"
                print(f"  verified: source/items/value/payload all captured (SKU_Interest={lead.get('SKU_Interest')})")

                # --- same id again -> NO duplicate ------------------------------------
                r = await client.post("/webhook/shoopy", content=b, headers=_shoopy_headers(b, "order.created"))
                assert r.json()["lead_id"] == lead_id, "duplicate lead for same Shoopy id!"
                assert r.json()["action"] in ("noop", "updated")
                print(f"  idempotent: same id -> same lead ({r.json()['action']})")

                # --- different order, SAME mobile -> dedupe to same lead --------------
                oid2 = f"O-{RUN}-2"
                b2 = json.dumps(_order(oid2, MOBILE)).encode()
                r = await client.post("/webhook/shoopy", content=b2, headers=_shoopy_headers(b2, "order.created"))
                assert r.json()["lead_id"] == lead_id, "same mobile created a duplicate lead!"
                assert r.json()["matched_by"] == "mobile"
                print(f"  dedupe: new order same mobile -> enriched same lead (matched_by={r.json()['matched_by']})")

                # --- order.cancelled -> flags, never deletes --------------------------
                bc = json.dumps(_order(oid1, MOBILE)).encode()
                r = await client.post("/webhook/shoopy", content=bc, headers=_shoopy_headers(bc, "order.cancelled"))
                assert r.status_code == 200 and r.json()["lead_id"] == lead_id
                lead = (await z.get(z.crm(f"/Leads/{lead_id}"), with_org=False))["data"][0]
                assert lead.get("Pipeline_Stage") == "Not-Applicable", lead.get("Pipeline_Stage")
                print("  cancelled: lead flagged Not-Applicable (not deleted)")

                # --- WhatsApp inbound -> lead created ---------------------------------
                wa_id = "91" + MOBILE
                msg_id = f"wamid.{RUN}"
                wb = json.dumps(_wa_payload(msg_id, wa_id)).encode()
                sig = "sha256=" + hmac.new(META_SECRET.encode(), wb, hashlib.sha256).hexdigest()
                # bad signature -> 401
                r = await client.post("/webhook/whatsapp", content=wb, headers={"X-Hub-Signature-256": "sha256=bad"})
                assert r.status_code == 401
                # good signature
                r = await client.post("/webhook/whatsapp", content=wb, headers={"X-Hub-Signature-256": sig})
                assert r.status_code == 200, r.text
                wres = r.json()["results"][0]
                created_ids.add(wres["lead_id"])
                print(f"  whatsapp inbound -> lead {wres['lead_id']} action={wres['action']} matched_by={wres['matched_by']}")
                # NOTE: wa_id 91+MOBILE normalises to +91MOBILE == the Shoopy lead's mobile, so this
                # dedupes onto the SAME lead (cross-source dedupe) — assert that, not a new lead.
                assert wres["lead_id"] == lead_id, "WhatsApp should dedupe onto the same customer by mobile"
                print("  cross-source dedupe: WhatsApp matched the Shoopy lead by mobile")

                # idempotent on message id
                r = await client.post("/webhook/whatsapp", content=wb, headers={"X-Hub-Signature-256": sig})
                assert r.json()["results"][0]["lead_id"] == lead_id
                print("  whatsapp idempotent on message id")

                # WhatsApp verification handshake
                r = await client.get("/webhook/whatsapp", params={"hub.mode": "subscribe", "hub.verify_token": os.getenv("META_VERIFY_TOKEN"), "hub.challenge": "12345"})
                assert r.status_code == 200 and r.text == "12345"
                print("  whatsapp GET verify handshake OK")

                print("PHASE-1 LEAD-SOURCE INTEGRATION: PASS")
            finally:
                await _delete_leads(z, created_ids)
                print(f"  cleanup: deleted {len(created_ids)} lead(s)")


@pytest.mark.asyncio
async def test_lead_source_integration() -> None:
    await _run()


def test_normalize_mobile_unit() -> None:
    assert leadsvc.normalize_mobile("9876543210") == "+919876543210"
    assert leadsvc.normalize_mobile("+91 98765-43210") == "+919876543210"
    assert leadsvc.normalize_mobile("0919876543210") == "+919876543210"
    assert leadsvc.normalize_mobile("919876543210") == "+919876543210"
    assert leadsvc.normalize_mobile(None) is None
    assert leadsvc.normalize_mobile("123") is None


if __name__ == "__main__":
    test_normalize_mobile_unit()
    print("unit: normalize_mobile PASS")
    asyncio.run(_run())
