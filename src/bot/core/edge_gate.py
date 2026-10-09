#!/usr/bin/env python3
"""eToro Trading Bot V3 — Edge-Gate (SHADOW-MODUS)
src/bot/core/edge_gate.py

Phase 3 (2026-10-09): Deterministisches Edge-Gate pro Signaltyp-Familie.

Fragt fuer einen Signal-Typ die netto Forward-Returns (fwd_5d_pnl, 1.1 %
Kosten) aus signal_outcomes (Phase 1a hat die Tabelle befuellt; Fallback:
trade_events) und berechnet:

    exp_net : Mittelwert der netten 5d-Forward-Returns
    lcb     : Lower Confidence Bound 95 % einseitig (z = 1.645)
    n       : Stichprobenumfang

Entscheidung (SHADOW-MODUS — keine Live-Blockade, nur Log + Kennzeichnung):

    n < n_min   -> SHADOW   (nicht fail-open: zu wenig Daten = keine Freigabe)
    lcb < 0     -> SHADOW   (keine nachweisbar positive Kante)
    sonst       -> LIVE     (freigegeben)

Warum SHADOW-MODUS: D6 — 24-h-Beobachtungsfenster des Fee-Fixes 7cd4cda
(Ende ~14:20 UTC am 10.10.). Das Gate laeuft parallel zur Live-Kette und
kennzeichnet Schatten-Trades in signal_outcomes, blockt aber nichts. Nach
dem Fenster wird der Shadow->Live-Umschalter dokumentiert (Abschluss-Report).

Regime-Konditionierung: signal_outcomes traegt (noch) keine Regime-Spalte,
deshalb ist `regime` ein Kontext-Label, das in die Shadow-Kennzeichnung
geschrieben wird; die Edge-Statistik laeuft ueber die volle Historie.
Konditionierung nach Regime ist eine ausdruemlich dokumentierte Erweiterung.
"""
from __future__ import annotations

import logging
import math
from typing import Iterable

logger = logging.getLogger("edge_gate")

# ── Konstanten ────────────────────────────────────────────────────────────────
Z_ONE_SIDED_95 = 1.645          # einseitiges 95 %-Quantil (Standardnormal)
N_MIN = 25                      # Mindeststichprobenumfang, sonst SHADOW
FORWARD_COL = "fwd_5d_pnl"      # netter 5d-Forward-Return (1.1 % Kosten)


def _components(signal_type: str) -> set[str]:
    """Komponenten-Menge eines Signal-Typs (Komma-Kombo wird gesplittet).

    Bewusst lokal (core bleibt workers-unabhaengig), logisch identisch mit
    signal_worker._quota_components — Kombo-Matching auf Familien-Ebene.
    """
    return {p.strip().upper() for p in (signal_type or "").split(",") if p.strip()}


def _std(samples: list[float]) -> float:
    """Sample-Standardabweichung (ddof=1); 0 bei n<2."""
    if len(samples) < 2:
        return 0.0
    mean = sum(samples) / len(samples)
    var = sum((x - mean) ** 2 for x in samples) / (len(samples) - 1)
    return math.sqrt(var)


def _lcb(exp_net: float, std: float, n: int) -> float:
    """Lower Confidence Bound 95 % einseitig: mean - z * (std/sqrt(n))."""
    if n < 1:
        return exp_net
    return exp_net - Z_ONE_SIDED_95 * std / math.sqrt(n)


def _collect_from_signal_outcomes(db, signal_type: str) -> list[float]:
    """Netto-Forward-Returns fuer signal_type (exakter String, dann
    Komponenten-Superset als Datenerweiterung bei Sparse-Typs)."""
    exact: list[float] = []
    try:
        rows = db.fetchall(
            f"SELECT {FORWARD_COL} FROM signal_outcomes "
            f"WHERE signal_type = ? AND {FORWARD_COL} IS NOT NULL",
            (signal_type,),
        )
        exact = [float(r[FORWARD_COL]) for r in rows]
    except Exception as exc:  # noqa: BLE001 — Fallback soll nie werfen
        logger.debug("edge_gate: signal_outcomes exakt nicht abrufbar: %s", exc)
    if len(exact) >= N_MIN:
        return exact
    # Datenerweiterung: Zeilen, deren gespeicherter Typ ALLE Komponenten des
    # abgefragten Typs enthaelt (Kombo-Signale zaehlen auf ihre Familien).
    comp = _components(signal_type)
    if not comp:
        return exact
    broad: list[float] = []
    try:
        rows = db.fetchall(
            f"SELECT signal_type, {FORWARD_COL} FROM signal_outcomes "
            f"WHERE {FORWARD_COL} IS NOT NULL"
        )
        for r in rows:
            if comp <= _components(str(r["signal_type"])):
                broad.append(float(r[FORWARD_COL]))
    except Exception as exc:  # noqa: BLE001
        logger.debug("edge_gate: signal_outcomes Broadening fehlgeschlagen: %s", exc)
    return broad if len(broad) > len(exact) else exact


