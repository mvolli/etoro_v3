#!/usr/bin/env python3
"""Market hours checker — Trading Bible V5.

Centralized market hours logic used by data_worker, signal_worker, execution_worker.

Region-aware: classifies instruments by suffix (.DE, .T, .HK, etc.) and checks
whether that specific market is currently open. Acts as Single Source of Truth
for all workers.

DST-aware: uses zoneinfo (Python stdlib) for accurate timezone handling — no more
fixed UTC tables that break during winter/summer time transitions.

Fail-open: unknown suffixes → True (better to miss a trade than skip a signal).
"""
import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)


# ─── Market Definitions (zoneinfo-based) ────────────────────────────────────────
# Each market defines:
#   - tz: IANA timezone string
#   - open_local / close_local: local trading hours (hour, minute)
#   - weekends_open: bool (default False). True = 24/5 or 24/7.

MARKET_DEFINITIONS: dict[str, dict] = {
    # Crypto — always open (24/7)
    'CRYPTO': {
        'always_open': True,
    },

    # Forex — 24/5 (Sunday ~22:00 → Friday ~22:00 in respective TZ)
    'FOREX': {
        'tz': 'UTC',
        'weekends_partial': True,  # open Sun evening → Fri evening
    },

    # Commodities (CFDs on eToro) — ~24/5 like forex
    'COMMODITIES': {
        'tz': 'UTC',
        'weekends_partial': True,
    },

    # ── Equity Markets ────────────────────────────────────────────────────────

    # USA (NYSE, NASDAQ) — 9:30–16:00 ET
    'US': {
        'tz': 'America/New_York',
        'open_local': (9, 30),
        'close_local': (16, 0),
    },

    # Indices follow US market hours (most are US indices)
    'INDICES_US': {
        'tz': 'America/New_York',
        'open_local': (9, 30),
        'close_local': (16, 0),
    },

    # Europe (Xetra, SIX, LSE, Euronext, Borsa Italiana)
    #   Frankfurt: 09:00–17:30 CET/CEST
    #   London:    08:00–16:30 GMT/BST
    #   We use a conservative overlap window that covers all major EU exchanges
    'EU': {
        'tz': 'Europe/Berlin',
        'open_local': (9, 0),
        'close_local': (17, 30),
    },

    # ── Asia-Pacific ────────────────────────────────────────────────────────

    # Tokyo (JPX) — two sessions with lunch break:
    #   Morning:  09:00–11:30 JST
    #   Afternoon: 12:30–15:00 JST
    'APAC_JP_GROUP': {
        'tz': 'Asia/Tokyo',
        'sessions': [
            ((9, 0), (11, 30)),
            ((12, 30), (15, 0)),
        ],
    },

    # Hong Kong — two sessions with lunch break:
    #   Morning:  09:30–12:00 HKT
    #   Afternoon: 13:00–16:00 HKT
    'APAC_HK_GROUP': {
        'tz': 'Asia/Hong_Kong',
        'sessions': [
            ((9, 30), (12, 0)),
            ((13, 0), (16, 0)),
        ],
    },

    # Shanghai + Shenzhen — two sessions with lunch break:
    #   Morning:  09:30–11:30 CST
    #   Afternoon: 13:00–15:00 CST
    'APAC_CN_GROUP': {
        'tz': 'Asia/Shanghai',
        'sessions': [
            ((9, 30), (11, 30)),
            ((13, 0), (15, 0)),
        ],
    },

    # Sydney (ASX) — 10:00–16:00 AEST/AEDT
    'APAC_AU': {
        'tz': 'Australia/Sydney',
        'open_local': (10, 0),
        'close_local': (16, 0),
    },

    # India (NSE/BSE) — 09:15–15:30 IST
    'APAC_IN': {
        'tz': 'Asia/Kolkata',
        'open_local': (9, 15),
        'close_local': (15, 30),
    },

    # Korea + Taiwan + Singapore — single session
    #   Korea (KOSPI):  09:00–15:30 KST
    #   Taiwan (TWSE):  09:00–13:30 CST
    #   Singapore (SGX): 09:00–17:00 SGT
    'APAC_KR_TW_SG': {
        'tz': 'Asia/Seoul',
        'open_local': (9, 0),
        'close_local': (15, 30),
    },
}


# ─── Suffix → Market Key Mapping ─────────────────────────────────────────────
# yfinance ticker suffixes mapped to market definitions above.

