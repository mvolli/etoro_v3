# Vorregistrierung E4 — Trendfolge statt Schwäche kaufen (2026-10-16)

**Vor dem Test festgelegt** (VoLLi-Freigabe 16.10.: „Alle drei testen“).
Die Entscheidung folgt nur der Regel unten. Kein Nachjustieren von
Schwellen, Horizonten, Kosten oder Split nach Sicht des Holdouts.

## Warum

Alle bisher getesteten Kaufsignale kaufen Schwäche mit 5–10 Tagen Horizont
(MACD/BB_LOW, TREND_PULLBACK, RSI-Umkehr E1–E3, Kandidaten 25.09.) und
verlieren nach Kosten. Live 16.10.: MACD-Familie 273 Trades −$266, brutto
5d −0,19 %, out-of-sample +0,01 %. E4 testet die Gegenrichtung: Stärke
kaufen, länger halten (Kosten fallen relativ weniger ins Gewicht).

## Universum und Daten

- **Universum:** alle Aktien/ETFs (`instruments.asset_class IN ('stock','etf')`)
  mit mindestens einem Kaufsignal seit 2026-07-13 (das reale Arbeitsuniversum
  des Bots), mit auflösbarem `yfinance_symbol`.
- **Kurse:** yfinance, täglich, auto-adjusted, 2019-01-01 → 2026-10-15.
  Cache `data/e4_bars/`. Titel mit < 300 Kerzen fallen raus.
- **Kein Lückenfüllen.** Lookbacks laufen nur über zusammenhängende Daten.

## Familien (fix)

| | Signal am Tag t | Horizont | Abstand je Titel |
|---|---|---|---|
| **F1 Ausbruch** | Close ≥ 0,98 × Max(Close, 252 Tage) UND SMA50 > SMA200 UND Close > SMA50 | 20 Tage | ≥ 20 Tage |
| **F2 Relative Stärke** | an jedem 20. Handelstag (globaler Kalender): Top 10 % nach Close(t−21)/Close(t−126) − 1, mind. 50 bewertete Titel | 20 Tage | durch Rhythmus |
| **F3 Rücksetzer im Trend** | Close > SMA200 UND RSI(2) < 10 (Wilder) | 5 Tage | ≥ 5 Tage |

Einstieg zum Schluss von t, Ausstieg zum Schluss von t+H (kein Stop im Test).

## Zielgrößen

- **Excess:** Close(t+H)/Close(t) − 1 minus Median derselben Größe über alle
  Titel mit Abdeckung am Tag t, minus Kosten. (Schlägt es den Markt?)
- **Absolut:** Close(t+H)/Close(t) − 1 minus Kosten. (Verdient es Geld?)
- **Kosten:** 1,1 % Runde (Entscheidung). Sensitivität 0,5 % nur berichtet.

## Split

Globaler Kalender-Split: die ersten 60 % der Handelstage (Datumsachse des
Universums) = Training (nur beschreibend), die letzten 40 % = **Holdout**
(einzige Entscheidungsgrundlage). Signale, deren Horizont den Split kreuzt,
zählen zum Fenster ihres Signaltags.

## Entscheidungsregel je Familie (Holdout)

1. n ≥ 200 Signale.
2. Mittlerer Netto-Excess > 0.
3. **t ≥ 2,4** mit Standardfehler pro Signaltag geclustert (wie Edge-Gate
   seit 16.10.). 2,4 statt 2,0 = Bonferroni für drei Familien.
4. 5 %-getrimmter Netto-Excess > 0.
5. Leave-one-out über Titel: Vorzeichen des Excess-Mittels bleibt.
6. Mittlerer **absoluter** Netto-Return > 0.

**Angenommen = alle sechs.** Sonst verworfen (n < 200: unentscheidbar).

## Was danach passiert

- **Angenommen:** Familie wird als Signaltyp gebaut. Vor Live: eigener
  Horizont im Edge-Gate (20d für F1/F2) und Exits, die den Horizont tragen
  (sonst wird nicht gehandelt, was getestet wurde). Plan geht an VoLLi.
- **Verworfen:** nicht gebaut. Ergebnis wird dokumentiert.

## Grenzen (vorab notiert)

- **Survivorship:** Universum = heute existierende Titel. Bevorzugt
  Momentum/Ausbruch leicht. Excess gegen den gleichtägigen Median dämpft das
  nur teilweise.
- Überwiegend Aktien ohne Krypto; Aussage gilt nur für dieses Universum.
- Ein-/Ausstieg zum Schlusskurs, keine Spreads je Titel, kein Stop.
