# Verlustprofil TREND_PULLBACK (D3)

**Stand:** 2026-10-09, Phase 1a
**Datenbasis:** `signal_outcomes` (1.820 Zeilen, 90 Tage, 284 Bar-Dateien)
**Kostenannahme:** 1.1 % Round-Trip (identisch zu `e_hypothesis_test.py`)

## Gesamtbild

| Metrik | Wert |
|---|---|
| n (alle TREND_PULLBACK-Varianten) | 144 |
| fwd_5d (netto) | −1.16 % |
| fwd_10d (netto) | −1.32 % |
| WR 10d | 34 % |

**Fazit:** TREND_PULLBACK ist in den letzten 90 Tagen konsistent negativ.
Die −$215-Verluste aus dem Handelsbuch (Pre-Phase-0b, mit Duplikaten)
werden von den unverzerrten Forward-Returns bestätigt.

## Aufschlüsselung

### Nach Conviction
| Conviction | n | fwd_10d | WR 10d |
|---|---|---|---|
| HIGH | 85 | −0.68 % | 38 % |
| MEDIUM | 59 | −2.39 % | 27 % |

→ HIGH-Conviction-Signale performen besser, sind aber immer noch negativ.
MEDIUM ist deutlich schlechter.

### Nach MA200-Position
| Position | n | fwd_10d | WR 10d |
|---|---|---|---|
| Darüber | 79 | −0.88 % | 35 % |
| Darunter | 58 | −1.85 % | 34 % |

→ Pullback unter MA200 ist fast doppelt so schmerzhaft.

### Nach Benchmark-Trend (SPY 20d)
| Trend | n | fwd_10d | WR 10d |
|---|---|---|---|
| Positiv | 102 | −0.55 % | 38 % |
| Negativ | 40 | −2.92 % | 25 % |

→ **Stärkster Prädiktor:** In negativen Benchmark-Umgebungen verliert
TREND_PULLBACK ~5× mehr. D3-Empfehlung: bei SPY-20d < 0 →
TREND_PULLBACK+GOLDEN_CROSS in Shadow legen (n=40, t zu klein für
Signifikanz, aber Effektstärke klar).

### Nach RSI-Tiefe
| RSI | n | fwd_10d | WR 10d |
|---|---|---|---|
| 40–54 | 114 | −1.54 % | 33 % |
| 55–69 | 30 | −0.46 % | 37 % |

→ Keine klare Trennschärfe; RSI allein erklärt den Verlust nicht.

### Nach Ko-Signalen (stärkster Prädiktor)
| Ko-Signale | n | fwd_10d | WR 10d |
|---|---|---|---|
| 0 | 99 | −1.74 % | 24 % |
| 1–2 | 45 | −0.45 % | 55 % |

→ **Ko-Signale drehen das Bild:** Mit 1–2 Ko-Signalen steigt WR von
24 % auf 55 % und der Verlust von −1.74 % auf −0.45 %. Ohne Ko-Signale
ist TREND_PULLBACK im Rauschen (WR 24 % < 30 % Zufalls-Niveau).

### Nach exaktem Signal-Typ
| Typ | n | fwd_10d | WR 10d |
|---|---|---|---|
| TREND_PULLBACK,GOLDEN_CROSS | 135 | −1.60 % | 32 % |
| MACD_TURN_BELOW_SMA20,TREND_PULLBACK | 9 | +2.64 % | 56 % |

→ GOLDEN_CROSS-Variante dominiert den Verlust. MACD-Variante klein
(n=9) und positiv — kein Handlungsbedarf.

## Empfehlung für das Edge-Gate (Phase 3)

1. **TREND_PULLBACK+GOLDEN_CROSS** → Shadow-Modus bis
   n ≥ 200 mit LCB > 0 (aktuell n=135, LCB < 0)
2. **Ko-Signal-Feature** in das Grading (Phase 1b) aufnehmen:
   0 Ko-Signale = Grade C, 1–2 = Grade B
3. **Benchmark-Trend-Feature** als Regime-Modifier:
   SPY 20d < 0 → Score-Abschlag (Phase 1d)
4. **Keine manuelle Sperre** (D3 bestätigt): das Gate regelt es
   datenbasiert
