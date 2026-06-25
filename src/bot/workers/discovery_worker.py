#!/usr/bin/env python3
"""eToro Trading Bot V3 — Discovery Worker
src/bot/workers/discovery_worker.py

Runs 4x daily at 06:00, 12:00, 18:00, 00:00 CET.
Scans the full instrument universe (80 symbols) for trading candidates
and updates the watchlist / signals DB.

Schedule (cron, CET):
  0 0,6,12,18 * * * cd /path/to/etoro_v3 && python3 -m bot.workers.discovery_worker
"""
from __future__ import annotations

import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any

import yaml

# ── Path setup ────────────────────────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent  # 4 levels up
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

# ── Discord Embeds ─────────────────────────────────────────────────────────────
try:
    import sys as _sys, os as _os
    _sys.path.insert(0, _os.path.join(_os.path.dirname(__file__), '..'))
    import discord_embeds as _DE
except Exception:
    _DE = None

def _discord(fn_name: str, **kwargs) -> None:
    """Best-effort Discord post. Never raises."""
    try:
        if _DE and hasattr(_DE, fn_name):
            getattr(_DE, fn_name)(**kwargs)
    except Exception:
        pass

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger("discovery_worker")

# ── Constants ─────────────────────────────────────────────────────────────────

WORKER_NAME = "discovery_worker"

MAX_STORE = 15          # final candidates stored as signals in DB
MAX_CANDIDATES = 20     # pre-sector-filter pool size
MIN_BUY_SCORE = 30.0    # minimum score to qualify
SIGNAL_TTL_MINUTES = 360  # 6 hours

# Full 80-symbol universe
FULL_UNIVERSE: list[str] = [
    # US Tech (20)
    "AAPL", "NVDA", "META", "MSFT", "AMZN", "GOOGL", "TSLA", "NFLX",
    "AMD", "INTC", "ADBE", "CRM", "ORCL", "PYPL", "UBER", "LYFT",
    "SNAP", "TWLO", "ZM", "DOCU",
    # Broad ETF (5)
    "QQQ", "SPY", "IWM", "VTI", "EEM",
    # Financial (9)
    "JPM", "BAC", "GS", "V", "MA", "BRK-B", "AXP", "C", "WFC",
    # Consumer (9)
    "WMT", "HD", "PG", "KO", "PEP", "COST", "NKE", "MCD", "SBUX",
    # Healthcare (7)
    "JNJ", "UNH", "PFE", "ABBV", "MRK", "LLY", "TMO",
    # Energy (4)
    "XOM", "CVX", "COP", "SLB",
    # Commodity/Alt (5)
    "GLD", "SLV", "CPER", "TLT", "USO",
    # Crypto (5)
    "BTC-USD", "ETH-USD", "XRP-USD", "SOL-USD", "BNB-USD",
    # International (6)
    "ENI.MI", "BP", "SHEL", "TSM", "BABA",
]

SECTOR_MAP: dict[str, str] = {
    "NVDA": "US_TECH", "AAPL": "US_TECH", "META": "US_TECH", "MSFT": "US_TECH",
    "AMZN": "US_TECH", "GOOGL": "US_TECH", "TSLA": "US_TECH", "NFLX": "US_TECH",
    "AMD": "US_TECH", "INTC": "US_TECH", "ADBE": "US_TECH", "CRM": "US_TECH",
    "QQQ": "ETF", "SPY": "ETF", "IWM": "ETF", "VTI": "ETF",
    "JPM": "FINANCIAL", "BAC": "FINANCIAL", "GS": "FINANCIAL",
    "V": "FINANCIAL", "MA": "FINANCIAL",
    "WMT": "CONSUMER", "HD": "CONSUMER", "PG": "CONSUMER", "KO": "CONSUMER",
    "JNJ": "HEALTHCARE", "UNH": "HEALTHCARE", "PFE": "HEALTHCARE",
    "XOM": "ENERGY", "CVX": "ENERGY",
    "GLD": "COMMODITY", "SLV": "COMMODITY",
    "BTC-USD": "CRYPTO", "ETH-USD": "CRYPTO", "XRP-USD": "CRYPTO",
    "ENI.MI": "INTL", "TSM": "INTL",
}

