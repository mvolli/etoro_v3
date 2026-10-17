#!/usr/bin/env python3
"""signal_outcomes taeglich fortschreiben — feat/outcome-refresh (2026-10-16).

Logik: bot/core/outcome_refresh.py. Zeigt immer die Edge-Gate-Tabelle vorher
und nachher, damit ein Typ, der auf LIVE kippt, VOR dem Schreiben sichtbar ist.

  PYTHONPATH=src /usr/bin/python3 scripts/refresh_signal_outcomes.py          # Probelauf
  PYTHONPATH=src /usr/bin/python3 scripts/refresh_signal_outcomes.py --write  # schreiben

Laeuft per Hermes-Cron (~/.hermes/scripts/v3_outcome_refresh.sh) mit --write.
Kippt dabei ein Typ von Block auf frei, steht das in der Ausgabe und im
system_log (WARN).
"""
from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from bot.core import edge_gate, outcome_refresh  # noqa: E402

DB = ROOT / "data" / "trading.db"


class _DB:
    def __init__(self, con):
        self.con = con

    def fetchall(self, q, p=()):
        return self.con.execute(q, p).fetchall()


def gate_table(con) -> dict[str, dict]:
    con.row_factory = sqlite3.Row
    types = [r[0] for r in con.execute(
        "SELECT signal_type FROM signal_outcomes WHERE fwd_5d_pnl IS NOT NULL "
        "GROUP BY 1 HAVING COUNT(*) >= 10")]
    return {t: edge_gate.evaluate(t, "refresh", _DB(con)) for t in types}


def _print_table(title, tab):
    print(f"\n{title}")
    for t, e in sorted(tab.items(), key=lambda kv: -kv[1]["lcb"]):
        state = "frei" if not edge_gate.blocks(e) else "BLOCK"
        print(f"  {t[:48]:48s} n={e['n']:5d} mean={e['exp_net']*100:+.2f}% "
              f"LCB={e['lcb']*100:+.2f}%  {state}{' (exempt)' if e['exempt'] else ''}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--recost", action="store_true",
                    help="einmalig ALLE Zeilen auf Marktkosten umrechnen (feat/gate-market-costs)")
    args = ap.parse_args()

    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=30)
    before = gate_table(con)
    res = outcome_refresh.compute(con)
    con.close()

    rows = res["rows"]
    n5 = sum(1 for r in rows if r["fwd_5d"] is not None)
    print(f"Fenster {res['since']} .. {res['today']} (exkl.): {res['signals']} Signale "
          f"(dedupliziert), {len(rows)} mit Kurs ({len(rows)/max(res['signals'],1)*100:.0f} %), "
          f"davon {n5} mit 5-Tage-Horizont")
    if res["no_bar"]:
        top = sorted(res["no_bar"].items(), key=lambda kv: -kv[1])[:5]
        print("  ohne Kurs am Signaltag: " + ", ".join(f"{t[:30]}={n}" for t, n in top))

    # Nachher-Tabelle auf einer Kopie berechnen (Probelauf schreibt nie live).
    with tempfile.TemporaryDirectory() as tmp:
        copy = Path(tmp) / "t.db"
        src = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=30)
        dst = sqlite3.connect(copy)
        src.backup(dst)
        src.close()
        if args.recost:
            print(f"  Umrechnung auf Marktkosten: {outcome_refresh.recost_all(dst)} Zeilen")
        outcome_refresh.write(dst, rows)
        dst.commit()
        after = gate_table(dst)
        dst.close()

    _print_table("Edge-Gate VORHER", before)
    _print_table("Edge-Gate NACHHER", after)
    flips = [t for t, e in after.items()
             if not edge_gate.blocks(e) and not e["exempt"]
             and (t not in before or edge_gate.blocks(before[t]))]
    if flips:
        print("\nACHTUNG — kippt auf frei: " + ", ".join(flips))

    if not args.write:
        print("\nProbelauf — nichts geschrieben (--write zum Schreiben).")
        return 0

    con = sqlite3.connect(DB, timeout=30)
    con.execute("PRAGMA busy_timeout = 30000")
    if args.recost:
        outcome_refresh.recost_all(con)
    n = outcome_refresh.write(con, rows)
    try:
        import json
        con.execute(
            # ts explizit wie LogRepo: der Spalten-Default datetime('now','utc')
            # verschiebt doppelt (-2 h).
            "INSERT INTO system_log (ts, level, worker, message, details) "
            "VALUES (datetime('now'),?,?,?,?)",
            ("WARN" if flips else "INFO", "outcome_refresh",
             f"signal_outcomes fortgeschrieben: {n} Zeilen ({res['since']}..{res['today']})"
             + (f" — kippt auf frei: {', '.join(flips)}" if flips else ""),
             json.dumps({"rows": n, "signals": res["signals"], "flips": flips})))
        con.commit()
    except sqlite3.Error:
        pass
    con.close()
    print(f"\n{n} Zeilen geschrieben.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
