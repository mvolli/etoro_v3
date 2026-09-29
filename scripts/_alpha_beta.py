#!/usr/bin/env python3
"""Beta vs Alpha decomposition (advisor round 2, the single next experiment).

Only BUY signals. Excess return:
  excess10 = fwd10(symbol) - fwd10(equal-weight market, same start date)
Entry price = OPEN of the NEXT trading day (no look-ahead).
Aggregation: per-day mean first, then over days. CI = block-bootstrap on days.
Folds: monthly walk-forward. Regime (system_log) at signal time vs market fwd.

Also: real cost measurement — fill prices vs daily OHLC (slippage/spread).
"""
import sqlite3, statistics, math, random
from collections import defaultdict

DB = "/home/mvolli/.hermes/workspace/etoro_v3/data/trading.db"
c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
c.row_factory = sqlite3.Row
def q(sql, args=()):
    return [dict(r) for r in c.execute(sql, args).fetchall()]

# ---------- price data ----------
print("loading bars...")
bars = {}
for r in q("SELECT i.symbol AS symbol, o.date AS date, o.open AS open, o.close AS close FROM ohlcv_daily o JOIN instruments i ON i.instrument_id=o.instrument_id"):
    s = r["symbol"]
    if not s: continue
    bars.setdefault(s, []).append((r["date"], r["open"], r["close"]))
for iid in bars: bars[iid].sort()
dates = sorted(set(r["date"] for r in q("SELECT DISTINCT date FROM ohlcv_daily")))
idx = {d: k for k, d in enumerate(dates)}
N = len(dates)

def fwd_close(sym, date, h=10):
    """close-to-close forward h trading days (market baseline)."""
    if sym not in bars or date not in idx: return None
    a = bars[sym]
    pmap = {x[0]: x[2] for x in a}
    i = idx[date]
    if i + h >= N: return None
    d1 = dates[i + h]
    p0 = pmap.get(date); p1 = pmap.get(d1)
    return (p1 / p0 - 1) if (p0 and p1) else None

def fwd_entry(sym, date, h=10):
    """entry = open of the NEXT trading day after signal; exit close h days after that."""
    if sym not in bars or date not in idx: return None
    a = bars[sym]
    pmap = {x[0]: (x[1], x[2]) for x in a}
    i = idx[date]
    if i + 1 >= N: return None
    if dates[i+1] not in pmap or not pmap.get(dates[i+1]): return None
    p0 = pmap[dates[i+1]][0]
    # close h trading days AFTER entry day
    j = i + 1
    if j + h >= N: return None
    d1 = dates[j + h]
    p1 = pmap.get(d1, (None, None))[1]
    return (p1 / p0 - 1) if (p0 and p1) else None

# market baseline cache: (date) -> mean fwd10 close across all instruments
print("computing market baseline...")
mkt_cache = {}
all_syms = [s for s in bars if len(bars[s]) >= 60]
for di, d in enumerate(dates):
    if di + 10 >= N: break
    vals = []
    for s in all_syms:
        v = fwd_close(s, d, 10)
        if v is not None: vals.append(v)
    mkt_cache[d] = (sum(vals) / len(vals), len(vals)) if vals else (None, 0)

# ---------- signals ----------
BUY_TYPES = {
    "MACD_TURN_BELOW_SMA20,BB_LOW_MACD_IMPROVING",
    "RSI_EXTREME_OVERSOLD,MACD_TURN_BELOW_SMA20",
    "RSI_EXTREME_OVERSOLD,MACD_TURN_BELOW_SMA20,BB_LOW_MACD_IMPROVING",
    "MACD_TURN_BELOW_SMA20,TREND_PULLBACK",
    "RSI_EXTREME_OVERSOLD,BB_LOW_MACD_IMPROVING",
}
TREND_TYPES = {"TREND_PULLBACK,GOLDEN_CROSS"}
SWEEP_TYPES = {"CORE_SWEEP"}

# regime at signal time (reconstructed from system_log)
reg_rows = q("""SELECT ts, message FROM system_log
                WHERE message LIKE 'Regime: %' AND ts >= '2026-07-20' ORDER BY ts""")
