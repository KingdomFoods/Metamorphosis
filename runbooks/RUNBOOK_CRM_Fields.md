# RUNBOOK — CRM Lead Fields (Phase 2)

**Audience:** CRM admin (Babli / sales ops)
**What is automated vs manual:** The fields below are created **by code** (`python crm_setup.py`,
idempotent). This runbook is the **source-of-truth spec** + the manual fallback if you ever need to
recreate one in the UI, and it documents the picklist values the sales team will use.

Org: Kingdom 24 Private Limited · Zoho CRM · DC **.com** · Module **Leads**

---

## 1. Fields created by `crm_setup.py` (verify they exist)

Run `python crm_setup.py` — it prints `label -> api_name` and is safe to re-run. After it runs,
confirm in **Setup → Customization → Modules and Fields → Leads**:

| Field label             | API name (use in Deluge/automation) | Type      | Picklist values |
|-------------------------|-------------------------------------|-----------|-----------------|
| Pipeline Stage          | `Pipeline_Stage`                    | Picklist  | New, Communicated, Qualified, Knock, Follow-up, Deal, Not-Applicable, Future Clients |
| Inbound Source          | `Inbound_Source`                    | Picklist  | Phone, WhatsApp, Website, Instamart, Meta Ads, Missed Call, Manual, IndiaMart |
| Product Interest        | `Product_Interest`                  | Text(255) | — |
| SKU Interest            | `SKU_Interest`                      | Text(255) | — |
| Estimated Order Value   | `Estimated_Order_Value`             | Currency  | — |
| Missed Call Flag        | `Missed_Call_Flag`                  | Checkbox  | — |
| Next Action Date        | `Next_Action_Date`                  | Date      | — |
| K24 Lead Score          | `K24_Lead_Score`                    | Number    | written by `score_lead` |

> **Naming note:** labels are prefixed/worded to dodge Zoho reserved display names. A bare
> "Source", "Stage", or "Lead Score" collides with system fields and the API rejects it
> (`System keyword not allowed in field label`). Hence `Inbound Source`, `Pipeline Stage`,
> `K24 Lead Score`. **The funnel channel field is `Inbound_Source`** — Phase 1 maps onto it.

## 2. Pre-existing fields the pipeline reuses (do NOT recreate)

| Field                | API name            | Notes |
|----------------------|---------------------|-------|
| City                 | `City`              | standard — used by `score_lead` proximity component |
| Business Type        | `Business_Type`     | custom, already present (Hotel, Restaurant, QSR, Caterer, Cloud Kitchen, Distributor, Institutional) |
| Lead Source          | `Lead_Source`       | standard Zoho field — **leave as-is**; use `Inbound_Source` for the K24 funnel |
| Monthly Volume Kg    | `Monthly_Volume_Kg` | custom, present |
| Storage Capability   | `Storage_Capability`| custom, present |
| City Zone            | `City_Zone`         | custom, present |
| FSSAI License        | `FSSAI_License`     | custom, present |
| Credit Limit INR     | `Credit_Limit_INR`  | custom, present |

## 3. Manual creation fallback (only if recreating one by hand)

**Setup → Customization → Modules and Fields → Leads → (drag a field type onto the layout)**
1. Pick the field type from the left tray (Pick List, Single Line, Currency, Number, Date, Checkbox).
2. Set the **Field Label** exactly as in the table (the API name auto-derives — keep it matching).
3. For picklists, add the values from the table, top to bottom, in that order.
4. **Save** the field, then **Save** the layout.

## 4. Add the score breakdown to the Lead detail view (optional, recommended)
The `score_lead` Deluge writes a one-line breakdown into **Description**. Make sure Description is on
the Leads layout so reps can see *why* a lead scored what it did.

---
**Linked runbooks:** [RUNBOOK_CRM_Workflows.md](RUNBOOK_CRM_Workflows.md) (rules that call the Deluge),
[RUNBOOK_CRM_Blueprint.md](RUNBOOK_CRM_Blueprint.md) (stage process on `Pipeline_Stage`).
