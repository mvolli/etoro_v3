# AGENTS.md — etoro_v3 (DOX-Child von ~/.hermes/workspace/AGENTS.md)

## Purpose

LIVE-Trading-Bot auf eToro (ECHTES GELD). 8 Worker auf Hermes-Cron
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
- **Zwei Symbol-Namespaces, nie vermischen** (fix/identity-gate-namespace,
  2196.HK-Incident 2026-07-29): `instruments.symbol` = eToro-Namespace
  (live verifiziert: '02196.HK', 'CAR.ASX', '836.HK'), `instruments.
  yfinance_symbol` = Yahoo-Namespace ('2196.HK', 'CAR.AX', '0836.HK').
  Die Padding-Konvention ist pro Instrument unterschiedlich und NICHT
  algorithmisch ableitbar (00027.HK ↔ Yahoo 0728.HK ist eine kuratierte
  Umbenennung, keine Null-Auffuellung). Alles Richtung eToro-API
  (Identity-Gate, Order-Bau) MUSS `instruments.symbol` per instrument_id
  aufloesen — siehe `execution_worker._canonical_symbol()`. Den Vergleich
  im Gate aufzuweichen ist verboten: er faengt den DOT-USD-Futures-vs-
  Spot-Incident. Discovery/Watchlist arbeiten im yfinance-Namespace —
  Schreiber Richtung Trading-Pfad (z.B. `core_sweep_whitelist`) muessen
  vorher kanonisieren.
- Worker-Wrapper liegen in `~/.hermes/scripts/v3_*.sh` — Script-Änderungen
  in `scripts/` müssen dorthin kopiert werden, sonst läuft der Cron alt.
