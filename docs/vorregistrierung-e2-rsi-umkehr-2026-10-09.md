# Vorregistrierung E2 — Kurzfrist-Umkehr nach Überverkauftheit (2026-10-09)

**Vor dem Test festgelegt.** Entstanden, weil E1 (`vorregistrierung-e1-rsi-uptrend-2026-10-09.md`)
in den Daten **0 Signale** erzeugt: bei RSI(14) < 30 liegt der Kurs immer unter SMA(50)
(Maximum -2,5 %, Median -14 %). E1 ist damit formal **nicht entscheidbar** (n < 200).
Ein Trendfilter ist für das Signal also ungeeignet. E2 verzichtet darauf.
Die Entscheidung über E2 erfolgt nur nach dem unten festgelegten Test, nicht nach Sicht von E1.

## Hypothese
Eine Aktie mit RSI(14) < 30 erzielt in den folgenden 10 Handelstagen eine Rendite über dem
gleichtägigen Universums-Median (Excess-Return), nach Kosten.

## Definitionen (identisch zu E1, außer dem Signal)
- Universum: `data/exit_replay_bars/*.csv`, mindestens 250 Tageskerzen (211 Titel).
- Signal (Tag t): RSI(14) < 30. Pro Symbol höchstens ein Signal je 10-Tage-Fenster.
- Zielgröße: Close(t+10)/Close(t) − 1, minus Median derselben Größe über das Universum am Tag t.
- Kosten: **1,1 % Rundenkosten** abgezogen.

## Split
- Die ersten 60 % der Handelstage: nur Beschreibung. Die letzten 40 %: Holdout, einzige Entscheidungsgrundlage.

## Entscheidungsregel (Holdout)
- **n ≥ 200** Signale. Darunter: nicht entscheidbar.
- Mittlerer Netto-Excess-Return **> 0**.
- t-Statistik **≥ 2,0** (naiv).
- Symmetrisch 5 %-getrimmter Mittelwert **> 0**.
- Leave-one-out über Symbole: Vorzeichen bleibt, ein beliebiges Symbol entfernt.

Erfolg nur bei allen fünf Kriterien. Keine Nachjustierung von Schwelle (30), Horizont (10 Tage) oder Kosten.

## Danach
- Erfolg: Shadow-Modus, 30 Tage Live-Beobachtung. Live-Freigabe nur durch VoLLi.
- Sonst: E2 wird nicht umgesetzt.
