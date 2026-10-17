# Plan: Aktienhandel „laufen lassen“ mit echtem Gebührenschema (2026-10-17)

**Status: Vorschlag, nicht live.** VoLLi 17.10.: „Ja, bewusst so einrichten“ —
Aktien-Trades trotz E5 (kein Vorteil gegenüber dem Index), mit Größe ~$880,
$1-Börsen und „laufen lassen“. Die Ausstiege sind seit `7930f9d` bereits live.

## Was E5 für dieses Setup erwarten lässt (Holdout 2023-06 → 2026-10, $880, X2)

| Familie (Live-Typ) | absolut netto je Trade | gegen ACWI | Trefferq. | Halt |
|---|---|---|---|---|
| A MACD_TURN + BB_LOW_MACD | +0,29 % | −0,94 Pp | 39 % | 16 T |
| C MACD_TURN allein | +0,17 % | −0,95 Pp | 38 % | 14 T |
| D TREND_PULLBACK allein | +0,10 % | −0,59 Pp | 38 % | 15 T |
| E TREND_PULLBACK + GOLDEN_CROSS | +1,87 % (Ausreißer) | +1,18 Pp (n. s.) | 40 % | 15 T |
| B RSI_EXTREME + MACD | −0,96 % | −1,85 Pp | 24 % | 7 T |

Je $880-Trade: absolut ca. +$1 bis +$3, gegenüber dem Index ca. −$5 bis −$9.
Bei 5 parallelen Positionen und ~15 Tagen Haltedauer: ~10 Trades/Monat,
absolut ca. +$10 bis +$30, gegenüber dem Index ca. −$50 bis −$90 je Monat.
Schwankung je Trade groß (Ø Gewinn +9 %, Ø Verlust −4,6 %).

## Bausteine

1. **Familien:** A, C, D, E zulassen; B (RSI_EXTREME) bleibt gesperrt.
2. **Universum:** nur $1-Börsen (kein .ASX/.AX/.HK/.T/.AE/.DH), kein Krypto —
   Filter im `signal_worker._filter_eligible` (wie beim Sweep).
3. **Größe:** feste Tranche je Signal-Trade = `max_trade_pct` des Regimes
   (NORMAL 10 % ≈ $880, CAUTION 7,5 %, DEFENSIVE 5 %), nie unter $200. Ersetzt
   die heutige Kette (Conviction × Aggressivität × Kelly × Parität ≈ $121).
   Pre-Trade-Veto darf weiterhin ablehnen; REDUCE unter $200 ⇒ Veto.
4. **Begrenzung:** höchstens N Signal-Positionen gleichzeitig (Vorschlag 5) und
   eine Cash-Reserve (Index-Kern-Regel: 18 %). Index-Kern hat Vorrang.
5. **Edge-Gate:** die Live-Statistik (Jul–Okt) ist für diese Familien negativ —
   der Gate würde weiter blocken. Deshalb wie CORE_SWEEP: zugelassene Familien
   **vom Gate ausgenommen**, dafür eine **Abbruchregel** auf Broker-Wahrheit:
   nach je 20 geschlossenen Signal-Trades, wenn Ø netto (netProfit − Provision)
   < −1,5 % je Trade ⇒ Signal-Käufe pausiert + Discord-Alarm, Freigabe nur VoLLi.
   Der Gate bleibt für alle anderen Familien unverändert.
6. **Ausstiege:** wie live seit `7930f9d` (SL clamp 1,5 × ATR 3,5–6 %, SELL-Signale,
   Zeitstopp 28 Kalendertage).
7. **Beobachtung:** wöchentlicher Bericht (Broker-Wahrheit): Trades, netto,
   gegen ACWI im selben Zeitraum.

## Umsetzung (nach Freigabe)

Config `trading.signal_trades` (enabled, families, fixed_by_regime_pct,
max_open, exclude_suffixes, exclude_asset_classes, abort_after_n, abort_avg_pct),
Filter + Sizing im `signal_worker`, Gate-Ausnahme über die Familienliste,
Abbruchregel im `signal_worker` (vor jedem Kauf geprüft), Tests, Probelauf auf
Live-Daten (welche Signale wären heute gekauft worden), dann live.

## Rückweg

`trading.signal_trades.enabled: false` — Signal-Käufe sofort wieder blockiert,
alles andere unverändert.
