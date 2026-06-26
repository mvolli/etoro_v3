#!/usr/bin/env python3
"""
eToro Trading Bot V3 — Data Worker
src/bot/workers/data_worker.py

Runs every 5 minutes (at :00).
Fetches market data, computes TA indicators, stores signals, and refreshes
current_price for active portfolio positions.

Pipeline:
  1. Load config + init DB connection
  2. Build symbol list  (TIER 1: active positions | TIER 2: watchlist if market open)
  3. Apply yfinance symbol alias map
  4. Batch-fetch OHLCV data via yf.download()
  5. Compute TA indicators with pandas_ta
  6. Apply Trading Bible V4 signal rules (generate_signal)
  7. Store qualifying signals (direction != HOLD, score >= 20)
  8. Expire stale signals
  9. Update portfolio_snapshot.current_price for active positions
 10. Print summary line
"""
from __future__ import annotations

import logging
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

# ── project path setup ────────────────────────────────────────────────────────
# When executed directly the package root might not be on sys.path.
_HERE = Path(__file__).resolve()
_PROJECT_ROOT = _HERE.parents[3]   # data_worker.py -> workers/ -> bot/ -> src/ -> etoro_v3/
if str(_PROJECT_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT / "src"))

from bot.db.connection import DB
from bot.db.repo import SignalRepo, PortfolioRepo
from bot.core.signals import generate_signal, compute_indicators
from bot.core.market_hours import is_market_open, get_market_status, CRYPTO_SYMBOLS, get_instrument_market_key
from bot.api.instruments import get_instrument_map, symbol_to_id

logger = logging.getLogger(__name__)

# ── Discord Embeds ─────────────────────────────────────────────────────────
try:
    import sys as _sys
    _sys.path.insert(0, '/home/mvolli/.hermes/workspace/etoro_v3/src/bot')
    import discord_embeds as _DE
except Exception:
    _DE = None

def _post(fn_name: str, **kwargs) -> None:
    """Best-effort Discord post. Never raises."""
    try:
        if _DE and hasattr(_DE, fn_name):
            getattr(_DE, fn_name)(**kwargs)
    except Exception as _e:
        pass

# ── constants ─────────────────────────────────────────────────────────────────

WORKER_NAME = "data_worker"

DEFAULT_WATCHLIST: list[str] = [
    "AAPL", "NVDA", "META", "MSFT", "AMZN", "GOOGL", "TSLA",
    "QQQ", "SPY", "GLD",
    "BTC-USD", "ETH-USD", "XRP-USD",
    "NFLX", "AMD", "INTC", "JPM", "V", "MA", "WMT",
    "HD", "PG", "JNJ", "UNH", "XOM", "NEE", "T", "DIS", "ADBE", "CRM",
    # EU instruments (yfinance ticker → eToro symbol mapping below)
    "SAP.DE", "SIE.DE", "ALV.DE", "DTE.DE", "BAS.DE", "BMW.DE", "VOW3.DE",
    "NESN.SW", "NOVN.SW", "UBSG.SW",
    "SHEL.L", "AZN.L", "GSK.L", "ULVR.L", "DGE.L",
    "ASML.AS", "INGA.AS", "PHIA.AS",
    "ENI.MI", "ENEL.MI",
    "MC.PA", "OR.PA", "SAN.PA",
]

# yfinance ticker aliases (eToro symbol → Yahoo Finance ticker)
SYMBOL_ALIAS_MAP: dict[str, str] = {
    "BTC-USD":  "BTC-USD",
    "ETH-USD":  "ETH-USD",
    "XRP-USD":  "XRP-USD",
    "UNI-USD":  "UNI7083-USD",
}

# Minimum signal score to store
MIN_SIGNAL_SCORE = 20.0

# Signal TTL in minutes
SIGNAL_TTL_MINUTES = 60

# ── Rate Limiting & Retry ─────────────────────────────────────────────────────

