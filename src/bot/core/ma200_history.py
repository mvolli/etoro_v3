"""
eToro Trading Bot V3 — MA200 daily-close history
src/bot/core/ma200_history.py

feat/ma200-filter (2026-09-29, advisor-frozen): the entry-quality MA200 gate
needs ~201 CLOSED daily closes per symbol, but the live OHLCV refresh only
carries 3mo (~65 bars). This module keeps a per-symbol daily-close history so
the frozen rule ``close[D-1] < SMA200(close[D-200..D-1])`` is evaluable on
every signal.

Keying
------
Rows are keyed by the YAHOO/yfinance symbol (e.g. ``AAPL``, ``0914.HK``) — the
same key the live fetch uses and the key of the 1-year replay CSVs, so a
one-time seed (``scripts/_seed_ma200_history.py``) and the per-run live upsert
write to the same series.

Freshness / correctness
-----------------------
- Only CLOSED bars are stored: the caller drops the last row of the live
  ``df`` when it is today's (intraday, unsettled) bar. The last stored bar is
  therefore D-1, exactly the decision bar of the frozen rule (live-faithful
  S12b_L2 semantics).
- ``upsert_closes`` is idempotent (INSERT OR IGNORE on PK symbol+date), so it
  self-heals gaps and is safe to run every cycle.

Idempotenz (AGENTS.md): ``ensure_table`` uses ``CREATE TABLE IF NOT EXISTS``
and runs once per worker start (best-effort, fail-open).
"""
from __future__ import annotations

import logging
import os
import sqlite3
from pathlib import Path
from typing import Iterable, Sequence

logger = logging.getLogger(__name__)

TABLE_NAME = "ma200_daily"
_CSV_DIR = Path(__file__).resolve().parents[3] / "data" / "exit_replay_bars"


def ensure_table(db) -> None:
    """Idempotente Migration (AGENTS.md): CREATE TABLE IF NOT EXISTS.

    ``db``: bot.db.db.DB (has .execute) oder ein rohes sqlite3.Connection.
    Fail-open: wirft nicht.
    """
    try:
        db.execute(f"""
            CREATE TABLE IF NOT EXISTS {TABLE_NAME} (
                symbol  TEXT    NOT NULL,          -- yfinance/yahoo symbol
                date    TEXT    NOT NULL,          -- YYYY-MM-DD (closed bar)
                close   REAL    NOT NULL,
                PRIMARY KEY (symbol, date)
            )
        """)
        db.execute(f"""
            CREATE INDEX IF NOT EXISTS idx_ma200_daily_symbol_date
               ON {TABLE_NAME} (symbol, date DESC)
        """)
    except Exception:
        logger.debug("ma200_history: ensure_table fehlgeschlagen (fail-open)",
                     exc_info=True)


def df_to_closed_pairs(df, today_str: str | None = None) -> list[tuple[str, float]]:
    """Convert a yfinance daily DataFrame to CLOSED ``(date, close)`` pairs.

    ``df``: columns Open/High/Low/Close/Volume with a DatetimeIndex (the
    ``price_data`` rows from the batch fetch). Only rows whose date is strictly
    BEFORE ``today_str`` are returned — the last (intraday, unsettled) bar of
    the live fetch is dropped so the series ends at D-1, the frozen rule's
    decision bar (live-faithful S12b_L2 semantics, no look-ahead).

    ``today_str``: ``YYYY-MM-DD``; defaults to the local date. Returns ``[]``
    on any error (fail-open).
    """
    if df is None:
        return []
    if today_str is None:
        from datetime import datetime
        today_str = datetime.now().strftime("%Y-%m-%d")
    pairs: list[tuple[str, float]] = []
    try:
        close = df["Close"]
        idx = df.index
        for ts, c in zip(idx, close):
            try:
                d = ts.strftime("%Y-%m-%d")
                if d >= today_str:
                    continue  # drop today's in-progress bar (and anything newer)
                c = float(c)
                if c > 0:
                    pairs.append((d, c))
            except Exception:
                continue
    except Exception:
        logger.debug("ma200_history: df_to_closed_pairs failed (fail-open)",
                     exc_info=True)
        return []
    return pairs


