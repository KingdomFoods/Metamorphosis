"""connectors.py — one class per bank; each turns a statement API into CanonicalTxns.

The core pipeline is bank-agnostic: it only ever sees CanonicalTxn. All the
bank-specific mess (OAuth, mTLS, payload encryption, JSON shape) is isolated here.

  BankConnector (ABC)
    ├─ MockConnector         — fully working; replays a parsed statement JSON (for E2E tests)
    └─ RestStatementConnector — generic OAuth2-client-credentials + GET-statement + row map
         ├─ HdfcConnector    — corporate API; fill env + confirm encryption/field map
         ├─ AxisConnector    — corporate API; fill env + confirm encryption/field map
         └─ BoiConnector     — API availability uncertain; may stay on manual import

REQUIRED per real bank (from the bank's API doc + your enrolment), via .env:
  <PFX>_API_BASE, <PFX>_TOKEN_URL, <PFX>_CLIENT_ID, <PFX>_CLIENT_SECRET,
  <PFX>_STATEMENT_PATH (with {account_no},{from},{to}), <PFX>_ACCOUNT_NO,
  optional: <PFX>_CERT / <PFX>_KEY (mTLS), <PFX>_ENC_KEY (payload crypto).
Where a bank encrypts payloads, override _decrypt_rows() in its subclass.
"""
from __future__ import annotations

import json
import os
from abc import ABC, abstractmethod
from typing import Any

import httpx

from .schema import CanonicalTxn, credit, debit


class ConnectorConfigError(RuntimeError):
    """Raised when a real connector is missing the credentials/spec it needs to run."""


class BankConnector(ABC):
    bank_code: str = ""
    account_last4: str = ""

    @abstractmethod
    async def fetch(self, since: str, until: str | None = None) -> list[CanonicalTxn]:
        """Return canonical txns dated >= since (ISO). Newest-safe; the caller dedups."""
        raise NotImplementedError


# --------------------------------------------------------------------- mock
class MockConnector(BankConnector):
    """Replays a parsed-statement JSON ({account:{...}, transactions:[...]}) —
    the exact shape hdfc_statement_parsed.json already has. Proves the whole
    pipeline end-to-end with zero bank access."""

    def __init__(self, source_path: str, bank_code: str = "hdfc"):
        self.source_path = source_path
        self.bank_code = bank_code
        with open(source_path, encoding="utf-8") as f:
            self._doc = json.load(f)
        self.account_last4 = str(self._doc.get("account", {}).get("number", ""))[-4:]

    async def fetch(self, since: str, until: str | None = None) -> list[CanonicalTxn]:
        out: list[CanonicalTxn] = []
        for r in self._doc.get("transactions", []):
            d = r["date"]
            if d < since or (until and d > until):
                continue
            if r.get("deposit"):
                out.append(credit(self.bank_code, self.account_last4, d, r["deposit"],
                                  r.get("narration", ""), r.get("ref", ""), r.get("balance")))
            elif r.get("withdrawal"):
                out.append(debit(self.bank_code, self.account_last4, d, r["withdrawal"],
                                 r.get("narration", ""), r.get("ref", ""), r.get("balance")))
        return out


