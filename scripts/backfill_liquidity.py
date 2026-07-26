#!/usr/bin/env python3
"""Backfill/Refresh von market_cap (+ggf. adv_usd) via yfinance fast_info.

feat/liquidity-tiering (2026-07-26): Das Ranking im signal_worker nutzt
liquidity_factor(market_cap, adv_usd). adv_usd schreibt der data_worker im
5-Minuten-Takt selbst; market_cap kommt nur aus diesem Script (fast_info).

Scope-Steuerung (Default: nur relevante Instrumente, nicht alle 15k):
  --scope active     Watchlist + offene Positionen + Signale der letzten 7 Tage
  --scope stale      wie active, aber nur Eintraege ohne market_cap oder
                     aelter als --max-age-days (Default 30)
  --scope symbols    explizite Symbole via --symbols AAPL,MSFT,...

Beispiele:
  PYTHONPATH=src python3 scripts/backfill_liquidity.py --scope active --limit 100
  PYTHONPATH=src python3 scripts/backfill_liquidity.py --scope stale
  PYTHONPATH=src python3 scripts/backfill_liquidity.py --scope symbols --symbols NVDA,KTA.DE

Rate-Limit-schonend: 0.3s Pause pro Ticker, --limit Deckel (Default 150).
Fehler pro Symbol sind nicht fatal (weiter mit dem naechsten).
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from bot.core.liquidity import (  # noqa: E402
    currency_factor,
    ensure_liquidity_columns,
    update_market_cap,
)
from bot.db.connection import DB  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = PROJECT_ROOT / "data" / "trading.db"

PAUSE_S = 0.3


def _select_targets(db: DB, scope: str, symbols_arg: str, max_age_days: int, limit: int):
    if scope == "symbols":
        wanted = [s.strip().upper() for s in symbols_arg.split(",") if s.strip()]
        if not wanted:
            print("--scope symbols braucht --symbols A,B,C")
            sys.exit(2)
        placeholders = ",".join("?" for _ in wanted)
        return db.fetchall(
            f"SELECT instrument_id, symbol, yfinance_symbol FROM instruments "
            f"WHERE UPPER(symbol) IN ({placeholders}) AND yfinance_symbol IS NOT NULL",
            tuple(wanted),
        )

    stale_clause = ""
    if scope == "stale":
        stale_clause = (
            "AND (i.market_cap IS NULL OR i.market_cap_updated_at IS NULL "
            f"OR i.market_cap_updated_at < datetime('now', '-{int(max_age_days)} days')) "
        )
    rows = db.fetchall(
        "SELECT DISTINCT i.instrument_id, i.symbol, i.yfinance_symbol "
        "FROM instruments i "
        "WHERE i.yfinance_symbol IS NOT NULL "
        "  AND (i.is_tradable IS NULL OR i.is_tradable = 1) "
        f" {stale_clause}"
        "  AND (i.instrument_id IN (SELECT instrument_id FROM watchlist WHERE instrument_id IS NOT NULL) "
        "       OR i.instrument_id IN (SELECT DISTINCT instrument_id FROM portfolio_snapshot) "
        "       OR i.instrument_id IN (SELECT DISTINCT instrument_id FROM signals "
        "                              WHERE generated_at > datetime('now', '-7 days'))) "
        "ORDER BY i.instrument_id LIMIT ?",
        (limit,),
    )
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scope", choices=("active", "stale", "symbols"), default="stale")
    parser.add_argument("--symbols", default="")
    parser.add_argument("--limit", type=int, default=150)
    parser.add_argument("--max-age-days", type=int, default=30)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    import yfinance as yf

    db = DB(db_path=str(DB_PATH))
    ensure_liquidity_columns(db)

    targets = _select_targets(db, args.scope, args.symbols, args.max_age_days, args.limit)
    print(f"{len(targets)} Instrumente im Scope '{args.scope}' (Limit {args.limit})")

    n_ok = n_skip = n_err = 0
    for row in targets:
        instrument_id, symbol, yf_sym = row[0], row[1], row[2]
        try:
            fi = yf.Ticker(yf_sym).fast_info
            mc_local = getattr(fi, "market_cap", None)
            if mc_local is None:
                mc_local = fi.get("market_cap") if hasattr(fi, "get") else None
            if not mc_local or mc_local <= 0:
                n_skip += 1
                continue
            # fast_info liefert Market-Cap in Listing-Waehrung; grobe
            # USD-Naeherung reicht fuers Tier-Bucketing (300M/2B/10B).
            mc_usd = float(mc_local) * currency_factor(yf_sym)
            if args.dry_run:
                print(f"  DRY {symbol:14s} ({yf_sym}): market_cap ~= {mc_usd/1e9:.2f}B USD")
            else:
                update_market_cap(db, instrument_id, mc_usd)
            n_ok += 1
        except Exception as exc:
            n_err += 1
            print(f"  WARN {symbol} ({yf_sym}): {exc}")
        time.sleep(PAUSE_S)

    print(f"Fertig: {n_ok} aktualisiert, {n_skip} ohne Market-Cap, {n_err} Fehler")
    return 0


if __name__ == "__main__":
    sys.exit(main())
