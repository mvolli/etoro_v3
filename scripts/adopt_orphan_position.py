#!/usr/bin/env python3
"""Eine Broker-Position adoptieren, die der Bot nicht kennt.

fix/veto-inflight (2026-09-12). Trade #2174 (VU.PA) wurde als Order
1582187392 abgeschickt, deferred, und im naechsten Zyklus vom LLM-Veto auf
REJECTED gesetzt — waehrend die Order beim Broker lag. eToro fuehrte sie
09:31:15 aus. Ergebnis: Position 3575889260 ist live, traegt broker-seitig
einen Stop-Loss, fehlt aber im `trades`-Ledger. Damit fehlt sie in der
PnL-Attribution, in jedem Report und in der Profit-Ladder-Verwaltung.

Der Code-Fix verhindert neue Faelle. Dieses Skript holt den bestehenden
nach: es belebt die VORHANDENE Trade-Zeile wieder, statt eine neue
anzulegen — sie traegt bereits order_id, signal_id, Betrag, Symbol und
Instrument, und nur so bleibt die Signal-Herkunft fuer die Lernschleife
erhalten.

Geschrieben wird per direktem SQL, NICHT ueber den Event-Posting-Pfad:
ein tagealter Fill soll kein Discord-Embed ausloesen.

DRY-RUN ist Standard. Schreiben nur mit --apply.

    PYTHONPATH=src /usr/bin/python3 scripts/adopt_orphan_position.py \
        --trade 2174 --position 3575889260 [--apply]
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC = PROJECT_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

DB_PATH = PROJECT_ROOT / "data" / "trading.db"


def _keys() -> tuple[str, str]:
    """ETORO_BOT_API_KEY / ETORO_BOT_USER_KEY aus ~/.hermes/.env."""
    import os
    env: dict[str, str] = {}
    p = Path.home() / ".hermes" / ".env"
    if p.exists():
        for line in p.read_text().splitlines():
            m = re.match(r"^([A-Z0-9_]+)\s*=\s*(.+)$", line.strip())
            if m:
                env[m.group(1)] = m.group(2).strip().strip('"').strip("'")
    api = env.get("ETORO_BOT_API_KEY") or os.environ.get("ETORO_BOT_API_KEY", "")
    user = env.get("ETORO_BOT_USER_KEY") or os.environ.get("ETORO_BOT_USER_KEY", "")
    if not api or not user:
        raise RuntimeError("ETORO_BOT_API_KEY / ETORO_BOT_USER_KEY fehlen")
    return api, user


def _broker_position(position_id: str) -> dict:
    from bot.api.client import EToroClient
    client = EToroClient(*_keys())
    payload = client.get_portfolio().get("clientPortfolio", {})
    for pos in payload.get("positions", []):
        if str(pos.get("positionID")) == str(position_id):
            return pos
    raise SystemExit(f"ABBRUCH: Position {position_id} ist beim Broker nicht offen.")


def _to_db_ts(iso: str) -> str:
    """'2026-09-11T09:31:15.443Z' -> '2026-09-11 09:31:15' (DB-Format)."""
    return str(iso).replace("T", " ")[:19]


def pruefen(db, trade_id: int, position_id: str, pos: dict) -> dict:
    """Alle Vorbedingungen. Wirft SystemExit, sobald eine nicht haelt."""
    row = db.execute(
        "SELECT id, symbol, instrument_id, status, amount_usd, order_id, "
        "api_position_id FROM trades WHERE id=?", (trade_id,)
    ).fetchone()
    if row is None:
        raise SystemExit(f"ABBRUCH: Trade #{trade_id} existiert nicht.")
    trade = dict(row)

    if trade["api_position_id"]:
        raise SystemExit(
            f"ABBRUCH: Trade #{trade_id} traegt bereits api_position_id="
            f"{trade['api_position_id']} — nichts zu adoptieren.")

    fremd = db.execute(
        "SELECT id, status FROM trades WHERE api_position_id=?", (position_id,)
    ).fetchone()
    if fremd is not None:
        raise SystemExit(
            f"ABBRUCH: Position {position_id} gehoert bereits Trade "
            f"#{fremd['id']} ({fremd['status']}).")

    if str(trade["order_id"] or "") != str(pos.get("orderID") or ""):
        raise SystemExit(
            f"ABBRUCH: order_id passt nicht — Trade #{trade_id} hat "
            f"{trade['order_id']}, die Position stammt aus Order "
            f"{pos.get('orderID')}. Das waere eine andere Order.")

    if int(trade["instrument_id"] or 0) != int(pos.get("instrumentID") or 0):
        raise SystemExit(
            f"ABBRUCH: instrument_id passt nicht — Trade {trade['instrument_id']}, "
            f"Position {pos.get('instrumentID')}.")

    # Betrag: der Broker rundet, deshalb Toleranz statt Gleichheit.
    if abs(float(trade["amount_usd"] or 0) - float(pos.get("amount") or 0)) > 1.0:
        raise SystemExit(
            f"ABBRUCH: Betrag weicht ab — Trade ${trade['amount_usd']}, "
            f"Position ${pos.get('amount')}.")

    return trade


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--trade", type=int, required=True)
    ap.add_argument("--position", required=True)
    ap.add_argument("--apply", action="store_true",
                    help="ohne dies: nur anzeigen, nichts schreiben")
    args = ap.parse_args()

    from bot.db.connection import DB
    db = DB(db_path=DB_PATH)

    pos = _broker_position(args.position)
    trade = pruefen(db, args.trade, args.position, pos)

    open_rate = float(pos["openRate"])
    sl_rate = float(pos.get("stopLossRate") or 0) or None
    confirmed = _to_db_ts(pos["openDateTime"])
    # stop_loss_pct aus der Broker-Wahrheit neu rechnen: die Zeile trug den
    # Wert der ENTSCHEIDUNG (5.5995), der Fill liegt woanders. Ueberall sonst
    # gilt stop_loss_price == entry_price * (1 - pct/100) — diese Invariante
    # soll auch hier halten.
    sl_pct = (1.0 - sl_rate / open_rate) * 100.0 if sl_rate else None
    grund = (f"Adoptiert {args.position}: Order {trade['order_id']} war beim "
             f"Broker ausgefuehrt, waehrend der Trade als REJECTED galt "
             f"(fix/veto-inflight).")

    unreal = pos.get("unrealizedPnL") or {}
    print(f"Trade #{trade['id']} {trade['symbol']}  (Status {trade['status']})")
    print(f"  Position      {args.position}  aus Order {pos.get('orderID')}")
    print(f"  eroeffnet     {confirmed}  ({pos.get('units')} Einheiten)")
    print(f"  entry_price   {open_rate}   (openRate, Instrumentenwaehrung)")
    alt_pct = db.execute("SELECT stop_loss_pct FROM trades WHERE id=?",
                         (args.trade,)).fetchone()[0]
    print(f"  stop_loss     {sl_rate}  ->  {sl_pct:.4f}%  "
          f"(Zeile trug {float(alt_pct):.4f}% aus der Entscheidung)")
    print(f"  amount_usd    {trade['amount_usd']}  (Broker: {pos.get('amount')})")
    print(f"  aktuell       {unreal.get('pnL'):+.2f} USD")
    print()

    if not args.apply:
        print("DRY-RUN — nichts geschrieben. Mit --apply ausfuehren.")
        return 0

    db.execute(
        """
        UPDATE trades
           SET status='ACTIVE', api_position_id=?, confirmed_at=?,
               entry_price=?, stop_loss_price=?, stop_loss_pct=?,
               rejection_reason=?
         WHERE id=? AND status='REJECTED' AND (api_position_id IS NULL OR api_position_id='')
        """,
        (str(args.position), confirmed, open_rate, sl_rate, sl_pct, grund, args.trade),
    )
    # Ledger-Zeile fuer die Kostenattribution. Direktes SQL: der
    # Event-Posting-Pfad wuerde ein Discord-Embed fuer einen tagealten
    # Fill erzeugen.
    db.execute(
        """
        INSERT INTO trade_events
            (trade_id, position_id, order_id, instrument_id, symbol, event_type,
             source, event_at, units, price, amount_usd, reported_final, reason)
        VALUES (?, ?, ?, ?, ?, 'OPEN', 'adopt_orphan_position', ?, ?, ?, ?, 1, ?)
        """,
        (args.trade, str(args.position), str(trade["order_id"]),
         trade["instrument_id"], trade["symbol"], confirmed,
         pos.get("units"), open_rate, trade["amount_usd"], grund),
    )
    # Kein db.commit(): der DB-Wrapper committet je execute() selbst.

    nach = dict(db.execute(
        "SELECT status, api_position_id, entry_price, stop_loss_price, confirmed_at "
        "FROM trades WHERE id=?", (args.trade,)).fetchone())
    print("GESCHRIEBEN:", nach)
    return 0


if __name__ == "__main__":
    sys.exit(main())
