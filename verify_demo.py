"""
verify_demo.py — READ-ONLY live verification for the founder demo.

Pulls evidence from the live Zoho org (.com) and writes demo_verification_raw.json.
It NEVER writes/modifies/deletes anything. No /crm/v6/users, no .in, no GST changes.
"""
from __future__ import annotations

import asyncio
import json
from collections import Counter

from zoho_client import ZohoClient

OUT = "../demo_verification_raw.json"

EXPECTED_CORE = ["Pipeline_Stage", "Inbound_Source", "Product_Interest", "SKU_Interest",
                 "Estimated_Order_Value", "Missed_Call_Flag", "Next_Action_Date", "K24_Lead_Score"]
EXPECTED_AUDIT = ["Source_Record_Id", "Source_Payload"]


def _user(v):
    if isinstance(v, dict):
        return {"name": v.get("name"), "id": v.get("id")}
    return v


async def main() -> None:
    evidence: dict = {}
    async with ZohoClient() as z:
        # --- org / GSTIN (compliance gate) ---
        org = (await z.get(z.inventory("/organizations")))["organizations"][0]
        evidence["org"] = {
            "name": org.get("name"), "organization_id": org.get("organization_id"),
            "gstin": org.get("gst_no") or org.get("tax_reg_no") or None,
            "currency": org.get("currency_code"),
        }

        # --- CRM Leads custom fields ---
        fresp = await z.get(z.crm("/settings/fields"), params={"module": "Leads"}, with_org=False)
        fields = fresp.get("fields", [])
        custom = [{"api_name": f.get("api_name"), "field_label": f.get("field_label"), "data_type": f.get("data_type")}
                  for f in fields if f.get("custom_field")]
        by_api = {f.get("api_name"): f for f in fields}
        evidence["crm_custom_fields"] = custom
        evidence["expected_core_fields"] = {n: (n in by_api) for n in EXPECTED_CORE}
        evidence["expected_audit_fields"] = {n: (n in by_api) for n in EXPECTED_AUDIT}

        # --- Inbound_Source picklist values ---
        src = by_api.get("Inbound_Source", {})
        evidence["inbound_source_values"] = [v.get("actual_value") for v in src.get("pick_list_values", [])]
        evidence["has_website_shoopy_value"] = "Website (Shoopy)" in evidence["inbound_source_values"]

        # --- standard Lead_Source picklist (for grouping the existing leads) ---
        ls = by_api.get("Lead_Source", {})
        evidence["lead_source_picklist"] = [v.get("actual_value") for v in ls.get("pick_list_values", [])]

        # --- Blueprint: report only what the API exposes (do not over-claim) ---
        bp = {"queried": True}
        try:
            r = await z.get(z.crm("/settings/blueprints"), with_org=False)
            bps = r.get("blueprints") or r.get("blueprint") or []
            bp["api_returned"] = bps if isinstance(bps, list) else [bps]
        except Exception as e:
            bp["api_error"] = str(e)[:200]
            bp["note"] = "Blueprint not exposed/created via API on this edition; UI-configured per RUNBOOK_CRM_Blueprint.md"
        evidence["blueprint"] = bp

        # --- ALL Leads with origin fields ---
        leads = await z.paginate_crm(
            "Leads",
            fields="id,Last_Name,Company,Lead_Source,Inbound_Source,Created_Time,Modified_Time,Created_By,Modified_By,Pipeline_Stage,K24_Lead_Score,Source_Record_Id,Email,Phone,Mobile",
        )
        lead_rows = []
        for ld in leads:
            lead_rows.append({
                "id": ld.get("id"),
                "name": ld.get("Last_Name"),
                "company": ld.get("Company"),
                "Lead_Source": ld.get("Lead_Source"),
                "Inbound_Source": ld.get("Inbound_Source"),
                "Created_Time": ld.get("Created_Time"),
                "Modified_Time": ld.get("Modified_Time"),
                "Created_By": _user(ld.get("Created_By")),
                "Source_Record_Id": ld.get("Source_Record_Id"),
            })
        evidence["leads_count"] = len(lead_rows)
        evidence["leads"] = lead_rows

        # groupings / clustering
        evidence["by_lead_source"] = dict(Counter((r["Lead_Source"] or "(empty)") for r in lead_rows))
        evidence["by_inbound_source"] = dict(Counter((r["Inbound_Source"] or "(empty)") for r in lead_rows))
        evidence["by_created_by"] = dict(Counter(((r["Created_By"] or {}).get("name") if isinstance(r["Created_By"], dict) else str(r["Created_By"])) for r in lead_rows))
        # created-date clustering (date only)
        evidence["by_created_date"] = dict(Counter((r["Created_Time"] or "")[:10] for r in lead_rows))
        ctimes = sorted([r["Created_Time"] for r in lead_rows if r["Created_Time"]])
        evidence["created_time_earliest"] = ctimes[0] if ctimes else None
        evidence["created_time_latest"] = ctimes[-1] if ctimes else None

        # --- Tasks (to resolve the "2019 dates" = due dates, not creation) ---
        try:
            tasks = await z.paginate_crm("Tasks", fields="id,Subject,Status,Due_Date,Created_Time,Who_Id,What_Id,$se_module")
        except Exception as e:
            tasks = []
            evidence["tasks_error"] = str(e)[:200]
        task_rows = []
        for t in tasks:
            task_rows.append({
                "id": t.get("id"), "subject": t.get("Subject"), "status": t.get("Status"),
                "Due_Date": t.get("Due_Date"), "Created_Time": t.get("Created_Time"),
                "Who_Id": _user(t.get("Who_Id")), "What_Id": _user(t.get("What_Id")), "se_module": t.get("$se_module"),
            })
        evidence["tasks_count"] = len(task_rows)
        evidence["tasks"] = task_rows
        due_dates = sorted([t["Due_Date"] for t in task_rows if t.get("Due_Date")])
        task_ctimes = sorted([t["Created_Time"] for t in task_rows if t.get("Created_Time")])
        evidence["task_due_earliest"] = due_dates[0] if due_dates else None
        evidence["task_due_latest"] = due_dates[-1] if due_dates else None
        evidence["task_created_earliest"] = task_ctimes[0] if task_ctimes else None
        evidence["task_created_latest"] = task_ctimes[-1] if task_ctimes else None
        evidence["task_due_years"] = dict(Counter((d or "")[:4] for d in due_dates))

        # --- Inventory: dummy SKU ---
        items = (await z.get(z.inventory("/items"), params={"sku": "K24-TEST-001"})).get("items", [])
        if items:
            it = items[0]
            evidence["dummy_sku"] = {
                "exists": True, "sku": it.get("sku"), "item_id": it.get("item_id"),
                "name": it.get("name"), "status": it.get("status"),
                "track_inventory": it.get("track_inventory"),
                "is_batch_tracking_enabled": it.get("is_batch_tracking_enabled"),
                "is_taxable": it.get("is_taxable"),
                "tax_exemption_code": it.get("tax_exemption_code"),
            }
        else:
            evidence["dummy_sku"] = {"exists": False}

    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(evidence, f, indent=2, ensure_ascii=False, default=str)
    print("WROTE", OUT)
    print("leads_count:", evidence["leads_count"], "| tasks_count:", evidence["tasks_count"])
    print("created earliest..latest:", evidence["created_time_earliest"], "..", evidence["created_time_latest"])
    print("task due years:", evidence["task_due_years"])
    print("by_lead_source:", evidence["by_lead_source"])
    print("GSTIN:", evidence["org"]["gstin"])


if __name__ == "__main__":
    asyncio.run(main())
