#!/usr/bin/env python3
"""Corporate-Action-Guard — Split-/Sonderdividenden-Artefakte in Kursreihen.

Alle yfinance-Fetches im Repo laufen mit ``auto_adjust=True``; Yahoo rechnet
Splits und Dividenden RUECKWIRKEND aus der Historie heraus. Der Guard greift
deshalb in genau EINEM Fenster: zwischen dem Ex-Tag und dem Moment, in dem
Yahoo den Anpassungsfaktor gesetzt hat (Stunden bis Tage). Solange der
Faktor fehlt, steht mitten in derselben 3-Monats-Reihe ein Niveausprung, den
kein Marktereignis erklaert — und JEDER Indikator daraus ist Muell: RSI misst
eine Rally, die nie stattfand, MACD kreuzt, Bollinger sprengt das Band, ATR
explodiert, SMA20/50 haengen zwischen zwei Kursniveaus.

Anlassfall: Johnson Matthey (JMAT.L) am 2026-08-17 — Sonderdividende
476,5 p plus Zusammenlegung 3-fuer-4. Dort hoben sich beide Effekte
absichtlich auf (Kurs blieb flach), aber genau diese Kombination ist die
Ausnahme; ein nackter Reverse-Split 1:10 verschiebt die Reihe um +900 %.

Gegenmittel: den Sprung messen und, solange er im laengsten Indikator-Fenster
(SMA50) steckt, keine neuen BUY-Signale aus dieser Reihe zulassen.

SELBSTHEILEND by design: sobald Yahoo angepasst hat, ist der Sprung aus der
Reihe verschwunden und das Gate oeffnet wieder — ohne State, ohne Cron,
ohne Aufraeum-Job.

NICHT betroffen sind Exits. Der Exit-Pfad rechnet mit eToro-``netProfit`` /
``investmentAmount`` (``trailing_stop.execute_trailing_actions``), also mit
broker-seitig korporationsbereinigten Zahlen — dort gibt es das Problem
nicht, und ein Gate waere dort Verlustschutz-Abschalten (siehe AGENTS.md,
"BE_CLOSE/SL sind Verlustschutz").

Reine Berechnung: kein DB-Zugriff. Einzige Netz-Funktion ist
``confirm_corporate_action()`` — die wird NICHT aus ``signals.py`` gerufen
(dessen Purity-Kontrakt), sondern vom data_worker, gedeckelt pro Lauf.
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# ─── Parameter ───────────────────────────────────────────────────────────────

CA_GATE_ENABLED = True

CA_SCAN_BARS = 50          # Scan-Fenster = laengstes Indikator-Fenster (SMA50).
                           # Ein aelterer Sprung verzerrt keinen Indikator mehr.
CA_MIN_GAP_PCT = 20.0      # Pfad A: ab hier ueberhaupt verdaechtig
CA_EXTREME_GAP_PCT = 35.0  # Pfad B: so gross, dass auch ohne Ratio-Treffer gesperrt wird
CA_RATIO_TOLERANCE = 0.02  # 2 % relative Toleranz auf das Split-Verhaeltnis
CA_MAX_EXPLAINED_FRAC = 0.5  # Pfad B: Bar-Range deckt < 50 % der Bewegung ab

# Gaengige Split-Verhaeltnisse als Close-Ratio (neu / alt).
# Forward-Split → Kurs faellt (Ratio < 1), Reverse-Split → Kurs steigt (> 1).
# Bewusst knapp gehalten: jede zusaetzliche Zahl legt ein weiteres
# Toleranzband auf die Zahlengerade und damit Flaeche fuer Fehlalarme.
# Drin ist, was real als Umtauschverhaeltnis vorkommt.
_SPLIT_FACTORS: tuple[tuple[int, int], ...] = (
    (2, 1), (3, 1), (4, 1), (5, 1), (6, 1), (8, 1), (10, 1),
    (15, 1), (20, 1), (25, 1), (50, 1), (100, 1),
    (3, 2), (4, 3), (5, 4), (5, 3), (5, 2),
)


def _build_ratio_table() -> tuple[tuple[str, float], ...]:
    """(Label, Ratio) fuer Reverse- und Forward-Richtung jedes Faktors."""
    table: list[tuple[str, float]] = []
    for num, den in _SPLIT_FACTORS:
        table.append((f"{num}:{den}", num / den))    # Reverse-Split: Kurs steigt
        table.append((f"{den}:{num}", den / num))    # Forward-Split: Kurs faellt
    return tuple(table)


COMMON_SPLIT_RATIOS = _build_ratio_table()


def _nearest_split_ratio(ratio: float) -> str | None:
    """Label des naechsten gaengigen Split-Verhaeltnisses, sonst ``None``.

    ``ratio`` ist close[i] / close[i-1]. Ein Treffer heisst: der Sprung
    entspricht auf CA_RATIO_TOLERANCE genau einem glatten Umtauschverhaeltnis
    — der staerkste verfuegbare Hinweis auf eine Kapitalmassnahme, weil echte
    Marktbewegungen keinen Grund haben, exakt auf 4/3 zu landen.
    """
    best_label: str | None = None
    best_rel = CA_RATIO_TOLERANCE
    for label, cand in COMMON_SPLIT_RATIOS:
        rel = abs(ratio - cand) / cand
        if rel <= best_rel:
            best_label, best_rel = label, rel
    return best_label


# ─── Metrik (pure) ───────────────────────────────────────────────────────────

def scan_price_gaps(df: Any, window: int = CA_SCAN_BARS) -> dict:
    """Groesste Kurs-Sprungstelle der letzten *window* Bars.

    Liefert ``{}``, wenn nichts oberhalb von CA_MIN_GAP_PCT existiert — ein
    fehlender Schluessel bedeutet also "kein Verdacht" (fail-open, wie bei
    den Knife-Metriken). Keys:

    ``ca_gap_pct``            Close-zu-Close-Sprung in Prozent (mit Vorzeichen)
    ``ca_gap_ratio``          close[i] / close[i-1]
    ``ca_gap_bars_ago``       0 = letzte Bar
    ``ca_gap_split_label``    z. B. "4:3", falls das Ratio passt, sonst ``None``
    ``ca_gap_explained_frac`` (High-Low der Sprung-Bar) / |Sprunghoehe|
    """
    try:
        import pandas as pd  # noqa: F401  (nur fuer NaN-Semantik der Series)

        closes = df["Close"].dropna()
        if len(closes) < 2:
            return {}
        closes = closes.iloc[-(window + 1):]
        try:
            highs = df["High"].reindex(closes.index)
            lows = df["Low"].reindex(closes.index)
        except Exception:
            highs = lows = None

        best: dict = {}
        n = len(closes)
        for i in range(1, n):
            prev_c = float(closes.iloc[i - 1])
            cur_c = float(closes.iloc[i])
            if prev_c <= 0 or cur_c <= 0:
                continue
            ratio = cur_c / prev_c
            gap_pct = (ratio - 1.0) * 100.0
            if abs(gap_pct) < CA_MIN_GAP_PCT:
                continue
            if best and abs(gap_pct) <= abs(best["ca_gap_pct"]):
                continue

            # Wie viel der Bewegung passiert INNERHALB der Sprung-Bar? Ein
            # echter Crash handelt die Spanne aus (weite Bar), ein Split-
            # Artefakt teleportiert zwischen zwei normal breiten Bars.
            explained = None
            if highs is not None and lows is not None:
                try:
                    hi, lo = float(highs.iloc[i]), float(lows.iloc[i])
                    if hi >= lo:
                        explained = (hi - lo) / abs(cur_c - prev_c)
                except Exception:
                    pass

            best = {
                "ca_gap_pct": gap_pct,
                "ca_gap_ratio": ratio,
                "ca_gap_bars_ago": n - 1 - i,
                "ca_gap_split_label": _nearest_split_ratio(ratio),
                "ca_gap_explained_frac": explained,
            }
        return best
    except Exception:
        return {}


# ─── Gate (pure) ─────────────────────────────────────────────────────────────

def is_corporate_action_artifact(indicators: dict) -> tuple[bool, str]:
    """Prueft die Gap-Metriken. Gibt ``(is_artifact, reason)`` zurueck.

    Drei Ausloeser, in dieser Reihenfolge:

    C  ``ca_confirmed`` ist gesetzt — eine echte, materielle Kapitalmassnahme
       aus Yahoos Action-Historie (``ConfirmBudget.annotate``). Grundwahrheit,
       schlaegt jede Heuristik und braucht keinen Ratio-Treffer.

    A  Sprung >= CA_MIN_GAP_PCT UND Ratio trifft ein glattes Split-Verhaeltnis.
       Der praezise heuristische Fall — Falsch-Positive brauchen einen echten
       Move, der zufaellig auf 2 % genau auf 1/2, 4/3, 10:1 … landet.

    B  Sprung >= CA_EXTREME_GAP_PCT, den die Bar-Spanne nicht erklaert. Netz
       fuer fehlerhafte Yahoo-Reihen, wenn keine Action-Historie vorliegt.

    Warum C noetig ist: bei JMAT.L wirkten am 2026-08-17 Zusammenlegung (0.75)
    und Sonderdividende (476,5 p) GEMEINSAM. Das Ergebnis (-21,9 %, Ratio
    0.7806) ist deshalb weder ein glattes Verhaeltnis (verfehlt 3:4 um 4,1 %)
    noch extrem genug fuer B — die Heuristik allein haette den Anlassfall
    durchgelassen.

    Fehlende Metriken = kein Artefakt (fail-open). Der Preis eines Fehlalarms
    ist ein paar Tage kein BUY in diesem Symbol; der Preis eines uebersehenen
    Artefakts ist ein Trade auf frei erfundenen Indikatoren.
    """
    if not CA_GATE_ENABLED:
        return False, ""

    confirmed = indicators.get("ca_confirmed")
    if confirmed:
        return True, f"bestaetigte Kapitalmassnahme ({confirmed})"

    gap = indicators.get("ca_gap_pct")
    if gap is None:
        return False, ""

    label = indicators.get("ca_gap_split_label")
    bars_ago = indicators.get("ca_gap_bars_ago")
    age = f", vor {bars_ago} Bars" if bars_ago is not None else ""

    if label and abs(gap) >= CA_MIN_GAP_PCT:
        return True, f"Kurssprung {gap:+.1f}% = Split-Verhaeltnis {label}{age}"

    explained = indicators.get("ca_gap_explained_frac")
    if abs(gap) >= CA_EXTREME_GAP_PCT and (
        explained is None or explained < CA_MAX_EXPLAINED_FRAC
    ):
        return True, f"unerklaerter Kurssprung {gap:+.1f}%{age}"

    return False, ""


# ─── Bestaetigung gegen bekannte Corporate Actions (Netz) ────────────────────

CA_CONFIRM_LOOKBACK_DAYS = 60
CA_CONFIRM_MAX_PER_RUN = 25     # Deckel: der Abruf ist ein Netz-Call je Symbol
CA_MATERIAL_DIV_PCT = 5.0       # Dividende erst ab 5 % vom Kurs ist ein Artefakt-Kandidat


def confirm_corporate_action(
    yf_symbol: str,
    price: float | None = None,
    lookback_days: int = CA_CONFIRM_LOOKBACK_DAYS,
) -> str | None:
    """Sucht MATERIELLE Kapitalmassnahmen in Yahoos Action-Historie.

    Materiell heisst: jeder Split, aber nur Dividenden ab CA_MATERIAL_DIV_PCT
    des aktuellen Kurses. Ohne diesen Filter wuerde jeder normale
    Quartalszahler (AAPL & Co.) als "bestaetigt" durchgehen und das Gate
    waere ein Zufallsgenerator.

    Gibt eine kurze Beschreibung zurueck oder ``None`` (nichts Materielles
    gefunden bzw. Abruf fehlgeschlagen — fail-open, ein Netz-Call darf nie
    zwischen dem Bot und einer Entscheidung stehen).
    """
    try:
        import pandas as pd
        import yfinance as yf

        cutoff = pd.Timestamp.now('UTC') - pd.Timedelta(days=lookback_days)
        ticker = yf.Ticker(yf_symbol)
        parts: list[str] = []

        try:
            splits = ticker.splits
            if splits is not None and len(splits):
                idx = pd.to_datetime(splits.index, utc=True)
                for ts, factor in splits[idx >= cutoff].items():
                    if float(factor) > 0:
                        parts.append(f"Split {float(factor):g}x am {ts.date()}")
        except Exception:
            pass

        try:
            divs = ticker.dividends
            if divs is not None and len(divs):
                idx = pd.to_datetime(divs.index, utc=True)
                for ts, amount in divs[idx >= cutoff].items():
                    amount = float(amount)
                    if price and price > 0:
                        pct = amount / float(price) * 100.0
                        if pct < CA_MATERIAL_DIV_PCT:
                            continue
                        parts.append(
                            f"Dividende {amount:g} ({pct:.1f}% vom Kurs) am {ts.date()}"
                        )
                    # Ohne Kursbezug ist Materialitaet nicht beurteilbar →
                    # Dividende zaehlt nicht, sonst Fehlalarm bei jedem Zahler.
        except Exception:
            pass

        return "; ".join(parts) if parts else None
    except Exception as exc:
        logger.debug("[corporate_actions] %s: Bestaetigung fehlgeschlagen — %s", yf_symbol, exc)
        return None


def needs_action_confirmation(indicators: dict) -> bool:
    """True, wenn die Reihe einen Sprung zeigt, der eine Nachfrage rechtfertigt.

    Haelt die Netz-Calls auf die Handvoll auffaelliger Symbole je Lauf
    begrenzt — ohne Sprung wird nichts abgefragt.
    """
    if not CA_GATE_ENABLED:
        return False
    gap = indicators.get("ca_gap_pct")
    return gap is not None and abs(gap) >= CA_MIN_GAP_PCT


class ConfirmBudget:
    """Gedeckelter Bestaetigungs-Abruf fuer einen Worker-Lauf.

    Ein Worker legt EINE Instanz je Lauf an und ruft ``annotate()`` fuer jedes
    Symbol mit Gap. Der Deckel schuetzt das Cron-Zeitbudget (data_worker hat
    ~120 s, siehe FETCH_DEADLINE_S) an Tagen, an denen ein Marktbeben viele
    Symbole gleichzeitig auffaellig macht.
    """

    def __init__(self, limit: int = CA_CONFIRM_MAX_PER_RUN):
        self.limit = limit
        self.used = 0
        self.hits = 0

    def annotate(self, yf_symbol: str, indicators: dict) -> bool:
        """Schreibt ``ca_confirmed`` in *indicators*, wenn bestaetigt.

        Gibt True zurueck, wenn eine materielle Kapitalmassnahme gefunden
        wurde. Ist das Budget erschoepft, passiert nichts — die Heuristik
        (Pfad A/B) bleibt als Netz aktiv.
        """
        if not needs_action_confirmation(indicators) or self.used >= self.limit:
            return False
        self.used += 1
        found = confirm_corporate_action(yf_symbol, price=indicators.get("price"))
        if found:
            indicators["ca_confirmed"] = found
            self.hits += 1
            return True
        return False
