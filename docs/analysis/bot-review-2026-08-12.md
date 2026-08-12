# Bot-Review etoro_v3 — 10 Analystenfragen, angewandt auf das eigene System

**Datenstand:** 2026-08-12, 10:32 UTC · **Quelle:** `data/trading.db` (read-only) + Quellcode
**Charakter:** reine Lese-Analyse. Kein Code, keine Config, kein Cron wurde geändert. Alle
Vorschläge am Ende sind Vorschläge — der Bot läuft unverändert weiter.

---

## Executive Summary

**Der Bot hat kein Pech, er hat einen negativen Erwartungswert.**

| | |
|---|---|
| Trefferquote | **23,6 %** (52 Wins / 220 geschlossene Trades) |
| Ø Gewinn / Ø Verlust | **+3,49 % / −1,74 %** |
| **Nötige Trefferquote für Breakeven** | **33,3 %** |
| Erwartungswert | **−0,50 % bzw. −$1,88 je Trade** |
| Realisiert gesamt | **−$414** |
| Equity | $8.667,85 (Start $10.000 am 2026-06-24) = **−13,3 %** |

Die Lücke von 9,7 Prozentpunkten zwischen tatsächlicher (23,6 %) und nötiger (33,3 %)
Trefferquote ist die eigentliche Diagnose. Sie ist **nicht** durch bessere Einstiege zu
schließen — die Verlustseite ist bereits ausgezeichnet kontrolliert (Ø Verlust nur
−1,74 %). Sie ist durch die **Gewinnseite** zu schließen, und die ist strukturell
gedeckelt:

> **Die erste Profit-Stufe liegt im Median bei +17,0 % (39 von 59 Positionen ≥ 15 %) —
> der durchschnittliche Gewinn-Trade bei +3,49 %.** Die Profit-Leiter feuert faktisch
> nie. Beendet werden Gewinner ausschließlich von Break-Even-Stop (+0,3 %) und
> Momentum-Fade. Zwischen +3,5 % und +17 % existiert kein Mechanismus, der einen
> Gewinner am Leben hält. (Abschnitt 3)

**Dazu kommt ein struktureller Befund, der schwerer wiegt als jede Parameterfrage:**
Der Pfad, der 41 der 57 offenen Trades eröffnet hat — **Core-Sweep** — durchläuft
**keines** der Risiko-Gates. Kein Exposure-Gate, kein Asset-Class-Gate, kein
Korrelations-Gate. Belegt an einem einzigen Log-Zeitstempel (Abschnitt 2).

### Zwei Caveats, die für jede Zeitreihe in diesem Dokument gelten

1. **45 der 220 geschlossenen Trades haben `closed_at IS NULL`** (2026-07-21 bis
   2026-08-07, 23 Wins, −$25,7). Das ist eine Reconciler-Lücke und zugleich eine
   Unschärfe auf jeder Vorher/Nachher-Betrachtung.
2. **Kohorten-Zensierung.** 50 der 57 offenen Positionen wurden nach dem 2026-07-26
   eröffnet. Vergleiche „vor/nach" sind deshalb **nicht** gleich-gegen-gleich — die
   jüngere Kohorte ist zu ~39 % noch unaufgelöst und trägt +$154 unrealisiert.

---

## 1. Markttrends — in welchen Märkten der Bot tatsächlich steckt

Die Frage nach „Sektor-Trends" ist für diesen Bot falsch adressiert: er wählt nicht
Sektoren, er wählt technische Setups. Was er dabei *faktisch* akkumuliert hat, ist eine
**geografische**, keine sektorale Position.

**Universum:** 4.161 handelbare Instrumente (`is_tradable=1, is_active=1`), 212 als nicht
handelbar markiert. EU-Aktien werden per Chunk-Rotation gescannt (2 Chunks/Lauf,
Vollzyklus ~16 h) gegen eine Signal-TTL von 24 h — d. h. rechnerisch ~8 h Puffer, aber
ein EU-Signal kann beim Kauf bis zu 24 h alt sein. `_signal_age_factor()`
deprioritisiert das im Ranking, verhindert es aber nicht.

**Reale Positionierung (investiertes Kapital, `portfolio_snapshot`):**

| Region | Positionen | USD | % Equity |
|---|---|---|---|
| EU | 24 | $3.364 | **47 %** |
| ASIA_CN | 10 | $1.556 | 22 % |
| US | 12 | $842 | 12 % |
| GLOBAL (UK u. a.) | 7 | $809 | 11 % |
| ASIA_JP | 5 | $448 | 6 % |
| ASIA_AU | 1 | $80 | 1 % |

**Bewertung.** Fast die Hälfte des Buchs steht in EU-Aktien, ein weiteres Fünftel in
China/Hongkong. Das ist keine getroffene Entscheidung, sondern ein Nebeneffekt: der
US-Markt ist besser abgedeckt und liquider, also finden die Mean-Reversion-Regeln dort
seltener „überverkaufte" Kandidaten, während EU- und Asien-Small-Caps ständig
Extremwerte liefern. **Kein Gate im System misst Region.**

Zur Einordnung der Klumpen — im Folgenden durchgängig so gruppiert: **EU 47 %**,
**Asien gesamt 29 %** (CN 22 + JP 6 + AU 1), **US 12 %**, **UK/Global 11 %**. Ein
EUR-Schock träfe knapp die Hälfte des Buchs auf einen Schlag, eine
China-Regulierungswelle gut ein Fünftel. Nicht-US-Märkte zusammen: **76 %**.

---

## 2. Diversifikation & Konzentrationsrisiko — der Kernabschnitt

### 2a. 🔴 Core-Sweep umgeht sämtliche Risiko-Gates

