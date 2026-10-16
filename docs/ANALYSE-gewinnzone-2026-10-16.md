# Analyse: Warum der Bot verliert und was in die Gewinnzone führt (2026-10-16)

Stand 16.10.2026. Basis: Broker-Historie (`/trading/info/trade/history`,
473 geschlossene Positionen 11.09.–15.10.), Live-Portfolio, `trading.db`.
Epoche: Konto-Neustart am **10.09. 19:31 mit $10.000 und 0 Positionen**.

## 1. Kurzfazit

1. **85 % des Verlusts sind Gebühren, nicht schlechte Trades.** Von −$1.198 seit
   10.09. sind ~$1.015 Flat-Fees ($397 Käufe, ~$618 Schließungen). Kursverlust
   realisiert: nur −$249.
2. **Kein Signaltyp hat nach Kosten einen positiven Erwartungswert** — weder in
   Broker-Wahrheit noch in den Forward-Returns. Auch CORE_SWEEP nicht.
3. **Das Edge-Gate (live seit 09.10.) arbeitet richtig**: es blockt die negativen
   Familien. Dass Cash liegen bleibt, ist die Folge davon, kein Fehler.
4. **Die Gewinnzone ist mit aktivem Handeln allein nicht erreichbar.** Kontrollierbar
   sind die Kosten. Positiver Erwartungswert ohne bewiesenen Signal-Edge entsteht nur
   durch Marktexposure mit wenig Umschlag — das ist eine Risikoentscheidung (VoLLi).

## 2. Verlustzerlegung (Broker-Wahrheit)

| Posten | Betrag |
|---|---|
| Start 10.09. | $10.000 |
| Kursergebnis geschlossener Positionen (Broker `netProfit`) | −$249 |
| unrealisiert (offen) | +$34 |
| **unerklärtes Residuum** | **−$983** |
| Equity 16.10. | $8.802 |

`netProfit` enthält **keine** Gebühr, das Feld `fees` der Historie ist praktisch leer
(−$1,91 gesamt). Test zweier Gebührenmodelle gegen das Residuum:

| Modell | Orders | erklärt |
|---|---|---|
| Flat-Fee nur auf Käufe | 314 | $395 (40 %) |
| **Flat-Fee auf jede Order** (Kauf, Schließung, Teilschließung) | 787 | **$997 (101,5 %)** |

→ eToro bucht $1 (Standard) bzw. $2 (.ASX/.HK/.T/.AX/.AE) **auf jede Order**.
Median-Einsatz je geschlossener Position: **$55** → 4–7 % Kosten je Runde.
Ø 2,65 Orders je Trade; 184 Teilschließungen mit Ø-Tranche **$38**.

## 3. Edge je Familie (Broker `netProfit`, Gebühr Kauf + jede Schließung)

| Familie | n | brutto | Gebühr | netto |
|---|---|---|---|---|
| MACD_TURN + BB_LOW_MACD | 222 | −$105 | $500 | −$605 |
| CORE_SWEEP | 153 | **−$118** | $294 | **−$412** |
| RSI_EXTREME + MACD_TURN | 35 | −$50 | $86 | −$136 |
| TREND_PULLBACK + GOLDEN_CROSS | 32 | +$3 | $69 | −$66 |

- Die frühere Ledger-Zahl **CORE_SWEEP +$192** war Doppelbuchungs-Artefakt.
  Forward-Returns robust: Median −0,45 %, 5 %-getrimmt −0,04 % → kein Edge.
- Die SELL-Typen (`TREND_KIPP_1H,SELL`, `BB_UPPER_RSI_OVERBOUGHT`) wirkten mit
  +1,2/+1,7 % positiv — **Rechenfehler**: Kosten wurden bei SELL als Bonus gebucht
  (Fix `b114b9c`). Korrekt: Median −1,02 % / −0,54 %.
- Hypothesen E1/E2/E3 (RSI-Umkehr, vorregistriert) alle verworfen.

## 4. Ausstiege

- **Stops sind richtig gesetzt.** Nach 46 Broker-Stop-Outs fiel der Kurs weiter
  (Ø −0,85 % nach 10 Tagen, nur 39 % darüber). Halten hätte −$45 mehr gekostet.
