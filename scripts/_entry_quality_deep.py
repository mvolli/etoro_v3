#!/usr/bin/env python3
"""Deep entry-quality analysis: why is WR 35%? Find pre-entry discriminators."""
import sqlite3, sys
from collections import defaultdict

DB = "data/trading.db"
c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
c.row_factory = sqlite3.Row

def q(sql, args=()):
    return [dict(r) for r in c.execute(sql, args).fetchall()]

def wr(n, w):
    return 100.0 * w / n if n else 0.0

# ---- base population: CLOSED, post-Zaesur, with signal ----
base = q("""
SELECT t.id, t.symbol, t.amount_usd, t.pnl_usd, t.pnl_pct, t.entry_price, t.exit_price,
       t.created_at, t.closed_at, t.signal_id, s.signal_type, s.conviction, s.score,
       s.rsi, s.macd_hist, s.bb_pct, s.price AS signal_price, s.generated_at,
       i.yfinance_symbol, i.asset_class, i.sector, i.atr_pct, i.adv_usd, i.market_cap
FROM trades t
JOIN signals s ON s.id = t.signal_id
LEFT JOIN instruments i ON i.instrument_id = t.instrument_id
WHERE t.status='CLOSED' AND t.created_at >= '2026-07-26'
""")
print(f"CLOSED post-Zaesur with signal: {len(base)}")
wins = [b for b in base if (b["pnl_usd"] or 0) > 0]
print(f"WR: {wr(len(base), len(wins)):.1f}%   sum pnl: ${sum(b['pnl_usd'] or 0 for b in base):.0f}")

# ---- 1. win/loss asymmetry ----
aw = sum(b["pnl_pct"] or 0 for b in base if (b["pnl_usd"] or 0) > 0) / max(len(wins), 1)
al = sum(b["pnl_pct"] or 0 for b in base if (b["pnl_usd"] or 0) <= 0) / max(len(base) - len(wins), 1)
print(f"\n1) AVG WIN pct: {aw:+.2f}% (n={len(wins)})   AVG LOSS pct: {al:+.2f}% (n={len(base)-len(wins)})")
print(f"   loss/win ratio (abs): {abs(al/aw) if aw else 0:.2f}")
# fee-adjusted breakeven WR
# breakeven WR p satisfies p*aw = (1-p)*|al|  => p = |al|/(aw+|al|)
print(f"   breakeven WR (gross, no fees): {100*abs(al)/(aw+abs(al)):.1f}%")

# ---- 2. regime at entry (reconstructed from system_log) ----
import re, datetime
reg_rows = q("""
SELECT ts, message FROM system_log
WHERE message LIKE 'Regime: %' AND ts >= '2026-07-20' ORDER BY ts
""")
regime_at = []  # (ts, regime) change points
last_reg = None
for r in reg_rows:
    m = re.match(r"Regime: (\w+)", r["message"])
    if m and m.group(1) != last_reg:
        regime_at.append((r["ts"], m.group(1)))
        last_reg = m.group(1)

def regime_for(ts):
    if not regime_at:
        return None
    reg = None
    for pts, rname in regime_at:
        if pts <= ts:
            reg = rname
        else:
            break
    return reg

print("\n2) WR by regime at entry (regime reconstructed from system_log):")
by_reg = defaultdict(lambda: [0, 0, 0.0])
for b in base:
    reg = regime_for(b["created_at"])
    if reg:
        g = by_reg[reg]
        g[0] += 1
        if (b["pnl_usd"] or 0) > 0: g[1] += 1
        g[2] += b["pnl_usd"] or 0
for reg, (n, w, s) in sorted(by_reg.items(), key=lambda kv: -kv[1][0]):
    print(f"   {reg:18s} n={n:4d} WR={wr(n,w):5.1f}%  sum=${s:+8.0f}")
# regime x dip-buy
print("   regime x signal-type (dip-buy combos only, n>=10):")
by_regtype = defaultdict(lambda: [0, 0, 0.0])
DIP = ("MACD_TURN_BELOW_SMA20", "RSI_EXTREME_OVERSOLD", "BB_LOW")
for b in base:
    if any(d in (b["signal_type"] or "") for d in DIP):
        reg = regime_for(b["created_at"]) or "?"
        g = by_regtype[(reg, b["signal_type"])]
        g[0] += 1
        if (b["pnl_usd"] or 0) > 0: g[1] += 1
        g[2] += b["pnl_usd"] or 0
for (reg, t), (n, w, s) in sorted(by_regtype.items()):
    if n >= 10:
        print(f"     {reg:12s} {t[:44]:44s} n={n:4d} WR={wr(n,w):5.1f}% sum=${s:+7.0f}")

# ---- 3. per-signal-type with win/loss asymmetry ----
print("\n3) By signal type:")
by_type = defaultdict(lambda: [0, 0, 0.0, 0.0, 0.0])
for b in base:
    g = by_type[b["signal_type"]]
    g[0] += 1
    if (b["pnl_usd"] or 0) > 0: g[1] += 1
    g[2] += b["pnl_usd"] or 0
    g[3] += b["pnl_pct"] or 0
    if (b["pnl_usd"] or 0) <= 0: g[4] += b["pnl_pct"] or 0