# --------------------------------------------------------- generic REST base
class RestStatementConnector(BankConnector):
    """OAuth2 client-credentials -> GET statement -> map rows -> CanonicalTxn.

    Works out of the box for a bank whose (post-decryption) statement response is
    JSON. Field locations are declared in ROW_MAP and can be overridden per bank.
    Banks that encrypt the payload override _decrypt_rows()."""

    env_prefix: str = ""
    # where each field lives in one statement row (dotted path); override per bank
    ROW_MAP: dict[str, str] = {
        "date": "transactionDate", "amount": "amount", "type": "drCr",
        "narration": "narration", "ref": "referenceNumber", "balance": "runningBalance",
    }
    CREDIT_TOKENS = {"C", "CR", "CREDIT", "cr"}

    def __init__(self):
        p = self.env_prefix
        self.base = (os.getenv(f"{p}_API_BASE") or "").rstrip("/")
        self.token_url = os.getenv(f"{p}_TOKEN_URL") or ""
        self.client_id = os.getenv(f"{p}_CLIENT_ID") or ""
        self.client_secret = os.getenv(f"{p}_CLIENT_SECRET") or ""
        self.statement_path = os.getenv(f"{p}_STATEMENT_PATH") or ""
        self.account_no = os.getenv(f"{p}_ACCOUNT_NO") or ""
        self.account_last4 = self.account_no[-4:]
        self.cert = os.getenv(f"{p}_CERT") or None
        self.key = os.getenv(f"{p}_KEY") or None

    def _require(self) -> None:
        missing = [k for k, v in {
            f"{self.env_prefix}_API_BASE": self.base,
            f"{self.env_prefix}_TOKEN_URL": self.token_url,
            f"{self.env_prefix}_CLIENT_ID": self.client_id,
            f"{self.env_prefix}_CLIENT_SECRET": self.client_secret,
            f"{self.env_prefix}_STATEMENT_PATH": self.statement_path,
            f"{self.env_prefix}_ACCOUNT_NO": self.account_no,
        }.items() if not v]
        if missing:
            raise ConnectorConfigError(
                f"{self.bank_code}: missing env {missing}. Add them from the bank's API "
                f"doc + your enrolment, then re-run. See bankfeed/README.md.")

    def _mtls(self) -> Any:
        return (self.cert, self.key) if self.cert and self.key else None

    async def _token(self, client: httpx.AsyncClient) -> str:
        r = await client.post(self.token_url, data={
            "grant_type": "client_credentials",
            "client_id": self.client_id, "client_secret": self.client_secret})
        r.raise_for_status()
        return r.json().get("access_token", "")

    def _decrypt_rows(self, payload: dict) -> list[dict]:
        """Default: response is plain JSON. Override for banks that encrypt payloads
        (e.g. AES payload + RSA-wrapped key) using <PFX>_ENC_KEY."""
        # common shapes: {"data":{"transactions":[...]}} or {"records":[...]}
        for path in ("data.transactions", "transactions", "records", "data.records"):
            node: Any = payload
            for part in path.split("."):
                node = node.get(part) if isinstance(node, dict) else None
            if isinstance(node, list):
                return node
        return []

    @staticmethod
    def _dig(row: dict, dotted: str) -> Any:
        node: Any = row
        for part in dotted.split("."):
            node = node.get(part) if isinstance(node, dict) else None
        return node

    def _to_canonical(self, row: dict) -> CanonicalTxn | None:
        m = self.ROW_MAP
        date = self._dig(row, m["date"])
        amount = self._dig(row, m["amount"])
        if date is None or amount is None:
            return None
        drcr = str(self._dig(row, m["type"]) or "").strip()
        narr = str(self._dig(row, m["narration"]) or "")
        ref = str(self._dig(row, m["ref"]) or "")
        bal = self._dig(row, m.get("balance", ""))
        is_credit = drcr in self.CREDIT_TOKENS
        mk = credit if is_credit else debit
        return mk(self.bank_code, self.account_last4, str(date)[:10], float(amount), narr, ref,
                  float(bal) if bal not in (None, "") else None)

    async def fetch(self, since: str, until: str | None = None) -> list[CanonicalTxn]:
        self._require()
        path = (self.statement_path
                .replace("{account_no}", self.account_no)
                .replace("{from}", since).replace("{to}", until or since))
        async with httpx.AsyncClient(timeout=60, cert=self._mtls()) as client:
            token = await self._token(client)
            r = await client.get(f"{self.base}{path}",
                                  headers={"Authorization": f"Bearer {token}"})
            r.raise_for_status()
            rows = self._decrypt_rows(r.json())
        out = [c for row in rows if (c := self._to_canonical(row)) and c.date >= since]
        return out


class HdfcConnector(RestStatementConnector):
    """HDFC corporate API-banking. Confirm ROW_MAP + encryption against HDFC's spec
    (developer.hdfcbank.com). HDFC typically wraps payloads — override _decrypt_rows."""
    bank_code = "hdfc"
    env_prefix = "HDFC"


class AxisConnector(RestStatementConnector):
    """Axis developer API (developer.axisbank.com). Confirm ROW_MAP + encryption."""
    bank_code = "axis"
    env_prefix = "AXIS"


class BoiConnector(RestStatementConnector):
    """Bank of India — programmatic statement API availability is uncertain; this
    account may have to stay on semi-automated import. Fill env if BOI grants API."""
    bank_code = "boi"
    env_prefix = "BOI"


CONNECTORS: dict[str, type[BankConnector]] = {
    "hdfc": HdfcConnector, "axis": AxisConnector, "boi": BoiConnector,
}
