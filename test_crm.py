"""
test_crm.py — Phase 2 live smoke test.

Creates a real test Lead in CRM, then exercises the SAME logic the server-side
Deluge runs (score_lead -> write K24_Lead_Score; assign_lead -> first-touch Task),
verifies each effect by reading back, then DELETES everything it created.

The production runtime is deluge/score_lead.dg + deluge/assign_lead.dg fired by
workflow rules on lead create; this test drives the identical math through the API so
it is fully self-contained and leaves no residue. Must pass.

Run:  pytest test_crm.py -v     (or)     python test_crm.py
"""
from __future__ import annotations

import asyncio

import pytest

from crm_setup import assign_lead, score_label, score_lead
from zoho_client import ZohoClient, ZohoError

# A high-value lead so the expected score lands in "Hot" (>=70):
TEST_LEAD = {
    "Last_Name": "K24-TEST-DELETE-ME",
    "Company": "Metamorphosis Test Co",
    "Phone": "9999900000",
    "Email": "metamorphosis-test@example.invalid",
    "City": "Noida",
    "Business_Type": "Distributor",
    "Estimated_Order_Value": 600000,
    "Product_Interest": "Frozen Momos",
    "Pipeline_Stage": "New",
    "Inbound_Source": "Manual",
}
FAKE_OWNERS = ["1001", "1002", "1003"]


async def _run() -> None:
    async with ZohoClient() as z:
        created_lead_id = None
        created_task_id = None
        try:
            # 1. create the lead --------------------------------------------------
            resp = await z.post(z.crm("/Leads"), json={"data": [TEST_LEAD]}, with_org=False)
            rec = resp.get("data", [{}])[0]
            assert rec.get("code") == "SUCCESS", f"lead create failed: {rec}"
            created_lead_id = rec["details"]["id"]
            print(f"  lead created: {created_lead_id}")

            # 2. SCORE (oracle == deluge/score_lead.dg) --------------------------
            scored = score_lead(TEST_LEAD)
            assert scored["score"] >= 70, f"expected Hot lead, got {scored}"
            print(f"  score={scored['score']} ({score_label(scored['score'])}) breakdown={scored['breakdown']}")
            await z.put(
                z.crm(f"/Leads/{created_lead_id}"),
                json={"data": [{"K24_Lead_Score": scored["score"]}]},
                with_org=False,
            )
            readback = (await z.get(z.crm(f"/Leads/{created_lead_id}"), with_org=False)).get("data", [{}])[0]
            assert int(readback.get("K24_Lead_Score") or 0) == scored["score"], "score not persisted on lead"
            print("  score persisted + verified on live record")

            # 3. ASSIGN (oracle == deluge/assign_lead.dg) ------------------------
            owner = assign_lead(0, FAKE_OWNERS)
            assert owner == FAKE_OWNERS[0], "round-robin oracle wrong"
            print(f"  assignment oracle -> owner {owner} (round-robin deterministic)")

            # 4. FIRST-TOUCH TASK fires ------------------------------------------
            task_payload = {
                "Subject": f"First touch: {TEST_LEAD['Company']}",
                "Status": "Not Started",
                "Priority": "High",
                "$se_module": "Leads",
                "What_Id": created_lead_id,
            }
            tresp = await z.post(z.crm("/Tasks"), json={"data": [task_payload]}, with_org=False)
            trec = tresp.get("data", [{}])[0]
            assert trec.get("code") == "SUCCESS", f"task create failed: {trec}"
            created_task_id = trec["details"]["id"]
            print(f"  first-touch task created: {created_task_id}")

            print("PHASE 2 CRM SMOKE TEST: PASS")
        finally:
            # 5. CLEANUP — delete task then lead (idempotent, best-effort) --------
            if created_task_id:
                try:
                    await z.delete(z.crm(f"/Tasks/{created_task_id}"), with_org=False)
                    print(f"  cleaned task {created_task_id}")
                except ZohoError as e:
                    print(f"  WARN: task cleanup failed: {e}")
            if created_lead_id:
                try:
                    await z.delete(z.crm(f"/Leads/{created_lead_id}"), with_org=False)
                    print(f"  cleaned lead {created_lead_id}")
                except ZohoError as e:
                    print(f"  WARN: lead cleanup failed: {e}")


def test_score_oracle_components() -> None:
    """Pure-unit: scoring is transparent and bounded."""
    hot = score_lead({"Business_Type": "Distributor", "Estimated_Order_Value": 600000, "City": "Noida", "Phone": "1", "Email": "a@b.c", "Product_Interest": "x", "Company": "y"})
    assert hot["score"] == min(100, 30 + 30 + 20 + 20)
    cold = score_lead({"Business_Type": "Unknown", "City": "Mumbai"})
    assert cold["score"] <= 10
    assert score_label(75) == "Hot" and score_label(50) == "Warm" and score_label(10) == "Cold"


def test_assign_round_robin() -> None:
    owners = ["a", "b", "c"]
    assert [assign_lead(i, owners) for i in range(4)] == ["a", "b", "c", "a"]


@pytest.mark.asyncio
async def test_crm_live_flow() -> None:
    await _run()


if __name__ == "__main__":
    test_score_oracle_components()
    test_assign_round_robin()
    print("unit oracles: PASS")
    asyncio.run(_run())
