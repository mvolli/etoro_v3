# Vorregistrierung E5 — Live-Signale × Ausstiege × echte Gebühren (2026-10-17)

**Vor dem Test festgelegt** (VoLLi-Freigabe 17.10.: „Ja, E5 durchführen“).
Entscheidung nur nach der Regel unten, kein Nachjustieren nach Sicht des Holdouts.

## Frage

Können die Live-Kaufsignale profitable Aktien-Trades erzeugen, wenn (a) die
Ausstiege Gewinner laufen lassen und (b) die Position groß genug ist, dass die
Provision ($1/$2 je Order) nicht dominiert? Anlass: Live-Daten (Jul–Okt) zeigen
negative Brutto-Kanten, der Backtest vom 21.08. fand MACD_TURN mit „laufen
lassen“ profitabel (PF 1,45). E5 entscheidet den Widerspruch.

## Daten

- Universum wie E4: 786 Aktien/ETFs mit Kaufsignal seit 13.07. (Liste aus
  `data/e4_bars/`), dazu SPY als Vergleich.
- yfinance OHLCV täglich, auto-adjusted, 2019-01-01 → 2026-10-15, Cache
  `data/e5_bars/`. Titel mit < 300 Kerzen fallen raus. Keine Lückenfüllung;
  Lücken > 10 Kalendertage beginnen ein neues Segment (kein Indikator darüber).

## Signale (Live-Regeln aus `src/bot/core/signals.py`, Stand 17.10.)

Indikatoren mit `ta` wie live (RSI14, MACD 12/26/9, BB 20/2, ATR14, SMA20/50,
Knife-Gate 4 rote Tage / ROC5 ≤ −12 % / ≥ 2,5 ATR unter SMA20). Regeln 1–7
wie live; Familie = **exakte Menge** der am Tag t feuernden Kauf-Regeln
(wie der Live-Signaltyp). SELL-Regel BB %B > 0,95 und RSI > 70 verdrängt BUY
wie live. Getestet werden:

| Familie | Komponenten (exakt) |
|---|---|
| A | MACD_TURN_BELOW_SMA20 + BB_LOW_MACD_IMPROVING |
| B | RSI_EXTREME_OVERSOLD + MACD_TURN_BELOW_SMA20 |
| C | MACD_TURN_BELOW_SMA20 allein |
| D | TREND_PULLBACK allein |
| E | TREND_PULLBACK + GOLDEN_CROSS |

Nicht modelliert: Corporate-Action-Gate, Score-/Conviction-Filter, Diversity,
Quoten, Kelly-Sizing. Einstieg zum **Open von t+1**. Höchstens eine offene
Position je Titel und Familie.

## Ausstiege (fix)

Stop-Loss bei Einstieg: clamp(1,5 × ATR% am Tag t, 3,5 %, 6 %) wie live.
Zeitstopp 20 Handelstage (Close). Arming-Werte nur aus Kerzen bis i−1.

| Ausstieg | Regeln |
|---|---|
| X1 „heute“ | SL; Break-Even: Spitze ≥ +3 % ⇒ Boden +0,3 %; Momentum-Fade: Spitze ≥ +2 % und Close gibt ≥ 40 % des Spitzengewinns ab ⇒ ganz schließen (Close); SELL-Regel ⇒ Close; Zeit 20 |
| X2 „laufen lassen“ | SL; Zeit 20 |
| X3 „laufen + SELL“ | SL; SELL-Regel ⇒ Close; Zeit 20 |

LLM-Tighten und Stale-Exit sind nicht modellierbar und fehlen in X1.

## Kosten

Provision je Order $2 für .ASX/.AX/.HK/.T/.AE/.DH, sonst $1; zwei Orders je
Trade. Positionsgröße $200, $400, $880. Spread 0,2 % je Runde. Sensitivität
0,5 % Spread nur berichtet.

## Zielgrößen

- **Excess (primär):** Trade-Rendite minus SPY-Rendite über dieselben Tage
  (SPY Open am Einstiegstag bis SPY Close am Ausstiegstag), minus Kosten.
- **Absolut (sekundär):** Trade-Rendite minus Kosten.

## Split

Globale Datumsachse: erste 60 % Training (nur beschreibend), letzte 40 %
**Holdout** (Einstiegsdatum). Nur der Holdout entscheidet.

## Entscheidungsregel je Familie × Ausstieg × Größe (Holdout)

1. n ≥ 200 Trades.
2. Mittlerer Netto-Excess > 0.
3. **t ≥ 3,0** mit Standardfehler pro Einstiegstag geclustert (Bonferroni für
   5 × 3 × 3 = 45 Tests, einseitig α = 0,05).
4. 5 %-getrimmter Netto-Excess > 0.
5. Leave-one-out über Titel: Vorzeichen des Excess-Mittels bleibt.
6. Mittlerer absoluter Netto-Return > 0.

**Angenommen = alle sechs.** Sonst verworfen (n < 200: unentscheidbar).
Zusätzlich berichtet (nicht entscheidend): Trefferquote, Ø Gewinn/Verlust,
Haltedauer, Ergebnis nach Börsentyp ($1 / $2).

## Was danach passiert

- **Angenommen:** Plan an VoLLi — Familie, Ausstieg und Mindestgröße als
  Live-Regel; Edge-Gate bekommt den passenden Horizont und die Kosten der
  Zielgröße. Erst nach VoLLi-Entscheid live.
- **Nichts angenommen:** Aktien-Signalhandel bleibt blockiert; Kapital über
  Index-Kern. LLM-Tighten-Schwelle (+3 %) und „ganz schließen“ werden mit dem
  X1/X2-Vergleich neu bewertet.

## Grenzen (vorab)

Survivorship (heutige Titel); lokale Währung gegen SPY in USD (FX-Rauschen);
Ein-/Ausstieg ohne titelgenauen Spread; Live-Filter (Score, Quote, Diversity,
Edge-Gate) fehlen, die Live-Familien sind also eine Teilmenge.
