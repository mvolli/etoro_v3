# Vorregistrierung E6 — Earnings-Katalysator-Long, 30 Tage bis 6 Monate (2026-10-17)

**Vor dem Test festgelegt.** VoLLi 17.10.: Long-Strategie wie ein professioneller
Analyst, Haltedauer 30 Tage bis 6 Monate, definitives Kaufsignal. Entscheidung nur
nach der Regel unten; kein Nachjustieren nach Sicht des Holdouts.

## Grundlage

**Recherche (17.10.):** Für 1–6 Monate am besten belegt sind:

- **Gewinnüberraschungen mit Marktbestätigung** (Post-Earnings-Drift): bei
  Large Caps seit ~2006 stark abgeschwächt, Studienlage gemischt.
- **Analysten-Revisionen:** Kursreaktion bis ~6 Monate nach Upgrades; Stickel:
  Up- minus Down-Portfolios +7 %.
- **Insider-Clusterkäufe:** ~+2,5 % Überrendite über 90 Tage (Daten hier nur 2 Jahre,
  nicht testbar).
- **Trump-Posts:** nur kurzlebige Volatilität (Review 2026), kein Kaufsignal.

**RoboCop (17.10.):** Er sieht Earnings und Kurse als backtestbar an; Insider und
Truth Social fehlen im Bot; das LLM soll Veto sein, nicht Signal. Lehre aus NVDA
(24.08.): Ein AVOID-Flag muss in jedem Kaufpfad wirken. Sein Einwand „Analysten
nicht zeitpunktgenau“: Die Ratings tragen ein Datum je Ereignis, die Abdeckung ist
aber lückenhaft — S2 ist deshalb ein eigener Test.

**Advisor:** Er legt fest: Maßstab = Universumsmittel statt SPY (Survivorship),
Tageszeit des Berichts beachten, SE pro Kalenderwoche clustern, eine einzige
Ausstiegsregel vorab, News und Truth Social nur als Live-Veto.

## Universum und Daten

- S&P 500 + S&P 400 (Liste Wikipedia, Abruf 17.10.) ∩ bei eToro handelbare
  US-Aktien = **842 Titel**. Survivorship: heutige Mitglieder.
- yfinance: OHLCV täglich (auto-adjusted) 2011 → 2026-10-15; Earnings-Termine mit
  EPS-Schätzung, Ist-EPS und Uhrzeit; Analysten-Ratings (`upgrades_downgrades`,
  Datum je Ereignis). Cache `data/e6_cache/`.

## Ereignis und Signal

- **Bericht:** |EPS-Schätzung| ≥ $0,05 (sonst ist die Überraschung instabil).
  Überraschung = (Ist − Schätzung) / |Schätzung| × 100.
- **Tag 0:** Bericht vor 16:00 ET ⇒ Tag 0 = Berichtstag. Nach Schluss oder Uhrzeit
  unbekannt ⇒ nächster Handelstag.
- **Reaktion:** Close(Tag 0) / Close(Tag −1) − 1 minus SPY über dieselben Tage.
- **Entscheidung** zum Close von Tag +1, **Einstieg** zum Open von Tag +2.

| Variante | Bedingungen |
|---|---|
| **S1** | Überraschung ≥ +5 % **und** Reaktion ≥ +2 % über SPY **und** Close(Tag +1) > SMA200 |
| **S2** | S1 **und** Rating-Saldo ≥ +1 in Tag −1 … Tag +1 (Upgrade +1, Downgrade −1, Kursziel erhöht +1, gesenkt −1) |

Höchstens eine offene Position je Titel und Variante.

## Ausstieg (eine Regel, vorab)

Feste Haltedauer **H ∈ {30, 63, 126} Handelstage** (Ausstieg Close), dazu ein
Stop bei −15 % unter dem Einstieg (Tief ≤ Stop ⇒ Ausstieg zu min(Open, Stop)).
Sonst nichts — kein Break-Even, keine Teilverkäufe.

## Kosten

$880 je Position, $1 Provision beim Kauf und $1 beim Verkauf, Spread 0,2 % je Runde
⇒ 0,43 % je Trade.

## Zielgrößen

- **Primär:** Netto-Excess = Trade-Rendite − Rendite des gleichgewichteten
  Universums über dieselben Tage − Kosten.
- **Sekundär (berichtet):** gegen SPY; absolut netto.

## Split

Einstiegsdatum, Achse 2012-01-01 … 2026-10-15: erste 60 % Training (nur
beschreibend), letzte 40 % **Holdout** (entscheidet).

## Entscheidungsregel je Variante × Horizont (6 Tests, Holdout)

1. n ≥ 300 Trades **und** ≥ 40 verschiedene Einstiegswochen (sonst unentscheidbar).
2. Mittlerer Netto-Excess > 0.
3. **t ≥ 2,4**, SE pro Einstiegswoche (ISO) geclustert — Bonferroni für 6 Tests,
   einseitig α = 0,05.
4. 5 %-getrimmter Netto-Excess > 0.
5. Leave-one-out über Titel: Vorzeichen bleibt.
6. Netto-Excess > 0 in mindestens ⌈2/3⌉ der Holdout-Kalenderjahre.

**Angenommen = alle Kriterien.** Bestehen mehrere, wird die Kombination mit dem
höchsten t das **definitive Kaufsignal**. Besteht keine: kein definitives Signal.

## Live-Schicht (nicht getestet, nur Veto)

Das LLM prüft vor dem Kauf News, 8-K/10-Q und Truth-Social-Erwähnungen von Firma
oder Branche (Zölle, Sanktionen) auf Warnsignale: Prognose gesenkt,
Bilanzkorrektur, Klage, CEO-Abgang, Zoll-Risiko. Es darf nur **ablehnen**, nie
auslösen. Das Veto muss in jedem Kaufpfad wirken (Lehre NVDA). Kein Abo bei
Truth-Social-Anbietern ohne VoLLi-Freigabe.

## Grenzen (vorab)

Survivorship (Universum heute); Rating-Abdeckung lückenhaft (S2 schmaler);
Earnings-Termine aus yfinance ohne Prüfung gegen eine zweite Quelle; Spread
pauschal. Live bräuchte die Strategie eigene Ausstiege (Zeitstopp/Stop je
Strategie statt der 28 Tage / 3,5–6 % heute) und ein eigenes Cash-Budget neben
`signal_trades` und dem Index-Kern.
