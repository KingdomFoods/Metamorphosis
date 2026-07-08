"""
golive_check.py — PART 3: go-live readiness gate (READ-ONLY).

Prints one PASS/FAIL table for every gate condition and writes golive_readiness.md so
anyone can see at a glance what still blocks LIVE_MODE. It NEVER changes anything:
  - does NOT flip LIVE_MODE
  - does NOT set the org GSTIN or rename the org (UI/CA action — only verified here)
  - does NOT create taxes / items / locations

Gate conditions (see the prompt's PART 3):
  1. Auth OK on .com for the (Kingdom Foods) org.
  2. Org GSTIN is set and equals ZOHO_GSTIN.                 [Blocker 2]
  3. Target location (ZOHO_LOCATION_ID) exists.              [Blocker 3, read-only half]
     Write access is proven separately by verify_location.py (it needs a write).
  4. Items: count with cost price set/missing; GST mapped/missing. [Blocker 1]
  5. Dummy SKU still present (safe-mode anchor).
  6. LIVE_MODE current value.

Run:  python golive_check.py
"""
from __future__ import annotations

import asyncio
import json

import structlog

import golive_common as gc
from zoho_client import TOKEN_REGEN_RUNBOOK, ZohoAuthError, ZohoClient, ZohoError

log = structlog.get_logger("golive_check")

REPORT_MD = "golive_readiness.md"
REPORT_JSON = "golive_readiness_raw.json"


async def run() -> dict:
    checks: list[dict] = []
    blocking: list[str] = []
    next_actions: list[str] = []
    raw: dict = {}

    # ---- 1. AUTH ----------------------------------------------------------
    try:
        async with ZohoClient() as z:
            org = await gc.fetch_org(z)
            raw["org"] = {k: org.get(k) for k in ("organization_id", "name", "gst_no", "tax_reg_no", "currency_code")}
            checks.append({
                "gate": "1. Auth on .com",
                "status": bool(org),
                "detail": f"org={org.get('name')} id={org.get('organization_id')}",
            })

            # ---- 2. GSTIN (Blocker 2) — VERIFY ONLY ---------------------
            found_gstin = gc.org_gstin(org)
            want = gc.norm_gstin(gc.GSTIN)
            gstin_ok = bool(want) and found_gstin == want
            checks.append({
                "gate": "2. Org GSTIN set + matches ZOHO_GSTIN [Blocker 2]",
                "status": gstin_ok,
                "detail": f"org GSTIN={found_gstin or 'UNSET'} expected={want or '(ZOHO_GSTIN unset)'}",
            })
            raw["gstin"] = {"found": found_gstin, "expected": want}
            if not gstin_ok:
                if not want:
                    blocking.append("ZOHO_GSTIN is not set in env — set it to the Kingdom Foods GSTIN.")
                    next_actions.append("Set ZOHO_GSTIN=09AFJPB3153M1ZC in .env.")
                elif not found_gstin:
                    blocking.append("Org has no GSTIN configured.")
                    next_actions.append("UI/CA: Settings -> Organization Profile -> set GSTIN to " + want + " (human action; code never sets it).")
                else:
                    blocking.append(f"Org GSTIN {found_gstin} != expected {want} (entity re-point not done yet).")
                    next_actions.append(f"UI: re-point/rename org to Kingdom Foods and set GSTIN {want}.")

            # ---- 3. LOCATION exists (Blocker 3, read-only half) ---------
            locations = await gc.fetch_locations(z)
            raw["locations"] = [{"location_id": l.get("location_id"), "location_name": l.get("location_name") or l.get("name")} for l in locations]
            loc_match = next((l for l in locations if str(l.get("location_id")) == str(gc.LOCATION_ID)), None) if gc.LOCATION_ID else None
            loc_ok = bool(loc_match)
            checks.append({
                "gate": "3. Target location exists [Blocker 3]",
                "status": loc_ok,
                "detail": (
                    f"ZOHO_LOCATION_ID={gc.LOCATION_ID} -> {loc_match.get('location_name') or loc_match.get('name')}"
                    if loc_ok else f"ZOHO_LOCATION_ID={gc.LOCATION_ID or '(unset)'} not found among {len(locations)} locations"
                ),
            })
            if not loc_ok:
                if not gc.LOCATION_ID:
                    blocking.append("ZOHO_LOCATION_ID unset — the D-39 Sec-59 location id is not configured.")
                    next_actions.append("UI: Settings -> Locations -> create 'D-39, Sector 59' -> set ZOHO_LOCATION_ID in .env.")
                else:
                    blocking.append(f"Location id {gc.LOCATION_ID} not found in the org.")
                    next_actions.append("UI: verify the location exists; correct ZOHO_LOCATION_ID.")
            # write-access is proven by verify_location.py (needs a write; kept out of this read-only gate)
            checks.append({
                "gate": "3b. Location WRITE access",
                "status": None,
                "detail": "run verify_location.py (safe +1/-1 net-zero test) - not checked here (read-only gate)",
            })

            # ---- 4. ITEMS: cost price + GST mapping (Blocker 1) ----------
            items = await gc.fetch_items(z)
            taxes = await gc.fetch_taxes(z)
            tax_map = gc.build_tax_rate_map(taxes)
            raw["tax_rates_available"] = sorted(tax_map.keys())
            # gate on ACTIVE, non-dummy items only (inactive/test items don't block go-live)
            real_items = [
                it for it in items
                if str(it.get("sku", "")).strip() != gc.DUMMY_SKU
                and str(it.get("status", "active")).lower() == "active"
            ]
            with_cost = [it for it in real_items if _num(it.get("purchase_rate")) > 0]
            missing_cost = [it for it in real_items if _num(it.get("purchase_rate")) <= 0]
            with_gst = [it for it in real_items if it.get("tax_id") or (it.get("tax_percentage") not in (None, "", 0))]
            missing_gst = [it for it in real_items if not (it.get("tax_id") or (it.get("tax_percentage") not in (None, "", 0)))]
            items_ok = bool(real_items) and not missing_cost and not missing_gst
            checks.append({
                "gate": "4. Items have cost price + GST mapped [Blocker 1]",
                "status": items_ok if real_items else False,
                "detail": (
                    f"{len(real_items)} real items | cost set {len(with_cost)}, missing {len(missing_cost)} | "
                    f"GST mapped {len(with_gst)}, missing {len(missing_gst)} | tax rates in org: {sorted(tax_map.keys()) or 'NONE'}"
                ),
            })
            raw["items"] = {
                "real_item_count": len(real_items),
                "cost_set": len(with_cost), "cost_missing": len(missing_cost),
                "gst_mapped": len(with_gst), "gst_missing": len(missing_gst),
            }
            if not real_items:
                blocking.append("No real items loaded yet (only the dummy SKU).")
                next_actions.append("Run load_items.py on the filled K24_Zoho_Item_Import_Template.xlsx (dry-run -> 2-item test -> full).")
            else:
                if missing_cost:
                    blocking.append(f"{len(missing_cost)} items missing cost price.")
                    next_actions.append("Fill Cost Price in the template and re-run load_items.py.")
                if missing_gst:
                    blocking.append(f"{len(missing_gst)} items have no GST/tax mapped.")
                    next_actions.append("Ensure org taxes exist for each rate; re-run load_items.py to map GST.")
            if not tax_map:
                blocking.append("Org has NO tax rates configured — GST cannot be mapped to items.")
                next_actions.append("UI/CA: create GST taxes (0/5/12/18/28%) in Settings -> Taxes (code never invents tax ids).")

            # ---- 5. DUMMY SKU present ------------------------------------
            dummy = await gc.find_item_by_sku(z, gc.DUMMY_SKU)
            checks.append({
                "gate": "5. Dummy SKU present (safe-mode anchor)",
                "status": bool(dummy),
                "detail": f"{gc.DUMMY_SKU} {'found' if dummy else 'MISSING'}",
            })
            if not dummy:
                blocking.append(f"Dummy SKU {gc.DUMMY_SKU} missing — safe-mode automation has no anchor.")

    except ZohoAuthError as e:
        checks.append({"gate": "1. Auth on .com", "status": False, "detail": str(e)})
        blocking.append("AUTH FAILED — cannot evaluate the rest of the gate.")
        next_actions.append(TOKEN_REGEN_RUNBOOK)
        return _emit(checks, blocking, next_actions, raw, auth_failed=True)
    except ZohoError as e:
        checks.append({"gate": "(API error)", "status": False, "detail": f"{e} payload={getattr(e, 'payload', None)}"})
        blocking.append(f"API error while evaluating gate: {e}")
        return _emit(checks, blocking, next_actions, raw)

    # ---- 6. LIVE_MODE (report only, never flip) ---------------------------
    checks.append({
        "gate": "6. LIVE_MODE",
        "status": None,
        "detail": f"currently {'true' if gc.LIVE_MODE else 'false'} (safe mode {'OFF' if gc.LIVE_MODE else 'ON'}) - this gate never flips it",
    })

    return _emit(checks, blocking, next_actions, raw)


