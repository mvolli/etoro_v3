# Plan: Marktregime, Verlust-Stufen, $200-Mindestgröße (2026-10-16)

Status: **PLAN — nichts umgesetzt.** Alle Punkte sind geldwirksam, Entscheidung VoLLi.
Grundlage: VoLLi 16.10. „leite das Regime vom Markt ab; Monatslimit NORMAL < 20 %,
CAUTION bis 30 %, CRITICAL über 40 %; CAUTION-Schwellen bereinigen; DEFENSIVE-Conviction
prüfen; $200 je Trade immer anstreben, keine Position darunter schrumpfen; max-trade-size
NORMAL 10 % und abgestuft“. Advisor konsultiert.

**Lesart (Annahme, bitte bestätigen):** Das Regime kommt vom Markt. Der Konto-Drawdown
gegen das 30-Tage-Hoch wird zur gestuften Bremse: < 20 % keine Bremse, 20–30 % CAUTION,
30–40 % DEFENSIVE (*Lückenfüller, von dir nicht genannt*), > 40 % CRITICAL.
**Es gilt das strengere von Markt- und Konto-Stufe.**

---

## 0. HEUTE zu entscheiden: Monatslimit kurz vor Auslösung

Das Monatslimit (12 % gegen 30-Tage-Hoch $9.772,67) löst bei ~$8.600 den Kill-Switch aus.
Equity ~$8.800 → **Puffer ~$200.** Ein Monats-Kill-Switch braucht manuellen Reset.
Plan + Schattenphase dauern Tage.

| Option | Wirkung |
|---|---|
| **A (Empfehlung):** `risk.monthly_loss_limit_pct` jetzt 12 → **20** | Schwelle ~$7.818, Puffer ~$980. Entspricht deiner neuen NORMAL-Grenze, liegt innerhalb der heutigen Schutzgrenze (≤ 25). Reine Config-Änderung. |
| B: belassen | Kill-Switch kann bei ~2 % Rückgang auslösen, Reset von Hand. |

## 1. Marktregime (Kern des Umbaus)

**Gemessen:** SPY lag vom 25.08. bis 15.10. durchgehend **+6 bis +8,5 % über dem
200-Tage-Schnitt**, höchstens **−2,6 %** unter dem 52-Wochen-Hoch. Ein Marktregime hätte
die ganze Zeit **NORMAL** gezeigt — das Konto-Regime stand auf DEFENSIVE.
Daten: SPY lückenlos ab 01.2025 (backtest_cache + exit_replay_bars + ohlcv_daily),
2021–2023 zusätzlich (Bärenmarkt 2022 als Kalibrierfall).

**Vorschlag Marktstufen (SPY, Tagesschluss, mit Hysterese — Werte im Schattenlauf kalibrieren):**

| Stufe | Eintritt | Austritt |
|---|---|---|
| NORMAL | SPY ≥ SMA200 und > −10 % vom 52W-Hoch | — |
| CAUTION | SPY < SMA200 **oder** ≤ −10 % | SPY > SMA200 · 1,01 **und** > −8 % |
| DEFENSIVE | SPY < SMA200 **und** ≤ −15 % | > −12 % |
| CRITICAL | ≤ −25 % | > −20 % |

**Konto-Bremse (deine Stufen) in Dollar, gegen das heutige 30-Tage-Hoch $9.772:**

| Stufe | Drawdown | Equity-Schwelle |
|---|---|---|
| CAUTION | ab 20 % | ~$7.818 |
| DEFENSIVE (Annahme) | ab 30 % | ~$6.841 |
| CRITICAL (keine Käufe) | ab 40 % | ~$5.864 |
| *zum Vergleich heute* | *Kill-Switch 12 %* | *~$8.600* |

Hysterese je Stufe: Austritt 2 Pp unter Eintritt.

**Ehrlich zum Risiko:** CRITICAL erst ab 40 % heißt: bis ~$3.900 Verlust gegen das
30-Tage-Hoch, bevor Käufe stoppen. Das überschreitet die im Code dokumentierten
Schutzgrenzen (`BIBLE_HARD_LIMITS`: `regime.critical_pct` ≤ 25, `risk.monthly_loss_limit_pct`
≤ 25). Diese Grenzen binden **nur die LLM**, nicht die Config — technisch also möglich.

