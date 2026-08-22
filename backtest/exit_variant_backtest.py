#!/usr/bin/env python3
"""Exit-Variant Backtest — etoro_v3

Universe-Definition + yfinance OHLCV (2025-01-01 → today) + Signal-Erzeugung
(Dip / Momentum / MACD-Turn) + 4 Exit-Varianten (V0..V3) simulated in parallel.

Variants (factorial: 3-level SL-Grid × Early-Lock × Chandelier-Trail):
  V0  SL-Grid            — fixed % stop from entry (Bible default 3%), 20-bar time stop
  V1  SL-Grid × 3        — ATR-widened SL (1.5×ATR, clamp 3..6% = live sl.atr_adaptive)
  V2  SL-Grid + Early-Lock — Break-Even floor at +3% peak (Bible Rule 9: BE → +0.3% floor)
  V3  V2 + Chandelier-Trail — Chandelier Exit 3×ATR(14) from highest close, arms at +3% peak

Commons:
  * Entry at NEXT bar open after signal bar (no lookahead; indicators use data ≤ t)
  * Intrabar causality: arm checks (Early-Lock peak, Chandelier trail) use data
    through bar i-1 only — a bar's own high cannot arm a stop its own low hits
  * One open trade per symbol AND per signal type (3 independent lanes; capital
    per lane is unconstrained — a capacity model would be needed for that)
  * Full closes only (no partials, no broker TP), $1000 notional per trade
  * Intra-bar fills: Low ≤ stop → fill at min(Open, stop) (gap-aware, conservative)
  * Time stop: 20 bars (Bible stale-exit max) → close at Close
  * Costs: --cost-pct X deducts a round-trip fee (X% of notional) from each trade;
    metrics are reported NET, gross_pnl_usd kept per trade for the gross/net table
  * MDD/Sharpe on the STRATEGY-PnL curve (realized + unrealized, calendar
    days), NOT the leveraged book: MDD in $ and % of peak equity; Sharpe =
    daily PnL returns normalized by AVERAGE BOUND CAPITAL (return on capital
    deployed), annualized ×√252. (Fix 2026-08-22: old version measured the
    ~21×$1k long book against a 10k base → −84% MDD on a winning run and
    Sharpe 2.8 on losing variants.)
  * Benchmark: equal-weight buy&hold of the universe + SPY over the sim window
  * Falling-Knife-Gate blocks DIP signals (≥4 down days / ROC5d ≤ -12% / ≥N ATR
    below SMA20; N = --knife-atr, live default 2.5)
  * Data: yfinance auto_adjust=True (splits/dividends adjusted), disk-cached CSVs

Usage:
  cd etoro_v3 && /usr/bin/python3 backtest/exit_variant_backtest.py
      [--study main] [--start 2025-01-01] [--end ...] [--sim-start ...]
      [--cost-pct 0.3] [--knife-atr 2.5] [--workers 4] [--limit N]
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sqlite3
import sys
import time
from dataclasses import dataclass, field
from multiprocessing import Pool, cpu_count
from pathlib import Path

import numpy as np
import pandas as pd

import yfinance as yf
import ta

ROOT = Path(__file__).resolve().parents[1]
CACHE_DIR = ROOT / "data" / "backtest_cache"
START = "2025-01-01"
END = None  # today

# ──────────────────────────────────────────────────────────────────────────────
# 1. UNIVERSE-DEFINITION
# ──────────────────────────────────────────────────────────────────────────────
# Curated liquid multi-asset universe (yfinance namespace). ETFs used as
# regional proxies (Asia/Europe) so the backtest covers the same asset classes
# the live bot trades.

UNIVERSE: dict[str, list[str]] = {
    "US_large_cap": ["AAPL", "MSFT", "NVDA", "GOOGL", "AMZN", "META", "TSLA",
                     "AVGO", "AMD", "NFLX", "UBER", "ABNB", "SHOP", "PLTR", "MELI"],
    "US_etf":       ["SPY", "QQQ", "IWM", "VOO", "GLD", "TLT", "XLE", "SMH"],
    "US_value":     ["JPM", "V", "PG", "KO", "JNJ", "WMT", "UNH", "XOM", "LLY"],
    "crypto":       ["BTC-USD", "ETH-USD"],
    "asia_proxy":   ["TOPT", "FXI", "KWEB", "EWJ", "FXA"],
    "europe_proxy": ["EAF"],
}
UNIVERSE_SYMBOLS: list[str] = [s for v in UNIVERSE.values() for s in v]
SYMBOL_GROUPS: dict[str, str] = {s: g for g, ss in UNIVERSE.items() for s in ss}

# ──────────────────────────────────────────────────────────────────────────────
# 2. CONFIG
# ──────────────────────────────────────────────────────────────────────────────
TRADE_NOTIONAL = 1000.0      # $ per trade
INITIAL_EQUITY = 10_000.0
MAX_HOLD_BARS = 20           # stale-exit horizon (Bible: max 20d)
RSI_PERIOD, BB_PERIOD, ATR_PERIOD = 14, 20, 14
MACD_FAST, MACD_SLOW, MACD_SIGN = 12, 26, 9
WARMUP = 60                  # min bars before signals may fire

# Signal thresholds (from bot/core/signals.py — Trading Bible V5)
RSI_OVERSOLD = 30.0
BB_LOWER_EXTREME = 0.05
KNIFE_MAX_DOWN_DAYS = 4
KNIFE_MAX_ROC5D_PCT = -12.0
KNIFE_MAX_ATR_BELOW_SMA20 = 2.5

# Exit-variant parameters
SL_GRID_PCT = 3.0            # V0 fixed SL (Bible default)
BE_ARM_PCT = 3.0             # Early-Lock arms at +3% peak (Bible Rule 9)
BE_FLOOR_PCT = 0.3           # BE floor: +0.3% (Bible: BREAK_EVEN_FLOOR_PCT)
CHANDELIER_MULT = 3.0        # Chandelier: 3×ATR from highest close
CHANDELIER_ARM_PCT = 3.0     # Chandelier only active once peak ≥ +3%


@dataclass(frozen=True)
class Variant:
    key: str
    name: str
    sl_pct: float            # fixed % stop distance
    atr_sl: bool             # SL = clamp(1.5*ATR%, 3, 6)
    early_lock: bool
    chandelier: bool


VARIANTS: list[Variant] = [
    Variant("V0", "SL-Grid",            3.0, False, False, False),
    Variant("V1", "SL-Grid×3 (ATR)",    3.0, True,  False, False),
    Variant("V2", "Early-Lock",         3.0, False, True,  False),
    Variant("V3", "Early-Lock+Chandelier", 3.0, False, True, True),
]

# ──────────────────────────────────────────────────────────────────────────────
# 3. DATA LOADING (yfinance, batched + disk-cached)
# ──────────────────────────────────────────────────────────────────────────────

# Study presets: (data start, sim window start, sim window end, out tag)
#   main = 2025-01 → today (primary study)
#   bear = 2022-01 → 2023-12 (bear-cycle stress; data from 2021-06 for warmup)
STUDIES: dict[str, dict] = {
    "main": {"start": "2025-01-01", "sim_start": None,  "end": None},
    "bear": {"start": "2021-06-01", "sim_start": "2022-01-01", "end": "2024-01-01"},
}


def _cache_path(sym: str, start: str, end: str | None) -> Path:
    safe = sym.replace("^", "x").replace("-", "_").replace("=", "")
    tag = end if end else "today"
    return CACHE_DIR / f"{safe}_{start}_{tag}.csv"


def load_ohlcv(symbols: list[str], start: str, end: str | None = None) -> dict[str, pd.DataFrame]:
    """Load OHLCV [start, end] with per-symbol + per-range disk cache."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    result: dict[str, pd.DataFrame] = {}
    missing: list[str] = []
    for sym in symbols:
        p = _cache_path(sym, start, end)
        if p.exists():
            try:
                df = pd.read_csv(p, parse_dates=["Date"], index_col="Date")
                if len(df) >= 100:
                    result[sym] = df
                    continue
            except Exception:
                pass
        missing.append(sym)

    for i in range(0, len(missing), 25):
        batch = missing[i:i + 25]
        try:
            raw = yf.download(batch, start=start, end=end, auto_adjust=True,
                              progress=False, threads=True)
        except Exception as e:
            print(f"  batch {batch[:3]}.. failed: {e}", file=sys.stderr)
            continue
        if raw.empty:
            continue
        if len(batch) == 1:
            cols = {batch[0]: raw}
        else:
            cols = {}
            for sym in batch:
                try:
                    df = raw.xs(sym, axis=1, level=1)
                    if not df.empty:
                        cols[sym] = df
                except Exception:
                    pass
        for sym, df in cols.items():
            if df.empty:
                continue
            df = df.dropna(subset=["Close"])
            if len(df) < 100:
                continue
            df = df[["Open", "High", "Low", "Close", "Volume"]].copy()
            df.index.name = "Date"
            try:
                df.to_csv(_cache_path(sym, start, end))
            except Exception:
                pass
            result[sym] = df

    return result


