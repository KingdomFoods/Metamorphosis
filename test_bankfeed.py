"""test_bankfeed.py — offline tests for the bankfeed core (no Zoho, no bank creds).

Covers the logic that must never silently break: dedup identity, mock parsing,
the exact Zoho payload per direction, and safe-mode (dry-run writes nothing).
"""
from __future__ import annotations

import json

import pytest

from bankfeed.connectors import MockConnector
from bankfeed.poster import ZohoBankPoster
from bankfeed.schema import CanonicalTxn, credit, debit
from bankfeed.state import FeedState

BANK = "b_bank"
SUS = "b_suspense"


# ---------------------------------------------------------------- schema / dedup
def test_external_id_stable_and_sensitive():
    a = credit("hdfc", "0996", "2026-06-24", 100.0, "UPI X", "R1")
    b = credit("hdfc", "0996", "2026-06-24", 100.0, "UPI X", "R1")
    c = credit("hdfc", "0996", "2026-06-24", 100.01, "UPI X", "R1")  # amount differs
    assert a.external_id == b.external_id       # identical line -> same id
    assert a.external_id != c.external_id       # 1 paisa difference -> different id


def test_signed_direction():
    assert credit("h", "1", "2026-01-01", 50, "x").signed() == 50
    assert debit("h", "1", "2026-01-01", 50, "x").signed() == -50


# ---------------------------------------------------------------- mock connector
async def test_mock_connector_parses_and_filters(tmp_path):
    doc = {"account": {"number": "50200116410996"}, "transactions": [
        {"date": "2026-06-24", "deposit": 200.0, "narration": "in", "ref": "A"},
        {"date": "2026-06-25", "withdrawal": 75.0, "narration": "out", "ref": "B"},
        {"date": "2026-06-20", "deposit": 999.0, "narration": "old", "ref": "C"},  # before 'since'
    ]}
    p = tmp_path / "s.json"
    p.write_text(json.dumps(doc), encoding="utf-8")
    conn = MockConnector(str(p), bank_code="hdfc")
    assert conn.account_last4 == "0996"

    txns = await conn.fetch(since="2026-06-24")
    assert len(txns) == 2                        # the 2026-06-20 line is filtered out
    assert txns[0].direction == "credit" and txns[0].amount == 200.0
    assert txns[1].direction == "debit" and txns[1].amount == 75.0


# ---------------------------------------------------------------- poster payloads
def _poster(live=False, state=None):
    return ZohoBankPoster(z=None, bank_account_id=BANK, suspense_id=SUS,
                          state=state or FeedState("_unused.json"), live=live)


def test_payload_credit_is_deposit_into_bank():
    p = _poster()._payload(credit("h", "1", "2026-06-24", 123.45, "narr", "REF"), SUS)
    assert p["transaction_type"] == "deposit"
    assert p["from_account_id"] == SUS and p["to_account_id"] == BANK
    assert p["amount"] == 123.45 and p["reference_number"] == "REF"


def test_payload_debit_is_transfer_out_of_bank():
    p = _poster()._payload(debit("h", "1", "2026-06-24", 500.0, "narr", "REF"), SUS)
    assert p["transaction_type"] == "transfer_fund"
    assert p["from_account_id"] == BANK and p["to_account_id"] == SUS


# ---------------------------------------------------------------- state / safe-mode
def test_state_dedup_roundtrip(tmp_path):
    st = FeedState(str(tmp_path / "st.json"))
    t = credit("hdfc", "1", "2026-06-24", 10, "x", "R")
    assert not st.seen("hdfc", t.external_id)
    st.mark("hdfc", t.external_id, "ZTXN1", t.date)
    assert st.seen("hdfc", t.external_id)
    assert st.last_date("hdfc") == "2026-06-24"


async def test_dryrun_writes_nothing(tmp_path):
    st = FeedState(str(tmp_path / "st.json"))
    poster = _poster(live=False, state=st)
    r = await poster.post(credit("hdfc", "1", "2026-06-24", 10, "x", "R"))
    assert r["status"] == "planned"
    assert st.count("hdfc") == 0                 # dry-run must not persist anything


async def test_dedup_skips_already_seen(tmp_path):
    st = FeedState(str(tmp_path / "st.json"))
    t = credit("hdfc", "1", "2026-06-24", 10, "x", "R")
    st.mark("hdfc", t.external_id, "ZTXN1", t.date)
    r = await _poster(live=True, state=st).post(t)   # live, but already seen -> no z call
    assert r["status"] == "skipped"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