`plan_core_sweep()` ([core_sweep.py:136](../../src/bot/core/core_sweep.py)) wird in Block 5b
des Signal-Workers ([signal_worker.py:1345–1420](../../src/bot/workers/signal_worker.py))
**nach** der Signalschleife aufgerufen und schreibt direkt `create → APPROVED`.
**Es gibt keinen `check_buy_gate`-Aufruf in diesem Pfad.**

Der Code-Kommentar dort behauptet, Core-Sweep laufe „ueber dieselbe create→APPROVED→
execution-Bahn wie normale Signale (erbt SL-Clamp, Market-Open-Guard,
Ghost-Order-Pipeline)". Das stimmt für den **Execution**-Pfad. Für die **Risiko-Gates**
stimmt es nicht.

**Damit unwirksam für diesen Pfad — verifiziert, bewusst eng gehalten:**

| Gate | Status im Core-Sweep-Pfad |
|---|---|
| **Exposure-Gate (75 %)** | ❌ nicht angewandt |
| **Asset-Class-Gate** | ❌ nicht angewandt |
| **Korrelations-Gate** | ❌ nicht angewandt |
| **Diversity-Gate (45 %/Kategorie)** | ❌ nicht angewandt |
| Regime-Gate | ✅ eigene Prüfung (nur NORMAL/CAUTION) |
| Cash-Floor | ✅ eigene Prüfung (Ziel 15 %, Floor 10 %) |
| Pyramiding | ✅ eigene Prüfung (`held_instrument_ids`) |
| Instrument-Limit | ✅ eigener Deckel 6 % — strenger als der 10 %-Default |
| Min-Buy / SL | ✅ 4 % Equity ≈ $347; SL via `adaptive_sl_pct` |
| Conviction | — entfällt: der Pfad legt ein synthetisches Signal mit `conviction="MEDIUM"` an |

**Der Kern in einem Satz:** Core-Sweep prüft ausschließlich **Cash- und
Einzeltitel-Grenzen**. Es gibt im gesamten Pfad keine einzige Prüfung, die das
**Portfolio als Ganzes** betrachtet — weder Gesamt-Exposure noch Korrelation noch Sektor.

**Der Beweis — ein einziger Worker-Lauf, 2026-08-12 05:19:29:**

```
Signal BLOCKED: ROVI.MC  →  "Exposure-Gate: 79,5 % > 75 % Max"
Trade 1591 2883.HK $345,90  →  via CORE_SWEEP ERÖFFNET
```

Gleicher Zeitstempel, gleicher Lauf. Der Signalpfad wurde korrekt gestoppt, der
Core-Sweep-Pfad kaufte weiter. Heute insgesamt 6 Exposure-Blocks auf dem Signalpfad
(FTK.DE, MEL.MC, ROVI.MC bei 79,5 – 83,4 %) — bei gleichzeitig laufendem Core-Sweep.

**Aufteilung der offenen Trades nach Herkunft:** CORE_SWEEP **41** · Signalpfad **12** ·
ohne Signal 4.

### 2b. Der Drift-Mechanismus — warum Exposure im Drawdown *steigt*

Kontraintuitiv, aber zentral: `amount_usd` ist **eingesetztes Kapital**, nicht Marktwert.
Exposure-% steigt also, wenn **Equity fällt** — nicht wenn Kurse steigen.

```
Start:  $7.100 investiert / $10.000 Equity = 71 %
Heute:  $7.100 investiert / $ 8.668 Equity = 81,9 %
```

`check_exposure_gate` ([risk.py:533](../../src/bot/core/risk.py)) ist ein reines
**Pre-Trade**-Gate; ein Post-Trade-Drift-Monitor existiert nicht. Für Asset-Klassen wurde
genau diese Lücke bereits geschlossen (`check_asset_class_violations`,
[concentration_monitor.py:119](../../src/bot/core/concentration_monitor.py)) — für
Gesamt-Exposure nicht.

Und Core-Sweep sieht ausschließlich *Cash gegen Reserve-Floor*. Cash liegt bei 16,3 % und
bleibt dort, weil geschlossene Positionen Cash zurückspülen. **Also kauft Core-Sweep im
Drawdown weiter, genau dann, wenn Exposure prozentual ohnehin steigt.** Das fehlende
Drift-Monitoring ist damit kein kosmetischer „Drift", sondern ein **Verlust-Verstärker**.

### 2c. 74,2 % des Buchs sind für das Sektor-Gate unsichtbar

`check_asset_class_gate()` ([risk.py:563](../../src/bot/core/risk.py)) macht
`ASSET_CLASS_MAP.get(symbol.upper())` und gibt bei einem Miss **`allowed=True`,
„kein Mapping"** zurück — fail-open. `ASSET_CLASS_MAP` enthält ~65 US-Ticker plus 5
INTL-Einträge. Die Symbole kommen aus `portfolio_snapshot.symbol`, also im
eToro-Namespace (`02196.HK`, `CAR.AX`, `ERIC-B.ST`).

| | Symbole | USD | % Equity |
|---|---|---|---|
| **Mit** Asset-Class-Mapping | 11 | $666 | **7,7 %** |
| **Ohne** Mapping → Gate passiert immer | **43** | **$6.433** | **74,2 %** |

Größter tatsächlich erfasster Block: HEALTHCARE 4,0 % gegen ein 20 %-Limit — das Gate hat
also noch nie etwas geblockt und könnte es beim aktuellen Buch auch nicht.

Verschärfend: **`instruments.sector` ist bei 0 von 15.501 Instrumenten gefüllt.** Es gibt
derzeit gar keine Datenquelle, aus der ein echtes Sektor-Limit gespeist werden könnte.

### 2d. Was tatsächlich funktioniert