for t, (n, w, s, wp, lp) in sorted(by_type.items(), key=lambda kv: -kv[1][0]):
    print(f"   {t[:52]:52s} n={n:4d} WR={wr(n,w):5.1f}% sum=${s:+7.0f} avgWin%={wp/max(w,1):+.2f} avgLoss%={lp/max(n-w,1):+.2f}")

# ---- 4. pre-entry indicators: discriminative power ----
# For each candidate pre-entry feature, compare wins vs losses (median + simple AUC)
def auc_by(feature_vals_win, feature_vals_loss):
    # AUC of P(positive) where higher feature = positive
    w = sorted(feature_vals_win); l = sorted(feature_vals_loss)
    if not w or not l: return None
    # rank-based
    import math
    # count pairs
    # O(n*m) ok for ~500
    gt = eq = 0
    for xw in w:
        for xl in l:
            if xw > xl: gt += 1
            elif xw == xl: eq += 1
    return 0.5 * (2*gt + eq) / (len(w)*len(l))

# features available at entry (from signals row + price at trade creation)
feats = {
    "signal score": "score",
    "rsi": "rsi",
    "bb_pct": "bb_pct",
    "amount_usd": "amount_usd",
    "atr_pct": "atr_pct",
    "adv_usd": "adv_usd",
}
print("\n4) Discriminative power (AUC, 0.5=noise, 1=perfect):")
for name, col in feats.items():
    wv = [b[col] for b in base if b[col] is not None and (b["pnl_usd"] or 0) > 0]
    lv = [b[col] for b in base if b[col] is not None and (b["pnl_usd"] or 0) <= 0]
    a = auc_by(wv, lv)
    if a is not None:
        print(f"   {name:14s} AUC={a:.3f} (higher=win)  -> best direction: {'higher' if a>0.5 else 'lower'}, |AUC-0.5|={abs(a-0.5):.3f}")

# ATR band cut (current atr_pct is a snapshot — descriptive only, see skill rule 8)
print("   ATR% bands (current snapshot, descriptive):")
for lo, hi in [(0,1.5),(1.5,3),(3,5),(5,10),(10,100)]:
    sub=[b for b in base if b["atr_pct"] is not None and lo<=b["atr_pct"]<hi]
    if sub:
        w=sum(1 for b in sub if (b["pnl_usd"] or 0)>0)
        s=sum(b["pnl_usd"] or 0 for b in sub)
        print(f"     ATR {lo}-{hi}%: n={len(sub):4d} WR={wr(len(sub),w):5.1f}% sum=${s:+7.0f}")

# ADV band cut
print("   ADV bands (current snapshot, descriptive):")
for lo, hi in [(0,500_000),(500_000,2_000_000),(2_000_000,20_000_000),(20_000_000,10**12)]:
    sub=[b for b in base if b["adv_usd"] is not None and lo<=b["adv_usd"]<hi]
    if sub:
        w=sum(1 for b in sub if (b["pnl_usd"] or 0)>0)
        s=sum(b["pnl_usd"] or 0 for b in sub)
        print(f"     ADV ${lo//1000_000}-{hi//1000_000 if hi<10**12 else 'inf'}M: n={len(sub):4d} WR={wr(len(sub),w):5.1f}% sum=${s:+7.0f}")

# ---- 5. entry->exit latency: how fast do losers die? ----
import datetime
print("\n5) Hold time by outcome (hours):")
for label, subset in [("WIN", [b for b in base if (b["pnl_usd"] or 0) > 0]),
                      ("LOSS", [b for b in base if (b["pnl_usd"] or 0) <= 0])]:
    hs = []
    for b in subset:
        try:
            t0 = datetime.datetime.fromisoformat(b["created_at"])
            t1 = datetime.datetime.fromisoformat(b["closed_at"])
            hs.append((t1 - t0).total_seconds() / 3600)
        except Exception:
            pass
    if hs:
        hs.sort()
        print(f"   {label}: n={len(hs)} median={hs[len(hs)//2]:.0f}h p25={hs[len(hs)//4]:.0f}h p75={hs[3*len(hs)//4]:.0f}h")

# ---- 6. same-instrument repeaters ----
print("\n6) Repeat entries on same symbol (within 21d of prior close):")
by_sym = defaultdict(list)
for b in base:
    by_sym[b["symbol"]].append(b)
repeat_stats = {"repeat": [0, 0, 0.0], "first": [0, 0, 0.0]}
for sym, bs in by_sym.items():
    bs.sort(key=lambda b: b["created_at"] or "")
    seen = set()
    for i, b in enumerate(bs):
        is_repeat = any(
            j != i and (b["created_at"] or "")[:10] >= (x["closed_at"] or "9999")[:10]
            for j, x in enumerate(bs)
        )
        # simpler: any OTHER trade on same symbol whose created_at is within 21 days before or after
        others = [x for x in bs if x is not b]
        is_repeat = False
        try:
            dt0 = datetime.datetime.fromisoformat(b["created_at"])
            for x in others:
                dtx = datetime.datetime.fromisoformat(x["created_at"])
                if abs((dt0 - dtx).days) <= 21:
                    is_repeat = True; break
        except Exception:
            pass
        g = repeat_stats["repeat" if is_repeat else "first"]
        g[0] += 1
        if (b["pnl_usd"] or 0) > 0: g[1] += 1
        g[2] += b["pnl_usd"] or 0
