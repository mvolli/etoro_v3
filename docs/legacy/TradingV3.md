# 🤖 eToro Trading V3 — Vollautonomer Bot

**Erstellt:** 24. Juni 2026  
**Audit-Partner:** Gemini 2.5 Pro + Live-System-Daten  
**Basis:** TradingV2.md (umgesetzt), Live-Audit 24.06.2026 20:20 Uhr  
**Status:** AKTIV — M1 Sofort-Fixes beginnen

---

## 📊 SYSTEM-ZUSTAND (Audit-Snapshot 24.06.2026)

| Metrik | Wert | Status |
|--------|------|--------|
| Equity | ~$9,469 | ⚠️ -5.31% Drawdown |
| Peak | $10,000 | — |
| Regime | DRAWDOWN | 🔴 BUYs müssen blockiert sein |
| Offene Positionen | 18 | ⚠️ Nahe Limit |
| Pipeline läuft | NEIN | 🔴 KRITISCH |
| Letzte Pipeline | unklar | 🔴 Kein Timestamp |
| trade_queue FAILED | 83 (archiviert) | ✅ bereinigt |
| pending_orders FAILED | 54 | 🔴 hohe Fehlerrate |
| SL-Watchdog | Timeout-anfällig | 🔴 Schutz unsicher |

---

## 🔍 A) SYSTEM-AUDIT — Probleme nach Schwere

### 🔴 KRITISCH

**1. Pipeline läuft NICHT autonom**
- `etoro_full_pipeline.sh` existiert nicht im Workspace
- Hermes Cron-Timeout = 120s, Pipeline braucht 5-10min → immer Timeout
- **Gemini-Urteil:** "Fundamentaler Fehler. Alle anderen Probleme sind sekundär."

**2. 'WARNING' vs 'WARN' Bug**
- `system_log` CHECK constraint: `level IN ('INFO','WARN','ERROR','CRITICAL')`
- Code schreibt `'WARNING'` → `IntegrityError` → Pipeline crasht
- Getroffen in Tick #271, Tick #? (mindestens 2× nachgewiesen)
- **Gemini-Urteil:** "Klassischer Showstopper."

**3. Trades im DRAWDOWN-Regime (Trading Bible Rule 3 VERLETZT)**
- DB zeigt 3 FILLED Trades trotz aktivem DRAWDOWN-Regime
- `check_buy_gate()` hat Rule 0 (Regime-Gate), aber Aufrufer umgeht es möglicherweise
- **Gemini-Urteil:** "Gefährlichster Fehler — verbrennt aktiv Kapital."

**4. API Key-Probleme**
- `ETORO_API_KEY nicht gesetzt` mehrfach in Logs (06-23)
- `.env` wird in manchen Prozessstart-Kontexten nicht geladen

### 🟠 HOCH

**5. Phase-Timeouts unrealistisch**
- `Candidate Ranking: 60s` → braucht tatsächlich **420s**
- `Data Ingestion: 180s` → braucht tatsächlich **366s**
- Pipeline bricht willkürlich ab → inkonsistente Systemzustände

**6. Trade Poller frequent restarts**
- 5 Neustarts in 24 Minuten (15:37–16:01)
- Watchdog killt nach >2h wegen "Memory Leak Prevention"
- 2h ist zu aggressiv — während Neustart: blinder Fleck für Order-Status

**7. sector_exposure_tracking = 0 rows**
- Sektor-Limits in `check_buy_gate()` implementiert, aber Tracking-Tabelle leer
- Sektor-Konzentrationsrisiken werden faktisch NICHT enforced

**8. Extrem hohe Trade-Fehlerrate**
- 83 FAILED + 54 FAILED/EXPIRED Orders in 24h → inakzeptabel
- Root Cause: API-Kommunikation, Validierung vor Order-Platzierung?

### 🟡 MITTEL

**9. 41 Items in consolidation_queue unverarbeitet**
- Symptom der nicht laufenden Pipeline, aber auch eigenständiges Problem

**10. Legacy Scripts (15+ post_*.py) ungeklärt**
- `post_report.py`, `post_executor_discord.py`, `post_momentum_report.py` etc.
- Dead Code oder manuell verwendet? Widerspricht Autonomie-Ziel

**11. Deprecated `infrastructure_module.get_db()` in 5 aktiven Scripts**
- `monitoring_alerts.py`, `consolidation_worker.py`, `pipeline_watchdog.py`
- `performance_report.py`, `instrument_rotation.py`
- Inkonsistente DB-Verbindungslogik → Race Condition Risiko

