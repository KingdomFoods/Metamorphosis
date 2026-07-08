# Kingdom Foods — Inventory Go-Live Readiness

_Read-only snapshot. This report never flips LIVE_MODE, sets the GSTIN, or writes data._

| Result | Gate condition | Detail |
| --- | --- | --- |
| PASS | 1. Auth on .com | org=Kingdom 24 Private Limited id=906246204 |
| FAIL | 2. Org GSTIN set + matches ZOHO_GSTIN [Blocker 2] | org GSTIN=UNSET expected=09AFJPB3153M1ZC |
| FAIL | 3. Target location exists [Blocker 3] | ZOHO_LOCATION_ID=(unset) not found among 2 locations |
| WARN | 3b. Location WRITE access | run verify_location.py (safe +1/-1 net-zero test) - not checked here (read-only gate) |
| FAIL | 4. Items have cost price + GST mapped [Blocker 1] | 370 real items | cost set 19, missing 351 | GST mapped 0, missing 370 | tax rates in org: [0, 5, 12, 18, 28, 40] |
| PASS | 5. Dummy SKU present (safe-mode anchor) | K24-TEST-001 found |
| WARN | 6. LIVE_MODE | currently false (safe mode ON) - this gate never flips it |

**Overall:** NOT READY — hard gates still failing.

## What's still blocking
- Org has no GSTIN configured.
- ZOHO_LOCATION_ID unset — the D-39 Sec-59 location id is not configured.
- 351 items missing cost price.
- 370 items have no GST/tax mapped.

## Ordered next actions
1. UI/CA: Settings -> Organization Profile -> set GSTIN to 09AFJPB3153M1ZC (human action; code never sets it).
2. UI: Settings -> Locations -> create 'D-39, Sector 59' -> set ZOHO_LOCATION_ID in .env.
3. Fill Cost Price in the template and re-run load_items.py.
4. Ensure org taxes exist for each rate; re-run load_items.py to map GST.

## The single remaining switch
- LIVE_MODE is currently **false**. Flip to true ONLY after every hard gate above is PASS and location WRITE access is confirmed by verify_location.py. This script does not flip it.
