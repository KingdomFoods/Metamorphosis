"""
rep_assignment.py — assign CRM Leads to reps who are NOT licensed Zoho users, + notify them.

The constraint (KNOWN_ISSUES #4): Rashi / Prashant / Manoj are not CRM users, so leads
cannot be OWNED by them (Owner must be a licensed user). We sidestep the license cost with
a plain custom text field `Assigned_Rep`:

  Layer 1  ensure_assigned_rep_field()  create the Assigned_Rep field (idempotent)
  Layer 1  assign_rep()                 round-robin BALANCED by current open-lead count
  Layer 2  notify_cliq()                push the lead to a Zoho Cliq channel (free, no CRM seat)
  Layer 4  build_message()             the alert text (name / company / phone / score / rep)

Wired into the shared leads.upsert_lead create path, so EVERY source (IndiaMART, TailorTalk/
WhatsApp, Shoopy) gets assigned + notified with one hook. Sheet-entered leads are already
"assigned" by the rep's own tab, so Code.gs sets Assigned_Rep directly there.

Why not COQL for the balance count? The refresh token lacks the COQL scope
(OAUTH_SCOPE_MISMATCH, verified live 2026-07-10) — same class as the blocked /users call. So
we count via /Leads/search (works under ZohoCRM.modules.ALL, which leads.py already relies on),
seed once per process, then keep an in-process tally so consecutive assignments stay balanced
despite Zoho's search-index lag.

CLI:
    python rep_assignment.py                 # ensure field + print live per-rep counts (read-only)
    python rep_assignment.py --backfill       # assign every currently-UNASSIGNED lead (no notify)
    python rep_assignment.py --backfill --notify   # ...and fire a Cliq alert per lead (spammy!)

Env:
    CLIQ_WEBHOOK_URL      Zoho Cliq incoming-webhook URL (channel -> integrations). If unset,
                          notification is skipped (assignment still happens).
    REP_PHONE_RASHI / REP_PHONE_PRASHANT / REP_PHONE_MANOJ   optional, shown in the alert.
    ASSIGNMENT_ENABLED    "0" to disable the upsert_lead hook (default on).
"""
from __future__ import annotations

import argparse
import asyncio
import os
from typing import Any

import httpx
import structlog

from zoho_client import ZohoClient, ZohoError

log = structlog.get_logger("rep_assignment")

MODULE = "Leads"
ASSIGNED_REP_FIELD = "Assigned_Rep"
REPS: list[str] = ["Rashi", "Prashant", "Manoj"]

# Stages that mean the lead is CLOSED (don't count toward a rep's live workload).
CLOSED_STAGES = {"Deal", "Not-Applicable"}

REP_PHONES: dict[str, str] = {
    "Rashi": os.getenv("REP_PHONE_RASHI", "").strip(),
    "Prashant": os.getenv("REP_PHONE_PRASHANT", "").strip(),
    "Manoj": os.getenv("REP_PHONE_MANOJ", "").strip(),
}
CLIQ_WEBHOOK_URL = os.getenv("CLIQ_WEBHOOK_URL", "").strip()
ASSIGNMENT_ENABLED = os.getenv("ASSIGNMENT_ENABLED", "1").strip() not in ("0", "false", "no", "")

# ── in-process state (seeded once from CRM, then incremented locally) ──────────
_local_counts: dict[str, int] = {}
_seeded = False
_field_ready = False


# ── Layer 1: field ────────────────────────────────────────────────────────────
async def ensure_assigned_rep_field(z: ZohoClient) -> str:
    """Create the Assigned_Rep text field if missing. Idempotent. Returns the api_name."""
    global _field_ready
    resp = await z.get(z.crm("/settings/fields"), params={"module": MODULE}, with_org=False)
    by_api = {f.get("api_name") for f in resp.get("fields", []) or []}
    if ASSIGNED_REP_FIELD in by_api:
        _field_ready = True
        log.info("assigned_rep_field_exists")
        return ASSIGNED_REP_FIELD
    spec = {"field_label": "Assigned Rep", "data_type": "text", "length": 50}
    await z.post(z.crm("/settings/fields"), json={"fields": [spec]}, params={"module": MODULE}, with_org=False)
    log.info("assigned_rep_field_created")
    _field_ready = True
    return ASSIGNED_REP_FIELD


