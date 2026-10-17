#!/usr/bin/env python3
"""E9-Test — Ausstiege und Kapital fuer Q2 (E8) + Earnings-Katalysator S1 (E6), nur lesend.

Vorregistrierung: docs/vorregistrierung-e9-ausstieg-kapital-2026-10-17.md (18ec5e3).
Kandidaten exakt nach E8 (Q2) und E6 (S1); Kurse data/e7_cache/bars/,
Earnings-Termine data/e6_cache/earn/.

  /usr/bin/python3 scripts/e9_exit_capital_test.py
"""
from __future__ import annotations

import bisect
import json
import math
import os
import random
import sys
from collections import defaultdict
from datetime import date, timedelta

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import e6_earnings_catalyst_test as E6  # noqa: E402
import e8_quality_growth_test as E8  # noqa: E402

HOLDOUT_FROM, END = "2020-11-11", "2026-10-15"
STOP, TRAIL, SMA_N, X2_MIN_DAY = 0.15, 0.20, 50, 10
CAPS = {"X0": 126, "X1": 252, "X2": 252, "X3": 252}
CAPITAL, FEE, SPREAD = 4600.0, 1.0, 0.002
KS = (4, 8, 12)
RUNS = 200
SEED = 17


# ── Kandidaten ───────────────────────────────────────────────────────────────

def q2_candidates(uni, bars):
    """{entry_date: [(score, ticker), ...]} — E8-Q2-Regeln, nur Titel aus S&P 500/400."""
    tcik = E8.ticker_cik(bars)
    filings, tsic = E8.build_filings(tcik)
    nonfin = [t for t in bars if not E8.is_fin(tsic.get(t))]
    spy = bars["SPY"]
    cal = list(spy.index)
    axis = [d for d in cal if "2020-09-01" <= d <= END]
    month_ends = [axis[i] for i in range(len(axis)) if i + 1 == len(axis) or axis[i + 1][:7] != axis[i][:7]]
    month_ends = [m for m in month_ends if m < END]
    out = defaultdict(list)
    for M in month_ends:
        lo = (date.fromisoformat(M) - timedelta(days=E8.MAX_AGE_DAYS)).isoformat()
        s = {}
        for t in nonfin:
            fl = filings.get(t)
            if not fl:
                continue
            i = bisect.bisect_left(fl, (M,)) - 1
            if i >= 0 and fl[i][0] >= lo and M in bars[t].index:
                s[t] = fl[i][1]
        if len(s) < 50:
            continue

        def pr(key, inv=False):
            items = sorted(s.items(), key=lambda kv: kv[1][key]); n = len(items)
            r = {t: (k + 1) / n for k, (t, _) in enumerate(items)}
            return {t: 1 - v + 1 / n for t, v in r.items()} if inv else r
        r1, r2, r3 = pr("gpa"), pr("acc", True), pr("growth")
        score = {t: (r1[t] + r2[t] + r3[t]) / 3 for t in s}
        thr = np.percentile(list(score.values()), 90)
        top = [t for t, v in score.items() if v >= thr]
        mom = {}
        for t in s:
            b = bars[t]; k = b.index.get_loc(M)
            if k >= 252:
                mom[t] = b.Close.iloc[k - 21] / b.Close.iloc[k - 252] - 1
        med = np.median(list(mom.values())) if mom else None
        q2 = [t for t in top if med is not None and t in mom and mom[t] > med and uni.get(t) == "large_mid"]
        k = bisect.bisect_right(cal, M)
        if k < len(cal) and HOLDOUT_FROM <= cal[k] <= END:
            out[cal[k]] += [(score[t], t) for t in q2]
    return out