- **Per-Instrument-Limit:** unkritisch. Größte Einzelposition 4,0 % gegen 10 % Limit.
  Das Konzentrationsrisiko liegt nachweislich **nicht** auf Einzelwert-Ebene.
- **Korrelations-Gate** ([correlation.py:17](../../src/bot/core/correlation.py)): Block bei
  r ≥ 0,80, Halbierung der Größe bei 0,60–0,80, 30-Tage-Returns, 4 h Cache. **Das ist der
  einzige echte Diversifikationsschutz im System** — und Core-Sweep umgeht ihn.
- **Diversity-Gate** ([signal_worker.py:1201](../../src/bot/workers/signal_worker.py)):
  max. 45 % der offenen Positionen in einer Signal-**Kategorie** (Mean-Reversion vs.
  Trend-Following). Misst Strategie-Klumpen, nicht Markt-Klumpen — und greift ebenfalls
  nicht für Core-Sweep.

### 2e. Core-Sweep ist kein Blue-Chip-Anker mehr

Ursprünglich war Core-Sweep als Anker in liquiden Blue Chips gedacht (Config-Whitelist:
SPY, AAPL, MSFT, AMZN, NVDA, META, JPM, V, KO, PG, JNJ, WMT). Über
`auto_discovery: true` zieht der Discovery-Worker inzwischen eine **DB-Whitelist mit 24 h
TTL** hinzu — aktuell 9 Einträge: 2883.HK, BKG.L, SAVE.ST, BFG.MI, SMWH.L, UNH, O,
VIRP.PA. Die zuletzt eröffneten Core-Sweep-Trades stammen alle aus dieser DB-Hälfte, nicht
aus der Blue-Chip-Liste.

**Konsequenz:** Core-Sweep ist faktisch ein **zweiter, ungegateter Signalpfad** geworden,
mit größeren Tickets (~4 % Equity ≈ $347) als der reguläre Pfad. Die „Anker"-Begründung,
mit der der Gate-Verzicht ursprünglich vertretbar war, trägt nicht mehr.

---

## 3. Risikomanagement — die Verlustseite ist gut, die Gewinnseite ist der Fehler

### Was vorhanden und wirksam ist

| Mechanismus | Ausprägung |
|---|---|
| Stop-Loss | ATR-adaptiv (Default 3 %, ×1,5 ATR, max 6 %) mit **Risk-Parity-Gegenskalierung** (Faktor-Floor 0,6) — weiterer Stop ⇒ kleinere Position, Dollar-Risiko konstant |
| SL-Kaskade | Warnung −2 %, Hard-Close −3 %, Notfall −4 % (`evaluate_sl`) |
| Break-Even | ab +3 % PnL → SL auf Einstand +0,3 % |
| Position-Sizing | Half-Kelly, geklemmt [0,3 … 1,5], mit Komponenten-Pooling ([sizing.py:12](../../src/bot/core/sizing.py)) |
| Regime-Scalar | NORMAL 1,00 / CAUTION 0,75 / DEFENSIVE 0,50 / CRITICAL 0,25, gesteuert per Drawdown (4/8/15 %) |
| Loss-Limits | täglich 5 %, wöchentlich 8 %, monatlich 12 % (MDD gegen Rolling-Peak) |
| Kill-Switch | `data/kill_switch.flag`, Tages-Scope mit Auto-Clear |

**Der Beweis, dass das funktioniert: Ø Verlust −1,74 %** bei einem 3 %-Stop. Die Stops
greifen früher als sie müssten, Ausreißer nach unten gibt es praktisch nicht. Das ist gut
gebaut.

### 🔴 Warum die Gewinne strukturell zu klein sind — die schärfste Zahl im Dokument

Die Profit-Leiter ist **ATR-skaliert**, sobald `atr_pct` bekannt ist
(`_resolve_profit_levels`, [trailing_stop.py:247](../../src/bot/core/trailing_stop.py)).
Die flache Leiter (+7/+15/+25/+50 %) ist nur der Fallback bei unbekanntem ATR — und
**alle 59 offenen Positionen haben einen ATR-Wert**, die ATR-Leiter ist also durchgängig
aktiv. Erste Stufe = `min(max(6 × ATR%, 6 %), 30 %)`.

Ausgerechnet für das reale Buch (ATR-Spanne 1,07 – 6,22 %, Ø 2,89 %):

| Erste Profit-Stufe | Wert |
|---|---|
| Minimum | 6,4 % |
| **Median** | **17,0 %** |
| Maximum | 30,0 % |
| Positionen mit erster Stufe ≥ 15 % | **39 von 59** |
| Positionen mit erster Stufe ≤ 7 % | 1 von 59 |

**Dem gegenüber steht ein durchschnittlicher Gewinn-Trade von +3,49 %.** Die erste
Profit-Stufe liegt im Median beim **Fünffachen** dessen, was ein Gewinner tatsächlich
erreicht. Die Profit-Leiter feuert für die überwiegende Mehrheit der Positionen faktisch
nie.

Was Gewinner stattdessen beendet (Live-Werte aus `config.yaml`, nicht die
Code-Defaults):
1. **Momentum-Fade:** Peak ≥ +2 %, dann **30 %** davon zurückgegeben → **25 %** der
   Position werden geschlossen, Cap bei 4 Prozentpunkten.
2. **Break-Even-Stop:** ab +3 % rückt der Stop auf Einstand +0,3 %. Jeder normale
   Rücksetzer danach beendet den Trade bei ~null.
3. **Stale-Exit:** ab 10 Tagen im Band |PnL| < 1,5 % ohne je +2 % Peak → Ausstieg.

