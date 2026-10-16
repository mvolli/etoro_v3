"""Core-Index — vom Bot verwalteter, langfristiger Index-Kern (feat/core-index, 2026-10-16).

WARUM: Die Analyse vom 16.10. (`docs/ANALYSE-gewinnzone-2026-10-16.md`) fand
keinen Signaltyp mit positivem Erwartungswert nach Kosten; das Edge-Gate haelt
Cash deshalb zu Recht zurueck (~89 % Cash). Positiver Erwartungswert ohne
bewiesenen Signal-Edge entsteht nur ueber Marktexposure mit wenig Umschlag.
VoLLi hat am 16.10. den bot-verwalteten Kern gewaehlt.

Was das Modul tut
-----------------
* Plant Tranchen (Kauf-only), bis der Kern ``target_pct`` der Equity erreicht.
  Eine Tranche liegt nie ueber dem ``max_trade_pct`` des aktuellen Regimes,
  hoechstens eine pro UTC-Tag, keine solange ein Kern-Trade APPROVED/SUBMITTING
  ist, nie unter die Cash-Reserve. CRITICAL (inkl. Kill-Switch) -> nichts.
* Fuehrt die Kern-Positionen (Trades mit synthetischem Signal ``CORE_INDEX``)
  und nimmt sie aus allen Ausstiegspfaden heraus (risk_worker filtert die
  Broker-Positionen; ``client.close_position`` verweigert Kern-Positionen als
  Sicherheitsnetz) sowie aus Exposure/Positionszahl/Diversitaet.
* KEINE automatischen Verkaeufe/Trims. Schutz nach unten: weiter Broker-Stop
  (``sl_pct``, Default 25 %), gesetzt beim Kauf.

Modi (``core_index.mode``): ``off`` (Default, nichts) | ``shadow`` (plant und
loggt, legt keinen Trade an) | ``live``. Alle Werte sind geldwirksam ->
VoLLi-Entscheidung; die Keys stehen NICHT in BIBLE_HARD_LIMITS, die
LLM-Selbstoptimierung kann sie also nicht aendern.

Equity, Regime und Verlustlimits enthalten den Kern weiterhin (Marktbewegung
des Kerns zaehlt). Groesse deshalb an den Puffern bis 12 %-Monats-/15 %-CRITICAL-
Schwelle ausrichten (siehe Analyse-Doc).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Iterable, Optional

logger = logging.getLogger(__name__)

SIGNAL_TYPE = "CORE_INDEX"
VALID_MODES = ("off", "shadow", "live")

DEFAULT_CONFIG: dict = {
    "mode": "off",            # off | shadow | live
    "symbol": "",             # USD-gelistetes Breit-ETF, z. B. ACWI / VTI
    "instrument_id": 0,
    "target_pct": 0.0,        # Zielgroesse des Kerns in % Equity
    "band_pct": 2.0,          # nachkaufen erst, wenn Luecke > band_pct der Equity
    "sl_pct": 25.0,           # Broker-Stop-Abstand beim Kauf (Bot-Gate: < 50 %)
    "min_order_usd": 200.0,   # kleinere Tranchen lohnen die Order nicht
    "min_cash_pct": 18.0,     # Cash-Reserve nach dem Kauf (wie core_sweep)
}


def load_config(cfg: Optional[dict]) -> dict:
    """DEFAULT_CONFIG flach mit ``cfg['core_index']`` gemischt. Fail-safe:
    unbekannter Modus -> 'off'."""
    out = dict(DEFAULT_CONFIG)
    sec = ((cfg or {}).get("core_index") or {}) if isinstance(cfg, dict) else {}
    for k in DEFAULT_CONFIG:
        if k in sec and sec[k] is not None:
            out[k] = sec[k]
    if out.get("mode") is False:  # YAML liest unquotiertes off als false
        out["mode"] = "off"
    if str(out.get("mode")) not in VALID_MODES:
        logger.error("core_index.mode=%r ungueltig — 'off'", out.get("mode"))
        out["mode"] = "off"
    return out


# ── Kern-Positionen ──────────────────────────────────────────────────────────
_CORE_IDS_SQL = """
    SELECT t.api_position_id AS pid
    FROM trades t JOIN signals s ON s.id = t.signal_id
    WHERE s.signal_type = ? AND t.api_position_id IS NOT NULL
      AND t.status NOT IN ('CLOSED', 'FAILED', 'REJECTED', 'CANCELLED')