- Trade-Event-Ledger `trade_events` (feat/pnl-nachreport, 2026-07-28):
  JEDER Open/Partial/Full-Close wird via `bot.core.event_log.
  record_posted_event()` persistiert — inkl. Discord-`message_id`, damit
  der Reconciler (9d/9e) Close-Embeds nach History-Match EDITIERT statt
  doppelt zu posten. Unbekanntes P/L ist durchgängig `None`, NIE `0.0`
  (Embed rendert grau „P/L folgt (Nachreport)"). Kein History-Match ⇒
  Trade bleibt PENDING (`verify_attempts`++), nach 7 Tagen UNRESOLVED —
  niemals VERIFIED ohne echte Zahlen.
- Discord-Channels (hartkodiert in `src/bot/discord_embeds.py`, config.yaml
  ist dead config): MAIN #etoro-trading, TRADE #trades, REPORTS #reports.
  Trade-Events (Fills/Closes/Partials, auch KI-EXIT/TIGHTEN) → #trades;
  Worker-Summaries → MAIN; Tagesreport → #reports.
- discord_embeds hat One-Shot-Slots (`_PENDING_CHART`, `_LAST_POST`) und
  wird von trailing_stop per importlib als EIGENE Instanz geladen —
  attach_chart/post/get_last_post immer am selben Modul-Objekt aufrufen.
- **Tests, die `execute_trailing_actions` mit erfolgreichem Close durchlaufen,
  MÜSSEN Discord stummschalten** (`_post_closed_embed`, `_get_discord_embeds`,
  `_verify_partial_close` patchen). Der Pfad postet sonst in den LIVE-Channel
  #trades — 2026-08-12 gingen so 7 erfundene „TINY"-Meldungen raus.
- **Jeder Pfad, der Positionen schliesst, braucht Market-Guard + PENDING-Muster.**
  eToro antwortet bei HK/ASIA langsam; `verify_full_close` läuft dort in den
  Timeout, obwohl der Close real ist. Unverifiziert ⇒ PENDING verbuchen, NICHT
  als Fehler (sonst Retry-Schleife: 165 s Blockade je 5-min-Lauf, belegt am
  2026-08-12 mit 2883.HK bei geschlossener HK-Börse).

## Portfolio-Grenzen (Stand 2026-08-12)

Vier Ebenen, alle im `check_buy_gate`-Pfad UND im Core-Sweep:

| Ebene | Grenze | Verhalten bei Überschreitung |
|-------|--------|------------------------------|
| Gesamt-Exposure | 75 % (`MAX_TOTAL_EXPOSURE_PCT`) | Pre-Trade-Block **+ Post-Trade-Auto-Trim** (LIFO, `risk.exposure_auto_trim`) |
| Sektor | 20 % (`sector_limits.max_per_sector_pct`) | Block. Quelle: `instruments.sector` (yfinance) |
| Region | Soft 35 % / Hard 50 % (`region_limits`) | Sizing-Damper, erst über Hard-Cap Block |
| Instrument | 10 % Default (`INSTRUMENT_LIMITS`) | Block + LIFO-Trim |

- **Core-Sweep ist KEIN gate-freier Pfad mehr** (fix/core-sweep-portfolio-gates):
  `plan_core_sweep` bekommt `total_exposed`/`max_exposure_pct` (deckelt
  `deployable` als Sizing-Input) und `correlation_gate` (injiziert). Wer dort
  Kandidaten hinzufügt, muss beide Argumente durchreichen.
- **`instruments.sector`** füllt `scripts/sync_instrument_sectors.py`
  (yfinance, ~5,6 s/Symbol, 200/Run, TTL 90 d, Priorität: gehalten → Whitelist
  → nie geprüft). `'unknown'` = kein Sektor vorhanden (Forex/Rohstoff/Index),
  wird wie NULL behandelt (fail-open).
- **`resolve_asset_class`**: kuratiertes `ASSET_CLASS_MAP` hat Vorrang, AUSSER
  bei FINANCIAL/CONSUMER/HEALTHCARE/ENERGY — die liefern nur den 20 %-Default
  und weichen dem DB-Sektor, sonst entstehen zwei Töpfe für eine Branche.
- **Profit-Leiter**: `PROFIT_LADDER_ATR_SCALE` (config `trailing.profit_ladder.
  atr_scale`, aktiv 0.35) skaliert `atr_mult` **und** `min_pct` aller Stufen,
  `max_pct` NICHT. Die Leiter ist pro Position in `position_state.
  profit_levels_json` EINGEFROREN — eine Skalen-Änderung erreicht bestehende
  Positionen nur nach Reset (`scripts/migrate_profit_ladder_reset.py`, setzt
  nur zurück, wo `levels_taken` leer ist).
- **`update_status(…, 'CLOSED')` setzt `closed_at` selbst**, wenn keins
  mitkommt — `llm_review_worker` und `config_experiment_worker` filtern
  darauf, eine Lücke verschluckt Trades still aus der Lernschleife.
- **`max_positions` existiert nicht mehr** (weder Gate, Konstante, Config-Key
  noch BIBLE-Limit). Grenzen sind Exposure/Cash/Instrument/Sektor/Region.

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
| `scripts/sync_instrument_sectors.py` | Füllt `instruments.sector`/`industry` aus yfinance (Quelle für das Sektor-Gate). TTL=90 Tage, 200/Run (~19 min bei ~5.6s/Symbol). Priorität: gehaltene Positionen → Core-Sweep-Whitelist → nie geprüft → älteste. Eigener `worker_lock('sector_sync')`. | Täglich 03:50 lokal (`629f07e94d80`, Wrapper `v3_sector_sync.sh`) |

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

## Signalqualitaet & Liquidity-Tiering (seit 2026-07-26)

### Liquidity-Tiering (`src/bot/core/liquidity.py`)

- `instruments` hat jetzt `adv_usd` (20d-Dollar-Volumen, data_worker schreibt
  im 5-min-Takt) und `market_cap` (USD-Naeherung, `scripts/backfill_liquidity.py`).
- Kandidaten-Ranking im signal_worker: 5. Sort-Term `liquidity_factor` [0.6..1.1]
  (min-Regel aus Market-Cap- und ADV-Tier, unbekannt = 1.0 neutral, fail-open).
  Config-Schalter: `trading.liquidity_tiering`.
- data_worker speichert KEINE BUY-Signale mehr unter `MIN_ADV_USD` (500k).
- FX-Naeherungen fuer Nicht-USD-Boersen zentral in `liquidity.currency_factor()`
  (nur fuers Tier-Bucketing — NIE fuer Bewertungen/PnL benutzen).

### Falling-Knife-Gate (`signals.py`)

`is_falling_knife()`: >=4 rote Tage ODER ROC5d <= -12% ODER >=2.5 ATR unter
SMA20 → Dip-Buy-Regeln (1,2,3,5) gesperrt; Rule 4 (MACD-Wende) bleibt erlaubt.
Metriken `consecutive_down_days`/`roc_5d_pct` kommen aus `compute_indicators()`.

### Lernschleife

- Combo-Conviction = SCHWAECHSTE Komponente (nicht mehr beste).
- LLM-Weights: Combo-Keys wirken auf Obermengen-Combos, `skip` zerlegt Combos.
- Kelly (`sizing.py`) poolt bei duennem Exact-Match auf Komponenten-Ebene.
- Core-Sweep-Trades tragen synthetisches Signal `CORE_SWEEP` (CONSUMED) —
  sichtbar fuer Scorecard/Kelly.
- news_flags_worker flaggt zusaetzlich Analysten-Kursziele (Preis >5% ueber
  Konsens → CAUTION, >25% → AVOID, Quelle `analyst_target`).

### Tote Tabelle: `ohlcv_daily`

Wird seit Pausierung des Legacy-Jobs `scripts/discovery_cron.py` (2026-07-03)
von NIEMANDEM mehr geschrieben oder gelesen (Stand 2026-07-26, max date
2026-07-01). Alle Live-Grader holen frische Daten direkt via yfinance.
NICHT als aktuelle Datenquelle verwenden.

---

## Child DOX Index

Keine Children — dieses Repo ist eine Einheit.
