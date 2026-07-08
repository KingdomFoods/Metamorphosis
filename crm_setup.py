"""
crm_setup.py — Phase 2 CRM pipeline setup (API-driven parts).

Creates the Lead custom fields the sales pipeline needs, IDEMPOTENTLY (re-runnable).
Anything the v6 API cannot create (Blueprint, workflow rules, stale-lead reassignment)
is documented as exact click-paths in the RUNBOOK_CRM_*.md files — this script never
fakes those.

It also defines the transparent rule-based `score_lead()` and round-robin `assign_lead()`
logic in plain Python so test_crm.py can assert on them. The SAME logic ships as Deluge
(deluge/score_lead.dg, deluge/assign_lead.dg) that runs server-side on lead create — the
Python here is the spec + test oracle, the Deluge is the runtime.

Run:  python crm_setup.py
"""
from __future__ import annotations

import asyncio
from typing import Any

import structlog

from zoho_client import ZohoClient, ZohoError

log = structlog.get_logger("crm_setup")

MODULE = "Leads"

# ---------------------------------------------------------------------------
# Desired custom fields. Labels are chosen to avoid Zoho reserved/system display
# names (e.g. a bare "Source"/"Stage"/"Lead Score" collides). The resulting
# api_name is shown so Deluge + app.py reference the right name.
#   label                     -> api_name (v6 auto-derives)        type
# ---------------------------------------------------------------------------
DESIRED_FIELDS: list[dict[str, Any]] = [
    {
        "field_label": "Pipeline Stage",        # -> Pipeline_Stage
        "data_type": "picklist",
        "pick_list_values": [
            {"display_value": v, "actual_value": v}
            for v in ["New", "Communicated", "Qualified", "Knock", "Follow-up", "Deal", "Not-Applicable", "Future Clients"]
        ],
    },
    {
        "field_label": "Inbound Source",        # -> Inbound_Source  (the funnel channel; Phase-1 maps onto this)
        "data_type": "picklist",
        "pick_list_values": [
            {"display_value": v, "actual_value": v}
            for v in ["Phone", "WhatsApp", "Website", "Instamart", "Meta Ads", "Missed Call", "Manual", "IndiaMart"]
        ],
    },
    {"field_label": "Product Interest", "data_type": "text", "length": 255},     # -> Product_Interest
    {"field_label": "SKU Interest", "data_type": "text", "length": 255},         # -> SKU_Interest
    {"field_label": "Estimated Order Value", "data_type": "currency"},           # -> Estimated_Order_Value
    {"field_label": "Missed Call Flag", "data_type": "boolean"},                 # -> Missed_Call_Flag
    {"field_label": "Next Action Date", "data_type": "date"},                    # -> Next_Action_Date
    # K24 Lead Score (api: K24_Lead_Score, integer) is created on first run too:
    {"field_label": "K24 Lead Score", "data_type": "integer"},                  # -> K24_Lead_Score
    # Lead-source integration (Shoopy / WhatsApp / future funnels):
    {"field_label": "Source Record Id", "data_type": "text", "length": 120},    # -> Source_Record_Id (external id; idempotency + dedupe search)
    {"field_label": "Source Payload", "data_type": "textarea", "textarea": {"type": "large"}},  # -> Source_Payload (raw inbound payload, audit)
]

# Extra picklist VALUES to guarantee on existing fields (added incrementally via PATCH).
# Funnel channels map onto Inbound_Source; "Website (Shoopy)" is the Shoopy lead channel.
REQUIRED_PICKLIST_VALUES: dict[str, list[str]] = {
    "Inbound_Source": ["Phone", "WhatsApp", "Website", "Website (Shoopy)", "Instamart", "Meta Ads", "Missed Call", "Manual", "IndiaMart"],
}

# Standard / pre-existing fields the pipeline also relies on (NOT created here):
#   City (standard), Business_Type (custom, exists), Lead_Source (standard),
#   Monthly_Volume_Kg / Storage_Capability / City_Zone / FSSAI_License / Credit_Limit_INR (exist).


async def ensure_fields(z: ZohoClient) -> dict[str, str]:
    """Create any missing DESIRED_FIELDS. Returns {field_label: api_name} for all of them."""
    resp = await z.get(z.crm("/settings/fields"), params={"module": MODULE}, with_org=False)
    existing = resp.get("fields", []) or []
    by_label = {f.get("field_label"): f for f in existing}
    result: dict[str, str] = {}

    for spec in DESIRED_FIELDS:
        label = spec["field_label"]
        if label in by_label:
            api = by_label[label].get("api_name")
            result[label] = api
            log.info("field_exists", label=label, api_name=api)
            continue
        try:
            created = await z.post(z.crm("/settings/fields"), json={"fields": [spec]}, params={"module": MODULE}, with_org=False)
            detail = (created.get("fields", [{}])[0] or {}).get("details", {})
            api = detail.get("api_name", "(re-read needed)")
            result[label] = api
            log.info("field_created", label=label, api_name=api)
        except ZohoError as exc:
            log.error("field_create_failed", label=label, error=str(exc), payload=exc.payload)
            result[label] = f"FAILED: {exc}"
        await asyncio.sleep(0.3)

    # Re-read to resolve any api_names we couldn't get from the create response
    resp2 = await z.get(z.crm("/settings/fields"), params={"module": MODULE}, with_org=False)
    by_label2 = {f.get("field_label"): f.get("api_name") for f in resp2.get("fields", []) or []}
    for label in result:
        if by_label2.get(label):
            result[label] = by_label2[label]
    return result