Das ist der komplette Mechanismus hinter der Asymmetrie: **Verluste werden bei −1,74 %
diszipliniert gekappt — und Gewinner ebenso, nur bei ~+3,5 %.** Die einzigen Ausstiege,
die überhaupt scharf sind, liegen alle im Bereich +0,3 % bis +3 %. Die Leiter, die
größere Gewinne strukturieren soll, beginnt eine Größenordnung darüber. **Zwischen
+3,5 % und +17 % existiert kein Mechanismus, der einen Gewinner am Leben hält.**

Das ist eine **Exit-Frage, keine Entry-Frage.** Jede weitere Verschärfung der
Einstiegs-Filter verschiebt an dieser Rechnung nichts.

---

## 4. Technische Analyse — nicht des Marktes, sondern der eigenen Signale

**Verwendete Indikatoren** (`compute_indicators`,
[signals.py:142](../../src/bot/core/signals.py)): RSI, MACD-Histogramm (inkl. Vorwert für
Wende-Erkennung), Bollinger %B, ATR, SMA20, SMA50, dazu `consecutive_down_days` und
`roc_5d_pct` für das Falling-Knife-Gate. Volumen fließt nur als 20-Tage-Dollar-Volumen
(`adv_usd`) ins Liquiditäts-Tiering ein, **nicht** als Bestätigungssignal. Klassische
Support-/Resistance-Levels werden nicht berechnet.

### Performance je Signalfamilie (geschlossene Trades mit PnL)

| Signaltyp | Trades | Wins | PnL | Bewertung |
|---|---|---|---|---|
| `RSI_EXTREME_OVERSOLD, MACD_TURN_BELOW_SMA20` | 10 | 4 | **+$94,8** | ✅ einzige klar profitable Familie |
| `BB_LOWER…, MACD_TURN…, BB_LOW_MACD_IMPROVING` (4er-Kombo) | 4 | 2 | +$43,5 | ✅ klein, aber positiv |
| `MACD_TURN_BELOW_SMA20, BB_LOW_MACD_IMPROVING` | 28 | 6 | −$64,5 | ⚠️ |
| `BB_LOWER…, BB_EXTREME…, RSI_EXTREME_OVERSOLD` | 37 | **1** | −$90,0 | ❌ reiner Dip-Kauf |
| `TREND_PULLBACK, GOLDEN_CROSS` | 42 | 12 | −$131,1 | ❌ größter Einzelverlust |
| `CORE_SWEEP` | 47 | 13 | −$124,0 | ❌ ungegatet (Abschnitt 2a) |

**Das Muster ist eindeutig:** Reine Überverkauft-Signale ohne Momentum-Bestätigung
verlieren fast immer (1 Win aus 37). Sobald **MACD-Wende** als Bestätigung dazukommt,
dreht das Vorzeichen. Die Lernschleife hat das erkannt — die Combo-Conviction wird
inzwischen aus der *schwächsten* Komponente gebildet, nicht der besten.

### Hat das Falling-Knife-Gate (2026-07-26) gewirkt? Ja.

Die `BB_LOWER*`-Familie hat **55 geschlossene Trades, alle vor dem 2026-07-26**, 6 Wins,
−$42,1. **Seit dem Gate: null neue.** Die schlechteste Signalfamilie wurde vollständig
stillgelegt. Die „1 Win aus 37" in der Tabelle oben ist ein historisches Artefakt, kein
laufendes Problem.

| Ära | Geschl. | Wins | Trefferquote | PnL | Ø % | Ø Größe | $/Trade |
|---|---|---|---|---|---|---|---|
| vor 2026-07-26 | 141 | 29 | 20,6 % | −$275,5 | −0,21 % | $453,0 | −$1,95 |
| ab 2026-07-26 | 79 | 23 | **29,1 %** | −$138,6 | −1,03 % | $158,7 | −$1,75 |

**Zwei Einordnungen, ohne die diese Tabelle irreführt:**

1. **Zensierung.** Die Post-Kohorte ist 79 geschlossen **+ 50 noch offen** ≈ 39 %
   unaufgelöst, und die offene Scheibe trägt +$154 unrealisiert. Die Pre-Kohorte ist zu
   ~95 % aufgelöst. Der Sprung 20,6 % → 29,1 % ist **kein Gleich-gegen-Gleich** und die
   Verzerrung begünstigt die neuere Periode.
2. **Der Ø-%-Abfall (−0,21 % → −1,03 %) ist ein Kompositionswechsel, keine
   Verschlechterung.** Die Ø Positionsgröße fiel von $453 auf $159 — das ist die
   ATR-Risk-Parity: volatilere Titel bekommen kleinere Positionen, bewegen sich aber in
   größeren Prozentschritten. **Pro eingesetztem Dollar wurde es besser:** −$1,95 → −$1,75
   je Trade.

**Fazit statt Buy/Sell/Hold:** Die Entry-Seite entwickelt sich messbar in die richtige
Richtung. Sie ist nicht das, was diesen Bot ins Minus bringt.

---

## 5. Makro-Indikatoren — der schwächste Kanal im System

Die Frage nach BIP, Arbeitslosigkeit, Inflation und Zinsen lässt sich für diesen Bot kurz
beantworten: **keiner dieser vier Werte wird irgendwo gelesen.**

**Was tatsächlich einfließt** (`_fetch_macro`,
[macro_advisor.py:52](../../src/bot/core/macro_advisor.py)):
SPY 1d/5d, QQQ 1d/5d, VIX-Level und VIX-5d-Delta, dazu der Fear-&-Greed-Index von
alternative.me. Ein LLM verdichtet das zu einem `LLM_MACRO_SCALAR`, Refresh alle 23 h,
Fallback auf neutral 1,0 nach 26 h TTL.

