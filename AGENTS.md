# AGENTS.md — etoro_v3 (DOX-Child von ~/.hermes/workspace/AGENTS.md)

## Purpose

LIVE-Trading-Bot auf eToro (ECHTES GELD). 7 Worker auf Hermes-Cron
(5–120 min Takt), SQLite-WAL-DB, Trading Bible V5 (4-Regime, risk_scalar,
ATR-Profit-Leiter, Momentum-Fade). Der Bot läuft während JEDER Änderung weiter.

## Ownership

- Code/Tests/Docs in diesem Repo: der Agent (Commits auf `main`, Push nach Test-Grün).
- Alle Trading-Parameter (SL, Sizing, Regime-Thresholds, Loss-Limits) werden autonom vom
  LLM Review Worker optimiert (taeglich 20:00 UTC, src/bot/workers/llm_review_worker.py).
  Unveraenderliche Grenzen: BIBLE_HARD_LIMITS im Quellcode — diese ueberschreitet keine LLM.
- Cron-Zeitpläne: `~/.hermes/cron/jobs.json` (Root-Scope), gespiegelt in `crontab.txt` hier.

## Local Contracts (Invarianten)

- Einzige DB: `data/trading.db`. Kill-Switch: `data/kill_switch.flag`
  (JSON scope: daily=Auto-Clear nächster UTC-Tag, sonst manuell).
- State-Machine: PENDING_APPROVAL→APPROVED→SUBMITTING→ACTIVE→CLOSING→
  CLOSED/FAILED/REJECTED. Neue Übergänge brauchen Reconciler-Recovery-Pfad.
- eToro-API: 200/statusID=1 = Order ANGENOMMEN, nicht ausgeführt →
  Portfolio-Polling-Verifikation Pflicht. Kein SL-Update-Endpoint.
- Einmal-Aktionen pro Position IMMER persistieren (position_state) —
  sonst feuert der 5-min-Zyklus endlos.
- DB-Migrationen idempotent (ALTER TABLE in try/except, je Spalte einzeln).
- Worker-Wrapper liegen in `~/.hermes/scripts/v3_*.sh` — Script-Änderungen
  in `scripts/` müssen dorthin kopiert werden, sonst läuft der Cron alt.

## Work Guidance

Workflow: Skill `finance/etoro-v3-safe-change` befolgen (lesen → pure
Function → Tests → volle Suite → Commit/Push → Live-Verifikation).
Debugging: Skills `finance/trading-system-debugging`, `ghost-order-debugging`.
Regeln: Skill `finance/trading-bible-v5`. NIEMALS-Liste der Root-AGENTS.md
gilt uneingeschränkt.

## Verification

```bash
PYTHONPATH=src python3 -m pytest                  # volle Suite, alles grün
sqlite3 -readonly data/trading.db "SELECT key,value FROM system_state WHERE key LIKE 'LAST_RUN_%'"
bash scripts/etoro_kill_switch_watchdog.sh        # leer = gesund
```

---

## Instrument-Katalog & Tradability (seit 2026-07-09)

### instruments-Tabelle (neue Spalten)

| Spalte | Typ | Bedeutung |
|--------|-----|-----------|
| `is_tradable` | INTEGER NULL | 1=handelbar, 0=nicht handelbar, NULL=noch nicht geprüft |
| `tradability_checked_at` | TEXT NULL | ISO-Timestamp der letzten Eligibility-API-Prüfung |

### Tradability-Filter-Kette

Alle Worker filtern non-tradable Instrumente heraus:

```
instruments.is_tradable = 0
        │
        ├── data_worker.py        (_get_watchlist_from_db: kein yfinance-Fetch)
        ├── discovery_worker.py   (_get_tradable_instruments: kein Signal-Scan)
        ├── signal_worker.py      (Bulk-Filter nach get_fresh(), vor Sortierung)
        └── open_position()       (allowOpenPosition=false → FAILED, kein Retry)
```

`is_tradable IS NULL` wird überall wie `is_tradable = 1` behandelt (fail-open bei
ungeprüften Instrumenten, damit neu importierte Instrumente nicht dauerhaft stumm bleiben).

### eToro Eligibility API

```
POST /api/v2/trading/info/eligibility
Body: {"instrumentIds": [1,2,...], "currency": "USD"}   # max 100 IDs pro Request

Response:
{
  "eligibilities": [
    {
      "instrumentId": 1,
      "allowOpenPosition": true,   # Instrument generell handelbar (statisch → wöchentl. Sync)
      "allowEntryOrders": true,    # Markt gerade offen (real-time — ersetzt market_hours)
      "leverageConfigs": [...],
      "minPositionExposure": 50
    }
  ],
  "notFoundInstrumentIds": [999]   # Instrument nicht mehr im Katalog → is_tradable=0
}
```

**Wichtig:** `GET /market-data/instruments?instrumentIds=1,2,3` unterstützt
kein Batching (HTTP 500 bei mehreren IDs) und enthält kein `allowOpenPosition`.
Ausschliesslich die Eligibility-API für Tradability-Checks verwenden.