**12. Ghost-Detection-Zeiten inkonsistent**
- `reconciler_service.py`: `GHOST_DETECTION_SECONDS = 120`
- `execution_module.py`: `GHOST_ORDER_WAIT_SECONDS = 30`
- Race Conditions möglich zwischen Poller und Reconciler

### 🟢 NIEDRIG

**13. Doppelte Verantwortung unified_pipeline / main_orchestrator**
- `unified_pipeline.py` orchestriert, `main_orchestrator.py` implementiert
- Architektonischer "Smell", aber kein akuter Fehler

---

## 📋 B) TRADING BIBLE KONFORMITÄT

| Rule | Status | Problem |
|------|--------|---------|
| Rule 1: Auto-SL (-3%) | ⚠️ UNSICHER | SL-Watchdog-Timeout → Schutz nicht garantiert |
| Rule 2: Symbol-Limits | ✅ | check_buy_gate() enforced |
| Rule 3: DRAWDOWN-Regime | 🔴 VERLETZT | 3 Trades trotz DRAWDOWN-Regime gefüllt |
| Rule 4: CB = kein Full-Close | ✅ | Korrekt implementiert heute |
| Sektor-Limits (20%/3 Sektoren) | 🔴 NICHT ENFORCED | sector_exposure_tracking leer |
| Cash-Gate (≥15%) | ✅ | check_buy_gate() enforced |
| Position Sizing | ✅ | anti-pyramiding aktiv |

---

## 🏗️ C) ARCHITEKTUR-ANALYSE

**Gemini-Urteil:** *"Die monolithische Pipeline ist für 100% Autonomie ungeeignet und nicht robust."*

**Kernproblem:** Single Point of Failure. Ein Bug (z.B. 'WARN' vs 'WARNING') legt das **gesamte System** lahm.

### Ziel-Architektur V3: Entkoppelte Worker

```
┌─────────────────────────────────────────────────────────┐
│                  MARKET HOURS AWARE SCHEDULER            │
├──────────┬──────────┬──────────┬───────────┬────────────┤
│  DATA    │ SIGNAL   │  RISK    │ EXECUTION │ MONITORING │
│ WORKER   │ WORKER   │ WORKER   │  WORKER   │  WORKER    │
│ (5min)   │ (15min)  │ (event)  │ (event)   │ (5min)     │
└────┬─────┴────┬─────┴────┬─────┴────┬──────┴────────────┘
     │          │          │          │
     ▼          ▼          ▼          ▼
┌─────────────────────────────────────────────────────────┐
│              SHARED STATE (SQLite WAL)                   │
│  signals | portfolio_state | approved_trades | regime    │
└─────────────────────────────────────────────────────────┘
```

**Jeder Worker ist:**
- Unabhängig (Fehler in einem blockiert andere nicht)
- Kurzlebig (startet, macht Arbeit, beendet sich)
- Idempotent (sicher mehrfach ausführbar)

---

## 🎯 D) V3 IMPLEMENTIERUNGSPLAN

### M1: SYSTEM-STABILISIERUNG — Feuer löschen (SOFORT, heute)

**Ziel:** Pipeline wieder autonom laufend, keine crashenden Bugs

- [ ] **M1.1: 'WARNING'→'WARN' Bug fixen**
  - Alle `level='WARNING'` in system_log-Schreibaufrufen → `'WARN'`
  - ODER: DB-Constraint anpassen: `level IN ('INFO','WARN','WARNING','ERROR','CRITICAL')`
  - Betroffene Files: alle die `insert_system_log()` aufrufen mit 'WARNING'

- [ ] **M1.2: Pipeline-Script + Cron-Timeout reparieren**
  - `etoro_full_pipeline.sh` erstellen (existiert nicht!)
  - Hermes Cron-Job `59e65fa133ee` Timeout auf ≥900s setzen
  - Pipeline Watchdog `9162816294a2` ebenfalls

- [ ] **M1.3: Phase-Timeouts auf realistische Werte anheben**
  - `Candidate Ranking`: 60s → **500s**
  - `Data Ingestion`: 180s → **420s**
  - `SL-Watchdog`: 120s → **180s** (API langsam)

- [ ] **M1.4: DRAWDOWN-Kaufblockade bombensicher machen**
  - Prüfen warum 3 Trades trotz DRAWDOWN-Regime FILLED wurden
  - `execute_buy_with_sl()` direkt absichern (nicht nur BUY-Gate)

- [ ] **M1.5: Sektor-Exposure-Tracking reparieren**
  - `sector_exposure_tracking` Tabelle wird nie befüllt → Bug finden
  - Temporärer Hotfix: bei jedem Reconciler-Lauf befüllen

