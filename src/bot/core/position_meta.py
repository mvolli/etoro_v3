"""position_meta.py — gemeinsame Anreicherung offener Positionen.

fix/position-meta-dedup (2026-07-15): signal_type/opened_at/days_held wurde
in position_review_worker und llm_review_worker dupliziert gepflegt (und
divergierte bereits bei den Status-Filtern). Eine Quelle fuer alle
Konsumenten (Reviews, kuenftig Stale-Exit-Auswertung).
"""
from __future__ import annotations

from datetime import datetime, timezone


def days_held_from(opened_at: str | None, now: datetime | None = None) -> int | None:
    """Kalendertage seit opened_at (ISO oder 'YYYY-MM-DD HH:MM:SS').

    None bei fehlendem/unparsbarem Wert (fail-safe — ein kaputter Timestamp
    darf nie als '0 Tage' oder 'uralt' fehlinterpretiert werden)."""
    if not opened_at:
        return None
    try:
        opened = datetime.fromisoformat(str(opened_at).replace(" ", "T"))
        if opened.tzinfo is None:
            opened = opened.replace(tzinfo=timezone.utc)
        return ((now or datetime.now(timezone.utc)) - opened).days
    except Exception:
        return None


def enrich_signal_and_age(
    cur,
    positions: list[dict],
    statuses: tuple[str, ...] = ("ACTIVE", "CONFIRMED"),
    default_signal_type: str = "UNBEKANNT",
) -> None:
    """Ergaenzt jede Position (dict mit instrument_id) IN-PLACE um
    signal_type + opened_at (neuester Trade des Instruments im Status-Filter)
    und days_held.

    cur: offener sqlite3-Cursor auf trading.db (read-only genuegt).
    Fehler pro Position → Defaults (fail-safe), nie Exception nach oben."""
    placeholders = ",".join("?" * len(statuses))
    for pos in positions:
        iid = pos.get("instrument_id")
        row = None
        if iid is not None:
            try:
                cur.execute(
                    f"""
                    SELECT s.signal_type, t.created_at
                    FROM trades t
                    JOIN signals s ON s.id = t.signal_id
                    WHERE t.instrument_id = ?
                      AND t.status IN ({placeholders})
                    ORDER BY t.created_at DESC LIMIT 1
                    """,
                    (iid, *statuses),
                )
                row = cur.fetchone()
            except Exception:
                row = None
        pos["signal_type"] = row["signal_type"] if row else default_signal_type
        pos["opened_at"] = row["created_at"] if row else None
        pos["days_held"] = days_held_from(pos["opened_at"])