"""


def core_position_ids(db: Any) -> set[str]:
    """api_position_ids aller offenen Kern-Positionen. Fail-safe: leeres Set
    bei DB-Fehler (dann greift das Sicherheitsnetz in close_position, das
    seinerseits fail-closed prueft)."""
    if db is None:
        return set()
    try:
        rows = db.fetchall(_CORE_IDS_SQL, (SIGNAL_TYPE,))
        return {str(r["pid"]) for r in rows or [] if r["pid"]}
    except Exception as exc:
        logger.warning("core_index: Kern-Positionen nicht lesbar: %s", exc)
        return set()


def is_core_position(db: Any, position_id: Any) -> bool:
    if position_id is None:
        return False
    return str(position_id) in core_position_ids(db)


def _pos_id(p: dict) -> Optional[str]:
    v = p.get("positionID") or p.get("positionId") or p.get("api_position_id")
    return str(v) if v is not None else None


def exclude_core(positions: Iterable[dict], core_ids: set[str]) -> list[dict]:
    """Positionen ohne Kern-Positionen (Broker-Payload oder Snapshot-Zeilen)."""
    if not core_ids:
        return list(positions)
    return [p for p in positions if _pos_id(p) not in core_ids]


def core_exposure(positions: Iterable[dict], core_ids: set[str]) -> tuple[float, int]:
    """(Summe amount_usd/amount, Anzahl) der Kern-Positionen in ``positions``."""
    total, n = 0.0, 0
    for p in positions:
        if _pos_id(p) in core_ids:
            n += 1
            try:
                total += float(p.get("amount_usd") if p.get("amount_usd") is not None
                               else p.get("amount") or 0.0)
            except (TypeError, ValueError):
                pass
    return total, n


# ── Planung ──────────────────────────────────────────────────────────────────
@dataclass
class TranchePlan:
    amount_usd: Optional[float]   # None = keine Tranche
    reason: str
    target_usd: float = 0.0
    core_usd: float = 0.0


def plan_tranche(*, cfg: dict, equity: float, cash: float, core_usd: float,
                 regime: str, regime_max_trade_pct: float,
                 pending_core_trade: bool, bought_today: bool) -> TranchePlan:
    """Reine Entscheidung (keine I/O). ``cfg`` = Ergebnis von load_config()."""
    mode = cfg.get("mode", "off")
    if mode == "off":
        return TranchePlan(None, "mode=off")
    if not cfg.get("symbol") or not int(cfg.get("instrument_id") or 0):
        return TranchePlan(None, "kein symbol/instrument_id konfiguriert")
    if equity <= 0:
        return TranchePlan(None, "equity <= 0 (fail-closed)")
    target = equity * float(cfg.get("target_pct") or 0.0) / 100.0
    gap = target - core_usd
    base = TranchePlan(None, "", target_usd=round(target, 2), core_usd=round(core_usd, 2))
    if target <= 0:
        base.reason = "target_pct=0"; return base
    if str(regime).upper() == "CRITICAL":
        base.reason = "Regime CRITICAL — keine Kaeufe"; return base
    if pending_core_trade:
        base.reason = "Kern-Trade noch APPROVED/SUBMITTING"; return base
    if bought_today:
        base.reason = "heute schon eine Tranche"; return base
    band = equity * float(cfg.get("band_pct") or 0.0) / 100.0
    if gap <= band:
        base.reason = f"Kern ${core_usd:.0f} im Band (Ziel ${target:.0f}, Band ${band:.0f})"
        return base
    cap = equity * float(regime_max_trade_pct or 0.0) / 100.0
    floor_cash = equity * float(cfg.get("min_cash_pct") or 0.0) / 100.0
    amount = min(gap, cap, max(0.0, cash - floor_cash))
    min_order = float(cfg.get("min_order_usd") or 0.0)
    if amount < min_order:
        base.reason = (f"Tranche ${amount:.0f} < min_order ${min_order:.0f} "
                       f"(Luecke ${gap:.0f}, Regime-Cap ${cap:.0f}, Cash ueber Reserve "
                       f"${max(0.0, cash - floor_cash):.0f})")
        return base
    base.amount_usd = round(amount, 2)
    base.reason = (f"Tranche ${amount:.0f} (Ziel ${target:.0f}, Kern ${core_usd:.0f}, "
                   f"Regime-Cap ${cap:.0f})")
    return base


def pending_core_trade(db: Any) -> bool:
    """True, wenn ein Kern-Trade noch auf Ausfuehrung wartet. Fail-closed."""
    try:
        row = db.fetchone(
            "SELECT COUNT(*) AS n FROM trades t JOIN signals s ON s.id = t.signal_id "
            "WHERE s.signal_type = ? AND t.status IN ('APPROVED','SUBMITTING','PENDING')",
            (SIGNAL_TYPE,))
        return bool(row and row["n"])
    except Exception as exc:
        logger.warning("core_index: pending-Pruefung fehlgeschlagen (blockiert): %s", exc)
        return True


def bought_today(db: Any) -> bool:
    """True, wenn heute (UTC) schon ein Kern-Trade angelegt wurde. Fail-closed."""
    try:
        row = db.fetchone(
            "SELECT COUNT(*) AS n FROM trades t JOIN signals s ON s.id = t.signal_id "
            "WHERE s.signal_type = ? AND date(t.created_at) = date('now') "
            "AND t.status NOT IN ('REJECTED','FAILED','CANCELLED')",
            (SIGNAL_TYPE,))
        return bool(row and row["n"])
    except Exception as exc:
        logger.warning("core_index: Tages-Pruefung fehlgeschlagen (blockiert): %s", exc)
        return True