# ──────────────────────────────────────────────────────────────────────────────
# 4. INDICATORS + SIGNALS (vectorized, no lookahead)
# ──────────────────────────────────────────────────────────────────────────────

def compute_arrays(df: pd.DataFrame) -> dict[str, np.ndarray]:
    c, h, l = df["Close"].to_numpy(float), df["High"].to_numpy(float), df["Low"].to_numpy(float)
    v = df["Volume"].to_numpy(float)

    rsi = ta.momentum.RSIIndicator(pd.Series(c), window=RSI_PERIOD).rsi().to_numpy()
    macd = ta.trend.MACD(pd.Series(c), window_slow=MACD_SLOW, window_fast=MACD_FAST,
                         window_sign=MACD_SIGN)
    hist = macd.macd_diff().to_numpy()
    bb = ta.volatility.BollingerBands(pd.Series(c), window=BB_PERIOD,
                                      window_dev=2).bollinger_pband().to_numpy()
    atr = ta.volatility.AverageTrueRange(pd.Series(h), pd.Series(l), pd.Series(c),
                                         window=ATR_PERIOD).average_true_range().to_numpy()
    sma20 = pd.Series(c).rolling(20).mean().to_numpy()
    sma50 = pd.Series(c).rolling(50).mean().to_numpy()
    vol_avg20 = pd.Series(v).rolling(20).mean().to_numpy()

    # Falling-knife metrics
    down_days = np.zeros(len(c), dtype=int)
    for i in range(1, len(c)):
        down_days[i] = down_days[i - 1] + 1 if c[i] < c[i - 1] else 0
    roc5 = np.full(len(c), np.nan)
    if len(c) > 5:
        roc5[5:] = (c[5:] / c[:-5] - 1.0) * 100.0

    return {"open": df["Open"].to_numpy(float),
            "high": h, "low": l, "close": c,
            "rsi": rsi, "hist": hist, "bb": bb, "atr": atr,
            "sma20": sma20, "sma50": sma50,
            "vol_ratio": np.where(vol_avg20 > 0, v / np.where(vol_avg20 > 0, vol_avg20, np.nan), np.nan),
            "down_days": down_days, "roc5": roc5}