async def ensure_picklist_values(z: ZohoClient) -> None:
    """Idempotently add any missing values to existing picklist fields (e.g. 'Website (Shoopy)').

    v6 adds incrementally via PATCH — send ONLY the new values (existing ones raise DUPLICATE_DATA).
    """
    resp = await z.get(z.crm("/settings/fields"), params={"module": MODULE}, with_org=False)
    by_api = {f.get("api_name"): f for f in resp.get("fields", []) or []}
    for api_name, wanted in REQUIRED_PICKLIST_VALUES.items():
        fld = by_api.get(api_name)
        if not fld:
            log.warning("picklist_field_absent", api_name=api_name)
            continue
        have = {v.get("actual_value") for v in fld.get("pick_list_values", [])}
        missing = [v for v in wanted if v not in have]
        if not missing:
            log.info("picklist_values_ok", api_name=api_name)
            continue
        new_vals = [{"actual_value": v, "display_value": v} for v in missing]
        try:
            await z.patch(
                z.crm(f"/settings/fields/{fld['id']}"),
                params={"module": MODULE},
                json={"fields": [{"pick_list_values": new_vals}]},
                with_org=False,
            )
            log.info("picklist_values_added", api_name=api_name, added=missing)
        except ZohoError as exc:
            log.error("picklist_values_failed", api_name=api_name, error=str(exc), payload=exc.payload)


# ===========================================================================
# Lead scoring  — transparent, rule-based (0..100). Mirrors deluge/score_lead.dg.
# ===========================================================================
BUSINESS_TYPE_WEIGHTS = {
    "Distributor": 30, "QSR": 25, "Hotel": 22, "Cloud Kitchen": 20,
    "Restaurant": 18, "Caterer": 15, "Institutional": 20,
}
NEAR_CITIES = {"Noida", "Greater Noida", "Ghaziabad", "Delhi"}      # delivery-proximate to Sector 68
NCR_CITIES = {"Gurgaon", "Gurugram", "Faridabad"}


def score_lead(lead: dict[str, Any]) -> dict[str, Any]:
    """Return {'score': int, 'breakdown': {...}} from Business_Type + Est value + city + completeness.

    Pure function (no I/O) so it is the test oracle for the Deluge of the same name.
    Accepts api_name keys: Business_Type, Estimated_Order_Value, City, Phone, Email,
    Product_Interest. Missing keys score 0 for that component.
    """
    bd: dict[str, int] = {}

    btype = (lead.get("Business_Type") or "").strip()
    bd["business_type"] = BUSINESS_TYPE_WEIGHTS.get(btype, 0)

    try:
        est = float(lead.get("Estimated_Order_Value") or 0)
    except (TypeError, ValueError):
        est = 0.0
    bd["order_value"] = 30 if est >= 500000 else 22 if est >= 200000 else 14 if est >= 50000 else 6 if est > 0 else 0

    city = (lead.get("City") or "").strip()
    bd["city_proximity"] = 20 if city in NEAR_CITIES else 12 if city in NCR_CITIES else 4 if city else 0

    completeness = 0
    for fld, pts in (("Phone", 6), ("Email", 6), ("Product_Interest", 4), ("Company", 4)):
        if str(lead.get(fld) or "").strip():
            completeness += pts
    bd["completeness"] = completeness

    score = min(100, sum(bd.values()))
    return {"score": score, "breakdown": bd}


def score_label(score: int) -> str:
    return "Hot" if score >= 70 else "Warm" if score >= 40 else "Cold"


# ===========================================================================
# Assignment — round-robin oracle (Deluge does the real assignment server-side,
# since /crm/v6/users needs ZohoCRM.users.READ which is intentionally NOT in our
# OAuth scope set). This Python mirror lets test_crm assert deterministic behaviour.
# ===========================================================================
def assign_lead(lead_index: int, owner_ids: list[str]) -> str:
    if not owner_ids:
        raise ValueError("no sales owners configured")
    return owner_ids[lead_index % len(owner_ids)]


async def main() -> None:
    print("=" * 70)
    print("PHASE 2 — CRM field setup (idempotent)")
    print("=" * 70)
    async with ZohoClient() as z:
        # auth proof first
        org = (await z.get(z.inventory("/organizations"))).get("organizations", [{}])[0]
        print(f"Org: {org.get('name')} ({org.get('organization_id')})  DC=.com")
        mapping = await ensure_fields(z)
        print("\nField label -> api_name:")
        for label, api in mapping.items():
            print(f"  {label:<26} -> {api}")
        await ensure_picklist_values(z)
        print("\nInbound_Source picklist values ensured (incl. 'Website (Shoopy)').")
    print("\nNote: Blueprint, workflow rules and stale-lead reassignment are UI-only —")
    print("see RUNBOOK_CRM_Blueprint.md and RUNBOOK_CRM_Workflows.md.")


if __name__ == "__main__":
    asyncio.run(main())
