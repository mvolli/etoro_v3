# Vorregistrierung E10 — Earnings-Katalysator S1 in Europa (2026-10-19)

**Vor dem Test festgelegt.** VoLLi hat am 19.10. zugestimmt („ja“). Es gibt genau
einen Lauf, entschieden wird ausschließlich nach der Regel unten. Der Test liefert
keinen Live-Schalter; über einen Live-Gang entscheidet VoLLi danach.

## Frage

Landet der Earnings-Katalysator aus E6 mit dem live genutzten Ausstieg X0 auch bei
europäischen Aktien in der Gewinnzone, nach eToro-Gebühren, Spread und
Börsensteuern? Der Qualitäts-Screen Q2 lässt sich in Europa nicht testen, weil es
keine kostenlosen Bilanzdaten mit historischem Stand gibt.

## Universum

- Bestandteile von DAX, MDAX, CAC 40, FTSE 100, FTSE 250, AEX, AMX, IBEX 35,
  FTSE MIB, SMI, OMX Stockholm 30, OMX Copenhagen 25, OMX Helsinki 25, OBX und
  BEL 20 laut Wikipedia, abgerufen am 19.10.
- Davon nur Titel, die bei eToro handelbar sind. Für jeden Titel muss die Zuordnung
  am Kurs bestätigt sein: eToro-Kurs und Yahoo-Kurs weichen höchstens 15 % ab,
  der Faktor 100 für Pence ist erlaubt.
- Ergebnis: **629 Titel**, festgehalten in `docs/e10-universum-2026-10-19.json`
  (Skript `scripts/e10_fetch_eu.py`). 12 Titel wurden wegen abweichender Kurse
  verworfen.
- Der FTSE 250 enthält Investment Trusts; sie bleiben im Universum.
- Survivorship-Bias: Es zählt die Zusammensetzung von heute.

## Daten

- **Kurse:** yfinance mit `auto_adjust`, ab 2006, in Lokalwährung.
- **Wechselkurse:** EURUSD, GBPUSD, CHFUSD, SEKUSD, DKKUSD, NOKUSD (yfinance,
  täglich). Das Konto bucht in USD.
- **Termine:** yfinance `get_earnings_dates(limit=100)` mit Schätzung und Ist,
  dieselbe Quelle wie in E6.

## Signal (Regeln wie E6, angepasst an europäische Handelszeiten)

1. **Überraschung** = (Ist − Schätzung) / |Schätzung| ≥ 5 %, mit |Schätzung| ≥ 0,05.
   Wegen Einheitenfehlern (Pence/Pfund) bleiben Ereignisse mit |Ist/Schätzung| > 20
   oder 0 < Ist/Schätzung < 0,05 außen vor.
2. **Reaktionstag day0.** Den Zeitstempel rechne ich in die Ortszeit der Börse um.
   - Liegt er vor dem Börsenschluss (London 16:30, Oslo 16:20, Kopenhagen 17:00,
     Helsinki 18:30, sonst 17:30), ist day0 das lokale Datum, wenn das ein
     Handelstag ist. Sonst ist es der nächste Handelstag.
   - Meldungen nach Schluss haben als day0 den nächsten Handelstag.
   - Meldungen **ohne Uhrzeit** (in New Yorker Zeit genau 00:00) haben als day0
     das Datum selbst, sonst den nächsten Handelstag. Das weicht von E6 ab, siehe
     Nachtrag.
3. **Reaktion:** Rendite day0 (Close zu Close) minus Rendite des gleichgewichteten
   Universums am selben Tag ≥ +2 %.
4. **Trend:** Close an day0+1 über dem SMA200.
5. **Einstieg** zum Open an day0+2. Je Titel ist höchstens eine Position offen.

## Ausstieg und Kosten

**Ausstieg X0** (live seit E9): 126 Handelstage halten, Stop bei −15 % (Tief ≤
Stop ⇒ min(Open, Stop)).

| Kostenart | Ansatz je Runde |
|---|---|
| Gebühr | 2 × $1 bei $575 Tranche (0,35 %) |
| Spread große Indizes | 0,2 % |
| Spread MDAX, FTSE 250, AMX | 0,5 % |
| Kaufsteuer UK | 0,5 % (Stempelsteuer, von eToro weitergegeben) |
| Kaufsteuer FR | 0,4 % |
| Kaufsteuer ES | 0,2 % |
| Kaufsteuer IT | 0,1 % |
| Kaufsteuer IE | 1,0 % |