BATCH_SIZE = 40              # symbols per yf.download() call
BATCH_PAUSE_S = 1.5          # seconds between batches (rate limiting)
MAX_BATCH_RETRIES = 2        # retry count for failed batches
RETRY_BACKOFF_BASE = 2.0     # exponential backoff: 2^attempt seconds
MAX_DOWNLOAD_TIMEOUT = 60    # per-batch timeout in seconds

# Failed symbol tracking: persistent SQLite-based cache to avoid repeated
# yf.download() calls on known-bad tickers (eToro CFDs Yahoo doesn't know).
# Symbols are auto-retried after COOLDOWN_DAYS and purged after CLEANUP_DAYS.
_FAILED_SYMBOLS_CACHE: set[str] = set()   # in-memory mirror for fast lookups within a session
_MAX_FAILED_CACHE = 200                   # soft cap for logging
_COOLDOWN_DAYS = 7                        # retry a failed symbol after N days
_CLEANUP_DAYS = 90                        # purge entries older than N days

def _ensure_failed_symbols_table(db: "DB") -> None:
    """Create the failed_symbols table if it doesn't exist."""
    db.execute("""
        CREATE TABLE IF NOT EXISTS failed_symbols (
            symbol TEXT PRIMARY KEY,
            first_failed_at DATETIME NOT NULL,
            last_failed_at DATETIME NOT NULL,
            failure_count INTEGER NOT NULL DEFAULT 1
        )
    """)

def _load_failed_cache(db: "DB") -> None:
    """Load failed symbols from DB into in-memory cache (only those still in cooldown)."""
    rows = db.fetchall("""
        SELECT symbol FROM failed_symbols
        WHERE last_failed_at > datetime('now', ? || ' days')
    """, (f"-{_COOLDOWN_DAYS}",))
    _FAILED_SYMBOLS_CACHE.clear()
    for row in rows:
        _FAILED_SYMBOLS_CACHE.add(row[0])
    if _FAILED_SYMBOLS_CACHE:
        logger.info(
            "[%s] Loaded %d failed symbols from persistent cache (cooldown: %d days)",
            WORKER_NAME, len(_FAILED_SYMBOLS_CACHE), _COOLDOWN_DAYS,
        )

def _is_known_bad_symbol(sym: str) -> bool:
    """Check if a symbol is in the failed-cache (in-memory mirror)."""
    return sym in _FAILED_SYMBOLS_CACHE

def _cache_failed_symbol(sym: str, db: "DB") -> None:
    """Add/update a symbol in the persistent failed cache (DB + in-memory)."""
    now = datetime.now().isoformat(sep=' ', timespec='seconds')
    existing = db.fetchone(
        "SELECT first_failed_at, failure_count FROM failed_symbols WHERE symbol = ?",
        (sym,)
    )
    if existing:
        db.execute("""
            INSERT OR REPLACE INTO failed_symbols (symbol, first_failed_at, last_failed_at, failure_count)
            VALUES (?, ?, ?, ?)
        """, (sym, existing[0], now, existing[1] + 1))
    else:
        db.execute("""
            INSERT INTO failed_symbols (symbol, first_failed_at, last_failed_at, failure_count)
            VALUES (?, ?, ?, ?)
        """, (sym, now, now, 1))
    _FAILED_SYMBOLS_CACHE.add(sym)

def _cleanup_old_failed_symbols(db: "DB") -> int:
    """Remove failed symbol entries older than CLEANUP_DAYS. Returns count deleted."""
    cur = db.execute("""
        DELETE FROM failed_symbols
        WHERE last_failed_at < datetime('now', ? || ' days')
    """, (f"-{_CLEANUP_DAYS}",))
    deleted = cur.rowcount
    if deleted:
        logger.info("[%s] Cleaned up %d stale failed-symbol entries (>=%d days)", WORKER_NAME, deleted, _CLEANUP_DAYS)
        # Also remove from in-memory cache
        for sym in list(_FAILED_SYMBOLS_CACHE):
            row = db.fetchone("SELECT symbol FROM failed_symbols WHERE symbol = ?", (sym,))
            if row is None:
                _FAILED_SYMBOLS_CACHE.discard(sym)
    return deleted