SUFFIX_TO_MARKET: dict[str, str] = {
    # Europe
    '.DE': 'EU',     # Germany (Xetra/Frankfurt)
    '.SW': 'EU',     # Switzerland (SIX Zurich)
    '.L': 'EU',      # UK (London Stock Exchange)
    '.AS': 'EU',     # Netherlands (Euronext Amsterdam)
    '.PA': 'EU',     # France (Euronext Paris)
    '.MI': 'EU',     # Italy (Borsa Italiana)
    '.BR': 'EU',     # Belgium (Euronext Brussels)
    '.MC': 'EU',     # Spain (BME Madrid)
    '.ST': 'EU',     # Sweden (Nasdaq Stockholm)
    '.LS': 'EU',     # Portugal (Euronext Lisbon)

    # Asia-Pacific — markets with lunch breaks use a GROUP key
    # that checks BOTH morning and afternoon sessions
    '.T': 'APAC_JP_GROUP',       # Tokyo (JPX) — morning + afternoon
    '.HK': 'APAC_HK_GROUP',      # Hong Kong (HKEX) — morning + afternoon
    '.SS': 'APAC_CN_GROUP',      # Shanghai (SSE) — morning + afternoon
    '.SZ': 'APAC_CN_GROUP',      # Shenzhen (SZSE) — morning + afternoon
    '.AX': 'APAC_AU',            # Sydney (ASX)
    '.NS': 'APAC_IN',            # India (NSE/BSE)
    '.KS': 'APAC_KR_TW_SG',      # Korea (KOSPI)
    '.KQ': 'APAC_KR_TW_SG',      # Korea (KOSDAQ)
    '.TW': 'APAC_KR_TW_SG',      # Taiwan (TWSE)
    '.SI': 'APAC_KR_TW_SG',      # Singapore (SGX)
}


# ─── Category → Market Key Mapping ──────────────────────────────────────────
# Fallback: when symbol suffix doesn't match, use the watchlist category.

CATEGORY_TO_MARKET: dict[str, str] = {
    'crypto': 'CRYPTO',
    'forex': 'FOREX',
    'commodities': 'COMMODITIES',
    'indices': 'INDICES_US',  # default to US indices; override per-symbol if needed
    'stocks': 'US',
    'etfs': 'US',
    'eu.stocks': 'EU',
    'eu.etfs': 'EU',
    'asia.T': 'APAC_JP_GROUP',
    'asia.HK': 'APAC_HK_GROUP',
    'asia.AX': 'APAC_AU',
    'asia.NS': 'APAC_IN',
    'asia.KS': 'APAC_KR_TW_SG',
    'asia.TW': 'APAC_KR_TW_SG',
}


# ─── yf Symbol → Market Key (direct override) ────────────────────────────────
# For instruments that don't follow suffix patterns (forex pairs, commodities, indices).

YF_SYMBOL_MARKET_OVERRIDE: dict[str, str] = {
    # Forex pairs (=X suffix from yfinance)
    'EURUSD=X': 'FOREX', 'GBPUSD=X': 'FOREX', 'NZDUSD=X': 'FOREX',
    'USDCAD=X': 'FOREX', 'USDJPY=X': 'FOREX', 'USDCHF=X': 'FOREX',
    'AUDUSD=X': 'FOREX', 'EURGBP=X': 'FOREX', 'EURCHF=X': 'FOREX',
    'EURJPY=X': 'FOREX', 'GBPAUD=X': 'FOREX', 'GBPJPY=X': 'FOREX',
    'AUDCAD=X': 'FOREX', 'AUDCHF=X': 'FOREX', 'AUDJPY=X': 'FOREX',
    'CADCHF=X': 'FOREX', 'CADJPY=X': 'FOREX', 'CHFJPY=X': 'FOREX',
    'EURAUD=X': 'FOREX', 'EURCAD=X': 'FOREX', 'GBPCAD=X': 'FOREX',
    'GBPCHF=X': 'FOREX', 'NZDCAD=X': 'FOREX', 'NZDCHF=X': 'FOREX',
    'NZDJPY=X': 'FOREX', 'USDCNY=X': 'FOREX', 'USDHKD=X': 'FOREX',
    'USDSGD=X': 'FOREX',

    # Commodities (yfinance patterns)
    'GC=F': 'COMMODITIES',   # Gold futures
    'SI=F': 'COMMODITIES',   # Silver futures
    'CL=F': 'COMMODITIES',   # Crude Oil
    'NG=F': 'COMMODITIES',   # Natural Gas
    'PL=F': 'COMMODITIES',   # Platinum

    # Indices (^ prefix or known tickers)
    '^GSPC': 'INDICES_US',   # S&P 500
    '^DJI': 'INDICES_US',    # Dow Jones
    '^IXIC': 'INDICES_US',   # NASDAQ Composite
    '^RUT': 'INDICES_US',    # Russell 2000
    '^NDX': 'INDICES_US',    # NASDAQ 100
    '^VIX': 'INDICES_US',    # VIX
}