MAX_PER_SECTOR = 3


# ── Helpers ───────────────────────────────────────────────────────────────────

def _load_config() -> dict:
    """Load config/config.yaml relative to the project root."""
    cfg_path = PROJECT_ROOT / "config" / "config.yaml"
    if not cfg_path.is_file():
        logger.warning("Config not found at %s — using defaults", cfg_path)
        return {}
    with cfg_path.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def _load_env() -> None:
    """Load environment variables from ~/.hermes/.env if present."""
    env_path = Path.home() / ".hermes" / ".env"
    if not env_path.exists():
        logger.debug(".env not found at %s — relying on existing environment", env_path)
        return
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip())


def _load_instrument_map() -> dict[int, str]:
    """
    Load instrument_map.json directly from data/.
    Format: {"_meta": {...}, "map": {"1003": "META", ...}}
    Returns {instrument_id (int): symbol (str)}.
    """
    map_path = PROJECT_ROOT / "data" / "instrument_map.json"
    if not map_path.is_file():
        logger.warning("instrument_map.json not found at %s", map_path)
        return {}
    try:
        raw = json.loads(map_path.read_text(encoding="utf-8"))
        return {int(k): v for k, v in raw.get("map", {}).items()}
    except Exception as exc:
        logger.warning("Failed to load instrument_map.json: %s", exc)
        return {}


def _symbol_to_instrument_id(
    symbol: str,
    instrument_map: dict[int, str],
) -> int:
    """
    Resolve symbol → eToro instrument_id.
    Falls back to a deterministic placeholder if not in the known map.
    """
    sym_upper = symbol.strip().upper()
    for iid, sym in instrument_map.items():
        if sym.strip().upper() == sym_upper:
            return iid
    # Deterministic placeholder: hash-based, 100000–999999
    return abs(hash(symbol)) % 900_000 + 100_000


def _batch_fetch(symbols: list[str]) -> dict[str, Any]:
    """
    Download 3 months of OHLCV data for all symbols in one yf.download() call.

    Handles both single-symbol (flat columns) and multi-symbol
    (two-level MultiIndex: Attribute / Ticker) responses from yfinance.

    Returns:
        {symbol: DataFrame(Open, High, Low, Close, Volume)} for symbols
        with >= 30 rows.
    """
    import yfinance as yf
    import pandas as pd  # noqa: F401 — ensures pandas is available

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
        logger.error("[%s] yf.download returned empty DataFrame — aborting", WORKER_NAME)
        raise RuntimeError("yf.download returned empty DataFrame for full universe")

    result: dict[str, Any] = {}

    if len(symbols) == 1:
        sym = symbols[0]
        try:
            df = raw[["Open", "High", "Low", "Close", "Volume"]].dropna(subset=["Close"])
            if len(df) >= 30:
                result[sym] = df
            else:
                logger.debug("[%s] %s: only %d rows — skipped", WORKER_NAME, sym, len(df))
        except Exception as exc:
            logger.warning("[%s] %s: single-symbol extraction failed — %s", WORKER_NAME, sym, exc)
    else:
        for sym in symbols:
            try:
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


def _ensure_instrument(
    symbol: str,
    instrument_id: int,
    db: Any,
) -> None:
    """Insert the instrument row if it does not already exist."""
    db.execute(
        """
        INSERT OR IGNORE INTO instruments (instrument_id, symbol)
        VALUES (?, ?)
        """,
        (instrument_id, symbol),
    )


def _apply_sector_filter(
    candidates: list[dict],
    max_per_sector: int = MAX_PER_SECTOR,
) -> list[dict]:
    """
    Apply sector-diversity cap: at most max_per_sector candidates per sector.
    Candidates are assumed to be already sorted by score descending.
    """
    sector_counts: dict[str, int] = {}
    filtered: list[dict] = []

    for cand in candidates:
        sym = cand["symbol"]
        sector = SECTOR_MAP.get(sym, "OTHER")
        count = sector_counts.get(sector, 0)
        if count < max_per_sector:
            filtered.append(cand)
            sector_counts[sector] = count + 1

    return filtered


