"""Bewertungs- und Schutzregeln nach dem Karpathy-Autoresearch-Loop.

QUELLE: Moon-Dev-Video "Andrej Karpathy's AI Trading Loop" (gesehen
2026-09-09). Der Loop laesst einen Agenten EINE Datei (`strategy.py`)
aendern, bewertet sie mit einer EINGEFRORENEN Harness und behaelt oder
verwirft — kein Mittelweg.

WAS WIR UEBERNEHMEN (und warum es zu unseren Befunden passt):

1. EINE Zahl entscheidet.  K = ln(1 + ROI) * Sharpe
   Gegen die veroeffentlichte Top-10-Tabelle nachgerechnet, groesste
   Abweichung 0.006 — die Formel ist verifiziert, nicht geraten.

2. TRADE-COUNT-GUARD (>= 50).  Eine Strategie mit drei Trades hat kein
   Ergebnis, sondern Glueck.

3. VOL-GUARD.  "Bigger bets raises return and sharpe on the exact same
   trades. That is a leverage dial, not an idea." Eine Variante zaehlt nur,
   wenn die Volatilitaet in einem Band bleibt.
   -> Genau unser Kelly-Drift-Fall: das gewichtete Mittel wanderte vom
      freigegebenen 0.30 auf 0.47 (+56 % Positionsgroesse) OHNE dass jemand
      eine Idee geaendert haette. Der Vol-Guard haette das gemeldet.

4. LOCK-WINDOW.  "1,000 tries on one window is 1,000 chances to curve fit."
   Der Loop sieht nur In-Sample; alles danach bleibt in der Schublade und
   wird einmal taeglich separat geprueft.
   -> Unsere Harness hatte GAR KEINE Trennung, und `--knife-atr` wurde
      bereits ueber zwei Werte gefahren (ka2.5 / ka4.0). Das ist der Anfang
      derselben Kurve.

WAS WIR NICHT UEBERNEHMEN: die 44.975 % selbst. Das Terminal im Video sagt
ausdruecklich "in-sample only" nach 95 Durchlaeufen auf EINEM Asset (ETH)
in einem Zeitraum, der die 2017er- und 2021er-Rallyes enthaelt. Der
Out-of-Sample-Test war zum Zeitpunkt der Aufnahme noch nicht gelaufen.
"""
from __future__ import annotations

import math

# Karpathy-Loop: mindestens 50 Trades, sonst ist das Ergebnis Rauschen.
MIN_TRADES = 50

# Vol-Guard: erlaubte Abweichung der annualisierten Vol gegen die Baseline.
# 15 % ist bewusst weit — es geht darum, den Hebel-Regler zu erkennen, nicht
# um Feinsteuerung. Im Video bleibt Vol% ueber die Top 10 in einem Band von
# 107.6 bis 116.9, also rund +/- 4 % um den Median. 15 % faengt grobe
# Hebelspiele und laesst echte Ideen durch.
VOL_BAND_PCT = 15.0