# ─── Crypto Detection ────────────────────────────────────────────────────────

CRYPTO_SYMBOLS = {
    'BTC-USD', 'ETH-USD', 'XRP-USD', 'SOL-USD', 'BNB-USD',
    'DOGE-USD', 'ADA-USD', 'DOT-USD', 'MATIC-USD', 'AVAX-USD',
    'BCH-USD', 'UNI7083-USD',
}


# ─── Public API ──────────────────────────────────────────────────────────────

def _get_market_key(symbol: str, yf_symbol: str = '', category: str = '') -> str:
    """Determine the market key for a symbol using 3-tier lookup.

    Tier 1: Direct yf_symbol override (forex pairs, commodities, indices)
    Tier 2: Suffix-based lookup (.DE, .T, .HK, etc.)
    Tier 3: Category-based fallback (from watchlist category)
    Default: US market
    """
    sym_upper = symbol.upper().strip()

    # ── Tier 1: Direct yf_symbol override ────────────────────────────────
    if yf_symbol and yf_symbol in YF_SYMBOL_MARKET_OVERRIDE:
        return YF_SYMBOL_MARKET_OVERRIDE[yf_symbol]

    # Also check common forex/commodity patterns in yf_symbol
    yf_upper = yf_symbol.upper().strip() if yf_symbol else ''
    if yf_upper.endswith('=X'):
        return 'FOREX'  # yfinance forex pattern: EURUSD=X
    if yf_upper.endswith('=F'):
        return 'COMMODITIES'  # yfinance futures pattern: GC=F
    if yf_upper.startswith('^'):
        return 'INDICES_US'  # yfinance index pattern: ^GSPC

    # Commodities detection (eToro CFD patterns like "GOLD (NO-USD", "OIL (NON-USD")
    commodity_keywords = ['GOLD', 'OIL', 'SILVER', 'NATURAL', 'PLATINUM', 'BRENT', 'COTTON']
    if any(kw in yf_upper for kw in commodity_keywords):
        return 'COMMODITIES'

    # Crypto check (before suffix lookup) — must be AFTER commodities check
    if sym_upper in CRYPTO_SYMBOLS or sym_upper.endswith('-USD') or yf_upper.endswith('-USD'):
        return 'CRYPTO'

    # ── Tier 2: Suffix-based lookup ──────────────────────────────────────
    if '.' in symbol:
        suffix = '.' + sym_upper.split('.')[-1]
        if suffix in SUFFIX_TO_MARKET:
            return SUFFIX_TO_MARKET[suffix]

    # ── Tier 3: Category-based fallback ──────────────────────────────────
    if category and category.lower() in CATEGORY_TO_MARKET:
        return CATEGORY_TO_MARKET[category.lower()]

    # Default: US market (no suffix = US stock/ETF)
    return 'US'


def _is_market_open_now(market_key: str, now_utc: datetime) -> bool:
    """Check if a specific market is open right now using zoneinfo."""
    mkt = MARKET_DEFINITIONS.get(market_key)

    if not mkt:
        # Fail-open: unknown market → assume open
        return True

    # Always-open markets (Crypto 24/7)
    if mkt.get('always_open'):
        return True

    # 24/5 markets (Forex, Commodities) — closed Fri 17:00 ET → Sun 17:00 ET
    if mkt.get('weekends_partial'):
        try:
            tz_ny = ZoneInfo("America/New_York")
            now_ny = now_utc.astimezone(tz_ny)
        except Exception:
            weekday = now_utc.weekday()
            return weekday < 5

        weekday_ny = now_ny.weekday()
        hour_ny = now_ny.hour

        if weekday_ny == 4 and hour_ny >= 17:   # Friday from 17:00 ET
            return False
        if weekday_ny == 5:                       # all day Saturday
            return False
        if weekday_ny == 6 and hour_ny < 17:      # Sunday before 17:00 ET
            return False
        return True

    # Standard equity market with single session
    tz_name = mkt.get('tz')
    open_h, open_m = mkt['open_local']
    close_h, close_m = mkt['close_local']

    # Weekend check
    weekday = now_utc.weekday()
    if weekday >= 5:
        return False

    # Convert UTC now to market local time
    try:
        tz = ZoneInfo(tz_name)
        now_local = now_utc.astimezone(tz)
    except Exception:
        return True  # Fail-open on TZ error

    open_time = (open_h, open_m)
    close_time = (close_h, close_m)
    current_time = (now_local.hour, now_local.minute)

    return open_time <= current_time < close_time


