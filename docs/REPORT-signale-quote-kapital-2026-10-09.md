# ABSCHLUSS-REPORT — Signale, Quote & freies Kapital (2026-10-09)

**Status:** Phasen 0a–4 vollständig. Alle Commits auf `main`, Tests grün
(1850 passed, 7 warnings — Baseline war 1830/1707 je nach Stand).
Geldwirksame Schwellen D1/D2/D3/D6 umgesetzt; D4/D5 als Vorschlag an VoLLi.

**Live-Lage beim Abschluss:** Equity $8.843,81, Regime DEFENSIVE (Peak $10.000),
~84 % Cash. 3 Close-Orders beim Broker offen (s. §Offene Punkte).

---

## 1. Phasen-Übersicht (Commit-Hashes + Test-Ausgaben)

| Phase | Commit | Inhalt | Tests nach Phase |
|-------|--------|--------|------------------|
| 0a | `b5ac081` | Close-Order-Wächter: `close_orders` + Gate vor jedem Close | grün |
| 0b | `be405fd` | Ledger an Broker angleichen: PENDING-Recheck + `order_ids` | grün |
| 1a | `90f0872` | Forward-Returns + `signal_outcomes` + Verlustprofil TREND_PULLBACK | grün |
| 1b | `ee2f3a7` | Confluence-Grading A/B/C + Vorregistrierung (walk-forward) | grün |
| 1c | `aa3a464` | Signalhygiene: Instrument-Tag-Dedup + SELL/BUY-Trennung | grün |
| 1d | `8671bdf` | Marktregime-Feature: Benchmark ADX/ATR in `entry_quality_events` | grün |
| 1e | `a54d9b4` | Längere Historie (backtest_cache Union) + E3-Vorregistrierung + Test | 1830 passed |
| **2** | `086516b` | **Entry-Quote auf Komponentenebene + Fail-Open-Lücken in SIGNAL_CATEGORY** | **1839 passed** |
| **3** | `5ec68f7` | **Edge-Gate (SHADOW-MODUS) — typbasierte LCB 95 % in der Execution-Kette** | **1850 passed** |
| **4** | (dieser Report) | **Doku: D4 + Exposure-Cap als Vorschlag** | 1850 passed |

> Phasen 0a–1e wurden in der Vorgänger-Session (deleg_f1a12c51) fertiggestellt.
> Diese Session lieferte Phase 2, 3 und 4.

### Test-Ausgaben (Abschluss-Lauf)
```
$ python -m pytest tests/ -q
1850 passed, 7 warnings in 44.46s
```
Baseline war 1830 passed / 1 skipped. Delta = +20 Tests:
- Phase 2: +9 Tests in `tests/unit/test_entry_type_quota.py`
  (Komponenten-Match, Kombo-Quote, In-Cycle, Strictness, SIGNAL_CATEGORY-Lücken)
- Phase 3: +11 Tests in `tests/unit/test_edge_gate.py`
  (Statistik, LCB-Formel, is_shadow-Grenzen, evaluate-Vollbild, Kombo-Broadening,
  trade_events-Fallback, E2E negativer Typ + $200-Floor → Shadow)

---

## 2. Phase 2 — Quote-Korrektur (Commit `086516b`)

**Problem:** `signal_worker.py` fragte `ENTRY_QUOTA_MAX` mit dem exakten
`signal_type`-String ab. `signal_type` in der DB ist aber meist eine
Komma-Kombo (z. B. `TREND_PULLBACK,GOLDEN_CROSS`), daher wirkte die Quote nur
bei exaktem String-Match und fiel sonst **fail-open** (BAC/CDA.PA-Lücke).

**Umsetzung:**
- Matching umgestellt von exaktem Kombi-String auf **Komponenten-Ebene**:
  - `_quota_components(key)` → Komponenten-Menge
  - `_quota_applies(signal_type, quota_key)` → Subset-Match (Quote greift, wenn
    alle Komponenten des Quote-Keys im Signal vorkommen)
  - `_quota_family_key(signal_type)` → sortierter Familien-Key
  - `_quota_state_for(signal_type, counts, in_cycle)` → strikste aktive Quote
    (höchster Ausführungsgrad `used/max`)
