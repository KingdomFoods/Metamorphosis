"""hdfc_xls.py — parse an HDFC Bank statement .xls export into canonical rows.

HDFC exports an OLE2 .xls with a header block, a Date/Narration/Chq/Value-Dt/
Withdrawal/Deposit/Closing table, then a STATEMENT SUMMARY footer. We read only
strict dated rows (skips wrapped-narration and the summary row that otherwise
doubles the totals) and, when the footer is present, validate against it.

Returns: {"account": {...}, "transactions": [...]}  (same shape as the parsed JSON).
"""
from __future__ import annotations

import re
from typing import Any

import xlrd

_DATE = re.compile(r"^\d{2}/\d{2}/\d{2}$")
_ACCNO = re.compile(r"Account No\s*:?\s*(\d+)", re.I)
_COLS = {"date": 0, "narration": 1, "ref": 2, "value_dt": 3, "withdrawal": 4, "deposit": 5, "balance": 6}


def _num(v: Any) -> float | None:
    s = str(v).strip()
    try:
        return float(s)
    except ValueError:
        return None


def parse_hdfc_xls(path: str) -> dict[str, Any]:
    sh = xlrd.open_workbook(path).sheet_by_index(0)

    # -- account number from the header block
    acc_no = ""
    for r in range(min(sh.nrows, 22)):
        for c in range(sh.ncols):
            m = _ACCNO.search(str(sh.cell_value(r, c)))
            if m:
                acc_no = m.group(1)
                break
        if acc_no:
            break

    txns: list[dict[str, Any]] = []
    for r in range(sh.nrows):
        d = str(sh.cell_value(r, _COLS["date"])).strip()
        if not _DATE.match(d):
            continue  # header, wrapped-narration, separator or summary row
        dd, mm, yy = d.split("/")
        txns.append({
            "date": f"20{yy}-{mm}-{dd}",
            "narration": str(sh.cell_value(r, _COLS["narration"])).strip(),
            "ref": str(sh.cell_value(r, _COLS["ref"])).strip(),
            "withdrawal": _num(sh.cell_value(r, _COLS["withdrawal"])),
            "deposit": _num(sh.cell_value(r, _COLS["deposit"])),
            "balance": _num(sh.cell_value(r, _COLS["balance"])),
        })

    return {
        "account": {"bank": "HDFC Bank", "number": acc_no, "last4": acc_no[-4:]},
        "transactions": txns,
        "validation": _validate(sh, txns),
    }


def _validate(sh: xlrd.sheet.Sheet, txns: list[dict]) -> dict[str, Any]:
    """Cross-check parsed rows against the STATEMENT SUMMARY footer, if present."""
    footer: dict[str, float] = {}
    for r in range(sh.nrows):
        if str(sh.cell_value(r, 0)).strip().replace(" ", "") in ("OpeningBalance", "Opening Balance".replace(" ", "")):
            # next row holds the numbers: opening | debits | credits | closing
            nxt = r + 1
            footer = {
                "opening": _num(sh.cell_value(nxt, 0)) or 0.0,
                "debits": _num(sh.cell_value(nxt, 4)) or 0.0,
                "credits": _num(sh.cell_value(nxt, 5)) or 0.0,
                "closing": _num(sh.cell_value(nxt, 6)) or 0.0,
            }
            break

    tw = round(sum(t["withdrawal"] or 0 for t in txns), 2)
    td = round(sum(t["deposit"] or 0 for t in txns), 2)
    out: dict[str, Any] = {"n": len(txns), "sum_withdrawals": tw, "sum_deposits": td, "footer": footer or None}
    if footer:
        out["reconciles"] = (abs(tw - footer["debits"]) < 0.01 and abs(td - footer["credits"]) < 0.01)
        # running balance check from footer opening
        run = footer["opening"]
        ok = True
        for t in txns:
            run = round(run - (t["withdrawal"] or 0) + (t["deposit"] or 0), 2)
            if t["balance"] is not None and abs(run - t["balance"]) > 0.01:
                ok = False
        out["running_balance_ok"] = ok
        out["computed_closing"] = round(run, 2)
    return out