**Aktueller Stand:** `LLM_MACRO_SCALAR = 1.0` — also exakt neutral, gesetzt heute 06:00
mit der Begründung „Marktumfeld stabil mit niedrigem VIX". **Ein Scalar von 1,0 ist ein
No-Op.** Der Makro-Kanal verändert derzeit nichts.

**Was das Sizing tatsächlich steuert, ist die Regime-Maschine** — und die ist rein
**endogen**: sie liest ausschließlich den eigenen Drawdown gegen den Rolling-Peak
(4 % → CAUTION, 8 % → DEFENSIVE, 15 % → CRITICAL). Aktuell: DD 7,41 % → CAUTION →
Risk-Scalar 0,75.

**Bewertung.** Das ist eine defensible Architektur — der eigene Drawdown ist ein
ehrlicheres Risikosignal als eine LLM-Interpretation von Makrodaten, und der Bot reagiert
damit auf das, was ihm tatsächlich passiert, statt auf Prognosen. Aber man sollte es nicht
„Makro-Steuerung" nennen: **der Bot ist makro-blind und regelt rein reaktiv über die
eigene Verlustkurve.** Bei einem Zinsentscheid oder einem Inflationsdruck erfährt er das
erst, wenn er Geld verloren hat.

---

## 6. Value-Investing — nicht anwendbar, und das ist richtig so

**Die Ø Haltedauer beträgt 3,0 Tage** (Maximum 16,7). Value-Investing ist eine
Mehrjahres-Strategie über Wiederannäherung an den inneren Wert. Bei drei Tagen ist die
Frage nach KGV, Buchwert, Free Cashflow und Wachstumsaussichten schlicht nicht die
Frage, die dieser Bot beantworten kann — und es wäre falsch, hier eine „Lücke" zu
konstruieren, die nach Nachrüstung verlangt.

**Was tatsächlich übertragbar ist**, ist der Teil des Value-Denkens, der *defensiv* ist:
**der Qualitätsfilter gegen die Value-Falle** — die Unterscheidung zwischen „billig weil
überverkauft" und „billig weil kaputt". Genau diese Aufgabe erfüllt bereits
`is_falling_knife()` ([signals.py:273](../../src/bot/core/signals.py)):

> ≥ 4 rote Tage **oder** ROC5d ≤ −12 % **oder** ≥ 2,5 ATR unter SMA20 → Dip-Kauf-Regeln
> gesperrt; nur die MACD-Wende-Regel bleibt erlaubt.

Und Abschnitt 4 zeigt, dass dieser Filter genau das Richtige getan hat.

**Ergänzend vorhanden:** Liquiditäts-Tiering über Marktkapitalisierung und
20-Tage-Dollar-Volumen (`liquidity.py`), Mindest-ADV $500k für BUY-Signale, sowie
Analysten-Kursziele als Dämpfer (Preis > 5 % über Konsens → CAUTION, > 25 % → AVOID).
Das ist der Bewertungsanteil, der bei drei Tagen Haltedauer sinnvoll ist.

---

## 7. Investoren-Sentiment — richtig konzipiert, quantitativ unterversorgt

Der `news_flags_worker` läuft stündlich und kondensiert Headlines, Earnings-Termine und
Analysten-Kursziele zu Risk-Flags in `data/llm_news_flags.json` (TTL 12 h).

**Das Design-Prinzip ist bemerkenswert gut:** *asymmetrische Rechte.* Flags können Trades
nur **dämpfen** (AVOID = Signal überspringen, CAUTION = halbe Größe), niemals verstärken.
Ein halluziniertes Flag kostet damit eine Gelegenheit, kein Geld. Earnings-Flags sind
regelbasiert (Termin binnen 2 Tagen → AVOID), das LLM bewertet ausschließlich Headlines.
Alles fail-open. Das ist die richtige Art, ein LLM in einen Geldpfad einzubauen.

**Das Problem ist die Abdeckung:**

| Cap | Wert | gegen |
|---|---|---|
| `NEWS_SYMBOL_CAP` | 20 | **54 Live-Symbole** |
| `EARNINGS_SYMBOL_CAP` | 12 | 54 |
| `ANALYST_SYMBOL_CAP` | 12 | 54 |

Für Earnings- und Analysten-Prüfung werden also **maximal 22 % des Buchs** betrachtet.
Aktuell ist genau **1 Flag** aktiv (00006.HK, AVOID wegen Earnings am 2026-08-12).

Nicht erfasst: Social-Media-Trends, Short-Interest, Optionsflow, Put/Call-Ratio,
Insider-Transaktionen. Der Fear-&-Greed-Index läuft, aber nur als Makro-Input in einen
Scalar, der bei 1,0 steht (Abschnitt 5).

**Bewertung.** Der Sentiment-Kanal ist konzeptionell sauber und praktisch fast blind —
nicht wegen schlechter Logik, sondern weil die Caps aus einer Zeit mit deutlich weniger
offenen Positionen stammen. Das ist die billigste Verbesserung im ganzen Dokument.

---

## 8. Earnings-Auswertung — bewusst außerhalb des Scopes, korrekt so

Der Bot hat `earnings_exit.py`: er **meidet das Ereignis** (Termin binnen 2 Tagen → AVOID
bzw. Exit vor dem Termin). Was er nicht hat, ist eine Earnings-**Analyse** — Umsatz, EPS,
Marge, Cashflow, Guidance, Year-over-Year, Vergleich gegen Analystenschätzungen.

