# Fix: yfinance_symbol Denormalisierung in portfolio_snapshot

**Datum:** 2026-07-14
**Status:** GEPLANTE IMPLEMENTIERUNG
**Zweck:** Eliminierung des instrument_id → yfinance_symbol JOIN-Overheads in allen yfinance-Calls

## Problem
Jeder yfinance-Call (correlation, signal_worker, data_worker) muss instrument_id → yfinance_symbol via instruments-Tabelle auflösen. Das ist ein JOIN oder separate Query pro Call.

## Lösung
Denormalisierte Spalte `yfinance_symbol TEXT` in `portfolio_snapshot` als Cache.

## Architektur
- **SSOT:** `instruments.yfinance_symbol` (einzige Quelle der Wahrheit)
- **Cache:** `portfolio_snapshot.yfinance_symbol` (denormalisiert, für performante Lesezugriffe)
- **Fail-open:** NULL → fallback zu instruments-Tabelle via `_resolve_yf_symbols()`

## Migration
1. `ALTER TABLE portfolio_snapshot ADD COLUMN yfinance_symbol TEXT`
2. `UPDATE portfolio_snapshot SET yfinance_symbol = (SELECT yfinance_symbol FROM instruments WHERE instruments.instrument_id = portfolio_snapshot.instrument_id)`
3. Data_worker: yfinance_symbol beim Sync mitnehmen
4. Reconciler: yfinance_symbol bei neuen Positionen schreiben

## Code-Änderungen
1. **migration:** `scripts/migrate_yf_symbol.py` — ALTER TABLE + UPDATE
2. **data_worker:** `src/bot/workers/data_worker.py` — yfinance_symbol beim INSERT/UPDATE mitnehmen
3. **correlation:** `src/bot/core/correlation.py` — `_resolve_yf_symbols()` entfernt, direkte Spalten-Lesung
4. **signal_worker:** `src/bot/workers/signal_worker.py` — direkte Spalten-Lesung
5. **trailing_stop:** `src/bot/core/trailing_stop.py` — `_action_market_open()` liest direkt aus portfolio_snapshot

## Edge-Cases
- NULL yfinance_symbol → fallback zu instruments-Tabelle
- Delisting → yfinance_symbol bleibt alt, yfinance liefert "delisted" Error → blacklist
- Neue Instrumente → data_worker muss yfinance_symbol aus instruments laden

## Test-Strategie
- Migration: Test mit leerer und gefüllter DB
- Data_worker: Mock instruments, verify yfinance_symbol wird geschrieben
- correlation: Test ohne instruments-Tabelle (NULL → fallback)
- signal_worker: Test mit NULL yfinance_symbol
