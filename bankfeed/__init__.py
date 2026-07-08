"""
bankfeed — real-time-ish bank -> Zoho Books statement integration for Kingdom Foods.

Pulls transactions from each bank's corporate statement API on a schedule,
normalises them to a canonical shape, de-duplicates, and posts them into the
matching Zoho Books bank account register — no manual CSV upload.

Design (bank-agnostic core, per-bank connectors):

    connector.fetch(since) -> [CanonicalTxn]   # bank-specific: auth, decrypt, map
        -> categorize (rules; unmatched -> suspense)
        -> state.dedup (never post the same line twice)
        -> ZohoBankPoster.post (deposit / transfer_fund, idempotent, safe-mode)

Zoho reality baked in (proven live, see repo memory): the public API has NO
"uncategorised bank feed" primitive. Money-in posts as `deposit` (suspense->bank)
and money-out as `transfer_fund` (bank->suspense); both contra legs must be a
cash/bank-type account. Everything lands in a "Bank Feed Suspense" account for the
CA to reclassify (or for future narration rules to categorise).

Run from the metamorphosis/ directory:  python -m bankfeed.run_feed --help
"""
