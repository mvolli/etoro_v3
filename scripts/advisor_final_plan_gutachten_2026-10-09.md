# Gegenprüfung Finalplan, 2. Runde

**Kurzurteil:** Die Phasen-Struktur trägt. Zwei Entscheidungen sind aber nicht vertretbar, und eine davon wirkt im Code anders als im Plan beschrieben (Code geprüft, siehe D4).

## (1) Widersprüche zu den Quellen

- **Wer entscheidet:** Der Plan nennt D1–D6 „autonom beschlossen“. Die Analyse-Übergabe (§8), PLAN-freies-kapital (§6 D, §7.4) und CLAUDE.md sagen dagegen, dass geldwirksame Schwellen allein VoLLi festlegt. D4 und D5 stehen dort ausdrücklich auf der VoLLi-Liste. Das ist ein harter Regelverstoß, kein Formfehler.
- **CORE_SWEEP +$192:** Diese Zahl stammt aus `trade_events`. Die Doppelbuchungs-Übergabe (§3, §5) zeigt für dieselbe Familie beim Broker **−$85,88** (seit 11.09.) und sagt: „jeder Typ ist negativ“. Auch die Dedup-Regel trifft den Broker nicht. Der Plan übernimmt die +$192 trotzdem als gesichert.
- **Fee-Modell:** Der Plan schreibt „geklärt (API 09.10.)“. Die Analyse-Übergabe um 17:26 nennt das Fee-Modell fürs Bot-Konto noch offen. Den Beleg dafür sollte der Plan zitieren.
- **„Eine Änderung nach der anderen, je 2–3 Tage“** (PLAN-freies-kapital §7.2): Der Finalplan stapelt Quote-Fix, Edge-Gate und D4 hinter ein einziges 24-h-Fenster.
- **D2 gegen D5:** MACD+BB hat −$0,11 pro Trade bei n=263. Die untere Konfidenzgrenze (LCB) ist damit sicher negativ, also schaltet das Gate die Familie in den Shadow-Modus. Das ist faktisch die Vollsperre, die D2 ausschließt.
- **0a ist nicht read-only:** Phase 0a legt eine neue Tabelle an, schreibt Korrektur-Events und setzt Trade 2824 neu. Die Korrektur-Events ändern `realized_by_trade`. Daraus rechnet das Kelly-Sizing, sie wirken also auf Geld und stören das D6-Fenster sowie die Messung 0c.

## (2) D1–D6

- **D4 ist so nicht vertretbar.** Im Code geprüft:
  - Die Größenreduktion existiert bereits als `entry_quality.core_sweep_regime` mit 0,25x.
  - Danach rundet `execution_worker.py:890` jeden Trade unter $200 auf **$200** hoch. Aus 0,25x werden also volle $200-Positionen.
  - Bis Phase 4 gibt es keinen Exposure- oder Anzahl-Cap für die 130 Whitelist-Titel.
  - In DEFENSIVE gibt es keine einzige Messung (n=0). Schon NORMAL liegt bei −$60, der Schluss von CAUTION auf DEFENSIVE ist reine Hoffnung.
  - Die Abbruchregel (Drawdown > $150) kommt bei vielen parallelen Einstiegen zu spät.
- **D5:** Die Richtung stimmt, „Gate vor Floor“ ist richtig. Es gibt aber drei Probleme:
  - Fail-open bei n < n_min. Wenn das Gate pro (Typ, Regime) rechnet, läuft CORE_SWEEP in DEFENSIVE (n=0) **ungegated** durch. Das hebelt das Gate genau dort aus, wo D4 aufmacht.
  - Ein Gate auf Ledger-Daten mit 18,9 % Duplikaten misst Buchungsfehler, nicht Edge.
  - Für das Re-Enable gibt es zum Go-live keine Messung: Shadow-Forward-Returns kommen erst mit Phase 3a. Ein LCB > 0 bei n=20 ist praktisch unerreichbar, der Shadow-Zustand wäre damit absorbierend. Dazu kommen LCB und Shrinkage obendrauf, also eine doppelte Strafe.
- **D1 greift zu kurz.** ELV kam vom **risk_worker** (5-min-Takt), nicht von der LLM-Runde. APE und 543A stehen auf ACTIVE, ein zweiter EMERGENCY-SL bleibt also möglich. Außerdem ist es jetzt 17:34, die Runde um 19:35 steht noch an.
- **D2, D3, D6:** in Ordnung.

## (3) Phasen-Reihenfolge

Im Kern logisch. Drei Korrekturen:
1. **Der Close-Order-Wächter aus 0b muss vor jeder Ledger-Korrektur aus 0a kommen.** Wer Trade 2824 vorher von CLOSED zurücksetzt, macht die Position für den risk_worker wieder „offen“. Dann folgt ein zweiter Close auf die wartende Order 1608203517, also genau der Fehler, den der Plan verhindern will. Reihenfolge daher: `close_orders` lesen → Wächter scharf → erst dann korrigieren.
2. **3a (Forward-Returns, rein lesend) vor Phase 2 ziehen.** Es stört D6 nicht und liefert den Re-Enable-Maßstab sowie das Verlustprofil für D3.
3. **D4 erst nach einer Woche stabilem Edge-Gate**, nicht zusammen damit. Der Plan führt D4 heute in Phase 2 und Phase 4 doppelt, ohne eindeutigen Live-Zeitpunkt.

## (4) Größter Blindspot

**Die Kapitalentscheidung steht auf der Datenquelle, deren Fehlerhaftigkeit eine der eigenen Quellen belegt.** D4 setzt auf die +$192 (Ledger mit Duplikaten), der Broker zeigt −$86. Weil der $200-Floor die 0,25x aufhebt und kein Cap existiert, würde ein 84-%-Cash-Konto in einem Regime ohne Messung mit vollen Positionen in die einzige Familie gehen, deren „Edge“ ein Buchungsartefakt sein kann. Das Edge-Gate fängt das nicht ab: Es rechnet auf demselben Ledger und lässt n=0 per Fail-open durch.

## Empfehlung

- D4 und D5 als **Vorschlag an VoLLi** umformulieren, nicht als Beschluss.
- D4 nur mit eigenem Sizing unterhalb des Floors oder mit hartem Cap (z. B. max. 3 offene Sweep-Positionen), validiert gegen Broker-netProfit statt `trade_events`.
- Fail-open für n=0 ersetzen durch Shadow-Modus.
- D1 auf den risk_worker ausdehnen, oder 0b vor der nächsten Marktöffnung fertigstellen.
