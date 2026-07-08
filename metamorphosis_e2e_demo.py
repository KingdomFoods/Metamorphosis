"""
metamorphosis_e2e_demo.py — end-to-end DEMO validation of the Zoho Inventory
full cycle (item -> opening stock -> customer/vendor -> SO -> PO -> invoice ->
stock reduction) for Kingdom Foods (org 906246204).

WHY THIS DEVIATES FROM THE PROMPT (deliberate, audited reasons — see repo memory):
  * Data centre is **.com**, NOT .zoho.in. The live audit proved org 906246204
    lives on the .com DC. All URLs come from zoho_client.py / env (.com).
  * The integration user can write to **Head Office** (7530276000000093251) but
    NOT to MMR (7530276000000132001 — the prompt's "PRIMARY_LOCATION"), which
    returns Zoho error 400040. So opening stock + transactions target the
    writable location, resolved from env (DEMO_LOCATION_ID / ZOHO_LOCATION_ID /
    ZOHO_SAFE_LOCATION_ID / ZOHO_PRIMARY_LOCATION_ID) with a live write probe.
  * Item create must NOT send `is_returnable` (Zoho rejects purchase-only items
    as "not returnable"). Items with opening stock cannot be hard-deleted —
    cleanup marks them INACTIVE.
  * GST is mapped to the org's EXISTING tax for that rate. A missing tax is NEVER
    invented — the demo falls back to a NONTAXABLE line and flags the gap.
  * No interactive input() — this runs headless. Behaviour is flag-driven.

MODES
  python metamorphosis_e2e_demo.py                # STEP 0 pre-flight only (read-only), then STOP at the gate
  python metamorphosis_e2e_demo.py --run          # full E2E (writes [DEMO] records to Zoho)
  python metamorphosis_e2e_demo.py --run --skip-to 3   # resume from step N using demo_state.json
  python metamorphosis_e2e_demo.py --cleanup      # void orders/invoice, delete/inactivate [DEMO] records

HARD RULES honoured: every name `[DEMO] `, every SKU `DEMO-`, every doc number
`DEMO-`, 0.7s between calls, log every call, STOP on failure, IDs saved to
demo_items_created.json, never touch non-DEMO data.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from typing import Any

import golive_common as gc  # forces UTF-8 stdout, shares env + read-only helpers
from zoho_client import TOKEN_REGEN_RUNBOOK, ZohoAuthError, ZohoClient, ZohoError

# ------------------------------------------------------------------ ANSI colour
_USE_COLOR = sys.stdout.isatty() or os.getenv("FORCE_COLOR") == "1"
if os.name == "nt":
    os.system("")  # enable VT100 escape processing on Windows consoles


def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _USE_COLOR else text


def ok(t: str) -> str: return _c("32", t)       # green
def bad(t: str) -> str: return _c("31", t)       # red
def warn(t: str) -> str: return _c("33", t)      # yellow
def head(t: str) -> str: return _c("1;36", t)    # bold cyan


OKM, FAILM, WARNM = ok("OK"), bad("FAIL"), warn("WARN")
TICK, CROSS = ok("PASS"), bad("FAIL")
SLEEP = 0.7
DEMO_DATE = "2026-07-07"
# org has auto-numbering ON; custom DEMO-* doc numbers need this query flag
IGNORE_AUTONUM = {"ignore_auto_number_generation": "true"}

STATE_FILE = "demo_state.json"
IDS_FILE = "demo_items_created.json"
RESULT_JSON = "demo_test_results.json"
RESULT_MD = "demo_test_results.md"

# ------------------------------------------------------------------ demo data
# (SKU, name, category, selling, cost, opening stock, unit, hsn, gst)
DEMO_ITEMS: list[dict[str, Any]] = [
    {"sku": "KFV01",  "name": "Green Peas 1Kg",            "cat": "Frozen Vegetables",    "sell": 120, "cost": 72,  "stock": 50, "unit": "kg",  "hsn": "21069099", "gst": 5},
    {"sku": "KFV05",  "name": "Sweet Corn 1Kg",            "cat": "Frozen Vegetables",    "sell": 130, "cost": 78,  "stock": 40, "unit": "kg",  "hsn": "21069099", "gst": 5},
    {"sku": "KMO01",  "name": "Veg Momos 1Kg",             "cat": "Momos",                "sell": 280, "cost": 168, "stock": 30, "unit": "kg",  "hsn": "21069099", "gst": 5},
    {"sku": "KMO05",  "name": "Chicken Momos 1Kg",         "cat": "Momos",                "sell": 350, "cost": 210, "stock": 25, "unit": "kg",  "hsn": "21069099", "gst": 5},
    {"sku": "KBK01",  "name": "Butter Croissant 1Kg",      "cat": "Bakery",               "sell": 450, "cost": 270, "stock": 20, "unit": "kg",  "hsn": "21069099", "gst": 5},
    {"sku": "KSC01",  "name": "Soya Chaap 1Kg",            "cat": "Soya Chaap",           "sell": 200, "cost": 120, "stock": 35, "unit": "kg",  "hsn": "21069099", "gst": 5},
    {"sku": "KPF01",  "name": "French Fries 1Kg",          "cat": "Processed Food",       "sell": 160, "cost": 96,  "stock": 45, "unit": "kg",  "hsn": "21069099", "gst": 5},
    {"sku": "KSP01",  "name": "Red Chilli Powder 100g",    "cat": "Spices & Condiments",  "sell": 60,  "cost": 36,  "stock": 60, "unit": "pcs", "hsn": "21069099", "gst": 5},
    {"sku": "KDAI01", "name": "Paneer 1Kg",                "cat": "Dairy Products",       "sell": 380, "cost": 228, "stock": 15, "unit": "kg",  "hsn": "21069099", "gst": 5},
    {"sku": "KNRT01", "name": "Basmati Rice 1Kg",          "cat": "Non Frozen Rice/Atta", "sell": 140, "cost": 84,  "stock": 50, "unit": "kg",  "hsn": "21069099", "gst": 5},
    {"sku": "KVS01",  "name": "Veg Spring Roll 1Kg",       "cat": "Veg Snacks",           "sell": 260, "cost": 156, "stock": 25, "unit": "kg",  "hsn": "21069099", "gst": 5},
    {"sku": "KVS05",  "name": "Dahi Kay Sholay 1Kg",       "cat": "Veg Snacks",           "sell": 350, "cost": 210, "stock": 20, "unit": "kg",  "hsn": "21069099", "gst": 5},
    {"sku": "KCHU01", "name": "Green Chutney Powder 500g",  "cat": "Chutney",             "sell": 615, "cost": 369, "stock": 10, "unit": "pcs", "hsn": "21069099", "gst": 5},
    {"sku": "KNV01",  "name": "Chicken Seekh Kebab 1Kg",   "cat": "Non Veg",              "sell": 420, "cost": 252, "stock": 15, "unit": "kg",  "hsn": "21069099", "gst": 5},
    {"sku": "KRTE01", "name": "Dal Makhani 1Kg",           "cat": "Ready To Eat",         "sell": 300, "cost": 180, "stock": 20, "unit": "kg",  "hsn": "21069099", "gst": 5},
]

# items used across SO / PO / invoice (SKU -> qty, rate)
SO_LINES = [("KMO01", 5, 280), ("KBK01", 3, 450), ("KFV01", 10, 120)]
PO_LINES = [("KFV01", 100, 72), ("KPF01", 50, 96), ("KDAI01", 20, 228)]

# The org's category names differ from the demo labels; map demo label -> the
# EXISTING org category name (verified live via GET /categories). Matching is
# alias -> exact -> substring, all case-insensitive.
CATEGORY_ALIAS = {
    "Frozen Vegetables":    "Processed F&V",
    "Momos":                "Momos",
    "Bakery":               "Bakery",
    "Soya Chaap":           "Chaap",
    "Processed Food":       "Processed F&V",
    "Spices & Condiments":  "Spice",
    "Dairy Products":       "Dairy",
    "Non Frozen Rice/Atta": "Rice & Biryani",
    "Veg Snacks":           "Veg Snacks",
    "Chutney":              "Chutney",
    "Non Veg":              "Non-Veg Snacks",
    "Ready To Eat":         "Ambient-Ready-to-eat",
}


def resolve_category_id(demo_cat: str, cat_by_name: dict[str, dict]) -> str | None:
    """cat_by_name: lower(name) -> category dict. alias -> exact -> substring."""
    alias = CATEGORY_ALIAS.get(demo_cat, demo_cat).lower()
    if alias in cat_by_name:
        return cat_by_name[alias].get("category_id")
    if demo_cat.lower() in cat_by_name:
        return cat_by_name[demo_cat.lower()].get("category_id")
    for name, c in cat_by_name.items():
        if alias in name or name in alias:
            return c.get("category_id")
    return None


def demo_name(n: str) -> str:
    return f"[DEMO] {n}"


def demo_sku(s: str) -> str:
    return f"DEMO-{s}"


# ------------------------------------------------------------------ state I/O
def load_state() -> dict[str, Any]:
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_state(state: dict[str, Any]) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, default=str)


def log_call(method: str, url: str, status: str, body: Any = "") -> None:
    snippet = ""
    if body:
        s = body if isinstance(body, str) else json.dumps(body, default=str)
        snippet = " | " + s[:500]
    # strip the base url for readability
    short = url.replace("https://www.zohoapis.com/inventory/v1", "").replace("https://www.zohoapis.com/books/v3", "")
    print(f"    -> {method} {short} [{status}]{snippet}")


# ------------------------------------------------------------------ location
def resolve_location_id() -> str:
    """The location we WRITE to. Prompt's PRIMARY (MMR) is not writable by the
    integration user; prefer the explicit/ safe (Head Office) location."""
    for env_key in ("DEMO_LOCATION_ID", "ZOHO_LOCATION_ID", "ZOHO_SAFE_LOCATION_ID", "ZOHO_PRIMARY_LOCATION_ID"):
        v = (os.getenv(env_key) or "").strip()
        if v:
            return v
    return ""


# ------------------------------------------------------------------ tax mapping
def build_gst_maps(taxes: list[dict]) -> tuple[dict[int, str], dict[int, str]]:
    """Return (intra_map, inter_map): GST rate -> tax_id.
      intra = tax_group taxes (GST5 = CGST2.5+SGST2.5) — for same-state supply.
      inter = single taxes (IGST5) — for inter-state supply.
    A document line MUST use the tax matching its place-of-supply, or Zoho rejects
    it (110802 'specify a tax', or an inter/intra-mismatch error)."""
    intra: dict[int, str] = {}
    inter: dict[int, str] = {}
    for t in taxes:
        try:
            rate = round(float(t.get("tax_percentage")))
        except (TypeError, ValueError):
            continue
        tid = t.get("tax_id")
        if str(t.get("tax_type", "")).lower() == "tax_group":
            intra.setdefault(rate, tid)
        else:
            inter.setdefault(rate, tid)
    return intra, inter


def _doc_lines(spec_lines: list[tuple], state: dict, gst_map: dict[int, str]) -> list[dict]:
    """Build order/invoice line_items, attaching the per-line GST tax_id chosen for
    this document's supply type (gst_map). Every demo item is 5% GST."""
    out = []
    for sku, qty, rate in spec_lines:
        gst = next(i["gst"] for i in DEMO_ITEMS if i["sku"] == sku)
        ln = {"item_id": state["items"][sku], "quantity": qty, "rate": rate}
        tid = gst_map.get(gst)
        if tid:
            ln["tax_id"] = tid
        out.append(ln)
    return out