def signal_mask(arr: dict[str, np.ndarray], knife_atr: float) -> dict[str, np.ndarray]:
    """Per-bar boolean masks: DIP / MOMENTUM / MACD_TURN (indicators ≤ bar t).

    knife_atr: Falling-Knife-Gate ATR criterion (live default 2.5; higher =
    looser gate = more DIP trades allowed through).
    """
    c, rsi = arr["close"], arr["rsi"]
    hist, bb, atr = arr["hist"], arr["bb"], arr["atr"]
    sma20, sma50 = arr["sma20"], arr["sma50"]
    vr = arr["vol_ratio"]
    n = len(c)

    atr_dist = np.full(n, np.nan)
    m = (sma20 > 0) & (atr > 0) & (c < sma20)
    with np.errstate(invalid="ignore", divide="ignore"):
        atr_dist = np.where(m, (sma20 - c) / atr, np.nan)
    # falling-knife: each criterion fail-open (NaN = no evidence = not a knife)
    knife = (
        (arr["down_days"] >= KNIFE_MAX_DOWN_DAYS)
        | (arr["roc5"] <= KNIFE_MAX_ROC5D_PCT)
        | (atr_dist >= knife_atr)
    )

    def ok(*masks) -> np.ndarray:
        out = np.ones(n, dtype=bool)
        for mm in masks:
            out &= mm
        return out

    # DIP (faithful to live signals.py dip rules, OR'd):
    #   R1: BB %B < 0.1 AND RSI < 30 AND price > SMA50 AND vol_ratio < 1.5 AND not knife
    #   R3: RSI < 25 AND not knife  (no trend/vol filter; conviction varies in live)
    rsi_ok = rsi < RSI_OVERSOLD
    trend_ok = c > sma50
    vol_ok = ~(vr >= 1.5)  # NaN → not distribution → ok
    r1 = ok(bb < 0.1, rsi_ok, trend_ok, vol_ok)
    r3 = rsi < 25
    dip = (r1 | r3) & ~knife

    macd_floor = -0.00005 * c
    pullback = ok(c > sma50, c <= sma20 * 1.02, (rsi >= 35) & (rsi <= 55),
                  hist > macd_floor, rsi > 30, sma20 > 0)
    # Golden cross event: SMA20 crossed above SMA50 within last lb bars
    lb = 2
    crossed = np.zeros(n, dtype=bool)
    if n > 1:
        crossed[1:] = (sma20[1:] > sma50[1:]) & (sma20[:-1] <= sma50[:-1])
    golden_win = crossed.copy()
    for j in range(1, lb + 1):
        shifted = np.zeros(n, dtype=bool)
        shifted[j:] = crossed[:n - j]
        golden_win |= shifted
    golden = ok(golden_win, hist > 0, rsi < 60, sma50 > 0)
    momentum = pullback | golden

    macd_turn = ok(hist > np.roll(hist, 1), hist < 0, c < sma20, sma20 > 0)
    macd_turn[0] = False

    # warmup
    warm = np.zeros(n, dtype=bool); warm[WARMUP:] = True
    return {"DIP": dip & warm, "MOMENTUM": momentum & warm, "MACD_TURN": macd_turn & warm}


