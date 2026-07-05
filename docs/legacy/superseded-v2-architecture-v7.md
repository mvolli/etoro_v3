# eToro Trading System — Architektur-Dokumentation

> ## ⚠️ SUPERSEDED — HISTORISCHES DOKUMENT (V2, tot seit 2026-06-24)
> Beschreibt das ARCHIVIERTE Alt-System. NICHTS hieraus ausführen oder zitieren.
> Alle Pfade (`workspace/etoro/`, `db/etoro_trading.db`) existieren NICHT mehr.
> Das Live-System ist etoro_v3 mit der EINZIGEN DB `etoro_v3/data/trading.db`.

**Version:** 7.0 | **Stand:** 2026-06-22 | **Portfolio:** ~$10k Agent Portfolio (GCID 48535175 / RoBoCoP-ZDCENBUT)

---

## 1. System-Überblick

Das eToro Trading System ist ein vollautonomes, regelbasiertes Handelssystem für ein reales eToro Agent-Portfolio (~$10k). Es läuft auf einem WSL2-Host und trifft alle Kauf-/Verkaufsentscheidungen automatisch auf Basis technischer Analyse (TA) und Trading-Bible-Regeln.

```
┌─────────────────────────────────────────────────────────────────┐
│                    HERMES CRON SCHEDULER                        │
│  [P2: every 30m] Reconciliation                                 │
│  [P3: every 15m] Data Ingestion + Trading                       │
│  [Consolidation: every 10m] Consolidation Worker               │
│  [Pending Reconciler: every 15m] Ghost Order Reconciliation     │
│  [Heartbeat Watchdog: every 10m] Pipeline Health               │
│  [Daily 20:00] Performance Report (Mo–Fr)                      │
│  [Weekly Sun 3am] DB-Hygiene                                    │
└──────────────┬──────────────────────────────────────────────────┘
               │
               ▼
┌─────────────────────────────────────────────────────────────────┐
│                   main_orchestrator.py                          │
│  1. Reconciliation (API→DB)                                     │
│  2. Data Ingestion (OHLCV + TA)                                 │
│  3. Candidate Ranking                                           │
│  4. Active Trading (execute_all_trades)                         │
│  5. Health Check                                                │
└──────────────┬──────────────────────────────────────────────────┘
               │
               ▼
┌─────────────────────────────────────────────────────────────────┐
│                    execution_module.py                          │
│  execute_buy() ← is_market_open() Gate (2026-06-22)            │
│  execute_buy_with_sl() — ATR-basierter SL                      │
│  execute_close() — Ghost Detection                              │
│  evaluate_ranked_candidates() — BUY/SELL Decisions             │
└─────────────────────────────────────────────────────────────────┘
               │
     ┌─────────┼───────────────┐
     ▼         ▼               ▼
infrastructure data_module  strategy_module
_module.py    (OHLCV+TA)   (scoring+signals)
(DB, API,
 RateLimit,
 Logging)
```

---

## 2. Modul-Inventar

### Kern-Module (aktiv im Betrieb)

| Modul | Funktion |
|-------|---------|
| `execution_module.py` | **Kanonische Quelle** für alle Trading-Logik und Order-Execution |
| `active_trading.py` | Einstiegspunkt für aktive Handelsscans (`execute_all_trades` + `main`) |
| `main_orchestrator.py` | Pipeline-Koordination (Reconcile → Data → Ranking → Trading → Health) |
| `infrastructure_module.py` | DB, eToro API-Client, Rate Limiter, Logger, Monitoring |
| `data_module.py` | OHLCV-Fetch via yfinance, TA-Berechnung, SQLite-Speicherung |
| `strategy_module.py` | Scoring, Kandidaten-Ranking, Signal-Filterung |
| `discovery_engine.py` | Momentum-Scan über 216 Instrumente → Watchlist |
| `portfolio_module.py` | Reconciliation API↔DB, Portfolio-Snapshots |
| `drawdown_monitor.py` | MDD Circuit Breaker (EMA-Rolling-Peak, Ratchet, Warmup 32 Intervalle) |
| `executor_enhancements.py` | Phase 3: Profit-Taking, Break-Even SL, Korrelationscheck |
| `trailing_stop_manager.py` | ATR-basierte Trailing Stops (ab +25% PnL) |
| `close_order_manager.py` | Ghost Detection + Retry-Queue für CLOSE/SELL Orders |
| `auto_stoploss.py` | Automatische SL-Durchsetzung (läuft VOR aktiven Scans) |
| `watchlist_manager.py` | Hybrid Instrument-Map (yf_symbol_map + discovery_watchlist.json) |
| `db_manager.py` | SQLite-Operationen (DBContext, canonical DB access) |
| `risk_limits.py` | check_buy_allowed() — Instrument-Limits, Cash-Check, Konzentration |
| `cash_strategy.py` | 5-Level Cash Management |
| `pipeline_mutex.py` | Mutex-Lock verhindert parallele Pipeline-Runs |
| `trading_bible_enforcement.py` | Trading-Bible v4 Regel-Enforcement |

