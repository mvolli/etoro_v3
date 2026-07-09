#!/usr/bin/env python3
"""eToro Trading Bot V3 — Discovery Worker
src/bot/workers/discovery_worker.py

Runs every 2 hours (raised from 4x/day — see fix/signal-pool-exhaustion).
Scans the core instrument universe (80 symbols + multi-asset watchlist)
plus one rotating slice of the EU universe (~2900 instruments, see
EU_DISCOVERY_CHUNK_COUNT) for trading candidates and updates the
watchlist / signals DB.

Schedule (cron, CET):
  0 */2 * * * cd /path/to/etoro_v3 && python3 -m bot.workers.discovery_worker
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

MAX_STORE = 30          # final candidates stored as signals in DB
                         # (raised from 15 — see fix/signal-pool-exhaustion:
                         #  at 3 candidates/15min-cycle, 15 signals lasted only
                         #  ~75min between 4x/day discovery runs, leaving the
                         #  pool empty for hours during open market sessions)
MAX_CANDIDATES = 40     # pre-sector-filter pool size (raised alongside MAX_STORE)
MIN_BUY_SCORE = 30.0    # minimum score to qualify
MIN_VOLUME_USD = 500_000  # Mindest-Tagesvolumen in USD (20-Tage-Avg) — illiquide Symbole ueberspringen
SIGNAL_TTL_MINUTES = 1440  # 24 hours — voller EU-Rotationszyklus (16h) mit Puffer

# Full 80-symbol universe (stocks/ETFs/crypto)
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


def _get_multiasset_universe(db: Any) -> list[tuple[str, int]]:
    """
    Load Multi-Asset watchlist from DB (Forex, Commodities, Indices).
    Returns list of (yfinance_symbol, instrument_id) tuples.
    Skips instruments already covered by FULL_UNIVERSE.
    """
    try:
        rows = db.fetchall("""
            SELECT i.yfinance_symbol, i.instrument_id
            FROM watchlist_multiasset w
            JOIN instruments i ON w.instrument_id = i.instrument_id
            WHERE w.is_active = 1
              AND (i.is_tradable IS NULL OR i.is_tradable = 1)
              AND i.yfinance_symbol IS NOT NULL
              AND i.asset_class IN ('forex', 'commodity', 'index')
        """)
        # Deduplicate yfinance symbols (keep first instrument_id)
        seen: set[str] = set()
        result: list[tuple[str, int]] = []
        for row in rows:
            yf_sym = row["yfinance_symbol"] if isinstance(row, dict) else row[0]
            inst_id = row["instrument_id"] if isinstance(row, dict) else row[1]
            if yf_sym and yf_sym not in seen:
                seen.add(yf_sym)
                result.append((yf_sym, inst_id))
        return result
    except Exception as exc:
        logger.warning("[%s] Failed to load multi-asset watchlist: %s", WORKER_NAME, exc)
        return []

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

# ── EU universe rotation (fix/eu-watchlist-expansion) ────────────────────────
# ~2900 EU instruments were reactivated 2026-07-03 (see docs/legacy/ +
# scripts/fix_eu_yfinance_symbols.py) but were invisible to discovery, which
# only scanned the hardcoded FULL_UNIVERSE (a single EU ticker: ENI.MI) plus
# watchlist_multiasset (forex/commodity/index only). Scanning all ~2900 in
# one run would ~15x the batch size and risk Yahoo rate limits, so the
# universe is partitioned deterministically by instrument_id % CHUNK_COUNT —
# each run covers one stable slice. At the current every-2h schedule (12
# runs/day), CHUNK_COUNT=16 gives a full rotation in ~32h (~1.3 days).
EU_DISCOVERY_CHUNK_COUNT = 16
EU_DISCOVERY_CHUNK_STATE_KEY = "discovery_eu_chunk_idx"

# fix/watchlist-promotion-all-regions: discovery's signal_repo.create() is a
# one-shot, 6h-TTL entry — a genuinely promising candidate from ANY region
# would otherwise vanish from active tracking after one cycle, not just EU
# ones. Promote it into the watchlist (5-min tracking) instead, capped
# per-region so one region can't crowd out another's watchlist slots; only
# displaces the weakest existing same-region slot when full, and only if the
# new candidate scores higher.
WATCHLIST_DISCOVERY_CAP = 60
_UNKNOWN_REGION_CATEGORY = "global.discovered"


def _discovery_category_for_region(market_region: str | None) -> str:
    """Map an instruments.market_region value to its watchlist category."""
    if not market_region:
        return _UNKNOWN_REGION_CATEGORY
    return f"{market_region.lower()}.discovered"


def _get_eu_discovery_chunk(
    db: Any, chunk_idx: int, chunk_count: int = EU_DISCOVERY_CHUNK_COUNT
) -> list[tuple[str, int, str]]:
    """Return (yfinance_symbol, instrument_id, symbol) for one rotating slice
    of the EU stock/ETF universe."""
    try:
        rows = db.fetchall(
            """
            SELECT instrument_id, symbol, yfinance_symbol
            FROM instruments
            WHERE market_region = 'EU'
              AND is_active = 1
              AND (is_tradable IS NULL OR is_tradable = 1)
              AND asset_class IN ('stock', 'etf')
              AND yfinance_symbol IS NOT NULL
              AND yfinance_symbol != ''
              AND instrument_id % ? = ?
            ORDER BY instrument_id
            """,
            (chunk_count, chunk_idx),
        )
        return [(row["yfinance_symbol"], row["instrument_id"], row["symbol"]) for row in rows]
    except Exception as exc:
        logger.warning("[%s] _get_eu_discovery_chunk failed: %s", WORKER_NAME, exc)
        return []


def _ensure_watchlist_score_columns(db: Any) -> None:
    """Lazy migration: watchlist had no way to rank entries for eviction."""
    for ddl in (
        "ALTER TABLE watchlist ADD COLUMN last_score REAL",
        "ALTER TABLE watchlist ADD COLUMN last_signal_at TEXT",
    ):
        try:
            db.execute(ddl)
        except Exception:
            pass  # column already exists


def _promote_to_watchlist(
    db: Any, instrument_id: int, symbol: str, score: float, cap: int = WATCHLIST_DISCOVERY_CAP
) -> None:
    """Promote a verified BUY-candidate into the watchlist for ongoing 5-min
    tracking, regardless of region. Never raises.

    Each region gets its own capped pool (category '<region>.discovered') so
    e.g. a flood of EU candidates can't crowd out US/Asia watchlist slots.
    """
    try:
        row = db.fetchone(
            "SELECT market_region FROM instruments WHERE instrument_id = ?", (instrument_id,)
        )
        if row is None:
            return
        category = _discovery_category_for_region(row["market_region"])

        _ensure_watchlist_score_columns(db)

        existing = db.fetchone(
            "SELECT id FROM watchlist WHERE instrument_id = ? AND category = ?",
            (instrument_id, category),
        )
        if existing is not None:
            db.execute(
                "UPDATE watchlist SET last_score = ?, last_signal_at = datetime('now','utc') "
                "WHERE id = ?",
                (score, existing["id"]),
            )
            return

        count_row = db.fetchone(
            "SELECT count(*) AS n FROM watchlist WHERE category = ?", (category,)
        )
        count = count_row["n"] if count_row else 0

        if count < cap:
            db.execute(
                "INSERT INTO watchlist (symbol, instrument_id, category, last_score, last_signal_at) "
                "VALUES (?, ?, ?, ?, datetime('now','utc'))",
                (symbol, instrument_id, category, score),
            )
            logger.info(
                "[%s] watchlist(%s): added %s (score=%.1f, %d/%d slots)",
                WORKER_NAME, category, symbol, score, count + 1, cap,
            )
            return

        # At cap — only displace the weakest existing same-region slot, and
        # only if we beat it.
        weakest = db.fetchone(
            "SELECT id, symbol, last_score FROM watchlist WHERE category = ? "
            "ORDER BY COALESCE(last_score, -1e9) ASC LIMIT 1",
            (category,),
        )
        if weakest is None:
            return
        weakest_score = weakest["last_score"]
        if weakest_score is not None and score <= weakest_score:
            return  # not strong enough to displace anything — stays a one-off signal

        db.execute("DELETE FROM watchlist WHERE id = ?", (weakest["id"],))
        db.execute(
            "INSERT INTO watchlist (symbol, instrument_id, category, last_score, last_signal_at) "
            "VALUES (?, ?, ?, ?, datetime('now','utc'))",
            (symbol, instrument_id, category, score),
        )
        logger.info(
            "[%s] watchlist(%s): %s (score=%.1f) displaced %s (score=%s) — cap %d reached",
            WORKER_NAME, category, symbol, score, weakest["symbol"], weakest_score, cap,
        )
    except Exception as exc:
        logger.warning("[%s] watchlist promotion failed for %s: %s", WORKER_NAME, symbol, exc)


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
) -> int | None:
    """
    Resolve symbol → eToro instrument_id via the local map.

    fix/discovery-identity-verification: returns None when unknown.
    The old fallback generated abs(hash(symbol)) % 900_000 + 100_000 —
    a FAKE instrument_id that (a) was not even deterministic across
    processes (PYTHONHASHSEED randomization), (b) could collide with a
    REAL eToro instrument, and (c) was persisted into the `instruments`
    table via INSERT OR IGNORE, permanently poisoning symbol resolution.
    This is the mechanism behind the placeholder pollution the symbol
    audit is cleaning up, and behind the VALT.L→$5,200 incident.
    Unresolvable symbols now simply produce NO signal (fail-closed).
    """
    sym_upper = symbol.strip().upper()
    for iid, sym in instrument_map.items():
        if sym.strip().upper() == sym_upper:
            return iid
    return None


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


def _post_discord(
    candidates: list[dict],
    scanned: int = 0,
    stored: int = 0,
    unverified: int = 0,
    elapsed_s: float = 0.0,
) -> None:
    """
    Post the top 5 candidates as a data-rich Discord embed
    (post_discovery_embed: Ranking, Score, Asset-Klasse, Preis, RSI,
    Trend, Begründung, Portfolio-Fit).
    Gracefully degrades if discord_embeds is unavailable.
    """
    if not candidates:
        return

    if _DE and hasattr(_DE, "post_discovery_embed"):
        _discord(
            "post_discovery_embed",
            candidates=candidates,
            scanned=scanned,
            stored=stored,
            unverified=unverified,
            elapsed_s=elapsed_s,
        )
        logger.info("[%s] Discovery embed posted (%d candidates)", WORKER_NAME, min(len(candidates), 5))
        return

    # Fallback: print
    lines = [
        f"{i}. {c['symbol']} — Score={c['score']:.0f} ({c['conviction']})"
        for i, c in enumerate(candidates[:5], 1)
    ]
    print("─── Discovery Top 5 ─────────────────────────────────")
    print("\n".join(lines))
    print("─────────────────────────────────────────────────────")


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> int:
    """
    Full discovery cycle:
      1. Load config + init DB
      2. Batch-fetch OHLCV for 80 symbols + multi-asset + 1 rotating EU chunk
      3. Compute TA indicators
      4. Apply Trading Bible V4 signal rules
      5. Filter: BUY signals with score >= 30
      6. Rank by score, take top 20
      7. Apply sector-diversity cap (max 3 per sector)
      8. Ensure instruments exist in DB
      9. Store top MAX_STORE candidates as signals (TTL 6h)
     10. Summary print + Discord embed
    """
    # ── Worker lock: prevent overlapping cron invocations ────────────────────
    from bot.core.worker_lock import worker_lock

    with worker_lock("discovery_worker") as acquired:
        if not acquired:
            print("DiscoveryWorker: SKIPPED (already running)")
            return 0

        t_start = time.monotonic()

        # ── 1. Setup ──────────────────────────────────────────────────────────────
        _load_env()
        cfg = _load_config()
    
        from bot.db.connection import DB
        from bot.db.repo import LogRepo, SignalRepo, StateRepo
        from bot.core.signals import compute_indicators, generate_signal
    
        db_cfg = cfg.get("db", {})
        db_path = PROJECT_ROOT / db_cfg.get("path", "data/trading.db")
        busy_timeout_ms = int(db_cfg.get("busy_timeout_ms", 5000))
        db = DB(db_path=db_path, busy_timeout_ms=busy_timeout_ms)
    
        signal_repo = SignalRepo(db)
        log_repo = LogRepo(db)
        state_repo = StateRepo(db)

        # ── Heartbeat (dead-man's switch) ──────────────────────────────────────────
        try:
            from bot.db.repo import StateRepo as _StateRepo
            from bot.core.heartbeat import record_heartbeat as _record_heartbeat
            _record_heartbeat(_StateRepo(db), "discovery_worker")
        except Exception:
            pass

        # ── API client for identity/price verification ─────────────────────────────
        # fix/discovery-identity-verification: signals are only stored after
        # the resolved instrument_id has been verified against eToro
        # (identity + price cross-check). Without API credentials, discovery
        # stores NOTHING — an unverified signal pool is exactly the bug
        # class that produced the VALT.L incident.
        from bot.api.client import APIError, ClientConfig, EToroClient
        _api_key = os.environ.get("ETORO_API_KEY", "")
        _user_key = os.environ.get("ETORO_USER_KEY", "")
        verify_client: EToroClient | None = None
        if _api_key and _user_key:
            verify_client = EToroClient(
                api_key=_api_key,
                user_key=_user_key,
                config=ClientConfig.from_dict(cfg.get("api", {})),
            )
        else:
            logger.critical(
                "[%s] ETORO_API_KEY/ETORO_USER_KEY fehlen — Kandidaten können nicht "
                "verifiziert werden, es werden KEINE Signale gespeichert (fail-closed)",
                WORKER_NAME,
            )
    
        instrument_map = _load_instrument_map()
        logger.info(
            "[%s] Loaded %d instruments from map", WORKER_NAME, len(instrument_map)
        )
    
        # ── 2. Build combined symbol universe (stocks + multi-asset + EU chunk) ────
        all_symbols: list[str] = list(FULL_UNIVERSE)
        symbol_to_inst_id: dict[str, int] = {}  # maps yf_symbol → instrument_id for multi-asset

        # Load multi-asset watchlist from DB
        multiasset = _get_multiasset_universe(db)
        logger.info("[%s] Loaded %d multi-asset instruments from watchlist", WORKER_NAME, len(multiasset))
        for yf_sym, inst_id in multiasset:
            if yf_sym not in all_symbols:
                all_symbols.append(yf_sym)
                symbol_to_inst_id[yf_sym] = inst_id

        # fix/eu-watchlist-expansion: one rotating slice of the ~2900-strong
        # EU universe per run (see EU_DISCOVERY_CHUNK_COUNT docstring above).
        try:
            eu_chunk_idx = int(state_repo.get(EU_DISCOVERY_CHUNK_STATE_KEY, "0")) % EU_DISCOVERY_CHUNK_COUNT
        except (TypeError, ValueError):
            eu_chunk_idx = 0
        # 2 Chunks pro Run → voller Zyklus 8 Runs × 2h = 16h statt 32h
        EU_CHUNKS_PER_RUN = 2
        for _chunk_offset in range(EU_CHUNKS_PER_RUN):
            _cidx = (eu_chunk_idx + _chunk_offset) % EU_DISCOVERY_CHUNK_COUNT
            eu_chunk = _get_eu_discovery_chunk(db, _cidx)
            logger.info(
                "[%s] EU chunk %d/%d: %d instruments",
                WORKER_NAME, _cidx, EU_DISCOVERY_CHUNK_COUNT, len(eu_chunk),
            )
            for yf_sym, inst_id, _orig_sym in eu_chunk:
                if yf_sym not in all_symbols:
                    all_symbols.append(yf_sym)
                    symbol_to_inst_id[yf_sym] = inst_id
        state_repo.set(EU_DISCOVERY_CHUNK_STATE_KEY, str((eu_chunk_idx + EU_CHUNKS_PER_RUN) % EU_DISCOVERY_CHUNK_COUNT))

        # ── Release DB connection before yfinance (prevents lock conflicts with data_worker) ──
        db.close()
        logger.info("[%s] DB released — starting yfinance batch fetch", WORKER_NAME)
    
        logger.info("[%s] Starting discovery scan of %d total symbols", WORKER_NAME, len(all_symbols))
        try:
            price_data = _batch_fetch(all_symbols)
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
    
                # Prio 5a: Liquiditaets-Filter — geringe Liquiditaet fuehrt zu Ghost-Orders
                try:
                    avg_vol = float(df["Volume"].iloc[-20:].mean())
                    _price = indicators.get("price") or float(df["Close"].iloc[-1])
                    vol_usd = avg_vol * _price
                    if vol_usd < MIN_VOLUME_USD:
                        logger.debug(
                            "[%s] %s: vol_usd=%.0f < %.0f — skipped (illiquid)",
                            WORKER_NAME, symbol, vol_usd, MIN_VOLUME_USD,
                        )
                        continue
                except Exception:
                    pass  # Volume-Berechnung fehlgeschlagen — Symbol trotzdem zulassen
    
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
    
        # ── 8+9. Resolve → VERIFY → store signals ──────────────────────────────────
        # fix/discovery-identity-verification: every candidate's instrument_id
        # is verified against eToro (identity via metadata, plausibility via
        # price cross-check) BEFORE a signal is stored. Mismatches like
        # VALT.L↔Valterra can no longer enter the signal pool.
        from bot.core.instrument_verification import (
            MAX_PRICE_DEVIATION_PCT_DEFAULT,
            verify_candidate,
        )

        max_dev_pct = float(
            cfg.get("discovery", {}).get(
                "max_price_deviation_pct", MAX_PRICE_DEVIATION_PCT_DEFAULT
            )
        )

        j_stored = 0
        n_unresolved = 0
        unverified: list[str] = []  # "SYMBOL: reason" for summary/Discord

        # Pass 1: resolve instrument_ids (fail-closed, no placeholders)
        resolved: list[tuple[dict, int]] = []  # (candidate, instrument_id)
        for cand in store_candidates:
            symbol = cand["symbol"]
            if symbol in symbol_to_inst_id:
                instrument_id = symbol_to_inst_id[symbol]
            else:
                instrument_id = _symbol_to_instrument_id(symbol, instrument_map)

            if instrument_id is None:
                # Last chance: an existing (non-placeholder) instruments row.
                row = db.fetchone(
                    "SELECT instrument_id FROM instruments WHERE symbol = ? LIMIT 1",
                    (symbol,),
                )
                if row is not None:
                    instrument_id = int(row["instrument_id"])

            if instrument_id is None:
                n_unresolved += 1
                logger.warning(
                    "[%s] %s: keine instrument_id auflösbar — Signal wird NICHT "
                    "gespeichert (fail-closed, kein Placeholder mehr)",
                    WORKER_NAME, symbol,
                )
                continue
            cand["instrument_id"] = int(instrument_id)   # für Embed-Anzeige
            resolved.append((cand, int(instrument_id)))

        # Pass 2: batch metadata for all resolved IDs (1 request / 50 IDs)
        metadata_by_id: dict[int, dict] = {}
        if verify_client is not None and resolved:
            try:
                metadata_by_id = verify_client.get_instruments_metadata_batch(
                    [iid for _, iid in resolved]
                )
            except APIError as exc:
                logger.warning(
                    "[%s] Batch-Metadata fehlgeschlagen (%s) — Identity-Check "
                    "läuft fail-open, Preis-Check bleibt aktiv",
                    WORKER_NAME, exc,
                )
            except Exception as exc:
                logger.warning(
                    "[%s] Batch-Metadata unerwartet fehlgeschlagen: %s",
                    WORKER_NAME, exc,
                )

        # Pass 3: verify + store
        for cand, instrument_id in resolved:
            symbol = cand["symbol"]
            try:
                if verify_client is None:
                    unverified.append(f"{symbol}: keine API-Credentials — nicht verifizierbar")
                    continue

                live_price = verify_client.get_current_price(instrument_id)
                ok, reason = verify_candidate(
                    expected_symbol=symbol,
                    meta=metadata_by_id.get(instrument_id),
                    reference_price=cand.get("price"),
                    live_price=live_price,
                    max_deviation_pct=max_dev_pct,
                )
                if not ok:
                    unverified.append(f"{symbol} (ID {instrument_id}): {reason}")
                    logger.error(
                        "[%s] VERIFIKATION FEHLGESCHLAGEN — %s (ID %d): %s",
                        WORKER_NAME, symbol, instrument_id, reason,
                    )
                    log_repo.write(
                        "ERROR", WORKER_NAME,
                        f"Kandidat verworfen: {symbol} (ID {instrument_id})",
                        {"reason": reason, "yf_price": cand.get("price"),
                         "etoro_price": live_price},
                    )
                    continue

                logger.info("[%s] Verifiziert: %s — %s", WORKER_NAME, symbol, reason)

                # Ensure instrument row exists (verified ID only)
                _ensure_instrument(symbol, instrument_id, db)

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

                # fix/watchlist-promotion-all-regions: a discovery signal is
                # a one-shot with a 6h TTL — promote the candidate into the
                # watchlist (region-capped) so it keeps getting tracked every
                # 5 min instead of vanishing.
                _promote_to_watchlist(db, instrument_id, symbol, cand["score"])
    
            except Exception as exc:
                logger.warning(
                    "[%s] Failed to store signal for %s: %s",
                    WORKER_NAME, symbol, exc, exc_info=True,
                )

        if verify_client is not None:
            try:
                verify_client.close()
            except Exception:
                pass

        # Alert when verification dropped candidates — this is exactly the
        # data-quality signal the symbol audit should pick up.
        if unverified:
            _discord(
                "post_alert_embed",
                title=f"🟠 Discovery: {len(unverified)} Kandidat(en) nicht verifiziert",
                description="\n".join(f"• {u[:180]}" for u in unverified[:8]),
                severity="WARNING",
            )
    
        # ── 10. Summary ───────────────────────────────────────────────────────────
        elapsed = time.monotonic() - t_start
        summary = (
            f"DiscoveryWorker: scanned {n_scanned} symbols, "
            f"{k_candidates} BUY candidates, "
            f"{j_stored} stored (top {MAX_STORE}), "
            f"{len(unverified)} unverified, {n_unresolved} unresolved, "
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
                    "n_unverified": len(unverified),
                    "n_unresolved": n_unresolved,
                    "elapsed_s":    round(elapsed, 2),
                    "top_symbols":  [c["symbol"] for c in store_candidates],
                },
            )
        except Exception as exc:
            logger.warning("[%s] Could not write to system_log: %s", WORKER_NAME, exc)
    
        # ── 11. Discord embed ─────────────────────────────────────────────────────
        _post_discord(
            store_candidates,
            scanned=n_scanned,
            stored=j_stored,
            unverified=len(unverified),
            elapsed_s=elapsed,
        )
    
        return 0
    
    
if __name__ == "__main__":
    sys.exit(main())
