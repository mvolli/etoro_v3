# Vorregistrierung E8 — Qualität + Wachstum (+ Momentum), 30 Tage bis 6 Monate (2026-10-17)

**Vor dem Test festgelegt** (VoLLi 17.10.: „Beide: E7 Insider + E8 Qualität“).
Entschieden wird ausschließlich nach der Regel unten. Die Ergebnisse aus E6 und E7
ändern diese Regeln nicht.

## Grundlage

- Novy-Marx (2013): Bruttoprofitabilität (Bruttogewinn / Bilanzsumme) sagt
  Renditen voraus, auch bei großen Werten.
- Sloan (1996): Hohe Periodenabgrenzungen (Gewinn deutlich über dem operativen
  Cashflow) gehen mit schwächeren Folgerenditen einher.
- Asness/Frazzini/Pedersen („Quality minus Junk“): Profitabel, wachsend, solide
  schlägt das Gegenteil.
- Momentum (12–1 Monate) als Bestätigung, wie es bei Quality-Growth-Momentum-
  Screens professioneller Analysten üblich ist.

## Daten

- SEC Financial Statement Data Sets, 2009 Q1 – 2026 Q3
  (`scripts/e8_fetch_fsds.py`). Verwendet werden nur 10-K und 10-Q
  (Originale), Werte in USD, ohne Segmente und ohne Co-Registrant.
- **Stichtag für die Verfügbarkeit ist `accepted`** (Zeitpunkt der Annahme bei
  der SEC). Eine Meldung zählt erst ab dem Handelstag nach `accepted`.
- Die Zuordnung CIK → Ticker kommt aus SEC `company_tickers.json` (Stand heute).
- Kurse: `data/e7_cache/bars/` (yfinance, auto_adjust, ab 2006).
- Universum wie in E7: S&P 500 + 400 + 600 ∩ bei eToro handelbar. **Ohne
  Finanzwerte (SIC 6000–6999)**, weil Bruttogewinn dort nicht definiert ist.

## Kennzahlen je Meldung

Grundlage sind die Werte für das laufende Geschäftsjahr bis zum Stichtag (YTD:
`qtrs` = 1/2/3 im 10-Q, 4 im 10-K, `ddate` = Periodenende). Vergleichswert ist
dieselbe Periode des Vorjahres aus **derselben** Meldung.

| Kennzahl | Formel | gut = |
|---|---|---|
| **GP/A** | (Umsatz − Herstellkosten, oder GrossProfit) × 4/qtrs ÷ Bilanzsumme | hoch |
| **Accruals** | (Jahresüberschuss − operativer Cashflow) × 4/qtrs ÷ Bilanzsumme | niedrig |
| **Wachstum** | Umsatz YTD ÷ Umsatz YTD Vorjahr − 1 | hoch |

Tag-Reihenfolge, die erste vorhandene zählt:

- **Umsatz:** Revenues, RevenueFromContractWithCustomerExcludingAssessedTax,
  RevenueFromContractWithCustomerIncludingAssessedTax, SalesRevenueNet,
  SalesRevenueGoodsNet, SalesRevenueServicesNet.
- **Bruttogewinn:** GrossProfit. Sonst Umsatz minus Herstellkosten, mit
  CostOfRevenue, CostOfGoodsAndServicesSold, CostOfGoodsSold, CostOfServices in
  dieser Reihenfolge.
- **Jahresüberschuss:** NetIncomeLoss, sonst ProfitLoss.
- **Operativer Cashflow:** NetCashProvidedByUsedInOperatingActivities, sonst
  …ContinuingOperations.
- **Bilanzsumme:** Assets zum Periodenende.

Eine Meldung ohne eine dieser Größen liefert keine Kennzahlen. Bilanzsumme und
Vorjahresumsatz müssen größer als 0 sein.

**Zuordnungsregeln:**

- **Zeitraum:** `fp` und `qtrs` müssen zusammenpassen: Q1 → 1, Q2 → 2, Q3 → 3,
  FY → 4. Laufende Werte haben `ddate == period`, die Bilanzsumme ebenfalls mit
  `qtrs = 0`. Meldungen, die nicht passen, werden verworfen.
- **Vorjahreswert:** gleiches `qtrs`, `ddate` 350 bis 380 Tage vor dem laufenden
  Wert und **derselbe Tag** wie der laufende Wert. Es zählt der erste Tag der
  Liste, der beide Werte hat. So wird beim ASC-606-Wechsel kein Tag gegen einen
  anderen verglichen.
- **Herstellkosten:** Werden sie statt GrossProfit verwendet, gilt die gleiche
  Regel für Umsatz und Kosten: gleiches `ddate` und gleiches `qtrs`.