events = []
for r in reg_rows:
    m = r["message"].replace("Regime: ", "").strip()
    if " |" in m:
        m = m.split(" |")[0].strip()
    events.append((r["ts"], m))
def regime_at(ts):
    reg = "NORMAL"
    for t, m in events:
        if t <= ts: reg = m
        else: break
    return reg

recs = []
for r in q("""SELECT s.signal_type, s.generated_at, s.conviction, i.symbol
              FROM signals s JOIN instruments i ON i.instrument_id = s.instrument_id
              WHERE s.generated_at >= '2026-07-20' AND s.signal_type IS NOT NULL"""):
    fam = "other"
    for t in BUY_TYPES:
        if t in r["signal_type"]: fam = "dipbuy"; break
    if fam == "other" and r["signal_type"] in TREND_TYPES: fam = "trend"
    if fam == "other" and r["signal_type"] in SWEEP_TYPES: fam = "sweep"
    if fam == "other": continue
    date = r["generated_at"][:10]
    if date not in idx: continue
    m10 = fwd_entry(r["symbol"], date, 10)
    mkt = mkt_cache.get(date)
    if m10 is None or not mkt or mkt[0] is None: continue
    recs.append(dict(fam=fam, date=date, sym=r["symbol"],
                     excess=m10 - mkt[0], ret=m10, regime=regime_at(r["generated_at"])))

print(f"buy-signal records with data: {len(recs)}")
# dedupe: keep one per (symbol, date, fam) — first
seen = set(); dd = []
for r in recs:
    k = (r["sym"], r["date"], r["fam"])
    if k in seen: continue
    seen.add(k); dd.append(r)
recs = dd
print(f"after dedupe (sym,date,fam): {len(recs)}")

def ci_excess(vals, B=4000, block=3, seed=42):
    """mean of the day-averaged series + moving-block bootstrap 95% CI + SE."""
    n = len(vals)
    if n < 8:
        return (None, None, None)
    rng = random.Random(seed)
    mean = sum(vals) / n
    se = statistics.stdev(vals) / math.sqrt(n) if n > 1 else 0.0
    boots = []
    for _ in range(B):
        draw = []
        pos = rng.randint(0, n - 1)
        while len(draw) < n:
            for _k in range(block):
                draw.append(vals[pos % n])
                pos += 1
        boots.append(sum(draw) / len(draw))
    boots.sort()
    lo = boots[max(0, int(0.025 * B))]
    hi = boots[min(B - 1, int(0.975 * B))]
    return mean, (lo, hi), se

def report(vals, label):
    if not vals:
        print(f"  {label:52s} n=0"); return
    res = ci_excess(vals)
    if res[0] is None:
        print(f"  {label:52s} days={len(vals):4d}  (too few to CI)")
        return
    mean, (lo, hi), se = res
    sd = statistics.pstdev(vals)
    print(f"  {label:52s} days={len(vals):4d} excess={100*mean:+5.2f}%  "
          f"CI95=[{100*lo:+.2f},{100*hi:+.2f}]  se={100*se:.2f}  sd={100*sd:.2f}")

# ---------- per-day aggregation ----------
print("\n=== EXCESS vs equal-weight market (buy signals, entry=next open, +10d) ===")
byfam = defaultdict(list)   # fam -> list of daily mean excess
byfam_date = defaultdict(lambda: defaultdict(list))
for r in recs:
    byfam_date[r["fam"]][r["date"]].append(r["excess"])
for fam, ds in byfam_date.items():
    daily = [sum(v) / len(v) for v in ds.values()]
    byfam[fam] = daily
for fam in ("dipbuy", "trend", "sweep"):
    report(byfam.get(fam, []), f"all months: {fam}")

# folds
folds = defaultdict(lambda: defaultdict(list))
for r in recs:
    mo = r["date"][:7]
    folds[mo][r["fam"]].append(r)
