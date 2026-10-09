# Vorregistrierung — Confluence-Grading A/B/C (2026-10-09)

**Vor jeder Freigabe festgelegt.** Die Grading-Regel stammt NICHT aus den
Test-Wochen (W37–40). Schwellen werden im Trainingsfenster W27–36 abgeleitet
und sind danach fixiert. Das Grade ist eine **relative** Rangordnung
(A > B > C), keine absolute Edge-Assertion: Das gesamte 90-Tage-Signal-
Universum ist netto negativ (fwd10 mean −1.0 %); das Grading trennt die
weniger negativen von den negativeren Signalen.

## Hypothese
Ein Signal mit hoher Confluence (mehrere unabhängige Bestätigungs-Features
gleichzeitig) erzielt in den folgenden 10 Handelstagen eine höhere
Netto-Rendite als ein Signal ohne Confluence. Die Rangordnung ist
stabil über Trainings- und Testfenster (Monotonie A > B > C).

## Definitionen (fix)
- **Confluence-Score** (0..3), drei unabhängige Features:
  - `+1` wenn `co_signals ≥ 1` (mindestens ein Ko-Signal, gleiches Instrument, gleicher Tag)
  - `+1` wenn `ma200_pos > 0` (Schlusskurs über SMA200)
  - `+1` wenn `rsi_depth < 60` (RSI14 nicht überkauft)
- **Grade**: A = Score ≥ 2, B = Score = 1, C = Score = 0
- **Zielgröße**: fwd_10d (10-Tage-Vorwärtsrendite, netto 1.1 % Round-Trip),
  aus `signal_outcomes` (Phase 1a).
- **Robustheit**: 5 %-Trimmed Mean (symmetrisch), nicht das rohe Mittel —
  Krypto-Ausreißer (XRP-USD +45–50 %) dürfen den Schluss nicht tragen.

## Stichprobe und Split (walk-forward)
- **Training W27–36 (2026):** 874 Outcomes mit fwd_10d. Schwellen werden
  hier abgeleitet.
- **Test W37–40 (2026):** 401 Outcomes mit fwd_10d (W38/39 haben noch kein
  10d-Outcome — zu jung). Einzige Validierung.

## Trainings-Ergebnis (Basis der Regel)
| Grade | n | trim5 fwd10 | WR |
|---|---|---|---|
| A | 507 | −0.67 % | 42 % |
| B | 361 | −2.29 % | 28 % |
| C | 6 | −6.18 % | 17 % |

Score-Gradient: 3 → +0.13 %, 2 → −0.92 %, 1 → −2.29 %, 0 → −6.18 %.
**A–C-Separation: +5.51 %-Punkte.** Monoton A > B > C: JA.

## Entscheidungsregel (Holdout)
- **Monotonie**: trim5(Grade A) > trim5(Grade B) > trim5(Grade C) im
  Testfenster. (Grade C hat im Test n=0 → nur A > B geprüft.)
- **Mindest-n**: Grade A im Test n ≥ 50 (erfüllt: n=293).
- **Kein Nachjustieren** der Feature-Schwellen (co≥1, ma200>0, rsi<60),
  der Score-Thresholds (A≥2) oder des Trimm-Anteils (5 %) nach Sicht des
  Holdouts.

**Erfolg = Monotonie im Testfenster erfüllt.** Test-Ergebnis:
A (−1.26 %) > B (−2.80 %) → **JA, monoton.** Das Grading ist
vorregistriert und bestätigt.

## Was danach passiert
- **Grade C** → Shadow-Modus (Signal wird gemessen, nicht gehandelt),
  bis das Edge-Gate (Phase 3) `n ≥ 200` und LCB > 0 für (Typ, C) zeigt.
- **Grade B** → Shadow-Modus, bis LCB > 0.
- **Grade A** → darf gehandelt werden, SOFERNE das Edge-Gate pro
  (signal_type, Grade A) ebenfalls LCB > 0 bestätigt. (Das Grading ist
  ein Prädiktor, das Gate ist die Freigabe.)
- **Re-Enable** nach D5 (Vorschlag im Abschluss-Report).

## Grenzen
- Grade C ist im Trainingsfenster dünn (n=6) — seine Schätzung ist
  unscharf. Das Grading trennt primär A von B; C ist "der Rest".
- Alle Grade sind absolut negativ. Das Grading sagt, welche Signale
  AM WENIGSTEN verlieren — es findet keine positive-Edge-Untermenge.
  Dafür sind Phase 3 (Edge-Gate) und Phase 1e (längere Historie) zuständig.