**LLM-Rückfall verhindern:** `regime.*` und `risk.*_loss_limit_pct` stehen heute auf der
Liste, die die LLM-Selbstoptimierung **autonom ändern darf**. Sie würde Werte außerhalb ihres
Rahmens zurückziehen. → Diese Keys aus `BIBLE_HARD_LIMITS` nehmen (dann nicht mehr
LLM-änderbar) oder die Grenzen anheben. Empfehlung: herausnehmen.

**Tag (5 %) und Woche (8 %)** bleiben unverändert als Kill-Switch-Notbremse, bis du anders
entscheidest. Der **Monats-Kill-Switch** entfällt zugunsten der Konto-Stufen (die heben
sich bei Erholung selbst auf, kein manueller Reset).

## 2. Was „Markt = NORMAL“ ab Tag 1 einschaltet

| Wirkung | heute (DEFENSIVE) | Markt NORMAL | Messung / Bewertung |
|---|---|---|---|
| Gewinnleiter (Teilverkäufe) | unterdrückt | aktiv | 184 Teilverkäufe seit 10.09., Ø $38; Gebühr je Teilverkauf offen (fee_probe) |
| Mindest-Conviction | MEDIUM | LOW | wirkungslos: seit 21.08. sind alle Signale MEDIUM |
| Größenfaktor | 0,50 | 1,00 | bei $200-Floor meist ohne Wirkung (s. §5) |
| CORE_SWEEP | D4: max. 3 offen, 0,25× | volle Größe, kein Cap | Broker brutto −$118 seit 10.09. |
| Dip-Käufe | 0,25× ohne Trendbestätigung | voll | Edge-Gate blockt die Familien weiterhin |
| Index-Kern | Tranche ≤ 5 % | ≤ 10 % | — |

Der Konto-Bremse ist zu verdanken, dass Gebührenverluste gedeckelt wurden. Das Edge-Gate
blockt negative Familien weiter; der Umstieg **bringt keinen Edge**, er ändert vor allem
Teilverkäufe, Sweep-Größe und Tradegröße.

**Vorschlag:** Gewinnleiter-Unterdrückung (`SUPPRESS_LADDER_REGIMES`) und den D4-Sweep-Cap
**vom Regime entkoppeln** und als eigene Schalter führen (Default = heutiges Verhalten:
Leiter aus, Sweep-Cap 3). Dann ändert der Regimewechsel nur, was er ändern soll.

## 3. Schattenphase (1–2 Wochen, vor dem Umschalten)

- Marktregime je Risk-Worker-Lauf berechnen und **neben** dem heutigen loggen, ohne Wirkung.
- Gemessen wird nicht das Etikett (das unterscheidet sich täglich), sondern **was anders
  passiert wäre**: Anzahl Teilverkäufe, zusätzliche Sweep-Käufe, Tradegrößen, Orders und
  geschätzte Gebühren.
- Rückblick 2021–2023 (inkl. 2022): Wie oft hätte das Marktregime die Stufe gewechselt?
  Ziel: kein Flattern (Hysterese prüfen).

## 4. CAUTION-Schwellen bereinigen

Heute: Eintritt 3,0 % (Kommentar sagt 4 %), Austritt 3,5 % → der Austritt wirkt nie.
Mit dem Marktregime entfällt die Konto-CAUTION bei 3 % ganz; **nicht nachjustieren,
sondern ablösen**. Beide neuen Systeme bekommen saubere Hysterese (Austritt < Eintritt).

## 5. $200-Mindestgröße und maximale Tradegröße

**Regel:** Jeder Kauf ≥ $200. Das Regime begrenzt nur nach oben:
`cap = max(max_trade_pct × Equity, $200)`, **in allen Regimen** durchgesetzt
(heute nur in DEFENSIVE; NORMAL 10 % / CAUTION 7,5 % sind wirkungslos).
Stufen bleiben: NORMAL 10 %, CAUTION 7,5 %, DEFENSIVE 5 %, CRITICAL 2,5 %.

