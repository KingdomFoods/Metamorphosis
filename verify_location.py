"""
verify_location.py — PART 2: confirm the target location + write access (Blocker 3).

Read-mostly. Steps:
  1. GET /inventory/v1/locations (NOT /warehouses) — list all locations with ids + names.
  2. Confirm ZOHO_LOCATION_ID (the D-39 Sec-59 location) exists; STOP if not.
  3. SAFE write-access test: a tiny inventory adjustment on the DUMMY SKU at ZOHO_LOCATION_ID,
     qty +1, dated description (no "->" chars). Success => write access confirmed.
     Permission error => report the exact error + the UI fix.
  4. Never leave residue: DELETE the test adjustment (fallback: post a compensating -1 so
     the net effect is zero). Report clean.

Run:  python verify_location.py
"""
from __future__ import annotations

import asyncio
import json

import structlog

import golive_common as gc
from zoho_client import TOKEN_REGEN_RUNBOOK, ZohoAuthError, ZohoClient, ZohoError

log = structlog.get_logger("verify_location")

REPORT_MD = "location_verify_report.md"
REPORT_JSON = "location_verify_raw.json"

# words that mark a Zoho error as "no write access to this location"
_PERMISSION_HINTS = ("do not have access", "not authorized", "permission", "no access", "associated location")


def _looks_like_permission_error(e: ZohoError) -> bool:
    msg = f"{e} {getattr(e, 'payload', '')}".lower()
    return any(h in msg for h in _PERMISSION_HINTS)


async def _create_adjustment(z: ZohoClient, item_id: str, qty: int, ref: str, desc: str) -> dict:
    payload = {
        "date": gc_today(),
        "reason": "Location write-access test",
        "description": desc,  # dated, no "->" chars
        "adjustment_type": "quantity",
        "reference_number": ref,
        "line_items": [{"item_id": item_id, "location_id": gc.LOCATION_ID, "quantity_adjusted": qty}],
    }
    resp = await z.post(z.inventory("/inventoryadjustments"), json=payload)
    return resp.get("inventory_adjustment", {}) or {}


def gc_today() -> str:
    # Local import keeps golive_common free of datetime; scripts may run in restricted envs.
    from datetime import date
    return date.today().isoformat()


async def run() -> dict:
    out: dict = {"steps": []}
    try:
        async with ZohoClient() as z:
            # -- step 1: list locations ------------------------------------
            locations = await gc.fetch_locations(z)
            out["locations"] = [
                {"location_id": l.get("location_id"), "name": l.get("location_name") or l.get("name"),
                 "is_primary": l.get("is_primary_location")}
                for l in locations
            ]
            print(f"\nLocations in org {z.org_id}:")
            for l in out["locations"]:
                print(f"  {l['location_id']}  {l['name']}{'  [primary]' if l['is_primary'] else ''}")

            # -- step 2: confirm target exists -----------------------------
            if not gc.LOCATION_ID:
                return _stop(out, "ZOHO_LOCATION_ID is not set. Create the D-39 Sector 59 location in the UI "
                                  "(Settings -> Locations) and set ZOHO_LOCATION_ID in .env.")
            match = next((l for l in locations if str(l.get("location_id")) == str(gc.LOCATION_ID)), None)
            if not match:
                return _stop(out, f"ZOHO_LOCATION_ID={gc.LOCATION_ID} not found among {len(locations)} locations. "
                                  "Create it in the UI (Settings -> Locations) or correct the id in .env.")
            loc_name = match.get("location_name") or match.get("name")
            out["target_location"] = {"id": gc.LOCATION_ID, "name": loc_name}
            print(f"\nTarget location OK: {gc.LOCATION_ID}  ({loc_name})")

            # -- need the dummy item to adjust -----------------------------
            dummy = await gc.find_item_by_sku(z, gc.DUMMY_SKU)
            if not dummy:
                return _stop(out, f"Dummy SKU {gc.DUMMY_SKU} not found — cannot run a safe write test. "
                                  "Create it first (app.py ensures it) or run golive_check.py.")
            item_id = dummy["item_id"]

            # -- step 3: SAFE write-access test (+1) -----------------------
            ref = f"LOCWRITE-{gc_today()}"
            desc = f"Write-access test on {loc_name} dated {gc_today()}. Net zero, auto-reversed."
            print(f"\nWrite test: +1 {gc.DUMMY_SKU} at {loc_name} ...")
            adj = {}
            try:
                adj = await _create_adjustment(z, item_id, 1, ref, desc)
            except ZohoError as e:
                if _looks_like_permission_error(e):
                    out["write_access"] = False
                    out["error"] = f"{e} | payload={getattr(e, 'payload', None)}"
                    return _emit(out, write_ok=False, permission=True)
                raise  # other errors: surface verbatim below

            adj_id = adj.get("inventory_adjustment_id")
            out["write_access"] = True
            out["test_adjustment_id"] = adj_id
            print(f"  SUCCESS — write access confirmed (adjustment {adj_id}).")

            # -- step 4: clean up — delete, fallback to compensating -1 ----
            cleaned, cleanup_note = await _cleanup(z, adj_id, item_id, ref, loc_name)
            out["cleanup"] = {"clean": cleaned, "note": cleanup_note}
            print(f"  cleanup: {cleanup_note}")
            return _emit(out, write_ok=True, permission=False)

    except ZohoAuthError as e:
        out["error"] = str(e)
        print("\nAUTH FAILED:", e)
        print(TOKEN_REGEN_RUNBOOK)
        out["auth_failed"] = True
        _write(out)
        return out
    except ZohoError as e:
        out["error"] = f"{e} | payload={getattr(e, 'payload', None)}"
        out["write_access"] = out.get("write_access", "unknown")
        print("\nAPI ERROR:", out["error"])
        _write(out)
        return out