def resolve_line_tax(rate: int, tax_map: dict[int, dict]) -> tuple[dict, str | None]:
    """Item/line tax fields for a GST rate. Never invents a tax id.
    Returns (fields, warning-or-None). On a missing rate -> NONTAXABLE fallback."""
    if rate == 0:
        if 0 in tax_map:
            return {"is_taxable": True, "tax_id": tax_map[0]["tax_id"]}, None
        return {"is_taxable": False, "tax_exemption_code": "NONTAXABLE"}, None
    t = tax_map.get(rate)
    if not t:
        return ({"is_taxable": False, "tax_exemption_code": "NONTAXABLE"},
                f"org has no {rate}% GST tax — item created NON-TAXABLE (tax never invented)")
    return {"is_taxable": True, "tax_id": t["tax_id"]}, None


# ==================================================================
# STEP 0 — pre-flight (read-only)
# ==================================================================
async def preflight(z: ZohoClient) -> dict[str, Any]:
    print(head("\n=== STEP 0 — PRE-FLIGHT (read-only) ==="))
    pf: dict[str, Any] = {"ok": True}

    # 0A. org / auth
    org = await gc.fetch_org(z)
    name = org.get("name") or org.get("company_name") or ""
    gstin = gc.org_gstin(org)
    auth_ok = "kingdom" in name.lower()
    pf["org"] = {"id": org.get("organization_id"), "name": name, "gstin": gstin or "UNSET"}
    pf["auth_ok"] = auth_ok
    log_call("GET", "/organizations", "200", {"name": name, "gstin": gstin or "UNSET"})
    print(f"  {OKM if auth_ok else FAILM} OAuth + org: {name!r}  (GSTIN {gstin or 'UNSET'})")
    if gstin and gstin != gc.GSTIN:
        print(f"  {WARNM} org GSTIN {gstin} != expected {gc.GSTIN} (entity re-point pending — informational)")
    pf["ok"] &= auth_ok
    await asyncio.sleep(SLEEP)

    # 0B. categories (soft — a missing category only means the item loads uncategorised)
    cats = (await z.get(z.inventory("/categories"))).get("categories", []) or []
    cat_by_name = {str(c.get("name", "")).strip().lower(): c for c in cats}
    needed = sorted({it["cat"] for it in DEMO_ITEMS})
    matched = {n: resolve_category_id(n, cat_by_name) for n in needed}
    n_matched = sum(1 for v in matched.values() if v)
    pf["categories_total"] = len(cats)
    pf["categories_matched"] = matched
    log_call("GET", "/categories", "200", f"{len(cats)} categories")
    cats_ok = n_matched == len(needed)
    print(f"  {OKM if cats_ok else WARNM} Categories: {len(cats)} in org, {n_matched}/{len(needed)} demo categories mapped")
    for n, v in matched.items():
        via = CATEGORY_ALIAS.get(n, n)
        mark = ok(f"-> {via}") if v else warn("no match -> item loads uncategorised")
        print(f"       - {n}: {mark}")
    # not a hard gate — items still load without a category
    await asyncio.sleep(SLEEP)

    # 0B2. taxes (do we have a 5% GST?)
    taxes = await gc.fetch_taxes(z)
    tax_map = gc.build_tax_rate_map(taxes)
    pf["tax_rates_available"] = sorted(tax_map.keys())
    pf["tax_map"] = {r: t.get("tax_id") for r, t in tax_map.items()}
    has_5 = 5 in tax_map
    log_call("GET", "/settings/taxes", "200", f"rates {sorted(tax_map.keys())}")
    print(f"  {OKM if has_5 else WARNM} Taxes: rates available {sorted(tax_map.keys()) or '[none]'} "
          f"({'5% GST present' if has_5 else '5% GST MISSING -> demo items will be NON-TAXABLE'})")
    await asyncio.sleep(SLEEP)

    # 0C. location write probe (THE known blocker)
    loc_id = resolve_location_id()
    locs = await gc.fetch_locations(z)
    loc_ids = {str(l.get("location_id")): l for l in locs}
    log_call("GET", "/locations", "200", f"{len(locs)} locations")
    pf["locations"] = [{"id": l.get("location_id"), "name": l.get("location_name")} for l in locs]
    pf["write_location_id"] = loc_id
    loc_name = loc_ids.get(loc_id, {}).get("location_name", "?") if loc_id else "(none configured)"
    print(f"  ..  Target write location: {loc_id or '(none)'}  ({loc_name})")
    for l in locs:
        print(f"       - {l.get('location_id')}  {l.get('location_name')}")

    write_ok, write_detail = await _probe_location_write(z, loc_id, tax_map)
    pf["location_write_ok"] = write_ok
    pf["location_write_detail"] = write_detail
    print(f"  {OKM if write_ok else FAILM} Location write access: {write_detail}")
    pf["ok"] &= write_ok
    await asyncio.sleep(SLEEP)

    # 0D. existing [DEMO] items (idempotency)
    existing = await _find_demo_items(z)
    pf["existing_demo_items"] = [{"sku": e.get("sku"), "id": e.get("item_id"), "name": e.get("name")} for e in existing]
    if existing:
        print(f"  {WARNM} Found {len(existing)} existing [DEMO] item(s) from a prior run:")
        for e in existing:
            print(f"       - {e.get('name')}  (SKU {e.get('sku')}, id {e.get('item_id')})")
        print(f"       Re-run steps are idempotent (existing items are reused, not duplicated). "
              f"Use --cleanup to remove them.")
    else:
        print(f"  {OKM} No existing [DEMO] items — clean slate.")

    pf["gate_passed"] = bool(pf["ok"])
    print(head(f"\n  GATE: {'PASSED' if pf['gate_passed'] else 'BLOCKED'}"))
    if not pf["gate_passed"]:
        if not pf.get("location_write_ok"):
            print(bad("  BLOCKER 3 ACTIVE: integration user lacks write access to the target location."))
            print("  FIX: Zoho UI -> Settings -> Users -> [integration user] -> Locations -> grant access,")
            print("       then set DEMO_LOCATION_ID / ZOHO_LOCATION_ID in metamorphosis/.env to a WRITABLE location.")
    return pf