# ──────────────────────────────────────────────────────────────────────────────
# 5. BACKTEST ENGINE (one open trade per symbol, next-open entry)
# ──────────────────────────────────────────────────────────────────────────────

def simulate_symbol(sym: str, df: pd.DataFrame, variant: Variant,
                    knife_atr: float = KNIFE_MAX_ATR_BELOW_SMA20,
                    cost_pct: float = 0.0,
                    min_entry: str | None = None) -> list[dict]:
    arr = compute_arrays(df)
    masks = signal_mask(arr, knife_atr)
    o, h, l, c = arr["open"], arr["high"], arr["low"], arr["close"]
    atr, sma20 = arr["atr"], arr["sma20"]
    n = len(c)
    dates = df.index
    # round-trip cost (entry + exit) in % of notional, applied at trade level
    cost_usd = TRADE_NOTIONAL * cost_pct / 100.0
    date_strs = dates.strftime("%Y-%m-%d")

    out: list[dict] = []
    for sig_name, mask in masks.items():
        in_trade = False
        entry_i = -1
        for t in range(n):
            if in_trade:
                # --- manage open trade (bars after entry) ---
                if t <= entry_i:
                    continue
                i = t
                entry_price = o[entry_i]
                sl_price = entry_price * (1 - (entry_sl_pct / 100.0)) if entry_sl_pct else None
                # INTRABAR CAUSALITY: arming state may only use bars ≤ i-1.
                # A bar's own high must not arm a stop that the same bar's
                # low then trips (intraday path order is unknowable → assume
                # worst: high-after-low, so arming needs the previous bar).
                peak_high = max(h[entry_i + 1: i]) if i > entry_i + 1 else h[entry_i]
                peak_pct = (peak_high - entry_price) / entry_price * 100.0

                exit_price = None
                exit_reason = None
                # 1) fixed/ATR stop
                if sl_price is not None and l[i] <= sl_price:
                    exit_price = min(o[i], sl_price)
                    exit_reason = "SL"
                # 2) early lock (BE floor) — not checkable on the entry bar itself
                if variant.early_lock and peak_pct >= BE_ARM_PCT:
                    floor = entry_price * (1 + BE_FLOOR_PCT / 100.0)
                    if l[i] <= floor:
                        p = min(o[i], floor)
                        if exit_price is None or p < exit_price:
                            exit_price, exit_reason = p, "BE_LOCK"
                # 3) chandelier trail
                if (variant.chandelier and peak_pct >= CHANDELIER_ARM_PCT
                        and atr[i - 1] == atr[i - 1]):
                    # ATR and highest close through bar i-1 (causal)
                    hc = max(c[entry_i + 1: i]) if i > entry_i + 1 else c[entry_i]
                    stop = hc - CHANDELIER_MULT * atr[i - 1]
                    # trail must never undercut entry (BE-floor already covers that)
                    stop = max(stop, entry_price)
                    if l[i] <= stop:
                        p = min(o[i], stop)
                        if exit_price is None or p < exit_price:
                            exit_price, exit_reason = p, "CHANDELIER"
                # 4) time stop
                if exit_price is None and (i - entry_i) >= MAX_HOLD_BARS:
                    exit_price, exit_reason = c[i], "TIME"

                if exit_price is not None:
                    gross_usd = TRADE_NOTIONAL * (exit_price / entry_price - 1.0)
                    out.append({
                        "symbol": sym, "signal": sig_name,
                        "entry_date": str(dates[entry_i].date()),
                        "exit_date": str(dates[i].date()),
                        "entry": float(entry_price), "exit": float(exit_price),
                        "pnl_pct": (exit_price / entry_price - 1.0) * 100.0,
                        "gross_pnl_usd": float(gross_usd),
                        "pnl_usd": float(gross_usd - cost_usd),
                        "bars": i - entry_i, "reason": exit_reason,
                    })
                    in_trade = False
                continue

            # --- look for entry: signal on bar t, enter at open[t+1] ---
            if mask[t] and t + 1 < n:
                e = t + 1
                if min_entry is not None and date_strs[e] < min_entry:
                    continue
                in_trade = True
                entry_i = e
                if variant.atr_sl:
                    a = atr[t]
                    entry_sl_pct = min(max(1.5 * a / c[t] * 100.0, 3.0), 6.0) if a == a else 3.0
                else:
                    entry_sl_pct = variant.sl_pct
                # the SL is DEFINED at entry (level from data ≤ t) → it can fire
                # on the entry bar itself. BE/Chandelier need a prior-bar peak,
                # so they start the next bar.
                if entry_sl_pct and l[e] <= o[e] * (1 - entry_sl_pct / 100.0):
                    exit_price = min(o[e], o[e] * (1 - entry_sl_pct / 100.0))
                    gross_usd = TRADE_NOTIONAL * (exit_price / o[e] - 1.0)
                    out.append({
                        "symbol": sym, "signal": sig_name,
                        "entry_date": str(dates[e].date()),
                        "exit_date": str(dates[e].date()),
                        "entry": float(o[e]), "exit": float(exit_price),
                        "pnl_pct": (exit_price / o[e] - 1.0) * 100.0,
                        "gross_pnl_usd": float(gross_usd),
                        "pnl_usd": float(gross_usd - cost_usd),
                        "bars": 0, "reason": "SL",
                    })
                    in_trade = False  # closed on the entry bar — do not leave it open
    return out


