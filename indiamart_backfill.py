"""
indiamart_backfill.py — one-time historical pull of IndiaMART enquiries into Zoho CRM.

Runs as a CLI (NOT a web endpoint) because IndiaMART's Pull API throttles to ~1
request / 5 minutes per key. Covering N days means several windowed requests spaced
5 minutes apart — minutes-to-hours of wall time, which a Render HTTP request cannot
hold open. So this is a worker script you run once from a shell / one-off Render job.

Reuses indiamart.fetch_window (same API call) and leads.upsert_lead (same dedupe +
scoring + idempotency) — so backfilled leads are indistinguishable from live ones and
re-running is safe (idempotent on Source_Record_Id / mobile).

Usage:
  python indiamart_backfill.py --days 30                 # last 30 days, 7-day windows
  python indiamart_backfill.py --days 90 --window 7 --gap 310
  python indiamart_backfill.py --days 7 --gap 0          # single window, no wait

Env: INDIAMART_API_KEY, ZOHO_* (same as app.py).
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timedelta

import structlog

import indiamart as im
from zoho_client import ZohoClient

log = structlog.get_logger("indiamart_backfill")

# IndiaMART throttle: ~1 request / 5 min. 310s default = 5 min + safety margin.
DEFAULT_GAP_SECONDS = 310
DEFAULT_WINDOW_DAYS = 7


async def run(days: int, window_days: int, gap: int, dry_run: bool) -> dict:
    end = datetime.now()
    start = end - timedelta(days=days)
    windows: list[tuple[datetime, datetime]] = []
    cur = start
    while cur < end:
        nxt = min(cur + timedelta(days=window_days), end)
        windows.append((cur, nxt))
        cur = nxt

    log.info("backfill_plan", days=days, windows=len(windows),
             est_minutes=round((len(windows) - 1) * gap / 60, 1))

    totals = {"windows": len(windows), "fetched": 0, "created": 0, "updated": 0, "noop": 0, "error": 0}
    z = ZohoClient()
    async with z:
        i, attempts, MAX_ATTEMPTS = 0, 0, 3
        while i < len(windows):
            w_start, w_end = windows[i]
            code, leads = await im.fetch_window(w_start, w_end)
            done = False
            if code == 200:
                totals["fetched"] += len(leads)
                for enq in leads:
                    if dry_run:
                        continue
                    try:
                        r = await im._ingest_one(z, enq)
                        totals[r.get("action", "noop")] = totals.get(r.get("action", "noop"), 0) + 1
                    except Exception as exc:  # noqa: BLE001
                        log.error("backfill_lead_failed", error=str(exc))
                        totals["error"] += 1
                log.info("backfill_window_done", window=f"{i+1}/{len(windows)}",
                         range=f"{w_start:%d-%b} to {w_end:%d-%b}", fetched=len(leads))
                i += 1
                attempts = 0
                done = (i >= len(windows))
            else:
                # non-200 = throttle (~1 req/5 min) -> wait the gap and RETRY the same window
                attempts += 1
                log.warning("backfill_retry", window=i + 1, attempt=attempts, code=code)
                if attempts >= MAX_ATTEMPTS:
                    totals["error"] += 1
                    log.error("backfill_window_gave_up", window=i + 1, range=f"{w_start:%d-%b} to {w_end:%d-%b}")
                    i += 1
                    attempts = 0
                    done = (i >= len(windows))
            if not done and gap > 0:
                await asyncio.sleep(gap)  # spacing before the next API call (retry or next window)  # respect the 5-min Pull-API limit
    log.info("backfill_complete", **totals)
    return totals


def main() -> None:
    ap = argparse.ArgumentParser(description="Backfill IndiaMART enquiries into Zoho CRM")
    ap.add_argument("--days", type=int, default=30, help="how many days back (max ~365)")
    ap.add_argument("--window", type=int, default=DEFAULT_WINDOW_DAYS, help="window size in days (IndiaMART max 7)")
    ap.add_argument("--gap", type=int, default=DEFAULT_GAP_SECONDS, help="seconds between windows (throttle=310)")
    ap.add_argument("--dry-run", action="store_true", help="fetch + count only, create nothing")
    args = ap.parse_args()
    totals = asyncio.run(run(min(args.days, 365), min(args.window, 7), max(args.gap, 0), args.dry_run))
    print(totals)


if __name__ == "__main__":
    main()