def k_score(total_return_pct: float | None, sharpe: float | None) -> float | None:
    """K = ln(1 + ROI) * Sharpe — die eine Zahl, die entscheidet.

    `total_return_pct` in Prozent (44975.0 fuer +44.975 %), `sharpe` roh.

    Warum diese Form: der Logarithmus daempft die Rendite, damit ein
    Ausreisser-Jahr nicht alles dominiert, und die Multiplikation mit Sharpe
    bestraft Ergebnisse, die nur durch Schwankung entstanden sind. Eine
    negative Rendite oder ein negativer Sharpe machen K negativ — genau
    richtig fuer "keeper or revert".

    Gibt None zurueck, wenn eine Eingabe fehlt oder die Rendite <= -100 %
    ist (Totalverlust — der Logarithmus ist dort nicht definiert).
    """
    if total_return_pct is None or sharpe is None:
        return None
    try:
        r = 1.0 + float(total_return_pct) / 100.0
        if r <= 0:
            return None
        lg = math.log(r)
        sh = float(sharpe)
    except (TypeError, ValueError):
        return None

    # fix/k-sign (2026-09-09): das rohe Produkt ln(1+r) * sharpe wird bei
    # VERLUST und negativem Sharpe wieder POSITIV — minus mal minus. Gemessen
    # an unserem eigenen Lauf: V2 out-of-sample -1.54 % bei Sharpe -0.06
    # ergab K = +0.001 und damit das Urteil "keeper" fuer eine Variante, die
    # in beiden Fenstern verliert. Im Karpathy-Loop faellt das nicht auf, weil
    # dort nur Gewinner ueberhaupt in die Naehe der Bestenliste kommen — als
    # Entscheidungsregel ist es ein Loch.
    #
    # Regel: sobald EIN Faktor Verlust anzeigt, muss K negativ sein. Die
    # Groessenordnung bleibt erhalten, damit die Rangfolge unter den
    # Verlierern noch etwas aussagt.
    k = lg * sh
    if lg < 0 or sh < 0:
        return -abs(k)
    return k


def check_guards(n_trades: int | None, vol_pct: float | None,
                 baseline_vol_pct: float | None = None,
                 min_trades: int = MIN_TRADES,
                 vol_band_pct: float = VOL_BAND_PCT) -> tuple[bool, list[str]]:
    """(bestanden, Gruende). Guards laufen VOR der Bewertung.

    Im Video: "check the guards first". Eine Variante, die einen Guard
    reisst, bekommt gar keine Punktzahl — sie ist kein Kandidat.
    """
    gruende: list[str] = []

    if n_trades is None or int(n_trades) < min_trades:
        gruende.append(
            f"Trade-Count {n_trades} < {min_trades} — zu wenig fuer eine Aussage")

    if baseline_vol_pct is not None and vol_pct is not None:
        try:
            base = float(baseline_vol_pct)
            if base > 0:
                abw = abs(float(vol_pct) - base) / base * 100.0
                if abw > vol_band_pct:
                    gruende.append(
                        f"Vol {vol_pct:.1f}% weicht {abw:.0f}% von der Baseline "
                        f"{base:.1f}% ab (>{vol_band_pct:.0f}%) — Hebel-Regler, "
                        f"keine Idee")
        except (TypeError, ValueError):
            pass

    return (not gruende), gruende


def split_trades(trades: list[dict], oos_start: str | None,
                 date_key: str = "entry_date") -> tuple[list[dict], list[dict]]:
    """Trades in In-Sample und Out-of-Sample teilen (Lock-Window).

    Getrennt wird am EINSTIEGSDATUM: ein Trade gehoert dorthin, wo die
    Entscheidung fiel, nicht wo er zufaellig endete. Ohne `oos_start` ist
    alles In-Sample — das bisherige Verhalten.
    """
    if not oos_start:
        return list(trades), []
    ins, oos = [], []
    for t in trades:
        d = t.get(date_key) or ""
        (oos if str(d) >= oos_start else ins).append(t)
    return ins, oos


def degradation_pct(is_value: float | None, oos_value: float | None) -> float | None:
    """Wie viel des In-Sample-Ergebnisses ueberlebt out-of-sample, in Prozent.

    100 = unveraendert, 0 = komplett weg, negativ = OOS dreht ins Minus.
    Das ist die Zahl, die "44.975 % in-sample" von einem echten Ergebnis
    unterscheidet.
    """
    if is_value is None or oos_value is None:
        return None
    try:
        iv = float(is_value)
        # Bei negativer In-Sample-Basis ist der Quotient bedeutungslos:
        # -1.5 / -0.5 ergibt "302 % ueberlebt", obwohl beide Fenster
        # verlieren. Eine Kante, die es nicht gab, kann nicht ueberleben.
        if iv <= 1e-9:
            return None
        return float(oos_value) / iv * 100.0
    except (TypeError, ValueError, ZeroDivisionError):
        return None