async def _probe_location_write(z: ZohoClient, loc_id: str, tax_map: dict[int, dict]) -> tuple[bool, str]:
    """Create a throwaway [DEMO] probe item WITH opening stock at loc_id, then
    inactivate it. Detects Zoho error 400040 (no location access)."""
    if not loc_id:
        return False, "no writable location configured (set DEMO_LOCATION_ID / ZOHO_LOCATION_ID)"
    probe_sku = "DEMO-__PROBE__"
    tax_fields, _ = resolve_line_tax(5, tax_map)
    payload = {
        "name": demo_name("__location write probe__"),
        "sku": probe_sku,
        "product_type": "goods",
        "item_type": "inventory",
        "unit": "qty",
        "hsn_or_sac": "21069099",
        "rate": 1,
        "purchase_rate": 1,
        "track_inventory": True,
        "opening_stock": 1,
        "opening_stock_value": 1,
        "locations": [{"location_id": loc_id, "initial_stock": 1, "initial_stock_rate": 1}],
        **tax_fields,
    }
    # reuse if a stale probe exists
    existing = await gc.find_item_by_sku(z, probe_sku)
    if existing:
        try:
            await z.post(z.inventory(f"/items/{existing['item_id']}/inactive"))
        except ZohoError:
            pass
        return True, f"writable (stale probe reused at {loc_id})"
    try:
        res = (await z.post(z.inventory("/items"), json=payload)).get("item", {})
        pid = res.get("item_id")
        log_call("POST", "/items", "201", {"probe_id": pid})
        # cleanup: stock-bearing item can't hard-delete -> inactivate
        try:
            await z.post(z.inventory(f"/items/{pid}/inactive"))
        except ZohoError:
            pass
        return True, f"writable (probe {pid} created + inactivated at {loc_id})"
    except ZohoError as e:
        code = getattr(e, "code", None)
        if str(code) == "400040" or "associated location" in str(e).lower():
            return False, f"error 400040 — no access to location {loc_id}"
        return False, f"write failed: {e} (code {code})"


