#!/usr/bin/env python3
"""E5-Test — Live-Kaufsignale × Ausstiege × echte Gebuehren (nur lesend).

Vorregistrierung: docs/vorregistrierung-e5-signale-exits-gebuehren-2026-10-17.md
(Commit 63d5aa8, VOR diesem Skript). Daten: data/e5_bars/ (scripts/e5_fetch_bars.py).

  /usr/bin/python3 scripts/e5_signal_exit_test.py
"""
from __future__ import annotations

import bisect
import glob
import math
import os
import sys
from collections import defaultdict
from datetime import date

import numpy as np
import pandas as pd
import ta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BARS = os.path.join(ROOT, "data", "e5_bars")

MIN_BARS, GAP_DAYS = 300, 10
SPLIT = 0.60
SIZES = (200.0, 400.0, 880.0)
SPREAD, SPREAD_SENS = 0.002, 0.005
TWO_DOLLAR = (".ASX", ".AX", ".HK", ".T", ".AE", ".DH")
N_MIN, T_MIN, TRIM = 200, 3.0, 0.05
TIME_BARS = 20
BE_ARM, BE_FLOOR = 3.0, 0.3
FADE_ARM, FADE_RETRACE = 2.0, 0.40

FAMILIES = {
    "A MACD_TURN+BB_LOW_MACD": frozenset({"MACD_TURN_BELOW_SMA20", "BB_LOW_MACD_IMPROVING"}),
    "B RSI_EXTREME+MACD_TURN": frozenset({"RSI_EXTREME_OVERSOLD", "MACD_TURN_BELOW_SMA20"}),
    "C MACD_TURN allein": frozenset({"MACD_TURN_BELOW_SMA20"}),
    "D TREND_PULLBACK allein": frozenset({"TREND_PULLBACK"}),
    "E TREND_PULLBACK+GOLDEN_CROSS": frozenset({"TREND_PULLBACK", "GOLDEN_CROSS"}),
}
EXITS = ("X1 heute", "X2 laufen lassen", "X3 laufen+SELL")
COMPONENTS = ("BB_LOWER_RSI_OVERSOLD", "BB_EXTREME_RSI_OVERSOLD", "RSI_EXTREME_OVERSOLD",
              "MACD_TURN_BELOW_SMA20", "BB_LOW_MACD_IMPROVING", "TREND_PULLBACK", "GOLDEN_CROSS")


# ── Daten ─────────────────────────────────────────────────────────────────────
def load(path):
    with open(path) as fh:
        head = fh.readline().strip().lstrip("#").split()
    df = pd.read_csv(path, comment="#")
    df = df[(df.close > 0) & (df.high > 0) & (df.low > 0) & (df.open > 0)].reset_index(drop=True)
    return (head[0] if head else ""), df


def segments(dates):
    seg, start = [], 0
    for i in range(1, len(dates) + 1):
        if i == len(dates) or (date.fromisoformat(dates[i]) - date.fromisoformat(dates[i - 1])).days > GAP_DAYS:
            seg.append((start, i)); start = i
    return seg


def indicators(df):
    c, h, l, v = df.close, df.high, df.low, df.volume
    out = pd.DataFrame(index=df.index)
    out["rsi"] = ta.momentum.RSIIndicator(c, window=14).rsi()
    out["hist"] = ta.trend.MACD(c, window_slow=26, window_fast=12, window_sign=9).macd_diff()
    out["bb"] = ta.volatility.BollingerBands(c, window=20, window_dev=2).bollinger_pband()
    out["atr"] = ta.volatility.AverageTrueRange(h, l, c, window=14).average_true_range()
    out["sma20"] = c.rolling(20).mean()
    out["sma50"] = c.rolling(50).mean()
    va = v.rolling(20).mean()
    out["vr"] = np.where(va > 0, v / va.replace(0, np.nan), np.nan)
    dd = np.zeros(len(c), dtype=int)
    cv = c.to_numpy()
    for i in range(1, len(cv)):
        dd[i] = dd[i - 1] + 1 if cv[i] < cv[i - 1] else 0
    out["down"] = dd
    out["roc5"] = (c / c.shift(5) - 1) * 100
    return out


