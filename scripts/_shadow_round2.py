#!/usr/bin/env python3
"""Round 2: what actually has positive EV in the holdout (Sept)?
1) Market context: equal-weight forward return per day (all instruments w/ bars)
2) Per-signal-type EV in holdout (top/bottom)
3) 5d vs 10d horizons for key gates
4) Exit behavior: realized trades vs forward-10d market return over same hold
"""
import sqlite3, math, re
from collections import defaultdict

DB = "data/trading.db"
NOTIONAL = 150.0
COST_PCT = {"stock": 0.0055, "crypto": 0.045, "etf": 0.0065}
CUTOFF = "2026-08-26"

c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
c.row_factory = sqlite3.Row
def q(sql, args=()):
    return [dict(r) for r in c.execute(sql, args).fetchall()]

bars = defaultdict(list)
for r in q("SELECT instrument_id, date, close FROM ohlcv_daily"):
    bars[r["instrument_id"]].append((r["date"], r["close"]))
for iid in bars:
    bars[iid].sort()

# ── market context: equal-weight mean forward-10d per calendar day ──
dates = sorted(set(r["date"] for r in q("SELECT DISTINCT date FROM ohlcv_daily")))
print(f"bar dates: {dates[0]} .. {dates[-1]} ({len(dates)} trading days)", flush=True)
idx_of = {d: i for i, d in enumerate(dates)}
pos_by_iid = {}
for iid, arr in bars.items():
    pos_by_iid[iid] = {x[0]: k for k, x in enumerate(arr)}
mkt_fwd = {}
for d in dates:
    i = idx_of[d]
    if i + 10 >= len(dates):
        break
    d10 = dates[i + 10]
    vals = []
    for iid, pos in pos_by_iid.items():
        if d in pos and d10 in pos:
            arr = bars[iid]
            p0, p1 = arr[pos[d]][1], arr[pos[d10]][1]
            if p0 and p1 and p0 > 0:
                vals.append(p1 / p0 - 1)
    if vals:
        mkt_fwd[d] = sum(vals) / len(vals)
print("\nMarket equal-weight fwd-10d by calendar day (sample):")
for d in sorted(mkt_fwd):
    if d >= "2026-07-20":
        print(f"  {d}: {mkt_fwd[d]*100:+.2f}%")

# ── per-signal-type EV in holdout ──
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

sigs = q("""
SELECT s.instrument_id, s.generated_at, s.signal_type, s.conviction, s.price,
       i.asset_class, i.adv_usd, i.atr_pct
FROM signals s LEFT JOIN instruments i ON i.instrument_id = s.instrument_id
WHERE s.generated_at >= '2026-08-26' AND s.price IS NOT NULL
""")
recs = []
for s in sigs:
    r10 = fwd_ret(s["instrument_id"], s["generated_at"], 10)
    if r10 is None:
        continue
    cost = COST_PCT.get(s["asset_class"] or "stock", 0.0055)
    recs.append({
        "ts": s["generated_at"], "type": s["signal_type"] or "?",
        "conv": s["conviction"] or "?", "class": s["asset_class"] or "stock",
        "regime": regime_for(s["generated_at"]),
        "r10_net": r10 - cost,
    })
print(f"\nholdout records: {len(recs)}")

def summ(recs, label, days_key="r10_net", min_n=40):
    vals = [r[days_key] for r in recs if r[days_key] is not None]
    n = len(vals)
    if n < min_n:
        return None
    mean = sum(vals) / n
    by_day = defaultdict(list)
    for r, v in zip([r for r in recs if r[days_key] is not None], vals):
        by_day[r["ts"][:10]].append(v)
    dm = [sum(v)/len(v) for v in by_day.values() if v]
    m2 = sum(dm)/len(dm)
    var = sum((x-m2)**2 for x in dm)/max(len(dm)-1,1)
    se = math.sqrt(var/max(len(dm),1))
    wr = 100*sum(1 for v in vals if v>0)/n
    sig = "***" if mean > 1.0*se else ("*" if mean > 0.67*se else "")
    print(f"  {label:42s} n={n:5d} net10d={mean*100:+.2f}% WR={wr:4.1f}% EV=${mean*NOTIONAL:+.2f} CI=±${se*NOTIONAL:.2f} {sig}")
    return {"n": n, "mean": mean, "se": se, "ev": mean*NOTIONAL}

