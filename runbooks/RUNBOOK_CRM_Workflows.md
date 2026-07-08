# RUNBOOK — CRM Workflow Rules, Custom Functions & Cadence (Phase 2)

**Audience:** CRM admin
**Automated vs manual:** The **logic** ships as code (`deluge/score_lead.dg`, `deluge/assign_lead.dg`).
**Wiring** them to triggers, and the follow-up/stale-lead cadence rules, are **UI-only** — do them here.
Zoho's API cannot create workflow rules or attach custom functions, so this is a click-path, not code.

Org: Zoho CRM · **.com** · Module **Leads**

---

## A. Install the two custom functions

**Setup → Automation → Actions → Functions → + New Function** (do this twice):

### A1. `K24_score_lead`
1. Name `K24_score_lead`, Module **Leads**, Category **Automation**.
2. **Edit Arguments:** add one argument `leadId` → type **Long** → map to **Lead Id** (`${Leads.Lead Id}`).
3. Paste the body of `deluge/score_lead.dg`. **Save**.

### A2. `K24_assign_lead`
1. Name `K24_assign_lead`, Module **Leads**.
2. Argument `leadId` (Long) → **Lead Id**.
3. Paste `deluge/assign_lead.dg`. **Before saving, edit the `salesOwners` / `seniorRep` IDs**
   to real CRM user IDs (Setup → Users → open each user → the number in the URL). **Save.**

## B. Wire them to triggers

**Setup → Automation → Workflow Rules → + Create Rule**, Module **Leads**:

### B1. Rule "Score Lead"
- **When:** On a record action → **Create** *and* **Edit** (Edit so re-scoring happens on field changes).
- **Condition:** All leads.
- **Instant action:** Function → `K24_score_lead` → pass **Lead Id**. Save.

### B2. Rule "Assign Lead"
- **When:** On a record action → **Create**.
- **Condition:** `Owner` is the default import user *(optional — prevents reassigning manually-owned leads)*.
- **Instant action:** Function → `K24_assign_lead` → pass **Lead Id**. Save.
- Order: let **Score Lead** run before **Assign Lead** (assignment reads `K24_Lead_Score` to route Hot
  leads to the senior rep). Workflow rules on the same trigger run in creation order — create Score first.

## C. Follow-up cadence (Day-0 / Day-3 / Day-7)

The Day-0 task is created by `K24_assign_lead`. Add Day-3 and Day-7 as **time-based** workflow actions:

**Workflow Rules → + Create Rule → "Lead Follow-up Cadence"**, Module **Leads**:
- **When:** Create.
- **Condition:** `Pipeline_Stage` is one of New, Communicated, Qualified (i.e. still open).
- **Add Action → Tasks (scheduled):**
  | Task | Due / Execute after | Subject | Assign to |
  |------|---------------------|---------|-----------|
  | Day-3 follow-up | 3 days after rule trigger | "Day-3 follow-up: ${Leads.Company}" | Lead Owner |
  | Day-7 follow-up | 7 days after rule trigger | "Day-7 follow-up: ${Leads.Company}" | Lead Owner |
- Use **Schedule Actions → "Execute after 3/7 Days"** based on **Created Time**. Save.

> Tasks auto-cancel if the lead leaves the open stages (Zoho removes scheduled actions when the
> rule's condition stops matching), so reps aren't nagged on already-converted leads.

## D. Stale-lead reassignment (14 days no activity)

**Workflow Rules → + Create Rule → "Stale Lead Reassign"**, Module **Leads**:
- **When:** based on a **date/time field** → **Last Activity Time** → **Execute: 14 days after** Last Activity Time.
- **Condition:** `Pipeline_Stage` not in (Deal, Not-Applicable, Future Clients).
- **Instant action:** either
  - **Field Update:** set `Next_Action_Date` = today and email the owner's manager; **or**
  - **Function:** a small reassignment Deluge (clone `K24_assign_lead`, force round-robin to the next rep).
- Recommended v1: **Field Update + Email Alert to manager** ("Lead idle 14 days — reassign?"). Save.

## E. Lead scoring — now vs later (Zia)
Rule-based `K24_score_lead` runs **now** and is fully transparent (breakdown written to Description).
**Zia predictive scoring** needs **≥ 75 converted leads** to train — do **not** block on it. Once the
team has that history: **Setup → Zia → Prediction / Lead Scoring → enable**, then keep `K24_Lead_Score`
as the manual override or retire it. No rework to Phase 2 is required to switch.

---
**Linked:** [RUNBOOK_CRM_Fields.md](RUNBOOK_CRM_Fields.md) · [RUNBOOK_CRM_Blueprint.md](RUNBOOK_CRM_Blueprint.md)