def s1_candidates(uni, bars):
    """{entry_date: [ticker, ...]} — E6-S1-Regeln (ohne Ratings), S&P 500/400."""
    spy = bars["SPY"]; sd = list(spy.index); sc = spy.Close.to_numpy()
    out = defaultdict(list)
    for t, b in bars.items():
        if uni.get(t) != "large_mid":
            continue
        dates = list(b.index); c = b.Close.to_numpy()
        sma200 = b.Close.rolling(200).mean().to_numpy()
        for ed, bmo, surprise in E6.load_earn(t):
            if not (E6.START <= ed <= END):
                continue
            k0 = bisect.bisect_left(dates, ed)
            if k0 >= len(dates):
                continue
            if not bmo or dates[k0] != ed:
                k0 = k0 + 1 if dates[k0] == ed else k0
            if k0 < 1 or k0 + 2 >= len(dates):
                continue
            i = bisect.bisect_left(sd, dates[k0 - 1]); j = bisect.bisect_left(sd, dates[k0])
            if j >= len(sd) or i >= len(sd):
                continue
            ear = c[k0] / c[k0 - 1] - 1 - (sc[j] / sc[i] - 1)
            d1 = k0 + 1
            if not (surprise >= E6.SURPRISE_MIN and ear >= E6.EAR_MIN):
                continue
            if not (math.isfinite(sma200[d1]) and c[d1] > sma200[d1]):
                continue
            e = d1 + 1
            if HOLDOUT_FROM <= dates[e] <= END:
                out[dates[e]].append(t)
    return out


def earnings_dates(t):
    """Alle Termine (auch ohne Schaetzung) aus dem E6-Cache, sortiert."""
    p = os.path.join(E6.C, "earn", f"{t}.csv")
    if not os.path.exists(p) or os.path.getsize(p) == 0:
        return []
    df = pd.read_csv(p)
    out = set()
    for v in df[df.columns[0]]:
        try:
            out.add(pd.Timestamp(v).strftime("%Y-%m-%d"))
        except Exception:  # noqa: BLE001
            pass
    return sorted(out)


# ── Ausstiege ────────────────────────────────────────────────────────────────

class Path:
    __slots__ = ("t", "d0", "d1", "g", "marks")

    def __init__(self, t, d0, d1, g, marks):
        self.t, self.d0, self.d1, self.g, self.marks = t, d0, d1, g, marks


def exit_path(b, arr, e, variant, edates):
    """Ausstieg ab Einstiegsindex e. Rueckgabe Path (marks: {date: Kurs/Einstieg})."""
    dates, o, l, c, sma = arr
    ep = o[e]
    cap = CAPS[variant]
    last = min(e + cap - 1, len(dates) - 1)
    while dates[last] > END:
        last -= 1
    nxt_e = None
    if variant == "X1":
        k = bisect.bisect_right(edates, dates[e])
        if k < len(edates):
            j = bisect.bisect_left(dates, edates[k]) - 1      # letzter Handelstag vor dem Termin
            if j >= e:
                nxt_e = j
    peak = c[e]
    xi, xp = last, c[last]
    for j in range(e, last + 1):
        lvl = ep * (1 - STOP)
        if variant == "X3" and j > e:
            lvl = max(lvl, peak * (1 - TRAIL))
        if l[j] <= lvl:
            xi, xp = j, min(o[j], lvl)
            break
        if variant == "X1" and nxt_e is not None and j == nxt_e:
            xi, xp = j, c[j]
            break
        if (variant == "X2" and j - e + 1 >= X2_MIN_DAY and j + 1 <= last
                and math.isfinite(sma[j]) and c[j] < sma[j]):
            xi, xp = j + 1, o[j + 1]
            break
        peak = max(peak, c[j])
    marks = {dates[j]: c[j] / ep for j in range(e, xi)}
    marks[dates[xi]] = xp / ep
    return Path(None, dates[e], dates[xi], xp / ep - 1, marks)


# ── Portfolio ────────────────────────────────────────────────────────────────