- **Andere Ausstiege kommen nicht systematisch zu früh**: Kurs 10 Tage später
  Median 0,00 %, getrimmt +0,55 %.
- **Teilschließungen sind Gebührenverbrennung**: 101 `sell_exit`-Teilverkäufe
  brutto +$17, Gebühren $131. Fix: `partial_close.min_tranche_usd` (`cb9b582`, Default aus).
- Label `reconciler_9d` ist **kein** 9-Tage-Zeitexit, sondern „vom Reconciler beim
  Broker entdeckte Schließung“ (meist Broker-Stop).

## 5. Regime: Teufelskreis aus Gebühren

`CURRENT_REGIME` ist der **Konto-Drawdown gegen den 30-Tage-Höchststand**, kein
Marktsignal. Gebühren → Drawdown → DEFENSIVE (Scalar 0,5, min. MEDIUM, Sweep-Cap 3).
Ohne einen Dollar Gewinn, nur durch das rollende Fenster: Drawdown < 7 % um den
**23.10.** (→ CAUTION), < 3,5 % um den **30.10.** (→ NORMAL).

## 6. Freies Kapital (89 % Cash)

Cash in Signale ohne Edge zu drücken kauft nur Gebühren. Optionen mit Rechnung:

| Option | Erwartung | Risiko | Aufwand |
|---|---|---|---|
| Cash lassen | $0 | keins | keiner |
| Zinsen auf Cash (falls eToro für dieses Konto zahlt) | Zinssatz × ~$7.800 | keins | prüfen |
| Index-Kern im **Hauptkonto** | 7–10 %/J. auf Einsatz | Marktbeta | 1 Order, kein Code |
| Bot-verwalteter Index-Kern | wie oben | Marktbeta | Code: Ausnahme in **allen** Exit-Pfaden (Risk-Worker bewertet jede Broker-Position), Exposure/Diversity-Ausschluss |

Rechnung Index-Kern $6.000: +$420–600/Jahr bei 7–10 % Drift; Rückweg auf $10.000
(+13,6 %) dauert damit 2–3 Jahre. Ein Marktrückgang von 20 % kostet −$1.200.

## 7. Umgesetzt (16.10.)

| Commit | Inhalt | Wirkung |
|---|---|---|
| `cb9b582` | `partial_close.min_tranche_usd` | Default 0 = aus; bei $100: alle 184 Teilverkäufe seit Epoch unterdrückt, ~$214 gespart |
| `b114b9c` | SELL-Kostenvorzeichen in `signal_hygiene` | SELL-Typen korrekt negativ; Edge-Gate (nur BUY) unverändert |
| `f929fdb` | Rekonziliationsbericht: Kaufgebühren-Zeile + Schätzung Schließgebühren | Tagesbericht geht auf; Residuum erklärt |
| (Cron) | Risk Worker wieder aktiv (VoLLi, 16.10.) | erster Lauf: 1 Order, KO +4,9 % voll geschlossen |

## 8. Befunde für RoboCop (nicht umgesetzt)

1. **D4-Abbruchregel ist zahnlos**: DD-Regel ($150) über `trades.pnl_usd` (letzte
   Tranche, ohne Gebühren) erreicht bei $200-Positionen praktisch nie die Schwelle;
   sie zählt zudem Trades **vor** der D4-Freigabe. Seit D4: 4 Sweeps geschlossen,
   alle negativ; WR-Regel stand am 16.10. bei genau 30 %.
2. **Stop vom Referenzpreis statt vom Fill** (3/307 Trades, −$8): MIN.ASX Stop 0,13 %
   unter Fill statt 3,5 %. Vermutung: `lastExecution` an der Börsenöffnung noch
   Vortagesschluss. Log-Beleg fehlt (Ausführungs-Logs werden nicht gespeichert).
   Vorschlag: Reconciler prüft nach dem Fill den SL-Abstand und meldet Abweichungen.
3. **Datenfehler `ohlcv_daily` DEO**: Sprung 86,83 → 1.609,72 (Pence/Dollar).
4. **Ledger-Symbol**: Trade 2839 (KO) als `1024` (Instrument-ID) gebucht.
5. **5 zeitabhängig gealterte Tests** (`test_corporate_action_guard.py`,
   `test_ca_confirm_cache.py`): Code seit 17.08. unverändert, am 09.10. noch grün.
