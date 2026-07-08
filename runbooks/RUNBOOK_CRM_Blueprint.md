# RUNBOOK — CRM Blueprint on the Qualification Pipeline (Phase 2)

**Audience:** CRM admin
**Why UI-only:** Blueprint is built on a visual canvas (states + transitions). There is no API to
create it — this is a click-path, by design. The field it runs on (`Pipeline_Stage`) was already
created by `crm_setup.py`.

Org: Zoho CRM · **.com** · Module **Leads** · Field **`Pipeline_Stage`**

Pipeline: **New → Communicated → Qualified → Knock → Follow-up → Deal**
Common exits (from any state): **Not-Applicable**, **Future Clients**

---

## 1. Create the Blueprint
**Setup → Automation → Blueprint → + Create Blueprint**
- **Module:** Leads
- **Field:** `Pipeline Stage` (`Pipeline_Stage`)
- **Layout:** Standard
- Name: `K24 Lead Qualification`. **Next** → opens the canvas.

## 2. Lay out the states
Drag each picklist value onto the canvas as a state, left→right:
`New` → `Communicated` → `Qualified` → `Knock` → `Follow-up` → `Deal`
Place `Not-Applicable` and `Future Clients` lower as terminal states.

## 3. Draw transitions (linear path)
Connect each state to the next. For every transition set:

| Transition (From → To)        | Mandatory before moving | After-transition automation |
|-------------------------------|-------------------------|-----------------------------|
| New → Communicated            | `Inbound_Source` set; a logged Call/Email | — |
| Communicated → Qualified      | `Business_Type`, `City`, `Estimated_Order_Value` set | run `K24_score_lead` (refresh score) |
| Qualified → Knock             | `K24_Lead_Score` ≥ 40 (Warm+); `Product_Interest` set | create task "Send sample / quote" |
| Knock → Follow-up             | a logged activity (call/visit/sample sent) | set `Next_Action_Date` (mandatory field on transition) |
| Follow-up → Deal              | `Est_Order_Value` confirmed; `Credit_Limit_INR` set | notify owner + manager; create Deal record |

**Per-transition setup (each one):** click the transition → **During** tab → add the mandatory
fields → **After** tab → add automation (Field Update / Task / Function / Email).

## 4. Common Transition for the two exits
On the canvas: **+ Common Transition** (applies from *any* state):
- **"Mark Not-Applicable"** → To: `Not-Applicable`. Mandatory: a **Reason** (add a `Lost_Reason`
  picklist field, or reuse Description) so dead leads are auditable. After: clear `Next_Action_Date`.
- **"Park as Future Client"** → To: `Future Clients`. Mandatory: `Next_Action_Date` (when to revisit).
  After: create a scheduled task on that date.

> A **Common Transition** is exactly the right tool here — it lets a rep bail out to
> Not-Applicable / Future Clients from New, Communicated, Qualified, Knock or Follow-up without
> drawing 5×2 individual arrows.

## 5. After-transition: notify owner + next task (standard pattern)
On the **After** tab of the forward transitions, add:
1. **Email Alert** → template "Lead advanced" → to **Lead Owner**.
2. **Task** → Subject "Next step: ${Leads.Pipeline_Stage}" → Due = `Next_Action_Date` (or +2 days) → Owner = Lead Owner.

## 6. Publish & test
- **Save** each transition, then **Publish** the Blueprint (top-right).
- Open a test lead → the detail page now shows the Blueprint **transition buttons** instead of a free
  `Pipeline_Stage` dropdown. Reps can only move along defined paths — enforcing the process.
- Run `python test_crm.py` afterwards: it sets `Pipeline_Stage="New"` on create, which the Blueprint
  permits as the entry state. (The test deletes its lead, so no residue.)

---
**Linked:** [RUNBOOK_CRM_Fields.md](RUNBOOK_CRM_Fields.md) · [RUNBOOK_CRM_Workflows.md](RUNBOOK_CRM_Workflows.md)
