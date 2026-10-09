Die Datenanalyse ist abgeschlossen. Kurz vorweg: Die beiden Fragen hängen an einer Voraussetzung, die in der Faktenbasis fehlt. Der Typ ist nach Kosten nicht profitabel, und kein Teil der Sizing-Kette kann einen Einstieg komplett verhindern. Die Quote ist deshalb eine Notbremse und kein Mechanismus, der das Problem löst.

## 0. Korrekturen an der Faktenbasis (direkt aus `trading.db` geprüft)

| Aussage in der Datei | Befund |
|---|---|
| „Die Quote drosselt: 466 REJECTED in 14d“ | **Falsch zugeordnet.** Etwa 400 der 465 REJECTED stammen vom 28.09.–07.10., also bevor es die Quote gab. Seit dem 07.10. **filtert** die Quote die Signale im eligible-Filter heraus (Log: `eligible-Filter: 11 -> 0 \| entry_quote=10 …`), und sie laufen danach als EXPIRED ab. |
| „85 % aller Signale verfallen“ | **Stark aufgebläht.** 3.999 der 6.239 Signale sind `BB_UPPER_RSI_OVERBOUGHT`-SELL-Signale, und auf keines davon wurde je gehandelt. Dazu kommt Mehrfachauslösung: die 1.000 MACD_TURN+BB-Signale verteilen sich auf nur **448 Instrument-Tage**, die SELL-Signale auf 453. Echte, unterscheidbare BUY-Gelegenheiten gab es in 14 Tagen rund 600, nicht 2.200. |
| „86 % Kapital liegt brach“ | Das kostet nichts. Es gilt die Asymmetrie aus AGENTS.md: Nicht-Kaufen kostet nichts. Solange die Expectancy negativ ist, ist das kein Argument für Lockerung. |
| „Edge Decay W33 → W36“ | Das Peak-Ergebnis beruht auf zu wenig Daten. W33 hat 11 von 13 Treffern, das 95-%-Intervall reicht von etwa 58 bis 96 %. Gesamt liegen 84/271 bei 31 % (Intervall 26–37 %), W36 bei 6/45 (6–26 %). Belegt ist nur: Der Typ hatte nie einen stabilen Edge. W33/34 war vermutlich ein Marktphasen-Effekt. |

## 1. Die eigentliche Diagnose

**Expectancy des Typs:**
- Gewinner bringen im Schnitt **+4,44 %**, Verlierer **−2,65 %**. Das R:R liegt also bei 1,67.
- Der Break-even-WR vor Kosten ist damit **37 %**. Ist-Wert: 31 %.
- Kelly (Gewinnquote minus Verlustquote geteilt durch R:R): 0,31 − 0,69/1,67 ≈ **−0,10**. Kelly sagt bereits jetzt „Größe null“.

**Warum der Typ trotzdem handelt:**
- Die Formel ist `factor = clamp(0.3681 + 0.45·kelly, 0.15, 0.818)`. Bei Kelly = −0,10 ergibt das einen Faktor von etwa **0,32**. Der Typ wird also nur kleiner, nie null.
- Danach greifen Score-Multiplikator, Decay-Floor 0,3, `min_buy` und **seit heute `signal_floor_usd = 200` in allen Regimes** (7cd4cda). Der Floor hebt die Größe wieder an.
- **Kelly ist in diesem System nur ein Multiplikator und nie ein Gate.** Genau diese Lücke flickt die Quote.

**Akutes Risiko durch den $200-Floor von heute:**
- Er gilt auch für diesen Typ. Bisher lag die Ø-Größe bei etwa $95, die beiden erlaubten Einstiege pro Woche laufen jetzt mit etwa $200.
- Überschlag: Nach $2 Fee-Roundtrip auf $200 (1 %) liegt der Break-even-WR bei rund **50 %** (+3,4 % gegen −3,65 %). Der Typ liefert 31 %.
- Absolut bleibt der Schaden durch die Quote klein (etwa −$3 pro Trade, 2 pro Woche). Es ist trotzdem ein bekannter Verlustpfad.

**Kosten statt Signal:**
- Pro Trade ist das Ergebnis nach Prozent fast neutral. Im RSI-Band 30–40 (n=194) liegt Ø `pnl_pct` bei **−0,06 %**, die Summe trotzdem bei **−$163**.
- Der Verlust in USD kommt also überwiegend aus Kosten und Struktur: kleine Größen, Flat-Fees und die ATR-Leiter.
- Gewinner haben im Schnitt etwa **2 PARTIAL_CLOSE** (149 Partials auf 73 Trades). Falls eToro pro Order eine Gebühr bucht, zahlt ein Gewinner rund 4 Gebühren und ein Verlierer 2.
- Das ist noch nicht verifiziert: `fee_usd` ist in `trade_events` für diese Trades leer.