async def _find_demo_items(z: ZohoClient) -> list[dict[str, Any]]:
    data = await z.get(z.inventory("/items"), params={"search_text": "[DEMO]"})
    return [it for it in (data.get("items", []) or []) if str(it.get("name", "")).startswith("[DEMO]")]


# ==================================================================
# STEP 1 — create items
# ==================================================================
def _sellable_payload(name: str, it: dict, tax_fields: dict, cat_id: str | None,
                      include_stock: bool, loc_id: str = "") -> dict[str, Any]:
    """Build the item payload. can_be_sold / can_be_purchased are REQUIRED — without
    them Zoho stores the item as inventory-tracked-only (rate/purchase_rate zeroed,
    rejected from Sales Orders with error 36073). is_returnable is intentionally NOT
    sent (Zoho rejects purchase-only items as 'not returnable')."""
    p: dict[str, Any] = {
        "name": name,
        "product_type": "goods",
        "item_type": "inventory",
        "unit": it["unit"],
        "hsn_or_sac": it["hsn"],
        "rate": it["sell"],
        "purchase_rate": it["cost"],
        "can_be_sold": True,
        "can_be_purchased": True,
        "track_inventory": True,
        "brand": "Kingdom Foods",
        **tax_fields,
    }
    if cat_id:
        p["category_id"] = cat_id
    if include_stock:
        p["sku"] = demo_sku(it["sku"])
        p["opening_stock"] = it["stock"]
        p["opening_stock_value"] = round(it["stock"] * it["cost"], 2)
        if loc_id:
            p["locations"] = [{"location_id": loc_id, "initial_stock": it["stock"], "initial_stock_rate": it["cost"]}]
    return p


async def step1_items(z: ZohoClient, tax_map: dict[int, dict], cat_by_name: dict[str, dict], loc_id: str,
                      state: dict) -> dict[str, Any]:
    print(head("\n=== STEP 1 — create 15 demo items ==="))
    created = state.setdefault("items", {})  # sku(no prefix) -> item_id
    tax_notes: list[str] = []
    for it in DEMO_ITEMS:
        sku = demo_sku(it["sku"])
        name = demo_name(it["name"])
        tax_fields, tax_warn = resolve_line_tax(it["gst"], tax_map)
        if tax_warn:
            tax_notes.append(f"{sku}: {tax_warn}")
        cat_id = resolve_category_id(it["cat"], cat_by_name)

        # find existing (idempotent upsert). Existing items are UPDATED so a prior
        # run that created them without can_be_sold/rate is corrected in place.
        existing = None
        if it["sku"] in created:
            existing = {"item_id": created[it["sku"]]}
        else:
            existing = await gc.find_item_by_sku(z, sku)

        try:
            if existing:
                iid = existing["item_id"]
                upd = _sellable_payload(name, it, tax_fields, cat_id, include_stock=False)
                await z.put(z.inventory(f"/items/{iid}"), json=upd)
                created[it["sku"]] = iid
                log_call("PUT", f"/items/{iid}", "200", {"sku": sku, "rate": it["sell"]})
                print(f"  {OKM} Upserted (update): {name} | SKU {sku} | id {iid}")
            else:
                payload = _sellable_payload(name, it, tax_fields, cat_id, include_stock=True, loc_id=loc_id)
                res = (await z.post(z.inventory("/items"), json=payload)).get("item", {})
                iid = res.get("item_id")
                created[it["sku"]] = iid
                log_call("POST", "/items", "201", {"sku": sku, "id": iid})
                catmark = "" if cat_id else warn(" [no category match]")
                print(f"  {OKM} Created: {name} | SKU {sku} | id {iid}{catmark}")
            save_state(state)
        except ZohoError as e:
            print(f"  {FAILM} {sku}: {e}")
            state.setdefault("errors", []).append({"step": 1, "sku": sku, "error": str(e), "payload": getattr(e, "payload", None)})
            save_state(state)
            raise
        await asyncio.sleep(SLEEP)

    with open(IDS_FILE, "w", encoding="utf-8") as f:
        json.dump(created, f, indent=2)
    print(f"\n  Summary: {len(created)}/15 items present. IDs saved to {IDS_FILE}.")
    if tax_notes:
        print(warn("  Tax notes (GST fell back to NON-TAXABLE — org lacks the rate):"))
        for n in tax_notes:
            print("    - " + n)
    state["tax_notes"] = tax_notes
    return {"count": len(created), "tax_notes": tax_notes}


