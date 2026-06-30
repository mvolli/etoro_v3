#!/usr/bin/env python3
"""Correlation Gate — Trading Bible V5.

Blocks BUYs when new symbol is too correlated with existing portfolio positions.
Uses yfinance 30-day returns. Results cached in SQLite (TTL 4h) for cross-process
persistence — every worker run now benefits from previously computed correlations.
"""
from __future__ import annotations
import logging
import sqlite3
import time
from pathlib import Path

logger = logging.getLogger(__name__)

CORRELATION_BLOCK_THRESHOLD  = 0.80  # Block BUY if r >= 0.80 with any existing position
CORRELATION_REDUCE_THRESHOLD = 0.60  # (future: halve size if 0.60 <= r < 0.80)
BROAD_ETFS = {'SPY', 'QQQ', 'VOO', 'VTI', 'IWM', 'DIA', 'RSP'}  # Exempt (tolerance 0.95)

CACHE_TTL = 4 * 3600  # 4 hours


# ─── SQLite Cache ─────────────────────────────────────────────────────────────
# Lives alongside trading.db — same WAL mode, same busy_timeout.

def _get_cache_conn(db_path: str | None = None) -> sqlite3.Connection:
    """Get a connection to the correlation cache DB (same as trading.db)."""
    if db_path is None:
        # correlation.py is at src/bot/core/correlation.py
        # 4 parents up: core → bot → src → etoro_v3/
        project_root = Path(__file__).resolve().parent.parent.parent.parent
        db_path = str(project_root / 'data' / 'trading.db')

    # Ensure parent directory exists
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(f"file:{db_path}", uri=True)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def _ensure_cache_table(conn: sqlite3.Connection) -> None:
    """Create correlation_cache table if it doesn't exist."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS correlation_cache (
            sym_a       TEXT NOT NULL,
            sym_b       TEXT NOT NULL,
            correlation REAL NOT NULL,
            computed_at REAL NOT NULL,
            PRIMARY KEY (sym_a, sym_b)
        )
    """)
    conn.commit()


def _cache_key(a: str, b: str) -> tuple[str, str]:
    """Canonical key: always sorted alphabetically."""
    return (min(a, b), max(a, b))


def _get_cached(conn: sqlite3.Connection, sym_a: str, sym_b: str) -> float | None:
    """Get cached correlation if still valid."""
    ka, kb = _cache_key(sym_a, sym_b)
    row = conn.execute(
        "SELECT correlation, computed_at FROM correlation_cache WHERE sym_a=? AND sym_b=?",
        (ka, kb),
    ).fetchone()

    if row is None:
        return None

    corr, ts = row
    if time.time() - ts < CACHE_TTL:
        return corr

    # Stale — will be refreshed below
    return None


def _set_cached(conn: sqlite3.Connection, sym_a: str, sym_b: str, corr: float) -> None:
    """Store correlation in cache."""
    ka, kb = _cache_key(sym_a, sym_b)
    conn.execute(
        "INSERT OR REPLACE INTO correlation_cache (sym_a, sym_b, correlation, computed_at) VALUES (?, ?, ?, ?)",
        (ka, kb, corr, time.time()),
    )
    conn.commit()


def get_correlation(sym_a: str, sym_b: str, lookback_days: int = 30, db_path: str | None = None) -> float | None:
    """Get 30-day return correlation between two symbols.

    Uses SQLite cache for cross-process persistence (4h TTL).
    Returns None on failure (fail-open).

    Args:
        sym_a: First symbol
        sym_b: Second symbol
        lookback_days: Number of days to fetch for correlation calculation
        db_path: Optional path to trading.db (resolved automatically if None)
    """
    conn = _get_cache_conn(db_path)
    try:
        _ensure_cache_table(conn)

        # Check cache first
        cached = _get_cached(conn, sym_a, sym_b)
        if cached is not None:
            return cached

        # Compute fresh correlation
        try:
            import yfinance as yf
            import pandas as pd

            period = f"{lookback_days + 5}d"
            data = yf.download([sym_a, sym_b], period=period, progress=False, auto_adjust=True)
            if data.empty:
                return None

            # Handle multi-level columns
            if isinstance(data.columns, pd.MultiIndex):
                closes = data['Close']
            else:
                closes = data[['Close']]

            if sym_a not in closes.columns or sym_b not in closes.columns:
                return None

            returns = closes[[sym_a, sym_b]].pct_change().dropna()
            if len(returns) < 10:
                return None

            corr = float(returns[sym_a].corr(returns[sym_b]))

            # Store in cache
            _set_cached(conn, sym_a, sym_b, corr)
            return corr

        except Exception as e:
            logger.debug("Correlation fetch failed for %s/%s: %s", sym_a, sym_b, e)
            return None

    finally:
        conn.close()