def _filter_known_bad(symbols: list[str]) -> tuple[list[str], int]:
    """Remove symbols that are known to fail from the fetch list.

    Returns (filtered_symbols, skipped_count).
    """
    filtered = [s for s in symbols if not _is_known_bad_symbol(s)]
    skipped = len(symbols) - len(filtered)
    if skipped:
        logger.debug(
            "[%s] Skipping %d known-bad symbols (cache size: %d, cooldown: %d days)",
            WORKER_NAME, skipped, len(_FAILED_SYMBOLS_CACHE), _COOLDOWN_DAYS,
        )
    return filtered, skipped


# ── helpers ───────────────────────────────────────────────────────────────────


def _load_config(project_root: Path) -> dict:
    """Load config/config.yaml relative to the project root."""
    cfg_path = project_root / "config" / "config.yaml"
    if not cfg_path.is_file():
        logger.warning("Config file not found at %s — using defaults", cfg_path)
        return {}
    with cfg_path.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def _apply_alias(symbol: str) -> str:
    """Translate an eToro symbol to the correct yfinance ticker."""
    return SYMBOL_ALIAS_MAP.get(symbol, symbol)


def _batch_fetch(symbols: list[str], batch_size: int = BATCH_SIZE) -> dict[str, Any]:
    """
    Download 3 months of OHLCV data for all symbols via yf.download().

    Features:
    - Batches of `batch_size` to avoid timeouts
    - Exponential backoff retry on failed batches (up to MAX_BATCH_RETRIES)
    - Failed-symbol cache to skip known-bad tickers on subsequent runs
    - Rate limiting between batches (BATCH_PAUSE_S)

    Returns:
        {symbol: DataFrame(Open, High, Low, Close, Volume)} for symbols
        with >= 30 rows.
    """
    import yfinance as yf
    import pandas as pd

    if not symbols:
        return {}

    # Filter out known-bad symbols (eToro CFDs Yahoo doesn't know)
    symbols, skipped_bad = _filter_known_bad(symbols)
    if not symbols:
        logger.info("[%s] All %d symbols are known-bad — skipping fetch", WORKER_NAME, len(symbols))
        return {}

    total_requested = len(symbols) + skipped_bad
    logger.info(
        "[%s] Fetching %d symbols in batches of %d (%d skipped from failed-cache)…",
        WORKER_NAME, len(symbols), batch_size, skipped_bad,
    )

    result: dict[str, pd.DataFrame] = {}
    total_batches = (len(symbols) + batch_size - 1) // batch_size

    for batch_idx in range(total_batches):
        batch_symbols = symbols[batch_idx * batch_size : (batch_idx + 1) * batch_size]

        logger.info(
            "[%s] Batch %d/%d: %d symbols",
            WORKER_NAME, batch_idx + 1, total_batches, len(batch_symbols),
        )

        batch_success = False
        failed_in_batch: list[str] = []

        for attempt in range(MAX_BATCH_RETRIES + 1):
            try:
                raw = yf.download(
                    batch_symbols,
                    period="3mo",
                    auto_adjust=True,
                    progress=False,
                    threads=True,
                )
                batch_success = True
                break

            except Exception as exc:
                wait_time = RETRY_BACKOFF_BASE ** (attempt + 1)
                logger.warning(
                    "[%s] Batch %d attempt %d/%d failed: %s — retrying in %.1fs",
                    WORKER_NAME, batch_idx + 1, attempt + 1, MAX_BATCH_RETRIES + 1, exc, wait_time,
                )
                time.sleep(wait_time)

        if not batch_success:
            # All retries exhausted — cache every symbol in this batch as failed (in-memory only, persisted at end of run)
            for sym in batch_symbols:
                _FAILED_SYMBOLS_CACHE.add(sym)
            logger.warning(
                "[%s] Batch %d: all retries exhausted — cached %d symbols as failed",
                WORKER_NAME, batch_idx + 1, len(batch_symbols),
            )
            continue

        if raw is None or raw.empty:
            logger.warning("[%s] Batch %d returned empty DataFrame", WORKER_NAME, batch_idx + 1)
            continue

        if len(batch_symbols) == 1:
            sym = batch_symbols[0]
            # Single-symbol: flat columns (Open, High, Low, Close, Volume)
            try:
                df = raw[["Open", "High", "Low", "Close", "Volume"]].dropna(
                    subset=["Close"]
                )
                if len(df) >= 30:
                    result[sym] = df
                else:
                    logger.debug("[%s] %s: only %d rows — skipped", WORKER_NAME, sym, len(df))
            except Exception as exc:
                logger.warning("[%s] %s: single-symbol extraction failed — %s", WORKER_NAME, sym, exc)
        else:
            # Multi-symbol: two-level MultiIndex columns (Attribute, Ticker)
            for sym in batch_symbols:
                try:
                    # xs(level=1) selects columns for this ticker
                    df = raw.xs(sym, axis=1, level=1)[
                        ["Open", "High", "Low", "Close", "Volume"]
                    ].dropna(subset=["Close"])
                    if len(df) >= 30:
                        result[sym] = df
                    else:
                        # Symbol in response but no valid data (all NaN / delisted)
                        # → cache as failed to avoid repeated yfinance ERROR logs
                        _FAILED_SYMBOLS_CACHE.add(sym)
                        logger.debug(
                            "[%s] %s: %d rows after dropna (delisted/no-data) → cached as failed",
                            WORKER_NAME, sym, len(df),
                        )
                except KeyError:
                    # Symbol not in response — likely invalid ticker (eToro CFD)
                    _FAILED_SYMBOLS_CACHE.add(sym)
                    logger.debug("[%s] %s: not found in batch response → cached as failed", WORKER_NAME, sym)
                except Exception as exc:
                    logger.warning("[%s] %s: extraction failed — %s", WORKER_NAME, sym, exc)

        # Rate limiting: pause between batches (not after last one)
        if batch_idx < total_batches - 1:
            time.sleep(BATCH_PAUSE_S)

    # Report cache stats
    if _FAILED_SYMBOLS_CACHE:
        logger.info(
            "[%s] Failed-symbol cache: %d symbols (will be skipped on next run)",
            WORKER_NAME, len(_FAILED_SYMBOLS_CACHE),
        )

    logger.info(
        "[%s] Batch fetch complete: %d/%d symbols with sufficient data (%d batches)",
        WORKER_NAME, len(result), total_requested, total_batches,
    )
    return result