# ==================================================================
# STEP 2 — verify stock / price / cost
# ==================================================================
async def invoiced_consumption(z: ZohoClient, state: dict) -> dict[str, int]:
    """sku -> qty already consumed by a POSTED (sent/paid) DEMO invoice. Empty if the
    invoice is still draft or absent. Lets stock checks stay correct across re-runs:
    expected current stock = opening - invoiced (idempotent, not a one-shot delta)."""
    inv_id = state.get("invoice_id")
    if not inv_id:
        return {}
    try:
        inv = (await z.get(z.inventory(f"/invoices/{inv_id}"))).get("invoice", {})
    except ZohoError:
        return {}
    if str(inv.get("status", "")).lower() in ("draft", "void", ""):
        return {}  # draft/void invoices don't move stock
    return {sku: qty for sku, qty, _ in SO_LINES}


async def step2_verify(z: ZohoClient, state: dict) -> dict[str, Any]:
    print(head("\n=== STEP 2 — verify stock / cost / price ==="))
    created = state.get("items", {})
    consumed = await invoiced_consumption(z, state)  # 0 on first run; SO qtys after invoice posts
    rows, all_ok = [], True
    for it in DEMO_ITEMS:
        exp_stock = it["stock"] - consumed.get(it["sku"], 0)
        iid = created.get(it["sku"])
        if not iid:
            rows.append([it["sku"], it["name"][:22], exp_stock, "-", "-", "-", CROSS]); all_ok = False
            continue
        item = (await z.get(z.inventory(f"/items/{iid}"))).get("item", {})
        soh = item.get("stock_on_hand")
        try:
            soh_n = float(soh)
        except (TypeError, ValueError):
            soh_n = None
        pr = item.get("purchase_rate")
        rt = item.get("rate")
        stock_ok = soh_n is not None and abs(soh_n - exp_stock) < 0.001
        cost_ok = pr is not None and abs(float(pr) - it["cost"]) < 0.001
        price_ok = rt is not None and abs(float(rt) - it["sell"]) < 0.001
        row_ok = stock_ok and cost_ok and price_ok
        all_ok &= row_ok
        rows.append([it["sku"], it["name"][:22], exp_stock, soh, ok("Y") if cost_ok else bad("N"),
                     ok("Y") if price_ok else bad("N"), TICK if row_ok else CROSS])
        await asyncio.sleep(SLEEP)
    print(gc.md_table(["SKU", "Name", "ExpStk", "ActStk", "Cost", "Price", "OK"], rows))
    note = " (net of posted DEMO invoice)" if consumed else ""
    print(f"\n  {OKM if all_ok else WARNM} Stock verification: {'all 15 match' if all_ok else 'mismatch(es) present'}{note}")
    return {"all_ok": all_ok, "rows": len(rows)}


# ==================================================================
# STEP 3 / 4 — customer + vendor
# ==================================================================
async def step3_customer(z: ZohoClient, state: dict) -> str:
    print(head("\n=== STEP 3 — create demo customer ==="))
    if state.get("customer_id"):
        print(f"  {OKM} exists (reuse): {state['customer_id']}"); return state["customer_id"]
    name = demo_name("Test Restaurant — Momentum Pvt Ltd")
    existing = await _find_contact(z, name)
    if existing:
        state["customer_id"] = existing; save_state(state)
        print(f"  {OKM} exists in Zoho (reuse): {existing}"); return existing
    payload = {
        "contact_name": name,
        "company_name": demo_name("Momentum Pvt Ltd"),
        "contact_type": "customer",
        "gst_treatment": "business_gst",
        "gst_no": "07AACCO8695A1ZB",
        "place_of_contact": "DL",
        "billing_address": {"city": "New Delhi", "state": "Delhi", "country": "India", "zip": "110001"},
        "contact_persons": [{"first_name": "Demo", "last_name": "Contact", "email": "demo@test.com",
                             "phone": "9999999999", "is_primary_contact": True}],
    }
    res = (await z.post(z.inventory("/contacts"), json=payload)).get("contact", {})
    cid = res.get("contact_id")
    state["customer_id"] = cid; save_state(state)
    log_call("POST", "/contacts", "201", {"customer_id": cid})
    print(f"  {OKM} Created customer {name} -> {cid}")
    await asyncio.sleep(SLEEP)
    return cid


async def step4_vendor(z: ZohoClient, state: dict) -> str:
    print(head("\n=== STEP 4 — create demo vendor ==="))
    if state.get("vendor_id"):
        print(f"  {OKM} exists (reuse): {state['vendor_id']}"); return state["vendor_id"]
    name = demo_name("Test Supplier — Demo Farms")
    existing = await _find_contact(z, name)
    if existing:
        state["vendor_id"] = existing; save_state(state)
        print(f"  {OKM} exists in Zoho (reuse): {existing}"); return existing
    payload = {
        "contact_name": name,
        "company_name": demo_name("Demo Farms"),
        "contact_type": "vendor",
        "gst_treatment": "business_gst",
        "gst_no": "09AABCU9603R1ZM",
        "place_of_contact": "UP",
        "billing_address": {"city": "Noida", "state": "Uttar Pradesh", "country": "India", "zip": "201301"},
    }
    res = (await z.post(z.inventory("/contacts"), json=payload)).get("contact", {})
    vid = res.get("contact_id")
    state["vendor_id"] = vid; save_state(state)
    log_call("POST", "/contacts", "201", {"vendor_id": vid})
    print(f"  {OKM} Created vendor {name} -> {vid}")
    await asyncio.sleep(SLEEP)
    return vid