# ── Layer 1: balanced round-robin ──────────────────────────────────────────────
async def _count_open_leads(z: ZohoClient, rep: str) -> int:
    """Count open (non-closed) leads currently assigned to `rep`, via /Leads/search.

    Search returns 204/empty for zero matches (the client yields {} -> data []). At current
    volume (<200/rep) this is a single page; paginate defensively anyway.
    """
    total, page = 0, 1
    while True:
        try:
            resp = await z.get(
                z.crm(f"/{MODULE}/search"),
                params={
                    "criteria": f"({ASSIGNED_REP_FIELD}:equals:{rep})",
                    "fields": "id,Pipeline_Stage",
                    "page": page,
                    "per_page": 200,
                },
                with_org=False,
            )
        except ZohoError as exc:
            log.warning("count_search_failed", rep=rep, error=str(exc))
            return total
        data = resp.get("data") or []
        total += sum(1 for r in data if (r.get("Pipeline_Stage") not in CLOSED_STAGES))
        if not (resp.get("info") or {}).get("more_records"):
            break
        page += 1
    return total


async def _ensure_seed(z: ZohoClient) -> None:
    """Seed the in-process tally from live CRM counts, once per process."""
    global _seeded
    if _seeded:
        return
    for rep in REPS:
        _local_counts[rep] = await _count_open_leads(z, rep)
    _seeded = True
    log.info("assignment_seeded", counts=dict(_local_counts))


def _pick_rep() -> str:
    """Rep with the fewest open leads (ties -> REPS order, deterministic)."""
    return min(REPS, key=lambda r: (_local_counts.get(r, 0), REPS.index(r)))


def _rep_from_source(source_record_id: Any) -> str | None:
    """Recover the rep from a sheet-synced lead's Source_Record_Id ('Rashi:Row12'). None if
    it isn't a known rep prefix (IndiaMART 'IM:', TailorTalk 'TT:', etc.)."""
    s = str(source_record_id or "")
    prefix = s.split(":", 1)[0].strip()
    return prefix if prefix in REPS else None


async def assign_rep(z: ZohoClient, lead_id: str) -> str:
    """Pick the least-loaded rep, write Assigned_Rep on the lead, bump the local tally."""
    if not _field_ready:
        await ensure_assigned_rep_field(z)
    await _ensure_seed(z)
    rep = _pick_rep()
    await z.put(z.crm(f"/{MODULE}/{lead_id}"), json={"data": [{ASSIGNED_REP_FIELD: rep}]}, with_org=False)
    _local_counts[rep] = _local_counts.get(rep, 0) + 1
    log.info("lead_assigned", lead_id=lead_id, rep=rep, counts=dict(_local_counts))
    return rep


# ── Layer 4: alert text ─────────────────────────────────────────────────────────
def build_message(rep: str, lead: dict[str, Any]) -> str:
    """The rep alert (Cliq/WhatsApp share the copy). `lead` uses friendly keys."""
    name = lead.get("name") or "New lead"
    company = lead.get("company") or "—"
    phone = lead.get("phone") or "—"
    product = lead.get("product") or "—"
    score = lead.get("score")
    city = lead.get("city") or "—"
    source = lead.get("source") or "—"
    lines = [
        f"🔔 New Lead Assigned → {rep}",
        "",
        f"👤 {name}",
        f"🏢 {company}",
        f"📞 {phone}  (tap to call)",
        f"📦 {product}",
    ]
    if score is not None:
        lines.append(f"⭐ Score: {score}")
    lines += [
        f"📍 {city}",
        f"↳ Source: {source}",
        "",
        "Please call within 30 min, then update your Google Sheet.",
    ]
    return "\n".join(lines)


# ── Layer 2: Cliq notification (best-effort) ────────────────────────────────────
async def notify_cliq(rep: str, lead: dict[str, Any]) -> bool:
    """POST the alert to the Zoho Cliq incoming webhook. Never raises — notification
    failure must not fail lead ingestion. Returns True if sent."""
    if not CLIQ_WEBHOOK_URL:
        log.info("cliq_skip_no_url", rep=rep)
        return False
    text = build_message(rep, lead)
    try:
        async with httpx.AsyncClient(timeout=15.0) as http:
            resp = await http.post(CLIQ_WEBHOOK_URL, json={"text": text})
        ok = resp.status_code < 300
        (log.info if ok else log.warning)("cliq_notify", rep=rep, status=resp.status_code)
        return ok
    except Exception as exc:  # noqa: BLE001 — best-effort by design
        log.warning("cliq_notify_failed", rep=rep, error=str(exc))
        return False


