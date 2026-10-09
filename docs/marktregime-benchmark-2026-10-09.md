# Phase 1d — Marktregime-Feature (Benchmark ADX/ATR)

**Problem:** Die Konto-Regime-Spalte in `entry_quality_events`
(NORMAL/CAUTION/DEFENSIVE/CRITICAL) erklärt in den Outcomes **nichts** —
alle vier Regimes sind negativ. Es fehlt ein **Marktregeim-Feature auf
BENCHMARK-Ebene**: Wie war der *Markt* (SPY) gerade?

## Feature (neu in `entry_quality_events`)

| Spalte | Bedeutung | Quelle |
|---|---|---|
| `bench_adx` | ADX(14) des SPY — **Trendstärke** (0-100). >25 trendend, <20 range-bound | SPY-Daily-Bars |
| `bench_atr_pct` | ATR(14)/Close in % — **Marktvolatilität** | SPY-Daily-Bars |

**Berechnung:** `ta`-Lib (ADXIndicator, AverageTrueRange), Wilder-Glättung.
**Kein Look-ahead:** pro Event-Zeile wird die **letzte SPY-BAR streng
VOR dem Event-Datum** genommen (das Signal feuert intraday
asiatisch/europäisch, die US-Bar des Tages ist noch offen).
**Fail-open:** ohne `ta`/`pandas` bleiben die Spalten NULL.
**Idempotent:** `ALTER TABLE` via PRAGMA-Check, UPDATE stabil.

## Datenlage

- **SPY-Bars:** 1.086 Tage (2021-06-01 … 2026-09-28), gemerged aus
  `backtest_cache` + `exit_replay_bars` (dedup auf Datum).
- **Coverage:** 18.686/18.686 Events = **100 %**.
- **ADX-Validierung:** `ta`-ADX vs. manuelle Wilder-Referenz
  (letzten 8 Bars): Übereinstimmung 0,7–2,5 Punkte (normal, unterschiedliche
  Glättungs-Initialisierung). Volle Serie: max 65,95 (2025er Bullenmarkt)
  → Rechenweg korrekt.

## Befund: Das Event-Fenster war durchgehend range-bound

| ADX-Level | n Events |
|---|---|
| range (<20) | **18.686 (100 %)** |
| neutral (20-25) | 0 |
| trend (>25) | 0 |

`bench_adx` im Event-Fenster (22.08.–09.10.2026): **min 10,5 / max 19,1 /
mean 12,7** — also *immer* unter 20. `bench_atr_pct` mean 0,85 %
(niedrige US-Volatilität).

**Interpretation:** In diesem Fenster gab es **keine Varianz** im
Benchmark-Trendregime — SPY war die gesamte Zeit range-bound. Das Feature
ist korrekt gefüllt und wird in **späteren Fenstern** (wenn ADX >20/25
tritt) diskriminierend wirken. Für die *aktuelle* Outcomes-Analyse
(Phasen 1a-1c) ist `bench_adx` ein **Konstant** (kein Prädiktor) — das
erklärt, warum es hier keine Trennschärfe bringt. Das ist ein
legitimes Daten-Ergebnis, kein Bug.

## Konsequenzen

- **Phase 3 (Edge-Gate):** `bench_adx`/`bench_atr_pct` stehen als
  Feature-Spalten bereit. Im *aktuellen* Fenster sind sie konstant
  (range-bound) → das Gate wird sie nicht trennen. In Trend-Fenstern
  (ADX>25) können sie ein zusätzliches Gate-Feature werden.
- **Kein Look-ahead, keine Datenverfälschung:** Feature ist pro Datum
  korrekt (vorherige US-Bar). Die 18.686 Events sind 100 % gefüllt.
- **Konstant im Fenster:** Für die 1a-1c-Outcomes ist `bench_adx`
  unscharf (keine Varianz). Das wird im Abschluss-Report als
  **Daten-Einschränkung** notiert — nicht als Fehlschlag.

## Was nicht geändert wurde

- Konto-Regime-Logik (`regime`-Spalte bleibt) — sie erklärt zwar
  nichts, ist aber der bestehende Write-Pfad (entry_quality.py).
- `entry_quality.py` (Write-Seite) — das Feature wird **nachträglich**
  von `market_regime_features.py` ergänzt (idempotent), Write-Pfad
  bleibt unverändert.
