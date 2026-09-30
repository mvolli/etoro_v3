#!/usr/bin/env python3
"""One-off backfill of ma200_daily for CRYPTO symbols (2026-09-30).

WHY: the MA200 shadow gate (commit 61566c8) only evaluates symbols with >=201
closed daily closes in ma200_daily; everything else fails OPEN. The original
seed (data/exit_replay_bars/*.csv) covered 284 equity symbols only — crypto
(148 active instruments, the bot's #1 unevaluated class) + FX are uncovered.
This script backfills the missing yfinance-mapped symbols with 2y of daily
history so the shadow ledger can accumulate on them.

RULES (frozen, see ma200_history.py):
  - closed bars only (today's in-progress bar dropped)
  - INSERT OR IGNORE idempotency — safe to re-run
  - fail-open per symbol; never touches ohlcv_daily
NOTE: splits — yfinance back-adjusts the whole series; if a symbol splits
after we store rows, stored history goes stale (known, advisor-noted).

Usage:  python3 scripts/_backfill_ma200_crypto.py [--dry-run]
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from bot.core import ma200_history as mh  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="list symbols, fetch nothing")
    ap.add_argument("--period", default="2y",
                    help="yfinance period for the backfill (default 2y)")
    ap.add_argument("--sleep", type=float, default=0.3,
                    help="sleep between fetches (rate-limit courtesy)")
    args = ap.parse_args()

    from bot.db.connection import DB
    db = DB(db_path=str(REPO / "data" / "trading.db"), busy_timeout_ms=5000)
    mh.ensure_table(db)

    # Universe = symbols the bot actually evaluates: watchlist ∪ 90d-closed trades.
    # NOT all 4465 active instruments (most are never scored → wasted fetches).
    syms = [r["yf"] for r in db.fetchall(
        """
        SELECT i.yfinance_symbol AS yf
        FROM watchlist w JOIN instruments i ON i.instrument_id = w.instrument_id
        WHERE i.yfinance_symbol IS NOT NULL AND i.yfinance_symbol != ''
        UNION
        SELECT i.yfinance_symbol
        FROM trades t JOIN instruments i ON i.instrument_id = t.instrument_id
        WHERE t.closed_at >= datetime('now', '-90 day')
          AND i.yfinance_symbol IS NOT NULL AND i.yfinance_symbol != ''
        ORDER BY i.yfinance_symbol
        """
    ) if r["yf"]]
    # Skip symbols already fully covered (>=201 closes) — idempotent re-runs,
    # and avoids re-fetching the 282 symbols the original equity seed already seeded.
    covered = {r["symbol"] for r in db.fetchall(
        "SELECT symbol FROM ma200_daily GROUP BY symbol HAVING COUNT(*) >= 201")}
    before = len(syms)
    syms = [s for s in syms if s not in covered]
    print(f"backfill: {len(syms)} symbols (skipped {before - len(syms)} already covered), "
          f"period={args.period}")
    if args.dry_run:
        for s in syms[:20]:
            print("  ", s)
        if len(syms) > 20:
            print(f"   ... +{len(syms) - 20} more")
        return 0

    ok = fail = short = 0
    total_rows = 0
    for i, sym in enumerate(syms, 1):
        df = None
        for attempt in (1, 2):  # 1 retry w/ backoff (yfinance rate-limits)
            try:
                import yfinance as yf
                df = yf.download(sym, period=args.period, interval="1d",
                                 auto_adjust=True, progress=False, threads=False)
                break
            except Exception:
                df = None
            if attempt == 1:
                time.sleep(5.0)
        try:
            if df is None or len(df) == 0:
                fail += 1
                print(f"[{i:3d}/{len(syms)}] {sym:<14} NO DATA", flush=True)
                time.sleep(args.sleep)
                continue
            import pandas as pd
            if isinstance(df.columns, pd.MultiIndex):
                df = df.droplevel(axis=1, level=1)
            pairs = mh.df_to_closed_pairs(df)
            n = mh.upsert_closes(db, sym, pairs)
            total_rows += n
            if len(pairs) < 201:
                short += 1
                tag = f"SHORT({len(pairs)})"
            else:
                ok += 1
                tag = "OK"
            print(f"[{i:3d}/{len(syms)}] {sym:<14} +{n:4d} rows {tag}", flush=True)
        except Exception as exc:  # noqa: BLE001 — fail-open per symbol
            fail += 1
            print(f"[{i:3d}/{len(syms)}] {sym:<14} FAIL {type(exc).__name__}: {exc}", flush=True)
        time.sleep(args.sleep)

    # coverage summary
    tot_rows = db.fetchone("SELECT COUNT(*) AS c FROM ma200_daily")
    n_sym_cov = db.fetchone(
        "SELECT COUNT(*) AS c FROM (SELECT 1 FROM ma200_daily GROUP BY symbol HAVING COUNT(*) >= 201)"
    )
    print(f"\nDONE ok={ok} short={short} fail={fail} rows_added={total_rows}")
    print(f"ma200_daily: {tot_rows['c'] if tot_rows else '?'} rows, "
          f"symbols>=201 closes: {n_sym_cov['c'] if n_sym_cov else '?'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