**Deliverable:** Pipeline läuft wieder autonom. Keine CHECK-Fehler. DRAWDOWN-Regime 100% enforced.

---

### M2: TRADING BIBLE ENFORCEMENT HÄRTUNG (Woche 1)

**Ziel:** Alle Regeln sind bombensicher — kein Weg daran vorbei

- [ ] **M2.1: Doppelter Regime-Gate in execute_buy_with_sl()**
  - check_buy_gate() kann umgangen werden (direkter execute_buy_with_sl()-Aufruf)
  - Regime-Check auch direkt in `execute_buy_with_sl()` einfügen

- [ ] **M2.2: SL-Watchdog Batch-API-Calls**
  - Aktuell: 1 API-Call pro Position → bei 18 Positionen = 18 Calls → Timeout
  - Fix: Alle Positionen in einem `/trading/info/real/pnl`-Call holen, dann lokal prüfen
  - Ziel: <30s statt 120s+

- [ ] **M2.3: Sektor-Tracking live halten**
  - `sector_exposure_tracking` nach jedem Reconciler-Lauf aktualisieren
  - Gate nutzt echte Daten statt leere Tabelle

- [ ] **M2.4: Ghost-Detection-Zeiten vereinheitlichen**
  - Einheitlich: 60s (Kompromiss zwischen 30s zu kurz, 120s zu lang)
  - `reconciler_service.py` + `execution_module.py` + `trade_poller.py` anpassen

**Deliverable:** Alle Trading-Bible-Regeln sind lucker- und umgehungssicher enforced.

---

### M3: PIPELINE-ARCHITEKTUR MODERNISIERUNG (Woche 2-3)

**Ziel:** Entkoppelte, resiliente Worker statt monolithischer Pipeline

- [ ] **M3.1: Orchestrator konsolidieren**
  - `unified_pipeline.py` und `main_orchestrator.py` zusammenführen
  - Eine klare Datei: Orchestrierung. Implementierung in Modulen.

- [ ] **M3.2: Data Worker entkoppeln**
  - `data_module.py` + `instrument_rotation.py` als eigenständiger 5min-Prozess
  - Schreibt frische Signale in DB → andere Phasen lesen davon

- [ ] **M3.3: Legacy-Code aufräumen**
  - Alle 15+ `post_*.py` Scripts analysieren
  - Was gebraucht wird → in Pipeline integrieren
  - Rest → `scripts/archive/`

- [ ] **M3.4: infrastructure_module vollständig ersetzen**
  - Alle 5 verbleibenden Files → `DBContext()` Migration
  - `infrastructure_module.py` → `scripts/archive/`

**Deliverable:** Saubere, wartbare Architektur. Ein Fehler killt nicht mehr alles.

---

### M4: PERFORMANCE + STABILITÄT (Woche 3-4)

**Ziel:** Pipeline <60s, keine Timeouts, Poller stabil

- [ ] **M4.1: Candidate Ranking optimieren**
  - `yf.download()` Cache: Daten nicht bei jedem Lauf neu laden
  - Nur neu laden wenn Daten >15min alt
  - Ziel: <60s statt 420s

- [ ] **M4.2: Trade Poller Memory Leak fixen**
  - `memory-profiler` nutzen um echten Leak zu finden
  - Dann reparieren statt alle 2h neu starten
  - Watchdog-Interval: 2h → 8h (übergangsweise)

- [ ] **M4.3: Market Hours Awareness**
  - Pipeline läuft auch nachts / am Wochenende sinnlos
  - Bei geschlossenen Märkten: nur Heartbeat + Reconciliation, kein Trading
  - NYSE Marktzeiten: Mo-Fr 15:30-22:00 CET

**Deliverable:** Pipeline <120s, SL-Watchdog <30s, Poller läuft stabil 24h+.

---

### M5: MONITORING + DISCORD EMBEDS (Woche 4)

**Ziel:** Vollständige Sichtbarkeit, proaktive Alerts

- [ ] **M5.1: Embed für Pipeline-Status pro Tick**
  - Jeder 15min-Tick: welche Phasen liefen, welche Durations, Status
  - Nur bei Problemen posten (kein Spam bei NORMAL)

- [ ] **M5.2: DRAWDOWN-Alert**
  - Bei Regime-Transition: sofort Discord-Alert
  - NORMAL→DRAWDOWN: 🔴 Alert #etoro-trading
  - DRAWDOWN→RECOVERY: 🟢 Alert #etoro-trading

- [ ] **M5.3: Trade-Embed für jeden ausgeführten BUY/SELL**
  - Jeder FILLED Trade: Embed in #etoro-trades
  - Jeder GHOST/FAILED: Embed in #etoro-trades mit Grund