async def _cleanup(z: ZohoClient, adj_id: str | None, item_id: str, ref: str, loc_name: str) -> tuple[bool, str]:
    """Leave no residue: try to DELETE the +1 adjustment; if that's not allowed, post a
    compensating -1 so the net stock effect is zero."""
    if adj_id:
        try:
            await z.delete(z.inventory(f"/inventoryadjustments/{adj_id}"))
            return True, f"deleted test adjustment {adj_id} — clean, no residue."
        except ZohoError as e:
            log.warning("delete_failed_falling_back_to_reverse", adj_id=adj_id, error=str(e))
    # fallback: compensating -1 (net zero)
    try:
        rev = await _create_adjustment(
            z, item_id, -1, f"{ref}-REV",
            f"Reversal of write-access test on {loc_name} dated {gc_today()}. Net zero.",
        )
        rev_id = rev.get("inventory_adjustment_id")
        return False, f"could not delete; posted compensating -1 adjustment {rev_id} (net stock zero, two audit records remain)."
    except ZohoError as e:
        return False, f"CLEANUP FAILED — residue may remain (adj {adj_id}). Error: {e}. Manually reverse in the UI."


def _stop(out: dict, message: str) -> dict:
    out["stopped"] = message
    print("\nSTOP:", message)
    lines = [
        "# Location Write-Access Verification (Blocker 3)",
        "",
        f"**STOPPED:** {message}",
        "",
        "## Locations in org",
        gc.md_table(["Location id", "Name", "Primary"],
                    [[l["location_id"], l["name"], "yes" if l["is_primary"] else ""] for l in out.get("locations", [])]),
        "",
    ]
    with open(REPORT_MD, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    _write(out)
    return out


def _emit(out: dict, *, write_ok: bool, permission: bool) -> dict:
    loc = out.get("target_location", {})
    lines = [
        "# Location Write-Access Verification (Blocker 3)",
        "",
        f"- **Target location:** {loc.get('id')} ({loc.get('name')})",
        f"- **Write access:** {'CONFIRMED' if write_ok else 'FAILED'}",
        "",
        "## Locations in org",
        gc.md_table(["Location id", "Name", "Primary"],
                    [[l["location_id"], l["name"], "yes" if l["is_primary"] else ""] for l in out.get("locations", [])]),
        "",
    ]
    if write_ok:
        lines += [
            f"Safe +1/reverse test on `{gc.DUMMY_SKU}` succeeded — the integration user can write to this location.",
            f"Cleanup: {out.get('cleanup', {}).get('note')}",
        ]
    elif permission:
        lines += [
            "**PERMISSION ERROR — the integration user cannot write to this location.**",
            "",
            f"Exact error:\n\n```\n{out.get('error')}\n```",
            "",
            "**UI fix:** Users -> the integration user -> Location/Branch access -> enable WRITE for "
            f"the D-39 Sec-59 location ({loc.get('id')}). Then re-run verify_location.py.",
        ]
    md = "\n".join(lines) + "\n"
    with open(REPORT_MD, "w", encoding="utf-8") as f:
        f.write(md)
    _write(out)
    if write_ok:
        print(f"\nRESULT: WRITE ACCESS CONFIRMED at location {loc.get('id')} ({loc.get('name')}).")
    elif permission:
        print(f"\nRESULT: WRITE ACCESS DENIED at location {loc.get('id')} ({loc.get('name')}).")
        print("  Exact error:", out.get("error"))
        print("  UI fix: Users -> integration user -> Location/Branch access -> enable WRITE for this location, then re-run.")
    print(f"Wrote {REPORT_MD} and {REPORT_JSON}")
    return out


def _write(out: dict) -> None:
    with open(REPORT_JSON, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2, default=str)


if __name__ == "__main__":
    asyncio.run(run())
