# Analyse: falsche `instruments.yfinance_symbol` im Bot (2026-10-17)

**Anlass:** Beim Bau der Analysten-Strategie fiel auf, dass für US-Aktien das
gespeicherte Yahoo-Symbol oft nicht der eToro-Ticker ist. Beispiele: DPZ →
„DPI“, FAST → „FC_4212“, MRK → „M&C“.

**Prüfskript:** `scripts/check_yf_symbol_identity.py`. Es liest nur und
vergleicht den letzten Close in `ohlcv_daily` mit dem eToro-Kurs (Batch-Rates).

## 1. Aktive Instrumente (Watchlist, Positionen, Signale der letzten 30 Tage)

Geprüft wurden 760 Instrumente:

| Ergebnis | Anzahl |
|---|---|
| Kurse passen | 535 |
| keine Tageskurse in `ohlcv_daily` | 214 (174 davon Krypto, Signale kommen dort aus anderer Quelle) |
| kein eToro-Kurs | 3 |
| **Verdacht** | **8** |

Die 8 Verdachtsfälle:

| Instrument | gespeichert | Befund |
|---|---|---|
| **CLPT** ClearPoint Neuro | CNI | **andere Firma** (Canadian National Railway), 87 Tage Fremdkurse |
| **BRBI** BR Advisory Partners | BAP | **andere Firma** (Credicorp), 128 Tage Fremdkurse; hat Signale erzeugt |
| **ALX.ASX** Atlas Arteria | AAL.AX | **andere Firma** |
| INDO.PA, JIM.L, VACI | identisch / VACI | Kurs weicht 18–175 % ab, Reihe teils veraltet |
| CottonNo7, US2000 | CT=F, ^RUT | Reihe seit 25.06. veraltet, Einheit verschieden |

Keine offene Position ist betroffen. Veraltet seit Juni sind außerdem 6
Devisenpaare (EURNOK, EURSEK, USDTRY, USDSGD, GBPCAD, EURDKK) und 2 Futures
(NSDQ100.FUT, HKG50.FUT).

## 2. Alle handelbaren US-Aktien (6 266)

Abgeglichen wurde der eToro-Kurs gegen Yahoo zum gespeicherten Symbol und gegen
Yahoo zum Ticker. Bei 5 777 Titeln weicht das gespeicherte Symbol ab:

| Kategorie | Anzahl |
|---|---|
| gespeichertes Symbol = **andere Firma**, Ticker passt | 36 |
| gespeichertes Symbol = andere Firma, Ticker ohne Yahoo-Daten | 80 |
| gespeichertes Symbol ohne Daten, Ticker passt | 913 |
| beide ohne passende Daten | 26 |
| keine Yahoo-Daten (Rate-Limit beim Abruf, unvollständig) | 4 714 |
| nur das gespeicherte Symbol passt (echte Umbenennung) | 8 |

**Fälle im Analysten-Universum (S&P 500/400):** AAP → AAPI, BXP → BPI, MPT →
MPTI, VNO → VRT, AM → AMC, APO → AGMI, VMI → VII, AGO → AGL, CI → CG, TSN → TFI,
EMN → ECC, GPN → GPI, XRAY → DSI, BG → BGS u. a.

## 3. Wo der Bot das gespeicherte Symbol nutzt

- **Kursdaten:** `data_worker` und `ohlcv_cache` (Signale, Charts). Ein Symbol
  ohne Daten fällt auf den Ticker zurück (Auflösung wird gespeichert). Ein
  **gültiges Symbol einer anderen Firma fällt nie zurück**: CLPT und BRBI.
- **News-Flags:** Headlines, Earnings-Termine und Kursziele.
  - **Behoben** (fix/yf-us-ticker): Für US-Aktien gilt jetzt der Ticker.
  - Vorher konnte ein AVOID aus fremden Headlines Käufe blocken und
    Strategie-Positionen verkaufen.
- **Pre-Trade-Veto und Analysten-Strategie:** Für US-Aktien gilt der Ticker
  (`buy_veto.yahoo_symbol`), schon vorher behoben.
- **Weitere Nutzer:**
  - `market_hours`: Börsenzeiten nach Suffix, z. B. SONY → „6758.T“ = Tokio.
  - `position_review_worker`, `exit_monitor`, `earnings_exit`, `trailing_stop`,
    `correlation`, `discovery_worker`.

## 4. Vorschlag zur Datenkorrektur (Entscheidung VoLLi)

1. **Symbol korrigieren**, wo der Ticker belegt zum eToro-Kurs passt.
   - Das sind 949 US-Aktien (36 + 913).
   - Dazu CLPT, BRBI und ALX.ASX (→ ALX.AX).
   - Umbenennungen bleiben.
2. **Fremde Kurshistorie löschen** in `ohlcv_daily` für die Instrumente mit
   anderer Firma (Cache-Tabelle). Der `data_worker` lädt sie dann neu.
3. **Rest (~4 800):** Abgleich wiederholen, langsam wegen des Rate-Limits. Sie
   sind ruhend (nicht in der Watchlist).
4. **Veraltete Reihen** (Devisen und Futures seit Juni): separat prüfen.
