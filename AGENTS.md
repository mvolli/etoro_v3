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
- **eToro-Positions-Payload hat KEINEN `symbol`-Key** (fix/trailing-symbol-
  resolution 2026-08-12): nur `instrumentID`. Wer aus Positionen ein Symbol
  braucht, löst über `instruments` auf (`trailing_stop.load_symbols()`,
  Batch). Ein `pos.get('symbol', str(pos.get('instrumentID')))` liefert
  garantiert die ID — und `is_market_open()` leitet die Börse aus dem
  ERSTEN Argument ab, ein korrektes `yf_symbol` im zweiten rettet nichts.
  Folge des Fehlers: der Market-Guard war für 48 von 60 Positionen blind
  (`is_market_open('3364','9633.HK','')` → True, obwohl HK zu), und
  9633.HK lief 2 Tage ohne Break-Even-Schutz.
- **Market-Key-Auflösung: der US-Default ist eine Antwort, kein Fehler**
  (fix/market-key-unknown 2026-08-17, Restlücke des 9633.HK-Incidents).
  `_get_market_key()` fiel für alles Unerkannte auf `'US'` zurück — einen
  DEFINIERTEN Key. Damit war der `fail_open`-Schalter konstruktionsbedingt
  tot: er greift nur bei Keys, die in `MARKET_DEFINITIONS` FEHLEN. Der Guard
  beantwortete still „ist die NYSE offen?" statt „ist die richtige Börse
  offen?". Drei Regeln halten das jetzt zusammen:
  1. **Beide Namespaces tragen Börsen-Suffixe.** `instruments.symbol` ist der
     eToro-Namespace mit EIGENEN Kürzeln (`.ASX`, `.ZU`, `.NV`, `.DH`, `.US`,
     `.RTH`, `.FUT`), `yfinance_symbol` benutzt andere (`.AX`, `.SW`).
     `SUFFIX_TO_MARKET` muss BEIDE kennen. Vor dem Fix fielen 751 tradable
     Instrumente (382 `.ASX`, 113 `.OL`, 89 `.CO`, 62 `.HE`, 55 `.ZU` …) auf
     `'US'` — mit Folgen: `BHP.ASX` wurde 8×, `STERV.HE` 3×, `YAR.OL`/
     `ELMRA.OL`/`APA.ASX` je 2× mit „Markt >4h geschlossen — Signal veraltet"
     verworfen, weil `execution_worker` während der ASX-/Oslo-Session die
     geschlossene NYSE abfragte.
  2. **Der yfinance-Fallback gilt NUR für Symbole MIT Suffix.** Ein
     suffixloses eToro-Symbol ist eine US-Notierung — auch wenn yfinance auf
     den Heimatmarkt zeigt: `HSBC`/`SONY`/`TM`/`BHP` sind NYSE-ADRs (yf
     `HSBA.L`/`6758.T`/`7203.T`/`BHP.AX`) und handeln zu US-Zeiten. Ein
     Fallback ohne diese Bedingung zieht 26 ADRs auf falsche Handelszeiten.
  3. **Rein numerische Symbole → `UNKNOWN_MARKET_KEY`** (bewusst NICHT in
     `MARKET_DEFINITIONS`). Das ist keine Aktie, sondern eine durchgereichte
     `instrumentID` — der Fallback in `trailing_stop.load_symbols()`, wenn die
     ID nicht auflösbar ist. Erst damit greift `fail_open` überhaupt.
