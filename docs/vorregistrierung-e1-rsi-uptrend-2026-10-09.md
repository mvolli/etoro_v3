# Vorregistrierung E1 — Kurzfrist-Überverkauftheit im Aufwärtstrend (2026-10-09)

**Vor jedem Test festgelegt.** Hypothese und Entscheidungsregel stammen NICHT aus den
bisher gemessenen Live-Trades (die sind als Auswahlgrundlage verbrannt).

## Hypothese
Eine Aktie, deren RSI(14) unter 30 fällt, während ihr Schlusskurs über dem SMA(50) liegt,
erzielt in den folgenden 10 Handelstagen eine Rendite über dem gleichtägigen Universums-Median.
Begründung (Marktlehrmeinung, Kurzfrist-Umkehr im intakten Trend): kurzer Ausverkauf in einem
Aufwärtstrend wird überdurchschnittlich oft zurückgekauft.

## Definitionen (fix)
- Universum: `data/exit_replay_bars/*.csv` mit mindestens 250 Tageskerzen.
- Signal (Tag t): RSI(14) < 30 UND Close(t) > SMA(50)(t). Pro Symbol höchstens ein Signal
  je 10-Tage-Fenster (keine Überlappung).
- Zielgröße: Vorwärtsrendite über 10 Tage ab Close(t), **minus** Median-Vorwärtsrendite des gesamten
  Universums am selben Tag (Excess-Return).
- Kosten: **1,1 % Rundenkosten** abgezogen (Flat-Fee $2 Hin+Rück auf $200 = 1,0 % + Spread ~0,1 %).

## Stichprobe und Split
- Zeitlicher Split: die ersten 60 % der Handelstage sind Trainingsfenster (nur Beschreibung,
  KEINE Entscheidung). Die letzten 40 % sind das Holdout-Fenster und das einzige, das über E1 entscheidet.

## Entscheidungsregel (Holdout)
- Mindest-Stichprobe: **n ≥ 200** Signale im Holdout. Darunter: „nicht entscheidbar“, kein Schluss.
- Mittlerer Netto-Excess-Return (nach Kosten) **> 0**.
- t-Statistik (Newey-West optional, mindestens naiv) **≥ 2,0**.
- Trimmen: Ergebnis darf nicht nur an den Top/Bottom 5 % Ausreißern hängen (Mittel nach
  symmetrischem 5 %-Trim bleibt > 0).
- Leave-one-out über Symbole: Vorzeichen bleibt, wenn ein beliebiges Symbol entfernt wird.

**Erfolg = alle Kriterien erfüllt.** Jedes Nicht-Erfüllen = E1 verworfen oder unentschieden.
Kein Nachjustieren von RSI-Schwelle, SMA-Länge, Horizont oder Kosten nach Sicht des Holdouts.

## Was danach passiert
- Erfolg: **Shadow-Modus** (Signal taggen, keine Orders), 30 Tage Live-Beobachtung. Live-Freigabe
  nur durch VoLLi.
- Verworfen oder unentschieden: E1 wird nicht umgesetzt. Nächste Hypothese wird ebenfalls vorregistriert.