**Das ist bei 3,0 Tagen Ø Haltedauer die richtige Scope-Entscheidung, kein Defizit.** Ein
Earnings-Report ist ein Repricing-Ereignis mit binärem Gap-Risiko; für eine
Drei-Tage-Position ist die einzig sinnvolle Antwort darauf, nicht dabei zu sein — genau
das tut der Bot. Eine Guidance-Interpretation würde eine Haltedauer von Wochen bis
Quartalen voraussetzen.

Die einzige belastbare Verbindung zur Earnings-Frage ist die Abdeckung:
`EARNINGS_SYMBOL_CAP = 12` gegen 54 Symbole (siehe Abschnitt 7) — **42 offene Positionen
werden nicht auf anstehende Earnings geprüft.** Das ist ein reales Gap-Risiko und der
Punkt, an dem die Earnings-Frage für diesen Bot tatsächlich beißt.

---

## 9. Growth vs. Dividende — die falsche Achse; die richtige heißt anders

Die Growth-vs-Dividende-Unterscheidung setzt einen Anlagehorizont von Jahren voraus:
Dividendenrendite wirkt über Ausschüttungszyklen, Growth-Prämien über
Gewinnwachstumspfade. Bei drei Tagen ist beides Rauschen. Der Bot kennt weder
Dividendenrendite noch Gewinnwachstum — und braucht sie nicht.

**Die Achse, die dieses System tatsächlich hat, ist Mean-Reversion vs. Trend-Following**
— und die ist explizit modelliert, inklusive 45 %-Kappe pro Kategorie
(`SIGNAL_CATEGORY` / `MAX_CATEGORY_FRACTION`).

| | Mean-Reversion | Trend-Following | Core-Sweep |
|---|---|---|---|
| Signale | RSI-Extrem, BB-Lower, MACD-Wende | TREND_PULLBACK, GOLDEN_CROSS | signal-agnostisch |
| Ergebnis | gemischt: mit MACD-Bestätigung **+$94,8**, ohne **−$90** | 42 Trades / 12 W / **−$131,1** | 47 Trades / 13 W / **−$124,0** |
| Risikoprofil | viele kleine Verluste, seltene größere Gewinne | wenige Einstiege, längere Haltedauer | größte Tickets (~$347), keine Gates |

**Das Analogon zur Ausgangsfrage:** Mean-Reversion ist hier die „Dividenden"-Seite —
häufige kleine Ergebnisse, hohe Frequenz, geringe Einzelvarianz. Trend-Following ist die
„Growth"-Seite — seltener, größere Einzelbeträge, abhängig davon, dass Gewinner laufen
dürfen. **Und genau das dürfen sie nicht** (Abschnitt 3): Break-Even-Stop bei +0,3 % und
Momentum-Fade bei 30 % Rückgabe kappen jeden Trend, lange bevor er die erste Profit-Stufe
erreicht — die im Median bei +17 % liegt.

**Das erklärt, warum die Trend-Familie mit −$131,1 der größte Einzelverlust ist:** Der Bot
fährt eine Trend-Strategie mit einem Mean-Reversion-Exit-Regime. Die beiden passen nicht
zusammen.

---

## 10. Globale Ereignisse & Schockresistenz

**Was bei einem Schock greift:**

| Schicht | Wirkung |
|---|---|
| Regime-Maschine | Drawdown 4/8/15 % → Sizing 0,75 / 0,50 / 0,25; ab DEFENSIVE kein Pyramiding, Core-Sweep pausiert komplett |
| Multi-Horizont-Loss-Limits | Tag 5 %, Woche 8 %, Monat 12 % als MDD gegen Rolling-Peak — fängt den langsamen Blutverlust, den ein reines Tageslimit durchlässt |
| Kill-Switch | `data/kill_switch.flag`, Tages-Scope mit Auto-Clear, Watchdog-Script |
| DEFER-Architektur | geschlossener Markt ⇒ Trade bleibt APPROVED und wird alle 15 min neu versucht, statt auf FAILED zu fallen |
| Korrelations-Gate | verhindert, dass sich der Bot vor dem Schock in gleichlaufende Titel häuft |

**Was nicht greift:**

1. **Gap-Risiko ist nicht abgesichert.** Der Stop-Loss liegt bei eToro serverseitig, aber
   **eToro hat keinen SL-Update-Endpoint** — das Trailing läuft rein softwareseitig im
   5-Minuten-Zyklus. Ein Wochenend-Gap oder ein Overnight-Schock springt über den Stop.
   Bei 47 % EU- und 29 % Asien-Exposure (Abschnitt 1) handelt der Bot zu 76 %
   Märkte, die geschlossen sind, während er selbst wach ist.
2. **Kein Regionen-Cap.** Ein EU-spezifisches Ereignis trifft 47 % des Buchs gleichzeitig,
   ein China-Ereignis 22 %.
3. **Core-Sweep umgeht die Gates, die im Schock am wichtigsten wären** — Exposure und
   Korrelation (Abschnitt 2a). Immerhin: der Regime-Gate greift für Core-Sweep, es
   pausiert ab DEFENSIVE. Bei CAUTION — dem aktuellen Zustand — kauft es weiter.
4. **Keine Absicherungsinstrumente.** Kein Hedging, keine inversen ETFs, keine
   Volatilitätsposition, keine Cash-Erhöhung über den 15 %-Floor hinaus.

**Bewertung.** Gegen einen *graduellen* Abschwung ist das System gut gerüstet — die
Regime-Kaskade und die Loss-Limits sind sauber gestaffelt und greifen nachweislich (der
Bot steht genau deshalb heute auf Risk-Scalar 0,75). Gegen einen *schlagartigen* Schock
ist die einzige echte Verteidigung die Positionsgröße (max. 4 % je Titel). Die zweite
Verteidigungslinie — nicht zu viel gleichzeitig zu halten — ist derzeit für 74,2 % des
Buchs nicht scharf.

