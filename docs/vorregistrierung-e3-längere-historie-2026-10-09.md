# Vorregistrierung E3 — RSI-Umkehr auf LÄNGERER Historie (2026-10-09)

**Vor dem Test festgelegt.** E1/E2 scheiterten nicht an der Hypothese,
sondern an der **Stichprobengröße**: sie benutzten nur
`data/exit_replay_bars` (~1 Jahr) → E1: 0 Signale (Trendfilter), E2:
n < 200 im Holdout. Phase 1e prüft `data/backtest_cache` (2021-06 →
2026-08) und zeigt: die **gemergte Union-Historie** macht E2-Formel
endgültig entscheidbar. Diese Vorregistrierung legt alles **vor** dem
Test fest; die Entscheidung folgt nur der unten definierten Regel.

## Warum dieses Mal entscheidbar (Befund Phase 1e, deskriptiv)

- **Universum:** Union aus `exit_replay_bars` + `backtest_cache` +
  `ohlcv_daily` = 310 Titel; **237 Titel mit ≥250 gemergten Tageskerzen**
  (251–1.542 Bars).
- **Historie:** `backtest_cache` liefert 2021-06-01 → 2026-08-21
  (~5,25 Jahre); `exit_replay_bars` ergänzt 2025-01 → 2026-09-28.
  Merge auf Datum pro Symbol (dedup, spätere Quelle überschreibt).
- **Signal-Dichte (RSI(14) < 30, max. 1/10-Tage-Fenster pro Symbol):**
  885 Signale gesamt, **319 im 40 % Holdout** → **n ≥ 200 erfüllt** ✓
  (E2 auf ~1 Jahr: < 200; das ist der einzige Grund für den Misserfolg.)

## Hypothese (identisch zu E2)

Eine Aktie mit RSI(14) < 30 erzielt in den folgenden 10 Handelstagen eine
Rendite über dem gleichtägigen Universums-Median (Excess-Return), nach
Kosten. **Kein Trendfilter** (E1-Erfahrung: bei RSI<30 liegt der Kurs
fast immer unter SMA(50) → Trendfilter erzeugt 0 Signale).

## Definitionen (fix)

- **Universum:** 237 Titel mit ≥250 gemergten Tageskerzen (Union, s.o.).
  Symbole mit <250 Bars sind ausgeschlossen (zu wenig RSI-Warmup +
  Forward-Abdeckung).
- **Signal (Tag t):** RSI(14) < 30. Pro Symbol höchstens ein Signal je
  10-Tage-Fenster (keine Überlappung).
- **RSI(14):** Wilder-Glättung, wie in `signal_forward_returns.py`
  (identische Implementierung, keine Neukalkulation).
- **Zielgröße:** `Close(t+10)/Close(t) − 1`, minus Median derselben Größe
  über das Universum am Tag t (Excess-Return).
- **Kosten:** **1,1 % Rundenkosten** abgezogen (Flat-Fee $2 Hin+Rück auf
  $200 = 1,0 % + Spread ~0,1 %).
- **Forward-Abdeckung:** nur Signale mit vollständiger 10-Tage-Abdeckung
  in den gemergten Bars zählen (kein Look-ahead, keine Null-Fills).

## Split

- Die **ersten 60 %** der Handelstage des gemergten Universums: nur
  Beschreibung (keine Entscheidung).
- Die **letzten 40 %**: **Holdout** — einzige Entscheidungsgrundlage.
- Split auf dem *gemergten* Zeitachse (2021-06 → 2026-09-28), nicht auf
  den Teilquellen (verhindert, dass die backtest_cache-Quelle als
  ganzes zum Trainingsfenster wird).

## Entscheidungsregel (Holdout) — identisch zu E1/E2

1. **n ≥ 200** Signale im Holdout. (Prognose: 319 → erfüllt.)
2. Mittlerer Netto-Excess-Return (nach 1,1 % Kosten) **> 0**.
3. t-Statistik (naiv) **≥ 2,0**.
4. Symmetrisch **5 %-getrimmter** Mittelwert **> 0** (Robustheit gegen
   Krypto-Ausreißer).
5. **Leave-one-out** über Symbole: Vorzeichen bleibt, wenn ein beliebiges
   Symbol entfernt wird (kein einzelnes Symbol trägt das Ergebnis).

**Erfolg = alle fünf Kriterien.** Jedes Nicht-Erfüllen = E3 verworfen
oder unentschieden. **Kein Nachjustieren** von RSI-Schwelle (30),
Horizont (10 Tage), Kosten (1,1 %) oder Split (60/40) nach Sicht des
Holdouts.

## Unterschied zu E1/E2 (nur Datenbasis, nicht Methode)

| | E1/E2 | E3 (diese) |
|---|---|---|
| Universum | exit_replay_bars (~1 J) | Union (5,25 J gemerged) |
| Titel ≥250 Bars | 211 (1 J) | 237 (gemerged) |
| RSI<30 Holdout-n | < 200 / 0 | **319** ✓ |
| Hypothese | RSI<30 | RSI<30 (identisch) |
| Entscheidungsregel | 5 Kriterien | 5 Kriterien (identisch) |

## Was danach passiert

- **Erfolg:** Shadow-Modus (Signal taggen, keine Orders), 30 Tage
  Live-Beobachtung. Live-Freigabe nur durch VoLLi.
- **Verworfen/unentschieden:** E3 wird nicht umgesetzt. Die
  Union-Datenbasis bleibt aber als Standard für alle Folge-Hypothesen
  (sie ist strikt besser als ~1 Jahr).

## Grenzen (ehrlich notiert)

- **2024er Lücke:** `backtest_cache` hat 2021-06 → 2023-12 und
  2025-01 → 2026-08; 2024 ist in den Teildateien lückenhaft. Der Merge
  füllt, was vorhanden ist; fehlende Tage fallen aus (konservativ, kein
  Forward-Fill). Das reduziert die effektive Historie leicht unter 5,25 J.
- **Universums-Drift:** 237 Titel heute ≠ 237 Titel 2021 (Survivorship).
  Für die *Relative* Excess-Return-Frage (vs. gleichtägigen Median) ist
  das weniger schädlich als für absolute Renditen — wird im Test-Report
  als Einschränkung benannt.
