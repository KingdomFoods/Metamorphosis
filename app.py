"""
app.py — Metamorphosis Phase 3 order -> production -> raw-material -> inventory API.

Single-file FastAPI app (matches the existing Render.com deployment style). Built in
SAFE MODE: while LIVE_MODE=false (the ONLY go-live switch), EVERY taxed/stock-moving
action is forced onto the dummy SKU K24-TEST-001, invoices are DRAFT and never sent,
no customer email / payment link fires, and stock posts to the Head-Office location the
integration user can actually write to. There is NO bypass.

Endpoints:
  GET  /health                 — liveness + safe-mode banner
  POST /webhook/order          — order in -> Zoho contact + DRAFT Sales Order (+DRAFT invoice)
  POST /production/stockin     — finished-goods Inventory Adjustment (qty IN), idempotent on batch
  POST /rm/stockin             — raw-material received -> stock IN (vendor/HSN/batch/expiry/zone)
  POST /rm/issue               — issue RM to a production batch -> stock OUT
  POST /production/workorder   — raise a make-to-order work order record (finished-goods IN only)

Auth: every POST requires header  X-Webhook-Secret: <WEBHOOK_SHARED_SECRET>  (401 otherwise).

Run locally:  uvicorn app:app --reload --port 8000
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any

import httpx
import structlog
from dotenv import load_dotenv
from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException, Request, Response
from pydantic import BaseModel, Field

import leads as leadsvc
from zoho_client import ZohoClient, ZohoError

load_dotenv()
log = structlog.get_logger("app")

# --- configuration (env-only) --------------------------------------------------
LIVE_MODE = os.getenv("LIVE_MODE", "false").strip().lower() == "true"
DUMMY_SKU = os.getenv("DUMMY_SKU", "K24-TEST-001")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SHARED_SECRET", "")
PRIMARY_LOCATION_ID = os.getenv("ZOHO_PRIMARY_LOCATION_ID", "7530276000000132001")  # MMR (go-live)
SAFE_LOCATION_ID = os.getenv("ZOHO_SAFE_LOCATION_ID", "7530276000000093251")        # Head Office (safe)

# In safe mode, stock posts to the location the integration user can write to (Head Office).
ACTIVE_LOCATION_ID = PRIMARY_LOCATION_ID if LIVE_MODE else SAFE_LOCATION_ID

# The org has NO GST configured (compliance gate). The dummy SKU is non-taxable, but the
# Sales-Order / Invoice APIs still demand an explicit tax OR exemption per line. We attach the
# org's built-in "NONTAXABLE" exemption to safe-mode lines. At go-live (LIVE_MODE=true) lines
# carry the CA-mapped GST instead and this code path is not used.
SAFE_TAX_EXEMPTION_CODE = "NONTAXABLE"

# --- lead-source integration secrets (env-only) --------------------------------
SHOOPY_WEBHOOK_TOKEN = os.getenv("SHOOPY_WEBHOOK_TOKEN", "")   # Bearer token Shoopy sends
SHOOPY_HMAC_SECRET = os.getenv("SHOOPY_HMAC_SECRET", "")       # optional: X-Shoopy-Signature over raw body
META_APP_SECRET = os.getenv("META_APP_SECRET", "")            # verifies X-Hub-Signature-256 on inbound
META_VERIFY_TOKEN = os.getenv("META_VERIFY_TOKEN", "")        # echoed back on Meta's GET handshake
# Outbound: when WHATSAPP_TOKEN + WHATSAPP_PHONE_NUMBER_ID are set, the number runs the CONVERSATIONAL
# Ria bot (salesiq_agent) and replies over WhatsApp via the Graph API. Without them, the /webhook/whatsapp
# POST falls back to the legacy behaviour (create a lead from the first message, no reply).
WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN", "")                       # permanent System-User token (secret)
WHATSAPP_PHONE_NUMBER_ID = os.getenv("WHATSAPP_PHONE_NUMBER_ID", "")   # sender phone-number id (Meta)
WHATSAPP_GRAPH_VERSION = os.getenv("WHATSAPP_GRAPH_VERSION", "v21.0")

# Module-level singleton client (opened on startup, closed on shutdown).
_z: ZohoClient | None = None
_dummy_item_id: str | None = None
_last_event: dict[str, str] = {}  # source -> iso timestamp of last received webhook


# ============================================================================
# Safe-mode helpers
# ============================================================================
def safe() -> bool:
    return not LIVE_MODE


def _today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def _z_client() -> ZohoClient:
    if _z is None:
        raise HTTPException(status_code=503, detail="Zoho client not ready")
    return _z


async def _resolve_item_id(z: ZohoClient, sku: str) -> tuple[str, str]:
    """Return (item_id, effective_sku). In SAFE MODE always resolves the dummy SKU, never a real one."""
    effective = DUMMY_SKU if safe() else sku
    data = await z.get(z.inventory("/items"), params={"sku": effective})
    items = data.get("items", [])
    if not items:
        if effective == DUMMY_SKU:
            # auto-heal the dummy in safe mode
            item = await _ensure_dummy_item(z)
            return item["item_id"], DUMMY_SKU
        raise HTTPException(status_code=404, detail=f"SKU not found: {effective}")
    return items[0]["item_id"], effective


async def _ensure_dummy_item(z: ZohoClient) -> dict[str, Any]:
    """Idempotently ensure K24-TEST-001 exists, tracked, NON-taxable (bypasses the GST gate)."""
    data = await z.get(z.inventory("/items"), params={"sku": DUMMY_SKU})
    items = data.get("items", [])
    if items:
        return items[0]
    payload = {
        "name": "K24 SAFE-MODE TEST ITEM",
        "sku": DUMMY_SKU,
        "item_type": "inventory",
        "product_type": "goods",
        "unit": "pcs",
        "rate": 100.0,
        "purchase_rate": 60.0,
        "is_taxable": False,
        "tax_exemption_code": "NONTAXABLE",
        "initial_stock": 0,
        "description": "Dummy SKU. Phase-3 automation targets this while LIVE_MODE=false. DO NOT sell.",
    }
    created = (await z.post(z.inventory("/items"), json=payload)).get("item", {})
    log.info("dummy_sku_created", item_id=created.get("item_id"), sku=DUMMY_SKU)
    return created


async def _idempotent_find(z: ZohoClient, endpoint: str, key: str, ref_field: str, ref_value: str) -> dict[str, Any] | None:
    """Find an existing Inventory record by a reference field (idempotency)."""
    data = await z.get(z.inventory(endpoint), params={ref_field: ref_value})
    rows = data.get(key, [])
    return rows[0] if rows else None


# ============================================================================
# Pydantic request models
# ============================================================================
class OrderLine(BaseModel):
    sku: str
    quantity: float = Field(gt=0)
    rate: float | None = Field(default=None, ge=0)


class OrderIn(BaseModel):
    external_order_id: str = Field(..., description="Idempotency key (CRM deal id / website order id)")
    customer_name: str
    customer_email: str | None = None
    customer_gstin: str | None = None
    lines: list[OrderLine]
    source: str = "crm_deal"  # crm_deal | website_razorpay (Phase-1) | manual


class ProductionStockIn(BaseModel):
    batch_id: str = Field(..., description="Idempotency key for the finished-goods batch")
    sku: str
    quantity: float = Field(gt=0)
    mfg_date: str | None = None
    expiry_date: str | None = None
    qc_status: str | None = None


class WorkOrder(BaseModel):
    external_order_id: str
    sku: str
    quantity: float = Field(gt=0)
    rm_batches_planned: list[str] = []


class RMStockIn(BaseModel):
    grn_id: str = Field(..., description="Idempotency key — goods-receipt / supplier-invoice id")
    sku: str
    quantity: float = Field(gt=0)
    vendor_name: str | None = None
    vendor_gstin: str | None = None
    hsn: str | None = None
    batch: str | None = None
    expiry_date: str | None = None
    zone: str | None = None  # ambient | chilled | frozen | quarantine


class RMIssue(BaseModel):
    issue_id: str = Field(..., description="Idempotency key")
    sku: str
    quantity: float = Field(gt=0)
    batch_id: str = Field(..., description="Production Batch_ID this RM is issued to")


# ============================================================================
# Auth dependency
# ============================================================================
async def require_secret(x_webhook_secret: str | None = Header(default=None)) -> None:
    if not WEBHOOK_SECRET or x_webhook_secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="invalid or missing X-Webhook-Secret")


# ============================================================================
# Lifespan: validate auth, ensure dummy SKU
# ============================================================================
@asynccontextmanager
async def lifespan(app: FastAPI):
    global _z, _dummy_item_id
    _z = await ZohoClient().__aenter__()
    # auth proof on .com (execution-order step 1) — fail fast, do not fabricate
    org = (await _z.get(_z.inventory("/organizations"))).get("organizations", [{}])[0]
    log.info("startup_auth_ok", org=org.get("name"), org_id=org.get("organization_id"),
             gstin=org.get("gst_no") or "UNSET", live_mode=LIVE_MODE)
    dummy = await _ensure_dummy_item(_z)
    _dummy_item_id = dummy.get("item_id")
    if not LIVE_MODE:
        log.warning("SAFE_MODE_ACTIVE", dummy_sku=DUMMY_SKU, location=ACTIVE_LOCATION_ID,
                    note="all automation forced to dummy SKU; invoices DRAFT; no sends")
    try:
        yield
    finally:
        if _z is not None:
            await _z.__aexit__(None, None, None)
            _z = None


app = FastAPI(title="K24 Metamorphosis Phase 3", version="1.0.0", lifespan=lifespan)

# IndiaMART lead capture — RE-MOUNTED 2026-07-08: the official plugin failed in practice
# (bugged UI function editor + expired connected-app auth → INVALID_TOKEN), so we serve the
# webhook ourselves. Self-contained router reuses leads.upsert_lead (dedup + score +
# idempotency) — see indiamart.py. Backfill is the indiamart_backfill.py CLI.
from indiamart import router as indiamart_router  # noqa: E402

app.include_router(indiamart_router)

# TailorTalk WhatsApp AI agent -> CRM lead (schema-tolerant; see tailortalk.py).
from tailortalk import router as tailortalk_router  # noqa: E402

app.include_router(tailortalk_router)

# SalesIQ WhatsApp AGENT (Claude-powered) -> CRM lead. Replaces TailorTalk with a controllable,
# grounded conversational qualifier. Router loads even without the anthropic SDK / ANTHROPIC_API_KEY
# (health-only + scripted fallback), so it can never take down the other webhooks. See salesiq_agent.py.
from salesiq_agent import router as salesiq_router  # noqa: E402
from salesiq_agent import handle_message as salesiq_handle  # noqa: E402

app.include_router(salesiq_router)


# ============================================================================
# Endpoints
# ============================================================================
@app.get("/health")
async def health() -> dict[str, Any]:
    return {
        "ok": True,
        "live_mode": LIVE_MODE,
        "safe_mode": safe(),
        "dummy_sku": DUMMY_SKU,
        "active_location_id": ACTIVE_LOCATION_ID,
        "banner": (
            "SAFE MODE — dummy SKU only, invoices DRAFT, no sends"
            if safe()
            else "LIVE MODE — real SKUs, real invoices"
        ),
    }


@app.post("/webhook/order", dependencies=[Depends(require_secret)])
async def webhook_order(order: OrderIn) -> dict[str, Any]:
    """Order in -> resolve/create contact -> DRAFT Sales Order -> DRAFT invoice (never sent in safe mode)."""
    z = _z_client()

    # idempotency on external order id (Sales Order reference_number)
    existing = await _idempotent_find(z, "/salesorders", "salesorders", "reference_number", order.external_order_id)
    if existing:
        return {"status": "exists", "salesorder_id": existing["salesorder_id"], "safe_mode": safe()}

    # 1. resolve/create customer (idempotent on email when present)
    contact_id = await _resolve_contact(z, order)

    # 2. build line items — SAFE MODE substitutes the dummy SKU for everything
    line_items = []
    substituted = []
    for ln in order.lines:
        item_id, eff = await _resolve_item_id(z, ln.sku)
        if eff != ln.sku:
            substituted.append({ln.sku: eff})
        li = {"item_id": item_id, "quantity": ln.quantity, "rate": ln.rate if ln.rate is not None else 100.0}
        if safe():
            li["tax_exemption_code"] = SAFE_TAX_EXEMPTION_CODE
        line_items.append(li)

    # 3. create DRAFT Sales Order
    so_payload = {
        "customer_id": contact_id,
        "reference_number": order.external_order_id,
        "location_id": ACTIVE_LOCATION_ID,
        "line_items": line_items,
        "notes": "Created by Metamorphosis Phase 3" + (" [SAFE MODE — DUMMY SKU]" if safe() else ""),
    }
    so = (await z.post(z.inventory("/salesorders"), json=so_payload)).get("salesorder", {})
    log.info("salesorder_created", so_id=so.get("salesorder_id"), status=so.get("status"), safe_mode=safe())

    # 4. DRAFT invoice — ALWAYS draft, NEVER auto-sent while not LIVE_MODE.
    #    (At go-live the native Sales Order Cycle does this — see RUNBOOK_SalesOrderCycle.md —
    #     and this explicit step is disabled. Here it proves the chain end-to-end in safe mode.)
    invoice = await _create_draft_invoice(z, contact_id, line_items, order.external_order_id)

    return {
        "status": "created",
        "safe_mode": safe(),
        "salesorder_id": so.get("salesorder_id"),
        "salesorder_status": so.get("status"),
        "invoice_id": invoice.get("invoice_id"),
        "invoice_status": invoice.get("status"),  # 'draft'
        "auto_sent": False,
        "substituted_skus": substituted,
    }


async def _resolve_contact(z: ZohoClient, order: OrderIn) -> str:
    if order.customer_email:
        found = await _idempotent_find(z, "/contacts", "contacts", "email", order.customer_email)
        if found:
            return found["contact_id"]
    payload: dict[str, Any] = {"contact_name": order.customer_name, "company_name": order.customer_name, "contact_type": "customer"}
    if order.customer_gstin and LIVE_MODE:
        payload["gst_no"] = order.customer_gstin
        payload["gst_treatment"] = "business_gst"
    if order.customer_email:
        payload["contact_persons"] = [{"email": order.customer_email, "first_name": order.customer_name[:40]}]
    contact = (await z.post(z.inventory("/contacts"), json=payload)).get("contact", {})
    return contact["contact_id"]


async def _create_draft_invoice(z: ZohoClient, contact_id: str, line_items: list[dict], ref: str) -> dict[str, Any]:
    """Create a Books invoice in DRAFT status. Never calls the /status/sent or /email endpoints."""
    inv_lines = []
    for li in line_items:
        row = {"item_id": li["item_id"], "quantity": li["quantity"], "rate": li["rate"]}
        if safe():
            row["tax_exemption_code"] = SAFE_TAX_EXEMPTION_CODE
        inv_lines.append(row)
    payload = {
        "customer_id": contact_id,
        "reference_number": ref,
        "line_items": inv_lines,
        # Books defaults new invoices to 'draft'; we NEVER call mark-as-sent or email in safe mode.
        "notes": "DRAFT — Metamorphosis Phase 3" + (" [SAFE MODE]" if safe() else ""),
    }
    try:
        inv = (await z.post(z.books("/invoices"), json=payload)).get("invoice", {})
    except ZohoError as e:
        # surfaces the compliance gate clearly if a non-dummy taxable item ever reaches here
        log.error("invoice_draft_failed", error=str(e), payload=e.payload)
        raise HTTPException(status_code=422, detail=f"DRAFT invoice failed (likely GST/tax gate): {e}")
    log.info("invoice_draft_created", invoice_id=inv.get("invoice_id"), status=inv.get("status"), auto_sent=False)
    return inv


@app.post("/production/stockin", dependencies=[Depends(require_secret)])
async def production_stockin(body: ProductionStockIn) -> dict[str, Any]:
    """Finished-goods IN: Inventory Adjustment (qty +). Idempotent on batch_id (via reference)."""
    z = _z_client()
    item_id, eff = await _resolve_item_id(z, body.sku)
    ref = f"FG-{body.batch_id}"
    existing = await _idempotent_find(z, "/inventoryadjustments", "inventory_adjustments", "reference_number", ref)
    if existing:
        return {"status": "exists", "adjustment_id": existing["inventory_adjustment_id"], "safe_mode": safe()}
    adj = {
        "reference_number": ref,
        "date": _today(),
        "reason": "Production finished-goods IN",
        "description": f"Batch {body.batch_id} mfg={body.mfg_date} exp={body.expiry_date} qc={body.qc_status}",
        "adjustment_type": "quantity",
        "line_items": [{"item_id": item_id, "location_id": ACTIVE_LOCATION_ID, "quantity_adjusted": body.quantity}],
    }
    res = (await z.post(z.inventory("/inventoryadjustments"), json=adj)).get("inventory_adjustment", {})
    log.info("production_stockin", adj_id=res.get("inventory_adjustment_id"), sku=eff, qty=body.quantity, safe_mode=safe())
    return {"status": "created", "adjustment_id": res.get("inventory_adjustment_id"), "sku": eff, "qty_in": body.quantity, "safe_mode": safe()}


@app.post("/production/workorder", dependencies=[Depends(require_secret)])
async def production_workorder(body: WorkOrder) -> dict[str, Any]:
    """Make-to-order: record a production work order (finished-goods IN only; NO RM-consumption costing).

    Zoho has no first-class 'work order' object on this plan, so the durable record is a Zoho Creator
    form (see RUNBOOK_Creator_Production.md). Here we (a) check stock, (b) return the work-order intent
    that the floor team's Creator form / the Deluge production_workorder.dg will persist + post back via
    /production/stockin. In safe mode this only ever references the dummy SKU."""
    z = _z_client()
    item_id, eff = await _resolve_item_id(z, body.sku)
    stock = await z.get(z.inventory(f"/items/{item_id}"))
    available = stock.get("item", {}).get("available_stock", 0)
    make_to_order = available < body.quantity
    log.info("workorder_raised", sku=eff, qty=body.quantity, available=available, make_to_order=make_to_order, safe_mode=safe())
    return {
        "status": "workorder_raised",
        "sku": eff,
        "requested_qty": body.quantity,
        "available_stock": available,
        "make_to_order": make_to_order,
        "rm_batches_planned": body.rm_batches_planned,
        "next": "floor team completes Creator production form -> on submit posts /production/stockin",
        "safe_mode": safe(),
    }


@app.post("/rm/stockin", dependencies=[Depends(require_secret)])
async def rm_stockin(body: RMStockIn) -> dict[str, Any]:
    """Raw-material received (QR-scanned supplier invoice) -> stock IN. Idempotent on grn_id."""
    z = _z_client()
    item_id, eff = await _resolve_item_id(z, body.sku)
    ref = f"RMIN-{body.grn_id}"
    existing = await _idempotent_find(z, "/inventoryadjustments", "inventory_adjustments", "reference_number", ref)
    if existing:
        return {"status": "exists", "adjustment_id": existing["inventory_adjustment_id"], "safe_mode": safe()}
    adj = {
        "reference_number": ref,
        "date": _today(),
        "reason": "Raw material received",
        "description": (
            f"GRN {body.grn_id} vendor={body.vendor_name} gstin={body.vendor_gstin} "
            f"hsn={body.hsn} batch={body.batch} exp={body.expiry_date} zone={body.zone}"
        ),
        "adjustment_type": "quantity",
        "line_items": [{"item_id": item_id, "location_id": ACTIVE_LOCATION_ID, "quantity_adjusted": body.quantity}],
    }
    res = (await z.post(z.inventory("/inventoryadjustments"), json=adj)).get("inventory_adjustment", {})
    log.info("rm_stockin", adj_id=res.get("inventory_adjustment_id"), sku=eff, qty=body.quantity, safe_mode=safe())
    return {"status": "created", "adjustment_id": res.get("inventory_adjustment_id"), "sku": eff, "qty_in": body.quantity, "safe_mode": safe()}


@app.post("/rm/issue", dependencies=[Depends(require_secret)])
async def rm_issue(body: RMIssue) -> dict[str, Any]:
    """Issue RM to a production batch -> stock OUT (negative adjustment). Idempotent on issue_id.

    Recorded for traceability (RM batches consumed by Batch_ID) — NO consumption costing yet, per the
    K24 decision (finished-goods-IN only)."""
    z = _z_client()
    item_id, eff = await _resolve_item_id(z, body.sku)
    ref = f"RMISS-{body.issue_id}"
    existing = await _idempotent_find(z, "/inventoryadjustments", "inventory_adjustments", "reference_number", ref)
    if existing:
        return {"status": "exists", "adjustment_id": existing["inventory_adjustment_id"], "safe_mode": safe()}
    adj = {
        "reference_number": ref,
        "date": _today(),
        "reason": "RM issued to production",
        "description": f"Issue {body.issue_id} to Batch {body.batch_id}",
        "adjustment_type": "quantity",
        "line_items": [{"item_id": item_id, "location_id": ACTIVE_LOCATION_ID, "quantity_adjusted": -abs(body.quantity)}],
    }
    res = (await z.post(z.inventory("/inventoryadjustments"), json=adj)).get("inventory_adjustment", {})
    log.info("rm_issue", adj_id=res.get("inventory_adjustment_id"), sku=eff, qty=-abs(body.quantity), batch=body.batch_id, safe_mode=safe())
    return {"status": "created", "adjustment_id": res.get("inventory_adjustment_id"), "sku": eff, "qty_out": body.quantity, "batch_id": body.batch_id, "safe_mode": safe()}


# ============================================================================
# LEAD-SOURCE INTEGRATION (Phase 1 funnels) — LEADS ONLY, never invoices/stock.
# Reuses leads.upsert_lead (dedupe mobile->email, idempotent on external id, scoring).
# ============================================================================
def _mark_event(source: str) -> None:
    _last_event[source] = leadsvc.now_iso()


def _to_float(v: Any) -> float | None:
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _coerce_ts(value: Any) -> str | None:
    """Shoopy timestamps may be epoch-ms, ISO string, or null — normalise to ISO or None."""
    if value is None or value == "":
        return None
    try:
        if isinstance(value, (int, float)) or str(value).isdigit():
            return datetime.fromtimestamp(int(value) / 1000, tz=timezone.utc).isoformat(timespec="seconds")
    except (ValueError, OverflowError, OSError):
        pass
    return str(value)


# ---------------------------------------------------------------------------
# SOURCE 1 — SHOOPY WEBSITE
# ---------------------------------------------------------------------------
def _verify_shoopy(request: Request, raw: bytes, authorization: str | None) -> None:
    """Bearer token (required) + optional HMAC over the RAW body. 401 on any failure."""
    if not SHOOPY_WEBHOOK_TOKEN:
        raise HTTPException(status_code=503, detail="SHOOPY_WEBHOOK_TOKEN not configured")
    token = (authorization or "").removeprefix("Bearer ").strip()
    if not hmac.compare_digest(token, SHOOPY_WEBHOOK_TOKEN):
        raise HTTPException(status_code=401, detail="invalid bearer token")
    if SHOOPY_HMAC_SECRET:
        sig = request.headers.get("X-Shoopy-Signature", "")
        expected = hmac.new(SHOOPY_HMAC_SECRET.encode(), raw, hashlib.sha256).hexdigest()
        provided = sig.split("=", 1)[-1].strip()  # accept hex or sha256=<hex>
        if not hmac.compare_digest(provided, expected):
            raise HTTPException(status_code=401, detail="invalid X-Shoopy-Signature")


def _shoopy_order_to_lead(order: dict[str, Any]) -> dict[str, Any]:
    """Map a Shoopy order payload to upsert_lead kwargs. EVERYTHING optional — never crash on null."""
    addr = order.get("address") or {}
    items = order.get("items") or []
    name = order.get("partner_name") or addr.get("customer_name") or "Shoopy Customer"
    parts = str(name).strip().split(" ", 1)
    first = parts[0] if len(parts) > 1 else None
    last = parts[1] if len(parts) > 1 else parts[0]
    item_lines = []
    skus = []
    names = []
    for it in items:
        if not isinstance(it, dict):
            continue
        item_lines.append(f"{it.get('name') or '?'} x{it.get('quantity') or 0} @ {it.get('price') or 0} (sku {it.get('sku') or '-'})")
        if it.get("sku"):
            skus.append(str(it.get("sku")))
        if it.get("name"):
            names.append(str(it.get("name")))
    note = (
        f"Shoopy order #{order.get('number') or order.get('id')} status={order.get('status')} "
        f"pay={order.get('payment_mode')} amount={order.get('amount')} due={order.get('due_amount')} "
        f"tracking={order.get('tracking_id') or '-'}; items: " + (" | ".join(item_lines) or "none")
    )
    return {
        "external_id": str(order.get("id")) if order.get("id") is not None else None,
        "first_name": first,
        "last_name": last,
        "company": order.get("company_name") or addr.get("company_name") or None,
        "mobile": addr.get("mobile") or order.get("mobile"),
        "email": order.get("email") or addr.get("email"),
        "city": addr.get("city"),
        "gstin": order.get("gstin") or order.get("tax_id"),
        "est_value": _to_float(order.get("amount")),
        "product_interest": (", ".join(names))[:255] or None,
        "sku_interest": (", ".join(skus))[:255] or None,
        "note": note,
    }


@app.post("/webhook/shoopy")
async def webhook_shoopy(request: Request, authorization: str | None = Header(default=None)) -> dict[str, Any]:
    raw = await request.body()
    _verify_shoopy(request, raw, authorization)
    try:
        payload = json.loads(raw or b"{}")
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="invalid JSON body")

    event = request.headers.get("X-Shoopy-Event", payload.get("event", "order.created"))
    _mark_event("shoopy")
    log.info("shoopy_webhook", shoopy_event=event, raw_len=len(raw))  # raw persisted in log before processing (audit)
    z = _z_client()

    obj = payload.get("data") or payload.get("order") or payload.get("customer") or payload
    if not isinstance(obj, dict):
        obj = payload

    if event in ("order.created", "order.updated", "order.cancelled"):
        kw = _shoopy_order_to_lead(obj)
        if event == "order.cancelled":
            kw["stage"] = "Not-Applicable"
            kw["note"] = "CANCELLED — " + (kw.get("note") or "")
        result = await leadsvc.upsert_lead(z, inbound_source="Website (Shoopy)", raw_payload=obj, **kw)
    elif event in ("customer.created", "customer.updated", "customer.deleted"):
        name = obj.get("name") or "Shoopy Customer"
        parts = str(name).strip().split(" ", 1)
        result = await leadsvc.upsert_lead(
            z,
            inbound_source="Website (Shoopy)",
            external_id=str(obj.get("id")) if obj.get("id") is not None else None,
            first_name=parts[0] if len(parts) > 1 else None,
            last_name=parts[1] if len(parts) > 1 else parts[0],
            company=obj.get("company_name") or None,
            mobile=obj.get("mobile"),
            email=obj.get("email"),
            gstin=obj.get("gstin") or obj.get("tax_id"),
            note=f"Shoopy customer event {event}",
            raw_payload=obj,
            stage="Not-Applicable" if event == "customer.deleted" else None,
        )
    else:
        log.warning("shoopy_unknown_event", shoopy_event=event)
        return {"status": "ignored", "event": event}

    return {"status": "ok", "event": event, **result}


@app.get("/webhook/shoopy/health")
async def shoopy_health() -> dict[str, Any]:
    return {"ok": True, "source": "shoopy", "last_event_at": _last_event.get("shoopy")}


# ---------------------------------------------------------------------------
# SOURCE 3 — WHATSAPP (Meta Cloud API)
#   • WHATSAPP_TOKEN + WHATSAPP_PHONE_NUMBER_ID set → CONVERSATIONAL Ria bot (replies via Graph API)
#   • otherwise                                      → legacy lead-capture (create lead, no reply)
# ---------------------------------------------------------------------------
_wa_seen_ids: set[str] = set()  # in-memory dedupe of Meta message ids (Meta retries on non-200)


async def _wa_send(to: str, text: str) -> None:
    """Send a WhatsApp text reply via the Meta Graph API. Best-effort — logs on failure."""
    if not (WHATSAPP_TOKEN and WHATSAPP_PHONE_NUMBER_ID):
        log.warning("wa_send_skipped_no_token", to=to)
        return
    url = f"https://graph.facebook.com/{WHATSAPP_GRAPH_VERSION}/{WHATSAPP_PHONE_NUMBER_ID}/messages"
    try:
        async with httpx.AsyncClient(timeout=30.0) as c:
            r = await c.post(
                url,
                headers={"Authorization": f"Bearer {WHATSAPP_TOKEN}"},
                json={"messaging_product": "whatsapp", "to": to, "type": "text", "text": {"body": text[:4096]}},
            )
        if r.status_code >= 300:
            log.error("wa_send_failed", to=to, status=r.status_code, body=r.text[:300])
        else:
            log.info("wa_send_ok", to=to)
    except Exception as exc:  # noqa: BLE001
        log.error("wa_send_error", to=to, error=str(exc))


async def _wa_process_conversation(wa_id: str, text: str, name: str) -> None:
    """Run the Ria brain for one inbound WhatsApp text and reply over WhatsApp. Runs in the
    background so the webhook can 200 inside Meta's 5s window (avoids retries / double replies)."""
    try:
        result = await salesiq_handle(wa_id, text, {"phone": wa_id, "name": name, "channel": "whatsapp"})
        reply = (result or {}).get("reply")
        if reply:
            await _wa_send(wa_id, reply)
    except Exception as exc:  # noqa: BLE001
        log.error("wa_conversation_error", wa_id=wa_id, error=str(exc))
        await _wa_send(wa_id, "Sorry, thodi technical dikkat aa gayi 🙏 Hamari team aapse jaldi connect karegi.")


