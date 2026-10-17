# Analyse: Wie werden Aktien-Trades profitabel? (2026-10-17)

Frage VoLLi 17.10.: Aktien sollen weiter gehandelt werden, mit dem richtigen
Gebührenschema. Welche Signaltypen können profitabel werden, und was hindert
uns daran?

Datenbasis: Broker-Historie 10.09.–16.10. (315 Positionen, über orderId/positionId
den Signaltypen zugeordnet, 5 ohne Zuordnung), `signal_outcomes` (Signale
13.07.–16.10., Kurse `ohlcv_daily`, SPY als Vergleich), Ledger `trade_events`,
Backtest `workspace/etoro-v3-exit-variant-backtest.md` (21.08.).
Gebührenmodell: eToro Cost&Charges-PDF ($1/$2 je Order, auch beim Schließen).

## 1. Wohin das Geld seit 10.09. ging (Broker-Wahrheit)

| | Betrag |
|---|---|
| Kursergebnis aller 315 Positionen (brutto, `netProfit`) | −$238 (−0,73 % je Position) |
| Provisionen (Ø 2,5 Orders × $1/$2) | **−$965** |
| **Summe** | **−$1.203** (= Equity 10.000 → 8.797) |

Ø Positionsgröße **$104**, also ~3 % Provision je Position. Die Gebühren erklären
rund 80 % des Verlusts. Seit 16./17.10. ist das größtenteils behoben ($200-Floor,
Teilverkauf < $100 ⇒ ganz schließen): höchstens ~1 % je Position bei $200.

| Familie | n | Ø Einsatz | brutto | Provision | netto |
|---|---|---|---|---|---|
| MACD_TURN + BB_LOW | 146 | $72 | −0,94 % | −$482 | −$581 |
| CORE_SWEEP | 111 | $157 | −0,55 % | −$308 | −$404 |
| RSI_EXTREME + MACD | 27 | $88 | −1,97 % | −$92 | −$139 |
| TREND_PULLBACK + GOLDEN_CROSS | 16 | $82 | −0,27 % | −$58 | −$62 |

## 2. Hindernis 1 — die Einstiegs-Signale wählen Nachzügler

Brutto-Rendite in Handelsrichtung nach 10 Tagen, gegen SPY am selben Tag
(Tages-Cluster-SE, * = untere 95 %-Grenze > 0):

| Familie | Markt | n | 10d brutto | 10d gegen SPY |
|---|---|---|---|---|
| MACD_TURN + BB_LOW | $1 | 836 | −0,66 % | −1,46 % |
| MACD_TURN + BB_LOW | US | 149 | −1,36 % | −2,09 % |
| RSI_EXTREME + MACD | $1 | 97 | −2,34 % | −3,22 % |
| TREND_PULLBACK + GC | $1 | 133 | +0,19 % | −0,23 % |
| CORE_SWEEP | US | 95 | +2,32 %* | +0,83 % |
| CORE_SWEEP | EU/sonst $1 | 105 | +1,49 %* | +0,26 % |
| SPY selbst (CORE_SWEEP-Tage) | | 208 | +1,35 %* | – |
| SELL: TREND_KIPP_1H | $1 | 598 | | +0,76 %* (vermiedener Verlust) |
| SELL: BB_UPPER | $1 | 221 | | +0,50 % |

- Die Kauf-Familien liegen 0,2–3 Pp **unter** dem Markt, auch bei US-Großwerten.
  **Keine Positionsgröße macht ein negatives Brutto profitabel**: Break-even
  braucht ~1,1 % brutto bei $200 und ~0,33 % bei $880. Größere Positionen
  verlieren hier nur schneller.
- CORE_SWEEP ($1-Märkte, liquide Titel) verdient brutto, aber im Wesentlichen
  den Marktanstieg: +0,26 bis +0,83 % gegen SPY, nicht signifikant.
- Die SELL-Signale funktionieren als Ausstieg: verkaufte Titel laufen danach
  schlechter als der Markt.

