# Vorregistrierung — Profit-Leiter im Drawdown-Regime (Variante iii)

**Angelegt:** 2026-10-06, VOR jeder Datenerhebung zu dieser Frage.
**Status:** Hypothese registriert. **Nicht umgesetzt.** Kein Code, keine
Schwelle, kein Handelsverhalten geändert.
**Umgesetzt wurde nur** `fix/ladder-regime-decision-stage` (Variante ii) —
handelsneutral, siehe dort.

---

## 1. Hypothese

> Die Unterdrückung der strukturierten Profit-Leiter in den Regimes
> DEFENSIVE und CRITICAL (`trailing_stop.SUPPRESS_LADDER_REGIMES`, Absicht
> „let winners run") kostet im Drawdown Geld, statt welches zu sparen.

**Gerichtet.** Erwartet wird ein *negatives* Vorzeichen der unten definierten
Kennzahl: der Teilverkauf hätte die Restposition vor weiterem Verfall
geschützt.

**Gegenhypothese (H0):** Der unterdrückte Trim macht keinen Unterschied oder
kostet (Kennzahl ≥ −0.30pp).

## 2. Woher die Hypothese kommt — und was daran schwach ist

Pilotmessung vom 2026-10-06 über die echten Ladder-Teilverkäufe
(`trade_events.source = 'trailing_partial'`), Kennzahl wie unter 3:

| Zeitraum | n | Mittel | t | Anteil „Trim war verfrüht" |
|---|---|---|---|---|
| gesamt | 27 | +0.10pp | +0.29 | 33 % |
| bis 31.08. | 19 | +0.47pp | +1.06 | 47 % |
| **ab 01.09.** | **8** | **−0.79pp** | **−3.83** | **0 %** |

Robustheit des September-Befunds: alle 8 Legs negativ, Spannweite −1.47 bis
−0.02pp; symmetrisch getrimmt −1/+1 → −0.81pp (t=−3.81), −2/+2 → −0.84pp
(t=−4.46); ungünstigstes Leave-one-out −0.70pp (t=−3.29).

**Die drei Schwächen, ausdrücklich festgehalten:**

1. **n = 8.** Klein.
2. **Der September-Split wurde NACH Sicht der Daten gewählt.** Das ist der
   Grund für diese Vorregistrierung — der Befund ist eine *Vermutung*, kein
   Ergebnis. Genau deshalb wird unten alles vorher festgelegt.
3. **Die Pilot-Streuung ist zu schmeichelhaft.** sd = 0.587 über die 8 Legs,
   aber sd = 1.74 über die volle Ladder-Historie (n=27). Die Stichprobenzahl
   unter 5 ist mit **sd = 1.74** gerechnet, nicht mit der Pilot-Streuung.

Zwei weitere Legs (SPFI, 07.09.) sind nicht auswertbar — Zwangsschluss
`reconciler_9d` am 10.09. 21:31:32, dem Epoch-Reset, ohne erfasstes PnL.
Datenlücke, keine verschwiegenen Gewinner.

## 3. Kennzahl — jetzt festgelegt

Pro unterdrückter Ladder-Stufe, deren Position inzwischen **geschlossen** ist:

```
gewichtete_drift_pp = (final_pnl_pct − entscheidungs_pnl_pct) × close_pct / 100
```

* `entscheidungs_pnl_pct` — PnL im Moment der Unterdrückung
* `final_pnl_pct` — `pnl_pct` des ersten `trade_events.CLOSE` dieser Position
  danach
* `close_pct` — die Quote, die die Stufe genommen hätte (vor KI-Kalibrierung,
  also der mechanische Wert aus `profit_levels_json`)

**Gewichtet, nicht roh.** Nur der verkaufte Anteil verpasst bzw. entgeht der
Weiterentwicklung. Die Rohdrift wird nachrichtlich mitberichtet, ist aber
nicht die Entscheidungsgrundlage. (Dieser Unterschied war im RoboCop-Report
vom 06.10. die Ursache für einen um Faktor ~5 zu großen Headline-Wert.)

**Vorzeichen:** negativ = Trim hätte gerettet = stützt die Hypothese.

**Eine Stufe = ein Leg.** Mehrere Zyklen derselben Stufe derselben Position
sind EIN Datenpunkt (`position_id`, `path`, `close_pct`), niemals mehrere —
das war der ZM-Fehler mit 191 Rows für eine Entscheidung.

## 4. Entscheidungsregel — jetzt festgelegt

**Hypothese bestätigt**, wenn ALLE gelten:
* n ≥ 20 unabhängige Legs
* Mittel ≤ −0.30pp
* t ≤ −2.0
* hält symmetrisches Trimmen: −1/+1 **und** −2/+2 bleiben ≤ −0.20pp
* ungünstigstes Leave-one-out bleibt ≤ −0.20pp bei t ≤ −2.0

**Hypothese verworfen**, wenn n ≥ 20 und (Mittel > −0.30pp oder t > −2.0).

**Unentschieden** sonst → weiter sammeln, aber siehe Abbruchregel.

Stichprobenzahl, mit sd = 1.74:

| angenommener Effekt | n für \|t\| ≥ 2 | bei ~0.32 Legs/Tag |
|---|---|---|
| −0.79pp (voller Pilot-Effekt) | 20 | ~2 Monate |
| −0.40pp (halber Effekt) | 76 | ~8 Monate |
| −0.26pp (Drittel) | 179 | ~1.5 Jahre |

Heißt: nur wenn der Effekt wirklich so groß ist wie im Piloten, ist die Frage
in überschaubarer Zeit entscheidbar. Ist er halb so groß, bleibt sie offen —
und das ist dann die Antwort, keine Ausrede.

## 5. Was ich NICHT tun werde

* **Keine nachträglichen Zeit-Splits als Ergebnis.** Genau ein Split ist
  erlaubt, am Mittelpunkt des Beobachtungsfensters, und nur als
  Stabilitätsprüfung — niemals als Headline.
* **Kein einseitiges Trimmen.** Ausreißer werden nur symmetrisch entfernt.
  (Einseitiges Kappen der Gewinner drückt fast jede Verteilung unter jede
  Schwelle — Lehre aus der Kandidaten-Robustheitsprüfung vom 25.09.)
* **Keine Untergruppen** nach Symbol, Region, Sektor, Strategie oder
  Signaltyp. Wenn eine auffällt, wird sie als *neue* Hypothese registriert.
* **Kein Nachjustieren der Schwellen** in Abschnitt 4, egal was die Daten
  zeigen.
* **Median und Anteil negativer Legs werden immer mitberichtet**, auch wenn
  sie dem Mittelwert widersprechen.

## 6. Abbruchregel

Kommen in **6 Wochen** (bis 2026-11-17) weniger als **5** auswertbare Legs
zusammen, wird die Hypothese als **nicht prüfbar** abgeschlossen — nicht als
„noch offen" weitergeschleppt.

Begründung: eine Hypothese ohne Datenzufluss ist kein Hebel, egal wie gut ihre
gemessene Kante aussieht. (Lehre aus Kandidat B
`MACD_TURN_BELOW_SMA20,TREND_PULLBACK`: überlebte jeden Robustheitstest und
war trotzdem wertlos, weil er seit dem 23.09. kein Signal mehr erzeugte.)

## 7. Offene Abhängigkeit — ohne sie ist das hier nicht messbar

**Die Daten aus Abschnitt 3 existieren heute nicht und entstehen auch nicht
von selbst.** Geprüft am 2026-10-06:

* `portfolio_snapshot` hält nur den **aktuellen** Stand (42 Rows = 42
  Positionen, wird überschrieben). Kein PnL-Pfad.
* `position_state.peak_pnl_pct` ist ein Maximum, kein Verlauf.
* Rückblickende Rekonstruktion ist damit **ausgeschlossen**.
* Seit `fix/ladder-regime-decision-stage` schreibt die unterdrückte Stufe auch
  keinen `partial_close_shadow`-Eintrag mehr (das war beabsichtigt — der
  Eintrag kam zusammen mit einem LLM-Call und war mit `allowed=1` falsch
  etikettiert).

**Nötig:** ein bewusster Schatten-Eintrag an der Unterdrückungsstelle in
`evaluate_trailing` — `path='trailing_regime_blocked'`, `allowed=0`,
`reason='regime_suppressed'`, mechanisches `close_pct` aus
`profit_levels_json`. Kein LLM-Call, keine Order, keine Geldwirkung. Der
vorhandene `_dedup_refresh` hält es bei einer Row pro Stufe.

**Wichtig — der naheliegende Ausweg funktioniert nicht:** abzuwarten, bis das
Regime DEFENSIVE verlässt (Equity ≥ ~9300, +4.2 % von 8922), und dann echte
Ladder-Legs zu messen, prüft die Hypothese **nicht**. Die Hypothese handelt
von der Leiter *im Drawdown*. Legs aus NORMAL/CAUTION beantworten eine andere
Frage — dort ist die Leiter ohnehin aktiv. Der Schatten-Eintrag ist der
einzige gültige Weg.

→ **Entscheidung von VoLLi ausstehend:** Schatten-Eintrag ergänzen (eigener,
geldneutraler Commit) oder diese Vorregistrierung ruhen lassen.

## 8. Wenn die Hypothese bestätigt wird

Dann steht *nicht* automatisch die Umsetzung an, sondern erst die Frage, welche
Form: Leiter im DEFENSIVE ganz freigeben, nur die unterste Stufe, oder
`close_pct` reduzieren. Das ist dann eine eigene Entscheidung — hier wird nur
die Richtung geprüft, nicht die Dosis.

---

**Prüfskript:** `~/.hermes/workspace/pc_shadow_pruefung.py` (Abschnitt 9 im
Docstring trägt den Variantenvergleich, aus dem diese Hypothese stammt).
