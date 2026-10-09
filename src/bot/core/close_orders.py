"""Close-Order-Wächter (Phase 0a, 2026-10-09).

Problem: Der Bot liest `ordersForClose` aus dem Portfolio nirgends.
Wenn ein Close-Order beim Broker wartet (z.B. ASX/Tokyo nachts), feuert
der nächste Worker-Zyklus (risk_worker 5-min-Takt, LLM-Runde, sell_exits)
einen ZWEITEN Close auf dieselbe Position → Doppel-Buchung, Überverkauf.

Lösung:
  1. Neue Tabelle `close_orders` speichert alle wartenden Close-Orders
     (aus `clientPortfolio.ordersForClose` des Broker).
  2. `sync_close_orders()` wird im Reconciler nach jedem Portfolio-Fetch
     aufgerufen und hält die Tabelle aktuell:
       - Neue Orders → INSERT (status=OPEN)
       - Orders die aus der API-Liste verschwunden sind → EXECUTED
  3. `has_open_close_order()` ist das GATE: wird vor JEDEM
     `close_position()`-Call geprüft. Open-Order vorhanden → kein
     zweiter Close.

Alle Funktionen sind pure (keine API-Calls), damit sie ohne Mocks
testbar sind.
"""
from __future__ import annotations

import logging
import sqlite3
from typing import Any

logger = logging.getLogger(__name__)