### Sync-Scripts

| Script | Zweck | Cron |
|--------|-------|------|
| `scripts/sync_instrument_catalog.py` | Importiert neue Instrumente aus eToro-Vollkatalog (`GET /market-data/instruments` ohne Parameter → ~15k Instrumente). Neue: `is_active=0, is_tradable=NULL`. Weggefallene: `is_active=0`. | Manuell (monatlich) |
| `scripts/sync_instrument_tradability.py` | Prüft `allowOpenPosition` via Eligibility-API für alle aktiven Instrumente. TTL=30 Tage. Verarbeitet max 500 Instrumente pro Run. 100er-Batches, 3s Sleep (Rate-Limit ~20 req/min). | Wöchentlich So 03:30 UTC (`c4d9e1f2a7b3`) |

```bash
# Einmaliger Vollabgleich nach eToro-Katalog-Erweiterung:
python3 scripts/sync_instrument_catalog.py

# Tradability für neue Instrumente (NULL priority, dann älteste zuerst):
python3 scripts/sync_instrument_tradability.py
```

---

## 24/5 Trading — DEFER-Architektur (seit 2026-07-09)

Statische Marktzeiten-Prüfungen (`is_market_open()`) wurden aus dem BUY-Pfad
entfernt. eToro entscheidet live via `allowEntryOrders`.

### Vor der Änderung

```
signal_worker  →  is_market_open()? Nein → SKIP (Signal nie placed)
execution_worker → is_market_open()? Nein → FAILED (permanent)
open_position() →  is_market_open()? Nein → {"success": False} → FAILED
```

### Nach der Änderung

```
signal_worker  →  kein statischer Check — allowEntryOrders prüft eToro live
execution_worker → is_market_open()? Nein → DEFER (bleibt APPROVED, retry 15min)
open_position() →  allowEntryOrders=false → {"success": False, "error": "...allowEntryOrders..."}
execution_worker → allowEntryOrders in block_error → DEFER (bleibt APPROVED)
```

**data_worker** behält `is_market_open()` (Zeile ~758): Live-Preissignale brauchen
offene Märkte für valide yfinance-Daten.

### DEFER-Regel

Ein Trade im Status APPROVED wird NIEMALS wegen geschlossenem Markt auf FAILED gesetzt.
Er bleibt APPROVED und wird vom execution_worker alle 15 Minuten neu versucht.
Sobald `allowEntryOrders=true`, geht die Order durch.

Block-Typen und ihre Behandlung:

| Block-Grund | Quelle | Behandlung |
|-------------|--------|------------|
| `allowEntryOrders=false` | Eligibility-API | DEFER (APPROVED bleiben) |
| `is_market_open()=False` (statisch) | market_hours | DEFER (APPROVED bleiben) |
| `allowOpenPosition=false` | Eligibility-API | FAILED (permanent) |
| SL-Gate-Verletzung | leverageConfigs | FAILED (permanent) |
| price=0 / API-Fehler | eToro-API | FAILED (permanent) |

---

## Signal-Abdeckung (seit 2026-07-09)

### Signal TTL und EU-Chunk-Rotation

| Parameter | Alt | Neu | Grund |
|-----------|-----|-----|-------|
| `SIGNAL_TTL_MINUTES` | 360 (6h) | 1440 (24h) | Voller EU-Rotationszyklus = 16h |
| EU Chunks/Run | 1 | 2 | Halbe Rotationszeit |
| EU Vollzyklus | 32h | 16h | 16 Chunks × 2h × 1 Run/2 Chunks |

Signal-Scoring: `_signal_age_factor()` deprioritisiert ältere Signale automatisch
im Sort — 23h-alte Signale konkurrieren nie gleichwertig mit frischen.

### Abdeckungs-Ziel

~4.150 tradable Instrumente haben durchgehend gültige Signale:
- US/Crypto/Forex: discovery_worker scannt alle pro Run
- EU-Aktien: 2 Chunks × 16h Rotation = jedes Instrument alle 16h gescannt,
  Signal 24h gültig → 8h Puffer ohne Signal-Lücke

---

## eToro API Surface (Übersicht)

| Endpoint | Methode | Nutzung |
|----------|---------|---------|
| `/market-data/instruments` (kein Param) | GET | Vollkatalog ~15k Instrumente (catalog sync) |
| `/market-data/instruments?instrumentIds=N` | GET | Einzelnes Instrument-Metadata (kein Batch) |
| `/api/v2/trading/info/eligibility` | POST | allowOpenPosition + allowEntryOrders (max 100 IDs) |
| `/api/v2/trading/info/real/positions` | GET | Offene Positionen, netProfit, investmentAmount |
| `/api/v2/trading/info/real/pnl` | GET | PnL-Übersicht |
| `/api/v2/trading/order/open` | POST | Neue Position öffnen |

---

## Child DOX Index

Keine Children — dieses Repo ist eine Einheit.