Gemessen: seit 10.09. **kein** Kauf über 10 % der Equity; seit 09.10. **2** Hochrundungen
($98/$100 → $200). Der Floor hebt den Größenfaktor bei kleinen Signalen ohnehin auf.
CRITICAL 2,5 % läge unter $8.000 Equity unter $200 — irrelevant, weil CRITICAL nicht kauft.

**„Keine Position unter $200 schrumpfen“ betrifft auch Teilverkäufe.** Da alle neuen
Käufe $200 groß sind, hieße eine Rest-Untergrenze von $200: **keine Teilverkäufe auf neuen
Positionen.** Zwei Varianten — Entscheidung VoLLi:

| Variante | Verhalten | Bewertung |
|---|---|---|
| **Halten (Empfehlung)** | Teilverkauf wird übersprungen, Position läuft bis Trailing/BE/SL/Vollausstieg | konsistent mit `min_tranche_usd` und fee_probe |
| Voll verkaufen | bestehender Pfad `would_breach_min_remaining → FULL_EXIT`: erste Leiterstufe (~+5 %) wird Vollverkauf | spürbare Änderung des Ausstiegsverhaltens |

Schalter: `partial_close.min_remaining_usd` (Default 0 = aus). Bestehende Positionen unter
$200 (die meisten der 12 offenen) werden nicht angefasst.

**Buchungsfehler nebenbei:** `trades.amount_usd` behält den Betrag vor dem Hochrunden
(CDA.PA $97,64, beim Broker $200). Execution soll den Betrag nach dem Floor zurückschreiben.

## 6. DEFENSIVE-Conviction

- Seit `4d6e916` (21.08.) erzeugt der Generator **nur noch MEDIUM** (Begründung damals:
  HIGH war die schlechteste Stufe, 78 Trades, WR 28 %, −$268). Seit 10.09. waren alle
  291 Signal-Trades MEDIUM (Broker brutto −$153, davon in DEFENSIVE 93 Trades, −$30).
- Damit unterscheidet die Stufe nichts: **HIGH = null Signal-Käufe, MEDIUM = alle.**
- Unter einem Marktregime heißt DEFENSIVE „Bärenmarkt“ — dafür gibt es in den Daten seit
  dem Neustart keinen einzigen Fall.
- **Empfehlung:** MEDIUM behalten, das Edge-Gate ist der eigentliche Filter. Texte
  korrigieren, die „nur HIGH+“ behaupten: `regime.py` (Beschreibung), `risk.check_regime_gate`,
  Reconciler-Embed.

## 7. Reihenfolge und Zuständigkeit

| # | Schritt | Schalter / Default | Entscheidung |
|---|---|---|---|
| 0 | Monatslimit 12 → 20 % (sofort) | `risk.monthly_loss_limit_pct` | **VoLLi, heute** |
| 1 | Marktregime im Schatten + Rückblick 2021–23 | `regime.source: account` | Bau: Agent |
| 2 | Leiter-Unterdrückung + Sweep-Cap vom Regime entkoppeln | eigene Schalter, Default heute | VoLLi |
| 3 | `max_trade_pct` in allen Regimen, `cap ≥ $200` | — | VoLLi |
| 4 | Rest-Untergrenze $200 (Halten vs. Vollverkauf) | `partial_close.min_remaining_usd: 0` | VoLLi |
| 5 | Regime-/Limit-Keys aus `BIBLE_HARD_LIMITS` | — | VoLLi |
| 6 | Umschalten auf `regime.source: market` + Konto-Stufen 20/30/40 | nach Schattenphase | VoLLi |
| 7 | Texte DEFENSIVE-Conviction, Buchungsfehler `amount_usd` | — | Agent |

## Weiter offen

- **Index-Kern:** Startwerte (Zielgröße, ETF, Stop, Schatten?) stehen aus; `mode: "off"`.
- **fee_probe:** läuft seit 11:56, bisher kein Kauf/Verkauf gemessen.