# ── Table schema ──────────────────────────────────────────────────────────────

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS close_orders (
    order_id        TEXT PRIMARY KEY,
    position_id     TEXT NOT NULL,
    symbol          TEXT,
    instrument_id   INTEGER,
    placed_at       TEXT,
    status          TEXT NOT NULL DEFAULT 'OPEN',
    units_to_deduct REAL,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
)
"""

_CREATE_INDEX = "CREATE INDEX IF NOT EXISTS idx_close_orders_pos ON close_orders(position_id)"


def ensure_table(db: Any) -> None:
    """Idempotent: `close_orders`-Tabelle + Index anlegen. Fail-open."""
    try:
        db.execute(_CREATE_TABLE)
        db.execute(_CREATE_INDEX)
    except Exception as exc:
        logger.debug("close_orders.ensure_table: %s", exc)


# ── Sync from API ─────────────────────────────────────────────────────────────

def sync_close_orders(
    db: Any,
    orders_for_close: list[dict],
    instrument_map: dict[int, str] | None = None,
) -> dict:
    """`ordersForClose` aus dem Portfolio in die `close_orders`-Tabelle syncen.

    Parameters
    ----------
    db : DB-Wrapper (execute/fetchall/commit)
    orders_for_close : list[dict]
        Rohliste aus ``clientPortfolio.ordersForClose``. Jeder Eintrag:
        ``{positionID, instrumentID, orderID, unitsToDeduct, statusID,
           openDateTime, ...}``
    instrument_map : dict[int, str] | None
        ``{instrument_id: symbol}`` für Symbol-Auflösung (optional).

    Returns
    -------
    dict
        ``{"inserted": N, "executed": N, "total_open": N}``
    """
    ensure_table(db)
    stats = {"inserted": 0, "executed": 0, "total_open": 0}
    if not orders_for_close:
        # API liefert leere Liste → alle offenen Orders sind executed
        stats["executed"] = _mark_missing_as_executed(
            db, live_order_ids=set(), instrument_map=instrument_map
        )
        return stats

    live_order_ids: set[str] = set()
    for order in orders_for_close:
        order_id = _extract_order_id(order)
        if not order_id:
            continue
        live_order_ids.add(order_id)
        position_id = str(order.get("positionID") or order.get("position_id") or "")
        instrument_id = order.get("instrumentID") or order.get("instrument_id")
        symbol = _resolve_symbol(instrument_id, instrument_map)
        placed_at = order.get("openDateTime") or order.get("openDateTimeUtc") or ""
        units = _normalize_units(order.get("unitsToDeduct"))

        try:
            db.execute(
                """
                INSERT INTO close_orders
                    (order_id, position_id, symbol, instrument_id,
                     placed_at, status, units_to_deduct, updated_at)
                VALUES (?, ?, ?, ?, ?, 'OPEN', ?, datetime('now'))
                ON CONFLICT(order_id) DO UPDATE SET
                    position_id = excluded.position_id,
                    symbol      = excluded.symbol,
                    instrument_id = excluded.instrument_id,
                    placed_at   = excluded.placed_at,
                    status      = 'OPEN',
                    units_to_deduct = excluded.units_to_deduct,
                    updated_at  = datetime('now')
                """,
                (order_id, position_id, symbol,
                 int(instrument_id) if instrument_id else None,
                 placed_at, units),
            )
            stats["inserted"] += 1
        except Exception as exc:
            logger.warning("close_orders.sync: INSERT %s failed: %s", order_id, exc)

    # Orders die aus der API-Liste verschwunden sind → EXECUTED
    executed = _mark_missing_as_executed(db, live_order_ids, instrument_map)
    stats["executed"] = executed

    # Zählen offene Orders
    try:
        row = db.fetchone("SELECT COUNT(*) as c FROM close_orders WHERE status = 'OPEN'")
        stats["total_open"] = int(row["c"]) if row else 0
    except Exception:
        pass

    if stats["inserted"] or stats["executed"]:
        logger.info(
            "close_orders.sync: %d inserted, %d executed, %d open total",
            stats["inserted"], stats["executed"], stats["total_open"],
        )
    return stats


def _mark_missing_as_executed(
    db: Any,
    live_order_ids: set[str],
    instrument_map: dict[int, str] | None = None,
) -> int:
    """Offene Orders die NICHT mehr in der API-Liste sind → EXECUTED.

    Returns die Anzahl markierter Orders.
    """
    try:
        rows = db.fetchall("SELECT order_id FROM close_orders WHERE status = 'OPEN'")
    except Exception:
        return 0
    count = 0
    for row in rows:
        oid = row["order_id"]
        if oid not in live_order_ids:
            try:
                db.execute(
                    "UPDATE close_orders SET status = 'EXECUTED', updated_at = datetime('now') "
                    "WHERE order_id = ? AND status = 'OPEN'",
                    (oid,),
                )
                count += 1
                logger.info("close_orders.sync: order %s → EXECUTED (aus API-Liste verschwunden)", oid)
            except Exception as exc:
                logger.warning("close_orders.sync: mark %s EXECUTED failed: %s", oid, exc)
    return count


# ── Gate (vor jedem close_position) ──────────────────────────────────────────

def has_open_close_order(db: Any, position_id: str, full_only: bool = False) -> bool:
    """True, wenn für *position_id* ein OFFENER Close-Order beim Broker existiert.

    Dient als GATE vor jedem neuen ``close_position()``-Call:
    Open-Order vorhanden → kein zweiter Close (sonst Doppel-Buchung /
    Überverkauf).

    Parameters
    ----------
    full_only : bool
        True → nur FULL-Close-Orders blockieren (``units_to_deduct IS NULL``).
        Teilverkäufe (Partial, ``units_to_deduct > 0``) blockieren NICHT,
        weil sie gestapelt werden dürfen (50% Partial → 50% Partial → Full).
        False (Default) → jeder offene Order blockiert.

    Fail-open: bei DB-Fehler ``False`` (Close wird nicht blockiert).
    """
    if db is None or not position_id:
        return False
    sql = "SELECT 1 FROM close_orders WHERE position_id = ? AND status = 'OPEN'"
    if full_only:
        sql += " AND units_to_deduct IS NULL"
    sql += " LIMIT 1"
    try:
        row = db.fetchone(sql, (str(position_id),))
        return row is not None
    except Exception as exc:
        logger.debug("has_open_close_order(%s, full_only=%s): %s", position_id, full_only, exc)
        return False


# ── Record a newly fired close order ─────────────────────────────────────────

def record_close_order(
    db: Any,
    order_id: str,
    position_id: str,
    symbol: str | None = None,
    instrument_id: int | None = None,
    units_to_deduct: float | None = None,
) -> None:
    """Neu gefeuerten Close-Order in die Tabelle aufnehmen.

    Wird aufgerufen DIREKT NACH `close_position()` (wenn die API-Antwort
    eine order_id enthält). So ist der Wächter sofort scharf, noch bevor
    der Reconciler den nächsten Sync macht.
    """
    if not order_id:
        return
    ensure_table(db)
    units = _normalize_units(units_to_deduct)
    try:
        db.execute(
            """
            INSERT INTO close_orders
                (order_id, position_id, symbol, instrument_id,
                 placed_at, status, units_to_deduct)
            VALUES (?, ?, ?, ?, datetime('now'), 'OPEN', ?)
            ON CONFLICT(order_id) DO UPDATE SET
                status = 'OPEN',
                units_to_deduct = excluded.units_to_deduct,
                updated_at = datetime('now')
            """,
            (order_id, str(position_id), symbol,
             int(instrument_id) if instrument_id else None,
             units),
        )
    except Exception as exc:
        logger.warning("close_orders.record(%s): %s", order_id, exc)


# ── Manual status update ─────────────────────────────────────────────────────

def mark_order_executed(db: Any, order_id: str) -> None:
    """Manuell: Order als EXECUTED markieren (z.B. nach manueller Prüfung)."""
    try:
        db.execute(
            "UPDATE close_orders SET status = 'EXECUTED', updated_at = datetime('now') WHERE order_id = ?",
            (order_id,),
        )
    except Exception as exc:
        logger.warning("close_orders.mark_executed(%s): %s", order_id, exc)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _extract_order_id(order: dict) -> str | None:
    """orderID aus einem ordersForClose-Element extrahieren."""
    for key in ("orderID", "orderId", "order_id", "OrderId"):
        val = order.get(key)
        if val is not None and str(val).strip():
            return str(val).strip()
    return None


def _resolve_symbol(instrument_id: int | None, instrument_map: dict[int, str] | None) -> str | None:
    """Symbol aus instrument_map auflösen (best-effort)."""
    if not instrument_id or not instrument_map:
        return None
    return instrument_map.get(int(instrument_id))


def _normalize_units(units) -> float | None:
    """unitsToDeduct normalisieren: 0.0/None → None (= FULL Close).

    eToro meldet in `ordersForClose` `unitsToDeduct: 0.0` für einen
    FULL-Close (gesamte Position). Für den Wächter ist das der
    kritische Fall: ein offener FULL-Close blockiert jeden zweiten Close.
    Partials (>0) bleiben als Zahl → blockieren bei `full_only=True` nicht.
    """
    if units is None:
        return None
    try:
        u = float(units)
    except (TypeError, ValueError):
        return None
    return u if u > 0 else None