---

## Priorisierte Empfehlungen — UMGESETZT am 2026-08-12

**Status-Update:** Alle Punkte wurden am selben Tag implementiert, getestet
(794 Tests grün) und live verifiziert. Commits `56b2382` … `bc96979`.

Zwei Abweichungen von den ursprünglichen Vorschlägen, beide auf
User-Entscheid „der Bot soll autonom handeln":

- **P2 Exposure-Drift** wurde **nicht** warn-only, sondern **Auto-Trim**
  (LIFO-Close bis zurück unter den Cap, `risk.exposure_auto_trim`).
- **P2 Regionen-Cap** wurde ein **Sizing-Damper** (Soft 35 % → linear auf
  Faktor 0,35 bis Hard 50 %) statt einer Warnung — ein harter Cap unter der
  aktuellen EU-Quote hätte die Haupt-Signalquelle abgeschaltet, also
  Autonomie genommen.

Zusätzlich bei der Verifikation gefunden und behoben: der Auto-Trim traf per
LIFO eine HK-Position bei geschlossener Börse und lief in eine 165-s-Retry-
Schleife (`bcf06fa` — Market-Guard + PENDING-Muster), und die Testsuite
postete echte Discord-Meldungen in den Live-Channel (`dae0428`).

Die folgende Liste dokumentiert den Stand **vor** der Umsetzung.

### P0 — Core-Sweep durch `check_buy_gate` führen

**Befund:** Der Pfad, der 41 der 57 offenen Trades eröffnet hat, umgeht Exposure-,
Asset-Class-, Korrelations-, Instrument-Limit-, Conviction- und Slippage-Gate.
**Wirkung:** Der Bot kauft im Drawdown weiter, während der reguläre Pfad korrekt blockt —
heute 6× belegt.
**Vorschlag:** In Block 5b vor `create()` mindestens Exposure- und Korrelations-Gate
aufrufen.
**⚠️ Risiko — bewusst zu entscheiden:** Bei aktuell 81,9 % Exposure stoppt das Core-Sweep
**sofort und vollständig**. Das ist die beabsichtigte Wirkung, bedeutet aber einen
faktischen Kaufstopp, bis Exposure unter 75 % fällt.

### P1 — `instruments.sector` befüllen und Sektor-Caps darauf stützen

**Befund:** 74,2 % des Equity haben kein Asset-Class-Mapping; `sector` ist bei 0 von
15.501 Zeilen gefüllt.
**Vorschlag:** Sektor per yfinance für die 4.161 handelbaren Instrumente nachziehen
(einmaliges Backfill-Script + TTL-Refresh), dann `check_asset_class_gate` auf
`instruments.sector` statt auf das hartkodierte Dict umstellen.
**⚠️ Ausdrücklich nicht der Alternativweg:** Das Gate einfach auf `classify_asset_class()`
umzustellen sieht wie die billigere Lösung aus, ist aber ein Footgun — die Funktion gibt
für jedes unbekannte Symbol `"STOCK"` zurück, wofür `ASSET_CLASS_LIMITS` keinen Eintrag
hat, also greift der 20 %-Default. Ergebnis: 74,2 % des Equity in einem einzigen Bucket
gegen ein 20 %-Limit ⇒ **sofortiger Aktien-Kaufstopp**. Die beiden Optionen sind nicht
gleichwertig.

### P1 — Exit-Logik statt Entry-Logik angehen

**Befund:** Die erste Profit-Stufe liegt im Median bei **+17,0 %** (ATR×6, 39 von 59
Positionen ≥ 15 %), der Ø Gewinn-Trade bei **+3,49 %**. Die Leiter feuert praktisch nie.
Was Gewinner beendet, sind ausschließlich Mechanismen im Band +0,3 % bis +3 %:
Break-Even-Stop und Momentum-Fade (30 % Rückgabe ab Peak +2 %).
**Wirkung:** Zwischen +3,5 % und +17 % gibt es keinen Mechanismus, der einen Gewinner
strukturiert am Leben hält — die Gewinnseite ist gedeckelt, ohne dass es jemand
entschieden hätte.

**Vorschlag (geldwirksam — nur nach ausdrücklicher Freigabe):** Die erste Profit-Stufe
näher legen, z. B. ATR×3 statt ATR×6 (Median dann ~8,5 %), damit die Leiter überhaupt in
Reichweite realer Bewegungen kommt.

**⚠️ Wichtig zur Erwartungshaltung:** Rein arithmetisch bräuchte es bei unveränderter
Trefferquote von 23,6 % einen Ø Gewinn von +5,63 % für Breakeven. **Diese Zahl ist aber
kein erreichbares Ziel**, denn Trefferquote und Ø Gewinn sind nicht unabhängig: Exits zu
lockern lässt mehr Trades zu null oder ins Minus zurücklaufen, senkt also die
Trefferquote, während der Ø Gewinn steigt. Die Richtung ist gut belegt, der Zielwert
nicht. **Deshalb zuerst als Backtest gegen die 220 geschlossenen Trades — nie direkt
live.**

### P2 — Exposure-Drift-Monitor

Analog zu `check_asset_class_violations` — Detection-only mit WARNING, kein Auto-Close,
weil erzwungenes Rebalancing eine deutlich größere Änderung wäre als eine Warnung.
Schließt die Lücke, dass Exposure durch fallende Equity über den Cap wandert.

### P2 — Regionen-Cap einführen

EU 47 % ist die reale Klumpenlage. `market_region` ist bereits gefüllt und gepflegt — ein
Cap darauf ist billiger als der Sektor-Backfill aus P1 und trifft das tatsächliche Risiko
dieses Buchs genauer.

### P2 — `closed_at`-Lücke im Reconciler schließen

