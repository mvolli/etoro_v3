#!/usr/bin/env python3
"""Correlation Gate — Trading Bible V5.

Blocks BUYs when new symbol is too correlated with existing portfolio positions.
Uses yfinance 30-day returns. Results cached in-memory (TTL 4h).
"""
from __future__ import annotations
import time
from functools import lru_cache

CORRELATION_BLOCK_THRESHOLD  = 0.80  # Block BUY if r >= 0.80 with any existing position
CORRELATION_REDUCE_THRESHOLD = 0.60  # (future: halve size if 0.60 <= r < 0.80)
BROAD_ETFS = {'SPY', 'QQQ', 'VOO', 'VTI', 'IWM', 'DIA', 'RSP'}  # Exempt (tolerance 0.95)

# Simple in-memory cache: {(sym_a, sym_b): (corr, timestamp)}
_cache: dict = {}
CACHE_TTL = 4 * 3600  # 4 hours


def _cache_key(a: str, b: str) -> tuple:
    return (min(a, b), max(a, b))


def get_correlation(sym_a: str, sym_b: str, lookback_days: int = 30) -> float | None:
    """Get 30-day return correlation between two symbols. Returns None on failure."""
    key = _cache_key(sym_a, sym_b)
    if key in _cache:
        corr, ts = _cache[key]
        if time.time() - ts < CACHE_TTL:
            return corr
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
        _cache[key] = (corr, time.time())
        return corr
    except Exception:
        return None


def check_correlation_gate(
    symbol: str,
    open_positions: list[dict],
) -> tuple[bool, str]:
    """Check if buying symbol would violate correlation limits.

    Args:
        symbol: Symbol to buy
        open_positions: list of {symbol: str, amount_usd: float}

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
    # Deduplicate
    existing_symbols = list(dict.fromkeys(existing_symbols))

    for existing in existing_symbols:
        corr = get_correlation(symbol, existing)
        if corr is None:
            continue  # Fail-open: can't get data, don't block
        if corr >= block_threshold:
            return False, (
                f'Korrelation {symbol}/{existing}: r={corr:.2f} '
                f'>= {block_threshold:.2f} — BUY blockiert'
            )

    return True, f'Korrelation OK (max checked: {len(existing_symbols)} Positionen)'