def _post_discord(candidates: list[dict]) -> None:
    """
    Post the top 5 candidates as a Discord embed.
    Gracefully degrades if discord_embeds is unavailable.
    """
    top5 = candidates[:5]
    if not top5:
        return

    # Build description text
    lines: list[str] = []
    for i, c in enumerate(top5, 1):
        rsi_str = f"RSI={c['rsi']:.1f}" if c.get("rsi") is not None else "RSI=N/A"
        bb_str = f"BB%B={c['bb_pct']:.2f}" if c.get("bb_pct") is not None else "BB%B=N/A"
        lines.append(
            f"**{i}. {c['symbol']}** — Score={c['score']:.0f} "
            f"({c['conviction']}) | {rsi_str} | {bb_str}"
        )
    description = "\n".join(lines)

    # Try Discord embed
    if _DE:
        _discord(
            "post_alert_embed",
            title="🔍 Discovery: Top Kandidaten",
            description=description,
            severity="INFO",
            channel="main",
        )
        logger.info("[%s] Discord embed posted (%d candidates)", WORKER_NAME, len(top5))
        return

    # Fallback: print
    print("─── Discovery Top 5 ─────────────────────────────────")
    print(description)
    print("─────────────────────────────────────────────────────")


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> int:
    """
    Full discovery cycle:
      1. Load config + init DB
      2. Batch-fetch OHLCV for 80 symbols
      3. Compute TA indicators
      4. Apply Trading Bible V4 signal rules
      5. Filter: BUY signals with score >= 30
      6. Rank by score, take top 20
      7. Apply sector-diversity cap (max 3 per sector)
      8. Ensure instruments exist in DB
      9. Store top MAX_STORE candidates as signals (TTL 6h)
     10. Summary print + Discord embed
    """
    t_start = time.monotonic()

    # ── 1. Setup ──────────────────────────────────────────────────────────────
    _load_env()
    cfg = _load_config()

    from bot.db.connection import DB
    from bot.db.repo import LogRepo, SignalRepo
    from bot.core.signals import compute_indicators, generate_signal

    db_cfg = cfg.get("db", {})
    db_path = PROJECT_ROOT / db_cfg.get("path", "data/trading.db")
    busy_timeout_ms = int(db_cfg.get("busy_timeout_ms", 5000))
    db = DB(db_path=db_path, busy_timeout_ms=busy_timeout_ms)

    signal_repo = SignalRepo(db)
    log_repo = LogRepo(db)

    instrument_map = _load_instrument_map()
    logger.info(
        "[%s] Loaded %d instruments from map", WORKER_NAME, len(instrument_map)
    )

    # ── 2. Batch-fetch OHLCV ─────────────────────────────────────────────────
    logger.info("[%s] Starting discovery scan of %d symbols", WORKER_NAME, len(FULL_UNIVERSE))
    try:
        price_data = _batch_fetch(FULL_UNIVERSE)
    except Exception as exc:
        logger.critical("[%s] Batch fetch failed — %s", WORKER_NAME, exc, exc_info=True)
        print(f"DiscoveryWorker: FATAL — batch fetch failed: {exc}")
        return 1

    n_scanned = len(price_data)
    logger.info("[%s] %d symbols with usable OHLCV data", WORKER_NAME, n_scanned)

    # ── 3+4+5. Compute indicators + generate signals + filter ─────────────────
    buy_candidates: list[dict] = []

    for symbol, df in price_data.items():
        try:
            indicators = compute_indicators(df)
            if not indicators:
                logger.debug("[%s] %s: no indicators — skipped", WORKER_NAME, symbol)
                continue

            result = generate_signal(symbol, indicators)

            if result.direction != "BUY" or result.score < MIN_BUY_SCORE:
                logger.debug(
                    "[%s] %s: direction=%s score=%.1f — not a candidate",
                    WORKER_NAME, symbol, result.direction, result.score,
                )
                continue

            buy_candidates.append({
                "symbol":     symbol,
                "score":      result.score,
                "conviction": result.conviction,
                "rsi":        result.rsi,
                "macd_hist":  result.macd_hist,
                "bb_pct":     result.bb_pct,
                "price":      result.price,
                "atr":        result.atr,
                "signal_types": result.signal_types,
            })
            logger.info(
                "[%s] CANDIDATE %s score=%.1f conviction=%s",
                WORKER_NAME, symbol, result.score, result.conviction,
            )

        except Exception as exc:
            logger.warning(
                "[%s] Error processing %s: %s", WORKER_NAME, symbol, exc, exc_info=True
            )
            # Per-symbol error: continue with remaining symbols

    k_candidates = len(buy_candidates)
    logger.info("[%s] %d BUY candidates (score >= %.0f)", WORKER_NAME, k_candidates, MIN_BUY_SCORE)

    # ── 6. Rank by score, take top 20 ─────────────────────────────────────────
    buy_candidates.sort(key=lambda c: c["score"], reverse=True)
    top_candidates = buy_candidates[:MAX_CANDIDATES]

    # ── 7. Apply sector-diversity cap ─────────────────────────────────────────
    filtered_candidates = _apply_sector_filter(top_candidates)
    logger.info(
        "[%s] %d candidates after sector filter (max %d/sector)",
        WORKER_NAME, len(filtered_candidates), MAX_PER_SECTOR,
    )

    # Limit to MAX_STORE for DB storage
    store_candidates = filtered_candidates[:MAX_STORE]

    # ── 8+9. Ensure instruments + store signals ────────────────────────────────
    j_stored = 0

    for cand in store_candidates:
        symbol = cand["symbol"]
        try:
            instrument_id = _symbol_to_instrument_id(symbol, instrument_map)

            # Ensure instrument row exists
            _ensure_instrument(symbol, instrument_id, db)

            # Resolve the actual instrument_id from the instruments table
            # (in case _ensure_instrument used a placeholder that differs from
            # the row inserted)
            row = db.fetchone(
                "SELECT instrument_id FROM instruments WHERE symbol = ? LIMIT 1", (symbol,)
            )
            if row is not None:
                instrument_id = row["instrument_id"]

            # Store signal with 6h TTL
            signal_types_str = (
                ",".join(cand["signal_types"]) if cand.get("signal_types") else "BUY"
            )
            signal_repo.create(
                instrument_id=instrument_id,
                signal_type=signal_types_str,
                conviction=cand["conviction"],
                score=cand["score"],
                rsi=cand.get("rsi"),
                macd_hist=cand.get("macd_hist"),
                bb_pct=cand.get("bb_pct"),
                price=cand.get("price"),
                ttl_minutes=SIGNAL_TTL_MINUTES,
            )
            j_stored += 1
            logger.info(
                "[%s] Stored signal — %s (score=%.1f conviction=%s instrument_id=%d)",
                WORKER_NAME, symbol, cand["score"], cand["conviction"], instrument_id,
            )

        except Exception as exc:
            logger.warning(
                "[%s] Failed to store signal for %s: %s",
                WORKER_NAME, symbol, exc, exc_info=True,
            )

    # ── 10. Summary ───────────────────────────────────────────────────────────
    elapsed = time.monotonic() - t_start
    summary = (
        f"DiscoveryWorker: scanned {n_scanned} symbols, "
        f"{k_candidates} BUY candidates, "
        f"{j_stored} stored (top {MAX_STORE}), "
        f"took {elapsed:.1f}s"
    )
    print(summary)

    try:
        log_repo.write(
            "INFO",
            WORKER_NAME,
            summary,
            {
                "n_scanned":    n_scanned,
                "k_candidates": k_candidates,
                "j_stored":     j_stored,
                "elapsed_s":    round(elapsed, 2),
                "top_symbols":  [c["symbol"] for c in store_candidates],
            },
        )
    except Exception as exc:
        logger.warning("[%s] Could not write to system_log: %s", WORKER_NAME, exc)

    # ── 11. Discord embed ─────────────────────────────────────────────────────
    _post_discord(store_candidates)

    return 0


if __name__ == "__main__":
    sys.exit(main())