### Async Execution Stack (seit 2026-06-22)

| Modul | Funktion |
|-------|---------|
| `trade_queue.py` | queue_trade() — QUEUED→EXECUTING→PENDING_API→FILLED/FAILED/GHOST |
| `trade_poller.py` | Daemon-Prozess — liest Queue, submittiert API-Calls, pollt Ergebnis |
| `db_event_writer.py` | Queue-basierter DB-Event-Writer |
| `db_queue_worker.py` | Worker für DB-Queue (poll 2s) |

### Support-Module

| Modul | Funktion |
|-------|---------|
| `config_manager.py` | YAML Config-Loader (instruments.yaml, scoring.yaml) |
| `yf_symbol_map.py` | Statische Map eToro-ID → yfinance-Symbol (216 Instrumente) |
| `instrument_rotation.py` | Priority-Tiered Rotation: Core(12)/OpenPos/Watchlist/Rest(~204) |
| `backtest_engine.py` | Walk-Forward Backtesting Engine |
| `portfolio_correlation.py` | Korrelationsmatrix-Berechnung |
| `pending_reconciler.py` | Reconciliert PENDING-Trades gegen Live-API |
| `pending_orders_db.py` | pending_orders-Tabelle (118 rows) — Ghost Order Tracking |
| `consolidation_worker.py` | Non-blocking DB-Queue-basierter Consolidator |
| `heartbeat_watchdog.py` | Pipeline-Stall-Detection |

### Discord-Reporting Module

| Modul | Funktion |
|-------|---------|
| `discord_embeds.py` | **Zentrales Embed-Modul** — P1 Heartbeat, P2 Reconciliation, P3 SL-Watchdog, P4 Trading Decisions, P5 Consolidation, P3-O Candidates |
| `post_executor_discord.py` | Trade-Execution Report (BUY/SELL summary) |
| `monitoring_alerts.py` | P3-O Candidate Snapshot, Market-Hours-Check |

---

## 3. Datenfluss — P3 Pipeline (every 15min)

```
main_orchestrator.py --full
    │
    ├── 1. run_reconciliation()
    │       → portfolio_module.reconcile()
    │       → API-Positionen vs. DB abgleichen
    │       → DrawdownMonitor.check(equity) (EMA-Peak Update)
    │
    ├── 2. run_data_ingestion()
    │       → data_module.fetch_price_data()
    │       → yfinance OHLCV für alle Watchlist-Instrumente
    │       → instrument_rotation.py (Priority-Tiered: Core/OpenPos/Watchlist/Rest)
    │       → data_module.compute_technical_indicators()
    │       → RSI, BB, MACD, SMA200, ATR → SQLite
    │
    ├── 3. run_candidate_ranking()
    │       → strategy_module.get_fresh_signals()
    │       → strategy_module.compute_score()
    │       → Top-Kandidaten → candidate_ranking.json
    │
    └── 4. active_trading.execute_all_trades()
            → auto_stoploss.check_stops()          ← ZUERST
            → DrawdownMonitor.check()
            → executor_enhancements.evaluate_profit_taking()
            → trailing_stop_manager.evaluate_trailing_stops()
            → execution_module.evaluate_ranked_candidates()
            │     → is_market_open() Gate ← NEU 2026-06-22
            │     → Cash-Check, Konzentrations-Limit
            │     → BUY → execute_buy_with_sl() → queue_trade()
            │                                      → trade_poller (async)
            └── generate_sell_decisions() → execute_close()
```

---

## 4. Datenfluss — Async BUY Execution

```
execute_buy_with_sl()
    │
    ├── is_market_open() → False? → return {blocked_by: "market_hours"}  ← GATE
    │
    ├── queue_trade() → trade_queue DB (status: QUEUED)
    │
    └── trade_poller.py (Daemon PID-Datei: run/trade_poller.pid)
            │
            ├── QUEUED → EXECUTING → submit to eToro API
            ├── API Response → PENDING_API (has api_order_id)
            └── Poll /portfolio → FILLED | FAILED | GHOST
```

---

## 5. Cron-Jobs (aktiv, Stand 2026-06-22)

