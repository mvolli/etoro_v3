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


def _bericht(db, since: str | None = None) -> dict:
    from bot.core.trade_pnl import reconcile
    r = reconcile(db, since=since)
    erwartet = (r["start_equity"] + r["realized_usd"]
                + r.get("unattributed_usd", 0.0) + r["unrealized_usd"])
    if since:
        print("── Rekonziliation SEIT RESET " + "─" * 34)
        print(f"  Epoche ab {since}")
        print("  (kumulative Sicht: ohne --seit-reset)")
    else:
        print("── Rekonziliation " + "─" * 45)
    print(f"  Start                                     ${r['start_equity']:>12,.2f}")
    print(f"  realisiert ({r['trades']:>4} Trades, {r['tranchen']:>5} Tranchen)  "
          f"${r['realized_usd']:>+12,.2f}")
    # fix/orphan-events (2026-09-10): Geisterpositionen ohne Trade-Zeile —
    # echtes Geld, aber keinem Trade zuordenbar. Eigene Zeile, damit der
    # Unterschied sichtbar bleibt statt in "realisiert" zu verschwinden.
    if r.get("unattributed_tranchen"):
        print(f"  + ohne Trade-Bezug ({r['unattributed_tranchen']:>4} Tranchen)        "
              f"${r['unattributed_usd']:>+12,.2f}")
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
    ap.add_argument("--seit-reset", dest="seit_reset", action="store_true",
                    help="nur ab EPOCH_START rechnen (Sicht 'seit Portfolio-Reset')")
    args = ap.parse_args()

    from bot.core.trade_pnl import event_pnl_usd
    from bot.db.connection import DB

    with DB(DB_PATH) as db:
        since = None
        if args.seit_reset:
            row = db.fetchone(
                "SELECT value FROM system_state WHERE key='EPOCH_START'")
            if not row:
                print("Keine Epoche gesetzt — scripts/portfolio_reset.py finalize")
                return 2
            since = row["value"]
        _bericht(db, since=since)

        # feat/sizing-drift-guard (2026-09-09): der Vol-Guard aus dem
        # Karpathy-Loop. Gehoert hierher, weil dieser Bericht der eine Ort
        # ist, an dem unsichtbare Groessen sichtbar werden — die Drift von
        # 0.30 auf 0.46 lief zwei Wochen, weil sie nirgends stand.
        try:
            from bot.core.sizing import check_sizing_drift
            g = check_sizing_drift(db)
            print("\n── Sizing-Drift " + "─" * 47)
            if g["current"] is None:
                print("  nicht messbar (keine Trades im Kelly-Fenster)")
            else:
                print(f"  mittleres Sizing heute:   {g['current']:.4f}")
                print(f"  freigegebenes Niveau:     {g['target']:.4f}"
                      f"   (Band ±{g['band_pct']:.0f}%)")
                print(f"  Drift:                    {g['drift_pct']:+.1f} %")
                print(f"  {'✓ im Band' if g['ok'] else '⚠ ' + g['reason']}")
        except Exception as exc:
            logger.debug("Sizing-Drift uebersprungen: %s", exc)

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