def _get_portfolio_symbols(db: DB) -> list[str]:
    """Return symbols from portfolio_snapshot (active positions, Tier 1)."""
    portfolio_repo = PortfolioRepo(db)
    try:
        positions = portfolio_repo.get_all()
        return [pos["symbol"] for pos in positions if pos.get("symbol")]
    except Exception as exc:
        logger.warning("[%s] Could not fetch portfolio symbols: %s", WORKER_NAME, exc)
        return []


def _get_watchlist_from_db(db: DB) -> list[dict]:
    """Load watchlist instruments from DB with category and yfinance_symbol.

    Returns list of dicts: [{symbol, yf_symbol, category, instrument_id}, ...]
    """
    try:
        rows = db.fetchall("""
            SELECT w.symbol, i.yfinance_symbol, w.category, w.instrument_id
            FROM watchlist w
            LEFT JOIN instruments i ON w.instrument_id = i.instrument_id
        """)

        watchlist = []
        for symbol, yf_symbol, category, instrument_id in rows:
            if not symbol:
                continue
            # Use yfinance_symbol as primary fetch target, fall back to symbol
            effective_yf = yf_symbol or symbol
            watchlist.append({
                'symbol': symbol,
                'yf_symbol': effective_yf,
                'category': category or 'stocks',
                'instrument_id': instrument_id,
            })

        logger.info("[%s] Loaded %d instruments from DB watchlist", WORKER_NAME, len(watchlist))

        # Category breakdown
        from collections import Counter
        cat_counts = Counter(item['category'] for item in watchlist)
        for cat, count in sorted(cat_counts.items()):
            logger.debug("[%s]   %s: %d instruments", WORKER_NAME, cat, count)

        return watchlist

    except Exception as exc:
        logger.error("[%s] Failed to load watchlist from DB: %s", WORKER_NAME, exc)
        # Fallback to hardcoded list if DB fails
        logger.warning("[%s] Falling back to DEFAULT_WATCHLIST", WORKER_NAME)
        return [
            {'symbol': sym, 'yf_symbol': sym, 'category': 'stocks', 'instrument_id': None}
            for sym in DEFAULT_WATCHLIST
        ]