# ──────────────────────────────────────────────────────────────────────────────
# 6. METRICS
# ──────────────────────────────────────────────────────────────────────────────

def metrics(trades: list[dict], calendar: list[str] | None = None,
            mark_closes: dict[str, dict[str, float]] | None = None) -> dict:
    """Aggregate trade stats.

    Realized stats (n/WR/PF/avg/totals) are always computed. The risk metrics
    (MDD, Sharpe) are built from the STRATEGY-PnL curve, NOT the leveraged
    book:

    * MDD — peak-to-trough of (realized + unrealized) PnL, reported in $ and
      as % of peak equity. The old version measured the ~21×$1k long book
      swinging against a 10k base, which produced −84% MDD on a +$3.8k PnL
      run (an impossibility).
    * Sharpe — daily returns of that PnL curve normalized by AVERAGE BOUND
      CAPITAL (return on capital deployed), annualized ×√252. The old version
      measured beta of the long book, so losing variants scored Sharpe 2.8.

    When `calendar` + `mark_closes` are supplied the curve is on a CALENDAR-day
    basis with open positions marked to the day's close (no look-ahead).
    Without them it falls back to event days (weaker, still valid for slices).
    """
    if not trades:
        return {"n": 0}
    pnls = np.array([t["pnl_pct"] for t in trades])
    wins = pnls[pnls > 0]
    losses = pnls[pnls <= 0]
    pf = (wins.sum() / abs(losses.sum())) if len(losses) and losses.sum() != 0 else (
        float("inf") if len(wins) else 0.0)
    total_net = float(np.sum([t["pnl_usd"] for t in trades]))
    total_gross = float(np.sum([t.get("gross_pnl_usd", t["pnl_usd"]) for t in trades]))

    # ---- equity curve ----
    entries_by_date: dict[str, list[dict]] = {}
    for t in trades:
        entries_by_date.setdefault(t["entry_date"], []).append(t)

    if calendar is not None and mark_closes is not None:
        days = list(calendar)
    else:
        days = sorted({t["entry_date"] for t in trades} | {t["exit_date"] for t in trades})

    pcurve = []
    capcurve = []
    realized = 0.0
    open_pos: list[dict] = []
    for d in days:
        # book realized PnL for positions closing today
        still = []
        for t in open_pos:
            if t["exit_date"] == d:
                realized += t["pnl_usd"]
            else:
                still.append(t)
        open_pos = still
        # new positions entering today
        for t in entries_by_date.get(d, []):
            open_pos.append(t)
        # mark open positions to today's close (unrealized, no look-ahead);
        # bound capital = sum of notional of open positions
        um = 0.0
        cap = 0.0
        if mark_closes is not None:
            for t in open_pos:
                cl = mark_closes.get(t["symbol"], {}).get(d)
                if cl is not None and cl > 0:
                    um += TRADE_NOTIONAL * (cl / t["entry"] - 1.0)
                cap += TRADE_NOTIONAL
        pcurve.append(realized + um)
        capcurve.append(cap)

    pcurve = np.array(pcurve)
    capcurve = np.array(capcurve)
    # MDD of the STRATEGY PnL curve (peak-to-trough in $, then % of peak
    # equity) — NOT the leveraged book against a 10k base.
    ppeak = np.maximum.accumulate(pcurve)
    mdd_usd = float((pcurve - ppeak).min()) if len(pcurve) else 0.0
    base_eq = INITIAL_EQUITY + float(ppeak.max())
    mdd = float((mdd_usd / base_eq) * 100) if base_eq > 0 else 0.0
    # Sharpe of strategy PnL normalized by AVERAGE BOUND CAPITAL (return on
    # capital deployed, not beta of a ~21k long book on a 10k base).
    cap_avg = float(capcurve.mean()) if len(capcurve) else 0.0
    if len(pcurve) > 1 and cap_avg > 0:
        rets = np.diff(pcurve) / cap_avg
        sharpe = float(rets.mean() / rets.std() * math.sqrt(252)) if rets.std() > 0 else 0.0
    else:
        sharpe = 0.0
    return {
        "n": len(trades),
        "wr": float((pnls > 0).mean() * 100),
        "avg_win": float(wins.mean()) if len(wins) else 0.0,
        "avg_loss": float(losses.mean()) if len(losses) else 0.0,
        "profit_factor": round(pf, 2) if pf != float("inf") else 99.0,
        "avg_pnl_pct": float(pnls.mean()),
        "total_pnl_usd": total_net,
        "gross_pnl_usd": total_gross,
        "costs_usd": round(total_gross - total_net, 2),
        "avg_bars": float(np.mean([t["bars"] for t in trades])),
        "mdd_usd": round(mdd_usd, 2),
        "mdd_pct": round(mdd, 2),
        "sharpe": round(sharpe, 2),
        "bound_capital_avg": round(cap_avg, 2),
        "bound_capital_peak": round(float(capcurve.max()), 2) if len(capcurve) else 0.0,
        "final_pnl_curve": round(float(pcurve[-1]), 2) if len(pcurve) else 0.0,
        "by_reason": {r: sum(1 for t in trades if t["reason"] == r)
                      for r in ("SL", "BE_LOCK", "CHANDELIER", "TIME")},
    }


