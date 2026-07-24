"""tailortalk_import.py — bulk-push a TailorTalk Leads export into Zoho CRM via the API.

The fallback for when the webhook is unreliable (it currently 500s on accented names / em-dashes).
Direct API push runs locally with full UTF-8, so it handles every character the live webhook chokes on.

Reuses the EXACT mapping the webhook uses (tailortalk.tailortalk_to_lead_kwargs → leads.upsert_lead),
so imported leads are identical to webhook-created ones and DEDUPE against them (upsert on mobile +
external_id "TT:<id>"). Safe to re-run.

Usage:
    python tailortalk_import.py export.csv --dry-run     # parse + show, write nothing
    python tailortalk_import.py export.csv               # push to CRM
    python tailortalk_import.py export.json              # JSON list or {"data":[...]} also accepted

Column/key aliases (case-insensitive) — adjust ALIASES if your export headers differ:
    name/lead_name · phone/mobile/contact/lead_contact · id/lead_id · summary/chat_summary
    buyer_type/business_type · city · product/product_category · status/lead_status · email
"""
from __future__ import annotations

import asyncio
import csv
import json
import sys

from dotenv import load_dotenv

import leads as leadsvc
from tailortalk import tailortalk_to_lead_kwargs
from zoho_client import ZohoClient, ZohoAuthError

load_dotenv()

# export header (lowercased) -> the key tailortalk_to_lead_kwargs expects inside "data"
ALIASES = {
    "name": "lead_name", "lead_name": "lead_name", "full_name": "lead_name",
    "phone": "lead_contact", "mobile": "lead_contact", "contact": "lead_contact",
    "lead_contact": "lead_contact", "whatsapp": "lead_contact", "number": "lead_contact",
    "id": "id", "lead_id": "id",
    "summary": "chat_summary", "chat_summary": "chat_summary", "notes": "chat_summary",
    "buyer_type": "buyer_type", "business_type": "buyer_type", "type": "buyer_type",
    "city": "city",
    "product": "product_category", "product_category": "product_category", "interest": "product_category",
    "status": "lead_status", "lead_status": "lead_status",
    "email": "email",
}


def load_rows(path: str) -> list[dict]:
    if path.lower().endswith(".json"):
        data = json.load(open(path, encoding="utf-8"))
        if isinstance(data, dict):
            data = data.get("data") or data.get("leads") or []
        return list(data)
    with open(path, newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def to_data(row: dict) -> dict:
    """Map an export row to the TailorTalk webhook 'data' shape via the alias table."""
    out: dict = {}
    for k, v in row.items():
        if v in (None, ""):
            continue
        key = ALIASES.get(str(k).strip().lower())
        if key and key not in out:
            out[key] = v
    return out


async def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    dry = "--dry-run" in sys.argv
    if not args:
        print(__doc__)
        return
    rows = load_rows(args[0])
    print(f"Loaded {len(rows)} rows from {args[0]}{'  (DRY RUN)' if dry else ''}")

    mapped = []
    for i, row in enumerate(rows):
        data = to_data(row)
        if not data.get("lead_contact"):
            print(f"  row {i}: SKIP — no phone/contact ({dict(list(row.items())[:3])})")
            continue
        body = {"data": data}
        kw = tailortalk_to_lead_kwargs(body)
        mapped.append((kw, body))
    print(f"{len(mapped)} rows have a contact and will be pushed.")

    if dry:
        for kw, _ in mapped[:10]:
            print(f"  → {kw.get('last_name')!r:28} {kw.get('mobile')!r:16} bt={kw.get('business_type')}")
        print("  (dry run — nothing written)")
        return

    try:
        async with ZohoClient() as z:
            created = updated = noop = failed = 0
            for kw, body in mapped:
                try:
                    res = await leadsvc.upsert_lead(z, inbound_source="WhatsApp", raw_payload=body, **kw)
                    a = res.get("action")
                    created += a == "created"; updated += a == "updated"; noop += a == "noop"
                except Exception as e:  # noqa: BLE001
                    failed += 1
                    print(f"  FAIL {kw.get('mobile')}: {type(e).__name__}: {e}")
    except ZohoAuthError as e:
        print(f"\n❌ AUTH FAILED — refresh the OAuth token.\n{e}")
        return

    print(f"\nDone: {created} created, {updated} updated, {noop} unchanged, {failed} failed.")


if __name__ == "__main__":
    asyncio.run(main())
