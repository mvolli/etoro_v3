# Vorregistrierung E7 — Insider-Clusterkäufe, 30 Tage bis 6 Monate (2026-10-17)

**Vor dem Test festgelegt** (VoLLi 17.10.: E7 + E8, S&P 600 einbeziehen, SEC-Kontakt
im User-Agent freigegeben). Entscheidung nur nach der Regel unten.

## Grundlage

Jeng/Metrick/Zeckhauser: Kaufportfolios von Insidern +0,37 bis 0,47 %/Monat nach
Größe, Value und Momentum, drei Viertel davon innerhalb von 6 Monaten. Cluster-
käufe (1986–2016): ~doppelt so stark wie Einzelkäufe, +2,5 % über 90 Handelstage.
Direktoren schwächer als Führungskräfte; Verkäufe ohne Signal; stärker bei
kleinen Werten.

## Daten

- SEC Insider Transactions Data Sets, Q1 2006 – Q3 2026 (`scripts/e7_fetch_insider.py`).
  Nur **Form 4 (Original, keine 4/A)**, Transaktionscode **P** (Kauf am offenen
  Markt), erworben (A). Wert = Stückzahl × Preis.
- Nur Melder, die **Director oder Officer** sind (10-%-Eigner ohne Amt zählen nicht).
- Kurse: yfinance OHLCV (auto-adjusted), Cache `data/e6_cache/bars/`.
- Universum: S&P 500 + 400 + 600 (Wikipedia, Abruf 17.10.) ∩ bei eToro handelbare
  US-Aktien; Zuordnung über `ISSUERTRADINGSYMBOL` (Punkt ⇒ Bindestrich).

## Signal

Stichtag = **Einreichungsdatum** F (nicht Transaktionsdatum). Betrachtet werden
Käufe mit Wert ≥ $10.000 je Transaktion, eingereicht in den 30 Kalendertagen bis
einschließlich F.

| Variante | Bedingung am Tag F |
|---|---|
| **I1** | ≥ 2 verschiedene Insider (owner_cik), zusammen ≥ $100.000 |
| **I2** | ≥ 3 verschiedene Insider, zusammen ≥ $250.000 |

Ausgelöst wird am ersten F, an dem die Bedingung erfüllt ist. Danach ist der Titel
für dieselbe Variante 90 Kalendertage gesperrt. **Einstieg** zum Open des ersten
Handelstags nach F. Höchstens eine offene Position je Titel und Variante.

## Ausstieg (wie E6)

Feste Haltedauer **H ∈ {30, 63, 126} Handelstage** (Ausstieg Close), dazu ein
Stop bei −15 % unter dem Einstieg (Tief ≤ Stop ⇒ min(Open, Stop)). Sonst nichts.

## Kosten

$880 je Position, $1 beim Kauf und $1 beim Verkauf. Spread je Runde: S&P 500/400
0,2 %, S&P 600 0,5 %.

## Zielgrößen

- **Primär:** Netto-Excess = Rendite − gleichgewichtetes Universum (S&P 1500
  handelbar) über dieselben Tage − Kosten.
- **Sekundär (berichtet):** gegen SPY; absolut netto; getrennt nach S&P 500/400
  und S&P 600.

## Split

Einstiegsdatum, Achse 2006-04-01 … 2026-10-15: erste 60 % Training (beschreibend),
letzte 40 % **Holdout**.

## Entscheidungsregel je Variante × Horizont (6 Tests, Holdout) — wie E6

1. n ≥ 300 und ≥ 40 verschiedene Einstiegswochen (sonst unentscheidbar).
2. Mittlerer Netto-Excess > 0.
3. t ≥ 2,4, SE pro Einstiegswoche (ISO) geclustert (Bonferroni, 6 Tests).
4. 5 %-getrimmter Netto-Excess > 0.
5. Leave-one-out über Titel: Vorzeichen bleibt.
6. Netto-Excess > 0 in mindestens ⌈2/3⌉ der Holdout-Kalenderjahre.

**Angenommen = alle.** Bestehen mehrere, gilt die mit dem höchsten t. Ergebnisse
aus E6 ändern diese Regeln nicht.

## Nachtrag vor dem Lauf (17.10., Daten geprüft, noch kein Ergebnis gesehen)

Die Signal- und Entscheidungsregeln oben bleiben unverändert. Ergänzt wurden nur
zwei Bereinigungen und eine Datenquelle:

1. **Ticker-Wiederverwendung:** 132 Kürzel im Universum wurden über die Jahre von
   mehr als einem Emittenten (CIK) gemeldet, zusammen 1 820 von 44 839 Käufen.
   Es zählen nur Meldungen des CIK, der das Kürzel zuletzt gemeldet hat.
   Dadurch fallen auch einige echte CIK-Wechsel weg, etwa Abspaltungen und
   Umgründungen. Das wirkt konservativ, weil es weniger Auslöser gibt.
2. **Unmögliche Werte:** Transaktionen über $100 Mio. werden ausgeschlossen
   (35 Zeilen). Sie sind überwiegend Preisfehler, etwa $44 394 je Aktie.
3. **Kurse:** Sie kommen aus `data/e7_cache/bars/` ab 2006
   (`scripts/e7_fetch_bars.py`), nicht aus `e6_cache`, weil dieser erst 2011
   beginnt. Quelle und Einstellungen sind dieselben.

Bekannt und nicht korrigiert: `e7_fetch_insider.py` übernimmt je Meldung nur den
ersten meldenden Insider. Bei gemeinsamen Meldungen zählt das weniger
verschiedene Insider, wirkt also ebenfalls konservativ.

## Grenzen (vorab)

Survivorship (Universum heute); Ticker aus der Meldung (Umbenennungen gehen
verloren); 10b5-1-Kennzeichen erst seit 2023, daher nicht genutzt; Spread pauschal.
Serien-Risiko: E3–E8 testen dieselbe Marktgeschichte; ein bestandener Test geht
unverändert live und wird über den Wochenbericht weiter geprüft.
