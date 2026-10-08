"""Dedup-Helfer für Close-Events (fix/doppelbuchungen, 2026-10-08).

Zwei Probleme, die dieselbe Wurzel haben: der Worker feuert jeden 5-min-Zyklus
neu, weil der Zustand „schon geschlossen" nicht persistiert wird.

  * ``extract_order_id`` — robuste Extraktion des Broker-Ankers aus der
    ``close_position()``-Antwort. Ohne ihn ist keine einzelne Buchung
    verifizierbar (92 % der Close-Events hatten kein ``order_id``).

  * ``has_recent_close`` — prüft, ob für eine Position innerhalb eines
    Zeitfensters schon ein Close-Event gebucht wurde. Dient als Gate,
    damit ``risk_sl`` und ``llm_tighten`` nicht in jedem Zyklus neu
    buchen (trade 1662: 105 Events statt 1).

  * ``trade_already_closed`` — prüft, ob der Trade für eine Position
    bereits CLOSED (PENDING oder VERIFIED) ist. Dient als Gate für
    ``risk_sl``: wenn der Close schon gesendet und der Trade auf CLOSED
    gesetzt wurde, muss kein zweiter Close gesendet werden.

Alle Funktionen sind pure (keine API-Calls), damit sie ohne Mocks
testbar sind.
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def extract_order_id(response: Any) -> str | None:
    """Order-ID aus einer ``close_position()``-Antwort extrahieren.

    eToro liefert ``orderId`` in der Response. Versucht mehrere
    Feldnamen für Robustheit (API-Varianten, Nested ``raw``).
    Gibt ``None`` zurück, wenn kein Order-ID-Feld gefunden wird —
    das ist kein Fehler (Fail-open), das Event wird einfach ohne
    Broker-Anker gespeichert.
    """
    if not response or not isinstance(response, dict):
        return None
    # Top-level
    for key in ('orderId', 'orderID', 'order_id', 'OrderId'):
        val = response.get(key)
        if val is not None and str(val).strip():
            return str(val).strip()
    # Nested 'raw' (manche Wrapper packen die API-Antwort darunter)
    raw = response.get('raw')
    if isinstance(raw, dict):
        for key in ('orderId', 'orderID', 'order_id', 'OrderId'):
            val = raw.get(key)
            if val is not None and str(val).strip():
                return str(val).strip()
    return None


def has_recent_close(db: Any, position_id: str,
                     window_minutes: int = 30) -> bool:
    """True, wenn für *position_id* innerhalb der letzten *window_minutes*
    Minuten schon ein Close-Event (CLOSE oder PARTIAL_CLOSE) gebucht wurde.

    Dient als Dedup-Gate für ``llm_tighten``: die LLM empfiehlt pro
    Zyklus denselben Close (``_ADVISOR_CACHE`` ist in-Prozess und
    verliert seinen Zustand bei jedem Cron-Fire). Ohne Gate wird
    jeder Zyklus neu gebucht (trade 2783: 9× je +8,93).

    Fail-open: bei DB-Fehler ``False`` (Close wird nicht blockiert).
    """
    if db is None or not position_id:
        return False
    try:
        row = db.fetchone(
            """
            SELECT 1 FROM trade_events
            WHERE position_id = ?
              AND event_type IN ('CLOSE', 'PARTIAL_CLOSE')
              AND event_at >= datetime('now', ?, 'localtime')
            LIMIT 1
            """,
            (str(position_id), f"-{int(window_minutes)} minutes"),
        )
        return row is not None
    except Exception as exc:
        logger.debug("has_recent_close(%s): %s", position_id, exc)
        return False


def trade_already_closed(db: Any, position_id: str) -> bool:
    """True, wenn der Trade für *position_id* bereits CLOSED ist
    (PENDING oder VERIFIED).

    Dient als Dedup-Gate für ``risk_sl``: nachdem der erste Close
    gesendet wurde, setzt der risk_worker den Trade auf
    ``status='CLOSED', verification_status='PENDING'``. Solange die
    Position im Live-Portfolio lingering bleibt (eToro API langsam,
    v. a. bei HK/ASIA-Märkten), würde jeder 5-min-Zyklus einen neuen
    Close senden und ein neues Event buchen (trade 1662: 105 Events
    in 11h). Mit diesem Gate wird nur der erste Close gebucht.

    Fail-open: bei DB-Fehler ``False`` (Close wird nicht blockiert).
    """
    if db is None or not position_id:
        return False
    try:
        row = db.fetchone(
            """
            SELECT 1 FROM trades
            WHERE api_position_id = ?
              AND status = 'CLOSED'
            LIMIT 1
            """,
            (str(position_id),),
        )
        return row is not None
    except Exception as exc:
        logger.debug("trade_already_closed(%s): %s", position_id, exc)
        return False
