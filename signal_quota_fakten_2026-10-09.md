# Faktenbasis: MACD_TURN+BB Signaltyp & Entry-Quota (2026-10-09)

Quelle: trading.db (Bot-Account RoBoCoP, $10k-Portfolio, DEFENSIVE, Equity $8,844)

## Der dominante Signal-Typ

`MACD_TURN_BELOW_SMA20,BB_LOW_MACD_IMPROVING` ist mit Abstand die größte
Signal-Familie. Alle Trades haben score >= 0.8, conviction MEDIUM/HIGH.

| Zeitraum | n  | WR    | PnL$    |
|----------|----|-------|---------|
| alle Zeiten | 271 | 31.0% | -267.78 |
| letzte 30d | 159 | 34.0% | -142.44 |

Wochen-Verlauf (70d) — deutliche Degradation nach W33:
  W31: n=11  WR=27.3% PnL=+7.34
  W32: n=3   WR=0.0%  PnL=-5.70
  W33: n=13  WR=84.6% PnL=+46.21   <- Peak
  W34: n=11  WR=54.5% PnL=+9.42
  W35: n=29  WR=10.3% PnL=-45.55   <- Abbruch
  W36: n=45  WR=13.3% PnL=-92.72   <- schlechteste Woche
  W37: n=19  WR=36.8% PnL=-23.02
  W38: n=51  WR=33.3% PnL=-58.52
  W39: n=45  WR=35.6% PnL=-35.52
  W40: n=30  WR=40.0% PnL=-3.55    <- leichte Erholung

Nach Conviction: HIGH n=12 WR=25.0% PnL=-63.42 | MEDIUM n=259 WR=31.3% PnL=-204.36
Nach Score: nur Band 0.8+ vorhanden (n=271, WR 31.0%)
Nach Size: <100 n=204 WR=33.3% | 100-200 n=55 WR=23.6% | 200+ n=12 WR=25.0%

## Signal-Volumen 14d (der Stau)

| Signal-Typ | total | fresh | expired | rejected | acted |
|-----------|-------|-------|---------|----------|-------|
| BB_UPPER_RSI_OVERBOUGHT (SELL) | 4002 | 12 | 3990 | 0 | 0 |
| MACD_TURN+BB (BUY) | 1001 | 11 | 476 | 466 | 48 |
| TREND_KIPP_1H,SELL | 818 | 0 | 763 | 0 | 55 |
| RSI_EXTREME+MACD_TURN | 189 | 0 | 49 | 126 | 14 |
| TREND_PULLBACK+GOLDEN | 182 | 0 | 38 | 125 | 19 |
| RSI+MACD+BB (3-Kombo) | 29 | 0 | 0 | 29 | 0 |
| MACD_TURN+TREND_PULLBACK | 13 | 0 | 0 | 13 | 0 |
| CORE_SWEEP | 5 | 0 | 0 | 0 | 5 |

GESAMT 14d: 6239 Signale, 5316 EXPIRED (85%), 759 REJECTED (12%), 141 CONSUMED (2%)

## Das aktuelle Gate (fix/entry-type-quote 2026-10-07)

config.yaml:
  diversity:
    max_category_fraction: 0.45
    category_overrides:
      MIXED: 1.0          # zurückgesetzt (Deadlock 07.10 aufgelöst)
    type_entry_quota:
      window_days: 7
      "MACD_TURN_BELOW_SMA20,BB_LOW_MACD_IMPROVING": 2

Effect: max 2 Neueinstiege dieser Familie pro 7 Tage. 466 REJECTED + 476
EXPIRED in 14d = die Quote drosselt den Typ, der 16% aller Signale liefert.
0/fehlend = Quote inaktiv (fail-open). Wert ist umkehrbar.

Kapital: $7,652 free von $8,847 Equity (86% liegt).

## Trade-Rejections 14d (neben Quote)
  12x LLM-Reduce unter Min-Buy: WR < 25%
   2x Spread-Gate > 1.5%
   1x LLM-Veto: WR 20%

## Kontext: 26.07-Zäsur
Nach der Zäsur (min_trades=25, Kelly-Restart) ist MACD_TURN+BB der größte
einzige Drag. Der Score-Multiplikator (0.25) und _signal_performance_decay
(0.3-Floor) sind am Boden und können die Familie nur untereinander sortieren,
nicht daran hindern, die UNGEKAPPT-Kategorie zu monopolisieren.

## Offene Fragen für den Advisor
1. Ist "2 Neueinstiege/7d" die richtige Dosis, oder ist der Mechanismus
   (Feste-Quote) falsch? Was wäre besser: dynamische Quote (WR-basiert),
   PnL-Gebühren-Modell, Kelly-gated Entry, oder Typ-Deaktivierung?
2. Wie generiert man mehr BESSERE Signale, wenn 85% verfallen und der eine
   Typ 16% des Volumens liefert aber nur 31% WR?