def signal_sets(df, ind):
    """Je Bar: frozenset der feuernden BUY-Regeln (leer bei SELL-Regel), plus SELL-Maske."""
    c = df.close.to_numpy(); rsi = ind["rsi"].to_numpy(); hist = ind["hist"].to_numpy()
    hp = np.roll(hist, 1); hp[0] = np.nan
    bb = ind["bb"].to_numpy(); atr = ind["atr"].to_numpy()
    s20 = ind["sma20"].to_numpy(); s50 = ind["sma50"].to_numpy()
    s20lb = np.roll(s20, 2); s50lb = np.roll(s50, 2); s20lb[:2] = np.nan; s50lb[:2] = np.nan
    vr = np.nan_to_num(ind["vr"].to_numpy(), nan=1.0)        # live: get('vol_ratio', 1.0)
    with np.errstate(invalid="ignore", divide="ignore"):
        dist = np.where((atr > 0) & (c < s20), (s20 - c) / atr, np.nan)
        knife = (ind["down"].to_numpy() >= 4) | (ind["roc5"].to_numpy() <= -12.0) | (dist >= 2.5)
        r = {
            "BB_LOWER_RSI_OVERSOLD": (bb < 0.1) & (rsi < 30) & (c > s50) & (vr < 1.5) & ~knife,
            "BB_EXTREME_RSI_OVERSOLD": (bb < 0.05) & (rsi < 30) & (c > s50) & (vr < 1.5) & ~knife,
            "RSI_EXTREME_OVERSOLD": (rsi < 25) & ~knife,
            "MACD_TURN_BELOW_SMA20": (hist > hp) & (hist < 0) & (c < s20),
            "BB_LOW_MACD_IMPROVING": (bb < 0.1) & (hist > hp) & ~knife,
            "TREND_PULLBACK": (c > s50) & (c <= s20 * 1.02) & (rsi >= 35) & (rsi <= 55)
                              & (hist > -0.00005 * c) & (rsi > 30),
            "GOLDEN_CROSS": (s20 > s50) & (s20lb <= s50lb) & (hist > 0) & (rsi < 60),
        }
        sell = (bb > 0.95) & (rsi > 70)
    sets = []
    for i in range(len(c)):
        if sell[i]:
            sets.append(frozenset()); continue
        sets.append(frozenset(k for k in COMPONENTS if r[k][i]))
    return sets, sell


# ── Simulation ────────────────────────────────────────────────────────────────
def simulate(df, ind, sets, sell, fam_set, exit_kind, a, b):
    o, h, l, c = (df[x].to_numpy() for x in ("open", "high", "low", "close"))
    atrp = (ind["atr"] / df.close * 100).to_numpy()
    d = df.date.tolist()
    trades = []
    t = a
    while t < b - 1:
        if sets[t] != fam_set:
            t += 1; continue
        e = t + 1
        ep = o[e]
        slp = atrp[t] * 1.5
        slp = 3.5 if not math.isfinite(slp) else min(max(slp, 3.5), 6.0)
        sl = ep * (1 - slp / 100)
        exit_i = exit_p = None; why = None
        if l[e] <= sl:
            exit_i, exit_p, why = e, min(o[e], sl), "SL"
        i = e + 1
        while exit_i is None and i < b:
            cands = []
            if l[i] <= sl:
                cands.append((min(o[i], sl), "SL"))
            peak = (max(h[e:i]) / ep - 1) * 100
            if exit_kind == "X1 heute":
                if peak >= BE_ARM:
                    fl = ep * (1 + BE_FLOOR / 100)
                    if l[i] <= fl:
                        cands.append((min(o[i], fl), "BE"))
                gain = (c[i] / ep - 1) * 100
                if peak >= FADE_ARM and gain <= peak * (1 - FADE_RETRACE):
                    cands.append((c[i], "FADE"))
            if exit_kind in ("X1 heute", "X3 laufen+SELL") and sell[i]:
                cands.append((c[i], "SELL"))
            if not cands and i - e >= TIME_BARS:
                cands.append((c[i], "TIME"))
            if cands:
                exit_p, why = min(cands, key=lambda x: x[0])
                exit_i = i
            i += 1
        if exit_i is None:
            break                              # Segmentende ohne Ausstieg: verworfen
        trades.append((d[e], d[exit_i], ep, exit_p, why, exit_i - e))
        t = exit_i if exit_i > t else t + 1
    return trades


def cluster_t(vals_keys):
    n = len(vals_keys)
    if n < 2:
        return 0.0
    m = sum(v for v, _ in vals_keys) / n
    g = defaultdict(float)
    for v, k in vals_keys:
        g[k] += v - m
    G = len(g)
    if G < 2:
        return 0.0
    var = sum(x * x for x in g.values()) / (n * n) * G / (G - 1)
    return m / math.sqrt(var) if var > 0 else 0.0


def trimmed(v, frac=TRIM):
    v = sorted(v); k = int(len(v) * frac)
    v = v[k:len(v) - k] if len(v) - 2 * k > 0 else v
    return sum(v) / len(v)


