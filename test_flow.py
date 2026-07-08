"""
test_flow.py — Phase 3 SAFE-MODE end-to-end test.

Drives app.py as an ASGI app in a SINGLE asyncio loop (runs the real lifespan ->
validates auth on .com, ensures the dummy SKU). Then:
  - fake order with REAL-looking SKUs -> DRAFT Sales Order + DRAFT invoice on the DUMMY SKU,
    asserts substitution happened, invoice status == 'draft', auto_sent == False (ZERO sends)
  - independently confirms the invoice is 'draft' by reading Books
  - production stock-in, RM stock-in, RM issue -> inventory adjustments posted
  - idempotency: re-posting the same order returns status 'exists'
Then DELETES everything it created (SO, invoice, adjustments, contact) — leaves no residue.

HARD GUARD: refuses to run if LIVE_MODE=true.

Run:  pytest test_flow.py -v     (or)     python test_flow.py
"""
from __future__ import annotations

import asyncio
import os
import uuid

import httpx
import pytest
from dotenv import load_dotenv
from httpx import ASGITransport

load_dotenv()

assert os.getenv("LIVE_MODE", "false").strip().lower() != "true", "REFUSING to run e2e test with LIVE_MODE=true"

import app as appmod  # noqa: E402  (import after the guard)

SECRET = os.getenv("WEBHOOK_SHARED_SECRET", "")
HDR = {"X-Webhook-Secret": SECRET}
RUN = uuid.uuid4().hex[:8]
EMAIL = f"e2e-{RUN}@example.invalid"


def _order_payload() -> dict:
    return {
        "external_order_id": f"E2E-ORDER-{RUN}",
        "customer_name": f"E2E Test Customer {RUN}",
        "customer_email": EMAIL,
        "customer_gstin": "09AAAAA0000A1Z5",
        "source": "crm_deal",
        "lines": [
            {"sku": "K24-REAL-MOMO-500", "quantity": 3, "rate": 250.0},   # real-looking -> MUST be substituted
            {"sku": "K24-REAL-SAMOSA-1KG", "quantity": 2},
        ],
    }


async def _cleanup(z, so_id, invoice_id, adj_ids) -> None:
    if invoice_id:
        try:
            await z.delete(z.books(f"/invoices/{invoice_id}"))
        except Exception as e:
            print(f"  WARN invoice cleanup: {e}")
    if so_id:
        try:
            await z.delete(z.inventory(f"/salesorders/{so_id}"))
        except Exception as e:
            print(f"  WARN SO cleanup: {e}")
    for aid in adj_ids:
        if aid:
            try:
                await z.delete(z.inventory(f"/inventoryadjustments/{aid}"))
            except Exception as e:
                print(f"  WARN adj cleanup {aid}: {e}")
    try:
        d = await z.get(z.inventory("/contacts"), params={"email": EMAIL})
        rows = d.get("contacts", [])
        if rows:
            await z.delete(z.inventory(f"/contacts/{rows[0]['contact_id']}"))
    except Exception as e:
        print(f"  WARN contact cleanup: {e}")


async def _run() -> None:
    so_id = invoice_id = None
    adj_ids: list = []
    transport = ASGITransport(app=appmod.app)
    # run the real lifespan (auth proof + ensure dummy) in THIS loop, so the app's
    # in-loop Zoho client is reusable for our independent verification + cleanup.
    async with appmod.app.router.lifespan_context(appmod.app):
        z = appmod._z  # the app's live, in-loop client
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            try:
                h = (await client.get("/health")).json()
                assert h["safe_mode"] is True and h["live_mode"] is False, h
                print(f"  health: {h['banner']}")

                # auth required
                assert (await client.post("/webhook/order", json=_order_payload())).status_code == 401
                print("  auth: missing secret -> 401 OK")

                # order -> draft SO + draft invoice on dummy
                r = await client.post("/webhook/order", json=_order_payload(), headers=HDR)
                assert r.status_code == 200, r.text
                o = r.json()
                so_id, invoice_id = o["salesorder_id"], o["invoice_id"]
                print(f"  order: SO={so_id} invoice={invoice_id} status={o['invoice_status']}")

                assert o["auto_sent"] is False, "an auto-send fired in safe mode!"
                assert o["invoice_status"] == "draft", f"invoice not draft: {o['invoice_status']}"
                assert o["substituted_skus"], "real SKUs were NOT substituted to dummy!"
                print(f"  substituted: {o['substituted_skus']}  auto_sent={o['auto_sent']}")

                # independent confirmation invoice is DRAFT in Books
                inv = (await z.get(z.books(f"/invoices/{invoice_id}"))).get("invoice", {})
                assert inv.get("status") == "draft", f"Books says status={inv.get('status')}"
                print(f"  Books confirms invoice status = {inv.get('status')}")

                # production finished-goods IN
                r = await client.post("/production/stockin", headers=HDR, json={
                    "batch_id": f"BATCH-{RUN}", "sku": "K24-REAL-MOMO-500", "quantity": 100,
                    "mfg_date": "2026-06-30", "expiry_date": "2026-12-30", "qc_status": "PASS"})
                assert r.status_code == 200, r.text
                assert r.json()["sku"] == appmod.DUMMY_SKU
                adj_ids.append(r.json()["adjustment_id"])
                print(f"  production stock-in adj: {r.json()['adjustment_id']} (sku={r.json()['sku']})")

                # RM stock-in
                r = await client.post("/rm/stockin", headers=HDR, json={
                    "grn_id": f"GRN-{RUN}", "sku": "RM-FLOUR", "quantity": 500,
                    "vendor_name": "ACME Flour", "vendor_gstin": "09BBBBB1111B1Z5", "hsn": "1101",
                    "batch": "FLOUR-B1", "expiry_date": "2027-01-01", "zone": "ambient"})
                assert r.status_code == 200, r.text
                adj_ids.append(r.json()["adjustment_id"])
                print(f"  RM stock-in adj: {r.json()['adjustment_id']}")

                # RM issue (stock OUT)
                r = await client.post("/rm/issue", headers=HDR, json={
                    "issue_id": f"ISS-{RUN}", "sku": "RM-FLOUR", "quantity": 50, "batch_id": f"BATCH-{RUN}"})
                assert r.status_code == 200, r.text
                adj_ids.append(r.json()["adjustment_id"])
                print(f"  RM issue adj: {r.json()['adjustment_id']} (qty_out={r.json()['qty_out']})")

                # workorder
                r = await client.post("/production/workorder", headers=HDR, json={
                    "external_order_id": f"E2E-ORDER-{RUN}", "sku": "K24-REAL-MOMO-500", "quantity": 99999})
                assert r.status_code == 200 and r.json()["make_to_order"] is True
                print(f"  workorder: make_to_order={r.json()['make_to_order']} (stock {r.json()['available_stock']})")

                # idempotency
                r2 = await client.post("/webhook/order", json=_order_payload(), headers=HDR)
                assert r2.json()["status"] == "exists" and r2.json()["salesorder_id"] == so_id
                print("  idempotency: re-post returned 'exists' (no duplicate)")

                print("PHASE 3 SAFE-MODE E2E: PASS (zero real sends)")
            finally:
                await _cleanup(z, so_id, invoice_id, adj_ids)
                print("  cleanup done")


@pytest.mark.asyncio
async def test_safe_mode_end_to_end() -> None:
    await _run()


if __name__ == "__main__":
    asyncio.run(_run())