- **`fail_open` ist am Exit-Boundary bewusst `True` — nicht vergessen, sondern
  entschieden.** Naheliegend wäre, den Trailing-Pfad analog zur BUY-Boundary
  auf `fail_open=False` zu ziehen. Das ist FALSCH und darf nicht „nachgezogen"
  werden: für eine Position, deren Börse wir nicht bestimmen können, hieße
  fail-closed, den Break-Even-Boden dauerhaft abzuschalten — derselbe Schaden
  wie im 9633.HK-Incident, nur über einen anderen Weg (vgl. „BE_CLOSE/SL sind
  Verlustschutz — Retry NIE unterdrücken"). Der Market-Guard ist auf
  Exit-Pfaden eine Effizienzmaßnahme gegen 165-s-Timeouts, die das
  PENDING-Muster ohnehin abfängt, kein Sicherheitsgatter. Die Asymmetrie:
  **nicht kaufen kostet nichts, nicht verkaufen kostet Geld.**

  | Aufrufstelle | `fail_open` | Grund |
  |---|---|---|
  | `data_worker` Tier-2-Fetch + BUY-Signal-Store | True | Datenpfad |
  | `signal_worker` Kandidaten-Slot | **False** | BUY, Skip kostet nichts (Signal bleibt FRESH) |
  | `execution_worker` Order-Gate | **False** | BUY, DEFER statt Ghost-Order |
  | `trailing_stop._action_market_open` (BE_CLOSE/SL) | True | Verlustschutz |
  | `risk_worker` Exposure-Trim | True | Positionsabbau |
  | `llm_execution` EXIT | True | Positionsabbau |

  Die Börsenfelder holt überall `market_hours.resolve_market_fields(db,
  instrument_id)` → `(symbol, yfinance_symbol, category)` oder `None`.
  **Nie wieder inline abfragen** — die Query stand vorher viermal im Baum,
  und `execution_worker`/`llm_execution` fragten ganz ohne sie (Krypto und
  Forex liefen dort an NYSE-Zeiten: `ETC` wurde 9×, `BTC`/`DOT`/`BCH`
  mehrfach mit „Markt >4h geschlossen" verworfen, obwohl 24/7 handelbar).
  `None` heißt „Zeile fehlt" und der Aufrufer entscheidet die Richtung —
  `risk_worker` nutzt das für sein fail-open.
- **`instruments.yfinance_symbol` ist streckenweise falsch — nie ungeprüft
  als Marktsignal verwenden.** 149 aktive Aktien (135 tradable) tragen einen
  Krypto-Ticker, **118 davon denselben**: FNMA/USB/FITB/GBCI stehen alle auf
  `'BNT-USD'`, 00285.HK/669.HK/STMMI.MI auf `'TRX-USD'`, ADE.DE auf
  `'BTC-USD'`. `resolve_market_fields()` verwirft deshalb ein `-USD`/`=X`/
  `=F`/`^`-Symbol, wenn `asset_class` `stock`/`etf` ist — für eine Aktie ist
  es beweisbar falsch, und ungefiltert hätte die `-USD`-Heuristik in
  `_get_market_key` US-Bankaktien am BUY-Gate auf 24/7 gesetzt.
  **Offen und NICHT von diesem Guard gedeckt: der Preis-Pfad.** `data_worker`
  zieht Kurse über `yfinance_symbol` — für diese 149 Instrumente also
  potenziell die Reihe eines fremden Assets. Vor dem nächsten Signal-Audit
  prüfen (`scripts/sync_instrument_catalog.py` ist die Quelle).
- **BE_CLOSE/SL sind Verlustschutz — Retry NIE unterdrücken.** Für optionale
  Korrekturen (Exposure-Trim) ist „unverifiziert ⇒ PENDING, kein Retry"
  richtig; für den Break-Even-Boden hieße das, ihn still abzuschalten. Bei
  Alarm-Spam die Meldung drosseln (`TRAILING_ERR_EMBED`, signaturbasiert),
  nicht die Aktion.
- **Keine Datenkürzung in Embeds** (fix/embeds-no-hidden-data 2026-08-12):
  Zeilen NIE per `[:N]` oder `"…+N weitere"` abschneiden — `pack_lines_into_fields()`
  legt Folgefelder an, `_split_embed()` verteilt auf bis zu 10 Embeds je
  Nachricht. `_clip_embed_limits` kappt nur noch Zeichenlängen, nicht die
  Feldanzahl. Als Netz teilt `_split_embed` jedes Feld >1024 Zeichen selbst
  auf — auch für Aufrufstellen, die den Packer nicht kennen.
- **Tests, die `execute_trailing_actions` mit erfolgreichem Close durchlaufen,
  MÜSSEN Discord stummschalten** (`_post_closed_embed`, `_get_discord_embeds`,
  `_verify_partial_close` patchen). Der Pfad postet sonst in den LIVE-Channel
  #trades — 2026-08-12 gingen so 7 erfundene „TINY"-Meldungen raus. Gleiches
  gilt für Embed-Tests: gegen ein gepatchtes `_request_discord` prüfen (Payload
  inspizieren), nie gegen das Netz.
- **Corporate-Action-Guard** (feat/corporate-action-guard 2026-08-17,
  `src/bot/core/corporate_actions.py`): Yahoo bereinigt Splits/Dividenden
  RUECKWIRKEND, aber mit Verzoegerung. Im Lag-Fenster steht ein Niveausprung
  in der `auto_adjust=True`-Reihe, der RSI/MACD/BB/ATR/SMA20/50 GLEICHZEITIG
  verfaelscht — deshalb sperrt das Gate ALLE sieben BUY-Regeln (anders als
  das Knife-Gate, wo Rule 4 ueberlebt) und sitzt als EINE Stelle vor der
  BUY-Aggregation in `generate_signal`. SELL und alle Exit-Pfade bleiben
  frei: Risiko abbauen darf ein Datenverdacht nie blockieren.
  Selbstheilend — sobald Yahoo anpasst, ist der Sprung weg, kein State.
  **Die Heuristik allein reicht nicht:** bei JMAT.L wirkten am 2026-08-17
  Zusammenlegung (0.75) und Sonderdividende (476,5 p) gemeinsam, das Ergebnis
  (-21,9 %, Ratio 0.7806) verfehlt 3:4 um 4,1 % und die Extremschwelle um
  13 pp. Deshalb Pfad C: `ConfirmBudget.annotate()` holt in data_worker und
  discovery_worker die echte Action-Historie — nur fuer Symbole MIT Sprung
  (sonst null Netz-Calls) und gedeckelt auf CA_CONFIRM_MAX_PER_RUN.
  Dividenden zaehlen erst ab CA_MATERIAL_DIV_PCT (5 %) vom Kurs, sonst
  bestaetigt jeder Quartalszahler alles.
  **Bestaetigungs-Cache mit asymmetrischem TTL** (perf/ca-confirm-cache,
  `src/bot/core/ca_confirm_cache.py`): der Sprung steht CA_SCAN_BARS (50) Bars
  in der Reihe, ohne Cache lief dieselbe Abfrage 288-mal am Tag je Symbol
  (+4,6 s je data_worker-Lauf, gemessen 2026-08-17 an KRS.L/ADME.L). Jeder Lauf
  ist ein eigener Prozess ⇒ Cache als Tabelle in trading.db, nicht im Prozess.
  Schluessel = `symbol|gap_date|ratio` — `ca_gap_bars_ago` waere untauglich
  (verschiebt sich mit jeder Bar), und ohne Sprung-Datum im Schluessel maskiert
  ein alter Negativ-Eintrag einen neuen Sprung. NEGATIVER TTL 6 h, positiver
  24 h: das Guard-Fenster IST das Lag zwischen Ex-Tag und Yahoos Anpassung
  (Stunden bis Tage) — ein negativer Tages-TTL verschluckte genau dieses
  Fenster und schaltete Pfad C still ab. Cache-Treffer verbrauchen das
  Netz-Budget NICHT, sonst verhungern frische Symbole hinter bekannten.
  NICHT betroffen und auch nicht noetig: der P/L-Pfad. `trailing_stop`
  rechnet `pnl_pct` aus eToro-`netProfit`/`investmentAmount` und
  `entry_price` kommt aus `openRate` — broker-seitig bereits bereinigt.
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
  → nie geprüft). Forex/Rohstoff/Index/Krypto hat yfinance KEINE Sektoren —
  die leitet das Script aus `asset_class` ab (`bot/core/sector_taxonomy.py`:
  FOREX/COMMODITY/INDEX/CRYPTO, fix/forex-sector-unknown 2026-08-20), sonst
  'unknown'. `'unknown'` wird wie NULL behandelt (fail-open).
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

## Haupt-Konto-Report (seit 2026-08-20)

`src/bot/workers/main_report_worker.py` berichtet taeglich 23:30 nach
**#reports** ueber das HAUPTKONTO des Users — zusaetzlich zum Bot-Tagesreport
(23:15). Zwei Abgrenzungen, die nicht aufgeweicht werden duerfen:

- **Keys:** ausschliesslich `ETORO_MAIN_*`. Der API-Key ist bei Bot- und
  Hauptkonto IDENTISCH, nur der USER-Key trennt sie — ein vertauschter
  User-Key liest klaglos das falsche Konto und der Report waere still falsch.
- **DB:** `data/main_portfolio.db`, NICHT `trading.db`. Auf `trading.db` wird
  nur LESEND zugegriffen (instrument_id -> Symbol/Sektor/yfinance_symbol).

Weiteres: `unrealizedPnL` auf Portfolio-Ebene ist autoritativ und weicht
bewusst von der Positions-Summe ab — die Differenz ist das Innenleben der
Copy-Trading-Mirrors und wird im Report ausgewiesen, nicht wegge­rechnet.
Der Snapshot wird ERST nach dem Posten gespeichert, sonst vergleicht ein
Wiederholungslauf gegen sich selbst. Erster Lauf = `is_baseline`, keine
Bewegungslisten (sonst meldete er "+71 Positionen").

## Work Guidance

Workflow: Skill `finance/etoro-v3-safe-change` befolgen (lesen → pure
Function → Tests → volle Suite → Commit/Push → Live-Verifikation).
Debugging: Skills `finance/trading-system-debugging`, `ghost-order-debugging`.
Regeln: Skill `finance/trading-bible-v5`.

### NIEMALS

1. **NIE** eine andere DB-Datei benutzen als `etoro_v3/data/trading.db`.
   `etoro_trading.db`, `etoro_v3.db`, `bot_state.db`, `db/etoro_trading.db`
   sind gelöschte Altlasten — wer sie referenziert, liest Phantom-Daten.
2. **NIE** Pfade unter `~/.hermes/workspace/etoro/` verwenden — archiviert.
3. **NIE** `/tmp/etoro_key.txt` oder Klartext-Keys in Dateien/Memories/Discord.
   Keys kommen ausschließlich aus `~/.hermes/.env`.
4. **NIE** Linux-crontab oder systemd für Bot-Jobs — der Bot läuft über
   `~/.hermes/cron/jobs.json`.
5. **NIE** pausierte (`enabled:false`) Cron-Jobs reaktivieren.
6. **NIE** alle Positionen auf einmal schließen (auch nicht bei Drawdown).
7. **NIE** Dateien außerhalb von tmp/cache löschen ohne Rückfrage bei VoLLi.
8. **NIE** Emails an Fremde, Käufe oder externe Posts ohne Rückfrage bei VoLLi.

Quelle: `~/.hermes/workspace/AGENTS.md`. Diese Liste steht hier im **Original**,
nicht als Verweis: Die AGENTS-Kette lädt "from git root down to cwd", und
`etoro_v3` ist ein eigenes Git-Repo — das Elternverzeichnis wird nie erreicht.
Ein blosser Verweis erreicht den Prompt also nicht. Änderungen an der
Root-Datei müssen hier nachgezogen werden.

## Verification

```bash
PYTHONPATH=src /usr/bin/python3 -m pytest           # volle Suite, alles grün
sqlite3 -readonly data/trading.db "SELECT key,value FROM system_state WHERE key LIKE 'LAST_RUN_%'"
bash scripts/etoro_kill_switch_watchdog.sh        # leer = gesund
git diff HEAD -- data/llm_signal_weights.json     # leer = Bot liest exakt die committeten Gewichte
```

**Weights-Drift-Check (Pflicht bei jeder Diagnostics/Review):** Ein sauberer
Commit heißt NICHT, dass der Bot ihn benutzt — der Worker liest `data/llm_signal_weights.json`
aus dem **Working Tree**, und `_update_signal_weights()` schreibt dort automatisch
beim LLM-Review-Lauf (Cron `30 22 * * *` — der Ausdruck steht in **Lokalzeit**: 22:30 CEST = 20:30 UTC, empirisch bestätigt durch `last_run_at`. `name` und `schedule_display` des Jobs behaupteten bis 2026-08-27 „20:00 UTC“ und sind korrigiert; fix/success-rate-denominator, 1abadd7). Wenn
`git diff HEAD -- data/llm_signal_weights.json` NICHT leer ist, ist die
Live-Abweichung vom belegten Stand — das ist die eigentliche Evidenz für
„aktive Gewichte ≠ committete Gewichte".

**Interpreter-Regel:** die Suite IMMER mit `/usr/bin/python3` (System-Python,
hat matplotlib 3.10.8) ausführen — die Cron-Worker laufen exakt darauf. Das bare
`python3` auf der WSL-PATH ist der `~/.hermes/hermes-agent/venv`, in dem
matplotlib NICHT installiert ist — alle chart/PNG-Tests (pulse_grid_png /
daily_grid_png / candle render) importieren matplotlib in try/except, geben
None zurück und scheitern daher leise unter dem Venv. Der Interpreter zählt:
`python3 -m pytest` allein reicht nicht. Stand 2026-08-20: 976 Tests grün
unter `/usr/bin/python3`.

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
| `scripts/sync_instrument_sectors.py` | Füllt `instruments.sector`/`industry`: Aktien/ETFs aus yfinance, Forex/Rohstoff/Index/Krypto aus `asset_class` (sector_taxonomy). TTL=90 Tage, 200/Run (~19 min bei ~5.6s/Symbol). Priorität: gehaltene Positionen → Core-Sweep-Whitelist → nie geprüft → älteste. Eigener `worker_lock('sector_sync')`. | Täglich 03:50 lokal (`629f07e94d80`, Wrapper `v3_sector_sync.sh`) |

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

### Datenanalyse-Kontrakt: 26.07.-Zaesur (Pflicht)

fix/combo-conviction-min (d0a07d7, 2026-07-26) aenderte die Combo-Conviction
auf die SCHWAECHSTE Komponente. Davor machte eine einzige VERY_HIGH-Komponente
die ganze Combo zu VERY_HIGH und damit zur groessten Position (Ø ~546 USD,
WR 10.9%, n=55) — seither existieren KEINE VERY_HIGH-Trades mehr.

REGEL: Jede Auswertung von Handelsdaten (WR, Conviction-Verteilung,
Sizing-Evidenz, Kelly/Score-Entscheidungen) MUSS per SQL auf
`trades.created_at >= '2026-07-26'` filtern (Phase 'nachher') ODER die Phase
explizit trennen. Pre-Fix-Zahlen duerfen NIE als Evidenz fuer aktuelles
Sizing/Conviction-Calibration zitiert werden; Vollverlauf nur mit klar
gelabelter Phase (vorher→nachher WR: 19.1% → 34.4%).

Kontext: Commit 56b6c04 (2026-08-24) flachte die Conviction-Leiter unter
anderem mit 'VERY_HIGH n=55 WR 10.9%' — PRE-Fix-Daten (Ziffern korrekt,
Kontext irrefuehrend). Korrektur-Note in `data/llm_trading_memory.json`
(strategy_notes, 2026-08-25).

### Tote Tabelle: `ohlcv_daily`

Wird seit Pausierung des Legacy-Jobs `scripts/discovery_cron.py` (2026-07-03)
von NIEMANDEM mehr geschrieben oder gelesen (Stand 2026-07-26, max date
2026-07-01). Alle Live-Grader holen frische Daten direkt via yfinance.
NICHT als aktuelle Datenquelle verwenden.

---

## Child DOX Index

Keine Children — dieses Repo ist eine Einheit.