print("\n=== MONTHLY FOLDS (excess vs market) ===")
for mo in sorted(folds):
    print(f"--- {mo} ---")
    for fam in ("dipbuy", "trend", "sweep"):
        f = folds[mo].get(fam)
        if not f: continue
        d = defaultdict(list)
        for r in f: d[r["date"]].append(r["excess"])
        report([sum(v) / len(v) for v in d.values()],
               f"  {fam} (n_recs={len(f)})")

# ---------- regime as market predictor ----------
print("\n=== Does regime predict the MARKET 10d forward? ===")
byreg = defaultdict(list)
for r in recs:
    d = r["date"]
    v = mkt_cache.get(d)
    if v and v[0] is not None:
        byreg[r["regime"]].append((d, v[0]))
for reg, pairs in sorted(byreg.items()):
    d = defaultdict(list)
    for dt, v in pairs: d[dt].append(v)
    daily = [sum(v) / len(v) for v in d.values()]
    report(daily, f"regime={reg} market fwd10")

# ---------- real cost measurement ----------
print("\n=== REAL COSTS: fill price vs daily OHLC ===")
evs = q("""SELECT e.symbol, e.event_at, e.event_type, e.price, e.spread_pct, e.cost_usd, e.amount_usd
           FROM trade_events e WHERE e.price IS NOT NULL""")
opmap = {}
for r in q("SELECT i.symbol, o.date, o.open, o.low, o.high, o.close FROM ohlcv_daily o JOIN instruments i ON i.instrument_id=o.instrument_id"):
    opmap.setdefault(r["symbol"], {})[r["date"]] = (r["open"], r["low"], r["high"], r["close"])
slip_open = []; slip_close = []
for e in evs:
    day = e["event_at"][:10]
    m = opmap.get(e["symbol"], {}).get(day)
    if not m: continue
    opn, lo, hi, cls = m
    if e["event_type"] == "OPEN":
        if opn and e["price"] > 0:
            slip_open.append(e["price"] / opn - 1)  # >0 = paid above open (bad for buy)
    else:
        if cls and e["price"] > 0:
            slip_close.append(e["price"] / cls - 1)
def st(vals, label, clip=0.25):
    if vals:
        v = [x for x in vals if abs(x) < clip]
        print(f"  {label}: n={len(v)}/{len(vals)} mean={100*sum(v)/len(v):+.3f}% "
              f"median={100*statistics.median(v):+.3f}% min={100*min(v):+.3f}% max={100*max(v):+.3f}%")
    else:
        print(f"  {label}: n=0")
st(slip_open, "OPEN fill vs day open")
st(slip_close, "CLOSE fill vs day close")
sp = [e["spread_pct"] for e in evs if e["spread_pct"]]
print(f"  spread_pct (events): n={len(sp)} mean={sum(sp)/len(sp):.2f} median={statistics.median(sp):.2f} "
      f"min={min(sp):.2f} max={max(sp):.2f} (units as logged by bot)")
costs = [e["cost_usd"] for e in evs if e["cost_usd"]]
print(f"  cost_usd non-zero: n={len(costs)} sum={sum(costs):.2f}")
# gross vs net on executed trades, and average round-trip cost in bps of amount
tr = q("""SELECT t.pnl_usd, e.amount_usd, e.cost_usd
          FROM trades t LEFT JOIN trade_events e ON e.trade_id=t.id AND e.event_type='OPEN'
          WHERE t.status='CLOSED' AND t.closed_at>='2026-07-26'""")
gross = [r["pnl_usd"] for r in tr if r["pnl_usd"] is not None]
costs2 = [r["cost_usd"] for r in tr if r["cost_usd"]]
amts = [r["amount_usd"] for r in tr if r["amount_usd"] and r["cost_usd"]]
print(f"  executed trades: n={len(gross)} gross sum=${sum(gross):+.2f}  cost sum=${sum(costs2):+.2f}")
if amts:
    bps = [1e4 * (c / a) for c, a in zip(costs2, amts) if a > 0]
    if bps:
        print(f"  avg round-trip cost = {sum(bps)/len(bps):.1f} bps of amount (n={len(bps)})")
c.close()
print("\nDONE")