# ──────────────────────────────────────────────────────────────────────────────
# 7. BENCHMARK (equal-weight B&H + SPY)
# ──────────────────────────────────────────────────────────────────────────────

def benchmark(data: dict[str, pd.DataFrame], sim_start: str | None,
              end: str | None) -> dict:
    """Honest baselines: equal-weight buy-and-hold of the universe + SPY.

    Per symbol: enter at first open ≥ sim_start (or ≥ START if none given),
    exit at last close. No rebalancing. SPY = raw total return.
    """
    per_sym: dict[str, float] = {}
    for sym, df in data.items():
        d = df
        if sim_start is not None:
            d = d.loc[d.index >= pd.Timestamp(sim_start)]
        if end is not None:
            d = d.loc[d.index < pd.Timestamp(end)]
        if d.empty or len(d) < 5:
            continue
        entry = float(d["Open"].iloc[0])
        last = float(d["Close"].iloc[-1])
        if entry > 0:
            per_sym[sym] = last / entry - 1.0
    eq_ret = float(np.mean(list(per_sym.values()))) if per_sym else 0.0
    spy = None
    if "SPY" in data:
        d = data["SPY"]
        if sim_start is not None:
            d = d.loc[d.index >= pd.Timestamp(sim_start)]
        if end is not None:
            d = d.loc[d.index < pd.Timestamp(end)]
        if len(d) >= 5:
            spy = float(d["Close"].iloc[-1] / d["Open"].iloc[0] - 1.0)
    return {"equal_weight_bh": {"n_symbols": len(per_sym),
                                "total_return_pct": round(eq_ret * 100, 2)},
            "spy_total_return_pct": round(spy * 100, 2) if spy is not None else None}