def check_correlation_gate(
    symbol: str,
    open_positions: list[dict],
    db_path: str | None = None,
) -> tuple[bool, str]:
    """Check if buying symbol would violate correlation limits.

    Args:
        symbol: Symbol to buy
        open_positions: list of {symbol: str, amount_usd: float}
        db_path: Optional path to trading.db (resolved automatically if None)

    Returns:
        (allowed, reason)
    """
    if not open_positions or not symbol:
        return True, 'OK'

    # Broad ETFs have higher tolerance
    block_threshold = 0.95 if symbol.upper() in BROAD_ETFS else CORRELATION_BLOCK_THRESHOLD

    existing_symbols = [
        p['symbol'] for p in open_positions
        if p.get('symbol') and p['symbol'] != symbol and float(p.get('amount_usd', 0)) > 0
    ]
    # Deduplicate, preserve order
    existing_symbols = list(dict.fromkeys(existing_symbols))

    for existing in existing_symbols:
        corr = get_correlation(symbol, existing, db_path=db_path)
        if corr is None:
            continue  # Fail-open: can't get data, don't block
        if corr >= block_threshold:
            return False, (
                f'Korrelation {symbol}/{existing}: r={corr:.2f} '
                f'>= {block_threshold:.2f} — BUY blockiert'
            )

    return True, f'Korrelation OK (max checked: {len(existing_symbols)} Positionen)'


def cleanup_stale_cache(db_path: str | None = None) -> int:
    """Remove expired entries from the correlation cache. Returns count deleted."""
    conn = _get_cache_conn(db_path)
    try:
        _ensure_cache_table(conn)
        cur = conn.execute(
            "DELETE FROM correlation_cache WHERE computed_at < ?",
            (time.time() - CACHE_TTL,),
        )
        deleted = cur.rowcount
        if deleted:
            logger.info("Cleaned up %d stale correlation cache entries", deleted)
        return deleted
    finally:
        conn.close()


# ─── Quick Test ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    # Ensure src/ is on path for imports
    project_root = Path(__file__).resolve().parent.parent.parent
    if str(project_root / "src") not in sys.path:
        sys.path.insert(0, str(project_root / "src"))

    print("=== Correlation Cache Test ===\n")

    # Test 1: Basic correlation computation
    print("Test 1: SPY/QQQ correlation (should be ~0.95+)")
    corr = get_correlation("SPY", "QQQ")
    if corr is not None:
        print(f"  r = {corr:.4f}")
    else:
        print("  No data returned (yfinance may have failed)")

    # Test 2: Cache hit
    print("\nTest 2: Second call should hit cache")
    corr2 = get_correlation("SPY", "QQQ")
    if corr is not None and corr2 is not None:
        print(f"  r = {corr2:.4f} (from cache)")

    # Test 3: Correlation gate simulation
    print("\nTest 3: Correlation gate check")
    mock_positions = [
        {"symbol": "AAPL", "amount_usd": 500},
        {"symbol": "MSFT", "amount_usd": 400},
        {"symbol": "NVDA", "amount_usd": 300},
    ]
    allowed, reason = check_correlation_gate("GOOGL", mock_positions)
    print(f"  GOOGL gate: {'ALLOWED' if allowed else 'BLOCKED'} — {reason}")

    # Test 4: Cache stats
    print("\nTest 4: Cache statistics")
    db_path = str(project_root / "data" / "trading.db")
    conn = _get_cache_conn(db_path)
    try:
        _ensure_cache_table(conn)
        total = conn.execute("SELECT COUNT(*) FROM correlation_cache").fetchone()[0]
        fresh = conn.execute(
            "SELECT COUNT(*) FROM correlation_cache WHERE computed_at > ?",
            (time.time() - CACHE_TTL,),
        ).fetchone()[0]
        print(f"  Total cached pairs: {total}")
        print(f"  Fresh (<4h): {fresh}")
    finally:
        conn.close()
