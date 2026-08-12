#!/usr/bin/env python3
"""
scripts/migrate_profit_ladder_reset.py
Einmal-Migration zu fix/profit-ladder-reachability (2026-08-12).

PROBLEM: Die ATR-Profit-Leiter wird pro Position beim ersten Eintritt in die
Gewinnzone in position_state.profit_levels_json EINGEFROREN (Schutz gegen
Doppelverkauf, wenn sich der ATR-Wert spaeter verschiebt). Eine Aenderung an
PROFIT_LADDER_ATR_SCALE erreicht deshalb NUR Positionen, die noch keine
eingefrorene Leiter haben — fuer alle anderen waere der Fix ein stiller No-Op.

REGEL: Zurueckgesetzt wird NUR, wo levels_taken LEER ist.

Begruendung: bei einer Position mit bereits genommenen Stufen stehen in
levels_taken die ALTEN Schwellenwerte (z.B. "3.75,11.24"). Nach dem Reset
loest die Leiter mit NEUEN Schwellen auf (z.B. 6.07/10.12/18.21) — die alten
Eintraege matchen dann nicht mehr, und jede neue Stufe unterhalb des bereits
realisierten Niveaus wuerde ERNEUT feuern. Das ist exakt der Doppelverkauf,
gegen den das Einfrieren gebaut wurde.

Die wenigen Positionen mit genommenen Stufen behalten daher ihre alte Leiter
und laufen darauf aus (Ø Haltedauer 3.0 Tage) — kein Eingriff noetig.

Idempotent: mehrfach ausfuehrbar, betrifft beim zweiten Lauf nichts mehr.

    PYTHONPATH=src python3 scripts/migrate_profit_ladder_reset.py --dry-run
    PYTHONPATH=src python3 scripts/migrate_profit_ladder_reset.py
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = PROJECT_ROOT / "data" / "trading.db"

SELECT_AFFECTED = """
    SELECT position_id, symbol, profit_levels_json, peak_pnl_pct
    FROM position_state
    WHERE profit_levels_json IS NOT NULL
      AND COALESCE(levels_taken, '') = ''
"""

SELECT_SKIPPED = """
    SELECT position_id, symbol, levels_taken, peak_pnl_pct
    FROM position_state
    WHERE profit_levels_json IS NOT NULL
      AND COALESCE(levels_taken, '') != ''
"""

UPDATE_RESET = """
    UPDATE position_state
    SET profit_levels_json = NULL, updated_at = datetime('now')
    WHERE profit_levels_json IS NOT NULL
      AND COALESCE(levels_taken, '') = ''
"""


def main() -> int:
    dry_run = "--dry-run" in sys.argv

    # Schritt 1: readonly zaehlen/pruefen (AGENTS.md-Workflow fuer Live-DB)
    ro = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    ro.row_factory = sqlite3.Row
    affected = ro.execute(SELECT_AFFECTED).fetchall()
    skipped = ro.execute(SELECT_SKIPPED).fetchall()
    total = ro.execute("SELECT COUNT(*) c FROM position_state").fetchone()["c"]
    ro.close()

    print(f"position_state gesamt: {total}")
    print(f"  → Reset (Leiter eingefroren, nichts genommen): {len(affected)}")
    print(f"  → Uebersprungen (Stufen bereits genommen):     {len(skipped)}")

    if skipped:
        print("\nUebersprungen — laufen auf der alten Leiter aus:")
        for r in skipped:
            print(f"    {str(r['symbol'] or '?'):10s} genommen={r['levels_taken']!r:16} "
                  f"peak={r['peak_pnl_pct']:.1f}%")

    if not affected:
        print("\nNichts zu tun.")
        return 0

    if dry_run:
        print("\nDRY-RUN — keine Aenderung geschrieben.")
        return 0

    # Schritt 2: gezieltes UPDATE, Schritt 3: changes() kontrollieren
    conn = sqlite3.connect(str(DB_PATH), timeout=10)
    conn.execute("PRAGMA busy_timeout=10000")
    cur = conn.execute(UPDATE_RESET)
    changed = cur.rowcount
    conn.commit()
    conn.close()

    print(f"\n{changed} Leiter zurueckgesetzt "
          f"(loesen beim naechsten risk_worker-Zyklus mit dem neuen Faktor auf).")
    if changed != len(affected):
        print(f"WARNUNG: erwartet {len(affected)}, geaendert {changed} — "
              f"nebenlaeufiger Worker-Lauf? Nochmal pruefen.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
