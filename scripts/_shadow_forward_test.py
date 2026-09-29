#!/usr/bin/env python3
"""Shadow forward-return test on the FULL signal log (18k+ signals).

Advisor plan (2026-09-29) H1/H3/H4/H6:
  - Forward return 5d & 10d from signal price, NET of estimated round-trip cost
    (stock 0.55%, crypto 4.5%, etf 0.65% — measured cost/notional, post-Zaesur).
  - Gates tested: regime, trend (SMA20/200, ROC5), ADV, ATR%, conviction.
  - 60/40 chronological split (cutoff 2026-08-26), holdout viewed ONCE.
  - Primary metric: expectancy per trade in USD (assume $150 notional).
  - CI: block bootstrap-lite on per-day means.
"""
import sqlite3, math, re
from collections import defaultdict

DB = "data/trading.db"
NOTIONAL = 150.0
COST_PCT = {"stock": 0.0055, "crypto": 0.045, "etf": 0.0065}
CUTOFF = "2026-08-26"   # ~60/40 split of 2026-07-26..2026-09-29

c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
c.row_factory = sqlite3.Row
def q(sql, args=()):
    return [dict(r) for r in c.execute(sql, args).fetchall()]

# ── daily bars ──
print("loading ohlcv...", flush=True)
bars = defaultdict(list)
for r in q("SELECT instrument_id, date, close FROM ohlcv_daily"):
    bars[r["instrument_id"]].append((r["date"], r["close"]))
for iid in bars:
    bars[iid].sort()
print(f"instruments with bars: {len(bars)}", flush=True)

def _idx_on_or_before(arr, t0):
    lo, hi, idx = 0, len(arr) - 1, -1
    while lo <= hi:
        mid = (lo + hi) // 2
        if arr[mid][0] <= t0:
            idx = mid; lo = mid + 1
        else:
            hi = mid - 1
    return idx

def fwd_ret(iid, ts, days):
    arr = bars.get(iid)
    if not arr:
        return None
    idx = _idx_on_or_before(arr, ts[:10])
    if idx < 0:
        return None
    p0 = arr[idx][1]
    j = idx + days
    if j >= len(arr) or p0 <= 0:
        return None
    return arr[j][1] / p0 - 1.0

def trend_feats(iid, ts):
    arr = bars.get(iid)
    if not arr:
        return None
    idx = _idx_on_or_before(arr, ts[:10])
    if idx < 0:
        return None
    closes = [x[1] for x in arr[max(0, idx - 199):idx + 1]]
    c0 = closes[-1]
    n = len(closes)
    sma20 = sum(closes[-20:]) / 20 if n >= 20 else None
    sma200 = sum(closes) / n if n >= 60 else None
    roc5 = (c0 / closes[-6] - 1) * 100 if n >= 6 else None
    return c0, sma20, sma200, roc5

# ── regime timeline ──
reg_rows = q("SELECT ts, message FROM system_log WHERE message LIKE 'Regime: %' AND ts >= '2026-07-20' ORDER BY ts")
regimes = []
last = None
for r in reg_rows:
    m = re.match(r"Regime: (\w+)", r["message"])
    if m and m.group(1) != last:
        regimes.append((r["ts"], m.group(1)))
        last = m.group(1)

def regime_for(ts):
    reg = None
    for pts, rn in regimes:
        if pts <= ts:
            reg = rn
        else:
            break
    return reg

# ── signals ──
print("loading signals...", flush=True)
sigs = q("""
SELECT s.id, s.instrument_id, s.generated_at, s.signal_type, s.conviction, s.score,
       s.price, i.asset_class, i.adv_usd, i.atr_pct
FROM signals s LEFT JOIN instruments i ON i.instrument_id = s.instrument_id
WHERE s.generated_at >= '2026-07-26' AND s.price IS NOT NULL
""")
print(f"signals with price: {len(sigs)}", flush=True)

R = []
for s in sigs:
    r5 = fwd_ret(s["instrument_id"], s["generated_at"], 5)
    r10 = fwd_ret(s["instrument_id"], s["generated_at"], 10)
    if r5 is None and r10 is None:
        continue
    cost = COST_PCT.get(s["asset_class"] or "stock", 0.0055)
    tf = trend_feats(s["instrument_id"], s["generated_at"])
    c0, sma20, sma200, roc5 = tf if tf else (None, None, None, None)
    R.append({
        "ts": s["generated_at"],
        "type": s["signal_type"] or "",
        "conv": s["conviction"] or "",
        "regime": regime_for(s["generated_at"]),
        "class": s["asset_class"] or "stock",
        "adv": s["adv_usd"],
        "atr": s["atr_pct"],
        "r5_net": r5 - cost if r5 is not None else None,
        "r10_net": r10 - cost if r10 is not None else None,
        "sma20_above": (sma20 is not None and c0 is not None and c0 > sma20),
        "sma200_above": (sma200 is not None and c0 is not None and c0 > sma200),
        "roc5": roc5,
        "has_trend": sma20 is not None,
        "split": "design" if s["generated_at"] < CUTOFF else "holdout",
    })