45 geschlossene Trades ohne `closed_at`. Trübt jede Zeitreihen-Auswertung, auch die
Lernschleife des LLM-Review-Workers.

### P3 — Tote Config `max_positions: 40`

`check_max_positions_gate` ist aus `check_buy_gate` entfernt
([risk.py:749](../../src/bot/core/risk.py)) — der Config-Wert wird von niemandem gelesen,
59 Positionen sind offen. Entweder verdrahten oder aus der Config entfernen, damit
niemand daran „tunt" in der Annahme, es wirke.

### P3 — News-/Earnings-Caps anheben

20/12/12 gegen 54 Symbole. Die Earnings-Prüfung ist die wichtigste davon (Gap-Risiko,
Abschnitt 8) — mindestens auf die Zahl der offenen Positionen anheben.

---

## Anhang — Queries zum Nachrechnen

Alle Zahlen dieses Dokuments stammen aus diesen Abfragen (alle read-only):

```sql
-- Kernkennzahl: Trefferquote, Ø Gewinn, Ø Verlust
SELECT COUNT(*) n, SUM(pnl_usd>0) wins,
       ROUND(AVG(CASE WHEN pnl_usd>0  THEN pnl_pct END),2) avg_win,
       ROUND(AVG(CASE WHEN pnl_usd<=0 THEN pnl_pct END),2) avg_loss,
       ROUND(SUM(pnl_usd),1) total
FROM trades WHERE status='CLOSED';

-- Kohortenvergleich inkl. Positionsgröße (Schnitt: Falling-Knife-Gate)
SELECT CASE WHEN created_at<'2026-07-26' THEN 'pre' ELSE 'post' END era,
       COUNT(*) n, SUM(pnl_usd>0) w, ROUND(AVG(amount_usd),1) avg_size,
       ROUND(SUM(pnl_usd)/COUNT(*),2) usd_per_trade
FROM trades WHERE status='CLOSED' AND pnl_usd IS NOT NULL GROUP BY 1;

-- Zensierung: offene Positionen je Kohorte
SELECT CASE WHEN created_at<'2026-07-26' THEN 'pre' ELSE 'post' END era, COUNT(*)
FROM trades WHERE status='ACTIVE' GROUP BY 1;

-- Herkunft der offenen Trades (Core-Sweep vs. Signalpfad)
SELECT CASE WHEN s.signal_type='CORE_SWEEP' THEN 'CORE_SWEEP'
            WHEN s.signal_type IS NULL THEN '(kein Signal)'
            ELSE 'Signal-Pfad' END src, COUNT(*)
FROM trades t LEFT JOIN signals s ON s.id=t.signal_id
WHERE t.status='ACTIVE' GROUP BY 1;

-- Beweis Gate-Bypass: Exposure-Blocks im Log
SELECT ts, message, details FROM system_log
WHERE details LIKE '%Exposure-Gate%' ORDER BY id DESC LIMIT 10;

-- Live-Exposure (Wahrheit ist portfolio_snapshot, NICHT trades.amount_usd)
SELECT COUNT(*) pos, COUNT(DISTINCT symbol) syms,
       ROUND(SUM(amount_usd),0) invested, ROUND(SUM(unrealized_pnl),1) upnl
FROM portfolio_snapshot;

-- Regionen-Konzentration
SELECT i.market_region, COUNT(*), ROUND(SUM(p.amount_usd),0)
FROM portfolio_snapshot p LEFT JOIN instruments i USING(instrument_id)
GROUP BY 1 ORDER BY 3 DESC;

-- Sektor-Befüllung (Ergebnis: 15.501x leer)
SELECT CASE WHEN sector IS NULL OR sector='' THEN 'EMPTY' ELSE 'SET' END, COUNT(*)
FROM instruments GROUP BY 1;

-- Performance je Signalfamilie
SELECT s.signal_type, COUNT(*) n, SUM(t.pnl_usd>0) w, ROUND(SUM(t.pnl_usd),1) pnl
FROM trades t JOIN signals s ON s.id=t.signal_id
WHERE t.status='CLOSED' AND t.pnl_usd IS NOT NULL GROUP BY 1 ORDER BY 2 DESC;
```

Zwei Messungen laufen nicht per SQL, weil die Referenzwerte im Quellcode leben, nicht in
der DB:

```bash
# Asset-Class-Mapping-Lücke (Ergebnis: 43 von 54 Symbolen / $6.433 / 74,2 %)
PYTHONPATH=src python3 -c "
import sqlite3; from bot.core.risk import ASSET_CLASS_MAP
c=sqlite3.connect('file:data/trading.db?mode=ro',uri=True); c.row_factory=sqlite3.Row
rows=c.execute('SELECT symbol, SUM(amount_usd) amt FROM portfolio_snapshot GROUP BY symbol').fetchall()
um=[(r['symbol'],r['amt']) for r in rows if not ASSET_CLASS_MAP.get(r['symbol'].upper())]
print(len(um),'von',len(rows),'Symbole ohne Mapping — \$%.0f' % sum(a for _,a in um))"

# Erste Profit-Stufe je Position (Ergebnis: Median 17,0 %)
PYTHONPATH=src python3 -c "
import sqlite3
c=sqlite3.connect('file:data/trading.db?mode=ro',uri=True); c.row_factory=sqlite3.Row
r=[min(max(6.0*x['atr_pct'],6.0),30.0) for x in c.execute(
   'SELECT i.atr_pct FROM portfolio_snapshot p JOIN instruments i USING(instrument_id)')]
r.sort(); print('Median %.1f%%  |  >=15%%: %d von %d' % (r[len(r)//2], sum(1 for v in r if v>=15), len(r)))"
```
