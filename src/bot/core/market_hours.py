#!/usr/bin/env python3
"""Market hours checker — Trading Bible V5.

Centralized market hours logic used by data_worker, signal_worker, execution_worker.

Region-aware: classifies instruments by suffix (.DE, .T, .HK, etc.) and checks
whether that specific market is currently open. Acts as Single Source of Truth
for all workers.

Fail-open: unknown suffixes → True (better to miss a trade than skip a signal).
"""
from datetime import datetime, time, timezone


# ─── Market Definitions ──────────────────────────────────────────────────────
# Structure: 'KEY': (start_utc, end_utc)  OR  (start_utc, end_utc, crosses_midnight)
#
# crosses_midnight=True → market is open when time >= start OR time < end
#                        (e.g. Sydney 21:00–05:00 UTC spans midnight)

MARKET_DEFINITIONS: dict[str, tuple] = {
    # Crypto — always open (24/7)
    'CRYPTO': (time(0, 0), time(23, 59, 59), True),

    # Forex — 24/5 (Sunday 22:00 UTC → Friday 22:00 UTC)
    'FOREX': (time(0, 0), time(23, 59, 59), True),

    # Commodities (CFDs on eToro) — ~24/5 like forex
    'COMMODITIES': (time(0, 0), time(23, 59, 59), True),

    # Indices — follows US market hours (most are US indices)
    # Individual index markets handled below for non-US
    'INDICES_US': (time(13, 30), time(20, 0)),

    # Europe (Xetra, SIX, LSE, Euronext, Borsa Italiana)
    'EU': (time(6, 0), time(17, 0)),

    # USA (NYSE, NASDAQ) — 13:30–20:00 UTC (9:30–16:00 ET)
    'US': (time(13, 30), time(20, 0)),

    # ── Asia-Pacific ────────────────────────────────────────────────────────
    # Tokyo (JPX) — two sessions with lunch break:
    #   Morning: 09:00–11:30 JST = 00:00–02:30 UTC
    #   Afternoon: 12:30–15:00 JST = 03:30–06:00 UTC
    'APAC_JP_M': (time(0, 0), time(2, 30)),     # Tokyo morning session
    'APAC_JP_A': (time(3, 30), time(6, 0)),     # Tokyo afternoon session

    # Hong Kong — two sessions with lunch break:
    #   Morning: 09:30–12:00 HKT = 01:30–04:00 UTC
    #   Afternoon: 13:00–16:00 HKT = 05:00–08:00 UTC
    'APAC_HK_M': (time(1, 30), time(4, 0)),     # HK morning session
    'APAC_HK_A': (time(5, 0), time(8, 0)),      # HK afternoon session

    # Shanghai + Shenzhen — two sessions with lunch break:
    #   Morning: 09:30–11:30 CST = 01:30–03:30 UTC
    #   Afternoon: 13:00–15:00 CST = 05:00–07:00 UTC
    'APAC_CN_M': (time(1, 30), time(3, 30)),    # China morning session
    'APAC_CN_A': (time(5, 0), time(7, 0)),      # China afternoon session

    # Sydney (ASX) — 10:00–16:00 AEDT = 23:00–05:00 UTC (crosses midnight!)
    # In winter (AEST): 10:00–16:00 = 22:00–04:00 UTC — we use the wider summer window
    'APAC_AU': (time(23, 0), time(5, 0), True),

    # India (NSE/BSE) — 09:15–15:30 IST = 03:45–10:00 UTC
    'APAC_IN': (time(3, 45), time(10, 0)),

    # Korea + Taiwan + Singapore — single session ~01:00–08:00 UTC
    'APAC_KR_TW_SG': (time(1, 0), time(8, 0)),
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
    '.BR': 'EU',     # Brazil (B3 / São Paulo)
    '.MC': 'EU',     # Monaco
    '.ST': 'EU',     # Sweden (Nasdaq Stockholm)
    '.LS': 'EU',     # Spain (BME Madrid)

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

# Markets with lunch breaks: map GROUP key → [morning_key, afternoon_key]
MARKET_GROUPS: dict[str, list[str]] = {
    'APAC_JP_GROUP': ['APAC_JP_M', 'APAC_JP_A'],
    'APAC_HK_GROUP': ['APAC_HK_M', 'APAC_HK_A'],
    'APAC_CN_GROUP': ['APAC_CN_M', 'APAC_CN_A'],
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


def is_market_open(symbol: str = '', yf_symbol: str = '', category: str = '') -> bool:
    """Returns True if the market for this symbol is currently open.

    Single Source of Truth — used by data_worker, signal_worker, execution_worker.

    When called WITHOUT a symbol, returns True if ANY major market is open
    (used by data_worker to decide whether to scan the watchlist at all).

    Args:
        symbol: Ticker symbol (e.g. 'AAPL', 'VOW3.DE', '7203.T', 'BTC-USD').
                Empty string → check if ANY market is open.
        yf_symbol: yfinance ticker for 3-tier lookup (optional).
        category: Watchlist category for fallback (optional).

    Returns:
        True if the relevant market is currently trading.
    """
    now = datetime.now(timezone.utc)
    weekday = now.weekday()  # 0=Mon, 6=Sun

    # No symbol provided — check if ANY market is open (data_worker gate)
    if not symbol.strip():
        for key, definition in MARKET_DEFINITIONS.items():
            if _check_time_slot(now.time(), definition):
                return True
        return False

    # Symbol-specific check
    market_key = _get_market_key(symbol, yf_symbol, category)

    # Weekend: 24/5 and 24/7 markets still work on weekends
    if weekday >= 5:
        # Crypto is 24/7
        if market_key == 'CRYPTO':
            return True
        # Forex/Commodities are 24/5 — closed on weekend
        if market_key in ('FOREX', 'COMMODITIES'):
            return False
        # All others closed on weekend
        return False

    # Handle GROUP keys (markets with lunch breaks: Tokyo, HK, China)
    if market_key in MARKET_GROUPS:
        session_keys = MARKET_GROUPS[market_key]
        for session_key in session_keys:
            session_info = MARKET_DEFINITIONS.get(session_key)
            if session_info and _check_time_slot(now.time(), session_info):
                return True
        return False  # neither session is open (lunch break or outside hours)

    market_info = MARKET_DEFINITIONS.get(market_key)

    if not market_info:
        # Fail-open: unknown market → assume open
        return True

    return _check_time_slot(now.time(), market_info)


def get_instrument_market_key(symbol: str, yf_symbol: str = '', category: str = '') -> str:
    """Public wrapper for _get_market_key — returns the resolved market key."""
    return _get_market_key(symbol, yf_symbol, category)



def _check_time_slot(current_time: time, definition: tuple) -> bool:
    """Check if current_time falls within a market's trading window.

    Handles both normal windows (start < end) and midnight-crossing windows.
    """
    start = definition[0]
    end = definition[1]
    crosses_midnight = len(definition) > 2 and definition[2]

    if crosses_midnight:
        # Market spans midnight: open when time >= start OR time < end
        return current_time >= start or current_time < end
    else:
        # Normal window: start <= time < end
        return start <= current_time < end


def get_market_status(symbol: str = '') -> str:
    """Returns human-readable market status string for a symbol."""
    if not symbol.strip():
        open_markets = []
        now = datetime.now(timezone.utc).time()
        for key, definition in MARKET_DEFINITIONS.items():
            if key == 'CRYPTO':
                continue
            if _check_time_slot(now, definition):
                open_markets.append(key)
        return ', '.join(open_markets) if open_markets else 'ALL CLOSED'

    market_key = _get_market_key(symbol)
    info = MARKET_DEFINITIONS.get(market_key, ('?', '?'))
    return f"{market_key} {info[0].strftime('%H:%M')}-{info[1].strftime('%H:%M')} UTC"


# ─── Quick Test ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    now_utc = datetime.now(timezone.utc)
    print(f"Aktuelle UTC-Zeit: {now_utc.strftime('%Y-%m-%d %H:%M:%S')} (Wochentag: {['Mo','Di','Mi','Do','Fr','Sa','So'][now_utc.weekday()]})")
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

    print(f"{'Symbol':<16} {'Markt':<14} {'Status':<12} {'Öffnungszeiten'}")
    print("-" * 70)
    for sym in test_symbols:
        key = _get_market_key(sym)
        open_ = is_market_open(sym)
        status = "🟢 OFFEN" if open_ else "🔴 ZU"
        info = MARKET_DEFINITIONS.get(key, ('?', '?'))
        hours = f"{info[0].strftime('%H:%M')}-{info[1].strftime('%H:%M')} UTC"
        print(f"{sym:<16} {key:<14} {status:<12} {hours}")

    print()
    print(f"Data Worker Gate (irgendwo offen?): {is_market_open()}")
    print(f"Offene Märkte: {get_market_status()}")
