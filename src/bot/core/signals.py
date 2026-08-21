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

from bot.core.corporate_actions import (
    is_corporate_action_artifact,
    scan_price_gaps,
)

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

GOLDEN_CROSS_LOOKBACK_DAYS = 2   # GOLDEN_CROSS nur, wenn SMA20 die SMA50 innerhalb
                                 # der letzten N Bars von unten gekreuzt hat (Ereignis,
                                 # nicht Dauerzustand — fix/golden-cross-event)


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

    # SMA-Werte vor GOLDEN_CROSS_LOOKBACK_DAYS Bars — Cross-Detection
    # (fix/golden-cross-event: Kreuzung als Ereignis erkennen, nicht Zustand)
    try:
        lb = GOLDEN_CROSS_LOOKBACK_DAYS
        if len(sma20) > lb and len(sma50) > lb:
            v20, v50 = sma20.iloc[-(lb + 1)], sma50.iloc[-(lb + 1)]
            if not pd.isna(v20) and not pd.isna(v50):
                indicators["sma20_lookback"] = float(v20)
                indicators["sma50_lookback"] = float(v50)
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

    # Falling-Knife-Metriken (feat/falling-knife-gate 2026-07-26): der Fall
    # selbst wurde nie gemessen — nur kategorisch per Signaltyp abgewehrt
    # (MACD-Pflicht). Diese Kennzahlen quantifizieren ihn:
    #   consecutive_down_days — rote Tagesschlusskurse in Folge
    #   roc_5d_pct            — 5-Tages-Rate-of-Change in %
    try:
        closes = df["Close"].dropna()
        if len(closes) >= 6:
            down = 0
            for i in range(len(closes) - 1, 0, -1):
                if float(closes.iloc[i]) < float(closes.iloc[i - 1]):
                    down += 1
                else:
                    break
            indicators["consecutive_down_days"] = down
            indicators["roc_5d_pct"] = (
                float(closes.iloc[-1]) / float(closes.iloc[-6]) - 1.0
            ) * 100.0
    except Exception:
        pass

    # Corporate-Action-Metriken (feat/corporate-action-guard 2026-08-17):
    # groesster unerklaerter Kurssprung im SMA50-Fenster. Leeres Dict = kein
    # Verdacht — die Keys tauchen nur auf, wenn wirklich ein Sprung drin ist.
    indicators.update(scan_price_gaps(df))

    return indicators


# ─── Falling-Knife-Gate (feat/falling-knife-gate 2026-07-26) ─────────────────
# Quantitative Messer-Erkennung: blockt die Dip-Buy-Regeln (1, 2, 3, 5), wenn
# der Fall selbst zu steil ist — unabhaengig davon, wie "guenstig" RSI/BB
# aussehen. TREND_PULLBACK/GOLDEN_CROSS brauchen strukturell einen Aufwaerts-
# trend und sind nicht betroffen. Scorecard-Basis: "Tiefer RSI ist KEIN
# Kaufargument, sondern ein Krisenzeichen (RSI<25: WR 9%)".

KNIFE_GATE_ENABLED = True
KNIFE_MAX_CONSECUTIVE_DOWN = 4    # >= 4 rote Tage in Folge = Messer
KNIFE_MAX_ROC_5D_PCT = -12.0      # <= -12% in 5 Tagen = Messer
KNIFE_MAX_ATR_BELOW_SMA20 = 2.5   # Preis >= 2.5 ATR unter SMA20 = Messer


def is_falling_knife(indicators: dict) -> tuple[bool, str]:
    """Prueft die Knife-Metriken. Gibt (is_knife, reason) zurueck.

    Fehlende Metriken zaehlen nicht als Messer (fail-open pro Kriterium) —
    das Gate soll steile Faelle blocken, nicht Datenluecken bestrafen.
    """
    if not KNIFE_GATE_ENABLED:
        return False, ""
    down = indicators.get("consecutive_down_days")
    if down is not None and down >= KNIFE_MAX_CONSECUTIVE_DOWN:
        return True, f"{down} rote Tage in Folge"
    roc = indicators.get("roc_5d_pct")
    if roc is not None and roc <= KNIFE_MAX_ROC_5D_PCT:
        return True, f"ROC 5d {roc:.1f}%"
    atr = indicators.get("atr")
    sma20 = indicators.get("sma20")
    price = indicators.get("price")
    if atr and sma20 and price and atr > 0 and price < sma20:
        dist_atr = (sma20 - price) / atr
        if dist_atr >= KNIFE_MAX_ATR_BELOW_SMA20:
            return True, f"{dist_atr:.1f} ATR unter SMA20"
    return False, ""


# ─── Signal Generation (Trading Bible V4 Rules) ───────────────────────────────

