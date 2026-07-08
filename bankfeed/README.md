# bankfeed — bank → Zoho Books statement integration

Pulls transactions from each bank's **corporate statement API** on a schedule,
normalises them, de-duplicates, and posts them straight into the matching Zoho
Books bank register. No manual CSV upload.

Runs from the `metamorphosis/` directory.

```
bankfeed/
  schema.py      CanonicalTxn — the one shape every connector emits
  connectors.py  BankConnector ABC + MockConnector + Hdfc/Axis/Boi (REST scaffold)
  categorize.py  narration → account rules (v1: everything → suspense)
  poster.py      posts to Zoho /banktransactions, idempotent, safe-mode
  state.py       durable cursor + de-dup (bankfeed_state.json)
  config.py      bank_code → Zoho account id + suspense id (from env)
  run_feed.py    CLI orchestrator — one run = one pull cycle
```

## Pipeline

```
connector.fetch(since) → [CanonicalTxn] → categorize → dedup(state) → poster.post → Zoho register
```

## How money maps to Zoho (proven live, org 906246204)

The Zoho public API has **no "uncategorised bank feed" primitive**, so:

| Statement line | Zoho transaction_type | from → to |
|---|---|---|
| Credit (money in) | `deposit` | Bank Feed Suspense → Bank |
| Debit (money out) | `transfer_fund` | Bank → Bank Feed Suspense |

The contra leg **must be a cash/bank-type account** (expense/income accounts are
rejected, error 11016). Every line lands in **Bank Feed Suspense**
(`7530276000000191006`); the CA reclassifies from there, or you enable narration
rules in `categorize.py` (see its docstring for the account-type caveat).

## Usage

```bash
# Dry-run from a parsed statement JSON (no bank access, writes nothing):
python -m bankfeed.run_feed --bank hdfc --mock ../hdfc_statement_parsed.json --since 2026-06-24

# Same, but actually write into Zoho:
python -m bankfeed.run_feed --bank hdfc --mock ../hdfc_statement_parsed.json --since 2026-06-24 --live

# Live pull from the bank's real API (once .env is filled):
python -m bankfeed.run_feed --bank hdfc --since 2026-07-01 --live
```

Safe by default: **without `--live` it only prints a plan.** Re-runs are
idempotent — the same line is never posted twice (dedup by content hash).

## Wiring a real bank

Each bank needs its Zoho account id + its API credentials, all via `.env`
(never entered into a bank website by the agent — you own and place the secrets).

1. **Create the Zoho bank account** (like HDFC already is), then set its id:
   `ZOHO_HDFC_ACCOUNT_ID`, `ZOHO_AXIS_ACCOUNT_ID`, `ZOHO_BOI_ACCOUNT_ID`.
2. **Add the bank's API config** (prefix `HDFC` / `AXIS` / `BOI`):
   ```
   HDFC_API_BASE=https://api.hdfcbank.com/...
   HDFC_TOKEN_URL=https://api.hdfcbank.com/oauth2/token
   HDFC_CLIENT_ID=...
   HDFC_CLIENT_SECRET=...
   HDFC_STATEMENT_PATH=/corp/statement?account={account_no}&from={from}&to={to}
   HDFC_ACCOUNT_NO=50200116410996
   HDFC_CERT=/path/client.pem      # if mTLS
   HDFC_KEY=/path/client.key
   ```
3. **Confirm the response mapping.** `RestStatementConnector.ROW_MAP` assumes JSON
   field names (`transactionDate`, `amount`, `drCr`, …). Override per bank to match
   the real API doc. If the bank **encrypts payloads** (HDFC/Axis often do — AES
   payload + RSA-wrapped key), override `_decrypt_rows()` in that connector.

### Bank reality
- **HDFC / Axis** — run corporate API-banking programs with statement APIs
  (developer.hdfcbank.com / developer.axisbank.com). Feasible once enrolled.
- **Bank of India** — programmatic statement API availability is uncertain; this
  account may have to stay on semi-automated import.

## Scheduling (near real-time)

One `run_feed` invocation = one pull cycle. Schedule it externally:

- **Windows Task Scheduler**: action `python -m bankfeed.run_feed --bank hdfc --live`,
  start-in = the `metamorphosis/` folder, trigger every 2 hours.
- Aggregator/API refreshes are typically a few times/day — true sub-minute
  real-time needs the bank's host-to-host push, a separate arrangement.

## What's proven vs pending

- ✅ Core pipeline, dedup, safe-mode, and the **live Zoho poster** (validated:
  posted → idempotent re-run skipped → deleted, account clean).
- ✅ HDFC statement normalises and reconciles to the paisa via the mock connector.
- ⏳ Live connectors need each bank's API base/creds/field-map/encryption from your
  enrolment — drop them in `.env` and the `--live` real-API path runs.