## 3. Hindernis 2 — die Ausstiege schneiden Gewinner ab

Gleiche Einstiege, realisiert gegen stures Halten (brutto):

| Familie | n | realisiert | 5d halten | 10d halten |
|---|---|---|---|---|
| MACD_TURN + BB_LOW | 140 | −1,01 % | −0,21 % | +0,03 % |
| CORE_SWEEP | 108 | −0,51 % | +0,09 % | +0,58 % |

Die Ausstiegslogik kostet **~0,7–1 Pp je Position** (Spread im realisierten
Wert enthalten, 10-Tage-Halten ohne Stop gerechnet, deshalb Spanne).

Ausstiegsarten seit 10.09. (Ledger-PnL, grob):

| Art | n | Ø PnL |
|---|---|---|
| KI/LLM-Tighten/Exit | 173 | +1,16 % |
| SELL-Signal | 162 | +0,73 % |
| Stop-Loss (Broker + risk_sl) | 86 | −4,02 % |
| Stale-Exit | 21 | −0,44 % |
| Momentum-Fade / Vollausstieg / Leiter | 35 | +4,4 bis +5,4 % |

Gewinner werden bei +0,7–1,2 % mitgenommen, Verlierer laufen bis −4 %. Bei
~38 % Trefferquote geht das nicht auf. Der Backtest vom 21.08. zeigt dasselbe:
„SL-Grid, laufen lassen“ (V0) netto +$4.621, „Early-Lock“ (V2) +$209,
mit Chandelier (V3) −$662.

## 4. Hindernis 3 — Universum und tatsächliche Größe

- 650 von 867 MACD-Signalen lagen auf Titeln mit ADV < $50 M oder unbekannt:
  Spreads dort deutlich über den angenommenen 0,1 %.
- Signal-Trades werden im Median mit **$121** angesetzt (16./17.10.) und auf $200
  hochgerundet. Die 10 %-Grenze (~$880) ist eine Obergrenze, die das Sizing
  (Conviction × Equity × Aggressivität × Kelly) praktisch nie erreicht.
- Unter den letzten Signal-Trades sind viele Krypto-Titel (ETH-USD, DOGE, SOL …):
  2 % Runde.

## 5. Nicht das Hindernis — der Edge-Gate

Der Gate rechnet mit $200-Kosten und 5 Tagen. Das wird erst relevant, sobald eine
Familie brutto einen Vorteil hat. Heute blockt er Familien, die auch bei $880 und
10 Tagen verlieren würden.

## 6. Ehrliche Grenzen

- `signal_outcomes` deckt einen starken Aufwärtsmarkt ab (Jul–Okt), die
  Broker-Historie nur 5 Wochen, überwiegend DEFENSIVE mit 0,25×-Sizing.
- Viele Teilschnitte (Familie × Markt × Horizont × Region × Liquidität):
  einzelne Sterne können Zufall sein. **Keine Live-Regeln aus diesen Schnitten.**
- Vergleichsbasis ist der Index (ISAC.L, provisionsfrei), nicht Cash.

## 7. Offener Widerspruch und der Test, der ihn entscheidet

Backtest 21.08.: MACD_TURN mit „laufen lassen“ PF 1,45. Live: negativ. Beides
entscheidet nichts (andere Definition, Universum, 0,3 % Kosten, Zeitraum bzw. nur
3 Monate). **E5** (vorregistriert) auf `data/e4_bars` (786 Titel, 7 Jahre):
Live-Einstiegsdefinitionen × Ausstiege (V0 laufen lassen gegen heutiges
Early-Lock) × Provision je Order bei $200/$400/$880. Primär: Excess gegen Index;
sekundär: absolut netto. Spannung: Variante A (ganz schließen, 17.10.) wurde
gegen die heutigen Ausstiege validiert. Gewinnt in E5 „laufen lassen“, ist die
Kombination LLM-Tighten + ganz schließen neu zu bewerten.
