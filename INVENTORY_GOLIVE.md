# Inventory Go-Live — Item Load, Location Fix, Readiness Gate

Extends the Metamorphosis bundle for the **entity change to Kingdom Foods** (proprietorship,
GSTIN `09AFJPB3153M1ZC`). All identity is **env-only** — nothing is hardcoded, so the code
keeps working after a human re-points the org in the Zoho UI.

## New env keys (`.env`)
| Key | Meaning | Who sets it |
| --- | --- | --- |
| `ZOHO_GSTIN` | Expected org GSTIN. Code only **verifies** it — never sets it. | you (env) |
| `ZOHO_LOCATION_ID` | The D-39, Sector 59, Noida stock location id. Blank until created. | human (UI) → env |

## The three scripts

| Script | Part | Blocker | Writes? |
| --- | --- | --- | --- |
| `golive_check.py` | 3 | 2 (+ whole gate) | **No** — read-only PASS/FAIL table → `golive_readiness.md` |
| `verify_location.py` | 2 | 3 | Only a net-zero +1/reverse test on the dummy SKU → `location_verify_report.md` |
| `load_items.py` | 1 | 1 | Dry-run by default; `--commit` writes a 2-item test batch → `item_load_report.md` |

`golive_common.py` holds shared, env-driven helpers (org/GSTIN/location/tax lookups, markdown).
All reuse the audited `zoho_client.py` (token cache, retry, scope fail-fast). `.com` DC,
`/inventory/v1/locations` (not `/warehouses`), `/categories` (not `/itemgroups`).

## Run order
```bash
python zoho_client.py        # 1. auth on .com (fail -> token-regen runbook -> stop)
python golive_check.py       # 2. baseline: what's blocking BEFORE changes
python verify_location.py    # 3. confirm D-39 Sec-59 exists + write access (needs ZOHO_LOCATION_ID)
python load_items.py         # 4. DRY RUN the filled template
python load_items.py --commit          # 4b. 2-item TEST BATCH (safe mode), then STOP
python golive_check.py       # 5. re-run: what cleared vs still blocking
```

### `load_items.py` safe-mode
- **no `--commit`** → full dry run, writes nothing, prints the exact would-do table.
- **`--commit` (LIVE_MODE=false)** → writes only the first `--limit N` (default 2) items, then stops.
- **`--commit --full` (LIVE_MODE=true)** → full idempotent load. `--full` is ignored while safe.
- Validation STOPS before any API call on any missing required field, duplicate SKU,
  non-numeric price, GST ∉ {0,5,12,18,28}, or non-8-digit HSN.
- GST maps to the org's **existing** tax for that rate; a missing rate is reported, never invented.
- Opening stock posts to `ZOHO_LOCATION_ID`.

## Hard rules honoured
Code never sets the GSTIN / renames the org, never creates taxes or invents tax ids, never
hardcodes org/GSTIN/location, never flips `LIVE_MODE`, never uses `.in` / `/warehouses` /
`/itemgroups`, never leaves the location write-test residue (deletes it; falls back to a
compensating −1).

> Note: Zoho forbids hard-deleting items that ever carried opening stock — such items can
> only be marked **inactive**. `golive_check.py` therefore gates on **active** items only.