async def _find_contact(z: ZohoClient, contact_name: str) -> str | None:
    data = await z.get(z.inventory("/contacts"), params={"contact_name": contact_name})
    for c in data.get("contacts", []) or []:
        if str(c.get("contact_name", "")).strip() == contact_name:
            return c.get("contact_id")
    return None


# ==================================================================
# STEP 5 — sales order
# ==================================================================
async def step5_so(z: ZohoClient, state: dict, loc_id: str, tax_inter: dict[int, str]) -> dict[str, Any]:
    print(head("\n=== STEP 5 — create demo Sales Order ==="))
    if state.get("salesorder_id"):
        print(f"  {OKM} exists (reuse): {state['salesorder_id']}")
        return {"id": state["salesorder_id"], "reused": True}
    number = "DEMO-SO-001"
    existing = await _find_doc(z, "/salesorders", "salesorders", "salesorder_number", number, "salesorder_id")
    if existing:
        state["salesorder_id"] = existing; save_state(state)
        print(f"  {OKM} exists in Zoho (reuse): {existing}")
        return {"id": existing, "reused": True}
    # customer is in Delhi -> inter-state supply -> IGST (single tax)
    lines = _doc_lines(SO_LINES, state, tax_inter)
    payload = {
        "customer_id": state["customer_id"], "date": DEMO_DATE, "salesorder_number": number,
        "reference_number": "DEMO-TEST", "location_id": loc_id, "line_items": lines,
        "notes": "DEMO TEST ORDER — DELETE AFTER VALIDATION",
    }
    res = (await z.post(z.inventory("/salesorders"), json=payload, params=IGNORE_AUTONUM)).get("salesorder", {})
    sid, total = res.get("salesorder_id"), res.get("total")
    state["salesorder_id"] = sid; save_state(state)
    log_call("POST", "/salesorders", "201", {"id": sid, "total": total})
    print(f"  {OKM} Sales Order {number} created | id {sid} | total ₹{total}")
    await asyncio.sleep(SLEEP)
    return {"id": sid, "total": total}


# ==================================================================
# STEP 6 — purchase order
# ==================================================================
async def step6_po(z: ZohoClient, state: dict, loc_id: str, tax_intra: dict[int, str]) -> dict[str, Any]:
    print(head("\n=== STEP 6 — create demo Purchase Order ==="))
    if state.get("purchaseorder_id"):
        print(f"  {OKM} exists (reuse): {state['purchaseorder_id']}")
        return {"id": state["purchaseorder_id"], "reused": True}
    number = "DEMO-PO-001"
    existing = await _find_doc(z, "/purchaseorders", "purchaseorders", "purchaseorder_number", number, "purchaseorder_id")
    if existing:
        state["purchaseorder_id"] = existing; save_state(state)
        print(f"  {OKM} exists in Zoho (reuse): {existing}")
        return {"id": existing, "reused": True}
    # vendor is in UP (same state as org) -> intra-state supply -> CGST+SGST group
    lines = _doc_lines(PO_LINES, state, tax_intra)
    payload = {
        "vendor_id": state["vendor_id"], "date": DEMO_DATE, "purchaseorder_number": number,
        "reference_number": "DEMO-TEST", "location_id": loc_id, "line_items": lines,
        "notes": "DEMO TEST PURCHASE — DELETE AFTER VALIDATION",
    }
    res = (await z.post(z.inventory("/purchaseorders"), json=payload, params=IGNORE_AUTONUM)).get("purchaseorder", {})
    pid, total = res.get("purchaseorder_id"), res.get("total")
    state["purchaseorder_id"] = pid; save_state(state)
    log_call("POST", "/purchaseorders", "201", {"id": pid, "total": total})
    print(f"  {OKM} Purchase Order {number} created | id {pid} | total ₹{total}")
    await asyncio.sleep(SLEEP)
    return {"id": pid, "total": total}


