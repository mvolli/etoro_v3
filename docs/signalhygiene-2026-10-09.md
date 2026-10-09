# Phase 1c — Signalhygiene (2026-10-09)

**Zwei Hygieneprobleme der Signal-Statistik, beide gelöst:**

## 1. Instrument-Tag-Dedup

Die `signals`-Tabelle ist stark dupliziert: jeder 5-min-Zyklus des
signal_worker schreibt neue FRESH-Rows für dasselbe (Instrument, Tag, Typ).

| 90 Tage | Wert |
|---|---|
| Roh-Signale | 35.941 |
| Instrument-Tage | 5.596 |
| **Dedup (Instr, Tag, Typ)** | **6.184** |
| Duplikations-Ratio | **6,4x** pro Instrument-Tag |

Distribution Signale/Instrument-Tag: 1→1.687, 2→721, 3→529, 4→347,
5→346 … (langschwellig). Phase 1a dedupliziert bereits beim Laden pro
`(instrument_id, date, signal_type)` — dieses Skript macht die Semantik
explizit. `signal_outcomes` (1.820 Rows) ist damit die kanonische
Dedup-Basis. Die Roh-Tabelle `signals` bleibt wie gehabt (Write-Pfad
unterschiedlich) — Dedup ist eine **Lese**-Sache.

## 2. SELL-Typen aus BUY-Statistik trennen

**~42 % der Outcomes sind SELL-Richtung** (TREND_KIPP_1H,SELL +
BB_UPPER_RSI_OVERBOUGHT). Ihre Forward-Returns sind preisbasiert
(Preis rauf = positiv) — für ein SELL ist das ein **Verlust**. In der
kombinierten Statistik erschienen die besten Signale damit als
mittelmäßig.

**Lösung:** `direction`-Spalte (BUY/SELL) + `fwd_Nd_pnl`-Spalten, bei
SELL vorzeichen-gedreht (Profit = Preis runter). Die **Typ-Logik in
risk.py/sizing.py bleibt unverändert** (PLAN-Regel) — nur die Statistik
trennt.

### Ergebnis (fwd_10d, 5 %-trimmed)

| Richtung | n | pnl | pnl-WR | (preis) | (preis-WR) |
|---|---|---|---|---|---|
| BUY | 746 | −0,02 % | 34 % | −0,02 % | 34 % |
| **SELL** | 529 | **+0,01 %** | **65 %** | −0,01 % | 35 % |
| ALL | 1.275 | −0,00 % | 47 % | — | — |

**SELL ist die profitable Seite** (WR 65 % vs. 34 %). Der Vorzeichen-
Flip zeigt, was die kombinierte Statistik verdeckt: die beiden SELL-Typen
sind die einzigen konsistent positiven Signale des Universums.

### Typ-Aufschlüsselung (pnl-Basis)

| Richtung | Typ | n | pnl | WR |
|---|---|---|---|---|
| SELL | BB_UPPER_RSI_OVERBOUGHT | 90 | **+2,15 %** | **76 %** |
| SELL | TREND_KIPP_1H,SELL | 439 | **+0,83 %** | **62 %** |
| BUY | CORE_SWEEP | 176 | +0,62 % | 48 % |
| BUY | MACD_TURN_BELOW_SMA20,BB_LOW_MACD_IMPROVING | 399 | −1,41 % | 30 % |
| BUY | TREND_PULLBACK,GOLDEN_CROSS | 127 | −1,60 % | 32 % |
| BUY | RSI_EXTREME_OVERSOLD,MACD_TURN_BELOW_SMA20 | 21 | −1,83 % | 29 % |
| BUY | RSI_EXTREME…,MACD_TURN…,BB_LOW… | 14 | −3,36 % | 14 % |
| BUY | MACD_TURN_BELOW_SMA20,TREND_PULLBACK | 9 | +2,64 % | 56 % |

## Konsequenzen für die Folgephasen

- **Phase 3 (Edge-Gate):** Das Gate muss pro `(signal_type, direction,
  grade)` messen — SELL-Typen sind die natürlichen Kandidaten für
  LCB > 0. Die BUY-Typen (außer CORE_SWEEP) werden Shadow bleiben.
- **Phase 1b (Confluence-Grading):** Das Grading war BUY-lastig
  kalkuliert (SELL-Typen trugen ungedrehte Returns). Das Grade bleibt
  gültig für die BUY-Untermenge; für SELL ist ein separates Grading
  nötig (n=529, eigene walk-forward-Split). → In den Abschluss-Report.
- **D5 (Re-Enable):** Die profitable Seite ist SELL — Re-Enable-
  Priorität liegt bei BB_UPPER_RSI_OVERBOUGHT und TREND_KIPP_1H,SELL.

## Was nicht geändert wurde

- `signals`-Tabelle (Write-Pfad des signal_worker) — Dedup ist Lesesache.
- Typ-Logik in `risk.py`/`sizing.py`/`sell_exits.py` — PLAN: "Typ-Logik
  bleibt". Nur die Statistik trennt.
- `signal_forward_returns.py` (Phase 1a) — die `direction`/`pnl`-Spalten
  werden nachträglich von `signal_hygiene.py` ergänzt (idempotent).
