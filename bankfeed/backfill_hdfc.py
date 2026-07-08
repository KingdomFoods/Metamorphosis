"""backfill_hdfc.py — load one or more HDFC .xls statement exports into Zoho.

HDFC caps each export (rows / date span), so a full history from account-open
usually comes as several files. Pass them all — dedup by content hash makes
overlapping date ranges safe (a line shared by two files posts once).

  python -m bankfeed.backfill_hdfc "../Acct_Statement_*.xls"                  # dry-run
  python -m bankfeed.backfill_hdfc "../stmt1.xls" "../stmt2.xls" --live       # write

Safe by default: prints a plan + per-file reconciliation; writes only with --live.
"""
from __future__ import annotations

import argparse
import asyncio
import glob
import os
import sys

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    except (AttributeError, ValueError):
        pass

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from zoho_client import ZohoClient  # noqa: E402

from .config import SUSPENSE_ACCOUNT_ID, zoho_bank_account_id  # noqa: E402
from .parsers.hdfc_xls import parse_hdfc_xls  # noqa: E402
from .poster import ZohoBankPoster  # noqa: E402
from .schema import CanonicalTxn, credit, debit  # noqa: E402
from .state import FeedState  # noqa: E402

BANK = "hdfc"


def _rows_to_canonical(parsed: dict) -> list[CanonicalTxn]:
    last4 = parsed["account"]["last4"]
    out: list[CanonicalTxn] = []
    for r in parsed["transactions"]:
        if r.get("deposit"):
            out.append(credit(BANK, last4, r["date"], r["deposit"], r.get("narration", ""), r.get("ref", ""), r.get("balance")))
        elif r.get("withdrawal"):
            out.append(debit(BANK, last4, r["date"], r["withdrawal"], r.get("narration", ""), r.get("ref", ""), r.get("balance")))
    return out


async def main(args: argparse.Namespace) -> int:
    files: list[str] = []
    for pat in args.files:
        files.extend(sorted(glob.glob(pat)))
    files = list(dict.fromkeys(files))  # de-dup, keep order
    if not files:
        print("no files matched:", args.files); return 2

    # parse + validate every file first
    all_txns: list[CanonicalTxn] = []
    print(f"=== HDFC backfill :: {len(files)} file(s) :: {'LIVE' if args.live else 'DRY-RUN'} ===")
    for f in files:
        p = parse_hdfc_xls(f)
        v = p["validation"]
        rec = v.get("reconciles")
        tag = "reconciles" if rec else ("NO FOOTER" if v.get("footer") is None else "MISMATCH")
        print(f"  {os.path.basename(f)}: {v['n']} txns | A/C ...{p['account']['last4']} | "
              f"in ₹{v['sum_deposits']:,.2f} out ₹{v['sum_withdrawals']:,.2f} | {tag}")
        if v.get("footer") and not rec:
            print(f"    ⚠ footer says debits {v['footer']['debits']:,.2f} / credits {v['footer']['credits']:,.2f} "
                  f"— parse disagrees, NOT loading this file. Check the export.")
            return 1
        all_txns.extend(_rows_to_canonical(p))

    print(f"  total lines across files: {len(all_txns)}")

    state = FeedState()
    async with ZohoClient() as z:
        poster = ZohoBankPoster(z, bank_account_id=zoho_bank_account_id(BANK),
                                suspense_id=SUSPENSE_ACCOUNT_ID, state=state, live=args.live)
        posted = skipped = planned = errors = 0
        for t in all_txns:
            r = await poster.post(t)
            st = r["status"]
            posted += st == "posted"; skipped += st == "skipped"
            planned += st == "planned"; errors += st == "error"
            if st == "error":
                print(f"  ERROR {t.date} {t.direction} {t.amount:.2f}: {r['error']}")
        state.save()

    verb = "posted" if args.live else "planned"
    print(f"\n  {posted if args.live else planned} {verb} | {skipped} skipped (already loaded) | {errors} errors")
    print(f"  cursor last_date={state.last_date(BANK)} | total tracked={state.count(BANK)}")
    if not args.live:
        print("\n  DRY-RUN — re-run with --live to write.")
    return 1 if errors else 0


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Backfill HDFC .xls statements into Zoho Books.")
    ap.add_argument("files", nargs="+", help="one or more .xls paths or globs")
    ap.add_argument("--live", action="store_true", help="actually write to Zoho (default: dry-run)")
    return ap.parse_args()


if __name__ == "__main__":
    sys.exit(asyncio.run(main(parse_args())))
