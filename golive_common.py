"""
golive_common.py — shared config + read-only helpers for the inventory go-live scripts.

Used by load_items.py, verify_location.py and golive_check.py. Everything here is
entity-agnostic: org id, GSTIN and the target stock location are read from env (the
entity just changed from Kingdom 24 Pvt Ltd to Kingdom Foods proprietorship), so the
code keeps working after a human re-points the org in the Zoho UI.

HARD RULES honoured here:
  - Never set the org GSTIN or rename the org — only VERIFY (read-only).
  - Never invent a tax id — map GST rate -> the org's existing tax; report if missing.
  - Never hardcode org id / GSTIN / location id — env only.
  - .com endpoints, /inventory/v1/locations (not /warehouses), /categories (not /itemgroups).
"""
from __future__ import annotations

import os
import sys
from typing import Any

from dotenv import load_dotenv

# Zoho data (e.g. the "K24 Sector 68 — MMR" location name) and template hints contain
# non-cp1252 glyphs; force UTF-8 stdout so Windows consoles don't crash on print().
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    except (AttributeError, ValueError):
        pass

from zoho_client import ZohoClient  # reuse the audited client (token cache, retry, scope fail-fast)

load_dotenv()

# --- env-only config (entity is configurable) ---------------------------------
GSTIN = (os.getenv("ZOHO_GSTIN") or "").strip()
LOCATION_ID = (os.getenv("ZOHO_LOCATION_ID") or "").strip()
LIVE_MODE = (os.getenv("LIVE_MODE", "false") or "").strip().lower() == "true"
DUMMY_SKU = (os.getenv("DUMMY_SKU", "K24-TEST-001") or "").strip()

# GST rates Zoho/K24 recognise; used only for validation, never to invent a tax.
VALID_GST_RATES = {0, 5, 12, 18, 28}


def norm_gstin(v: Any) -> str:
    return str(v or "").strip().upper()


async def fetch_org(z: ZohoClient) -> dict[str, Any]:
    """Return the org dict matching ZOHO_ORG_ID (or the first org). Read-only."""
    data = await z.get(z.inventory("/organizations"))
    orgs = data.get("organizations", []) or []
    return next((o for o in orgs if str(o.get("organization_id")) == str(z.org_id)), orgs[0] if orgs else {})


def org_gstin(org: dict[str, Any]) -> str:
    """Extract the org GSTIN across the field names Zoho uses on different editions."""
    for k in ("gst_no", "tax_reg_no", "gstin", "tax_registration_number"):
        v = org.get(k)
        if v:
            return norm_gstin(v)
    return ""


async def fetch_locations(z: ZohoClient) -> list[dict[str, Any]]:
    """GET /inventory/v1/locations (NOT /warehouses)."""
    data = await z.get(z.inventory("/locations"))
    return data.get("locations", []) or []


async def fetch_taxes(z: ZohoClient) -> list[dict[str, Any]]:
    """Org's configured taxes. Read-only — we map to these, never create them."""
    data = await z.get(z.inventory("/settings/taxes"))
    return data.get("taxes", []) or []


def build_tax_rate_map(taxes: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    """rate(int) -> tax dict, for single (non-group) taxes only. Lets us map a row's
    GST Rate to an EXISTING org tax id. A missing rate is reported, never invented."""
    out: dict[int, dict[str, Any]] = {}
    for t in taxes:
        if str(t.get("tax_type", "tax")).lower() == "tax_group":
            continue
        try:
            rate = round(float(t.get("tax_percentage")))
        except (TypeError, ValueError):
            continue
        out.setdefault(rate, t)  # first (usually the intra-state CGST+SGST group parent or single)
    return out


async def fetch_items(z: ZohoClient) -> list[dict[str, Any]]:
    """All inventory items (paginated)."""
    return await z.paginate_inventory("/items", "items")


async def find_item_by_sku(z: ZohoClient, sku: str) -> dict[str, Any] | None:
    """Idempotency helper: exact-SKU lookup. Returns the item dict or None."""
    if not sku:
        return None
    data = await z.get(z.inventory("/items"), params={"sku": sku})
    for it in data.get("items", []) or []:
        if str(it.get("sku", "")).strip() == str(sku).strip():
            return it
    return None


# --- tiny markdown helpers (reports are the deliverable) ----------------------
def md_table(headers: list[str], rows: list[list[Any]]) -> str:
    line = "| " + " | ".join(headers) + " |"
    sep = "| " + " | ".join("---" for _ in headers) + " |"
    body = ["| " + " | ".join("" if c is None else str(c) for c in r) + " |" for r in rows]
    return "\n".join([line, sep, *body])


def status_icon(ok: bool | None) -> str:
    return "PASS" if ok else ("WARN" if ok is None else "FAIL")