def _is_multi_session_open(market_key: str, now_utc: datetime) -> bool:
    """Check markets with multiple sessions (lunch breaks)."""
    mkt = MARKET_DEFINITIONS.get(market_key)
    if not mkt or 'sessions' not in mkt:
        return False

    weekday = now_utc.weekday()
    if weekday >= 5:
        return False

    tz_name = mkt['tz']
    try:
        tz = ZoneInfo(tz_name)
        now_local = now_utc.astimezone(tz)
    except Exception:
        return True  # Fail-open

    current_time = (now_local.hour, now_local.minute)

    for open_time, close_time in mkt['sessions']:
        if open_time <= current_time < close_time:
            return True

    return False  # Between sessions or outside hours


def is_market_open(
    symbol: str = '',
    yf_symbol: str = '',
    category: str = '',
    fail_open: bool = True,
) -> bool:
    """Returns True if the market for this symbol is currently open.

    Single Source of Truth — used by data_worker, signal_worker, execution_worker.

    When called WITHOUT a symbol, returns True if ANY major market is open
    (used by data_worker to decide whether to scan the watchlist at all).

    Args:
        symbol: Ticker symbol (e.g. 'AAPL', 'VOW3.DE', '7203.T', 'BTC-USD').
                Empty string → check if ANY market is open.
        yf_symbol: yfinance ticker for 3-tier lookup (optional).
        category: Watchlist category for fallback (optional).
        fail_open: Verhalten wenn der aufgelöste Market-Key NICHT in
            MARKET_DEFINITIONS existiert (Mapping-Loch). True (Default) =
            offen annehmen — richtig für Daten-Fetches. False = geschlossen
            annehmen — Pflicht am BUY/Execution-Boundary, wo ein unbekannter
            Markt sonst Ghost-Orders produziert (fix/market-hours-fail-closed).

    Returns:
        True if the relevant market is currently trading.
    """
    now_utc = datetime.now(timezone.utc)

    # No symbol provided — check if ANY market is open (data_worker gate)
    if not symbol.strip():
        for key in MARKET_DEFINITIONS:
            if _check_market_open(key, now_utc):
                return True
        return False

    # Symbol-specific check
    market_key = _get_market_key(symbol, yf_symbol, category)
    if market_key not in MARKET_DEFINITIONS and not fail_open:
        logger.warning(
            "is_market_open: Market-Key %r für %s unbekannt — fail-CLOSED "
            "(BUY-Boundary): Markt gilt als geschlossen",
            market_key, symbol,
        )
        return False
    return _check_market_open(market_key, now_utc)


def _check_market_open(market_key: str, now_utc: datetime) -> bool:
    """Internal dispatcher for market open checks."""
    mkt = MARKET_DEFINITIONS.get(market_key)
    if not mkt:
        return True  # Fail-open (Daten-Pfade; BUY-Pfade nutzen fail_open=False oben)

    if mkt.get('always_open'):
        return True

    if mkt.get('weekends_partial'):
        # Real ET boundary: closed Fri 17:00 ET → Sun 17:00 ET
        # (Forex/Commodities close ~5pm ET Friday, reopen ~5pm ET Sunday)
        try:
            tz_ny = ZoneInfo("America/New_York")
            now_ny = now_utc.astimezone(tz_ny)
        except Exception:
            # Fail-open on TZ error — consistent with rest of this file
            weekday = now_utc.weekday()
            return weekday < 5

        weekday_ny = now_ny.weekday()
        hour_ny = now_ny.hour

        # Closed: Friday from 17:00 ET onwards
        if weekday_ny == 4 and hour_ny >= 17:
            return False
        # Closed: all day Saturday
        if weekday_ny == 5:
            return False
        # Closed: Sunday before 17:00 ET
        if weekday_ny == 6 and hour_ny < 17:
            return False
        return True

    if 'sessions' in mkt:
        return _is_multi_session_open(market_key, now_utc)

    return _is_market_open_now(market_key, now_utc)


