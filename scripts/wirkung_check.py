#!/usr/bin/env python3
"""Wirkungskontrolle der Aenderungen vom 2026-08-24.

Zeigt zwei Dinge nebeneinander:
  1. Die eligible-Filter-Zaehler (warum Kandidaten aussortiert wurden)
  2. Den Restanteil offener Positionen (greift die 50-%-Untergrenze?)

Ausgangslage am 2026-08-24 zum Vergleich:
  Restanteil je Position:  Median 11 %  (min 2 %, max 100 %)
  Peak-PnL offen:          Median 9.89 %
  realisierte Gewinner:    Median 0.27 %
  momentum_faded:          36 von 42 Positionen (86 %)

Aufruf:  python3 scripts/wirkung_check.py [--tage N]
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import statistics as st
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DB = REPO / "data" / "trading.db"


def _q(c, sql, params=()):
    return c.execute(sql, params).fetchall()


def _pct(xs, p):
    xs = sorted(xs)
    if not xs:
        return None
    return xs[min(len(xs) - 1, int(round(p / 100.0 * (len(xs) - 1))))]


def zaehler(c, tage: int) -> None:
    print("=" * 72)
    print("1) ELIGIBLE-FILTER — warum Kandidaten aussortiert wurden")
    print("=" * 72)
    rows = _q(c, """
        SELECT ts, message, details FROM system_log
        WHERE worker = 'signal_worker' AND message LIKE 'eligible-Filter%'
          AND ts >= datetime('now', ?)
        ORDER BY ts DESC
    """, (f"-{tage} days",))
    if not rows:
        print("  Keine Zaehler-Zeilen im Zeitraum.")
        print("  (Der Worker schreibt sie nur, wenn ueberhaupt BUY-Signale anlagen.)")
        return

    gesamt: dict[str, int] = {}
    ein = aus = 0
    for r in rows:
        try:
            d = json.loads(r["details"] or "{}")
            for k, v in (d.get("skip_counts") or {}).items():
                gesamt[k] = gesamt.get(k, 0) + int(v)
        except Exception:
            pass
        msg = r["message"]
        try:
            teil = msg.split(":", 1)[1].split("|")[0]
            a, b = teil.split("->")
            ein += int(a.strip()); aus += int(b.strip().split()[0])
        except Exception:
            pass

    print(f"  {len(rows)} Laeufe im Zeitraum   Signale {ein} -> Kandidaten {aus}")
    if gesamt:
        print("\n  Aussortiert nach Grund:")
        for k, v in sorted(gesamt.items(), key=lambda kv: -kv[1]):
            print(f"    {k:26} {v:>5}")
    else:
        print("  (keine Aussortierungen protokolliert)")
    print("\n  Die letzten Zeilen im Wortlaut:")
    for r in rows[:5]:
        print(f"    {str(r['ts'])[5:19]}  {r['message'][:96]}")


def restanteil(c) -> None:
    print()
    print("=" * 72)
    print("2) RESTANTEIL — greift die 50-%-Untergrenze?")
    print("=" * 72)
    rows = _q(c, """
        SELECT p.symbol, p.amount_usd AS jetzt, p.unrealized_pnl_pct AS upnl,
               t.amount_usd AS einstieg, ps.remaining_frac AS frac,
               ps.peak_pnl_pct AS peak, ps.momentum_faded AS faded
        FROM portfolio_snapshot p
        LEFT JOIN trades t ON t.api_position_id = p.api_position_id
        LEFT JOIN position_state ps ON ps.position_id = p.api_position_id
    """)
    if not rows:
        print("  Keine offenen Positionen.")
        return

    mit = [r for r in rows if r["einstieg"]]
    if mit:
        q = [100.0 * r["jetzt"] / r["einstieg"] for r in mit]
        print(f"  {len(mit)} von {len(rows)} Positionen mit auffindbarem Einstieg")
        print(f"    Restanteil  Median {st.median(q):.0f} %   p25 {_pct(q,25):.0f} %   "
              f"p75 {_pct(q,75):.0f} %   min {min(q):.0f} %")
        print(f"    Vergleich 2026-08-24:  Median 11 %   <- Ziel: deutlich hoeher")
        print(f"    Summe Einstieg {sum(r['einstieg'] for r in mit):.0f} USD "
              f"-> Bestand {sum(r['jetzt'] for r in mit):.0f} USD")

    fr = [r["frac"] for r in rows if r["frac"] is not None]
    if fr:
        print(f"\n  position_state.remaining_frac gefuehrt fuer {len(fr)} Positionen"
              f"   Median {st.median(fr)*100:.0f} %   min {min(fr)*100:.0f} %")
        unter = [f for f in fr if f < 0.5]
        print(f"    unter 50 %: {len(unter)}  (sollte 0 sein — darunter wird ganz geschlossen)")
    else:
        print("\n  remaining_frac noch nicht gefuellt (wird bei jedem Teilverkauf gesetzt)")

    peaks = [r["peak"] for r in rows if r["peak"] is not None]
    if peaks:
        print(f"\n  Peak-PnL   Median {st.median(peaks):.2f} %   (Vergleich: 9.89 %)")
    faded = sum(1 for r in rows if r["faded"])
    print(f"  momentum_faded: {faded} von {len(rows)} "
          f"({100.0*faded/len(rows):.0f} %)   Vergleich: 86 %")

    print("\n  Realisierte Gewinner (geschlossen seit 2026-08-24):")
    g = [r["pnl_pct"] for r in _q(c, """
        SELECT pnl_pct FROM trades
        WHERE pnl_pct > 0 AND closed_at >= '2026-08-24'
    """)]
    if g:
        print(f"    n={len(g)}  Median {st.median(g):.2f} %   (Vergleich vorher: 0.27 %)")
    else:
        print("    noch keine")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tage", type=int, default=1)
    args = ap.parse_args()
    c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    zaehler(c, args.tage)
    restanteil(c)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