@app.get("/webhook/whatsapp")
async def whatsapp_verify(request: Request) -> Response:
    """Meta verification handshake — echo hub.challenge when the verify token matches."""
    params = request.query_params
    if params.get("hub.mode") == "subscribe" and META_VERIFY_TOKEN and params.get("hub.verify_token") == META_VERIFY_TOKEN:
        return Response(content=params.get("hub.challenge", ""), media_type="text/plain")
    raise HTTPException(status_code=403, detail="verification failed")


@app.post("/webhook/whatsapp")
async def whatsapp_webhook(request: Request, background: BackgroundTasks) -> dict[str, Any]:
    raw = await request.body()
    if not META_APP_SECRET:
        raise HTTPException(status_code=503, detail="META_APP_SECRET not configured")
    sig = request.headers.get("X-Hub-Signature-256", "")
    expected = "sha256=" + hmac.new(META_APP_SECRET.encode(), raw, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected):
        raise HTTPException(status_code=401, detail="invalid X-Hub-Signature-256")

    try:
        payload = json.loads(raw or b"{}")
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="invalid JSON body")
    _mark_event("whatsapp")

    conversational = bool(WHATSAPP_TOKEN and WHATSAPP_PHONE_NUMBER_ID)
    log.info("whatsapp_webhook", raw_len=len(raw), mode="conversational" if conversational else "legacy")
    z = None if conversational else _z_client()

    processed = 0
    for entry in payload.get("entry", []) or []:
        for change in entry.get("changes", []) or []:
            value = change.get("value", {}) or {}
            contacts = {c.get("wa_id"): (c.get("profile") or {}).get("name") for c in value.get("contacts", []) or []}
            for msg in value.get("messages", []) or []:
                msg_id = msg.get("id") or ""
                if msg_id and msg_id in _wa_seen_ids:
                    continue  # Meta retry — already handled
                if msg_id:
                    _wa_seen_ids.add(msg_id)
                    if len(_wa_seen_ids) > 5000:  # bound memory
                        _wa_seen_ids.clear()
                        _wa_seen_ids.add(msg_id)
                wa_id = msg.get("from")
                # Only text messages carry a conversation. Others (image/audio/location/etc.) get a nudge.
                if msg.get("type") != "text":
                    if conversational and wa_id:
                        background.add_task(_wa_send, wa_id, "Abhi main sirf text messages padh sakti hoon 🙏 Aap type karke bhejein.")
                    continue
                text = (msg.get("text") or {}).get("body") or ""
                name = contacts.get(wa_id) or "WhatsApp Lead"
                if conversational:
                    background.add_task(_wa_process_conversation, wa_id, text, name)
                else:
                    parts = str(name).strip().split(" ", 1)
                    await leadsvc.upsert_lead(
                        z,
                        inbound_source="WhatsApp",
                        external_id=msg_id,  # idempotent on message id
                        first_name=parts[0] if len(parts) > 1 else None,
                        last_name=parts[1] if len(parts) > 1 else parts[0],
                        mobile=wa_id,
                        note=f"WhatsApp inbound: {text[:300]}",
                        raw_payload=msg,
                    )
                processed += 1
    return {"status": "ok", "processed": processed, "mode": "conversational" if conversational else "legacy"}
