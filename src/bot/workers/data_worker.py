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
from bot.core.market_hours import is_market_open, get_market_status, CRYPTO_SYMBOLS
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


def _batch_fetch(symbols: list[str]) -> dict[str, Any]:
    """
    Download 3 months of OHLCV data for all symbols in one yf.download() call.

    Handles both single-symbol (flat MultiIndex columns) and multi-symbol
    (two-level MultiIndex: Attribute / Ticker) responses from yfinance.

    Returns:
        {symbol: DataFrame(Open, High, Low, Close, Volume)} for symbols
        with >= 30 rows.
    """
    import yfinance as yf
    import pandas as pd

    if not symbols:
        return {}

    logger.info("[%s] Batch-downloading %d symbols…", WORKER_NAME, len(symbols))

    raw = yf.download(
        symbols,
        period="3mo",
        auto_adjust=True,
        progress=False,
        threads=True,
    )

    if raw is None or raw.empty:
        logger.warning("[%s] yf.download returned empty DataFrame", WORKER_NAME)
        return {}

    result: dict[str, pd.DataFrame] = {}

    if len(symbols) == 1:
        sym = symbols[0]
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
        for sym in symbols:
            try:
                # xs(level=1) selects columns for this ticker
                df = raw.xs(sym, axis=1, level=1)[
                    ["Open", "High", "Low", "Close", "Volume"]
                ].dropna(subset=["Close"])
                if len(df) >= 30:
                    result[sym] = df
                else:
                    logger.debug("[%s] %s: only %d rows — skipped", WORKER_NAME, sym, len(df))
            except KeyError:
                logger.debug("[%s] %s: not found in batch response", WORKER_NAME, sym)
            except Exception as exc:
                logger.warning("[%s] %s: extraction failed — %s", WORKER_NAME, sym, exc)

    logger.info(
        "[%s] Batch fetch complete: %d/%d symbols with sufficient data",
        WORKER_NAME, len(result), len(symbols),
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
            # Direct SQL update (PortfolioRepo.upsert would overwrite all cols)
            db.execute(
                """
                UPDATE portfolio_snapshot
                   SET current_price = ?,
                       last_synced   = datetime('now','utc')
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

    # 2. Determine symbol lists -----------------------------------------------

    # Tier 1: always fetch (need fresh prices for SL checks)
    tier1_symbols: list[str] = _get_portfolio_symbols(db)
    logger.info("[%s] Tier 1 (portfolio): %d symbols", WORKER_NAME, len(tier1_symbols))

    # Tier 2: watchlist — region-aware (only fetch instruments whose market is open)
    watchlist = cfg.get("watchlist", DEFAULT_WATCHLIST)
    if isinstance(watchlist, str):
        watchlist = [watchlist]

    # Filter: only include watchlist items whose specific market is currently open
    tier2_symbols: list[str] = [
        sym for sym in watchlist if is_market_open(sym)
    ]
    skipped = [sym for sym in watchlist if not is_market_open(sym)]

    logger.info(
        "[%s] Tier 2 region-aware: %d open, %d market-closed out of %d watchlist symbols",
        WORKER_NAME, len(tier2_symbols), len(skipped), len(watchlist),
    )
    if skipped:
        logger.debug("[%s] Skipped (market closed): %s", WORKER_NAME, ', '.join(skipped[:10]))

    # Merge and deduplicate, preserving Tier 1 priority
    all_symbols_raw: list[str] = list(dict.fromkeys(tier1_symbols + tier2_symbols))

    # 2c. Apply yfinance alias map
    # Build reverse map: yf_ticker -> original symbol
    alias_to_original: dict[str, str] = {}
    all_yf_symbols: list[str] = []
    for sym in all_symbols_raw:
        yf_sym = _apply_alias(sym)
        if yf_sym not in alias_to_original:
            alias_to_original[yf_sym] = sym
            all_yf_symbols.append(yf_sym)

    logger.info("[%s] Total symbols to fetch: %d", WORKER_NAME, len(all_yf_symbols))

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
            if result.direction == "BUY" and not is_market_open(original_sym):
                logger.debug(
                    "DataWorker: market closed for %s — signal skipped", original_sym
                )
                continue

            # Resolve instrument_id (required FK for signals table)
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
            logger.error(
                "[%s] Error processing %s: %s",
                WORKER_NAME, original_sym, exc, exc_info=True,
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

    # 10. Summary -------------------------------------------------------------
    elapsed = time.monotonic() - t_start
    print(
        f"DataWorker: {n_fetched} symbols fetched, "
        f"{n_signals} signals written (market_open={is_market_open()})"
    )

    # Discord: data worker summary (only if interesting)
    if n_signals > 0:
        open_regions = get_market_status()
        _post('post_alert_embed',
            title=f'📊 Data Worker: {n_signals} Signal(s) generated',
            description=(
                f'Symbole: {n_fetched} | Signale: {n_signals}\n'
                f'Offene Märkte: {open_regions}'
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
