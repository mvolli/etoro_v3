# 🤖 eToro Trading Bot V3 — Architektur & Implementierungsplan

**Erstellt:** 24. Juni 2026  
**Validiert:** Gemini 2.5 Pro  
**Status:** GREENFIELD — Neuschrieb. Alter Code bleibt in `/scripts/` als Referenz.

---

## Gemini-Validierung: Kernentscheidungen

| Frage | Gemini-Empfehlung | Umgesetzt als |
|-------|-------------------|---------------|
| SQLite OK für 6 Workers? | ✅ Ja, mit **Staggered Scheduling** | Cron-Jobs versetzt (nicht alle zur :00) |
| API-Calls Blocking? | `requests` + `tenacity` (5s/10s Timeout) | `src/bot/api/client.py` |
| Instrument-Map Startup? | JSON-Cache mit 24h TTL | `data/instrument_map.json` |
| Ghost-Order-Prävention? | State Machine in DB (`PENDING→SUBMITTING→ACTIVE→CLOSED`) | `trades` Tabelle |
| Redis nötig? | ❌ Overkill für dieses Scale | Nicht implementieren |

---

## Projektstruktur

```
etoro_v3/                          ← Neues Verzeichnis (neben altem /scripts/)
├── config.yaml                    ← API-Keys, Parameter, DB-Pfad
├── data/
│   ├── trading.db                 ← Einzige DB-Datei (WAL-Mode)
│   └── instrument_map.json        ← 24h-Cache der Instrument-IDs
├── src/bot/
│   ├── api/
│   │   ├── client.py              ← requests + tenacity, timeout=(5,10)
│   │   └── instruments.py         ← get_instrument_map() mit File-Cache
│   ├── core/
│   │   ├── regime.py              ← DRAWDOWN/NORMAL/RECOVERY Logik
│   │   ├── risk.py                ← Cash-Gate, SL-Check, Position-Limits
│   │   └── signals.py             ← RSI/BB/MACD Berechnung (yfinance)
│   ├── db/
│   │   ├── connection.py          ← SQLite WAL, 10s Timeout, kein globaler State
│   │   ├── schema.sql             ← Schema-Definition
│   │   └── repo.py                ← CRUD-Funktionen (kein SQL im Worker)
│   └── workers/
│       ├── data_worker.py         ← Marktdaten + Signale (alle 5min :00)
│       ├── risk_worker.py         ← Regime + SL-Enforcement (alle 5min :01)
│       ├── reconciler.py          ← API↔DB Sync (alle 5min :02)
│       ├── signal_worker.py       ← Kandidaten ranken (alle 15min :03)
│       ├── execution_worker.py    ← Trade-Execution (alle 15min :04)
│       └── monitor_worker.py      ← Discord Embeds (alle 30min)
├── tests/
└── scripts/                       ← Altes System (Read-Only Referenz)
```

---

## DB-Schema (minimal, normalisiert)

```sql
-- Referenz: eToro Instrumente (24h-Cache aus API)
CREATE TABLE instruments (
    instrument_id   INTEGER PRIMARY KEY,
    symbol          TEXT NOT NULL,
    name            TEXT,
    sector          TEXT,
    asset_class     TEXT,
    last_updated    TEXT NOT NULL
);

-- TA-Signale (von data_worker, gelesen von signal_worker)
CREATE TABLE signals (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    instrument_id   INTEGER REFERENCES instruments(instrument_id),
    generated_at    TEXT NOT NULL,
    signal_type     TEXT NOT NULL,  -- 'RSI_OVERSOLD', 'BB_LOWER', etc.
    conviction      TEXT NOT NULL,  -- 'VERY_HIGH'|'HIGH'|'MEDIUM'|'LOW'
    score           REAL NOT NULL,
    rsi             REAL,
    macd_hist       REAL,
    bb_pct          REAL,
    expires_at      TEXT NOT NULL   -- Signale älter als 1h ignorieren
);

-- Trade State Machine (Kern des Systems)
CREATE TABLE trades (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    instrument_id   INTEGER REFERENCES instruments(instrument_id),
    symbol          TEXT NOT NULL,
    direction       TEXT NOT NULL CHECK(direction IN ('BUY','SELL')),
    status          TEXT NOT NULL CHECK(status IN (
                        'PENDING_APPROVAL',  -- von signal_worker erzeugt
                        'APPROVED',          -- von risk_worker freigegeben
                        'SUBMITTING',        -- execution_worker hat Lock
                        'ACTIVE',            -- bei eToro offen, positionID bekannt
                        'CLOSING',           -- SL/TP ausgelöst, warte auf Confirm
                        'CLOSED',            -- final
                        'FAILED',            -- API-Fehler
                        'REJECTED'           -- risk_worker hat abgelehnt
                    )),
    amount_usd      REAL NOT NULL,
    stop_loss_pct   REAL NOT NULL DEFAULT 3.0,
    api_position_id TEXT,           -- von eToro nach ACTIVE
    entry_price     REAL,
    exit_price      REAL,
    stop_loss_price REAL,
    pnl_usd         REAL,
    pnl_pct         REAL,
    rejection_reason TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now','utc')),
    approved_at     TEXT,
    submitted_at    TEXT,
    confirmed_at    TEXT,
    closed_at       TEXT,
    signal_id       INTEGER REFERENCES signals(id)
);

-- Live Portfolio (von reconciler gepflegt)
CREATE TABLE portfolio_snapshot (
    api_position_id TEXT PRIMARY KEY,
    instrument_id   INTEGER,
    symbol          TEXT,
    is_buy          INTEGER NOT NULL,
    amount_usd      REAL,
    open_price      REAL,
    current_price   REAL,
    unrealized_pnl  REAL,
    unrealized_pnl_pct REAL,
    stop_loss_rate  REAL,
    is_no_stop_loss INTEGER DEFAULT 0,
    last_synced     TEXT NOT NULL
);

-- Globaler System-State (Key-Value)
CREATE TABLE system_state (
    key             TEXT PRIMARY KEY,
    value           TEXT NOT NULL,
    updated_at      TEXT NOT NULL DEFAULT (datetime('now','utc'))
);
-- Keys: CURRENT_REGIME, PEAK_EQUITY, CURRENT_EQUITY, DRAWDOWN_PCT,
--       LAST_RECONCILE, LAST_DATA_FETCH, CB_ACTIVE

-- Einheitliches Logging
CREATE TABLE system_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              TEXT NOT NULL DEFAULT (datetime('now','utc')),
    level           TEXT NOT NULL CHECK(level IN ('DEBUG','INFO','WARN','ERROR','CRITICAL')),
    worker          TEXT NOT NULL,
    message         TEXT NOT NULL,
    details         TEXT
);
```

