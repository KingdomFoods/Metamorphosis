"""run_feed.py — orchestrator + CLI. One invocation = one pull cycle; schedule it
externally (Task Scheduler / cron) every N hours for "near real-time".

  python -m bankfeed.run_feed --bank hdfc --mock ../hdfc... .json --since 2026-06-24   # dry-run
  python -m bankfeed.run_feed --bank hdfc --mock ...json --since 2026-06-24 --live      # writes
  python -m bankfeed.run_feed --bank hdfc --since 2026-07-01 --live                     # real API

Safe by default: prints a plan and writes NOTHING unless --live is passed.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys

# ₹ and bank glyphs crash cp1252 Windows consoles — force UTF-8 stdout/stderr.
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    except (AttributeError, ValueError):
        pass

# make `import zoho_client` work when run as a module from metamorphosis/
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from zoho_client import ZohoClient  # noqa: E402

from .config import SUSPENSE_ACCOUNT_ID, zoho_bank_account_id  # noqa: E402
from .connectors import CONNECTORS, MockConnector  # noqa: E402
from .poster import ZohoBankPoster  # noqa: E402
from .state import FeedState  # noqa: E402


def _c(code: str, t: str) -> str:
    return f"\033[{code}m{t}\033[0m" if (sys.stdout.isatty() or os.getenv("FORCE_COLOR") == "1") else t


OK, BAD, WARN, HEAD = (lambda t: _c("32", t)), (lambda t: _c("31", t)), (lambda t: _c("33", t)), (lambda t: _c("1;36", t))


async def run_once(args: argparse.Namespace) -> int:
    state = FeedState()
    connector = (MockConnector(args.mock, bank_code=args.bank) if args.mock
                 else CONNECTORS[args.bank]())
    since = args.since or state.last_date(args.bank) or "1900-01-01"

    print(HEAD(f"\n=== bankfeed :: {args.bank.upper()} :: since {since} :: "
               f"{'LIVE (writing)' if args.live else 'DRY-RUN'} ==="))
    txns = await connector.fetch(since, args.until)
    print(f"  fetched {len(txns)} line(s) from {'mock' if args.mock else 'bank API'}")
    if not txns:
        print(OK("  nothing to post.")); return 0

    async with ZohoClient() as z:
        poster = ZohoBankPoster(z, bank_account_id=zoho_bank_account_id(args.bank),
                                suspense_id=SUSPENSE_ACCOUNT_ID, state=state, live=args.live)
        posted = skipped = planned = errors = 0
        cin = cout = 0.0
        for t in txns:
            r = await poster.post(t)
            st = r["status"]
            if st == "posted":
                posted += 1
            elif st == "skipped":
                skipped += 1
            elif st == "planned":
                planned += 1
            elif st == "error":
                errors += 1
                print(BAD(f"  ERROR {t.date} {t.direction} {t.amount:.2f}: {r['error']} (code {r.get('code')})"))
            if st in ("posted", "planned"):
                if t.direction == "credit":
                    cin += t.amount
                else:
                    cout += t.amount
        state.save()

    verb = "posted" if args.live else "planned"
    print(f"\n  {OK(str(posted)) if args.live else OK(str(planned))} {verb} | "
          f"{WARN(str(skipped))} skipped (already seen) | {BAD(str(errors)) if errors else '0'} errors")
    print(f"  money in ₹{cin:,.2f} | money out ₹{cout:,.2f} | net ₹{cin - cout:,.2f}")
    print(f"  cursor last_date={state.last_date(args.bank)} | total tracked={state.count(args.bank)}")
    if not args.live:
        print(WARN("\n  DRY-RUN — re-run with --live to write these into the Zoho register."))
    return 1 if errors else 0


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Pull a bank statement and post it into Zoho Books.")
    ap.add_argument("--bank", required=True, choices=list(CONNECTORS), help="which bank")
    ap.add_argument("--mock", metavar="PARSED_JSON", help="replay a parsed-statement JSON instead of the live API")
    ap.add_argument("--since", help="ISO date lower bound (default: cursor / all)")
    ap.add_argument("--until", help="ISO date upper bound (optional)")
    ap.add_argument("--live", action="store_true", help="actually write to Zoho (default: dry-run)")
    return ap.parse_args()


if __name__ == "__main__":
    sys.exit(asyncio.run(run_once(parse_args())))