def _update_portfolio_prices(
    db: DB,
    price_data: dict[str, Any],
    alias_to_original: dict[str, str],
) -> None:
    """Update current_price in portfolio_snapshot for positions we have fresh data for."""
    import sqlite3 as _sqlite3

    portfolio_repo = PortfolioRepo(db)
    try:
        positions = portfolio_repo.get_all()
    except Exception as exc:
        logger.warning("[%s] Could not load portfolio for price update: %s", WORKER_NAME, exc)
        return

    for pos in positions:
        symbol: str = pos.get("symbol", "") or ""
        if not symbol:
            continue

        yf_sym = _apply_alias(symbol)
        df = price_data.get(yf_sym)
        if df is None or df.empty:
            continue

        try:
            current_price = float(df["Close"].iloc[-1])
            # Direct SQL update — only touch current_price, NEVER last_synced.
            # last_synced is the Reconciler's domain (orphan detection).
            # Updating it here would resurrect orphan positions and prevent cleanup.
            db.execute(
                """
                UPDATE portfolio_snapshot
                   SET current_price = ?
                 WHERE api_position_id = ?
                """,
                (current_price, pos["api_position_id"]),
            )
            logger.debug(
                "[%s] Updated %s current_price → %.4f",
                WORKER_NAME, symbol, current_price,
            )
        except Exception as exc:
            logger.warning(
                "[%s] Failed to update price for %s: %s", WORKER_NAME, symbol, exc
            )


# ── main logic ────────────────────────────────────────────────────────────────