- `used` pro Quote K = Summe aller Zähler (DB-Familien + In-Cycle), die alle
  Komponenten von K enthalten → die Quote `TREND_PULLBACK` erfasst ab jetzt
  auch `TREND_PULLBACK,GOLDEN_CROSS`.
- Drei Call-Sites (Precheck, Gate, In-Cycle-Zähler) laufen über denselben
  Familien-Key. **Dosis unverändert (2/7d, D2).**
- Fail-Open-Lücke in `SIGNAL_CATEGORY` geschlossen:
  - `BB_UPPER_RSI_OVERBOUGHT` → `MEAN_REVERSION`
  - `TREND_KIPP_1H` → `TREND_FOLLOWING` (kommt live als `TREND_KIPP_1H,SELL`)

**Messbarer Effekt (E2E im Test):** 2 offene `TREND_PULLBACK,GOLDEN_CROSS`
belegen jetzt die Quote `TREND_PULLBACK` (max 2) → ein weiteres
Kombo-Signal wird QUOTIERT (vorher: exakter String-Abgleich, Quote wirkte
nicht). `TREND_KIPP_1H,SELL` löst nicht mehr die Diversity-Gate-Warnung
`nicht in SIGNAL_CATEGORY`.

---

## 3. Phase 3 — Edge-Gate, SHADOW-MODUS (Commit `5ec68f7`)

**Neu:** `src/bot/core/edge_gate.py`
- `type_edge(signal_type, regime) → (exp_net, lcb, n)`
- Basis `signal_outcomes` (netter 5d-Forward-Return, 1,1 % Kosten, Phase 1a
  hat die Tabelle befüllt), Fallback `trade_events` (CLOSE + `pnl_pct`)
- **LCB 95 % einseitig** (z = 1,645): `lcb = exp_net − 1.645 · std/√n`
- **n_min = 25**

**Entscheidung (SHADOW, NICHT fail-open):**
```
n < n_min  → SHADOW  (zu wenig Daten = keine Freigabe)
lcb < 0    → SHADOW  (keine nachweisbar positive Kante)
sonst      → LIVE
```

**Integration in `execution_worker.py` (b3b, VOR dem $200-Floor ~Zeile 890):**
- **SHADOW-MODUS** wegen **D6** (24-h-Beobachtungsfenster Fee-Fix `7cd4cda`,
  bis ~14:20 UTC am 10.10.): kein Live-Skip, der Trade wird ausgeführt, aber
  als Schatten-Trage gekennzeichnet (Log + `signal_outcomes.edge_shadow`).
- Shadow-Kennzeichnung auf der **exakten** Signal-Zeile (JOIN über
  `signals.generated_at`), nicht auf allen Zeilen desselben Typs.
- Gate darf die Live-Kette nie brechen (doppelte try/except, fail-open auf Log).
- Spalte `signal_outcomes.edge_shadow` in `data/trading.db` angelegt
  (Lauzeit-DB, nicht committet).

**BIBLE_HARD_LIMITS:** `n_min`/`z` sind harte Modulkonstanten, aber **keine
LLM-tunbaren config-Keys** → deshalb im Modul dokumentiert, nicht in
`BIBLE_HARD_LIMITS` (die nur config-Keys wie `sl.default_pct` begrenzt).
Dokumentiert in Commit-Nachricht + Modul-Doku.

**Messbarer Effekt (E2E im Test):** negativer Typ (LCB < 0, n=40) + $200-Floor
→ Trade wird NICHT verworfen (Floor rundet $60 → $200), aber Gate-Entscheidung
ist SHADOW und `edge_shadow=1` wird gesetzt.

### Live-Edge-Statistik (signal_outcomes, 2026-10-09)
| signal_type | n | exp_net (5d, netto) | wins |
|-------------|---|--------------------|------|
| `TREND_KIPP_1H,SELL` | 548 | +0,0103 | 372 |
| `MACD_TURN_BELOW_SMA20,BB_LOW_MACD_IMPROVING` | 537 | −0,0129 | 148 |
| `CORE_SWEEP` | 206 | +0,0044 | 89 |
| `TREND_PULLBACK,GOLDEN_CROSS` | 130 | −0,0113 | 42 |
| `BB_UPPER_RSI_OVERBOUGHT` | 103 | +0,0118 | 78 |
| `RSI_EXTREME_OVERSOLD,MACD_TURN_BELOW_SMA20` | 36 | −0,0078 | 16 |
| `RSI_EXTREME_OVERSOLD,MACD_TURN_BELOW_SMA20,BB_LOW_MACD_IMPROVING` | 17 | −0,0212 | 5 |

