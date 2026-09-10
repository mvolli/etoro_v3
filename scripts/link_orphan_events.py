#!/usr/bin/env python3
"""Verwaiste trade_events nachtraeglich mit ihrem Trade verknuepfen.

fix/event-trade-link (2026-09-10). Der Code-Fix in TradeEventRepo.record()
loest trade_id ab sofort aus position_id auf. Dieses Skript holt die
Altbestaende nach.

Messung am 2026-09-10 vor dem Lauf:
    212 Close-Events ohne trade_id, davon 169 aufloesbar
    risk_sl 157 (ALLE), exposure_trim 13 (alle), concentration 6 (alle)
    zusammen -225,56 USD realisiertes PnL, das realized_by_trade() verwarf

Verknuepft wird NUR bei eindeutigem Treffer. Leere position_id-Strings sind
ausgeschlossen: Fehltrades aus Juni/Juli tragen api_position_id='' und
wuerden sonst 35 Trades gleichzeitig treffen.

DRY-RUN ist Standard. Schreiben nur mit --apply.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC = PROJECT_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

DB_PATH = PROJECT_ROOT / "data" / "trading.db"


def _kandidaten(db) -> list[dict]:
    """(event_id, trade_id) fuer eindeutig aufloesbare Waisen."""
    return db.fetchall(
        """
        SELECT e.id AS event_id, e.symbol, e.source, e.event_type,
               e.position_id, e.amount_usd, e.pnl_pct, e.pnl_usd,
               (SELECT t.id FROM trades t
                 WHERE t.api_position_id = e.position_id) AS trade_id
        FROM trade_events e
        WHERE e.trade_id IS NULL
          AND e.position_id IS NOT NULL
          AND TRIM(e.position_id) <> ''
          AND (SELECT COUNT(*) FROM trades t
                WHERE t.api_position_id = e.position_id) = 1
        ORDER BY e.id
        """
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true",
                    help="schreiben (ohne dieses Flag nur Bericht)")
    args = ap.parse_args()

    from bot.db.connection import DB

    with DB(DB_PATH) as db:
        offen = db.fetchone(
            "SELECT COUNT(*) n FROM trade_events WHERE trade_id IS NULL")["n"]
        kand = _kandidaten(db)

        print("── Verwaiste trade_events ─────────────────────────────────────")
        print(f"  ohne trade_id insgesamt:   {offen}")
        print(f"  eindeutig aufloesbar:      {len(kand)}")
        print(f"  bleiben unaufloesbar:      {offen - len(kand)}")

        nach_quelle: dict[str, int] = {}
        pnl = 0.0
        for r in kand:
            nach_quelle[r["source"]] = nach_quelle.get(r["source"], 0) + 1
            if r["event_type"] in ("CLOSE", "PARTIAL_CLOSE"):
                from bot.core.trade_pnl import event_pnl_usd
                v = event_pnl_usd(r["amount_usd"], r["pnl_pct"], r["pnl_usd"])
                if v is not None:
                    pnl += v

        print("\n── nach Quelle ────────────────────────────────────────────────")
        for src, n in sorted(nach_quelle.items(), key=lambda kv: -kv[1]):
            print(f"  {src:20} {n:4}")
        print(f"\n  PnL, das realized_by_trade() dadurch zurueckbekommt: "
              f"${pnl:+,.2f}")

        if not args.apply:
            print("\n  (DRY-RUN — nichts geschrieben. Mit --apply ausfuehren.)")
            return 0

        n = 0
        for r in kand:
            try:
                db.execute("UPDATE trade_events SET trade_id = ? WHERE id = ?",
                           (int(r["trade_id"]), int(r["event_id"])))
                n += 1
            except Exception as exc:
                print(f"  FEHLER bei Event {r['event_id']}: {exc}")
        print(f"\n  {n} Events verknuepft.")
        rest = db.fetchone(
            "SELECT COUNT(*) n FROM trade_events WHERE trade_id IS NULL")["n"]
        print(f"  verbleibend ohne trade_id: {rest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
