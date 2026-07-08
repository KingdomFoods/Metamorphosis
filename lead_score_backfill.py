"""
lead_score_backfill.py — score existing CRM leads via API using the SAME oracle the
Deluge workflow uses (crm_setup.score_lead), so the founder demo shows K24_Lead_Score
even before the score_lead.dg workflow is deployed in the CRM UI.

Idempotent: only PUTs a lead whose stored score differs from the freshly-computed one.
Sets K24_Lead_Score ONLY (does not touch Description — preserves rep notes / sync
provenance; the live Deluge workflow is what writes the score breakdown to Description).

Usage:
  python lead_score_backfill.py --dry-run     # show score distribution, write nothing
  python lead_score_backfill.py               # backfill all leads whose score changed
"""
from __future__ import annotations

import argparse
import asyncio

import structlog

from crm_setup import score_label, score_lead
from zoho_client import ZohoClient

log = structlog.get_logger("score_backfill")

FIELDS = "id,Company,Last_Name,Business_Type,Estimated_Order_Value,City,Phone,Email,Product_Interest,K24_Lead_Score"


def compute(lead: dict) -> int:
    return int(score_lead({
        "Business_Type": lead.get("Business_Type") or "",
        "Estimated_Order_Value": lead.get("Estimated_Order_Value") or 0,
        "City": lead.get("City") or "",
        "Phone": lead.get("Phone") or "",
        "Email": lead.get("Email") or "",
        "Product_Interest": lead.get("Product_Interest") or "",
        "Company": lead.get("Company") or "",
    })["score"])


async def main(dry_run: bool) -> None:
    async with ZohoClient() as z:
        leads = await z.paginate_crm("Leads", fields=FIELDS, per_page=200)
        dist: dict[str, int] = {}
        changed = 0
        for l in leads:
            new = compute(l)
            old = l.get("K24_Lead_Score")
            old = int(old) if old not in (None, "") else None
            dist[score_label(new)] = dist.get(score_label(new), 0) + 1
            if old == new:
                continue
            changed += 1
            if dry_run:
                continue
            await z.put(z.crm(f"/Leads/{l['id']}"), json={"data": [{"K24_Lead_Score": new}]}, with_org=False)
            await asyncio.sleep(0.7)  # rate limit

        print(f"leads={len(leads)}  would-change={changed if dry_run else 0} "
              f"changed={0 if dry_run else changed}")
        print("score distribution (label -> count):", dist)
        if dry_run:
            print("DRY RUN — nothing written. Re-run without --dry-run to apply.")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    asyncio.run(main(ap.parse_args().dry_run))
