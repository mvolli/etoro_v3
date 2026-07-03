# eToro Close Order API — Verified Reference (2026-06-21)

## Sources
- `/mnt/f/etoroapi/llms_txt.html` — Official eToro API docs (local clone)
- Gemini 2.5 Flash analysis + live API testing

---

## Close Position Endpoint

```
POST /api/v1/trading/execution/market-close-orders/positions/{positionId}
```

### Request Body
```json
{
  "UnitsToDeduct": null
}
```

**CRITICAL:** `InstrumentID` is **NOT required** in the body when `positionId` is in the URL path.
- `UnitsToDeduct: null` → Full close (entire position)
- `UnitsToDeduct: <number>` → Partial close (specific units)

### Headers
```
x-api-key: {API_KEY}
x-user-id: {USER_KEY}
x-request-id: {UUID_v4}  ← Use unique UUID per attempt for idempotency
Content-Type: application/json
```

### Response
```json
{
  "statusID": 1,
  "message": "Order placed successfully",
  "orderID": "..."
}
```

### Example (working cURL)
```bash
curl -X POST \
  'https://public-api.etoro.com/api/v1/trading/execution/market-close-orders/positions/3481605213' \
  -H 'x-api-key: YOUR_API_KEY' \
  -H 'x-user-id: YOUR_USER_KEY' \
  -H 'x-request-id: 550e8400-e29b-41d4-a716-446655440000' \
  -H 'Content-Type: application/json' \
  -d '{"UnitsToDeduct": null}'
```

---

## Cancel Pending Close Order

```
DELETE /api/v1/trading/execution/market-close-orders/pending/{positionId}
```

### Response
```json
{
  "statusID": 1,
  "message": "Order cancelled successfully"
}
```

---

## Get Open Positions (Portfolio)

```
GET /api/v1/trading/info/portfolio
```

Returns positions array with `positionID`, `symbol`, `instrumentID`, `quantity`, `marketValue`.

**NOTE:** `/agent-portfolios/{id}/positions/open` returns "RouteNotFound" — use `/trading/info/portfolio` instead.

---

## Known Bugs & Fixes (2026-06-21)

### Bug 1: Wrong CLOSE_BODY_TEMPLATE
**Problem:** `CLOSE_BODY_TEMPLATE` included `InstrumentID: None` which caused issues.
**Fix:** Removed `InstrumentID` — only `UnitsToDeduct` is needed.
**File:** `execution_module.py` line ~394

### Bug 2: Idempotency Key Collision
**Problem:** `x-request-id` used MD5 hash of hour → all retries in same hour had the SAME ID, causing duplicate-order rejections.
**Fix:** Use `uuid.uuid4()` for each close attempt.
**File:** `execution_module.py` → `execute_close()`

### Bug 3: No Verification After Close
**Problem:** System sent close order but never verified if position was actually closed.
**Fix:** Added `verify_position_closed()` — polls `/trading/info/portfolio` every 5s for max 30s.
**File:** `execution_module.py`

### Bug 4: 180s Blocking Wait in Ghost Detection
**Problem:** `detect_ghost_order()` called `time.sleep(180)` blocking the pipeline for 3 minutes.
**Fix:** Active polling every 5s, max 30s total wait. Uses `/trading/info/portfolio` (not `/trading/info/real/pnl`).
**File:** `close_order_manager.py` → `detect_ghost_order()`

### Bug 5: instrument_id Always None in Retries
**Problem:** `process_pending_retries()` always set `instrument_id: None` in the returned dict.
**Fix:** Extract `instrument_id` from `original_decision_data` JSON field.
**File:** `close_order_manager.py` → `process_pending_retries()`

### Bug 6: API Auth Expired (401)
**Problem:** API calls failed with HTTP 401 "API auth expired" — stale keys in memory.
**Fix:** Added `_refresh_api_keys()` to reload from `.env` on 401 errors. Retry decorator already handles 401 with exponential backoff.
**File:** `infrastructure_module.py`

---

## Instrument IDs (Verified)
| Symbol | InstrumentID | Notes |
|--------|-------------|-------|
| META | 1003 | Previously used wrong ID (3127) |
| ENI.MI | 1283 | Italian stock, market hours matter |

---

## Close Flow (Corrected)
1. Get position from `/trading/info/portfolio` → extract `positionID`
2. Send `POST /market-close-orders/positions/{positionId}` with `{"UnitsToDeduct": null}`
3. Use unique `uuid.uuid4()` as `x-request-id`
4. Poll `/trading/info/portfolio` every 5s to verify position is gone (max 30s)
5. If position still exists after 30s → Ghost Order detected → retry with exponential backoff
6. Log result to Discord #etoro-trades
