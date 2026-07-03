#!/usr/bin/env python3
"""Re-test and reactivate EU instruments deactivated by a wrong yfinance_symbol.

Root cause (analyzed 2026-07-03): most EU instruments (.DE/.PA/.L/.ST/.MI/.CO/
.SW/.LS/.OL/.MC) have a garbled yfinance_symbol (e.g. BMWA.DE instead of the
correct BMW.DE) that never matched anything on Yahoo. The 3-strikes rule
(fix/yfinance-fallback-resolution) had no EU fallback candidates, so these
were marked yahoo_status='delisted' and deactivated by cleanup_instruments.py
even though the eToro `symbol` column was correct the whole time.

This script re-verifies each affected row against live Yahoo Finance using
bot.core.ohlcv_cache.generate_symbol_candidates(yfinance_symbol,
original_symbol=symbol) — the same candidate logic data_worker now uses at
runtime — and on success corrects yfinance_symbol + reactivates the row.
Genuine delistings (e.g. Abertis/Jazztel, both acquired years ago) are left
untouched; they get reported separately for manual review.

Usage:
    python3 scripts/fix_eu_yfinance_symbols.py                # dry-run report
    python3 scripts/fix_eu_yfinance_symbols.py --apply         # write changes
    python3 scripts/fix_eu_yfinance_symbols.py --apply --limit 50
    python3 scripts/fix_eu_yfinance_symbols.py --apply --all-instruments  # not just watchlist
"""
from __future__ import annotations

import argparse
import csv
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from bot.core.ohlcv_cache import generate_symbol_candidates  # noqa: E402
from bot.db.connection import DB  # noqa: E402

RATE_LIMIT_S = 0.3
MIN_ROWS = 3  # matches the manual sample-test threshold


def _backup_db(db_path: Path) -> Path:
    backup_dir = db_path.parent / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    backup_path = backup_dir / f"trading-{ts}.db"
    src = sqlite3.connect(str(db_path))
    try:
        dst = sqlite3.connect(str(backup_path))
        with dst:
            src.backup(dst)
        dst.close()
    finally:
        src.close()
    return backup_path


def _load_candidates(db: DB, watchlist_only: bool, limit: int | None) -> list[dict]:
    if watchlist_only:
        query = """
            SELECT DISTINCT i.instrument_id, i.symbol, i.yfinance_symbol
            FROM watchlist w
            JOIN instruments i ON w.instrument_id = i.instrument_id
            WHERE i.market_region = 'EU'
              AND i.yahoo_status = 'delisted'
              AND i.symbol != i.yfinance_symbol
            ORDER BY i.instrument_id
        """
    else:
        query = """
            SELECT instrument_id, symbol, yfinance_symbol
            FROM instruments
            WHERE market_region = 'EU'
              AND yahoo_status = 'delisted'
              AND symbol != yfinance_symbol
            ORDER BY instrument_id
        """
    rows = db.fetchall(query)
    items = [{"instrument_id": r[0], "symbol": r[1], "yfinance_symbol": r[2]} for r in rows]
    if limit:
        items = items[:limit]
    return items


def _try_resolve(symbol: str, yfinance_symbol: str) -> tuple[str | None, int]:
    """Try each candidate against live Yahoo. Returns (winning_ticker, rows) or (None, 0)."""
    import yfinance as yf

    candidates = [
        c for c in generate_symbol_candidates(yfinance_symbol, original_symbol=symbol)
        if c != yfinance_symbol
    ]
    for cand in candidates:
        try:
            df = yf.Ticker(cand).history(period="5d")
            if not df.empty and len(df) >= MIN_ROWS:
                return cand, len(df)
        except Exception:
            pass
        time.sleep(RATE_LIMIT_S)
    return None, 0


def main() -> int:
    p = argparse.ArgumentParser(description="Re-verify and reactivate EU instruments with a broken yfinance_symbol")
    p.add_argument("--apply", action="store_true", help="Write changes (default: dry-run report only)")
    p.add_argument("--all-instruments", action="store_true",
                    help="Scan the whole instruments table, not just current watchlist members")
    p.add_argument("--limit", type=int, default=None, help="Cap number of rows processed")
    p.add_argument("--report", default=str(PROJECT_ROOT / "data" / "eu_yfinance_fix_report.csv"))
    args = p.parse_args()

    cfg_db_path = PROJECT_ROOT / "data" / "trading.db"
    db = DB(db_path=cfg_db_path)

    items = _load_candidates(db, watchlist_only=not args.all_instruments, limit=args.limit)
    scope = "watchlist-only" if not args.all_instruments else "ALL instruments"
    print(f"Scope: {scope} — {len(items)} candidate rows to test\n")

    if args.apply and items:
        backup_path = _backup_db(cfg_db_path)
        print(f"Backup written: {backup_path}\n")

    rescued, dead = [], []
    for item in items:
        iid, symbol, yf_sym = item["instrument_id"], item["symbol"], item["yfinance_symbol"]
        winner, n_rows = _try_resolve(symbol, yf_sym)
        if winner:
            rescued.append({**item, "resolved_symbol": winner, "rows": n_rows})
            print(f"  RESCUED  {symbol:15s} ({yf_sym:15s} -> {winner:15s})  {n_rows} rows")
            if args.apply:
                db.execute(
                    """UPDATE instruments
                       SET yfinance_symbol = ?, yahoo_status = 'ok',
                           yahoo_fail_count = 0, is_active = 1,
                           last_updated = CURRENT_TIMESTAMP
                       WHERE instrument_id = ?""",
                    (winner, iid),
                )
        else:
            dead.append(item)
            print(f"  DEAD     {symbol:15s} ({yf_sym})  no candidate resolved — left untouched")

    with open(args.report, "w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=["instrument_id", "symbol", "yfinance_symbol", "resolved_symbol", "rows", "status"])
        writer.writeheader()
        for r in rescued:
            writer.writerow({**r, "status": "RESCUED" + (" (applied)" if args.apply else " (dry-run)")})
        for d in dead:
            writer.writerow({**d, "resolved_symbol": "", "rows": 0, "status": "STILL_DEAD"})

    print(f"\n{'='*60}")
    print(f"Rescued: {len(rescued)}/{len(items)}   Still dead/unresolved: {len(dead)}/{len(items)}")
    print(f"Report written to {args.report}")
    if not args.apply and rescued:
        print("\nDry-run only — re-run with --apply to write these changes.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
