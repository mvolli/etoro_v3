# eToro V3 — Sizing & Combo-Optimierung: Ist-Zustand (live trading.db, 2026-09-28)

Portfolio-Reset auf 10.000 USD am 2026-09-10. Alle Zahlen unten: trades created_at >= 2026-07-26 (26.07.-Zäsur,
Combo-Conviction = schwächste Komponente; KEINE VERY_HIGH mehr). "Pre/Post" = closed_at vs 2026-09-21 10:32
(deploy der sizing-Überarbeitung). real$ = realized via trade_pnl (net of broker fees, partials summed).

## 1. Was die letzten Code-Changes bewirkt haben (2026-09-12 … 2026-09-28)

### Deploy-Zeitleiste (UTC)
| Zeit | Change |
|---|---|
| 09-12 17:20 | entry_quality_gate.py: per-Componente-EQ-Gate (8 Gates, floor 0.5, soft_mode=observe, live seit 09-12) |
| 09-21 09:58 | SIZING_PARITY_FLOOR 0.35→0.40 (commit 98a6d91) |
| 09-21 10:32 | sizing.py max_trade_pct 0.12→0.06 (4c25b69, config 0.12→0.06) |
| 09-21 12:15 | signal_worker: signal-Path-Pyramiding (d0f7f93) |
| 09-22 22:33 | signal_floor 40→50 (1a987b3) |
| 09-25 18:04 | regime.py: 3 neue Regimes (BULL_VOLATILE 0.8 / BEAR_TRENDING 0.65 / RANGE_BEARISH 0.55) + dust_floor (85b3133) |
| 09-26 04:58 | sizing.py: signal_floor 50→75, dust_floor 50→75 (934814c) |
| 09-26 05:05 | fee-skip ASX/HK/Japan (c779e49) |
| 09-26 18:53 | entry_quality_gate: Gate 8 ATR% (atr_band_mult=1.30, atr_pct_max=4.0, atr_pct_dust 12.0) (2d7850f) |
| 09-27 14:39 | fee-skip: 37→62 tickers incl. .T/.KS/.TW (31a637d) |
| 09-27 22:22 | regime.py: BULL_TRENDING 0.45→0.40, BULL_CONSOLIDATION 0.22→0.18 (f3e1b17) |
| 09-28 04:52 | entry_quality_gate: Gate 8: atr_pct_max 4.0→3.0 (2a189f2) |

### Sizing-Kette (signal path, max_trade_pct=0.06 → cap 600 USD, min 300):
size = max(75, floor(0.5 × max(50, raw × kelly(0.368) × conviction_mult × quality_mult × regime_mult) × equity_mult(1.2)))
- kelly_size_factor = 0.368 für ALLE Combos (gleiche 2026-08-25-Snapshot-Basis — identisch, kein Kombodifferenz)
- conviction: MEDIUM = 1.0 (einziger Level, der nach 26.07 existiert), HIGH = 1.3, VERY_HIGH = 1.6
- entry quality: live seit 09-12. 8778 Events: mean size_mult 0.707, p50 = 0.5, min 0.5. Gate-8-Hit-Counter ist leer
  (hits-Array nie gefüllt — Messlücke!). Gate 8 (ATR% > 3.0 → 0.5) aktiv seit 09-28 04:52 (vorher 4.0).
- regime multipliers (seit 09-25/27): BULL_TRENDING 0.40, BULL_CONSOLIDATION 0.18, BULL_VOLATILE 0.8,
  BEAR_TRENDING 0.65, RANGE_BEARISH 0.55, BEAR_VOLATILE 0.35, NEUTRAL 0.7
- pyramiding: signal-path-Pyramiding seit 09-21 (18:46-Deploy-Phase)
- Core-Sweep path (unverändert, max 55% of max_trade_pct, cap 330, regime mult ×2, min 150):
  pre-change Ø 206 USD / post-change Ø 152 USD

