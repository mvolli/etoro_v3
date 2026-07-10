#!/usr/bin/env python3
"""Signal computation — TA indicators for trading decisions.

Uses yfinance for price data and pandas-ta for indicators.
No DB, no API calls to eToro — pure computation, unit-testable.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta

import pandas as pd

logger = logging.getLogger(__name__)

# ─── Signal Types ─────────────────────────────────────────────────────────────

SIGNAL_BUY = "BUY"
SIGNAL_SELL = "SELL"
SIGNAL_HOLD = "HOLD"

CONVICTION_VERY_HIGH = "VERY_HIGH"
CONVICTION_HIGH = "HIGH"
CONVICTION_MEDIUM = "MEDIUM"
CONVICTION_LOW = "LOW"

CONVICTION_ORDER = [CONVICTION_VERY_HIGH, CONVICTION_HIGH, CONVICTION_MEDIUM, CONVICTION_LOW]

# ─── TA Parameters (Trading Bible V4) ────────────────────────────────────────

RSI_PERIOD = 14
RSI_OVERSOLD = 30
RSI_OVERBOUGHT = 70
RSI_EXTREME_OVERSOLD = 25

BB_PERIOD = 20
BB_STD = 2
BB_LOWER_EXTREME = 0.05   # BB %B below this = extreme oversold
BB_UPPER_EXTREME = 0.95   # BB %B above this = extreme overbought

MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL = 9

ATR_PERIOD = 14


# ─── Result dataclass ─────────────────────────────────────────────────────────

@dataclass
class SignalResult:
    symbol: str
    direction: str          # BUY | SELL | HOLD
    conviction: str         # VERY_HIGH | HIGH | MEDIUM | LOW
    score: float            # 0-100
    signal_types: list[str] = field(default_factory=list)
    rsi: float | None = None
    macd_hist: float | None = None
    bb_pct: float | None = None
    price: float | None = None
    atr: float | None = None
    generated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def is_actionable(self, min_conviction: str = CONVICTION_MEDIUM) -> bool:
        """True if conviction meets minimum threshold."""
        min_idx = CONVICTION_ORDER.index(min_conviction)
        our_idx = CONVICTION_ORDER.index(self.conviction)
        return self.direction != SIGNAL_HOLD and our_idx <= min_idx


# ─── Price Data Fetching ──────────────────────────────────────────────────────

def fetch_price_data(symbol: str, period: str = "3mo") -> pd.DataFrame | None:
    """Fetch OHLCV data from Yahoo Finance.

    Returns DataFrame with columns: Open, High, Low, Close, Volume
    Returns None on failure (network error, delisted, etc.)
    """
    try:
        import yfinance as yf
        ticker = yf.Ticker(symbol)
        df = ticker.history(period=period, auto_adjust=True)
        if df.empty or len(df) < 30:
            logger.warning(f"[signals] {symbol}: insufficient data ({len(df)} rows)")
            return None
        df.index = pd.to_datetime(df.index, utc=True)
        return df[["Open", "High", "Low", "Close", "Volume"]]
    except Exception as e:
        logger.warning(f"[signals] {symbol}: fetch failed — {e}")
        return None


def fetch_batch_price_data(
    symbols: list[str],
    period: str = "3mo",
) -> dict[str, pd.DataFrame]:
    """Batch fetch for multiple symbols — single yf.download() call."""
    try:
        import yfinance as yf
        raw = yf.download(
            symbols,
            period=period,
            auto_adjust=True,
            progress=False,
            threads=True,
        )
        if raw.empty:
            return {}

        result: dict[str, pd.DataFrame] = {}

        if len(symbols) == 1:
            sym = symbols[0]
            df = raw[["Open", "High", "Low", "Close", "Volume"]].dropna()
            if len(df) >= 30:
                result[sym] = df
        else:
            for sym in symbols:
                try:
                    df = raw.xs(sym, axis=1, level=1)[
                        ["Open", "High", "Low", "Close", "Volume"]
                    ].dropna()
                    if len(df) >= 30:
                        result[sym] = df
                except Exception:
                    pass

        logger.info(f"[signals] Batch fetched {len(result)}/{len(symbols)} symbols")
        return result
    except Exception as e:
        logger.error(f"[signals] Batch fetch failed: {e}")
        return {}


# ─── Indicator Computation ────────────────────────────────────────────────────

def compute_indicators(df: pd.DataFrame) -> dict:
    """Compute all TA indicators from OHLCV data.

    Uses `ta` library (Python 3.11 compatible, unlike pandas-ta which needs 3.12).
    Returns dict with: rsi, macd_hist, macd_hist_prev, bb_pct, atr, price, sma20, sma50
    """
    try:
        import ta as _ta
    except ImportError:
        logger.error("[signals] ta library not installed — run: uv pip install ta")
        return {}

    close = df["Close"]
    high  = df["High"]
    low   = df["Low"]

    indicators: dict = {}
    indicators["price"] = float(close.iloc[-1])

    # RSI
    try:
        rsi_s = _ta.momentum.RSIIndicator(close, window=RSI_PERIOD).rsi()
        if not rsi_s.empty and not pd.isna(rsi_s.iloc[-1]):
            indicators["rsi"] = float(rsi_s.iloc[-1])
    except Exception:
        pass

    # MACD histogram
    try:
        macd_obj = _ta.trend.MACD(close, window_slow=MACD_SLOW,
                                   window_fast=MACD_FAST, window_sign=MACD_SIGNAL)
        hist = macd_obj.macd_diff()
        if not hist.empty:
            indicators["macd_hist"] = float(hist.iloc[-1])
            if len(hist) >= 2:
                indicators["macd_hist_prev"] = float(hist.iloc[-2])
    except Exception:
        pass

    # Bollinger Bands %B
    try:
        bb_obj = _ta.volatility.BollingerBands(close, window=BB_PERIOD, window_dev=BB_STD)
        bb_pct = bb_obj.bollinger_pband()
        if not bb_pct.empty and not pd.isna(bb_pct.iloc[-1]):
            indicators["bb_pct"] = float(bb_pct.iloc[-1])
    except Exception:
        pass

    # ATR
    try:
        atr_s = _ta.volatility.AverageTrueRange(high, low, close,
                                                 window=ATR_PERIOD).average_true_range()
        if not atr_s.empty and not pd.isna(atr_s.iloc[-1]):
            indicators["atr"] = float(atr_s.iloc[-1])
    except Exception:
        pass

    # SMA20, SMA50
    try:
        sma20 = _ta.trend.SMAIndicator(close, window=20).sma_indicator()
        if not sma20.empty and not pd.isna(sma20.iloc[-1]):
            indicators["sma20"] = float(sma20.iloc[-1])
    except Exception:
        pass
    try:
        sma50 = _ta.trend.SMAIndicator(close, window=50).sma_indicator()
        if not sma50.empty and not pd.isna(sma50.iloc[-1]):
            indicators["sma50"] = float(sma50.iloc[-1])
    except Exception:
        pass

    # Volume Ratio (current vol vs. 20-day avg)
    # vol_ratio > 1.5 during a price decline = distribution (institutions selling)
    # vol_ratio < 1.5 = volume exhausted → potential bottom
    try:
        vol = df["Volume"]
        vol_avg20 = float(vol.rolling(20).mean().iloc[-1])
        if vol_avg20 > 0:
            indicators["vol_ratio"] = float(vol.iloc[-1] / vol_avg20)
    except Exception:
        pass

    return indicators


# ─── Signal Generation (Trading Bible V4 Rules) ───────────────────────────────

def generate_signal(symbol: str, indicators: dict) -> SignalResult:
    """Apply Trading Bible V4 signal rules to computed indicators.

    Rules (BUY):
    1. BB Lower + RSI < 30 + price > SMA50 + vol_ratio < 1.5 → VERY_HIGH
    2. BB %B < 0.05 + RSI < 30 + price > SMA50 + vol_ratio < 1.5 → VERY_HIGH
    3. RSI < 25 → HIGH (MEDIUM wenn price < SMA50 * 0.90, tief im Downtrend)
    4. MACD histogram increasing + below SMA20 → MEDIUM
    5. BB %B < 0.1 + MACD improving → MEDIUM-HIGH
    6. Trend pullback: above SMA50, near/below SMA20, RSI 35-55 → HIGH
    7. Golden Cross: SMA20 > SMA50 + MACD positive + RSI < 60 → HIGH  (fix: war invertiert)

    Rules (SELL):
    1. BB Upper + RSI > 70 → SELL / take profits
    2. Concentration exceeded → handled by risk gate, not here
    """
    rsi = indicators.get("rsi")
    macd_hist = indicators.get("macd_hist")
    macd_hist_prev = indicators.get("macd_hist_prev")
    bb_pct = indicators.get("bb_pct")
    price = indicators.get("price", 0.0)
    sma20 = indicators.get("sma20")
    sma50 = indicators.get("sma50")

    signals: list[tuple[str, str, float]] = []  # (type, conviction, score_contribution)

    # ── BUY Rules ───────────────────────────────────────────────────────────

    # Rule 1: BB Lower + RSI extreme — nur im Aufwärtstrend (price > sma50)
    # + Volume nicht in Distribution (vol_ratio < 1.5 = kein Ausverkaufs-Volumen)
    vol_ratio = indicators.get("vol_ratio", 1.0)
    if bb_pct is not None and rsi is not None and sma50 is not None:
        if (bb_pct < 0.1 and rsi < RSI_OVERSOLD
                and price > sma50           # Aufwärtstrend-Filter
                and vol_ratio < 1.5):       # kein Distributions-Volumen
            signals.append(("BB_LOWER_RSI_OVERSOLD", CONVICTION_VERY_HIGH, 35.0))

    # Rule 2: BB extreme + RSI extreme — nur im Aufwärtstrend
    if bb_pct is not None and rsi is not None and sma50 is not None:
        if (bb_pct < BB_LOWER_EXTREME and rsi < RSI_OVERSOLD
                and price > sma50           # Aufwärtstrend-Filter
                and vol_ratio < 1.5):       # kein Distributions-Volumen
            signals.append(("BB_EXTREME_RSI_OVERSOLD", CONVICTION_VERY_HIGH, 40.0))

    # Rule 3: RSI extreme oversold — Conviction hängt vom Trend ab.
    # Tief im Downtrend (price < sma50 * 0.90) = MEDIUM (Vorsicht: weitere Verluste möglich)
    # Nahe oder über SMA50 = HIGH (kurzfristige Übertreibung, Erholung wahrscheinlicher)
    if rsi is not None and rsi < RSI_EXTREME_OVERSOLD:
        if sma50 is not None and price < sma50 * 0.90:
            signals.append(("RSI_EXTREME_OVERSOLD", CONVICTION_MEDIUM, 15.0))
        else:
            signals.append(("RSI_EXTREME_OVERSOLD", CONVICTION_HIGH, 25.0))

    # Rule 4: MACD turning + below SMA20
    if macd_hist is not None and macd_hist_prev is not None:
        if macd_hist > macd_hist_prev and macd_hist < 0:  # improving from negative
            if sma20 is not None and price < sma20:
                signals.append(("MACD_TURN_BELOW_SMA20", CONVICTION_MEDIUM, 15.0))

    # Rule 5: BB low + MACD improving
    if bb_pct is not None and macd_hist is not None and macd_hist_prev is not None:
        if bb_pct < 0.1 and macd_hist > macd_hist_prev:
            signals.append(("BB_LOW_MACD_IMPROVING", CONVICTION_HIGH, 20.0))

    # Rule 6: Trend pullback — MACD-Histogramm Floor (fix/trend-pullback-macd-floor)
    # MACD muss > -0.01 sein → filtert starke Downtrends heraus.
    # Vorher: 63.6% Fail-Rate (28/44), weil TREND_PULLBACK auch bei stark
    # negativem MACD feuerte → Preis unter SMA50, aber Signal ignorierte Trendkraft.
    if all(x is not None for x in [rsi, sma20, sma50, price, macd_hist]):
        if (price > sma50 and price <= sma20 * 1.02  # near/below SMA20
                and 35 <= rsi <= 55
                and macd_hist > -0.01):  # MACD Floor: nicht im starken Abwärtstrend
            signals.append(("TREND_PULLBACK", CONVICTION_HIGH, 20.0))

    # Rule 7: Golden Cross — schnellerer MA (SMA20) über langsamerem MA (SMA50).
    # fix/golden-cross-direction: war sma50 > sma20 (= Death-Cross-Struktur, BEARISH).
    # Echter Golden Cross = SMA20 > SMA50 (kurze MA hat lange MA überholt → BULLISH).
    if all(x is not None for x in [sma20, sma50, macd_hist, rsi]):
        if sma20 > sma50 and macd_hist > 0 and rsi < 60:
            signals.append(("GOLDEN_CROSS", CONVICTION_HIGH, 18.0))

    # ── SELL Rules ──────────────────────────────────────────────────────────

    if bb_pct is not None and rsi is not None:
        if bb_pct > BB_UPPER_EXTREME and rsi > RSI_OVERBOUGHT:
            # Sell signal — take profits
            return SignalResult(
                symbol=symbol,
                direction=SIGNAL_SELL,
                conviction=CONVICTION_HIGH,
                score=30.0,
                signal_types=["BB_UPPER_RSI_OVERBOUGHT"],
                rsi=rsi,
                macd_hist=macd_hist,
                bb_pct=bb_pct,
                price=price,
                atr=indicators.get("atr"),
            )

    # ── Aggregate BUY signals ───────────────────────────────────────────────

    if not signals:
        return SignalResult(
            symbol=symbol,
            direction=SIGNAL_HOLD,
            conviction=CONVICTION_LOW,
            score=0.0,
            rsi=rsi,
            macd_hist=macd_hist,
            bb_pct=bb_pct,
            price=price,
            atr=indicators.get("atr"),
        )

    # Best conviction wins, score is cumulative (capped at 100)
    conviction_order = {CONVICTION_VERY_HIGH: 0, CONVICTION_HIGH: 1,
                        CONVICTION_MEDIUM: 2, CONVICTION_LOW: 3}
    best_conviction = min(signals, key=lambda s: conviction_order[s[1]])[1]
    total_score = min(sum(s[2] for s in signals), 100.0)
    signal_types = [s[0] for s in signals]

    return SignalResult(
        symbol=symbol,
        direction=SIGNAL_BUY,
        conviction=best_conviction,
        score=total_score,
        signal_types=signal_types,
        rsi=rsi,
        macd_hist=macd_hist,
        bb_pct=bb_pct,
        price=price,
        atr=indicators.get("atr"),
    )


def analyze_symbol(symbol: str) -> SignalResult | None:
    """Full pipeline: fetch → compute → signal for one symbol."""
    df = fetch_price_data(symbol)
    if df is None:
        return None
    indicators = compute_indicators(df)
    if not indicators:
        return None
    return generate_signal(symbol, indicators)


def analyze_batch(symbols: list[str]) -> dict[str, SignalResult]:
    """Batch analysis for multiple symbols."""
    price_data = fetch_batch_price_data(symbols)
    results: dict[str, SignalResult] = {}
    for sym, df in price_data.items():
        indicators = compute_indicators(df)
        if indicators:
            results[sym] = generate_signal(sym, indicators)
    return results