- **Dubletten:** Hat ein Schlüssel (adsh, tag, ddate, qtrs) mehrere
  *verschiedene* Werte, gilt dieser Tag in dieser Meldung als fehlend, und der
  nächste Tag der Liste wird geprüft. Identische Dubletten werden zu einem Wert
  zusammengefasst.

## Signal (monatlich)

Stichtag ist jeweils der **letzte Handelstag eines Monats** M. Für jeden Titel
gilt die jüngste Meldung, deren `accepted` vor M liegt und die höchstens
200 Kalendertage alt ist. Zu jeder Kennzahl wird das Perzentil-Rang über alle
Titel mit vollständigen Kennzahlen gebildet; bei Accruals wird der Rang
umgekehrt. Der **Score** ist der Mittelwert der drei Ränge.

| Variante | Bedingung zum Stichtag M |
|---|---|
| **Q1** | Score im obersten Dezil (≥ 90. Perzentil) |
| **Q2** | Q1 und dazu 12–1-Monats-Momentum (Close M−21 Handelstage ÷ Close M−252 − 1) über dem Median der Titel mit vollständigen Kennzahlen zum Stichtag M |

**Einstieg** zum Open des ersten Handelstags nach M. Je Titel, Variante und
Horizont ist höchstens eine Position offen. Ein Titel, der im Folgemonat
weiter im Dezil steht, löst keinen neuen Trade aus, solange seine Position
noch offen ist.

## Ausstieg und Kosten (wie E6/E7)

- Feste Haltedauer **H ∈ {30, 63, 126} Handelstage**, Ausstieg zum Close.
- Stop bei −15 % unter dem Einstieg (Tief ≤ Stop ⇒ min(Open, Stop)).
- Je Position $880, $1 beim Kauf und $1 beim Verkauf.
- Spread je Runde: S&P 500/400 0,2 %, S&P 600 0,5 %.

## Zielgrößen

- **Primär:** Netto-Excess = Rendite − gleichgewichtetes Universum **ohne
  Finanzwerte** (S&P 1500 handelbar, ohne SIC 6000–6999) über dieselben Tage −
  Kosten. Q1 und Q2 können keine Finanzwerte wählen. Ein Vergleich mit dem
  Universum inklusive Finanzwerte wäre daher eine Sektorwette.
- **Sekundär (berichtet):** gegen das Universum inklusive Finanzwerte; gegen
  SPY; absolut netto; getrennt nach Segment.

## Split

Die Achse reicht vom Start bis 2026-10-15. Die ersten 60 % der Einstiegsdaten
sind Training (beschreibend), die letzten 40 % **Holdout**.

**Start der Achse:** Ab 2009 kamen zunächst nur große Firmen in XBRL, kleinere
folgten später. Die Ränge sollen nicht über eine fast reine Large-Cap-Menge
gebildet werden. Deshalb beginnt die Achse mit dem ersten Stichtag M, an dem
**mindestens 70 %** der Nicht-Finanzwerte im Universum (mit Kursen)
vollständige Kennzahlen haben. Diese Abdeckung wird vor dem Lauf ohne Renditen
gemessen und im Nachtrag festgehalten.

## Entscheidungsregel je Variante × Horizont (6 Tests, Holdout)

1. n ≥ 300 und ≥ 40 verschiedene Einstiegsmonate (sonst unentscheidbar).
2. Mittlerer Netto-Excess > 0.
3. **t ≥ 2,4**, berechnet aus den Monatskohorten: Mittelwert des Netto-Excess je
   Einstiegsmonat, t mit Newey-West-Standardfehler, Lags = ⌈H/21⌉. Das weicht
   von E6/E7 ab, weil sich überlappende Monatskohorten sonst zu einer zu kleinen
   Streuung führen würden. Die Bonferroni-Korrektur gilt für 6 Tests.
4. 5 %-getrimmter Netto-Excess > 0 (über die Trades).
5. Leave-one-out über Titel: Das Vorzeichen bleibt.
6. Netto-Excess > 0 in mindestens ⌈2/3⌉ der Holdout-Kalenderjahre.

**Angenommen = alle Kriterien erfüllt.** Bestehen mehrere Varianten, gilt die mit
dem höchsten t.

## Grenzen (vorab)

- **Survivorship:** Das Universum ist das von heute, ebenso die
  CIK-Ticker-Zuordnung.
- **Daten:** XBRL-Tags sind nicht einheitlich. Firmen ohne passende Tags fallen
  heraus.
- **Kosten:** Der Spread ist pauschal angesetzt.
- **Serien-Risiko:** E3 bis E8 testen dieselbe Marktgeschichte. Ein bestandener
  Test geht unverändert live und wird über den Wochenbericht weiter geprüft.
