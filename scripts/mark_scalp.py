#!/usr/bin/env python3
"""
mark_scalp.py — Eine offene Position als kurzfristigen Scalp-Trade markieren
(oder zurueck auf Swing setzen).

Eine als 'scalp' getaggte Position bekommt beim naechsten risk_worker-Lauf eine
zusaetzliche, sehr fruehe erste Profit-Stufe (ATR x2, clamp [2%,5%]) und sichert
so schnell einen Teilgewinn, statt auf die Swing-Leiter (+6/+10/+18%) zu warten.
Der universelle Momentum-Fade-Schutz greift ohnehin fuer JEDE Position — dieses
Tag steuert nur die zusaetzliche fruehe Stufe.

Nutzung:
    # offene Positionen mit ihren IDs + aktueller PnL anzeigen:
    PYTHONPATH=src python3 scripts/mark_scalp.py --list

    # per Symbol taggen (loest die Position-ID ueber die Live-Portfolio-API auf):
    PYTHONPATH=src python3 scripts/mark_scalp.py --symbol TSLA

    # per Position-ID taggen:
    PYTHONPATH=src python3 scripts/mark_scalp.py --position-id 123456789

    # zurueck auf Swing:
    PYTHONPATH=src python3 scripts/mark_scalp.py --symbol TSLA --strategy swing
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from bot.config import load_config
from bot.db.connection import DB
from bot.api.client import EToroClient, ClientConfig
from bot.core.trailing_stop import set_strategy, load_position_dynamic


def _open_positions(client: EToroClient) -> list[dict]:
    portfolio = client.get_portfolio() or {}
    return (portfolio.get("clientPortfolio", {}) or {}).get("positions", []) or []


def main() -> int:
    ap = argparse.ArgumentParser(description="Tag an open position as scalp/swing.")
    ap.add_argument("--list", action="store_true", help="list open positions and exit")
    ap.add_argument("--symbol", help="symbol of the position to tag (resolved via live portfolio)")
    ap.add_argument("--position-id", help="position ID to tag directly")
    ap.add_argument("--strategy", choices=("scalp", "swing"), default="scalp",
                    help="strategy tag to set (default: scalp)")
    args = ap.parse_args()

    cfg = load_config()
    db = DB(db_path=PROJECT_ROOT / cfg["db"]["path"],
            busy_timeout_ms=cfg["db"].get("busy_timeout_ms", 5000))

    client = EToroClient(
        api_key=os.environ.get("ETORO_API_KEY", ""),
        user_key=os.environ.get("ETORO_USER_KEY", ""),
        config=ClientConfig.from_dict(cfg.get("api", {})),
    )

    try:
        positions = _open_positions(client)
    except Exception as exc:
        print(f"⚠️  Konnte Live-Portfolio nicht laden: {exc}")
        positions = []

    if args.list or (not args.symbol and not args.position_id):
        pos_ids = [str(p.get("positionID") or p.get("positionId") or "") for p in positions]
        meta = load_position_dynamic(db, [p for p in pos_ids if p])
        print(f"{'POSITION_ID':>14}  {'SYMBOL':<10} {'PnL%':>7}  {'STRATEGY':<8}")
        print("-" * 46)
        for p in positions:
            pid = str(p.get("positionID") or p.get("positionId") or "")
            sym = p.get("symbol", str(p.get("instrumentID", "")))
            amount = float(p.get("amount", 0) or 0)
            upnl = p.get("unrealizedPnL") or {}
            pnl_usd = float(upnl.get("pnL", 0)) if isinstance(upnl, dict) else 0.0
            pnl_pct = (pnl_usd / amount * 100) if amount > 0 else 0.0
            strat = meta.get(pid, {}).get("strategy", "swing")
            print(f"{pid:>14}  {sym:<10} {pnl_pct:>6.1f}%  {strat:<8}")
        if not args.symbol and not args.position_id:
            return 0

    # Resolve target position(s)
    targets: list[tuple[str, str]] = []  # (position_id, symbol)
    if args.position_id:
        sym = next(
            (p.get("symbol", "") for p in positions
             if str(p.get("positionID") or p.get("positionId") or "") == args.position_id),
            "",
        )
        targets.append((args.position_id, sym))
    if args.symbol:
        matched = [
            (str(p.get("positionID") or p.get("positionId") or ""), p.get("symbol", ""))
            for p in positions
            if str(p.get("symbol", "")).upper() == args.symbol.upper()
        ]
        if not matched:
            print(f"❌ Kein offenes Instrument mit Symbol '{args.symbol}' gefunden.")
            return 1
        targets.extend(matched)

    for pid, sym in targets:
        if not pid:
            print("❌ Leere Position-ID — uebersprungen.")
            continue
        set_strategy(db, pid, sym or "", args.strategy)
        print(f"✅ {sym or pid} (ID {pid}) → strategy='{args.strategy}'")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
