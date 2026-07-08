"""state.py — durable cursor + de-dup store so a re-run never double-posts.

One JSON file (bankfeed_state.json) keyed by bank_code:
  { "hdfc": { "seen": { external_id: zoho_txn_id }, "last_date": "2026-07-06" } }
"""
from __future__ import annotations

import json
import os
from typing import Any

STATE_FILE = os.getenv("BANKFEED_STATE_FILE", "bankfeed_state.json")


class FeedState:
    def __init__(self, path: str = STATE_FILE):
        self.path = path
        try:
            with open(path, encoding="utf-8") as f:
                self._d: dict[str, Any] = json.load(f)
        except (OSError, ValueError):
            self._d = {}

    def _bank(self, code: str) -> dict[str, Any]:
        return self._d.setdefault(code, {"seen": {}, "last_date": None})

    def seen(self, code: str, external_id: str) -> bool:
        return external_id in self._bank(code)["seen"]

    def mark(self, code: str, external_id: str, zoho_txn_id: str | None, date: str) -> None:
        b = self._bank(code)
        b["seen"][external_id] = zoho_txn_id
        if not b["last_date"] or date > b["last_date"]:
            b["last_date"] = date

    def last_date(self, code: str) -> str | None:
        return self._bank(code)["last_date"]

    def count(self, code: str) -> int:
        return len(self._bank(code)["seen"])

    def save(self) -> None:
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(self._d, f, indent=2, default=str)