def simulate(cal, cands, paths, k, rng):
    T = CAPITAL / k
    cost = 2 * FEE + SPREAD * T
    held = {}            # ticker -> Path
    realized = 0.0
    equity = []
    n_trades = 0
    for d in cal:
        for t in [t for t, p in held.items() if p.d1 == d]:
            p = held.pop(t)
            realized += T * p.g - cost
        today = cands.get(d)
        if today:
            q2 = sorted([c for c in today if c[0] == "Q2"], key=lambda c: -c[1])
            s1 = [c for c in today if c[0] == "S1"]
            keys = sorted(rng.random() for _ in q2)
            order = list(zip(keys, q2)) + [(rng.random(), c) for c in s1]
            order.sort(key=lambda x: x[0])
            for _, c in order:
                if len(held) >= k:
                    break
                t = c[2]
                if t in held:
                    continue
                p = paths.get((c[0], t, d))
                if p is None:
                    continue
                if p.d1 == d:                                  # Ein-Tages-Trade
                    realized += T * p.g - cost; n_trades += 1
                    continue
                held[t] = p; n_trades += 1
        unreal = sum(T * (p.marks.get(d, None) or _last_mark(p, d)) - T for p in held.values())
        equity.append(CAPITAL + realized + unreal - cost * len(held))
    return np.array(equity), n_trades


def _last_mark(p, d):
    ks = [k for k in p.marks if k <= d]
    return p.marks[max(ks)] if ks else 1.0


def metrics(cal, eq, n_trades, k):
    yrs = len(cal) / 252.0
    ann = (eq[-1] / CAPITAL) ** (1 / yrs) - 1
    peak = np.maximum.accumulate(eq); mdd = float(((eq - peak) / peak).min())
    by_year = {}
    for i, d in enumerate(cal):
        by_year.setdefault(d[:4], [i, i])[1] = i
    yr = {}
    prev = CAPITAL
    for y in sorted(by_year):
        end = eq[by_year[y][1]]
        yr[y] = end / prev - 1; prev = end
    roll = eq[126:] / eq[:-126] - 1 if len(eq) > 126 else np.array([0.0])
    T = CAPITAL / k
    return dict(ann=ann, mdd=mdd, yr=yr, loss_years=sum(1 for v in yr.values() if v < 0), n_years=len(yr),
                q5_6m=float(np.percentile(roll, 5)), trades_py=n_trades / yrs,
                cost_py=n_trades / yrs * (2 * FEE + SPREAD * T))