def run(project_root: Path | None = None) -> dict:
    """
    Execute one full data-worker cycle.

    Returns a summary dict:
        {symbols_fetched, signals_generated, elapsed_s}
    """
    t_start = time.monotonic()

    if project_root is None:
        project_root = _PROJECT_ROOT

    # 1. Load config ---------------------------------------------------------
    cfg = _load_config(project_root)
    db_cfg = cfg.get("db", {})
    db_path = project_root / db_cfg.get("path", "data/trading.db")
    busy_timeout_ms = int(db_cfg.get("busy_timeout_ms", 5000))
    signal_ttl = int(cfg.get("cache", {}).get("signal_ttl_minutes", SIGNAL_TTL_MINUTES))

    db = DB(db_path=db_path, busy_timeout_ms=busy_timeout_ms)

    # 0. Initialize persistent failed-symbol cache ----------------------------
    _ensure_failed_symbols_table(db)
    _load_failed_cache(db)

    # 2. Determine symbol lists -----------------------------------------------

    # Tier 1: always fetch (need fresh prices for SL checks)
    tier1_symbols: list[str] = _get_portfolio_symbols(db)
    logger.info("[%s] Tier 1 (portfolio): %d symbols", WORKER_NAME, len(tier1_symbols))

    # Tier 2: watchlist from DB — market-aware filtering
    db_watchlist = _get_watchlist_from_db(db)

    # Filter: only include instruments whose specific market is currently open
    tier2_items: list[dict] = []
    skipped_count = 0
    for item in db_watchlist:
        sym = item['symbol']
        yf_sym = item['yf_symbol']
        cat = item['category']
        if is_market_open(sym, yf_sym, cat):
            tier2_items.append(item)
        else:
            skipped_count += 1

    logger.info(
        "[%s] Tier 2 market-aware: %d open, %d market-closed out of %d watchlist instruments",
        WORKER_NAME, len(tier2_items), skipped_count, len(db_watchlist),
    )

    # Merge portfolio symbols with watchlist, deduplicate by yf_symbol
    # Build set of tier1 yf_symbols for quick lookup
    tier1_yf_set = {_apply_alias(s) for s in tier1_symbols}

    # All items to fetch: Tier 1 + Tier 2 (deduplicated)
    all_items: list[dict] = []
    seen_yf: set[str] = set()

    # Add Tier 1 first (highest priority)
    for sym in tier1_symbols:
        yf_sym = _apply_alias(sym)
        if yf_sym not in seen_yf:
            all_items.append({
                'symbol': sym,
                'yf_symbol': yf_sym,
                'category': 'portfolio',
                'instrument_id': None,
            })
            seen_yf.add(yf_sym)

    # Add Tier 2 (skip if already in Tier 1)
    for item in tier2_items:
        yf_sym = item['yf_symbol']
        if yf_sym not in seen_yf:
            all_items.append(item)
            seen_yf.add(yf_sym)

    # Build final yf symbol list and reverse map
    alias_to_original: dict[str, str] = {}
    all_yf_symbols: list[str] = []
    for item in all_items:
        yf_sym = item['yf_symbol']
        original_sym = item['symbol']
        if yf_sym not in alias_to_original:
            alias_to_original[yf_sym] = original_sym
            all_yf_symbols.append(yf_sym)

    logger.info("[%s] Total symbols to fetch: %d (Tier1=%d, Tier2=%d)", WORKER_NAME,
                len(all_yf_symbols), len(tier1_symbols), len(tier2_items))

    if not all_yf_symbols:
        logger.info("[%s] No symbols to fetch — exiting early", WORKER_NAME)
        return {"symbols_fetched": 0, "signals_generated": 0, "elapsed_s": 0.0}

    # 3. Batch fetch OHLCV data -----------------------------------------------
    price_data = _batch_fetch(all_yf_symbols)
    n_fetched = len(price_data)

    # 4–6. Compute indicators + generate signals per symbol -------------------
    instrument_map = get_instrument_map()           # {instrument_id: symbol}
    # Invert for symbol → id lookup (case-insensitive handled inside symbol_to_id)
    signal_repo = SignalRepo(db)
    n_signals = 0

    for yf_sym, df in price_data.items():
        original_sym = alias_to_original.get(yf_sym, yf_sym)

        # Find the watchlist item for this symbol to get category and instrument_id
        watch_item = None
        for item in all_items:
            if item['yf_symbol'] == yf_sym:
                watch_item = item
                break

        category = watch_item['category'] if watch_item else 'stocks'
        instrument_id_from_db = watch_item.get('instrument_id') if watch_item else None

        t_sym_start = time.monotonic()
        try:
            # 5. Compute indicators
            indicators = compute_indicators(df)
            if not indicators:
                logger.debug("[%s] %s: no indicators computed", WORKER_NAME, original_sym)
                continue

            # 6. Generate signal
            result = generate_signal(original_sym, indicators)

            if result.direction == "HOLD" or result.score < MIN_SIGNAL_SCORE:
                logger.debug(
                    "[%s] %s: direction=%s score=%.1f — not stored",
                    WORKER_NAME, original_sym, result.direction, result.score,
                )
                continue

            # Market-hours guard: skip BUY signals when market is closed
            if result.direction == "BUY" and not is_market_open(original_sym, yf_sym, category):
                logger.debug(
                    "DataWorker: market closed for %s (category=%s) — signal skipped",
                    original_sym, category
                )
                continue

            # Resolve instrument_id
            if instrument_id_from_db:
                instrument_id = instrument_id_from_db
            else:
                instrument_id = symbol_to_id(original_sym, instrument_map)
            if instrument_id is None:
                # Try yf ticker as fallback
                instrument_id = symbol_to_id(yf_sym, instrument_map)
            if instrument_id is None:
                # Auto-register new instrument with generated ID
                try:
                    existing_ids = db.execute("SELECT MAX(instrument_id) FROM instruments").fetchone()[0] or 0
                    instrument_id = existing_ids + 1
                    db.execute(
                        "INSERT OR IGNORE INTO instruments (instrument_id, symbol) VALUES (?, ?)",
                        (instrument_id, original_sym),
                    )
                    logger.info(
                        "[%s] AUTO-REGISTERED %s → instrument_id=%d",
                        WORKER_NAME, original_sym, instrument_id,
                    )
                except Exception as reg_exc:
                    logger.debug(
                        "[%s] %s: auto-register failed — %s",
                        WORKER_NAME, original_sym, reg_exc,
                    )
                    continue

            # 7. Store signal in DB
            signal_types_str = ",".join(result.signal_types) if result.signal_types else result.direction
            signal_repo.create(
                instrument_id=instrument_id,
                signal_type=signal_types_str,
                conviction=result.conviction,
                score=result.score,
                rsi=result.rsi,
                macd_hist=result.macd_hist,
                bb_pct=result.bb_pct,
                price=result.price,
                ttl_minutes=signal_ttl,
            )
            n_signals += 1
            logger.info(
                "[%s] SIGNAL %s %s conviction=%s score=%.1f",
                WORKER_NAME, result.direction, original_sym, result.conviction, result.score,
            )

        except Exception as exc:
            elapsed = time.monotonic() - t_sym_start
            if elapsed > 5.0:
                logger.warning(
                    "[%s] %s: processing took %.1fs → cached as failed",
                    WORKER_NAME, original_sym, elapsed,
                )
                _FAILED_SYMBOLS_CACHE.add(yf_sym)
            logger.error(
                "[%s] Error processing %s (%.1fs): %s",
                WORKER_NAME, original_sym, elapsed, exc, exc_info=True,
            )
            # Per-symbol error: continue with remaining symbols

    # 8. Expire stale signals -------------------------------------------------
    try:
        n_expired = signal_repo.expire_old()
        if n_expired:
            logger.info("[%s] Expired %d stale signals", WORKER_NAME, n_expired)
    except Exception as exc:
        logger.warning("[%s] expire_old failed: %s", WORKER_NAME, exc)

    # 9. Update portfolio current_price ---------------------------------------
    _update_portfolio_prices(db, price_data, alias_to_original)

    # 10. Persist failed symbols to DB & cleanup stale entries -----------------
    for sym in _FAILED_SYMBOLS_CACHE:
        _cache_failed_symbol(sym, db)
    _cleanup_old_failed_symbols(db)

    # 11. Summary -------------------------------------------------------------
    elapsed = time.monotonic() - t_start
    print(
        f"DataWorker: {n_fetched} symbols fetched, "
        f"{n_signals} signals written ({elapsed:.1f}s, failed_cache={len(_FAILED_SYMBOLS_CACHE)})"
    )

    # Discord: data worker summary (only if interesting)
    if n_signals > 0:
        open_regions = get_market_status()
        _post('post_alert_embed',
            title=f'📊 Data Worker: {n_signals} Signal(s) generated',
            description=(
                f'Symbole: {n_fetched}/{len(all_yf_symbols)} | '
                f'Signale: {n_signals} | '
                f'Dauer: {elapsed:.1f}s\n'
                f'Offene Märkte: {open_regions}\n'
                f'Failed-Cache: {len(_FAILED_SYMBOLS_CACHE)} Symbole (abgesprungen)'
            ),
            severity='INFO',
            dry_run=False
        )
    # Market closed — no post (too noisy every 5min at night)

    return {
        "symbols_fetched": n_fetched,
        "signals_generated": n_signals,
        "elapsed_s": elapsed,
    }


# ── entry point ───────────────────────────────────────────────────────────────

def main() -> int:
    """CLI entry point — configure logging and run one cycle."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    try:
        run()
        return 0
    except Exception as exc:
        logger.critical("[%s] Fatal error: %s", WORKER_NAME, exc, exc_info=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
