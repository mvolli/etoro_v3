#!/usr/bin/env python3
"""Ein- und Auszahlungen des Bot-Kontos buchen.

fix/capital-ledger (2026-09-10). `reconcile()` rechnet gegen die Summe
aller Kapitalbewegungen. Ohne Buchung sieht frisches Geld wie verschwundene
Kosten aus: Eine Auffuellung von 7.834 auf 10.000 USD wuerde das Residuum
von -2.403 auf -237 USD "verbessern", ohne dass sich real etwas aendert.

  Stand ansehen:   scripts/capital_event.py
  Einzahlung:      scripts/capital_event.py --amount 2166.00 --note "Auffuellung" --apply
  Auszahlung:      scripts/capital_event.py --amount -500.00 --note "Entnahme"   --apply

Ohne --apply passiert nichts.
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


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--amount", type=float,
                    help="USD, positiv = Einzahlung, negativ = Auszahlung")
    ap.add_argument("--note", default="", help="Begruendung (empfohlen)")
    ap.add_argument("--at", default=None,
                    help="Zeitpunkt 'YYYY-MM-DD HH:MM:SS' (Default: jetzt)")
    ap.add_argument("--apply", action="store_true", help="wirklich buchen")
    args = ap.parse_args()

    from bot.db.connection import DB
    from bot.db.repo import CapitalRepo

    with DB(DB_PATH) as db:
        repo = CapitalRepo(db)

        print("── Kapital-Ledger ─────────────────────────────────────────────")
        for r in repo.all():
            print(f"  {str(r['occurred_at'])[:19]}  ${r['amount_usd']:>+11,.2f}"
                  f"   {r['note'] or ''}")
        basis = repo.base()
        print(f"  {'-' * 58}")
        print(f"  Basis fuer die Rekonziliation             ${basis:>+12,.2f}")

        eq = db.fetchone(
            "SELECT value FROM system_state WHERE key='CURRENT_EQUITY'")
        if eq:
            print(f"  aktuelle Equity                           "
                  f"${float(eq['value']):>12,.2f}")

        if args.amount is None:
            print("\n  (nur Bericht — mit --amount buchen)")
            return 0

        print(f"\n  Buchung: ${args.amount:+,.2f}   {args.note or '(ohne Notiz)'}")
        print(f"  neue Basis waere:                         "
              f"${basis + args.amount:>+12,.2f}")

        if not args.apply:
            print("\n  (DRY-RUN — nichts gebucht. Mit --apply ausfuehren.)")
            return 0
        if not args.note:
            print("\n  ABBRUCH: --note ist bei einer echten Buchung Pflicht.")
            print("  Eine Kapitalbewegung ohne Begruendung ist spaeter nicht")
            print("  mehr rekonstruierbar.")
            return 2

        eid = repo.add(args.amount, args.note, args.at)
        if eid is None:
            print("\n  FEHLER: Buchung fehlgeschlagen.")
            return 1
        print(f"\n  gebucht (id={eid}). Neue Basis: ${repo.base():+,.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