print("\n=== HOLDOUT: per signal type (n>=40) ===")
by_type = defaultdict(list)
for r in recs:
    by_type[r["type"]].append(r)
results = []
for t, lst in by_type.items():
    res = summ(lst, t)
    if res:
        results.append((t, res))
results.sort(key=lambda x: -x[1]["ev"])
print("\nTOP 8 by EV:")
for t, r in results[:8]:
    print(f"  {t:42s} n={r['n']:4d} EV=${r['ev']:+.2f}")
print("BOTTOM 8 by EV:")
for t, r in results[-8:]:
    print(f"  {t:42s} n={r['n']:4d} EV=${r['ev']:+.2f}")

# ── exit behavior: realized holding vs 10d forward ──
print("\n=== EXIT BEHAVIOR (realized trades vs 10d hold) ===")
tr = q("""
SELECT t.symbol, t.entry_price, t.exit_price, t.created_at, t.closed_at,
       t.direction, t.amount_usd, t.pnl_usd, i.instrument_id, i.asset_class
FROM trades t LEFT JOIN instruments i ON i.instrument_id = t.instrument_id
WHERE t.status='CLOSED' AND t.created_at>='2026-08-26'
  AND t.entry_price IS NOT NULL AND t.exit_price IS NOT NULL AND i.instrument_id IS NOT NULL
""")
n = 0; under = 0; over = 0; both = 0
tot_real = 0; tot_10d = 0
for t in tr:
    sign = -1 if (t["direction"] or "buy").lower() != "buy" else 1
    amt = t["amount_usd"] or 0
    real = t["pnl_usd"] or 0
    f10 = fwd_ret(t["instrument_id"], t["created_at"], 10)
    if f10 is None:
        continue
    n += 1
    tot_real += real
    f10_net = sign * f10 * amt - (0.0055 * amt if t["asset_class"] != "crypto" else 0.045 * amt)
    tot_10d += f10_net
    both += 1
    if real < f10_net - 0.5:
        under += 1
    elif real > f10_net + 0.5:
        over += 1
if n:
    print(f"  trades w/ fwd data: {n}")
    print(f"  avg realized: ${tot_real/n:+.2f}   avg 10d-hold-net: ${tot_10d/n:+.2f}")
    print(f"  exits UNDER 10d-hold: {under} ({100*under/n:.0f}%)   OVER: {over} ({100*over/n:.0f}%)   within $0.50: {n-under-over}")

# 5d horizon comparison for holdout dip-buys
DIP = ("MACD_TURN_BELOW_SMA20", "RSI_EXTREME_OVERSOLD", "BB_LOW")
print("\n=== 5d vs 10d (holdout dip-buys) ===")
d5 = q("""
SELECT s.instrument_id, s.generated_at, s.signal_type, s.conviction, i.asset_class
FROM signals s LEFT JOIN instruments i ON i.instrument_id = s.instrument_id
WHERE s.generated_at >= '2026-08-26' AND s.price IS NOT NULL
""")
d5rec = []
for s in d5:
    if not any(d in (s["signal_type"] or "") for d in DIP):
        continue
    r5 = fwd_ret(s["instrument_id"], s["generated_at"], 5)
    r10 = fwd_ret(s["instrument_id"], s["generated_at"], 10)
    if r5 is None and r10 is None:
        continue
    cost = COST_PCT.get(s["asset_class"] or "stock", 0.0055)
    d5rec.append({"ts": s["generated_at"], "regime": regime_for(s["generated_at"]),
                  "r5_net": r5 - cost if r5 is not None else None,
                  "r10_net": r10 - cost if r10 is not None else None})
for label, recs2 in (("dip ALL", d5rec),
                     ("dip !DEFENSIVE", [r for r in d5rec if r["regime"] != "DEFENSIVE"])):
    v5 = [r["r5_net"] for r in recs2 if r["r5_net"] is not None]
    v10 = [r["r10_net"] for r in recs2 if r["r10_net"] is not None]
    if v5 and v10:
        print(f"  {label:22s} 5d net={sum(v5)/len(v5)*100:+.2f}% (n={len(v5)})   10d net={sum(v10)/len(v10)*100:+.2f}% (n={len(v10)})")
c.close()
print("\nDONE")