### Effekte vor/nach 09-21 10:32 (n = closed trades, post-Zaesur):
| Fenster | n | WR% | avg% | real$ | avg size $ |
|---|---|---|---|---|---|
| PRE-change (closed < 09-21 10:32) | 399 | 34.6 | +0.02 | +134 | 137 |
| POST-change (closed >= 09-21 10:32) | 92 | 34.8 | **-1.33** | **-73** | **102** |
| ALL post-Zaesur | 491 | 34.6 | -0.23 | +61 | 130 |

→ Die Überarbeitung hat WR NICHT verbessert (34.6→34.8, Rauschen) und das avg% deutlich verschlechtert
(+0.02 → -1.33), während die Positionen 25% kleiner wurden (137→102 USD). Net-Effect der letzten Woche: -73 USD.

## 2. Size-Band-Analyse (signal path only, post-Zaesur)
| Band | Phase | n | WR% | avg% | real$ |
|---|---|---|---|---|---|
| 50-75 USD | pre | 116 | 35.3 | +0.61 | -92 |
| 50-75 USD | post | 30 | 43.3 | -0.98 | -16 |
| 75-100 USD | pre | 49 | 26.5 | -0.03 | +12 |
| 75-100 USD | post | 23 | 26.1 | -2.30 | -35 |
| 100-150 USD | pre | 45 | 20.0 | -1.32 | -61 |
| 100-150 USD | post | 7 | 28.6 | -2.59 | -16 |
| 150-250 USD | pre | 21 | 19.0 | -0.77 | -5 |
| 250+ USD | pre | 1 | 0.0 | 0.00 | 0 |

→ KLEINEREN Positionen = BESSERE WR (50-75: 35-43%) aber FEEN-DOMINIERT im real$ (491 closed × Ø130 USD ×
~0.0065 roundtrip fee = ~-140 USD fees; nur 231 cost_events mit 21 USD erfasst — fees größtenteils im
trade_pnl-basis, nicht als cost_event). GRÖSSTEREN Positionen = SCHLECHTERE WR (19-28%).
Der Fee-Fußabdruck frisst ~1.1% des avg position size pro roundtrip. Bei 491 closed Trades auf Ø130 USD
= ~-140 USD fees insgesamt, vs +61 real$ Gesamt → fees alone ≈ -2.3× der net profit.

## 3. Combo-Statistik (closed, post-Zaesur, real$ = realized net)
| Combo | Phase | n | WR% | avg% | real$ | Ø size |
|---|---|---|---|---|---|---|
| MACD_TURN_BELOW_SMA20,BB_LOW_MACD_IMPROVING | pre | 157 | 24.2 | +0.15 | -7 | 88 |
| MACD_TURN_BELOW_SMA20,BB_LOW_MACD_IMPROVING | post | 52 | 38.5 | -1.48 | -50 | 74 |
| CORE_SWEEP | pre | 167 | 42.5 | +0.09 | +281 | 206 |
| CORE_SWEEP | post | 32 | 34.4 | -0.69 | -7 | 152 |
| TREND_PULLBACK,GOLDEN_CROSS | pre | 68 | 36.8 | -0.45 | -149 | 80 |
| TREND_PULLBACK,GOLDEN_CROSS | post | 3 | 33.3 | -1.27 | -4 | 89 |
| RSI_EXTREME_OVERSOLD,MACD_TURN_BELOW_SMA20 | pre | 4 | 75.0 | +1.02 | +11 | 132 |
| RSI_EXTREME_OVERSOLD,MACD_TURN_BELOW_SMA20 | post | 4 | 0.0 | -3.91 | -12 | 87 |
| MACD_TURN_BELOW_SMA20,TREND_PULLBACK | pre | 3 | 33.3 | -0.95 | -2 | 99 |

