"""schema.py — the canonical transaction every connector must produce.

One shape, so the poster and dedup logic never care which bank a line came from.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Literal

Direction = Literal["credit", "debit"]  # credit = money IN, debit = money OUT


@dataclass(frozen=True)
class CanonicalTxn:
    bank_code: str            # "hdfc" | "axis" | "boi"
    account_last4: str        # "0996"
    date: str                 # ISO "YYYY-MM-DD" (posting/value date)
    amount: float             # always POSITIVE; direction carries the sign
    direction: Direction      # "credit" (in) or "debit" (out)
    narration: str            # bank narration / description
    ref: str = ""             # bank ref / cheque / UTR number
    balance: float | None = None   # running balance after this line, if the bank gives it

    @property
    def external_id(self) -> str:
        """Stable per-line identity for de-duplication. Bank refs can collide
        (internal TPT refs repeat), so we hash the whole tuple, not just the ref."""
        raw = f"{self.bank_code}|{self.account_last4}|{self.date}|{self.amount:.2f}|{self.direction}|{self.ref}|{self.narration}"
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:20]

    def signed(self) -> float:
        return self.amount if self.direction == "credit" else -self.amount


def credit(bank_code: str, last4: str, date: str, amount: float, narration: str,
           ref: str = "", balance: float | None = None) -> CanonicalTxn:
    return CanonicalTxn(bank_code, last4, date, abs(float(amount)), "credit", narration, ref, balance)


def debit(bank_code: str, last4: str, date: str, amount: float, narration: str,
          ref: str = "", balance: float | None = None) -> CanonicalTxn:
    return CanonicalTxn(bank_code, last4, date, abs(float(amount)), "debit", narration, ref, balance)