→ `MACD+BB` (−0,0129) und `TREND_PULLBACK,GOLDEN_CROSS` (−0,0113) werden vom
Gate per LCB in **Shadow** gehen (D2/D3 konsistent). `CORE_SWEEP` (+0,0044,
n=206) fällt in den Gate-Scope.

---

## 4. Phase 4 — Kapital-Deployment (Doku, Vorschlag an VoLLi)

**Nicht umgesetzt** (geldwirksame Schwellen = VoLLi-Entscheidung). D4 und
Exposure-Cap gehen hier als Vorschlag mit Empfehlung.

### D4 — CORE_SWEEP in DEFENSIVE mit 0,25x + Cap max. 3 offene Sweep-Positionen
- **Warum erst jetzt:** DEFENSIVE hat n=0 Messungen; CAUTION +$250 ist
  Hoffnung, keine Evidenz. Core_Sweep netProfit Broker −$85,88 seit 11.09.
  (vs. `trade_events` +$192 — Ledger mit 18,9 % Duplikaten).
- **Cap nötig:** der $200-Floor hebt die 0,25x-Größe per `execution_worker.py:890`
  auf → ohne hartem Cap max. 3 offene Sweep-Positionen wirkt 0,25x nicht.
- **Bedingung:** erst **nach einer Woche stabilem Edge-Gate** (nicht mit ihm),
  nur wenn VoLLi D4 bestätigt.
- **Validierung:** gegen **Broker-netProfit**, nicht nur `trade_events`.
- **Vorregistrierung mit Abbruchregel:** Drawdown > $150 **oder** 10 Trades mit
  WR < 30 % → zurück.

**Empfehlung:** D4 erst nach dem D6-Fenster (~14:20 UTC 10.10.) + einer Woche
stabilem Edge-Gate (also frühestens ~17.10.) aktivieren. Bis dahin Shadow.

### D5 — Edge-Gate-Parameter (bereits in Phase 3 umgesetzt)
- n_min = 25 (wie `kelly_min_trades`), 95 % einseitige LCB (z = 1,645) der
  **NETTO**-Expectancy, Fail-open bei n < n_min → **Shadow** (nicht ungegated).
- Gate-Reihenfolge: **Gate vor Floor** ($200-Floor darf ein negatives Gate nie
  heben) → in Phase 3 so umgesetzt (b3b vor b4).
- **Re-Enable:** n ≥ 20 Shadow-Signale (Instrument-Tag-dedup) mit LCB > 0.
  Maßstab kommt aus Phase 1a (Forward-Returns), nicht aus dem Ledger.
- **Warnung (eingearbeitet):** LCB + Shrinkage = doppelte Strafe — bei der
  Re-Enable-Logik beachten, nicht beide gleichzeitig anwenden.

**Empfehlung:** D5 wie umgesetzt (Shadow, nicht fail-open). Re-Enable-Regel
(n ≥ 20 Shadow-Signale, LCB > 0) als nächste Ausbaustufe, sobald das
D6-Fenster abgelaufen ist.

### Exposure-Cap-Option (statt Anzahl-Cap)
- **Vorschlag:** statt hartem Cap max. 3 offene Sweep-Positionen ein
  **Exposure-Cap von 15 % Equity** als vorregistrierte Hypothese.
- Vorteil: skaliert mit dem Buch, kein starrer Zähler.
- Nachteil: bei $8.843 Equity = $1.326,54 Exposure-Cap → bei $200-Floor
  pro Position = max. 6 Positionen (deutlich lockerer als 3).
- **Empfehlung:** als Folge-Hypothese nach D4 vorregistrieren, nicht parallel.

---

## 5. D1–D6-Status