| Job-ID | Name | Schedule | Channel |
|--------|------|----------|---------|
| `f50fe0861405` | P2: Reconciliation | every 30m | #etoro-trading |
| `5dfb67815cb1` | P3: Data Ingestion + Trading | every 15m | #etoro-trading |
| `f60344ea3bb6` | Consolidation Worker | every 10m | #etoro-trading |
| `7d3b305dbcfa` | Pending Order Reconciler | every 15m | #etoro-trading |
| `a78bf277e072` | Heartbeat Watchdog | every 10m | #etoro-trading |
| `d9c2d26cdcc8` | SL-Watchdog v2 Health Check | every 15m | #etoro-trading |
| `e6e04f18246f` | DB Queue Worker Health Check | every 15m | #etoro-trading |
| `1e2793d61122` | Daily Performance Report | Mo–Fr 20:00 CEST | #etoro-trading |
| `2ad84ca6a00d` | DB-Hygiene | weekly Sun 3am | #etoro-trading |
| `87fb0e4e9d3a` | Dream Cycle (Memory) | daily 3am | origin |

**Paused Jobs (Blue/Green — für Rollback behalten):**
- `8ce1c120fb93` eToro Discovery Engine (daily 3am) — ersetzt durch manuelle Runs
- `bbe2a8348299` P1: SL-Watchdog + Heartbeat (5min) — in SL-Watchdog v2 integriert
- `293a6384fb58` eToro Core Pipeline (15min) — ersetzt durch P3
- `115d7a848bce` Unified Pipeline (10min) — ersetzt durch P3

---

## 6. Datenbankschema (SQLite)

**Datenbank:** `db/etoro_trading.db` (CANONICAL — nicht `data/trading.db` oder `data/etoro_trading.db`)

| Tabelle | Inhalt | Rows (ca.) |
|---------|--------|-----------|
| `portfolio_state` | Stündliche Snapshots: equity, cash, positions | 240 |
| `trades_history` | Alle Trades mit Preis, Amount, SL, Entscheidungsgrund | 137 |
| `price_data` | OHLCV-Daten (yfinance) | 57k |
| `signals` | TA-Signale: RSI, BB, MACD, SMA200, ATR pro Instrument | 52k |
| `system_log` | Alle Systemereignisse, Warnungen, Fehler | 72k |
| `trade_queue` | Async BUY/SELL Queue (QUEUED→FILLED/GHOST) | 68 |
| `consolidation_queue` | Fragment-Close Queue | 39 |
| `pending_orders` | Ghost Order Tracking (legacy) | 118 |
| `cash_locks` | Cash-Locking für parallele BUY-Prevention | 88 |
| `instrument_metadata` | 216 Instrumente mit Rotation-Metadaten | 216 |
| `open_positions_sl_config` | SL-Konfiguration offener Positionen | 21 |
| `drawdown_tracking` | Equity-History für MDD | 1225 |
| `peak_equity` | smoothed_peak + ratcheted_peak + warmup_count | 1 |
| `correlation_data` | Korrelationsmatrix | 23k |
| `symbol_cooldowns` | Nach Consolidation: Cooldown pro Symbol | 3 |
| `dedup_log` | Duplikat-Prevention | 2 |

---

## 7. Trading-Bible Regeln (v5.2)

### Position Sizing
- `MAX_SINGLE_TRADE_PCT = 5.0%` — Max. Einzeltrade als % der Equity
- `MIN_FREE_CASH = $100` — Minimum Cash vor jedem Trade
- `ATR_BASED_CAP_PCT = 3.0%` — ATR-adjustierter Cap
- `MAX_OPEN_POSITIONS = 15` — Maximale parallele Positionen

### Cash Management
- `CASH_TARGET_MIN_PCT = 15.0%` — Unter Minimum → SELL_PARTIAL
- `CASH_TARGET_MAX_PCT = 30.0%` — Über Maximum → BUY aggressiver
- `CASH_EMERGENCY_PCT = 10.0%` — Notfall → alle profitablen Pos. teilweise schließen

### Drawdown Protection (DrawdownMonitor v5.0)
- EMA-Rolling-Peak (statt statisches Maximum)
- Warmup: erste 32 Intervalle (8h) — keine Alerts
- Ratchet-Peak: schützt Gewinne in 5%-Stufen
- Absoluter Floor $8.500 → CIRCUIT_BREAKER
- `MDD_DAILY_PCT = 2.0%` — Warning → Trade-Größe halbiert
- `MDD_DAILY_PCT = 5.0%` (CRITICAL) → Alle BUYs blockiert
- `MDD_WEEKLY_PCT = 5.0%` — Alle Positionen prüfen
- `MDD_MONTHLY_PCT = 10.0%` — Circuit Breaker

### Stop-Loss
- `DEFAULT_SL_PCT = 3.0%` — Hard SL bei -3% (ATR-basiert dynamisch)
- `BREAK_EVEN_TRIGGER_PCT = 5.0%` — Nach +5% → SL auf Einstandspreis
- Gestaffelte Gewinnmitnahme: +10%, +20%, +30%, +40%, +50%, +75%, +100%
- **Broker-Side SL:** `IsNoStopLoss=False` + ATR-basierter SL-Preis (BUY_BODY_TEMPLATE)