def get_instrument_market_key(symbol: str, yf_symbol: str = '', category: str = '') -> str:
    """Public wrapper for _get_market_key — returns the resolved market key."""
    return _get_market_key(symbol, yf_symbol, category)


def is_market_key_open_at(market_key: str, at_utc: datetime) -> bool:
    """True wenn der gegebene Markt-Key zum Zeitpunkt at_utc offen ist.

    Fuer Discovery-Scheduling (fix/region-rotation-market-hours): eine Region
    wird nur gescannt, wenn ihr Markt offen ist oder bald oeffnet — der Aufrufer
    prueft dafuer mehrere Zeitpunkte (jetzt, +1.5h, +3h).
    Unbekannter Key → True (fail-open, konsistent zu _check_market_open)."""
    return _check_market_open(market_key, at_utc)


def get_market_status(symbol: str = '') -> str:
    """Returns human-readable market status string for a symbol."""
    if not symbol.strip():
        now_utc = datetime.now(timezone.utc)
        open_markets = []
        for key in MARKET_DEFINITIONS:
            if _check_market_open(key, now_utc):
                open_markets.append(key)
        return ', '.join(open_markets) if open_markets else 'ALL CLOSED'

    market_key = _get_market_key(symbol)
    mkt = MARKET_DEFINITIONS.get(market_key, {})
    tz_name = mkt.get('tz', 'UTC')

    # Build hours string from local times
    if 'sessions' in mkt:
        sessions_str = ' + '.join(
            f"{oh:02d}:{om}–{ch:02d}:{cm}"
            for (oh, om), (ch, cm) in mkt['sessions']
        )
        return f"{market_key} {sessions_str} {tz_name}"

    if 'open_local' in mkt:
        oh, om = mkt['open_local']
        ch, cm = mkt['close_local']
        return f"{market_key} {oh:02d}:{om}–{ch:02d}:{cm} {tz_name}"

    if mkt.get('always_open'):
        return f"{market_key} 24/7"

    if mkt.get('weekends_partial'):
        return f"{market_key} 24/5 (Mon–Fri)"

    return f"{market_key} (unknown schedule)"


# ─── Quick Test ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    now_utc = datetime.now(timezone.utc)
    print(f"Aktuelle UTC-Zeit: {now_utc.strftime('%Y-%m-%d %H:%M:%S')} (Wochentag: {['Mo','Di','Mi','Do','Fr','Sa','So'][now_utc.weekday()]})")

    # Show DST offsets for key markets
    print("\nDST-Status:")
    for tz_name in ['America/New_York', 'Europe/Berlin', 'Asia/Tokyo', 'Australia/Sydney']:
        tz = ZoneInfo(tz_name)
        now_local = now_utc.astimezone(tz)
        utcoffset = now_local.utcoffset()
        if utcoffset:
            total_hours = utcoffset.total_seconds() / 3600
            print(f"  {tz_name:>25s}: UTC{total_hours:+.1f} → {now_local.strftime('%H:%M')} local")

    print()

    test_symbols = [
        # Crypto
        'BTC-USD', 'ETH-USD', 'XRP-USD',
        # US
        'AAPL', 'TSLA', 'NVDA', 'SPY', 'QQQ',
        # EU
        'VOW3.DE', 'NESN.SW', 'SHEL.L', 'ASML.AS', 'MC.PA', 'ENI.MI',
        # Asia-Pacific
        '7203.T',          # Toyota (Tokyo)
        '9988.HK',         # Tencent (HK)
        '600519.SS',       # Kweichow Moutai (Shanghai)
        '300750.SZ',       # CATL (Shenzhen)
        'BHP.AX',          # BHP (Sydney)
        'RELIANCE.NS',     # Reliance (India)
        '005930.KS',       # Samsung (Korea)
        '2330.TW',         # TSMC (Taiwan)
        # Unknown
        'UNKNOWN.XX',
    ]

    print(f"{'Symbol':<16} {'Markt':<18} {'Status':<12} {'Öffnungszeiten'}")
    print("-" * 80)
    for sym in test_symbols:
        key = _get_market_key(sym)
        open_ = is_market_open(sym)
        status = "🟢 OFFEN" if open_ else "🔴 ZU"
        hours = get_market_status(sym)
        print(f"{sym:<16} {key:<18} {status:<12} {hours}")

    print()
    print(f"Data Worker Gate (irgendwo offen?): {is_market_open()}")
    print(f"Offene Märkte: {get_market_status()}")
