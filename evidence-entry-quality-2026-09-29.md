# Entry-Quality Decomposition — Evidence (2026-09-29)

Datenbasis: trading.db, 509 CLOSED Trades seit 26.07-Zaesur (WR 34.8%, Summe -508 USD),
22.555 Signale mit Preis (18.146 davon HIGH), ohlcv_daily 4.003 Instrumente.
Shadow-Tests = Forward-Return (5d/10d) netto nach 0.66% Round-Trip, 60/40
chronologisch, Holdout = Sep. 2026 (einmalig angeschaut, wie von dir gefordert).

## 1. Breakeven-Korrektur (dein Punkt 0)
- Ø-Gewinn +3.31% (n=177), Ø-Verlust -2.23% (n=332) → Verhaeltnis 1.48:1
- Breakeven-WR (gross) = 40.2%, NICHT 51%. 34.8% ist ~5.4pp unter Breakeven, nicht 16pp.
- Ø-Notional: Median ~75 USD, Mittel ~98 USD (meist 50-100 USD, kein einziger Trade > 200 USD seit Zäsur).
- Kosten (0.66% RT): bei Ø 98 USD ≈ 0.65 USD/Trade ≈ 13.3% des Ø-Notional-Roundtrip.
  GROSS-Summe geschätzt ≈ -97 USD (nahe null), NETTO -508 → ~80% des Verlusts ist Kosten.
  ABER: DB erfasst cost_usd unzureichend (227 OPEN-Events, Summe 21.75 USD) →
  Kostenzahl ist Modell, keine Messung. Kosten-Erfassung ist Messlücke.

## 2. Regime-Zerlegung (deine H1, system_log-Reconstruction)
| Regime bei Entry | n | WR | Summe |
|---|---|---|---|
| CAUTION | 300 | 37.3% | -309 |
| NORMAL | 112 | 38.4% | -105 |
| DEFENSIVE | 97 | 22.7% | -94 |

Dip-Buy-Kombo (MACD_TURN_BELOW_SMA20,BB_LOW_MACD_IMPROVING, n=218, WR 27.5%, -175):
- CAUTION: n=109 WR 35.8% -39 | NORMAL: n=42 WR 33.3% -42 | DEFENSIVE: n=67 WR 10.4% -94
→ Defensiver Regime = Hauptverursacher des Dip-Buy-Verlusts (10.4% WR).

## 3. H4 Konviktions-Stufen (Shadow, Forward-Return)
Design: MEDIUM stock n=478 net10d +0.20% EV +0.30 (CI ±2.05) | HIGH n=3360 net10d -1.04% EV -1.56
Holdout: MEDIUM n=1202 net10d -2.20% | HIGH n=1149 net10d -2.30%
→ Monotonie NICHT bestätigt. HIGH = MEDIUM oder schlechter. Beide im Holdout negativ.
Keine Basis für 500-USD-Positionen. (18k HIGH-Signale, 0 ausgeführt: System läuft MEDIUM-only.)

## 4. H1 Trend-Gates (Shadow, dip-buys, stock)
Design: BASE n=474 EV +0.30 (CI ±2.06) | +regime N/C n=327 EV +1.43 (CI ±2.45) | +SMA200 n=5 (n zu klein)
Holdout: BASE n=978 EV -3.20 (CI ±1.56) | +regime N/C n=336 EV -0.95 (CI ±1.64) | +SMA200 n=147 EV -0.17
→ Regime-Gate reduziert Holdout-Verlust von -3.20 auf -0.95 (3.4x besser), aber NICHT >0.
SMA200-Schnitt im Holdout ≈ 0. Kein einziger Gate-Kandidat ist im Holdout eindeutig positiv.

## 5. H3 Kosten-Hürde / ADV (Shadow, stock, all signals)
Holdout: ALL n=2351 EV -3.37 | ADV>=20M n=842 EV -3.44 | ADV<2M n=558 EV -2.86 | ATR>=3% n=802 EV -4.76
→ Liquidity-Filter (Hoch-ADV) HILFT NICHT im Holdout. ATR>=3% (volatil) ist am schlechtesten.
Dip-buys x ADV (design): >=20M n=231 EV +0.64, <5M n=141 EV +1.02 — auch dort kein signifikanter Spread.

## 6. Per-Signal-Type im Holdout (n>=40)
| Type | n | net10d | EV |
|---|---|---|---|
| BB_UPPER_RSI_OVERBOUGHT | 1334 | -1.43% | -2.15 |
| MACD_TURN_BELOW_SMA20,BB_LOW_MACD_IMPROVING | 1003 | -1.89% | -2.84 |
| TREND_KIPP_1H,SELL | 660 | -2.80% | -4.21 |
| TREND_PULLBACK,GOLDEN_CROSS | 287 | -2.13% | -3.20 |
→ KEIN Signal-Typ hat positive Edge im Holdout. Market-Context (equal-weight, alle 4003 Bars,
10d-forward) im selben Fenster: median -0.87%, aber 36% positive → Signal-Layer unterperformt
selbst den Market-Context. Edge ist nicht stabil über Zeit (design +0.30 → holdout -3.30 bei MEDIUM).

## 7. Exit-Verhalten (dein H5-Vorabcheck)
- Ø-Hold 5.2 Tage (Median 3.4). Wins Ø 142h, Losses Ø 74h → Wins werden LÄNGER gehalten.
- 61% der Trades beenden sich ÜBER dem 10d-Hold-Wert (Ø realized -1.50 vs Ø 10d-net -2.39).
→ Exits sind NICHT das Problem. -3%-Stop frisst keinen dokumentierten Wert.

## 8. Weitere Diskriminatoren (509 Trades, AUC)
- ADV AUC=0.614 (höher=besser) | bb_pct AUC=0.585 | score AUC=0.428 (höher score = SCHLECHTER)
- Repeat-Entries (gleiches Symbol <21d nach Close): n=312 WR 32.4% -313 vs First n=197 WR 38.6% -195
  → Re-Entry-Loop (Bot kauft gleiche Symbole wieder) ist 61% der Trades und schlechter.
- Wochentag: Mon n=128 WR 25.8% -148, Fri WR 29.2% -180 (worst days); Tue 44.0% best.
- Asset: stock n=468 WR 33.3% -555 | crypto n=27 WR 55.6% +48 | etf n=14 WR 42.9% -2.

## Offene Fragen an dich
1. Ist die Kombination "Dip-Buy + DEFENSIVE-Regime = 10.4% WR" schon ein harter Kill-Grund
   für den Gate (MEDIUM dip-buys in DEFENSIVE komplett blocken)?
2. Wo ist der eigentliche Edge? Kein Signal-Typ ist holdout-positiv. Ist das System in der
   Phase, wo es NICHT handeln sollte (regime-gated Standstill), oder fehlt ein Signal-Familie?
3. Kosten: 80% des Verlusts = 0.66% RT bei 50-100 USD Notional. Ist die Sizing-Strategie
   (500 USD) das eigentliche Fix, weil Edge/Trade dann > Cost/Trade wird?
4. Re-Entry-Loop (61% der Trades): ist ein Cooldown pro Symbol ein sinnvoller, messbarer Fix?
5. Was ist der nächste messende Schritt, der die meisten Freiheitsgrade schließt?
