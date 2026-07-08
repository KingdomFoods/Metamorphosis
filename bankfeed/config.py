"""config.py — maps each bank to its Zoho Books account + the shared suspense account.

Zoho account ids come from env so the same code works if the org is re-pointed.
Sensible defaults are the accounts already created live in org 906246204.
"""
from __future__ import annotations

import os

# Bank Feed Suspense (cash) — contra leg for every posted line until reclassified.
SUSPENSE_ACCOUNT_ID = os.getenv("ZOHO_SUSPENSE_ACCOUNT_ID", "7530276000000191006")

# bank_code -> Zoho Books bank account id (the register lines post into)
ZOHO_BANK_ACCOUNTS: dict[str, str] = {
    "hdfc": os.getenv("ZOHO_HDFC_ACCOUNT_ID", "7530276000000191002"),
    "axis": os.getenv("ZOHO_AXIS_ACCOUNT_ID", ""),   # set once the Axis account is created
    "boi":  os.getenv("ZOHO_BOI_ACCOUNT_ID", ""),    # set once the BOI account is created
}


def zoho_bank_account_id(bank_code: str) -> str:
    aid = ZOHO_BANK_ACCOUNTS.get(bank_code, "")
    if not aid:
        raise RuntimeError(
            f"No Zoho bank account configured for '{bank_code}'. Create the account in "
            f"Zoho Books, then set ZOHO_{bank_code.upper()}_ACCOUNT_ID in .env.")
    return aid