def get_daily_closes(db, symbol: str, *, limit: int = 400) -> list[float]:
    """Chronological daily closes (oldest -> newest), most recent LAST.

    Returns the last ``limit`` CLOSED closes for ``symbol``. Because only
    closed bars are stored, the last element is the latest CLOSED bar (D-1
    intraday). ``[]`` when the symbol has no history (gate fails open).
    Fail-open: returns [] on any error.
    """
    if not symbol:
        return []
    try:
        rows = db.fetchall(
            f"SELECT close FROM {TABLE_NAME} WHERE symbol=? "
            f"ORDER BY date DESC LIMIT ?",
            (symbol, int(limit)),
        )
        closes = [float(r["close"]) for r in rows]
        closes.reverse()
        return closes
    except Exception:
        logger.debug("ma200_history: get_daily_closes fehlgeschlagen (fail-open)",
                     exc_info=True)
        return []


def upsert_closes(db, symbol: str,
                  pairs: Sequence[tuple[str, float]]) -> int:
    """Idempotently insert (date, close) rows. Returns number inserted.

    ``pairs``: iterable of ``(date_str, close)``. The PK (symbol, date) makes
    re-inserts no-ops, so this is safe to run every cycle and self-heals gaps.
    Fail-open: returns 0 on error.
    """
    if not symbol or not pairs:
        return 0
    rows = [(symbol, d, float(c)) for d, c in pairs if d]
    if not rows:
        return 0
    try:
        # One executemany + single commit (raw connection — the
        # per-statement `execute()` helper would commit 246 times).
        conn = db._get_conn()
        conn.executemany(
            f"INSERT OR IGNORE INTO {TABLE_NAME} (symbol, date, close) "
            f"VALUES (?,?,?)",
            rows,
        )
        conn.commit()
        return len(rows)
    except Exception:
        logger.debug("ma200_history: upsert_closes fehlgeschlagen (fail-open)",
                     exc_info=True)
        return 0


def seed_from_csvs(db, csv_dir: str | Path | None = None,
                   symbols: Iterable[str] | None = None,
                   today_str: str | None = None) -> dict:
    """Load the 1-year replay CSVs into ``ma200_daily``.

    Each ``<yfinance_symbol>.csv`` has columns date,open,high,low,close[,volume].
    ``symbols``: optional whitelist of yfinance symbols to load (default: every
    CSV in the dir). Rows dated ``today_str`` (default: local today) are DROPPED
    — a same-day fetch row is an unsettled in-progress bar and must not enter
    the closed-bars history (same rule as ``df_to_closed_pairs``). Idempotent
    (INSERT OR IGNORE). Returns a small summary.
    """
    if today_str is None:
        from datetime import datetime
        today_str = datetime.now().strftime("%Y-%m-%d")
    csv_dir = Path(csv_dir) if csv_dir else _CSV_DIR
    if symbols is not None:
        wanted = {s for s in symbols if s}
    else:
        wanted = None
    loaded = 0
    rows = 0
    try:
        ensure_table(db)
        for fn in sorted(csv_dir.glob("*.csv")):
            stem = fn.stem  # yfinance symbol (slashes were replaced on write)
            if wanted is not None and stem not in wanted:
                continue
            try:
                import csv as _csv
                with open(fn, newline="") as fh:
                    pairs: list[tuple[str, float]] = []
                    for rec in _csv.DictReader(fh):
                        d = rec.get("date")
                        c = rec.get("close")
                        if d and c:
                            d = d[:10]
                            if d >= today_str:
                                continue  # drop today's in-progress bar
                            try:
                                pairs.append((d, float(c)))
                            except (TypeError, ValueError):
                                continue
                if pairs:
                    upsert_closes(db, stem, pairs)
                    loaded += 1
                    rows += len(pairs)
            except Exception:
                logger.debug("ma200_history: seed skipped %s (fail-open)", fn,
                             exc_info=True)
        return {"symbols": loaded, "rows": rows, "dir": str(csv_dir)}
    except Exception:
        logger.debug("ma200_history: seed_from_csvs fehlgeschlagen (fail-open)",
                     exc_info=True)
        return {"symbols": 0, "rows": 0, "dir": str(csv_dir), "error": True}
