import sys, numpy as np
sys.path.insert(0, 'backtest')
from exit_variant_backtest import (compute_arrays, load_ohlcv, UNIVERSE_SYMBOLS, START)
data = load_ohlcv(UNIVERSE_SYMBOLS, START)
print(f"loaded {len(data)}/{len(UNIVERSE_SYMBOLS)}")
rows = []
for sym, df in data.items():
    arr = compute_arrays(df)
    c, rsi = arr["close"], arr["rsi"]
    atr, sma20 = arr["atr"], arr["sma20"]
    valid = (~np.isnan(atr) & ~np.isnan(sma20) & ~np.isnan(rsi)
             & (atr > 0) & (sma20 > 0))
    rsi25 = valid & (rsi < 25)
    for i in np.where(rsi25)[0]:
        rows.append({
            "below": (sma20[i] - c[i]) / atr[i],
            "dd4": bool(arr["down_days"][i] >= 4),
            "roc": bool(arr["roc5"][i] <= -12),
        })
n = len(rows)
below = np.array([r["below"] for r in rows])
print(f"\nRSI<25 bars total: {n}")
for q in (0, 10, 25, 50, 75, 90, 100):
    print(f"  pct{q:>3}  ATR-below-SMA20 = {np.percentile(below, q):.2f}")
for thr in (2.5, 3.0, 3.5, 4.0, 5.0):
    u = int((below < thr).sum())
    print(f"  knife_atr={thr:<4}: ATR-criterion passes {u:>4}/{n} ({u/n*100:5.1f}%)")
dd_or_roc = int(sum(r["dd4"] or r["roc"] for r in rows))
both_ok = n - dd_or_roc
print(f"\n  RSI<25 bars with dd>=4 OR roc<=-12: {dd_or_roc}/{n}")
print(f"  bars passing dd+roc even at infinite atr gate: {both_ok}/{n}")
print("  (that is the HARD ceiling for DIP-unlock via the ATR criterion)")
