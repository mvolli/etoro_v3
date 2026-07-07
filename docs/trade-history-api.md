# eToro Trade History API

**Endpoint:** `GET /api/v1/trading/info/trade/history`  
**Auth:** x-api-key + x-user-key + x-request-id (UUID)  
**Found:** 2026-07-07 via OpenAPI spec at https://api-portal.etoro.com/api-reference/openapi.json

## Parameters

| Parameter | Required | Type | Description |
|-----------|----------|------|-------------|
| `minDate` | **YES** | string (date) | Start date of the period to view |
| `page` | NO | integer | Page number for pagination |
| `pageSize` | NO | integer | Number of trades per page |

## Response Fields (Array of Trade Objects)

| Field | Type | Description |
|-------|------|-------------|
| `orderId` | integer | Order ID — matches our DB `order_id`! |
| `positionId` | integer | Position ID |
| `parentPositionId` | integer | Parent position ID (for partial closes) |
| `instrumentId` | integer | Instrument ID |
| `isBuy` | boolean | True = long, False = short |
| `openRate` | number | Entry price |
| `closeRate` | number | Exit price |
| `openTimestamp` | string (datetime) | When position was opened |
| `closeTimestamp` | string (datetime) | When position was closed |
| `units` | number | Number of units traded |
| `initialInvestment` | number | Initial investment amount |
| `investment` | number | Current/final investment amount |
| `netProfit` | number | Net P&L in USD (positive = profit, negative = loss) |
| `fees` | number | Fees charged on this trade |
| `leverage` | integer | Leverage used (e.g. 1, 2, 5, 10) |
| `stopLossRate` | number | Stop-loss rate if set |
| `takeProfitRate` | number | Take-profit rate if set |
| `trailingStopLoss` | boolean | Whether trailing stop was active |
| `socialTradeId` | integer | Social trade ID (copy trading) |

## Use Cases for V3

1. **Reconciler final verification**: Statt nur zu prüfen ob Position weg ist, können wir jetzt den EXAKTEN Close-Price + P&L aus der API holen und die DB mit echten Werten updaten (statt Schätzung).
2. **Trade History Reporting**: Alle geschlossenen Trades abrufen für Reports/Analytics.
3. **Ghost Order Detection**: Orders in unserer DB die nicht in der API History erscheinen = Ghost Orders.

## Demo Account (Parallel)

`GET /api/v1/trading/info/trade/demo/history` — identisches Schema, nur für Demo-Konto.
