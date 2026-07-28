"""Generischer PnL-Retro-Fill aus der eToro-Trade-History (feat/pnl-nachreport).

Drei Nutzer:
  - reconciler.py Step 9d (PENDING-Closes finalisieren) und Step 9e
    (trade_events mit reported_final=0 bestaetigen + Embeds editieren)
  - scripts/backfill_trade_history.py (Einmal-Backfill der Alt-Trades)
  - scripts/migrate_trade_alerts_to_trades.py (Anreicherung)

Kernidee: die History EINMAL pro Lauf holen und indizieren, statt (wie
frueher in 9d) 2 Seiten PRO pending Trade zu fetchen. Die History hat
eine Zeile PRO Partial Close — by_position haelt deshalb LISTEN,
chronologisch nach closeTimestamp sortiert; die letzte Zeile ist der
finale Full-Close.

Pure Matching-Logik ohne DB-Zugriff — testbar mit Fixture-Rows.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)


class HistoryIndex:
    """Index ueber get_trade_history-Rows: positionId -> [rows], orderId -> row."""

    def __init__(self) -> None:
        self.by_position: dict[int, list[dict]] = {}
        self.by_order: dict[int, dict] = {}
        self.row_count = 0

    def add(self, row: dict) -> None:
        pos = _to_int(row.get("positionId"))
        if pos is not None:
            self.by_position.setdefault(pos, []).append(row)
        order = _to_int(row.get("orderId"))
        if order is not None and order not in self.by_order:
            self.by_order[order] = row
        self.row_count += 1

    def sort(self) -> None:
        for rows in self.by_position.values():
            rows.sort(key=lambda r: str(r.get("closeTimestamp") or ""))


def _to_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _parse_ts(value: Any) -> datetime | None:
    try:
        s = str(value).strip().replace("Z", "+00:00")
        if " " in s and "T" not in s:
            s = s.replace(" ", "T")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def fetch_history_index(
    client: Any,
    min_date_iso: str | None = None,
    max_pages: int = 10,
    page_size: int = 100,
) -> HistoryIndex:
    """History paginieren (bis kurze Seite oder max_pages) und indizieren.

    max_pages=10 × 100 Rows begrenzt die API-Last pro Lauf; fuer den
    Einmal-Backfill kann der Aufrufer max_pages hochsetzen.
    """
    idx = HistoryIndex()
    for page in range(1, max_pages + 1):
        try:
            rows = client.get_trade_history(
                min_date=min_date_iso, page=page, page_size=page_size
            )
        except Exception as exc:
            logger.warning("[pnl_backfill] History-Fetch Seite %d fehlgeschlagen: %s",
                           page, exc)
            break
        if not rows:
            break
        for row in rows:
            idx.add(row)
        if len(rows) < page_size:
            break
    idx.sort()
    logger.info("[pnl_backfill] History-Index: %d Rows, %d Positionen",
                idx.row_count, len(idx.by_position))
    return idx


def match_close(index: HistoryIndex, pos_id: Any,
                order_id: Any = None) -> dict | None:
    """Finalen Full-Close einer Position finden (letzte Row der Position).

    Fallback orderId-Match, wenn die positionId nicht in der History ist
    (z.B. eToro-seitig neu nummeriert).
    """
    pos = _to_int(pos_id)
    if pos is not None and pos in index.by_position:
        return index.by_position[pos][-1]
    order = _to_int(order_id)
    if order is not None:
        return index.by_order.get(order)
    return None


def match_partial(index: HistoryIndex, pos_id: Any, event_at: Any,
                  units: float | None = None,
                  tolerance_minutes: float = 30.0) -> dict | None:
    """Partial-Close-Row zu einem trade_events-Eintrag finden.

    Naechster closeTimestamp innerhalb ±tolerance_minutes; bei mehreren
    Kandidaten entscheidet die Units-Naehe (History hat eine Row pro
    Teilverkauf).
    """
    pos = _to_int(pos_id)
    if pos is None or pos not in index.by_position:
        return None
    ev_dt = _parse_ts(event_at)
    if ev_dt is None:
        return None
    best, best_score = None, None
    for row in index.by_position[pos]:
        row_dt = _parse_ts(row.get("closeTimestamp"))
        if row_dt is None:
            continue
        dt_min = abs((row_dt - ev_dt).total_seconds()) / 60.0
        if dt_min > tolerance_minutes:
            continue
        score = dt_min
        if units is not None:
            try:
                row_units = float(row.get("units") or 0)
                if row_units > 0:
                    score += abs(row_units - float(units)) / row_units * 10.0
            except (TypeError, ValueError):
                pass
        if best_score is None or score < best_score:
            best, best_score = row, score
    return best


def pnl_from_row(row: dict) -> dict:
    """PnL-Zahlen aus einer History-Row extrahieren.

    Returns {pnl_usd, pnl_pct, entry, exit, units, investment} —
    pnl_pct ist None (nicht 0!), wenn investment fehlt: ein echter
    netProfit darf nicht durch eine fehlende Bezugsgroesse zu "0%"
    verfaelscht werden (Alt-Bug in 9d).
    """
    def _f(key: str) -> float | None:
        try:
            v = row.get(key)
            return float(v) if v is not None else None
        except (TypeError, ValueError):
            return None

    pnl_usd = _f("netProfit")
    investment = _f("investment") or _f("initialInvestment")
    pnl_pct = None
    if pnl_usd is not None and investment and investment > 0:
        pnl_pct = pnl_usd / investment * 100.0
    return {
        "pnl_usd": pnl_usd,
        "pnl_pct": pnl_pct,
        "entry": _f("openRate"),
        "exit": _f("closeRate"),
        "units": _f("units"),
        "investment": investment,
    }
