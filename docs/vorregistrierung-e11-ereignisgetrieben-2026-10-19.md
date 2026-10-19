# Vorregistrierung E11 — Ereignisgetriebene Käufe statt Monats-Screen (2026-10-19)

**Vor dem Test festgelegt.** VoLLi hat am 19.10. „Ja, alle drei Varianten“ gewählt.
Es gibt genau einen Lauf, entschieden wird ausschließlich nach der Regel unten. Über
einen Live-Gang entscheidet VoLLi danach.

## Frage

VoLLi: „Profi-Analysten kaufen nicht einmal im Monat, sondern wenn die Vorzeichen gut
stehen.“ Das führt zu drei Fragen:

- Bringen ereignisgetriebene Auslöser auf Basis des Qualitäts-Screens Q2 (E8) mindestens
  so viel wie der live genutzte Monats-Screen?
- Ist das Ergebnis statistisch belastbar?
- Liegt es im Portfolio in der Gewinnzone?

Ideen: VoLLi und Claude (A und B), RoboCop (C).

## Gemeinsame Basis (wie E8/E9)

- **Kennzahlen je Meldung:** GP/A, Accruals, Umsatzwachstum aus den SEC Financial
  Statement Data Sets (`data/e8_cache`). Die Regeln stehen in
  `quality_screen.filing_metrics`, dieselben wie E8 und live.
- **Rangmenge zum Tag D:** alle Nicht-Finanzwerte des S&P 1500 mit Kurs an D. Je Titel
  zählt die jüngste Meldung mit `accepted` vor D, höchstens 200 Tage alt.
  - Der Score ist der Mittelwert der drei Perzentil-Ränge.
  - Top-Dezil heißt Score ≥ 90. Perzentil der Rangmenge an D.
- **Momentum:** Close(D−21) / Close(D−252) − 1. Der Median wird über die Rangmenge an D
  gebildet.
- **Kaufbar:** nur S&P 500/400.
- **Kurse:** `data/e7_cache/bars`.
- **Ausstieg X0:** 126 Handelstage, Stop −15 %. Je Titel höchstens eine Position.
- **Kosten:** 2 × $1 / $575 + 0,2 % Spread.
- **Zeitraum:** Holdout wie E9, Einstiege 2020-11-11 bis 2026-10-15.

## Varianten

| Variante | Auslöser | Entscheidung / Einstieg |
|---|---|---|
| **A — Q2 bei Einreichung** | Ein Titel reicht 10-Q/10-K ein (`accepted` an Tag F). Mit den neuen Kennzahlen liegt er an d1 (erster Handelstag nach F) im Top-Dezil der Rangmenge, Momentum > Median. **Nur beim Eintritt:** Seine vorherige Meldung lag an ihrem eigenen d1 nicht im Top-Dezil, oder es gab keine. | Close d1 / Open d1+1 |
| **B — Qualität + Earnings** | S1-Ereignis nach E6 (Überraschung ≥ 5 %, Reaktion ≥ +2 % gegen SPY, über SMA200) **und** Q2-Score des Titels im Top-Dezil der Rangmenge an d1 | wie S1: Open day0+2 |
| **C — Qualität + Insider (RoboCop)** | Mindestens 2 verschiedene Director/Officer-Käufer (Form 4, Code P, ≥ $10k je Transaktion; Daten und Bereinigung **unverändert wie E7**: nur Original-Form-4, nur der jüngste CIK je Ticker, Wert ≤ $100 Mio., je Meldung nur der erste Melder) mit Einreichungsdatum innerhalb von 7 Kalendertagen, darunter mindestens ein Officer, zusammen ≥ $50k. Auslösetag F ist das Einreichungsdatum der vervollständigenden Meldung, danach 90 Tage Sperre je Titel. Dazu Q2-Score im Top-Dezil an F | Open des ersten Handelstags nach F |

**Vergleichsbasis M:** der Monats-Screen Q2 (Kandidaten wie E9), gleich gemessen.

## Kennzahlen je Variante (Holdout)

- **Pro-Trade-Kennzahlen und NW-t nur aus abgeschlossenen Trades** (126 Handelstage
  vor 2026-10-15 erreicht oder vorher gestoppt). Die Portfolio-Simulation bewertet
  noch offene Positionen zum Close am Ende. Die Regel gilt gleich für M, A, B und C.
  Die FSDS-Daten enden mit 2026 Q3, für abgeschlossene Trades ist das unerheblich.
- n, Ø netto, Median, Trefferquote;
- **NW-t:** Mittel je Einstiegswoche (ISO), Newey-West mit 26 Lags;
- Portfolio wie E9/E10 mit 8 Plätzen, $4 600, $575 je Tranche, 200 Läufe:
  Jahresrendite (Median, 10-%-Quantil), max. Drawdown, Jahre positiv.
  - **Reihenfolge wie live (E9):** Kandidaten vom Q2-Typ (M, A, B, C) in absteigender
    Score-Reihenfolge, Zufall nur bei Gleichstand.
  - S1 zufällig dazwischen.
- Portfolios **„A + S1“ und „M + S1“**, also so, wie es live läuft. Sie sind Teil der
  Ersetzungsregel unten.
- Beschreibend auch für den Zeitraum vor dem Holdout (Einstiege 2011-09 bis
  2020-11-10): Ø netto, NW-t, Trefferquote für M, A, B und C. Ein Effekt, der nur im
  Holdout auftaucht, wäre ein Warnzeichen.

## Entscheidungsregel je Variante A, B, C

**Angenommen nur, wenn alle Punkte gelten:**

1. n ≥ 100 Trades und ≥ 30 Einstiegswochen, sonst unentscheidbar.
2. Ø netto > 0 und **NW-t ≥ 2,4** (drei Varianten, Bonferroni-ähnlich).
3. Portfolio-Median **und** 10-%-Quantil der Jahresrendite > 0.
4. Im Median-Lauf sind mindestens ⌈2/3⌉ der Holdout-Jahre positiv.

**Empfehlung für live:**
- **A ersetzt den Monats-Screen M** nur, wenn alle drei Bedingungen gelten:
  - A ist angenommen;
  - der Portfolio-Median von A allein ist mindestens so hoch wie der von M allein;
  - der Median von „A + S1“ ist mindestens so hoch wie der von „M + S1“.
- **B und C** kämen nur als Zusatz-Auslöser in Frage, wenn sie angenommen sind.
- Die Entscheidung trifft VoLLi.

## Grenzen

- Derselbe Marktpfad wie E8, E9 und E10.
- Survivorship-Bias.
- C wird vermutlich wenige Fälle haben und dann unentscheidbar sein.
- B ist eine Teilmenge von S1, die nach S1 wählt. S1 selbst ist live.
- **Kosten eines Live-Gangs von A:** A bräuchte einen täglichen Filings-Feed (EDGAR
  Daily Index oder companyfacts-Abfrage nach Einreichung) statt des Monatslaufs.
- Der Holdout wurde schon in E8, E9 und im US-Vergleich verwendet, deshalb wird der
  Zeitraum davor zusätzlich berichtet.