# ==================================================================
# STEP 7 — invoice from SO + stock reduction check
# ==================================================================
async def step7_invoice(z: ZohoClient, state: dict, loc_id: str, tax_inter: dict[int, str]) -> dict[str, Any]:
    print(head("\n=== STEP 7 — invoice (from SO) + stock reduction ==="))
    number = "DEMO-INV-001"
    if state.get("invoice_id"):
        print(f"  {OKM} invoice exists (reuse): {state['invoice_id']}")
        inv_id = state["invoice_id"]
    else:
        existing = await _find_doc(z, "/invoices", "invoices", "invoice_number", number, "invoice_id")
        if existing:
            inv_id = existing
            state["invoice_id"] = inv_id; save_state(state)
            print(f"  {OKM} invoice exists in Zoho (reuse): {inv_id}")
        else:
            # same Delhi customer -> inter-state -> IGST; plain invoice reduces stock
            lines = _doc_lines(SO_LINES, state, tax_inter)
            payload = {
                "customer_id": state["customer_id"], "date": DEMO_DATE, "invoice_number": number,
                "reference_number": "DEMO-SO-001", "location_id": loc_id, "line_items": lines,
                "notes": "DEMO TEST INVOICE — DELETE AFTER VALIDATION",
            }
            res = (await z.post(z.inventory("/invoices"), json=payload, params=IGNORE_AUTONUM)).get("invoice", {})
            inv_id, total = res.get("invoice_id"), res.get("total")
            state["invoice_id"] = inv_id; save_state(state)
            log_call("POST", "/invoices", "201", {"id": inv_id, "total": total})
            print(f"  {OKM} Invoice {number} created | id {inv_id} | total ₹{total}")
    await asyncio.sleep(SLEEP)

    # A DRAFT invoice does NOT reduce inventory — mark it 'sent' to post the stock
    # movement. Idempotent: a already-sent invoice just returns success.
    try:
        await z.post(z.inventory(f"/invoices/{inv_id}/status/sent"))
        log_call("POST", f"/invoices/{inv_id}/status/sent", "200", "marked sent")
        print(f"  {OKM} Invoice marked SENT (stock movement posted)")
    except ZohoError as e:
        print(f"  {WARNM} could not mark invoice sent: {e}")
    await asyncio.sleep(SLEEP)

    # verify reduction — ABSOLUTE check (idempotent across re-runs): current stock
    # must equal the item's opening stock minus the invoiced qty. Uses opening from
    # DEMO_ITEMS, not a captured 'before', so re-running never double-counts.
    rows, all_ok = [], True
    opening_by_sku = {i["sku"]: i["stock"] for i in DEMO_ITEMS}
    for sku, qty, rate in SO_LINES:
        item = (await z.get(z.inventory(f"/items/{state['items'][sku]}"))).get("item", {})
        actual = float(item.get("stock_on_hand") or 0)
        opening = opening_by_sku[sku]
        expected = opening - qty
        row_ok = abs(actual - expected) < 0.001
        all_ok &= row_ok
        rows.append([demo_name(next(i["name"] for i in DEMO_ITEMS if i["sku"] == sku))[:26],
                     opening, qty, expected, actual, TICK if row_ok else CROSS])
        await asyncio.sleep(SLEEP)
    print(gc.md_table(["Item", "Opening", "Invoiced", "Expected", "Actual", "OK"], rows))
    print(f"\n  {OKM if all_ok else WARNM} Stock reduction: {'all 3 items reduced correctly' if all_ok else 'mismatch — check invoice status (draft vs confirmed)'}")
    return {"invoice_id": inv_id, "all_ok": all_ok}


# ------------------------------------------------------------------ doc lookup
async def _find_doc(z: ZohoClient, endpoint: str, key: str, num_field: str, number: str, id_field: str) -> str | None:
    data = await z.get(z.inventory(endpoint), params={num_field: number})
    for d in data.get(key, []) or []:
        if str(d.get(num_field, "")).strip() == number:
            return d.get(id_field)
    return None


# ==================================================================
# FINAL summary
# ==================================================================
def final_summary(pf: dict, results: dict, state: dict) -> None:
    print(head("\n" + "=" * 64))
    print(head("           METAMORPHOSIS E2E DEMO TEST — RESULTS"))
    print(head("=" * 64))

    def mark(b: bool | None) -> str:
        return ok("PASS") if b else (warn("WARN") if b is None else bad("FAIL"))

    lines = [
        ("Step 0: Pre-flight gate", pf.get("gate_passed")),
        (f"   - OAuth + org ({pf.get('org', {}).get('name', '?')})", pf.get("auth_ok")),
        (f"   - Categories matched ({sum(1 for v in pf.get('categories_matched', {}).values() if v)}/{len(pf.get('categories_matched', {}))})",
         all(pf.get("categories_matched", {}).values()) if pf.get("categories_matched") else None),
        ("   - Location write access", pf.get("location_write_ok")),
        (f"   - 5% GST tax available", 5 in pf.get("tax_rates_available", [])),
        ("Step 1: Items created", results.get("step1", {}).get("count") == 15),
        ("Step 2: Stock verification", results.get("step2", {}).get("all_ok")),
        ("Step 3: Customer created", bool(state.get("customer_id"))),
        ("Step 4: Vendor created", bool(state.get("vendor_id"))),
        ("Step 5: Sales Order", bool(state.get("salesorder_id"))),
        ("Step 6: Purchase Order", bool(state.get("purchaseorder_id"))),
        ("Step 7: Invoice + stock reduction", results.get("step7", {}).get("all_ok")),
    ]
    for label, st in lines:
        print(f"  {mark(st):<20} {label}")
    overall = all(v for _, v in lines if v is not None and not isinstance(v, type(None)))
    # overall = every non-None check truthy
    overall = all((v is True) for _, v in lines if v is not None)
    print(head("\n  OVERALL: " + (ok("PASS") if overall else warn("PARTIAL / SEE NOTES"))))
    if results.get("step1", {}).get("tax_notes"):
        print(warn("  Note: some items are NON-TAXABLE because the org lacks a 5% GST tax."))
        print("        Create GST5 in Zoho -> Settings -> Taxes, then GST will apply on orders/invoices.")

    payload = {"preflight": pf, "results": results, "state": state, "overall": overall}
    with open(RESULT_JSON, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, default=str)
    _write_result_md(lines, overall, pf, results)
    print(f"\n  Saved {RESULT_JSON} and {RESULT_MD}.")


def _write_result_md(lines, overall, pf, results) -> None:
    def m(b): return "PASS" if b is True else ("WARN" if b is None else "FAIL")
    out = ["# Metamorphosis E2E Demo Test — Results", "",
           f"- **Org:** {pf.get('org', {}).get('name')} (GSTIN {pf.get('org', {}).get('gstin')})",
           f"- **Write location:** {pf.get('write_location_id')}",
           f"- **Tax rates available:** {pf.get('tax_rates_available')}",
           f"- **Overall:** {'PASS' if overall else 'PARTIAL'}", "",
           "| Check | Result |", "| --- | --- |"]
    out += [f"| {label.strip()} | {m(st)} |" for label, st in lines]
    if results.get("step1", {}).get("tax_notes"):
        out += ["", "## Tax notes", *[f"- {n}" for n in results["step1"]["tax_notes"]]]
    with open(RESULT_MD, "w", encoding="utf-8") as f:
        f.write("\n".join(out))