**Deliverable:** VoLLi sieht sofort was passiert, ohne in Logs schauen zu müssen.

---

### M6: VOLLAUTONOMIE (Woche 5-6)

**Ziel:** Zero manual intervention — wirklich autonom

- [ ] **M6.1: Recovery-Strategie nach DRAWDOWN**
  - Wenn DD < 2% (RECOVERY-Regime): automatisch neu starten mit kleineren Positionen
  - Kein manuelles Reset notwendig

- [ ] **M6.2: Discovery-Feedback-Loop**
  - Kandidaten die zu GHOST werden → automatisch aus Discovery-Watchlist entfernen
  - Kandidaten mit positiver PnL → Score erhöhen

- [ ] **M6.3: Kill Switch via Discord**
  - `/stop-trading` Befehl → alle neuen BUYs blockieren (kein Full-Close)
  - `/resume-trading` → System wieder freigeben
  - `/status` → aktueller Snapshot in Discord

**Deliverable:** System agiert vollständig autonom. VoLLi muss nur im Notfall eingreifen.

---

## ⚡ D) TOP 5 SOFORT-FIXES (heute noch)

### Fix #1: DRAWDOWN-Kaufblockade in execute_buy_with_sl() härten
```python
# In execution_module.py → execute_buy_with_sl() ganz am Anfang:
from cash_strategy import get_current_regime
if get_current_regime() == "DRAWDOWN":
    logger.warning("BUY BLOCKED: DRAWDOWN-Regime (execute_buy_with_sl guard)")
    return {"success": False, "error": "DRAWDOWN-Regime: BUY verboten"}
```

### Fix #2: 'WARNING' Bug in system_log
```sql
-- Option A: DB-Constraint erweitern (Migration)
ALTER TABLE system_log RENAME TO system_log_old;
CREATE TABLE system_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT,
    level TEXT CHECK(level IN ('INFO','WARN','WARNING','ERROR','CRITICAL')),
    category TEXT,
    message TEXT,
    details TEXT
);
INSERT INTO system_log SELECT * FROM system_log_old;
DROP TABLE system_log_old;
```

### Fix #3: etoro_full_pipeline.sh erstellen + Cron-Timeout hochsetzen
```bash
#!/bin/bash
set -a && source /home/mvolli/.hermes/.env && set +a
cd /home/mvolli/.hermes/workspace/etoro
exec python3 scripts/unified_pipeline.py --once 2>&1
```
Cron-Job Timeout: 120s → **900s**

### Fix #4: Phase-Timeouts auf realistische Werte
```python
# In unified_pipeline.py:
PHASE_TIMEOUTS = {
    "SL-Watchdog":     180,   # war 120
    "Candidate Ranking": 500, # war 60 (!!!)
    "Data Ingestion":  420,   # war 180 (!!!)
    "Active Trading":  180,   # war 120
    "Reconciliation":  120,   # bleibt
}
```

### Fix #5: Sektor-Exposure-Tracking Hotfix
```python
# In reconciler_service.py: nach Positions-Fetch, sector_exposure_tracking befüllen
# Temporär: aus live positions Sektor-Anteile berechnen und in Tabelle schreiben
```

---

## 📈 ERWARTETER IMPACT NACH V3

| Metrik | Aktuell | Ziel V3 |
|--------|---------|---------|
| Pipeline läuft autonom | NEIN | 100% |
| Trading Bible Violations | 3 aktiv | 0 |
| Phase Timeouts/Tag | 5-10 | 0 |
| Trade-Fehlerrate | ~70% FAILED | <10% |
| Manuelle Eingriffe/Woche | täglich | ~0 |
| SL-Watchdog Dauer | 120s+ (Timeout) | <30s |
| Discovery Feedback | kein | automatisch |

---

## ⚠️ OFFENE RISIKEN

1. **eToro API Rate Limits** — bei 18 Positionen + Eligibility-Checks: wie viele Calls/Minute?
2. **SQLite bei hoher Concurrency** — 5 Cron-Jobs parallel + Daemons → WAL-Mode reicht?
3. **Backtesting V3 Rules** — Neue Timeout/Regime-Regeln nie gegen historische Daten getestet
4. **DRAWDOWN-Recovery** — Wann ist manuelles Peak-Reset sinnvoll vs. automatisch?

---

*Audit erstellt mit Gemini 2.5 Pro + Live-System-Daten.*  
*Nächster Schritt: M1 Sofort-Fixes umsetzen, 48h beobachten, dann M2.*