def _num(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _emit(checks, blocking, next_actions, raw, auth_failed: bool = False) -> dict:
    rows = [[gc.status_icon(c["status"]), c["gate"], c["detail"]] for c in checks]
    table = gc.md_table(["Result", "Gate condition", "Detail"], rows)

    hard = [c for c in checks if c["status"] is False]
    ready = not hard and not auth_failed

    lines = [
        "# Kingdom Foods — Inventory Go-Live Readiness",
        "",
        "_Read-only snapshot. This report never flips LIVE_MODE, sets the GSTIN, or writes data._",
        "",
        table,
        "",
        f"**Overall:** {'READY for LIVE_MODE (all hard gates PASS)' if ready else 'NOT READY — hard gates still failing'}.",
        "",
        "## What's still blocking",
    ]
    lines += [f"- {b}" for b in blocking] or ["- Nothing — all hard gates pass."]
    lines += ["", "## Ordered next actions"]
    lines += [f"{i}. {a}" for i, a in enumerate(dict.fromkeys(next_actions), 1)] or ["1. Flip LIVE_MODE=true only after the prerequisites in GO_LIVE_CHECKLIST.md."]
    lines += [
        "",
        "## The single remaining switch",
        f"- LIVE_MODE is currently **{'true' if gc.LIVE_MODE else 'false'}**. Flip to true ONLY after every hard gate above is PASS "
        "and location WRITE access is confirmed by verify_location.py. This script does not flip it.",
        "",
    ]
    md = "\n".join(lines)

    with open(REPORT_MD, "w", encoding="utf-8") as f:
        f.write(md)
    with open(REPORT_JSON, "w", encoding="utf-8") as f:
        json.dump({"checks": checks, "blocking": blocking, "next_actions": next_actions, "ready": ready, "raw": raw}, f, indent=2, default=str)

    print("\n" + table + "\n")
    print("READY for LIVE_MODE" if ready else "NOT READY — see blocking list below")
    for b in blocking:
        print("  BLOCK:", b)
    print(f"\nWrote {REPORT_MD} and {REPORT_JSON}")
    return {"ready": ready, "checks": checks, "blocking": blocking}


if __name__ == "__main__":
    asyncio.run(run())
