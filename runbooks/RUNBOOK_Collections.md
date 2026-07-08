# RUNBOOK — Collections Reminders (Phase 3)

**Audience:** Zoho admin + Finance
**Code:** `deluge/collections_reminder.dg` (buckets + flag + call task; **SAFE MODE = log only**).
**Why UI-wired:** the schedule + the customer-email send toggle are Settings UI actions.

---

## 1. What the function does
- Pulls **overdue** invoices from Zoho Books.
- Buckets them **7 / 15 / 30 days** overdue.
- **Flags any customer with > ₹10,00,000 overdue** (high-risk).
- On exactly day **7 / 15 / 30**: creates an internal **collections call task** and (LIVE only)
  emails the customer a reminder.
- **SAFE MODE (`liveMode=false`): NO customer email is sent** — it only logs + creates the internal
  task. This matches the org-wide "no sends until compliant" rule.

## 2. Install the function
**Setup → Automation → Functions → + New Function** → name `K24_collections_reminder`,
standalone (no module) → paste `deluge/collections_reminder.dg` → replace `ORG_ID` with the Books
connection / org id → **Save**.

## 3. Schedule it (daily)
**Setup → Automation → Schedules → + New Schedule:**
- Name: `Daily Collections Sweep`
- Frequency: **Daily**, ~09:30 IST.
- Function: `K24_collections_reminder`.
- Save & **Activate**.

## 4. Reminder cadence (day 7 / 15 / 30 + call task)
The function already fires the reminder + call task on those exact day counts. To make the **escalation**
visible to Finance, also add a CRM **Tasks view** filtered to subject contains "Collections call",
sorted by priority — high-priority (>₹10L) rows surface first.

## 5. Turn ON customer sends (LIVE only)
After go-live (compliant invoices), set `liveMode = true` in the function so day-7/15/30 reminders are
actually emailed. **Owner: Finance.** Until then it stays log-only.

> Aging analytics (DSO, bucket totals over time) are produced by the repo-root
> `analytics/receivables_aging.py`; this runbook is only the operational reminder loop.

---
**Linked:** [../GO_LIVE_CHECKLIST.md](../GO_LIVE_CHECKLIST.md)