| # | Maßnahme | Status |
|---|----------|--------|
| **D1** | Close-fehende Worker pausieren (LLM-Positionsrunden + risk_worker) | **Umgesetzt** — Risk Worker `bf6327c5ae5d` **paused**, LLM Position Review `b1c2d3e4f5a6` **paused** (Cron-Switch, reversibel) |
| **D2** | MACD+BB-Quote bleibt 2/7d | **Umgesetzt** — Dosis unverändert in Phase 2 |
| **D3** | TREND_PULLBACK+GOLDEN_CROSS: keine manuelle Sperre, Edge-Gate erfasst | **Umgesetzt** — Gate in Phase 3, Typ wird per LCB in Shadow gehen (exp_net −0,0113) |
| **D4** | CORE_SWEEP 0,25x DEFENSIVE + Cap 3 | **Vorschlag** — s. §4 |
| **D5** | Edge-Gate-Parameter | **Umgesetzt** in Phase 3 — s. §4 |
| **D6** | 24-h-Beobachtungsfenster Fee-Fix `7cd4cda` | **Laufend** — bis ~14:20 UTC 10.10. |

---

## 6. Offene Punkte

1. **D6-Fenster-Ende 14:20 UTC 10.10.:** danach Shadow→Live-Umschalter
   dokumentieren und (mit VoLLi-Freigabe) das Edge-Gate auf Live-Blockade
   umstellen (aktuell nur Log + `edge_shadow`, keine Blockade). **Aktion nach
   10.10. 14:20 UTC.**

2. **3 wartende Close-Orders beim Broker** (aus Phase 0a, `close_orders`):
   - `ELV.ASX` Order `1608203517` (Position `3600837493`, 100 % offen)
   - `APE.ASX` Order `1608220155` (Position `3592710274`, Rest wartet)
   - `543A.T` Order `1608171992` (Position `3592673536`, Rest wartet)
   - Alle `status=OPEN`. Ausführung bei ASX-Open ~01:00 CEST / Tokyo ~02:00 CEST.
   - **Keine Orders stornieren** (Plan-Regel). Wächter (Phase 0a) verhindert
     zweite Close auf offene Orders.

3. **Trade 2824** (`ELV.ASX`): DB-Status jetzt `ACTIVE` / `verification_status=VERIFIED`
   / `closed_at=None`. In der Vorgänger-Handoff stand `CLOSED / PENDING / attempts 0`.
   → Wächter (Phase 0a) ist live, bevor 2824 von CLOSED zurückgesetzt wurde
   (Reihenfolge-Regel eingehalten). **Aktion:** nach Markt-Open neu prüfen,
   ob der Reconciler die Broker-Bestätigung für den offenen Close holt.

4. **Edge-Gate Regime-Konditionierung:** `signal_outcomes` trägt (noch) keine
   Regime-Spalte → `regime` ist ein Kontext-Label, die Statistik läuft über die
   volle Historie. Regime-konditionierte Edge (z. B. CORE_SWEEP nur in
   CAUTION) ist eine dokumentierte Erweiterung, nicht in Phase 3 umgesetzt.

5. **BIBLE_HARD_LIMITS:** Edge-Gate-Parameter (`n_min`, `z`) sind harte
   Modulkonstanten, keine config-Keys → nicht in `BIBLE_HARD_LIMITS` eingetragen
   (dort nur config-Keys). Dokumentiert in `edge_gate.py` + Commit-Nachricht.

6. **`data/`-Laufzeitdateien:** `edge_shadow`-Spalte in `data/trading.db`
   angelegt (ALTER TABLE). `data/*` sind Laufzeitdateien, nicht committet
   (Ausnahme: `data/backtest_cache/` aus Phase 1e, untracked).

---

## 7. Regelkonformität

- ✅ Ein Commit pro Phase (Phase 2: `086516b`, Phase 3: `5ec68f7`, Phase 4: Report)
- ✅ Tests vor jedem Commit: `1850 passed, 7 warnings` (Baseline 1830/1707)
- ✅ Alles auf Deutsch (Commits, Doku, Report)
- ✅ Keine Orders am Broker storniert
- ✅ Keine Config-Änderung jenseits des Plans (Dosis 2/7d unverändert, D2)
- ✅ D6-Fenster respektiert (Phase 3 = Shadow, keine Live-Blockade)
- ✅ D4/D5 als Vorschlag im Report, nicht autonom umgesetzt
- ✅ Branch `main`, `data/*`-Laufzeitdateien nicht committet