# ──────────────────────────────────────────────────────────────────────────────
# 8. WORKER + MAIN
# ──────────────────────────────────────────────────────────────────────────────
_variant_ref: "Variant | None" = None
_sim_params: tuple = (KNIFE_MAX_ATR_BELOW_SMA20, 0.0, None)  # (knife_atr, cost_pct, min_entry)


def _process_symbol(args):
    global _variant_ref
    sym, cache_csv = args
    variant = _variant_ref
    assert variant is not None
    knife_atr, cost_pct, min_entry = _sim_params
    df = pd.read_csv(cache_csv, parse_dates=["Date"], index_col="Date")
    trades = simulate_symbol(sym, df, variant, knife_atr=knife_atr,
                             cost_pct=cost_pct, min_entry=min_entry)
    return trades


def main() -> int:
    global _variant_ref, _sim_params
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=max(2, cpu_count() // 2))
    ap.add_argument("--limit", type=int, default=0, help="only first N symbols (smoke test)")
    ap.add_argument("--study", choices=sorted(STUDIES.keys()), default="main",
                    help="date-range preset (main=2025→today, bear=2022-23 stress)")
    ap.add_argument("--start", default=None, help="override data start (ISO)")
    ap.add_argument("--sim-start", default=None, help="override sim window start (ISO)")
    ap.add_argument("--end", default=None, help="override sim window end (ISO, exclusive)")
    ap.add_argument("--knife-atr", type=float, default=KNIFE_MAX_ATR_BELOW_SMA20,
                    help=f"knife-gate ATR-below-SMA20 threshold (default {KNIFE_MAX_ATR_BELOW_SMA20})")
    ap.add_argument("--cost-pct", type=float, default=0.0,
                    help="round-trip cost %% per trade (0.2 = 20 bps each way)")
    ap.add_argument("--out", default=None, help="output JSON tag (default: study name)")
    args = ap.parse_args()

    st = dict(STUDIES[args.study])
    start = args.start or st["start"]
    sim_start = args.sim_start if args.sim_start is not None else st["sim_start"]
    end = args.end if args.end is not None else st["end"]
    sim_start = sim_start or START
    if args.out:
        tag = args.out
    else:
        tag = args.study
        if args.knife_atr != KNIFE_MAX_ATR_BELOW_SMA20:
            tag += f"_ka{args.knife_atr:g}"
        if args.cost_pct > 0:
            tag += f"_c{args.cost_pct:g}"
        if args.start or args.sim_start or args.end:
            tag += "_custom"
    _sim_params = (args.knife_atr, args.cost_pct, sim_start)

    t0 = time.time()
    symbols = UNIVERSE_SYMBOLS[:args.limit] if args.limit else UNIVERSE_SYMBOLS
    print(f"Study: {tag} | data {start} → {end or 'today'} | sim window from {sim_start}")
    print(f"Universe: {len(symbols)} symbols | knife_atr={args.knife_atr} | cost={args.cost_pct}%/rt")
    data = load_ohlcv(symbols, start, end)
    print(f"  loaded {len(data)}/{len(symbols)} symbols in {time.time()-t0:.1f}s")
    failed = [s for s in symbols if s not in data]
    if failed:
        print(f"  FAILED (skipped): {failed}")

    # per-symbol CSVs for the worker pool (sim window slice)
    tmp = ROOT / "data" / "backtest_symfiles"
    tmp.mkdir(parents=True, exist_ok=True)
    paths = []
    for sym, df in data.items():
        p = tmp / f"{tag}_{sym.replace('^','x').replace('-','_')}.csv"
        df.to_csv(p)
        paths.append((sym, str(p)))

    results: dict[str, list[dict]] = {}
    for v in VARIANTS:
        _variant_ref = v
        t_v = time.time()
        with Pool(args.workers) as pool:
            for sym_trades in pool.imap_unordered(_process_symbol, paths, chunksize=1):
                results.setdefault(v.key, []).extend(sym_trades)
        print(f"  {v.key} {v.name}: {len(results[v.key])} trades in {time.time()-t_v:.1f}s")

    # calendar + mark closes for the overall equity curve (sim window only)
    all_dates = sorted({d for df in data.values()
                        for d in df.index.strftime("%Y-%m-%d").tolist()
                        if d >= sim_start and (end is None or d < end)})
    mark_closes = {}
    for sym, df in data.items():
        d = df.loc[df.index.strftime("%Y-%m-%d") >= sim_start]
        if end:
            d = d.loc[d.index.strftime("%Y-%m-%d") < end]
        mark_closes[sym] = {ds: float(c) for ds, c in
                            zip(d.index.strftime("%Y-%m-%d"), d["Close"])}

    # aggregate — per-signal stats only over trades that ENTERED in the sim
    # window (the overall curve keeps boundary trades so open positions settle)
    in_win = lambda tr: [t for t in tr
                         if t["entry_date"] >= sim_start
                         and (end is None or t["entry_date"] < end)]
    summary = {}
    for v in VARIANTS:
        tr = results[v.key]
        tw = in_win(tr)
        by_sig = {s: metrics([t for t in tw if t["signal"] == s])
                  for s in ("DIP", "MOMENTUM", "MACD_TURN")}
        overall = metrics(tw, calendar=all_dates, mark_closes=mark_closes)
        summary[v.key] = {"name": v.name, "overall": overall,
                          **{f"sig_{s}": m for s, m in by_sig.items()}}

    out_path = ROOT / "backtest" / "results" / f"exit_variant_results_{tag}.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"generated": pd.Timestamp.now(tz="UTC").isoformat(),
               "study": tag, "data_start": start, "sim_start": sim_start,
               "sim_end": end, "knife_atr": args.knife_atr, "cost_pct": args.cost_pct,
               "benchmark": benchmark(data, sim_start, end),
               "symbols": list(data.keys()), "n_symbols": len(data),
               "trades_per_variant": {k: len(in_win(vv)) for k, vv in results.items()},
               "summary": summary}
    out_path.write_text(json.dumps(payload, indent=2, default=str))
    print(f"\nResults → {out_path}")
    print(f"Total wall time: {time.time()-t0:.1f}s")

    # console table
    bm = payload["benchmark"]
    print(f"\n=== BENCHMARK (sim {sim_start} → {end or 'today'}) ===")
    print(f"  equal-weight B&H ({bm['equal_weight_bh']['n_symbols']} sym): "
          f"{bm['equal_weight_bh']['total_return_pct']:+.2f}%")
    if bm["spy_total_return_pct"] is not None:
        print(f"  SPY total return: {bm['spy_total_return_pct']:+.2f}%")

    print("\n=== OVERALL BY VARIANT ===")
    hdr = (f"{'Var':4} {'Name':26} {'Trades':>7} {'WR%':>6} {'PF':>6} {'Avg%':>7} "
           f"{'Gross$':>9} {'Net$':>9} {'Cost$':>7} {'MDD$':>8} {'MDD%':>7} "
           f"{'Cap$avg':>9} {'Sharpe':>7}")
    print(hdr)
    print("  (MDD/Sharpe on the STRATEGY-PnL curve; Cap$avg = average bound capital;")
    print("   Sharpe = return on bound capital — see metrics() docstring)")
    for v in VARIANTS:
        m = summary[v.key]["overall"]
        if m["n"] == 0:
            print(f"{v.key:4} {v.name:26} {'0':>7}")
            continue
        print(f"{v.key:4} {v.name:26} {m['n']:>7} {m['wr']:>6.1f} {m['profit_factor']:>6.2f} "
              f"{m['avg_pnl_pct']:>7.2f} {m['gross_pnl_usd']:>9.0f} {m['total_pnl_usd']:>9.0f} "
              f"{m['costs_usd']:>7.0f} {m['mdd_usd']:>8.0f} {m['mdd_pct']:>7.1f} "
              f"{m['bound_capital_avg']:>9.0f} {m['sharpe']:>7.2f}")

    print("\n=== BY SIGNAL TYPE (per variant) ===")
    for v in VARIANTS:
        print(f"\n[{v.key} {v.name}]")
        for s in ("DIP", "MOMENTUM", "MACD_TURN"):
            m = summary[v.key][f"sig_{s}"]
            if m["n"] == 0:
                print(f"  {s:10} no trades")
                continue
            print(f"  {s:10} n={m['n']:>4} WR={m['wr']:>5.1f}% PF={m['profit_factor']:>5.2f} "
                  f"avg={m['avg_pnl_pct']:>+6.2f}% tot=${m['total_pnl_usd']:>9.0f} "
                  f"reasons={m['by_reason']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