Bei FR, ES, IT und IE ist nicht geklärt, ob eToro die Steuer weitergibt. Ich setze
sie deshalb vorsichtig an. Einen FX-Aufschlag zieht eToro laut Kostendokument
nicht ab.

**Rendite in USD (primär):** (Ausstiegskurs × FX am Ausstieg) / (Einstiegskurs ×
FX am Einstieg) − 1, jeweils mit dem FX-Schlusskurs desselben Tages. Die
Lokalwährungsrendite berichte ich nur zusätzlich.

## Split

Die Einstiegsachse läuft von 2006-01-01 bis 2026-10-15. Die ersten 60 % sind
Training (nur beschreibend), die letzten 40 % der **Holdout**.

## Entscheidungsregel (Holdout, ein Test: S1 mit X0)

**Angenommen nur, wenn alle Punkte gelten:**

1. n ≥ 150 Trades und ≥ 30 verschiedene Einstiegswochen, sonst unentscheidbar.
2. Mittlere absolute Netto-Rendite je Trade in USD > 0, mit **t ≥ 2,0**.
   - Berechnung wie in E8: erst das Mittel je Einstiegswoche (ISO), dann ein
     Newey-West-t über diese Wochenreihe mit 26 Lags.
   - Grund: Positionen über ein halbes Jahr überlappen und teilen denselben
     Marktpfad, eine einfache Clusterung nach Woche würde den Fehler
     unterschätzen.
3. Portfolio-Simulation wie E9 in USD mit 8 Plätzen, $4 600 Kapital, $575 je
   Tranche und den Kosten oben, 200 Läufe mit zufälliger Reihenfolge bei
   Gleichstand:
   - Median der Jahresrendite > 0;
   - **10-%-Quantil der Jahresrendite > 0.**
4. Im Median-Lauf ist das Ergebnis in mindestens ⌈2/3⌉ der Holdout-Kalenderjahre
   positiv.

## Beschreibend berichtet

- Rendite über dem gleichgewichteten Universum und über dem STOXX 600 (^STOXX).
- STOXX 600 Buy & Hold mit demselben Kapital in USD, als Vergleich mit der
  Simulation.
- Ergebnis je Land und je Indexgruppe (groß/mittel).
- Trefferquote sowie das 5-%- und 95-%-Quantil.
- Training gegen Holdout.

## Grenzen

- Nur ein Marktpfad.
- Survivorship-Bias.
- Halbjährlich berichtende Firmen haben weniger Termine.
- Die Kaufsteuern bei FR, ES, IT und IE sind unbestätigt. Das Land bestimme ich
  über den Börsen-Suffix, die Steuern richten sich aber nach dem Firmensitz.
- Die Uhrzeiten von Yahoo bei europäischen Meldungen sind teils unplausibel (00–02
  Uhr Ortszeit). Nach der Regel oben zählen sie als „vor Börsenschluss“.

## Nachtrag vor dem Lauf (19.10., reine Datenprüfung, keine Strategie-Renditen)

**Reaktionstag.** Ich habe das Handelsvolumen am Meldedatum (Tag A) mit dem am
nächsten Handelstag (Tag B) verglichen, je nach Uhrzeit der Meldung:

| Uhrzeit (Ortszeit) | n | A höher | B höher |
|---|---|---|---|
| 00–02 | 2 522 | 35 % | 65 % |
| 03–09 | 9 844 | 77 % | 23 % |
| 10–15 | 3 594 | 74 % | 26 % |
| 16–23 | 2 280 | 48 % | 52 % |
| ohne Uhrzeit | 699 | 83 % | 17 % |

Bei 00–02 Uhr fällt das lokale Datum auf Tag B. Die Regel „vor Börsenschluss am
lokalen Datum“ trifft damit für alle Uhrzeiten den volumenstärkeren Tag. Nur bei
Meldungen ohne Uhrzeit wäre die E6-Regel („nächster Handelstag“) in Europa falsch,
deshalb gilt dort das Datum selbst.

**EPS-Einheiten.** Bei 18 847 Ereignissen mit Schätzung habe ich das Verhältnis
Ist zu Schätzung geprüft:
- 14 liegen über 20;
- 23 liegen zwischen 0 und 0,05;
- 11 liegen bei etwa 100 bzw. 0,01.

Daraus folgt die Bereinigungsregel unter „Signal“.

**Advisor-Prüfung:** Rendite in USD statt Lokalwährung; t-Wert nach Newey-West
über Wochenmittel statt Clusterung nach Woche.
