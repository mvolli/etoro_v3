#!/usr/bin/env python3
"""
scripts/repair_missing_closed_at.py
Einmal-Reparatur zu fix/closed-at-guarantee (2026-08-12).

PROBLEM: 45 Trades stehen auf status='CLOSED' mit closed_at IS NULL
(created_at 2026-07-21 .. 2026-08-07, alle VERIFIED, alle mit pnl_usd).
Jede zeitbasierte Auswertung verliert sie stillschweigend:
  - llm_review_worker filtert "AND t.closed_at IS NOT NULL"
  - config_experiment_worker vergleicht Fenster ueber closed_at
  - get_pending_verification sortiert danach
Die Lernschleife des Bots wurde also mit einem Loch gefuettert.

QUELLE DER WAHRHEIT: der trade_events-Ledger (feat/pnl-nachreport). Alle 45
haben ein CLOSE-Event mit `event_at` — der Zeitstempel wird NICHT geschaetzt,
sondern aus dem Ledger uebernommen. Bei mehreren CLOSE-Events je Position
gilt das FRUEHESTE (der erste vollstaendige Close; spaetere Eintraege sind
Nachreport-Edits).

Idempotent: repariert nur Zeilen mit closed_at IS NULL.

    PYTHONPATH=src python3 scripts/repair_missing_closed_at.py --dry-run
    PYTHONPATH=src python3 scripts/repair_missing_closed_at.py
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = PROJECT_ROOT / "data" / "trading.db"

SELECT_REPAIRABLE = """
    SELECT t.id, t.symbol, t.created_at, t.api_position_id,
           MIN(e.event_at) AS close_at
    FROM trades t
    JOIN trade_events e
      ON e.position_id = t.api_position_id
     AND e.event_type = 'CLOSE'
    WHERE t.status = 'CLOSED' AND t.closed_at IS NULL
    GROUP BY t.id
"""

COUNT_REMAINING = """
    SELECT COUNT(*) AS n FROM trades
    WHERE status = 'CLOSED' AND closed_at IS NULL
"""


def main() -> int:
    dry_run = "--dry-run" in sys.argv

    ro = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    ro.row_factory = sqlite3.Row
    rows = ro.execute(SELECT_REPAIRABLE).fetchall()
    total_broken = ro.execute(COUNT_REMAINING).fetchone()["n"]
    ro.close()

    print(f"CLOSED ohne closed_at:        {total_broken}")
    print(f"davon aus trade_events belegbar: {len(rows)}")
    if total_broken > len(rows):
        print(f"NICHT reparierbar (kein CLOSE-Event): {total_broken - len(rows)}"
              f" — bleiben NULL statt geraten zu werden")

    if not rows:
        print("\nNichts zu tun.")
        return 0

    print("\nBeispiele:")
    for r in rows[:5]:
        print(f"  Trade {r['id']:5d} {str(r['symbol']):10s} "
              f"created {r['created_at']} -> closed_at {r['close_at']}")

    # Sanity: kein Close darf VOR dem Anlegen liegen
    bad = [r for r in rows if r["close_at"] and r["close_at"] < r["created_at"]]
    if bad:
        print(f"\nABBRUCH: {len(bad)} Event(s) liegen vor created_at — "
              f"Ledger-Zuordnung unplausibel, nichts geschrieben.")
        for r in bad[:5]:
            print(f"  Trade {r['id']}: created {r['created_at']} > close {r['close_at']}")
        return 1

    if dry_run:
        print("\nDRY-RUN — keine Aenderung geschrieben.")
        return 0

    conn = sqlite3.connect(str(DB_PATH), timeout=10)
    conn.execute("PRAGMA busy_timeout=10000")
    changed = 0
    for r in rows:
        cur = conn.execute(
            "UPDATE trades SET closed_at = ? WHERE id = ? AND closed_at IS NULL",
            (r["close_at"], r["id"]),
        )
        changed += cur.rowcount
    conn.commit()
    remaining = conn.execute(COUNT_REMAINING).fetchone()[0]
    conn.close()

    print(f"\n{changed} Trades repariert. Noch ohne closed_at: {remaining}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