---

## Cron-Scheduling (Staggered — kein Thundering Herd)

| Worker | Schedule | Versatz | Zweck |
|--------|----------|---------|-------|
| `data_worker` | `*/5 * * * *` | :00 | Marktdaten + Signale |
| `risk_worker` | `1-59/5 * * * *` | :01 | Regime + SL |
| `reconciler` | `2-59/5 * * * *` | :02 | API↔DB Sync |
| `signal_worker` | `3-59/15 * * * *` | :03 | Kandidaten ranken |
| `execution_worker` | `4-59/15 * * * *` | :04 | Trade ausführen |
| `monitor_worker` | `*/30 * * * *` | :00 | Heartbeat Discord |

---

## API-Client (requests + tenacity)

```python
# src/bot/api/client.py
import requests
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

BASE_URL = "https://public-api.etoro.com/api/v1"
TIMEOUT = (5, 10)  # (connect, read) — nie mehr blockieren

@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(min=2, max=10),
    retry=retry_if_exception_type((requests.Timeout, requests.ConnectionError))
)
def api_get(endpoint: str, api_key: str, user_key: str) -> dict:
    resp = requests.get(
        f"{BASE_URL}{endpoint}",
        headers={"x-api-key": api_key, "x-user-key": user_key},
        timeout=TIMEOUT
    )
    resp.raise_for_status()
    return resp.json()
```

---

## Trade State Machine (Execution Worker)

```
signal_worker:     PENDING_APPROVAL  (Signal identifiziert)
risk_worker:       → APPROVED        (Regime OK, Cash OK, Gates bestanden)
                   → REJECTED        (Gates failed → done)
execution_worker:  → SUBMITTING      (Lock gesetzt, verhindert Doppelausführung)
                   → ACTIVE          (positionID von eToro bestätigt)
                   → FAILED          (API-Fehler)
reconciler:        → CLOSING         (Position nicht mehr in API)
                   → CLOSED          (final, PnL berechnet)
```

---

## Milestones

### Sprint 1: Foundation (Tag 1)
- [ ] `etoro_v3/` Projektstruktur anlegen
- [ ] `schema.sql` + `init_db.py` (DB anlegen, WAL-Mode, PRAGMA)
- [ ] `src/bot/api/client.py` (requests + tenacity, Config aus YAML)
- [ ] `src/bot/api/instruments.py` (get_instrument_map mit File-Cache)
- [ ] `src/bot/db/repo.py` (CRUD: get_active_trades, create_trade, update_trade_status)
- [ ] Config-Loading aus `config.yaml`

### Sprint 2: Core Logic (Tag 1-2)
- [ ] `src/bot/core/regime.py` (DRAWDOWN/NORMAL/RECOVERY aus system_state)
- [ ] `src/bot/core/risk.py` (Cash-Gate, Max-Positions, SL-Check)
- [ ] `src/bot/core/signals.py` (RSI, BB, MACD via yfinance)
- [ ] Unit Tests für core (keine DB, kein API nötig)

### Sprint 3: Workers (Tag 2-3)
- [ ] `reconciler.py` — zuerst (braucht funktionierende DB + API)
- [ ] `data_worker.py`
- [ ] `risk_worker.py`
- [ ] `signal_worker.py`
- [ ] `execution_worker.py` (State Machine, Lock auf SUBMITTING)
- [ ] `monitor_worker.py` (Discord Embeds aus discord_embeds.py übernehmen)

### Sprint 4: Integration (Tag 3)
- [ ] Cron-Jobs einrichten (Staggered Scheduling)
- [ ] 48h Parallel-Betrieb: Altes System pausiert, neues beobachtet
- [ ] Altes System deaktivieren wenn V3 stabil

---

## Übernahme aus altem System

| Was übernehmen | Woher | Status |
|----------------|-------|--------|
| Discord Embeds (P1-P13) | `scripts/discord_embeds.py` | ✅ Direkt kopieren |
| Instrument Map Cache | `scripts/instrument_map.py` | Adapt für File-Cache |
| Discovery Engine Logic | `scripts/discovery_engine.py` | Adapt für `data_worker` |
| Trading Bible Konstanten | `scripts/trading_bible_constants.py` | In `config.yaml` |
| TA-Signal-Logik | `scripts/ta_engine.py` | In `core/signals.py` |

---

*V3 Greenfield. Alter Code in `/scripts/` bleibt als Read-Only Referenz.*
*Kein Patch-Betrieb mehr — entweder Alt oder Neu, nie beides live.*
