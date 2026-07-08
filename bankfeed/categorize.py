"""categorize.py — map a narration to a Zoho account, else fall back to suspense.

v1 is deliberately conservative: it returns None for everything, so every line
parks in the Bank Feed Suspense account and the CA reclassifies. This is the
honest default — auto-categorising real company money to P&L accounts by regex is
a decision for the accountant, not the feed.

To switch a pattern on later, add a rule below AND note the target account must be
compatible with the posting primitive:
  - money-IN  posts as `deposit`      -> from_account_id must be cash/bank type
  - money-OUT posts as `transfer_fund`-> to_account_id must be cash/bank type
Categorising to a real income/expense account needs a different Zoho endpoint
(customer payments / expenses), which is a separate build — see README.
"""
from __future__ import annotations

import re

from .schema import CanonicalTxn

# (compiled pattern, account_id, human label). Empty in v1.
RULES: list[tuple[re.Pattern[str], str, str]] = [
    # Example (disabled): route bank charges automatically once approved.
    # (re.compile(r"\b(CHRG|CHARGES|GST ON)\b", re.I), "<bank_fees_account_id>", "Bank Fees and Charges"),
]


def categorize(txn: CanonicalTxn) -> tuple[str | None, str]:
    """Return (account_id_or_None, label). None => park in suspense."""
    for pat, acct, label in RULES:
        if pat.search(txn.narration):
            return acct, label
    return None, "Bank Feed Suspense"