**Weitere Befunde:**
- **Das Regime-Label erklärt nichts.** NORMAL, CAUTION und DEFENSIVE sind alle negativ, das ist Konto-Risiko und kein Marktregime. Ein ADX/ATR-Marktregime wird nicht gespeichert.
- `bb_pct` ist durch die Signaldefinition fast konstant (269/271 in 0–0,1) und trennt deshalb nicht.
- **Tiefes RSI ist schlechter.** RSI < 30 kommt auf WR 19 % und −2,0 %, das typische „fallende Messer“.
- **Die Konfluenz-Variante wird blockiert.** `RSI_EXTREME_OVERSOLD,MACD_TURN_BELOW_SMA20` ist der einzige profitable Kauf-Typ: +$65, Ø-Gewinn $11,74, n=36. Die 3-Kombo mit BB wurde in 14 Tagen **29 von 29 Mal ohne Trade-Datensatz verworfen**. Der Grund wird nicht geloggt, das muss geklärt werden.

## 2. Antwort auf Frage 1: Welcher Mechanismus?

Die feste Quote ist als Brandschutz richtig, als Dauerlösung aber falsch. Sie misst die Zeit und nicht den Edge. Zu den Alternativen:

| Option | Urteil |
|---|---|
| Dynamische Quote nach WR | ✗ Der WR allein ignoriert das R:R. Der profitable RSI_EXTREME-Typ hat ebenfalls nur 30,6 % WR. |
| Deaktivierung bei WR < 25 % | ✗ Gleicher Fehler. Das LLM-Reduce-Gate macht das bereits (12× in 14d) und wird von den Floors teils ausgehebelt. |
| PnL-Fee-Modell | ✓ Als **Eingangsgröße**: Die Expectancy muss netto nach Fees gerechnet werden, sonst ist sie falsch. |
| **Kelly-gated Entry auf Netto-Expectancy** | ✓✓ **Empfehlung.** Pro Signaltyp eine geschrumpfte Netto-Expectancy (Shrinkage ist mit `kelly_shrink_k0` schon vorhanden). Ist die untere Konfidenzgrenze < 0, gibt es **keinen Live-Einstieg** und das Signal geht in den Shadow-Modus. Der Typ wird wieder freigegeben, wenn die Shadow-Expectancy über N unabhängige Signale positiv ist. |
| Konfluenz-Upgrade | ✓ Gehört zu Frage 2. Das ist der Hebel für bessere Signale, kein Ersatz für das Gate. |

Wichtig: Die Floors (`signal_floor_usd`, Decay-Floor, `min_buy`) dürfen ein negatives Gate **nie** nach oben runden. Dafür muss es eine Regel „Gate vor Floor“ geben, die in `test_signal_worker_e2e.py` abgesichert ist.

## 3. Antwort auf Frage 2: Bessere Signale

1. **Alle Signale bewerten, nicht nur Trades.** Für jedes Signal wird die Rendite nach 5 Tagen berechnet (passend zur Ø-Haltedauer von 5,4 Tagen), aus `ohlcv_daily`, pro Instrument-Tag dedupliziert, und zwar **auch für EXPIRED und REJECTED**. Das liefert etwa 450 unabhängige, nicht vom Gate verzerrte Stichproben allein für diesen Typ, statt 271 Trades.
2. **Konfluenz offline entwickeln, nicht übernehmen.** Die AlgoMatrix-Zahl (WR 48 → 62 %) ist Marketing und nicht auf diese Daten übertragbar. Kandidaten-Features, die Daten dafür liegen bereits vor:
   - Kurs über oder unter `ma200_daily`: Mean-Reversion funktioniert klassisch nur über SMA200.
   - RSI-Tiefe (unter 30 meiden).
   - Index- bzw. Markt-Trend (SPY/Benchmark über SMA50).
   - Volumen relativ zum Durchschnitt.
   - Abstand zur SMA20 in ATR.
   - Ko-Signal RSI_EXTREME.
3. **Das Regime-Research nicht wörtlich übernehmen.** „Entry nur in Trend-Regimes“ gilt für Trendfolger. MACD_TURN+BB ist ein **Dip-Kauf**. Ob er in Range- oder Trend-Phasen funktioniert, muss die Forward-Return-Tabelle zeigen. Dafür braucht es ein echtes Marktregime-Feature (ADX/ATR), nicht das Konto-Regime.
4. **Edge pro Quelle wie bei Freqtrade.** Das passt zum Gate aus Frage 1: Expectancy pro `signal_type` × Konfluenz-Grad (A/B/C), Sizing danach.
5. **Signalrauschen abstellen.** Signale pro Instrument-Tag deduplizieren, und SELL-Signale ohne Kaufpfad nicht in die BUY-Statistik zählen. `BB_UPPER_RSI_OVERBOUGHT` wird in `risk.py` und `sizing.py` gelesen und darf deshalb **nicht** einfach abgeschaltet werden.