def main() -> int:
    files = sorted(glob.glob(os.path.join(BARS, "*.csv")))
    _, spy = load(os.path.join(BARS, "SPY.csv"))
    spy_d = spy.date.tolist(); spy_o = spy.open.to_numpy(); spy_c = spy.close.to_numpy()

    def spy_ret(d0, d1):
        i = bisect.bisect_left(spy_d, d0); j = bisect.bisect_right(spy_d, d1) - 1
        if i >= len(spy_d) or j < i:
            return None
        return spy_c[j] / spy_o[i] - 1

    axis = set(); res = defaultdict(list); n_tit = 0
    for f in files:
        key = os.path.basename(f)[:-4]
        if key == "SPY":
            continue
        sym, df = load(f)
        if len(df) < MIN_BARS:
            continue
        n_tit += 1
        axis.update(df.date)
        fee = 2.0 if sym.upper().endswith(TWO_DOLLAR) else 1.0
        for a, b in segments(df.date.tolist()):
            if b - a < 60:
                continue
            seg = df.iloc[a:b].reset_index(drop=True)
            ind = indicators(seg)
            sets, sell = signal_sets(seg, ind)
            for fam, fs in FAMILIES.items():
                for ex in EXITS:
                    for tr in simulate(seg, ind, sets, sell, fs, ex, 0, len(seg)):
                        res[(fam, ex)].append((key, fee) + tr)
    axis = sorted(axis)
    split = axis[int(len(axis) * SPLIT)]
    print(f"Universum {n_tit} Titel, {axis[0]}..{axis[-1]}, Holdout ab {split}\n")

    verdict = {}
    for fam in FAMILIES:
        for ex in EXITS:
            rows = [r for r in res[(fam, ex)] if r[2] >= split]
            if not rows:
                print(f"{fam} | {ex}: keine Holdout-Trades"); continue
            gross = [r[5] / r[4] - 1 for r in rows]
            spyr = [spy_ret(r[2], r[3]) for r in rows]
            ok = [(r, g, s) for r, g, s in zip(rows, gross, spyr) if s is not None]
            wins = [g for _, g, _ in ok if g > 0]; losses = [g for _, g, _ in ok if g <= 0]
            reasons = defaultdict(int)
            for r, _, _ in ok:
                reasons[r[6]] += 1
            hold = sorted(r[7] for r, _, _ in ok)[len(ok) // 2]
            print(f"── {fam} | {ex} — n={len(ok)}, WR {len(wins)/len(ok)*100:.0f} %, "
                  f"Ø Gewinn {np.mean(wins)*100 if wins else 0:+.2f} %, Ø Verlust "
                  f"{np.mean(losses)*100 if losses else 0:+.2f} %, Halt {hold} T, "
                  f"brutto {np.mean([g for _, g, _ in ok])*100:+.2f} %, SPY {np.mean([s for *_, s in ok])*100:+.2f} %, "
                  f"Ausstiege {dict(reasons)}")
            for size in SIZES:
                ex_net = [(g - s - 2 * r[1] / size - SPREAD, r[2]) for r, g, s in ok]
                ab_net = [g - 2 * r[1] / size - SPREAD for r, g, s in ok]
                exv = [v for v, _ in ex_net]
                n = len(exv); m = sum(exv) / n
                t = cluster_t(ex_net); tm = trimmed(exv)
                by = defaultdict(list)
                for (r, _, _), v in zip(ok, exv):
                    by[r[0]].append(v)
                tot = sum(exv)
                loo = m > 0 and all((tot - sum(v)) / (n - len(v)) > 0 for v in by.values() if n - len(v) > 0)
                ma = sum(ab_net) / n
                c = [n >= N_MIN, m > 0, t >= T_MIN, tm > 0, loo, ma > 0]
                v_ = "UNENTSCHEIDBAR" if not c[0] else ("ANGENOMMEN" if all(c) else "verworfen")
                verdict[(fam, ex, size)] = v_
                sens = m + SPREAD - SPREAD_SENS
                print(f"   ${size:>4.0f}: Excess {m*100:+.2f} % (t {t:+.2f}, Trim {tm*100:+.2f} %, LOO {'✓' if loo else '✗'}), "
                      f"absolut {ma*100:+.2f} %  -> {v_}   [Spread 0,5 %: Excess {sens*100:+.2f} %]")
            # Boersentyp (beschreibend, $880)
            for lab, fee_v in (("$1", 1.0), ("$2", 2.0)):
                sub = [(g, s) for r, g, s in ok if r[1] == fee_v]
                if len(sub) >= 50:
                    print(f"   [{lab}-Boersen, $880] n={len(sub)} absolut "
                          f"{np.mean([g - 2*fee_v/880 - SPREAD for g, _ in sub])*100:+.2f} %, Excess "
                          f"{np.mean([g - s - 2*fee_v/880 - SPREAD for g, s in sub])*100:+.2f} %")
            tr_rows = [r for r in res[(fam, ex)] if r[2] < split]
            if tr_rows:
                g_tr = [r[5] / r[4] - 1 for r in tr_rows]
                print(f"   [Training] n={len(tr_rows)} brutto {np.mean(g_tr)*100:+.2f} %")
    acc = [k for k, v in verdict.items() if v == "ANGENOMMEN"]
    print("\nAngenommen:", acc if acc else "keine")
    return 0


if __name__ == "__main__":
    sys.exit(main())
