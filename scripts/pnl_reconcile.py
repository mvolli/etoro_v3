#!/usr/bin/env python3
"""Rekonziliation: Trade-Ergebnisse gegen Kontoentwicklung.

WARUM (2026-09-05): `trades.pnl_usd` enthaelt nur die LETZTE Tranche einer
gestaffelt geschlossenen Position, und 898 von 910 Teilschliessungen tragen
gar keinen Dollar-Betrag. Die Folge war, dass rund 1.400 $ Kontoverlust in
keinem Trade-Datensatz auftauchten — unsichtbar, also nicht behandelbar.

Dieses Script macht die Luecke zu einer Zahl:

    Start + realisiert(alle Tranchen) + unrealisiert  ==  Equity  ?

Das Residuum ist alles, was kein Trade erklaert: Spread, Gebuehren,
Slippage, nicht gebuchte Schliessungen. Nichts davon wird heute erfasst.

  Bericht:   PYTHONPATH=src python3 scripts/pnl_reconcile.py
  Backfill:  PYTHONPATH=src python3 scripts/pnl_reconcile.py --backfill
             (fuellt trade_events.pnl_usd; ohne Flag wird NICHTS geschrieben)

Der Backfill schreibt nur dort, wo `pnl_usd` fehlt — ein vom Broker
gemeldeter Wert wird nie ueberschrieben.
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC = PROJECT_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("pnl_reconcile")

DB_PATH = PROJECT_ROOT / "data" / "trading.db"


def _bericht(db) -> dict:
    from bot.core.trade_pnl import reconcile
    r = reconcile(db)
    erwartet = r["start_equity"] + r["realized_usd"] + r["unrealized_usd"]
    print("── Rekonziliation " + "─" * 45)
    print(f"  Start                                     ${r['start_equity']:>12,.2f}")
    print(f"  realisiert ({r['trades']:>4} Trades, {r['tranchen']:>5} Tranchen)  "
          f"${r['realized_usd']:>+12,.2f}")
    print(f"  unrealisiert (offene Positionen)          ${r['unrealized_usd']:>+12,.2f}")
    print(f"  {'-' * 58}")
    print(f"  = erwartete Equity                        ${erwartet:>12,.2f}")
    if r["equity"] is None:
        print("  tatsaechliche Equity                      (nicht verfuegbar)")
        return r
    print(f"  tatsaechliche Equity                      ${r['equity']:>12,.2f}")
    print(f"  RESIDUUM (kein Trade-Datensatz)           ${r['residual_usd']:>+12,.2f}")

    fills = _fill_count(db)
    if fills and r["residual_usd"] is not None:
        print(f"\n  Fills gesamt: {fills}   ->   ${abs(r['residual_usd']) / fills:.2f} je Fill")
    from bot.core.trade_pnl import recorded_costs
    k = recorded_costs(db)
    print(f"\n── Erfasste Kosten " + "─" * 44)
    print(f"  Fills mit gemessenem Spread: {k['fills_mit_kosten']} / {k['fills_gesamt']}")
    print(f"  gebuchte Reibung:            ${k['cost_usd']:>+12,.2f}")
    if k["fills_mit_kosten"] == 0:
        print("  (feat/fill-costs ist neu — Altdaten haben keinen Spread,")
        print("   die Spalte fuellt sich ab dem naechsten Fill.)")
    elif r["residual_usd"] is not None:
        print(f"  davon im Residuum erklaert:  "
              f"{100 * abs(k['cost_usd']) / max(abs(r['residual_usd']), 1e-9):.0f} %")

    print("\n  Das Residuum ist NICHT bewiesen als Kosten — es ist alles, was")
    print("  kein Trade-Datensatz erklaert. Weder `trades` noch `trade_events`")
    print("  haben eine Spalte fuer Spread, Gebuehr oder Slippage.")
    return r


def _fill_count(db) -> int:
    try:
        row = db.fetchone(
            "SELECT COUNT(*) AS n FROM (SELECT DISTINCT trade_id, event_at, "
            "close_pct FROM trade_events)")
        return int(row["n"]) if row else 0
    except Exception:
        return 0


def _luecken(db) -> list[dict]:
    try:
        rows = db.fetchall(
            """
            SELECT id, trade_id, symbol, amount_usd, pnl_pct
            FROM trade_events
            WHERE event_type IN ('PARTIAL_CLOSE', 'CLOSE')
              AND pnl_usd IS NULL
              AND amount_usd IS NOT NULL
              AND pnl_pct IS NOT NULL
            """
        )
        return [dict(r) for r in (rows or [])]
    except Exception as exc:
        logger.warning("Luecken-Abfrage fehlgeschlagen: %s", exc)
        return []


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--backfill", action="store_true",
                    help="fehlende trade_events.pnl_usd schreiben")
    args = ap.parse_args()

    from bot.core.trade_pnl import event_pnl_usd
    from bot.db.connection import DB

    with DB(DB_PATH) as db:
        _bericht(db)

        luecken = _luecken(db)
        print(f"\n── Fehlende pnl_usd " + "─" * 43)
        print(f"  Ereignisse ohne Dollar-Betrag: {len(luecken)}")
        if not luecken:
            return 0

        summe = sum(v for v in
                    (event_pnl_usd(g["amount_usd"], g["pnl_pct"]) for g in luecken)
                    if v is not None)
        print(f"  herleitbarer Betrag insgesamt: ${summe:+,.2f}")

        if not args.backfill:
            print("  (DRY-RUN — nichts geschrieben. Mit --backfill ausfuehren.)")
            return 0

        n = 0
        for g in luecken:
            val = event_pnl_usd(g["amount_usd"], g["pnl_pct"])
            if val is None:
                continue
            try:
                db.execute("UPDATE trade_events SET pnl_usd = ? WHERE id = ? "
                           "AND pnl_usd IS NULL", (round(val, 4), g["id"]))
                n += 1
            except Exception as exc:
                logger.warning("id=%s: %s", g["id"], exc)
        print(f"  geschrieben: {n}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