print(f"usable records: {len(R)}")

def summarize(recs, label, days=10):
    pairs = [(r[f"r{days}_net"], r["ts"][:10]) for r in recs if r[f"r{days}_net"] is not None]
    if not pairs:
        print(f"   {label:58s} n=0")
        return None
    vals = [v for v, _ in pairs]
    n = len(vals)
    mean = sum(vals) / n
    by_day = defaultdict(list)
    for v, d in pairs:
        by_day[d].append(v)
    day_means = [sum(v) / len(v) for v in by_day.values() if v]
    m2 = sum(day_means) / len(day_means)
    var = sum((x - m2) ** 2 for x in day_means) / max(len(day_means) - 1, 1)
    se = math.sqrt(var / max(len(day_means), 1))
    ci = 1.96 * se
    ev = mean * NOTIONAL
    wr = 100 * sum(1 for v in vals if v > 0) / n
    sig_mark = "***" if mean > 1.0 * se else ("*" if mean > 0.67 * se else "")
    print(f"   {label:58s} n={n:5d} net{days}d={mean*100:+.2f}% WR={wr:4.1f}% EV=${ev:+.2f} CI95=±${ci*NOTIONAL:.2f} {sig_mark}")
    return {"n": n, "mean": mean, "ev": ev, "wr": wr, "se": se}

DIP = ("MACD_TURN_BELOW_SMA20", "RSI_EXTREME_OVERSOLD", "BB_LOW")
def dip(r):
    return any(d in r["type"] for d in DIP) and r["class"] == "stock"

print(f"\n=== BASELINE (10d NET, notional ${NOTIONAL:.0f}) ===")
for sp in ("design", "holdout"):
    print(f"--- {sp} ---")
    summarize([r for r in R if r["split"] == sp], "ALL signals")
    summarize([r for r in R if r["split"] == sp and r["class"] == "stock"], "stock only")

print("\n=== H4: conviction ===")
for sp in ("design", "holdout"):
    for cv in ("MEDIUM", "HIGH"):
        summarize([r for r in R if r["split"] == sp and r["conv"] == cv and r["class"] == "stock"], f"stock {cv}")

print("\n=== H1: dip-buy trend gates (stock dip-buys) ===")
for sp in ("design", "holdout"):
    base = [r for r in R if r["split"] == sp and dip(r)]
    summarize(base, "dip-buys BASE")
    summarize([r for r in base if r["sma20_above"]], "dip + price>SMA20")
    summarize([r for r in base if r["sma200_above"]], "dip + price>SMA200")
    summarize([r for r in base if r["sma20_above"] and r["sma200_above"]], "dip + SMA20 + SMA200")
    summarize([r for r in base if r["regime"] in ("NORMAL", "CAUTION")], "dip + regime NORMAL/CAUTION")
    summarize([r for r in base if r["regime"] in ("NORMAL", "CAUTION") and r["sma20_above"]], "dip + N/C + SMA20")
    summarize([r for r in base if r["regime"] == "DEFENSIVE"], "dip + regime DEFENSIVE")
    summarize([r for r in base if r["regime"] != "DEFENSIVE"], "dip + !DEFENSIVE")
    summarize([r for r in base if (r["roc5"] is None or r["roc5"] > -5)], "dip + ROC5>-5 (fail-open)")

print("\n=== H3: cost hurdle — ADV / ATR (stock, ALL signals) ===")
for sp in ("design", "holdout"):
    st = [r for r in R if r["split"] == sp and r["class"] == "stock"]
    summarize(st, "stock ALL")
    summarize([r for r in st if r["adv"] is not None and r["adv"] >= 2e7], "ADV>=20M")
    summarize([r for r in st if r["adv"] is not None and r["adv"] >= 5e6], "ADV>=5M")
    summarize([r for r in st if r["adv"] is not None and r["adv"] < 2e6], "ADV<2M")
    summarize([r for r in st if r["atr"] is not None and r["atr"] >= 3], "ATR%>=3")
    summarize([r for r in st if r["adv"] is not None and r["adv"] >= 2e7 and r["atr"] is not None and r["atr"] >= 2.5], "ADV>=20M + ATR>=2.5")

print("\n=== dip-buys x conviction (H4 on the problem cluster) ===")
for sp in ("design", "holdout"):
    summarize([r for r in R if r["split"] == sp and dip(r) and r["conv"] == "MEDIUM"], "dip MEDIUM")
    summarize([r for r in R if r["split"] == sp and dip(r) and r["conv"] == "HIGH"], "dip HIGH")

print("\n=== dip-buys x ADV (liquidity matters for reversion) ===")
for sp in ("design", "holdout"):
    base = [r for r in R if r["split"] == sp and dip(r)]
    summarize([r for r in base if r["adv"] is not None and r["adv"] >= 2e7], "dip ADV>=20M")
    summarize([r for r in base if r["adv"] is not None and r["adv"] < 5e6], "dip ADV<5M")

print("\nDONE")