for label, (n, w, s) in repeat_stats.items():
    print(f"   {label:6s}: n={n:4d} WR={wr(n,w):5.1f}% sum=${s:+7.0f}")

# ---- 7. hour-of-day / day-of-week entry ----
print("\n7) By entry hour (UTC) — n>=15 only:")
by_hour = defaultdict(lambda: [0, 0, 0.0])
for b in base:
    try:
        h = datetime.datetime.fromisoformat(b["created_at"]).hour
    except Exception:
        continue
    g = by_hour[h]; g[0] += 1
    if (b["pnl_usd"] or 0) > 0: g[1] += 1
    g[2] += b["pnl_usd"] or 0
for h in sorted(by_hour):
    n, w, s = by_hour[h]
    if n >= 15:
        print(f"   {h:02d}:00  n={n:4d} WR={wr(n,w):5.1f}% sum=${s:+7.0f}")

print("\n7b) By day of week:")
by_dow = defaultdict(lambda: [0, 0, 0.0])
for b in base:
    try:
        d = datetime.datetime.fromisoformat(b["created_at"]).weekday()
    except Exception:
        continue
    names = ["Mon","Tue","Wed","Thu","Fri","Sat","Sun"]
    g = by_dow[names[d]]; g[0] += 1
    if (b["pnl_usd"] or 0) > 0: g[1] += 1
    g[2] += b["pnl_usd"] or 0
for d, (n, w, s) in by_dow.items():
    print(f"   {d}: n={n:4d} WR={wr(n,w):5.1f}% sum=${s:+7.0f}")

# ---- 8. signal age at trade (generated_at -> created_at) ----
print("\n8) Signal age at execution (hours):")
ages_win, ages_loss = [], []
for b in base:
    try:
        t0 = datetime.datetime.fromisoformat(b["generated_at"])
        t1 = datetime.datetime.fromisoformat(b["created_at"])
        age = (t1 - t0).total_seconds() / 3600
        (ages_win if (b["pnl_usd"] or 0) > 0 else ages_loss).append(age)
    except Exception:
        pass
ages_win.sort(); ages_loss.sort()
print(f"   WIN  median={ages_win[len(ages_win)//2]:.1f}h  (n={len(ages_win)})")
print(f"   LOSS median={ages_loss[len(ages_loss)//2]:.1f}h  (n={len(ages_loss)})")
# binned
bins = [(0,2),(2,6),(6,12),(12,24),(24,1000)]
print("   age bins:")
for lo, hi in bins:
    n = w = 0; s = 0.0
    for b in base:
        try:
            t0 = datetime.datetime.fromisoformat(b["generated_at"])
            t1 = datetime.datetime.fromisoformat(b["created_at"])
            age = (t1 - t0).total_seconds() / 3600
        except Exception:
            continue
        if lo <= age < hi:
            n += 1
            if (b["pnl_usd"] or 0) > 0: w += 1
            s += b["pnl_usd"] or 0
    lbl = f"{lo}-{hi if hi<1000 else '+'}h"
    print(f"     {lbl:9s} n={n:4d} WR={wr(n,w):5.1f}% sum=${s:+7.0f}")

# ---- 9. asset class / sector ----
print("\n9) By asset class:")
by_cls = defaultdict(lambda: [0, 0, 0.0])
for b in base:
    g = by_cls[b["asset_class"] or "unknown"]; g[0] += 1
    if (b["pnl_usd"] or 0) > 0: g[1] += 1
    g[2] += b["pnl_usd"] or 0
for t, (n, w, s) in sorted(by_cls.items(), key=lambda kv: -kv[1][0]):
    print(f"   {t:12s} n={n:4d} WR={wr(n,w):5.1f}% sum=${s:+7.0f}")

print("\n9b) Top 10 losing sectors / bottom 10 winning sectors (n>=8):")
by_sec = defaultdict(lambda: [0, 0, 0.0])
for b in base:
    g = by_sec[b["sector"] or "unknown"]; g[0] += 1
    if (b["pnl_usd"] or 0) > 0: g[1] += 1
    g[2] += b["pnl_usd"] or 0
secs = [(s, n, w, s_2) for s, (n, w, s_2) in by_sec.items() if n >= 8]
for s, n, w, s_2 in sorted(secs, key=lambda kv: kv[3])[:10]:
    print(f"   WORST {s[:38]:38s} n={n:4d} WR={wr(n,w):5.1f}% sum=${s_2:+7.0f}")
print("   ...")
for s, n, w, s_2 in sorted(secs, key=lambda kv: -kv[3])[:10]:
    print(f"   BEST  {s[:38]:38s} n={n:4d} WR={wr(n,w):5.1f}% sum=${s_2:+7.0f}")

c.close()
print("\nDONE")