def main() -> int:
    uni = json.load(open(os.path.join(E8.C7, "universe.json")))
    bars = {t: E8.load_bars(t) for t in uni}
    bars = {t: b for t, b in bars.items() if b is not None and len(b) > 60}
    bars["SPY"] = E8.load_bars("SPY")
    cal = [d for d in bars["SPY"].index if HOLDOUT_FROM <= d <= END]
    print(f"Holdout {cal[0]}..{cal[-1]} ({len(cal)} Handelstage), Kapital ${CAPITAL:,.0f}")

    q2 = q2_candidates(uni, {t: b for t, b in bars.items()})
    s1 = s1_candidates(uni, bars)
    cands = defaultdict(list)
    for d, lst in q2.items():
        cands[d] += [("Q2", sc, t) for sc, t in lst]
    for d, lst in s1.items():
        cands[d] += [("S1", 0.0, t) for t in lst]
    print(f"Kandidaten: Q2 {sum(len(v) for v in q2.values())} an {len(q2)} Stichtagen, "
          f"S1 {sum(len(v) for v in s1.values())} Ereignisse")

    arrs, edates = {}, {}
    for t in {c[2] for v in cands.values() for c in v}:
        b = bars[t]
        arrs[t] = (list(b.index), b.Open.to_numpy(), b.Low.to_numpy(), b.Close.to_numpy(),
                   b.Close.rolling(SMA_N).mean().to_numpy())
        edates[t] = earnings_dates(t)

    results = {}
    for var in CAPS:
        paths = {}
        for d, lst in cands.items():
            for kind, _, t in lst:
                dates = arrs[t][0]
                e = bisect.bisect_left(dates, d)
                if e >= len(dates) or dates[e] != d:
                    continue
                paths[(kind, t, d)] = exit_path(bars[t], arrs[t], e, var, edates[t])
        # Einzeltrades (alle Kandidaten, je Titel ohne Ueberlappung) — beschreibend
        gs = [p.g - (2 * FEE / (CAPITAL / 8) + SPREAD) for p in paths.values()]
        hold = [len(p.marks) for p in paths.values()]
        print(f"\n== {var}: {len(paths)} Kandidaten-Pfade, Ø netto je Trade {np.mean(gs)*100:+.2f} % "
              f"(Median {np.median(gs)*100:+.2f} %, Gewinner {np.mean([g > 0 for g in gs])*100:.0f} %), "
              f"Ø Haltedauer {np.mean(hold):.0f} Tage")
        for k in KS:
            runs = []
            for r in range(RUNS):
                eq, n = simulate(cal, cands, paths, k, random.Random(SEED + r))
                runs.append(metrics(cal, eq, n, k))
            runs.sort(key=lambda m: m["ann"])
            med = runs[len(runs) // 2]
            results[(var, k)] = dict(med=med, ann_med=float(np.median([m["ann"] for m in runs])),
                                     ann_p10=runs[len(runs) // 10]["ann"], ann_p90=runs[len(runs) * 9 // 10]["ann"],
                                     mdd_med=float(np.median([m["mdd"] for m in runs])))
            print(f"   k={k:2d} (T ${CAPITAL / k:,.0f}): Jahresrendite Median {results[(var, k)]['ann_med']*100:+.1f} % "
                  f"(10–90 %: {results[(var, k)]['ann_p10']*100:+.1f} … {results[(var, k)]['ann_p90']*100:+.1f} %), "
                  f"max. DD {results[(var, k)]['mdd_med']*100:.1f} %, Verlustjahre {med['loss_years']}/{med['n_years']}, "
                  f"6M-Rendite 5 %-Quantil {med['q5_6m']*100:+.1f} %, Trades/Jahr {med['trades_py']:.0f}, "
                  f"Kosten/Jahr ${med['cost_py']:.0f}")
            print("      Jahre (Median-Lauf): " + ", ".join(f"{y} {v*100:+.1f}%" for y, v in med["yr"].items()))

    spy = bars["SPY"]; i0 = list(spy.index).index(cal[0]); i1 = list(spy.index).index(cal[-1])
    spy_tot = spy.Close.iloc[i1] / spy.Open.iloc[i0]
    print(f"\nSPY Buy & Hold: Jahresrendite {(spy_tot ** (252 / len(cal)) - 1)*100:+.1f} %")

    base = results[("X0", 8)]
    best = None
    for var in ("X1", "X2", "X3"):
        r = results[(var, 8)]
        yrs = [y for y in r["med"]["yr"] if y in base["med"]["yr"]]
        ge = sum(1 for y in yrs if r["med"]["yr"][y] >= base["med"]["yr"][y])
        c1 = r["ann_med"] - base["ann_med"] >= 0.01
        c2 = r["mdd_med"] >= base["mdd_med"] - 0.02
        c3 = ge >= math.ceil(len(yrs) * 2 / 3)
        ok = c1 and c2 and c3
        print(f"{var} vs X0 (k=8): Δ Jahresrendite {(r['ann_med']-base['ann_med'])*100:+.1f} pp "
              f"{'✓' if c1 else '✗'}, Δ DD {(r['mdd_med']-base['mdd_med'])*100:+.1f} pp {'✓' if c2 else '✗'}, "
              f"Jahre ≥ X0 {ge}/{len(yrs)} {'✓' if c3 else '✗'} -> {'ANGENOMMEN' if ok else 'verworfen'}")
        if ok and (best is None or r["ann_med"] > results[(best, 8)]["ann_med"]):
            best = var
    print(f"\nAusstieg: {best or 'X0 (Basis bleibt)'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