### Market Hours Gate (NEU 2026-06-22)
- `is_market_open()` in `execute_buy()` — blockiert Pre-Market/Post-Market BUYs
- Marktzeiten: **Mo–Fr 13:30–20:00 UTC** (= 15:30–22:00 CEST)
- Pre-Market BUYs → Ghost Orders → Root Cause der 73 fehlgeschlagenen Orders (2026-06-22)

### Instrument-Limits (Konzentration)
- NVDA: 25% | META: 15% | MSFT: 15% | AMZN: 12% | TSLA: 5%
- QQQ: 25% | SPY: 20%
- BTC/USD: 5% | ETH/USD: 5%
- Default (nicht gelistet): 10%

---

## 8. Async Execution Stack (seit 2026-06-22)

### trade_poller Daemon

```bash
# Starten:
cd /home/mvolli/.hermes/workspace/etoro/scripts
python3 trade_poller.py --daemon

# PID-Datei:
/home/mvolli/.hermes/workspace/etoro/run/trade_poller.pid

# Prüfen ob läuft:
ps aux | grep trade_poller | grep -v grep

# Neu starten wenn tot (PID file entfernen):
rm -f run/trade_poller.pid
python3 trade_poller.py --daemon &
```

### Trade Queue Status

```sql
SELECT status, COUNT(*) FROM trade_queue GROUP BY status;
-- QUEUED: noch nicht verarbeitet (poller muss laufen!)
-- EXECUTING: poller bearbeitet gerade
-- PENDING_API: API-Call gemacht, warte auf Fill
-- FILLED: erfolgreich ausgeführt
-- GHOST: Order accepted aber nie gefüllt (eToro Ghost Order)
-- FAILED: API-Fehler oder Timeout
```

---

## 9. Bekannte Probleme & Pitfalls

### Ghost Orders
- **Root Cause:** BUYs während Nicht-Marktzeiten (Pre-Market/Post-Market)
- **Fix:** `is_market_open()` Gate in `execute_buy()` (2026-06-22)
- **Erkennung:** `trade_queue.status = 'GHOST'`
- **Dokumentation:** `reports/ghost-order-root-cause-2026-06-11.md`

### Trade Poller kann sterben
- **Symptom:** Trades bleiben in `QUEUED` State, keine Ausführung
- **Check:** `cat run/trade_poller.pid && ps -p <pid>`
- **Fix:** PID-Datei löschen + Daemon neu starten (SL-Watchdog v2 Health Check überwacht)

### DB-Pfad Verwirrung
- **Canonical:** `db/etoro_trading.db` — alle Scripts müssen das nutzen
- **Legacy/leer:** `data/trading.db`, `data/etoro_trading.db`, `data/etoro.db` (nicht nutzen!)
- **db_manager.DBContext()** stellt canonical Pfad bereit

### Broker-Side Stop-Loss
- `IsNoStopLoss=False` + ATR-basierter SL-Preis via `execute_buy_with_sl()`
- `auto_stoploss.py` ist Post-Hoc-Backup, kein Ersatz für Broker-SL

---

## 10. Architektur-Changelog

### v7.0 — 2026-06-22
- `is_market_open()` Gate in `execute_buy()` — blockiert Pre-Market BUYs (Root Cause 73 Ghost Orders)
- 3 Cron-Jobs auf `gemini-2.5-flash` migriert (war `qwen3.6-35b` → 404)
- 3 Cron-Jobs auf korrekten Discord-Channel `1513971015108263957` umgeleitet (war gelöschter Channel)
- `trade_poller` Daemon neu gestartet (PID 14842) nach unbemerkt abgestürztem PID 3782
- ARCHITECTURE.md vollständig aktualisiert (v6.2 war veraltet seit 2026-06-16)

### v6.2 — 2026-06-16
- Async Trade Execution Stack: `trade_queue.py` + `trade_poller.py`
- `execute_buy()` default `async_mode=True`
- Cash Locking persistent in DB (`cash_locks` table, 5min TTL)
- DB Unification: `db_manager.DBContext()` canonical

### v6.1 — 2026-06-16
- Discovery Engine: 8 Kategorien, `MIN_MOMENTUM_SCORE` 60→40
- Cron-Jobs von 5 auf 3 konsolidiert

### v6.0 — 2026-06-16
- SELL-Strategie v5.2 in `main_orchestrator.py`
- Ghost Order Detection: 180s Wait pro BUY

### v5.0 — 2026-06-11
- SL-Watchdog v2 + Async Execution
- DrawdownMonitor v5.0 (EMA-Rolling-Peak)