async def assign_and_notify(z: ZohoClient, lead_id: str, lead: dict[str, Any], *, notify: bool = True) -> str | None:
    """Full hook used by leads.upsert_lead on CREATE: assign, then (optionally) notify.
    Returns the assigned rep, or None if assignment is disabled/failed (ingestion continues)."""
    if not ASSIGNMENT_ENABLED:
        return None
    rep = await assign_rep(z, lead_id)
    if notify:
        await notify_cliq(rep, lead)
    return rep


# ── CLI: seed / report / backfill ───────────────────────────────────────────────
async def _live_counts(z: ZohoClient) -> dict[str, int]:
    return {rep: await _count_open_leads(z, rep) for rep in REPS}


async def backfill(z: ZohoClient, *, notify: bool) -> dict[str, Any]:
    """Assign every currently-UNASSIGNED lead round-robin, balancing from live counts.

    Idempotent: leads that already carry an Assigned_Rep are skipped. Notification is OFF
    by default (a backfill of hundreds of leads should not blast the channel)."""
    await ensure_assigned_rep_field(z)
    all_leads = await z.paginate_crm(
        MODULE, fields="id,Last_Name,Company,Phone,Product_Interest,City,K24_Lead_Score,Inbound_Source,Assigned_Rep,Pipeline_Stage,Source_Record_Id", per_page=200
    )
    unassigned = [l for l in all_leads if not (l.get(ASSIGNED_REP_FIELD) or "").strip()]
    await _ensure_seed(z)
    log.info("backfill_start", total=len(all_leads), unassigned=len(unassigned), seed=dict(_local_counts))

    assigned = 0
    for l in unassigned:
        lead_id = l["id"]
        # Sheet-synced leads carry their true rep in Source_Record_Id ("Rashi:Row12"). Honour
        # that so backfill never contradicts the sheet; round-robin only source-less leads.
        # (The tally is bumped once, after a successful PUT, for whichever rep we land on.)
        rep = _rep_from_source(l.get("Source_Record_Id")) or _pick_rep()
        try:
            await z.put(z.crm(f"/{MODULE}/{lead_id}"), json={"data": [{ASSIGNED_REP_FIELD: rep}]}, with_org=False)
        except ZohoError as exc:
            log.warning("backfill_assign_failed", lead_id=lead_id, error=str(exc))
            continue
        _local_counts[rep] = _local_counts.get(rep, 0) + 1
        assigned += 1
        if notify:
            await notify_cliq(rep, {
                "name": l.get("Last_Name"), "company": l.get("Company"), "phone": l.get("Phone"),
                "product": l.get("Product_Interest"), "score": l.get("K24_Lead_Score"),
                "city": l.get("City"), "source": l.get("Inbound_Source"),
            })
        await asyncio.sleep(0.15)
    return {"total": len(all_leads), "unassigned": len(unassigned), "assigned": assigned, "final_counts": dict(_local_counts)}


async def _main() -> None:
    ap = argparse.ArgumentParser(description="Assign CRM leads to reps (Assigned_Rep) + Cliq notify.")
    ap.add_argument("--backfill", action="store_true", help="assign all currently-unassigned leads")
    ap.add_argument("--notify", action="store_true", help="fire a Cliq alert per lead during backfill")
    args = ap.parse_args()

    async with ZohoClient() as z:
        await ensure_assigned_rep_field(z)
        print(f"Field: {ASSIGNED_REP_FIELD} ready.  Cliq webhook: {'SET' if CLIQ_WEBHOOK_URL else 'UNSET (notify skipped)'}")
        if args.backfill:
            print("Backfilling unassigned leads (round-robin, balanced)...")
            report = await backfill(z, notify=args.notify)
            print(f"  total={report['total']}  unassigned={report['unassigned']}  assigned={report['assigned']}")
            print(f"  final open-lead counts: {report['final_counts']}")
        else:
            counts = await _live_counts(z)
            print(f"Live open-lead counts per rep: {counts}")
            print(f"Next lead would go to: {min(counts, key=lambda r: (counts[r], REPS.index(r)))}")


if __name__ == "__main__":
    asyncio.run(_main())
