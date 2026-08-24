# Exit-Variant-Backtest — gueltige Laeufe

Alle hier abgelegten Laeufe wurden mit der **reparierten Engine** erzeugt, also nach
`8409709` (MDD/Sharpe) und `ac06bc4` (Position-Leak in der Bound-Capital-Kurve).
Aeltere Ergebnisdateien aus `bed9602` wurden am 2026-08-24 entfernt — sie meldeten
unmoegliche Werte (Bear-Lauf: MDD -318 % bei long-only) und hatten in einem Fall
sogar die Rangfolge der Varianten invertiert. Wer sie braucht: `git show bed9602`.


## Laeufe

| Datei | Studie | Zeitraum | Kosten | knife_atr | Symbole | erzeugt |
|---|---|---|---|---|---|---|
| `exit_variant_results_bear_c0.3.json` | bear_c0.3 | 2022-01-01 → 2024-01-01 | 0.3 % | 2.5 | 39 | 2026-08-24 |
| `exit_variant_results_main.json` | main | 2025-01-01 → today | 0.0 % | 2.5 | 40 | 2026-08-24 |
| `exit_variant_results_main_c0.3.json` | main_c0.3 | 2025-01-01 → today | 0.3 % | 2.5 | 40 | 2026-08-22 |

## Ergebnis je Lauf (Rendite auf gebundenem Kapital)


### bear_c0.3  (Kosten 0.3 %)

Benchmark: Equal-Weight Buy&Hold **5.67 %**, SPY **2.95 %**

| Variante | n | WR | PF | Netto USD | OnCap % | MDD % | Sharpe |
|---|---|---|---|---|---|---|---|
| V0 SL-Grid | 1336 | 25.0 % | 1.18 | 1550 | **6.69** | -56.70 | 0.04 |
| V1 SL-Grid×3 (ATR) | 1123 | 31.9 % | 1.05 | -1515 | **-6.18** | -58.47 | -0.04 |
| V2 Early-Lock | 1470 | 42.1 % | 1.21 | 895 | **4.58** | -60.05 | 0.03 |
| V3 Early-Lock+Chandelier | 1474 | 24.0 % | 1.18 | 213 | **1.10** | -62.11 | 0.01 |

### main  (Kosten 0.0 %)

Benchmark: Equal-Weight Buy&Hold **37.72 %**, SPY **32.2 %**

| Variante | n | WR | PF | Netto USD | OnCap % | MDD % | Sharpe |
|---|---|---|---|---|---|---|---|
| V0 SL-Grid | 857 | 31.5 % | 1.35 | 6373 | **30.80** | -16.87 | 0.29 |
| V1 SL-Grid×3 (ATR) | 731 | 39.7 % | 1.3 | 5847 | **27.16** | -17.17 | 0.26 |
| V2 Early-Lock | 933 | 44.1 % | 1.16 | 2445 | **14.00** | -16.33 | 0.14 |
| V3 Early-Lock+Chandelier | 935 | 27.1 % | 1.1 | 1609 | **9.32** | -16.88 | 0.09 |

### main_c0.3  (Kosten 0.3 %)

Benchmark: Equal-Weight Buy&Hold **37.72 %**, SPY **32.2 %**

| Variante | n | WR | PF | Netto USD | OnCap % | MDD % | Sharpe |
|---|---|---|---|---|---|---|---|
| V0 SL-Grid | 857 | 31.5 % | 1.35 | 3802 | **18.37** | -19.14 | 0.17 |
| V1 SL-Grid×3 (ATR) | 731 | 39.7 % | 1.3 | 3654 | **16.98** | -19.81 | 0.16 |
| V2 Early-Lock | 933 | 44.1 % | 1.16 | -354 | **-2.03** | -22.27 | -0.02 |
| V3 Early-Lock+Chandelier | 935 | 27.1 % | 1.1 | -1196 | **-6.93** | -24.96 | -0.07 |

## Befund (Stand 2026-08-24)

**V1 (SL-Grid x3 ATR) wurde geprueft und NICHT implementiert.** Es verliert in jedem
Lauf gegen die Baseline V0 und macht im Bear-Regime Verlust. Es existiert ohnehin kein
Produktionscode fuer die Varianten — sie leben ausschliesslich in diesem Verzeichnis.


Die Commit-Nachricht von `889fa9d` behauptet *"V1 best PnL +330 % @ 33 % WR"*. Diese Zahl
steht in keiner vorhandenen Ergebnisdatei; die dort genannte `exit_variant_results.json`
existiert nicht mehr. **Nicht als Beleg verwenden.**


Uebergeordnet: Ausser V0 im Bear-Regime schlaegt keine Variante Buy&Hold — im
Main-Zeitraum verlieren alle vier, sogar ohne Transaktionskosten. Solange das so ist,
ist "an den Exits nichts aendern" das Ergebnis; der Hebel liegt bei der Entry-Auswahl.


## Reproduzieren

```bash
PYTHONPATH=src python3 backtest/exit_variant_backtest.py \
  --study bear --knife-atr 2.5 --cost-pct 0.3 --out bear_c0.3
PYTHONPATH=src python3 backtest/exit_variant_backtest.py \
  --study main --knife-atr 2.5 --cost-pct 0.0 --out main
PYTHONPATH=src python3 backtest/exit_variant_backtest.py \
  --study main --knife-atr 2.5 --cost-pct 0.3 --out main_c0.3
```

OHLCV liegt in `data/backtest_cache/` (nicht versioniert) — der erste Lauf einer neuen
Periode laedt von yfinance nach, danach ist er offline reproduzierbar.