## 4. Priorisierter Plan

**P0 – Diagnose (nur lesend, kein Live-Risiko, ca. 1 Tag)**
1. `scripts/signal_forward_returns.py`:
   - Signale der letzten 90 Tage, pro (instrument_id, date) dedupliziert.
   - Forward-Returns für 1, 3 und 5 Tage aus `ohlcv_daily`.
   - Features: rsi, Abstand zu SMA200 (`ma200_daily`), SMA20/ATR, Volumen relativ, Benchmark-Trend.
   - Ausgabe: Tabelle `signal_outcomes`, idempotent per `CREATE IF NOT EXISTS`.
2. Fee-Attribution klären: Bucht eToro die Gebühr pro Order, also auch pro Partial Close? Dazu `totalExternalFees` einer Position mit Partials über die API prüfen. Falls ja, kostet die ATR-Leiter bei Positionen unter $300 mehr, als sie absichert.
3. Den Grund für die 29/29 Verwürfe der 3-Kombo finden und ein Log ergänzen.

**P1 – Gate vor Floor (größter Sicherheitsgewinn)**
1. Neu: `src/bot/core/edge_gate.py` mit `type_edge(signal_type) -> (exp_net, lcb, n)`. Die Expectancy wird netto nach Fees aus `trades` und `trade_events` gerechnet, mit vorhandener Shrinkage.
2. In `_filter_eligible` ein Gate `edge_negativ` vor die Sizing-Kette setzen. Fail-open nur, wenn n unter dem Minimum liegt, in Anlehnung an `kelly_min_trades = 25`.
3. Shadow-Logging: Geblockte Signale bekommen ein Flag in `signal_outcomes`, damit eine Erholung messbar wird.
4. E2E-Szenarien:
   - Ein negativer Typ wird geblockt, auch bei `signal_floor_usd = 200`.
   - Ein positiver Typ läuft unverändert durch.
   - Bei n unter dem Minimum greift fail-open.
5. Die Quote bleibt aktiv, bis das Gate eine Woche live verifiziert ist (Heartbeats, Watchdog). Danach kann sie entfallen.
6. Das Gate gehört in `BIBLE_HARD_LIMITS`, damit der LLM Review Worker es nicht lockern kann.

**P2 – Konfluenz-Grading (nach P0-Daten)**
1. Auf `signal_outcomes` die Feature-Buckets auswerten und einen A/B/C-Grade definieren. Walk-forward: Training auf W27–W36, Test auf W37–W40.
2. Der Grade kommt als Spalte in `signals`. Das Gate aus P1 rechnet die Expectancy dann pro (Typ, Grade). So kann ein A-Grade-MACD_TURN aktiv bleiben, während C-Grade geblockt wird. Daraus entstehen bessere Signale statt nur weniger.
3. Das LLM bewertet weiterhin, aber erst nach dem Grade, als Veto und nicht als Edge-Quelle.

**P3 – Signalhygiene**
1. Signale pro Instrument und Tag in `data_worker`/`signals.py` deduplizieren. Danach sind die Verfalls-Statistiken ehrlich.
2. Ein Marktregime aus ADX/ATR auf Benchmark-Ebene berechnen und in `entry_quality_events` speichern, als Feature für P2.

**P4 – Exit- und Kosten-Struktur**
1. Falls P0.2 bestätigt, dass pro Partial eine Gebühr anfällt: Die ATR-Leiter erst ab einer Positionsgröße X zulassen. Das ist ein geldwirksamer Schwellenwert, also deine Entscheidung.

## 5. Entscheidungen, die bei dir liegen (geldwirksam, in `config.yaml` habe ich nichts angefasst)

- **Sofort:** Bleibt die Quote bei 2/7d mit jetzt $200 pro Einstieg, oder setzen wir sie bis P1 auf **0**? Das sperrt den Typ voll; laut Kommentar im Code („0/fehlend = Quote inaktiv“) bedeutet 0 aber Quote aus, das prüfe ich vor dem Umstellen. Ich empfehle, den Typ zu sperren, weil der Break-even-WR bei $200 etwa 50 % beträgt und der Typ bei 31 % liegt.
- Für das Gate in P1: Mindest-n, Konfidenzniveau der unteren Grenze und Re-Enable-Kriterium.
- Die Größenschwelle für die ATR-Leiter in P4.

Soll ich mit P0 anfangen? Das Forward-Return-Skript ist rein lesend und hat kein Live-Risiko.
