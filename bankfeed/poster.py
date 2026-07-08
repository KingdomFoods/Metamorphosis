"""poster.py — post a CanonicalTxn into a Zoho Books bank register, idempotently.

Payloads proven live against org 906246204 (see repo memory):
  money IN  : transaction_type 'deposit'       from_account_id=contra(cash) to_account_id=bank
  money OUT : transaction_type 'transfer_fund'  from_account_id=bank         to_account_id=contra(cash)
The contra leg MUST be a cash/bank-type account; expense/income accounts are
rejected with 11016 "Involved account types are not applicable".
"""
from __future__ import annotations

import asyncio

from zoho_client import ZohoClient, ZohoError  # metamorphosis/ must be on sys.path

from .categorize import categorize
from .schema import CanonicalTxn
from .state import FeedState

RATE_SLEEP = 0.7


class ZohoBankPoster:
    def __init__(self, z: ZohoClient, *, bank_account_id: str, suspense_id: str,
                 state: FeedState, live: bool = False):
        self.z = z
        self.bank_id = bank_account_id
        self.suspense_id = suspense_id
        self.state = state
        self.live = live

    def _payload(self, txn: CanonicalTxn, contra_id: str) -> dict:
        common = {
            "amount": round(txn.amount, 2),
            "date": txn.date,
            "reference_number": (txn.ref or "")[:100],
            "description": (txn.narration or "")[:500],
        }
        if txn.direction == "credit":  # money IN -> deposit into the bank
            return {**common, "transaction_type": "deposit",
                    "from_account_id": contra_id, "to_account_id": self.bank_id}
        # money OUT -> transfer bank -> contra
        return {**common, "transaction_type": "transfer_fund",
                "from_account_id": self.bank_id, "to_account_id": contra_id}

    async def post(self, txn: CanonicalTxn) -> dict:
        """Returns {status: skipped|planned|posted|error, ...}. Idempotent."""
        eid = txn.external_id
        if self.state.seen(txn.bank_code, eid):
            return {"status": "skipped", "reason": "already posted", "eid": eid}

        acct, label = categorize(txn)
        contra_id = acct or self.suspense_id
        payload = self._payload(txn, contra_id)

        if not self.live:
            return {"status": "planned", "eid": eid, "category": label, "payload": payload}

        try:
            res = (await self.z.post(self.z.books("/banktransactions"), json=payload)).get("banktransaction", {})
            tid = res.get("transaction_id")
            self.state.mark(txn.bank_code, eid, tid, txn.date)
            self.state.save()
            await asyncio.sleep(RATE_SLEEP)
            return {"status": "posted", "eid": eid, "txn_id": tid, "category": label}
        except ZohoError as e:
            return {"status": "error", "eid": eid, "error": str(e), "code": getattr(e, "code", None),
                    "payload": payload}