→ CORE_SWEEP trägt ALLES: +281 USD pre / -7 USD post. Alle Signal-Combos: pre real$ -149+11-2 = -140;
  post real$ -50-4-12 = -66. Signal path ist systematisch net-negativ, Core-Sweep systematisch net-positive.

## 4. Conviction & Asset-Klasse
| Conviction | Phase | n | WR% | avg% | real$ |
|---|---|---|---|---|---|
| MEDIUM | pre | 399 | 35.3 | +0.14 | +286 |
| MEDIUM | post | 92 | 34.8 | -1.33 | -73 |
| HIGH | pre | 31 | 25.8 | -1.43 | -152 |

→ HIGH Conviction ist SCHLECHTER als MEDIUM (WR 25.8 vs 35.3, avg -1.43 vs +0.14, real$ -152 vs +286).
  HIGH multiplies sizing ×1.3 for the WORST trades. The conviction ladder is inverted.

| Asset-Klasse | n | WR% | avg% | real$ |
|---|---|---|---|---|
| crypto | 27 | 55.6 | +6.59 | +93 |
| etf | 12 | 41.7 | +0.29 | +9 |
| stock | 452 | 33.2 | -0.65 | -41 |

→ Crypto is the only clearly profitable class (n=27 small). Stocks = -41 real$ but Ø 130 USD sizes.
  High-fee tickers (ASX/HK/JP/KS/TW) now skipped (62 tickers, since 09-27 14:39) — 09-28: 100% of 2 opens
  were high-fee (pre-skip residual or new tickers).

## 5. Exit-Mix (top reasons, post-Zaesur, closed)
| Reason | n |
|---|---|
| SELL-Signal TREND_KIPP_1H (Überhitzung, 50% Gewinnmitnahme) | 135+ |
| Extern geschlossen — P/L folgt | 30 |
| Break-Even-Schutz (war ≥+3%, now ≤+0.3%) | 40+ |
| KI TIGHTEN (Momentum FADED, PnL > 0%, Δ < -1.5%) | 90+ |

→ ~30% of exits are Break-Even-Schutz (capping winners at ~+0.2…+0.3% instead of +3%+) — this directly
  caps the profit side. ~20% are KI TIGHTEN (taking small profits when momentum fades). These two exit
  mechanisms together convert ~50% of trades into small wins/flat, while the avg% is negative — the
  loss side is bigger than the win side.

## 6. Kern-Frage (an den Advisor)
VoLLi will:
1. Positionen DEUTLICH GRÖSSER (realistisch bis $500 for low-risk setups), SMALLER for high-risk.
2. WR deutlich optimieren, net-negative → net-positive.
3. Which signal combos to introduce / de-weight / remove?

Constraints from the Bible (MUST keep):
- Floor-Regel: positions < 100 USD = fee-trap (eToro fees ~0.0065 roundtrip on avg size → ~1.1% drag)
- MAX consolidation before BUY for fragments > MAX
- Circuit-Breaker: no Full-Close
- Cash-Check before BUY
- 30 trades/day × 1/30 max cash ≈ 333 USD target per position (VoLLi's own calc from 09-21)

Open questions for the Advisor:
a) Is the entry quality gate (floor 0.5, mean mult 0.707, 8778 events) the main cause of "positions
   too small"? Should floor be 1.0 (pass-through) for CORE_SWEEP?
b) Should HIGH conviction multiplier be inverted (×0.8 instead of ×1.3) given the data?
c) Should the Break-Even-Schutz threshold be raised from +3% to +5% or removed entirely?
d) Which new signal combos would fit the existing architecture and the "high-risk = small, low-risk = big"
   sizing logic? (e.g. RSI_EXTREME_OVERSOLD + TREND_PULLBACK + BB_LOW? VWAP-reversion? Gap-fill?)
e) Which existing combos to remove or de-weight based on the data above?
f) Is the kelly_size_factor=0.368 (identical for all combos) a problem? Should it be combo-specific?
