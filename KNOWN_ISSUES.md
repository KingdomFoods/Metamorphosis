# Kingdom Foods × Zoho — Known Issues Register (2026-07-08)

Live-verified issues + workarounds. Severity: 🔴 blocks correct go-live · 🟡 fix soon · ⚪ hygiene.

| # | Sev | Issue | Impact | Workaround / Fix | Owner |
|---|-----|-------|--------|------------------|-------|
| 1 | 🔴 | **Org GSTIN UNSET** (`gst_no=None`, `gst=False`) | Invoices can't carry a GSTIN; GST may not compute as registered | UI: Settings → Org Profile/Tax → set **`09AFJPB3153M1ZC`** (Kingdom Foods) + mark GST-registered. **NOT** `09AAJCK4455F1ZC` (K24 Pvt Ltd) | Babli (Super Admin) |
| 2 | 🔴 | **Lead scoring workflow not deployed** | `score_lead.dg` never ran (0/37 leads scored natively) | Backfilled via API today; deploy the workflow (RUNBOOK_CRM_Workflows §A–B) for it to run on new leads | Divyanshu |
| 3 | 🟡 | **All leads score "Cold"** | `Business_Type` empty on every lead (30-pt driver) | Capture Business_Type at intake (add sheet column + map in Code.gs; add to IndiaMART/Shoopy) | Divyanshu |
| 4 | 🟡 | **Reps not licensed CRM users** | Leads can't be *owned* by Rashi/Prashant (only Deepak S, Vishal Kaushal, Manjubhat7 exist) | License reps as CRM users, or leave owned by API user + rep name in `Assigned_Rep` + Description | Founder/Admin |
| 5 | 🟡 | **Refresh token lacks `ZohoCRM.users.READ`** | `GET /users` → OAUTH_SCOPE_MISMATCH; can't map rep→owner id programmatically | Regen token with users.READ scope, or fill OWNER_MAP from Setup→Users UI | Divyanshu |
| 6 | 🟡 | **4 synced Rashi leads malformed** (col-shift bug) | ANKUR/PRIME plus test rows Cosmos/Divyan have wrong fields | Re-run corrected `bulkSyncAllTabs` (fixes them via col-L id); then delete Cosmos/Divyan row+lead | Divyanshu |
| 7 | ⚪ | **Token spans multiple orgs** | `.com/inventory/organizations[0]` = "Agro Nexus Private Limited"; wrong-org risk if org_id unpinned | Always pass `organization_id=906246204` (all our code does) | — |
| 8 | ⚪ | **Location MMR/D-39 write-blocked** (error 400040) | Can't write inventory to that location | Use "Office" location (write-access confirmed) | — |
| 9 | ⚪ | **`/Leads/search` index lag** | Dedup returns 0 right after create | In-process cache (leads.py) + CRM-ID writeback (sheet col L) | — |
| 10 | ⚪ | **2 orphan frozen SKUs** hold stock | `K24-FRZ-SPRLL-1KG` (30u), `K24-FRZ-MOMO-1KG` (50u), inactive | Human decision: add to catalog, or zero-stock + delete | Trilok/Divyanshu |
| 11 | ⚪ | **HDFC feed re-auth** | Yodlee/aggregator re-auth every ~90 days | Note in ops runbook; re-auth when feed stalls | Finance |

## Adding / removing a sales rep — it is a FOUR-place change
Three independent code paths assign `Assigned_Rep`, and a fourth holds the leads already
assigned. Miss one and leads keep flowing to a rep who no longer works them:

1. `rep_assignment.py` → `REPS`  — IndiaMART / Shoopy / anything via `leads.upsert_lead`
2. `sheet_sync/Code.gs` → `CONFIG.REP_TABS` — sheet-entered leads (also drives pull + dashboard)
3. `deluge/salesiq_lead_create.dg` → `reps` — WhatsApp / SalesIQ. **Runs inside Zoho**, so
   editing the repo file is not enough — it must be re-pasted into the Zoho Deluge editor.
4. Existing CRM leads — `python rep_assignment.py --reassign-from <rep> --apply` moves them
   to the active reps (dry-run without `--apply`). `test_metamorphosis.py` now fails on any
   lead still assigned outside `ACTIVE_REPS`.

Manoj was removed this way on 2026-07-13. Note `Source_Record_Id` ("Manoj:Row12") is historical
provenance and is deliberately **not** rewritten.

## Data-centre facts (settled, to stop future churn)
- **CRM, Inventory, Books, OAuth: ALL on `.com`.** `.in` fails (OAuth `invalid_client`,
  inventory 401). The recurring ".in for India DC" assumption is wrong for this org.
- Correct invoicing entity is the **Kingdom Foods proprietorship**, GSTIN `09AFJPB3153M1ZC`.
