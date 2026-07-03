# eToro Agent Portfolio — API Execution Reference (v3.1)

## Quick Reference Card

### Authentication
```bash
curl -s -X POST \
  -H "x-api-key: $ETORO_API_KEY" \
  -H "x-user-key: $ETORO_USER_KEY" \
  -H "x-request-id: $(python3 -c 'import uuid; print(uuid.uuid4())')" \
  -H "Content-Type: application/json" \
  -d '<BODY>' \
  "https://public-api.etoro.com/api/v1<ENDPOINT>"
```

### Read Operations
| Endpoint | Method | Purpose |
|---|---|---|
| `/trading/info/real/pnl` | GET | Portfolio snapshot (positions, cash, equity, PnL) |
| `/me` | GET | Account info (GCID, username) |

### Write Operations — OPEN Orders
```json
{
  "InstrumentID": <id>,
  "Amount": <dollar_amount>,
  "Leverage": 1,
  "IsNoStopLoss": true,
  "IsNoTakeProfit": true,
  "isBuy": true
}
```
Endpoint: `/trading/execution/market-open-orders/by-amount`

### Write Operations — CLOSE Orders
```json
{
  "InstrumentID": <id>,
  "UnitsToDeduct": null
}
```
Endpoint: `/trading/execution/market-close-orders/positions/{positionID}`

### Critical Rules
1. **Always use FLAT body** — never nested `MarketOpenOrders[{...}]` or `MarketCloseOrders[{...}]`
2. **Use InstrumentID** — never Symbol field
3. **Wait 10-15s after POST** — PnL cache refresh delay before verifying position
4. **Include isBuy: true** for buy orders

## Response Format

### Open Order Success
```json
{
  "orderForOpen": {
    "instrumentID": 1003,
    "amount": 10.0,
    "isBuy": true,
    "statusID": 1,
    "orderID": 1496581443
  }
}
```

### Close Order Success
```json
{
  "orderForClose": {
    "instrumentID": 1003,
    "statusID": 1,
    "orderID": 1496569485
  }
}
```

## Error Handling

| Error | Cause | Fix |
|---|---|---|
| `RouteNotFound` | Wrong endpoint path | Use endpoints listed above |
| `Validation Error: InstrumentID must be > 0` | Using Symbol instead of InstrumentID | Use numeric InstrumentID |
| `MethodNotAllowed` | GET on POST-only endpoint | Use correct HTTP method |
| Position not appearing after order | PnL cache delay | Wait 10-15 seconds and retry |

## Instrument ID Mapping

| Asset | ID |
|---|---|
| BTC/USD | 100000 |
| NVDA | 1137 |
| TSLA | 1111 |
| META | 1003 |
| MSFT | 1004 |
| AMZN | 1005 |
| QQQ | 3006 |
| SPY | 3000 |

## Related Documentation

- `open-order-execution-fix.md` — Detailed bug analysis and resolution
- `../scripts/execute_trade.py` — Python execution script
- `/home/mvolli/.hermes/skills/finance/etoro-agent-portfolio/SKILL.md` — Full skill docs
