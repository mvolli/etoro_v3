#!/usr/bin/env python3
"""eToro Trading Bot V3 — Edge-Gate (LIVE-MODUS)
src/bot/core/edge_gate.py

Phase 3 (2026-10-09): Deterministisches Edge-Gate pro Signaltyp-Familie.
D6 (2026-10-09, VoLLi): Shadow → LIVE-Umschaltung.

Fragt fuer einen Signal-Typ die netto Forward-Returns (fwd_5d_pnl, 1.1 %
Kosten) aus signal_outcomes (Phase 1a hat die Tabelle befuellt; Fallback:
trade_events) und berechnet:

    exp_net : Mittelwert der netten 5d-Forward-Returns
    lcb     : Lower Confidence Bound 95 % einseitig (z = 1.645), SE
              pro Handelstag geclustert (seit 2026-10-16, _cluster_lcb)
    n       : Stichprobenumfang

Entscheidung (LIVE-MODUS seit 2026-10-09, VoLLi-Freigabe D6):

    n < n_min   -> SHADOW   (nicht fail-open: zu wenig Daten = keine Freigabe)
    lcb < 0     -> SHADOW   (keine nachweisbar positive Kante)
    sonst       -> LIVE     (freigegeben)

LIVE-MODUS: Das Gate blockt Shadow-Trades in der Execution-Kette
(execution_worker b3b). CORE_SWEEP ist ausgenommen (D4: VoLLi-Freigabe,
eigene Schutzregeln 0.25x + Cap 3 + Abort).

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

# D6 (2026-10-09, VoLLi): LIVE-MODUS. True = Gate blockt Shadow-Trades.
# CORE_SWEEP ist ausgenommen (D4: eigene Schutzregeln, VoLLi-Freigabe).
LIVE_MODE = True

# D4: CORE_SWEEP ist vom Edge-Gate ausgenommen — VoLLi hat D4 (CORE_SWEEP
# 0.25x DEFENSIVE + Cap 3 + Abort) explizit befreit. CORE_SWEEP hat
# LCB −0.0014 (knapp negativ), ist aber der einzige klar positive Pfad
# (+$192, WR 47 %). Die D4-Schutzregeln (0.25x, Cap 3, DD>150/WR<30%)
# ersetzen die Gate-Blockade fuer diesen Typ.
# (Korrektur 2026-10-16: "+$192" war Ledger; Broker brutto −$118 seit 10.09.,
#  siehe docs/ANALYSE-gewinnzone-2026-10-16.md. Ausnahme bleibt VoLLi-Entscheid.)
# feat/core-index (2026-10-16): CORE_INDEX ist kein Signal-Trade, sondern ein
# Marktexposure-Kern (VoLLi-Entscheid 16.10.). Ein 5-Tage-Forward-Edge ist dafuer
# das falsche Mass; seine Schutzregeln liegen in core_index.plan_tranche.
GATE_EXEMPT_TYPES = frozenset({"CORE_SWEEP", "CORE_INDEX"})

# feat/signal-trades (VoLLi 2026-10-17): die in trading.signal_trades
# zugelassenen Familien sind ausgenommen (Komponenten-Menge exakt). VoLLi hat
# sie trotz negativer Live-Statistik bewusst freigegeben (E5: absolut leicht
# positiv bei ~$880 und "laufen lassen"). None = noch nicht geladen.
_EXTRA_EXEMPT: set | None = None


def _extra_exempt_sets() -> set:
    global _EXTRA_EXEMPT
    if _EXTRA_EXEMPT is None:
        try:
            import yaml
            from bot.config import _PROJECT_ROOT
            from bot.core.signal_trades import load_config
            raw = yaml.safe_load((_PROJECT_ROOT / "config" / "config.yaml").read_text()) or {}
            st = load_config(raw)
            _EXTRA_EXEMPT = set(st["family_sets"]) if st["enabled"] else set()
        except Exception as exc:  # noqa: BLE001
            logger.debug("edge_gate: signal_trades-Ausnahmen nicht geladen: %s", exc)
            _EXTRA_EXEMPT = set()
    return _EXTRA_EXEMPT


def is_exempt(signal_type: str) -> bool:
    """CORE_SWEEP/CORE_INDEX oder eine zugelassene signal_trades-Familie."""
    st = (signal_type or "").upper()
    return st in GATE_EXEMPT_TYPES or _components(st) in _extra_exempt_sets()


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


def _cluster_lcb(samples: list[tuple[float, str]]) -> tuple[float, float, int]:
    """(Mittel, LCB 95 % einseitig, Anzahl Cluster) mit Cluster-robustem SE.

    feat/edge-gate-cluster (2026-10-16, VoLLi): Signale desselben Handelstags
    sind nicht unabhaengig — an einem schlechten Markttag fallen viele
    gemeinsam. Der i.i.d.-SE unterschaetzte die Unsicherheit (MACD-Familie:
    537 Signale, aber nur 51 Tage) und haette einen Typ knapp ueber null zu
    leicht freigegeben. Mittel bleibt das gepoolte (jeder Trade gleich
    gewichtet, wie das Geld eingesetzt wird); nur der SE wird pro Tag
    geclustert (Liang-Zeger mit G/(G-1)-Korrektur). G < 2 -> LCB = -inf.
    """
    n = len(samples)
    if n == 0:
        return 0.0, 0.0, 0
    mean = sum(v for v, _ in samples) / n
    resid: dict[str, float] = {}
    for v, key in samples:
        resid[key] = resid.get(key, 0.0) + (v - mean)
    g = len(resid)
    if g < 2:
        return mean, float("-inf"), g
    var = sum(r * r for r in resid.values()) / (n * n) * g / (g - 1)
    return mean, mean - Z_ONE_SIDED_95 * math.sqrt(var), g


def _collect_from_signal_outcomes(db, signal_type: str) -> list[tuple[float, str]]:
    """(Netto-Forward-Return, Signaltag) fuer signal_type (exakter String,
    dann Komponenten-Superset als Datenerweiterung bei Sparse-Typs)."""
    exact: list[tuple[float, str]] = []
    try:
        rows = db.fetchall(
            f"SELECT {FORWARD_COL}, signal_date FROM signal_outcomes "
            f"WHERE signal_type = ? AND {FORWARD_COL} IS NOT NULL",
            (signal_type,),
        )
        exact = [(float(r[FORWARD_COL]), str(r["signal_date"])[:10]) for r in rows]
    except Exception as exc:  # noqa: BLE001 — Fallback soll nie werfen
        logger.debug("edge_gate: signal_outcomes exakt nicht abrufbar: %s", exc)
    if len(exact) >= N_MIN:
        return exact
    # Datenerweiterung: Zeilen, deren gespeicherter Typ ALLE Komponenten des
    # abgefragten Typs enthaelt (Kombo-Signale zaehlen auf ihre Familien).
    comp = _components(signal_type)
    if not comp:
        return exact
    broad: list[tuple[float, str]] = []
    try:
        rows = db.fetchall(
            f"SELECT signal_type, {FORWARD_COL}, signal_date FROM signal_outcomes "
            f"WHERE {FORWARD_COL} IS NOT NULL"
        )
        for r in rows:
            if comp <= _components(str(r["signal_type"])):
                broad.append((float(r[FORWARD_COL]), str(r["signal_date"])[:10]))
    except Exception as exc:  # noqa: BLE001
        logger.debug("edge_gate: signal_outcomes Broadening fehlgeschlagen: %s", exc)
    return broad if len(broad) > len(exact) else exact


def _collect_from_trade_events(db, signal_type: str) -> list[tuple[float, str]]:
    """Fallback: geschlossene Trades (CLOSE) mit pnl_pct, gruppiert ueber
    trade_events -> trades -> signals.signal_type (exakter String).
    Cluster = Tag des Close (event_at); ohne Spalte je Zeile ein Cluster."""
    try:
        cols = {r["name"] for r in db.fetchall("PRAGMA table_info(trade_events)")}
        day = "substr(e.event_at, 1, 10)" if "event_at" in cols else "e.rowid"
        rows = db.fetchall(f"""
            SELECT e.pnl_pct, {day} AS k
            FROM trade_events e
            JOIN trades t  ON t.id = e.trade_id
            JOIN signals s ON s.id = t.signal_id
            WHERE e.event_type = 'CLOSE'
              AND e.pnl_pct IS NOT NULL
              AND s.signal_type = ?
        """, (signal_type,))
        return [(float(r["pnl_pct"]), str(r["k"])) for r in rows]
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
    exp_net, lcb, _g = _cluster_lcb(samples)
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


def blocks(res: dict) -> bool:
    """LIVE-Blockade-Entscheidung aus einem evaluate()-Ergebnis."""
    return bool(res.get("live_mode")) and bool(res.get("shadow")) and not res.get("exempt")


def mark_shadow(db, signal_id, shadow: bool) -> None:
    """Gate-Entscheidung an die signal_outcomes-Zeile des Signals schreiben.

    Exakte Zeile (signal_date + instrument_id ueber signals), nicht alle
    Zeilen desselben Typs. No-op ohne Spalte/Zeile; wirft nie.
    """
    if not signal_id:
        return
    try:
        cols = {r["name"] for r in db.fetchall("PRAGMA table_info(signal_outcomes)")}
        if "edge_shadow" not in cols:
            return
        db.execute(
            """
            UPDATE signal_outcomes SET edge_shadow = ?
            WHERE id = (
                SELECT so.id FROM signal_outcomes so
                JOIN signals s
                  ON s.instrument_id = so.instrument_id
                 AND s.signal_type = so.signal_type
                 AND s.generated_at LIKE so.signal_date || '%'
                WHERE s.id = ?
                ORDER BY so.signal_date DESC
                LIMIT 1
            )
            """,
            (1 if shadow else 0, signal_id),
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("edge_gate: Shadow-Kennzeichnung fehlgeschlagen: %s", exc)


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
        # fix/edge-gate-n0-keys (2026-10-14): die n=0-Frueh-Rueckgabe
        # hatte live_mode + exempt NICHT — execution_worker (913/914)
        # warf KeyError -> outer except -> FAIL-OPEN (Trade laeuft). Das
        # widersprach dem Gate-Vertrag "n < n_min -> SHADOW (nicht
        # fail-open)": ein Typ ohne Outcome-Daten ist n=0 < n_min=25 und
        # muess in LIVE-Modus SHADOW-blocken (wenn nicht exempt), nicht
        # durchfallen. Keys jetzt konsistent zum Haupt-Pfad.
        #
        # Leerer/nicht auflösbarer Signal-Typ (fehlendes signal_id) ist
        # eine Daten-Lücke, KEIN Edge-Beweis. Fail-open (Alt-Verhalten),
        # damit ein D4-exempter Core-Sweep-Trade mit nicht gelöster
        # signal_id nicht still blockiert wird (D4 nicht verschärfen —
        # Advisor-Gegenprobe 3a, 2026-10-14).
        if not (signal_type or "").strip():
            return {
                "signal_type": signal_type, "regime": regime,
                "exp_net": 0.0, "lcb": 0.0, "n": 0,
                "shadow": False,
                "reason": "leerer Signal-Typ (fail-open)",
                "source": source,
                "live_mode": LIVE_MODE,
                "exempt": False,
            }
        return {
            "signal_type": signal_type, "regime": regime,
            "exp_net": 0.0, "lcb": 0.0, "n": 0,
            "shadow": True, "reason": "keine Outcome-Daten",
            "source": source,
            "live_mode": LIVE_MODE,
            "exempt": is_exempt(signal_type),
        }
    exp_net, lcb, n_days = _cluster_lcb(samples)
    if n < N_MIN:
        shadow, reason = True, f"n={n} < n_min={N_MIN} (nicht fail-open)"
    elif n_days < 2:
        shadow, reason = True, f"nur {n_days} Handelstag (kein Cluster-SE)"
    elif lcb < 0.0:
        shadow, reason = True, f"LCB={lcb:.5f} < 0 (keine positive Kante)"
    else:
        shadow, reason = False, "nreich und LCB > 0"
    return {
        "signal_type": signal_type, "regime": regime,
        "exp_net": exp_net, "lcb": lcb, "n": n,
        "shadow": shadow, "reason": reason, "source": source,
        "n_days": n_days,
        "live_mode": LIVE_MODE,
        "exempt": is_exempt(signal_type),
    }