def generate_signal(symbol: str, indicators: dict) -> SignalResult:
    """Apply Trading Bible V4 signal rules to computed indicators.

    Rules (BUY):
    1. BB Lower + RSI < 30 + price > SMA50 + vol_ratio < 1.5 → VERY_HIGH
    2. BB %B < 0.05 + RSI < 30 + price > SMA50 + vol_ratio < 1.5 → HIGH
    3. RSI < 25 → HIGH (MEDIUM wenn price < SMA50 * 0.90, tief im Downtrend)
    4. MACD histogram increasing + below SMA20 → MEDIUM
    5. BB %B < 0.1 + MACD improving → MEDIUM-HIGH
    6. Trend pullback: above SMA50, near/below SMA20, RSI 35-55 → HIGH
    7. Golden Cross: SMA20 kreuzt SMA50 binnen 2 Bars + MACD positiv + RSI < 60 → HIGH

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

    # Falling-Knife-Gate: bei steilem Fall sind die Dip-Buy-Regeln (1, 2, 3, 5)
    # gesperrt — Rule 4 (MACD_TURN) bleibt erlaubt, weil die MACD-Wende genau
    # die Bestaetigung ist, auf die das System wartet (WR 32% vs. 8%).
    knife, _knife_reason = is_falling_knife(indicators)

    # Rule 1: BB Lower + RSI extreme — nur im Aufwärtstrend (price > sma50)
    # + Volume nicht in Distribution (vol_ratio < 1.5 = kein Ausverkaufs-Volumen)
    vol_ratio = indicators.get("vol_ratio", 1.0)
    if bb_pct is not None and rsi is not None and sma50 is not None and not knife:
        if (bb_pct < 0.1 and rsi < RSI_OVERSOLD
                and price > sma50           # Aufwärtstrend-Filter
                and vol_ratio < 1.5):       # kein Distributions-Volumen
            signals.append(("BB_LOWER_RSI_OVERSOLD", CONVICTION_VERY_HIGH, 35.0))

    # Rule 2: BB extreme + RSI extreme — nur im Aufwärtstrend
    if bb_pct is not None and rsi is not None and sma50 is not None and not knife:
        if (bb_pct < BB_LOWER_EXTREME and rsi < RSI_OVERSOLD
                and price > sma50           # Aufwärtstrend-Filter
                and vol_ratio < 1.5):       # kein Distributions-Volumen
            signals.append(("BB_EXTREME_RSI_OVERSOLD", CONVICTION_HIGH, 25.0))

    # Rule 3: RSI extreme oversold — Conviction hängt vom Trend ab.
    # Tief im Downtrend (price < sma50 * 0.90) = MEDIUM (Vorsicht: weitere Verluste möglich)
    # Nahe oder über SMA50 = HIGH (kurzfristige Übertreibung, Erholung wahrscheinlicher)
    # feat/falling-knife-gate: Distributions-Volumen (vol_ratio >= 1.5) drueckt
    # jetzt auch hier auf MEDIUM — Rule 3 hatte als einzige Dip-Regel weder
    # Trend- noch Volumenfilter.
    if rsi is not None and rsi < RSI_EXTREME_OVERSOLD and not knife:
        deep_downtrend = sma50 is not None and price < sma50 * 0.90
        distribution = vol_ratio >= 1.5
        if deep_downtrend or distribution:
            signals.append(("RSI_EXTREME_OVERSOLD", CONVICTION_MEDIUM, 15.0))
        else:
            signals.append(("RSI_EXTREME_OVERSOLD", CONVICTION_HIGH, 25.0))

    # Rule 4: MACD turning + below SMA20
    if macd_hist is not None and macd_hist_prev is not None:
        if macd_hist > macd_hist_prev and macd_hist < 0:  # improving from negative
            if sma20 is not None and price < sma20:
                signals.append(("MACD_TURN_BELOW_SMA20", CONVICTION_MEDIUM, 15.0))

    # Rule 5: BB low + MACD improving
    if bb_pct is not None and macd_hist is not None and macd_hist_prev is not None and not knife:
        if bb_pct < 0.1 and macd_hist > macd_hist_prev:
            signals.append(("BB_LOW_MACD_IMPROVING", CONVICTION_HIGH, 20.0))

    # Rule 6: Trend pullback — MACD-Histogramm Floor (fix/trend-pullback-macd-floor)
    # MACD muss > -0.005 sein (verschärft von -0.01) → filtert stärkere Downtrends heraus.
    # Vorher: 63.6% Fail-Rate (28/44), weil TREND_PULLBACK auch bei stark
    # negativem MACD feuerte → Preis unter SMA50, aber Signal ignorierte Trendkraft.
    # fix/tp-conviction-calibration (2026-08-21): Einzel-Komponente TREND_PULLBACK
    # lief mit HIGH Conviction durchweg rot (non-CORE_SWEEP: HIGH-Conviction
    # WR 28% / -268 USD; das TREND_PULLBACK-Cluster 40 Verluste avg -2.36%
    # gegen 17 Gewinne avg +0.11% — WR ~32% mit Payoff ~0.05, d.h. fast jede
    # Gewinnerholung ging an den SL). Der EDGE lebt in der MACD_TURN-Combo
    # (WR 50%, avg +5%) — die behält ihre eigene MEDIUM-Conviction, und per
    # fix/combo-conviction-min kann kein Component die Combo mehr nach oben
    # ziehen. Einzel-Pullback ohne MACD-Wende ist eine ZIEHUNG, keine
    # Bestätigung → MEDIUM (Score 15 statt 20 = kleinere Position).
    if all(x is not None for x in [rsi, sma20, sma50, price, macd_hist]):
        # fix/macd-floor-scale: das MACD-Histogramm hat Preiseinheiten — der
        # absolute Floor -0.005 war bei einem $100-Titel -0.005% vom Preis,
        # bei BTC (~$100k) praktisch 0 und bei Penny-Stocks wirkungslos.
        # -0.00005 * price entspricht der alten Kalibrierung bei $100.
        _macd_floor = -0.00005 * price if price else -0.005
        if (price > sma50 and price <= sma20 * 1.02  # near/below SMA20
                and 35 <= rsi <= 55
                and macd_hist > _macd_floor and rsi > 30):  # RSI>30: keine Entries im Downtrend
            signals.append(("TREND_PULLBACK", CONVICTION_MEDIUM, 15.0))

    # Rule 7: Golden Cross — schnellerer MA (SMA20) über langsamerem MA (SMA50).
    # fix/golden-cross-direction: war sma50 > sma20 (= Death-Cross-Struktur, BEARISH).
    # Echter Golden Cross = SMA20 > SMA50 (kurze MA hat lange MA überholt → BULLISH).
    # fix/golden-cross-event: der reine Zustand sma20 > sma50 ist in jedem
    # stabilen Aufwaertstrend dauerhaft wahr — feuerte jeden Zyklus neu
    # (~1500 Signale/Tag am 2026-07-13) und ist kein Entry-Timing-Signal.
    # Jetzt: Kreuzung muss innerhalb GOLDEN_CROSS_LOOKBACK_DAYS passiert sein
    # (vor N Bars war SMA20 <= SMA50, jetzt SMA20 > SMA50). Fehlt die Historie,
    # feuert das Signal NICHT (fail-closed).
    sma20_lb = indicators.get("sma20_lookback")
    sma50_lb = indicators.get("sma50_lookback")
    if all(x is not None for x in [sma20, sma50, macd_hist, rsi]):
        crossed_recently = (
            sma20_lb is not None and sma50_lb is not None and sma20_lb <= sma50_lb
        )
        if sma20 > sma50 and crossed_recently and macd_hist > 0 and rsi < 60:
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

    # ── Corporate-Action-Gate (feat/corporate-action-guard 2026-08-17) ──────
    # Steht hier bewusst NACH den SELL-Regeln (die returnen oben schon) und
    # VOR der BUY-Aggregation: ein Split-/Sonderdividenden-Artefakt in der
    # Yahoo-Reihe verfaelscht RSI, MACD, BB, ATR und beide SMAs gleichzeitig,
    # also taugt KEINE der sieben BUY-Regeln mehr — anders als beim Knife-Gate,
    # wo Rule 4 ueberlebt. Verworfene BUYs fallen in den HOLD-Zweig darunter.
    # SELL und alle Exit-Pfade bleiben unberuehrt: Risiko abbauen darf ein
    # Datenverdacht nie blockieren.
    if signals:
        _ca_artifact, _ca_reason = is_corporate_action_artifact(indicators)
        if _ca_artifact:
            logger.warning(
                "[signals] %s: BUY unterdrueckt — Corporate-Action-Verdacht (%s)",
                symbol, _ca_reason,
            )
            signals = []

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

    # fix/combo-conviction-min (2026-07-26): Combos erben jetzt die
    # SCHWAECHSTE Komponenten-Conviction (vorher: die beste). Eine einzige
    # VERY_HIGH-Komponente machte die ganze Combo VERY_HIGH und damit zur
    # groessten Position (sizing 7%) — waehrend genau diese Combos laut
    # Scorecard 80-90% Verlustrate hatten (BB_LOWER+BB_EXTREME+RSI_EXTREME:
    # n=37, WR 2.7%). Einzelsignale sind unveraendert (min = max = eigene).
    # Score bleibt kumulativ (capped at 100) — die Combo gewinnt weiterhin
    # das Ranking, sie bekommt nur nicht mehr automatisch die Maximal-Size.
    conviction_order = {CONVICTION_VERY_HIGH: 0, CONVICTION_HIGH: 1,
                        CONVICTION_MEDIUM: 2, CONVICTION_LOW: 3}
    combo_conviction = max(signals, key=lambda s: conviction_order[s[1]])[1]
    total_score = min(sum(s[2] for s in signals), 100.0)
    signal_types = [s[0] for s in signals]

    return SignalResult(
        symbol=symbol,
        direction=SIGNAL_BUY,
        conviction=combo_conviction,
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