def _collect_from_trade_events(db, signal_type: str) -> list[float]:
    """Fallback: geschlossene Trades (CLOSE) mit pnl_pct, gruppiert ueber
    trade_events -> trades -> signals.signal_type (exakter String)."""
    try:
        rows = db.fetchall("""
            SELECT e.pnl_pct
            FROM trade_events e
            JOIN trades t  ON t.id = e.trade_id
            JOIN signals s ON s.id = t.signal_id
            WHERE e.event_type = 'CLOSE'
              AND e.pnl_pct IS NOT NULL
              AND s.signal_type = ?
        """, (signal_type,))
        return [float(r["pnl_pct"]) for r in rows]
    except Exception as exc:  # noqa: BLE001
        logger.debug("edge_gate: trade_events-Fallback fehlgeschlagen: %s", exc)
        return []


def type_edge(signal_type: str, regime: str, db) -> tuple[float, float, int]:
    """Edge-Statistik fuer einen Signal-Typ.

    Gibt (exp_net, lcb, n) zurueck. Basis ist signal_outcomes (netter
    5d-Forward-Return); bei leerer/fehlender Tabelle Fallback auf
    trade_events. `regime` ist ein Kontext-Label (siehe Modul-Doku) und
    aendert die Statistik nicht.
    """
    samples = _collect_from_signal_outcomes(db, signal_type)
    if not samples:
        samples = _collect_from_trade_events(db, signal_type)
    n = len(samples)
    if n == 0:
        return (0.0, 0.0, 0)
    exp_net = sum(samples) / n
    lcb = _lcb(exp_net, _std(samples), n)
    return (exp_net, lcb, n)


def is_shadow(exp_net: float, lcb: float, n: int,
              n_min: int = N_MIN) -> bool:
    """SHADOW-Entscheidung (nicht fail-open).

    True  -> Schatten (zu wenig Daten ODER keine nachweisbar positive Kante)
    False -> Live (nreich und LCB > 0)
    """
    if n < n_min:
        return True
    if lcb < 0.0:
        return True
    return False


def evaluate(signal_type: str, regime: str, db) -> dict:
    """Vollstaendige Gate-Bewertung als Dict (fuer Log + Kennzeichnung).

    Keys: signal_type, regime, exp_net, lcb, n, shadow (bool), reason,
    source ('signal_outcomes' | 'trade_events' | 'none').
    """
    from_outcomes = _collect_from_signal_outcomes(db, signal_type)
    if from_outcomes:
        samples, source = from_outcomes, "signal_outcomes"
    else:
        samples = _collect_from_trade_events(db, signal_type)
        source = "trade_events" if samples else "none"

    n = len(samples)
    if n == 0:
        return {
            "signal_type": signal_type, "regime": regime,
            "exp_net": 0.0, "lcb": 0.0, "n": 0,
            "shadow": True, "reason": "keine Outcome-Daten",
            "source": source,
        }
    exp_net = sum(samples) / n
    lcb = _lcb(exp_net, _std(samples), n)
    if n < N_MIN:
        shadow, reason = True, f"n={n} < n_min={N_MIN} (nicht fail-open)"
    elif lcb < 0.0:
        shadow, reason = True, f"LCB={lcb:.5f} < 0 (keine positive Kante)"
    else:
        shadow, reason = False, "nreich und LCB > 0"
    return {
        "signal_type": signal_type, "regime": regime,
        "exp_net": exp_net, "lcb": lcb, "n": n,
        "shadow": shadow, "reason": reason, "source": source,
    }
