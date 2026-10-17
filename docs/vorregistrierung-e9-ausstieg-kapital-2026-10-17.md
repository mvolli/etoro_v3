# Vorregistrierung E9 — Ausstiege und Kapital für Q2 + Earnings-Katalysator (2026-10-17)

**Vor dem Test festgelegt.** Entschieden wird ausschließlich nach den Regeln unten.

## Auftrag (VoLLi 17.10.)

- **Ziel ist die Gewinnzone, nicht den Markt zu schlagen.** Der Bot soll nach
  eigenen Entscheidungen Kandidaten finden und mit ihnen handeln.
- **Strategie:** Analysten-Screen Q2 (E8: Qualität + Wachstum + Momentum,
  monatlich) **und** Earnings-Katalysator S1 (E6).
- **Ausstieg:** nicht starr nach 126 Handelstagen, sondern „optimiert berechnet
  oder analysiert“. Schließen ist erlaubt, wenn kein weiterer Anstieg zu erwarten
  ist, vor Earnings oder bei negativen News.
- **Kapital:** fundierte Bewertung, mit dem Advisor abgestimmt.

## Daten und Kandidaten

- **Universum:** S&P 500 + 400 ∩ bei eToro handelbar. Ohne S&P 600, weil es
  Earnings-Termine (für X1) nur für 500/400 gibt und die Spreads dort niedriger
  sind. **Offenlegung:** In E7/E8 war 500/400 auch das bessere Segment; dieser
  Befund lag vor der Wahl vor.
- **Kurse:** `data/e7_cache/bars/`. **Earnings-Termine:** `data/e6_cache/earn/`.
- **Q2-Kandidaten:** exakt nach den Regeln von E8 (Rangmenge über alle
  Nicht-Finanzwerte des S&P 1500 mit Kennzahlen, wie in E8). Gekauft werden nur
  Titel aus 500/400, in der Reihenfolge ihres Scores. Einstieg zum Open des
  ersten Handelstags nach dem Monatsstichtag.
- **S1-Kandidaten:** exakt nach den Regeln von E6 (Überraschung ≥ 5 %,
  Reaktion ≥ +2 % gegen SPY, Close über SMA200). Einstieg zum Open von day0+2.
- **Zeitraum:** nur Holdout-Einstiege ab **2020-11-11**, also der spätere der
  beiden Holdout-Starts von E6 und E8. Ende ist 2026-10-15; offene Positionen
  werden dann zum Close bewertet.

## Ausstiegsvarianten

Alle Varianten haben einen Stop bei −15 % unter dem Einstieg (Tief ≤ Stop ⇒
min(Open, Stop)).

| Variante | Regel | Höchstdauer |
|---|---|---|
| **X0** | fest 126 Handelstage (Basis, wie getestet) | 126 |
| **X1** | Ausstieg zum Close des Handelstags **vor** dem nächsten Earnings-Termin nach dem Einstieg. Bei S1 ist der auslösende Termin ausgenommen. | 252 |
| **X2** | Trendbruch: Close unter SMA50 ⇒ Ausstieg zum Open des nächsten Tages, frühestens ab Tag 10 nach dem Einstieg | 252 |
| **X3** | Nachlaufender Stop: Stop = max(Einstieg × 0,85; höchster Close seit Einstieg × 0,80) | 252 |

News-Ausstiege mit LLM-Urteil lassen sich nicht backtesten und gehören nicht
zu E9.

## Portfolio-Simulation

**Kapital:** C = $4 600 für Aktien. Das sind Equity $8 797 − Index-Kern 30 % −
Reserve 18 %; der Altbestand ist dann ausgelaufen.

**Plätze und Tranche:** k ∈ {4, 8, 12} Plätze, Tranche T = C / k, also $1 150,
$575 bzw. $383.

**Ablauf:** täglich, chronologisch.

- Kandidaten des Tages (Q2-Stichtag und/oder S1) werden in Score-Reihenfolge
  gekauft, solange ein Platz frei ist und der Titel nicht schon gehalten wird.
- Bei S1 entscheidet bei Gleichstand der Zufall (S1 ist unsortiert).
- Ein Ausstieg gibt den Platz frei.

**Kosten:** je Runde 2 × $1 / T + 0,2 % Spread.

**Bewertung:** täglich zum Close. Freies Cash bringt keinen Zins. Keine
Zinseszinsen, die Tranche ist fest.

**Läufe:** 200 Läufe mit zufälligem Tie-Break bei S1 und zufälliger Reihenfolge
bei Score-Gleichstand. Berichtet wird der Median über diese Läufe.

**Kennzahlen je (Variante, k):**

- Jahresrendite auf C;
- maximaler Drawdown;
- Anteil der Kalenderjahre mit Verlust;
- 5-%-Quantil der rollierenden 6-Monats-Rendite;
- Trades pro Jahr und Kosten pro Jahr;
- Vergleich: SPY Buy & Hold mit demselben C.

## Entscheidungsregeln

**Ausstieg (bei k = 8):** Eine Variante X1–X3 ersetzt X0 nur, wenn alle drei
Punkte gelten:

1. Die Median-Jahresrendite liegt **≥ 1,0 Prozentpunkte über X0**.
2. Der maximale Drawdown ist höchstens 2 Prozentpunkte schlechter als bei X0.
3. In mindestens ⌈2/3⌉ der Holdout-Kalenderjahre liegt die Jahresrendite
   ≥ X0 (Median-Lauf).

Erfüllen mehrere Varianten das, gilt die mit der höchsten Median-Jahresrendite.
Erfüllt keine es, bleibt X0.

**Kapital:** Die Simulation entscheidet nicht automatisch. Berichtet wird die
Tabelle für k = 4, 8 und 12 mit der gewählten Ausstiegsvariante. Die
Empfehlung folgt daraus: das kleinste k, dessen 5-%-Quantil der 6-Monats-Rendite
nicht mehr deutlich besser wird. VoLLi entscheidet.

## Grenzen (vorab)

- **Ein einziger Marktpfad**, 2020–2026, überwiegend steigend. Zufall steckt
  nur in der Reihenfolge, nicht im Markt.
- **Survivorship:** Das Universum ist das von heute.
- **Kosten:** Der Spread ist pauschal angesetzt.
- **Wiederholte Tests:** E9 testet auf denselben Holdout-Daten wie E6 und E8.
  Ergebnisse sind deshalb vorsichtig zu lesen.