# ==================================================================
# CLEANUP
# ==================================================================
async def cleanup(z: ZohoClient, state: dict) -> None:
    print(head("\n=== CLEANUP — remove [DEMO] records ==="))
    # 1. invoice -> void then delete
    for label, iid, void_ep, del_ep in [
        ("invoice", state.get("invoice_id"), "/invoices/{}/status/void", "/invoices/{}"),
        ("salesorder", state.get("salesorder_id"), "/salesorders/{}/status/void", "/salesorders/{}"),
        ("purchaseorder", state.get("purchaseorder_id"), "/purchaseorders/{}/status/cancelled", "/purchaseorders/{}"),
    ]:
        if not iid:
            continue
        try:
            await z.post(z.inventory(void_ep.format(iid)))
        except ZohoError as e:
            print(f"  {WARNM} void {label} {iid}: {e}")
        try:
            await z.delete(z.inventory(del_ep.format(iid)))
            print(f"  {OKM} deleted {label} {iid}")
        except ZohoError as e:
            print(f"  {WARNM} delete {label} {iid}: {e}")
        await asyncio.sleep(SLEEP)

    # 2. contacts
    for label, cid in [("customer", state.get("customer_id")), ("vendor", state.get("vendor_id"))]:
        if not cid:
            continue
        try:
            await z.delete(z.inventory(f"/contacts/{cid}"))
            print(f"  {OKM} deleted {label} {cid}")
        except ZohoError as e:
            # active-txn contacts can't be deleted -> mark inactive
            try:
                await z.post(z.inventory(f"/contacts/{cid}/inactive"))
                print(f"  {WARNM} {label} {cid} had history -> marked inactive")
            except ZohoError as e2:
                print(f"  {WARNM} {label} {cid}: {e2}")
        await asyncio.sleep(SLEEP)

    # 3. items — stock-bearing items can't hard-delete -> inactivate
    for sku, iid in (state.get("items") or {}).items():
        try:
            await z.delete(z.inventory(f"/items/{iid}"))
            print(f"  {OKM} deleted item DEMO-{sku} {iid}")
        except ZohoError:
            try:
                await z.post(z.inventory(f"/items/{iid}/inactive"))
                print(f"  {WARNM} item DEMO-{sku} {iid} had stock -> marked inactive")
            except ZohoError as e2:
                print(f"  {WARNM} item DEMO-{sku} {iid}: {e2}")
        await asyncio.sleep(SLEEP)

    remaining = await _find_demo_items(z)
    active = [r for r in remaining if r.get("status") == "active"]
    print(f"\n  {OKM if not active else WARNM} Cleanup done. Remaining ACTIVE [DEMO] items: {len(active)} "
          f"(total [DEMO] records incl. inactive: {len(remaining)})")


# ==================================================================
# MAIN
# ==================================================================
async def main_async(args: argparse.Namespace) -> int:
    state = load_state()
    try:
        async with ZohoClient() as z:
            if args.cleanup:
                await cleanup(z, state)
                return 0

            pf = await preflight(z)
            state["preflight"] = {k: pf[k] for k in ("gate_passed", "location_write_ok", "write_location_id", "tax_rates_available")}
            save_state(state)

            if not args.run:
                print(warn("\n  Pre-flight only. Re-run with  --run  to execute write steps 1–7 "
                           "(creates [DEMO] records in Zoho)."))
                return 0 if pf["gate_passed"] else 2

            if not pf["gate_passed"]:
                print(bad("\n  Gate BLOCKED — not proceeding to write steps. Fix the blocker above and re-run."))
                return 2

            # build maps for the write steps
            taxes = await gc.fetch_taxes(z)
            tax_map = gc.build_tax_rate_map(taxes)
            tax_intra, tax_inter = build_gst_maps(taxes)  # rate -> group id / single id
            cats = (await z.get(z.inventory("/categories"))).get("categories", []) or []
            cat_by_name = {str(c.get("name", "")).strip().lower(): c for c in cats}
            loc_id = pf["write_location_id"]

            results: dict[str, Any] = {}
            step = args.skip_to
            if step <= 1: results["step1"] = await step1_items(z, tax_map, cat_by_name, loc_id, state)
            else: results["step1"] = {"count": len(state.get("items", {})), "tax_notes": state.get("tax_notes", [])}
            if step <= 2: results["step2"] = await step2_verify(z, state)
            if step <= 3: await step3_customer(z, state)
            if step <= 4: await step4_vendor(z, state)
            if step <= 5: results["step5"] = await step5_so(z, state, loc_id, tax_inter)
            if step <= 6: results["step6"] = await step6_po(z, state, loc_id, tax_intra)
            if step <= 7: results["step7"] = await step7_invoice(z, state, loc_id, tax_inter)

            final_summary(pf, results, state)
            return 0
    except ZohoAuthError as e:
        print(bad(f"\nAUTH FAILED: {e}"))
        print(TOKEN_REGEN_RUNBOOK)
        return 3
    except ZohoError as e:
        print(bad(f"\nSTOPPED on Zoho error: {e}"))
        print(f"  code={getattr(e, 'code', None)} payload={getattr(e, 'payload', None)}")
        save_state(state)
        return 1


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Metamorphosis E2E demo test for Zoho Inventory.")
    ap.add_argument("--run", action="store_true", help="execute write steps 1–7 (default: pre-flight only)")
    ap.add_argument("--cleanup", action="store_true", help="remove [DEMO] records and exit")
    ap.add_argument("--skip-to", type=int, default=1, help="resume from step N using demo_state.json")
    return ap.parse_args()


if __name__ == "__main__":
    sys.exit(asyncio.run(main_async(parse_args())))
