#!/usr/bin/env python3
"""E8-Test — Qualitaet + Wachstum (+ Momentum), 30-126 Handelstage, nur lesend.

Vorregistrierung: docs/vorregistrierung-e8-qualitaet-wachstum-2026-10-17.md
(VOR diesem Lauf committet). Daten: data/e8_cache/ (scripts/e8_fetch_fsds.py),
Kurse data/e7_cache/bars/.

  /usr/bin/python3 scripts/e8_quality_growth_test.py --coverage   # nur Abdeckung, keine Renditen
  /usr/bin/python3 scripts/e8_quality_growth_test.py              # Test
"""
from __future__ import annotations

import bisect
import json
import math
import os
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
C8 = os.path.join(ROOT, "data", "e8_cache")
C7 = os.path.join(ROOT, "data", "e7_cache")

END = "2026-10-15"
AXIS_FROM = "2009-07-01"
COVERAGE_SMALL_FRAC, PLATEAU_FROM = 0.90, "2014-01-01"
SPLIT = 0.60
HORIZONS = (30, 63, 126)
STOP = 0.15
SIZE, FEE = 880.0, 1.0
SPREAD = {"large_mid": 0.002, "small": 0.005}
MAX_AGE_DAYS = 200
N_MIN, MONTHS_MIN, T_MIN, TRIM = 300, 40, 2.4, 0.05

REV_TAGS = ["Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax",
            "RevenueFromContractWithCustomerIncludingAssessedTax", "SalesRevenueNet",
            "SalesRevenueGoodsNet", "SalesRevenueServicesNet"]
COGS_TAGS = ["CostOfRevenue", "CostOfGoodsAndServicesSold", "CostOfGoodsSold", "CostOfServices"]
NI_TAGS = ["NetIncomeLoss", "ProfitLoss"]
CFO_TAGS = ["NetCashProvidedByUsedInOperatingActivities",
            "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations"]
FP_QTRS = {"Q1": 1, "Q2": 2, "Q3": 3, "FY": 4}


def cost(seg):
    return 2 * FEE / SIZE + SPREAD[seg]


def load_bars(t):
    p = os.path.join(C7, "bars", f"{t}.csv")
    if not os.path.exists(p) or os.path.getsize(p) == 0:
        return None
    df = pd.read_csv(p, index_col=0, parse_dates=True)
    df = df[(df.Close > 0) & (df.Open > 0) & (df.Low > 0)]
    df.index = df.index.strftime("%Y-%m-%d")
    return df


def ticker_cik(uni):
    ct = json.load(open(os.path.join(C8, "company_tickers.json")))
    m = {}
    for v in ct.values():
        m.setdefault(str(v["ticker"]).upper().replace(".", "-"), int(v["cik_str"]))
    return {t: m[t] for t in uni if t in m}


def ddiff(a, b):
    return (datetime.strptime(a, "%Y%m%d") - datetime.strptime(b, "%Y%m%d")).days


def filing_metrics(sub_row, vals):
    """vals: {(tag, ddate, qtrs): value | None (widerspruechliche Dublette)}"""
    q = FP_QTRS.get(sub_row["fp"])
    if q is None:
        return None
    per = sub_row["period"]

    def cur(tag, qt=q):
        return vals.get((tag, per, qt))

    def prior(tag, qt=q):
        for (tg, dd, qq), v in vals.items():
            if tg == tag and qq == qt and v is not None and 350 <= ddiff(per, dd) <= 380:
                return v
        return None

    def first(tags, qt=q):
        for tg in tags:
            v = cur(tg, qt)
            if v is not None:
                return tg, v
        return None, None

    assets = cur("Assets", 0)
    if assets is None or assets <= 0:
        return None
    rev_tag = rev = rev_prior = None
    for tg in REV_TAGS:
        c, p = cur(tg), prior(tg)
        if c is not None and p is not None:
            rev_tag, rev, rev_prior = tg, c, p
            break
    if rev is None or rev_prior is None or rev_prior <= 0:
        return None
    gp = cur("GrossProfit")
    if gp is None:
        _, cogs = first(COGS_TAGS)                   # gleiches ddate/qtrs wie der Umsatz
        if cogs is None:
            return None
        gp = rev - cogs
    _, ni = first(NI_TAGS)
    _, cfo = first(CFO_TAGS)
    if ni is None or cfo is None:
        return None
    scale = 4.0 / q
    return {"gpa": gp * scale / assets, "acc": (ni - cfo) * scale / assets, "growth": rev / rev_prior - 1}


def build_filings(tcik):
    cik_t = defaultdict(list)
    for t, c in tcik.items():
        cik_t[c].append(t)
    sub = pd.read_csv(os.path.join(C8, "sub.csv"), dtype=str)
    sub = sub[sub.form.isin(["10-K", "10-Q"])]
    sub["cik_i"] = sub.cik.astype(int)
    sub = sub[sub.cik_i.isin(cik_t)]
    sic = {}
    for _, r in sub.sort_values("filed").iterrows():
        try:
            sic[r.cik_i] = int(float(r.sic))
        except (TypeError, ValueError):
            pass
    keep = set(sub.adsh)
    vals = defaultdict(dict)
    for ch in pd.read_csv(os.path.join(C8, "num.csv"), dtype={"adsh": str, "tag": str, "ddate": str},
                          chunksize=2_000_000):
        ch = ch[ch.adsh.isin(keep)]
        for adsh, tag, dd, qt, v in zip(ch.adsh, ch.tag, ch.ddate, ch.qtrs, ch.value):
            try:
                k = (tag, str(dd)[:8], int(qt)); v = float(v)
            except (TypeError, ValueError):
                continue
            d = vals[adsh]
            if k in d and d[k] is not None and d[k] != v:
                d[k] = None                              # widerspruechliche Dublette -> fehlt
            elif k not in d:
                d[k] = v
    out = defaultdict(list)                              # ticker -> [(avail_date, metrics)]
    for _, r in sub.iterrows():
        m = filing_metrics(r, vals.get(r.adsh, {}))
        if m is None:
            continue
        acc = str(r.accepted)[:10]
        for t in cik_t[r.cik_i]:
            out[t].append((acc, m))
    for t in out:
        out[t].sort(key=lambda x: x[0])
    tsic = {t: sic.get(c) for t, c in tcik.items()}
    return out, tsic


def is_fin(s):
    return s is not None and 6000 <= s <= 6999


def nw_t(series, lags):
    x = np.asarray(series, float); T = len(x)
    if T < 3:
        return 0.0
    m = x.mean(); e = x - m
    var = (e @ e) / T
    for l in range(1, min(lags, T - 1) + 1):
        var += 2 * (1 - l / (lags + 1)) * (e[l:] @ e[:-l]) / T
    return m / math.sqrt(var / T) if var > 0 else 0.0


def trimmed(v):
    v = sorted(v); k = int(len(v) * TRIM)
    v = v[k:len(v) - k] if len(v) - 2 * k > 0 else v
    return sum(v) / len(v)


def ew_index(bar_map):
    rets = []
    for b in bar_map.values():
        r = b.Close.pct_change()
        rets.append(r[(r > -0.5) & (r < 1.0)])
    ew = pd.concat(rets, axis=1).mean(axis=1).fillna(0.0)
    idx = (1 + ew).cumprod()
    d = list(idx.index); v = idx.to_numpy()

    def ret(d0, d1):
        i = bisect.bisect_left(d, d0) - 1; j = bisect.bisect_right(d, d1) - 1
        return None if i < 0 or j <= i else v[j] / v[i] - 1
    return ret


def main() -> int:
    coverage_only = "--coverage" in sys.argv
    uni = json.load(open(os.path.join(C7, "universe.json")))
    bars = {t: load_bars(t) for t in uni}
    bars = {t: b for t, b in bars.items() if b is not None and len(b) > 60}
    spy = load_bars("SPY")
    tcik = ticker_cik(bars)
    filings, tsic = build_filings(tcik)
    nonfin = [t for t in bars if not is_fin(tsic.get(t))]
    print(f"Universum {len(bars)} Titel mit Kursen, CIK-Treffer {len(tcik)}, "
          f"Nicht-Finanz {len(nonfin)}, mit Kennzahlen {sum(1 for t in nonfin if filings.get(t))}")

    axis = sorted(set().union(*[set(b.index) for b in bars.values()]) | set(spy.index))
    axis = [d for d in axis if AXIS_FROM <= d <= END]
    month_ends = [axis[i] for i in range(len(axis)) if i + 1 == len(axis) or axis[i + 1][:7] != axis[i][:7]]
    month_ends = [m for m in month_ends if m < END]

    def snapshot(M):
        """ticker -> metrics (juengste Meldung mit accepted < M, <= 200 Tage alt)"""
        lo = (date.fromisoformat(M) - timedelta(days=MAX_AGE_DAYS)).isoformat()
        out = {}
        for t in nonfin:
            fl = filings.get(t)
            if not fl:
                continue
            i = bisect.bisect_left(fl, (M,)) - 1      # accepted-Datum < M
            if i >= 0 and fl[i][0] >= lo and M in bars[t].index:
                out[t] = fl[i][1]
        return out

    snaps, cov = {}, {}
    for M in month_ends:
        s = snapshot(M)
        live = [t for t in nonfin if M in bars[t].index]
        snaps[M] = s
        cov[M] = len(s) / len(live) if live else 0.0
    # Nachtrag 2 (vor dem Lauf): Start = erster Stichtag, an dem die Abdeckung der
    # S&P-600-Nicht-Finanzwerte >= 90 % ihres eigenen Medians ab 2014 erreicht
    # (Ende des XBRL-Phase-in; die urspruengliche 70-%-Schwelle lag im Plateau-Rauschen).
    cov_s = {}
    for M in month_ends:
        live_s = [t for t in nonfin if uni[t] == "small" and M in bars[t].index]
        cov_s[M] = sum(1 for t in live_s if t in snaps[M]) / len(live_s) if live_s else 0.0
    plateau = float(np.median([c for M, c in cov_s.items() if M >= PLATEAU_FROM]))
    start = next((M for M in month_ends if cov_s[M] >= COVERAGE_SMALL_FRAC * plateau), None)
    by_year = defaultdict(list); by_year_s = defaultdict(list)
    for M, c in cov.items():
        by_year[M[:4]].append(c); by_year_s[M[:4]].append(cov_s[M])
    print("Abdeckung je Jahr (Mittel): " + ", ".join(f"{y} {np.mean(v)*100:.0f}%" for y, v in sorted(by_year.items())))
    print("Abdeckung S&P 600 je Jahr: " + ", ".join(f"{y} {np.mean(v)*100:.0f}%" for y, v in sorted(by_year_s.items())))
    print(f"Plateau S&P 600 (Median ab {PLATEAU_FROM}): {plateau*100:.1f} %, Schwelle {COVERAGE_SMALL_FRAC*plateau*100:.1f} %"
          f" -> erster Stichtag {start}")
    if coverage_only or start is None:
        return 0

    months = [M for M in month_ends if M >= start]
    ent_axis = [d for d in axis if d > start]
    split = ent_axis[int(len(ent_axis) * SPLIT)]
    print(f"Achse {ent_axis[0]}..{ent_axis[-1]}, {len(months)} Stichtage, Holdout ab {split}")

    ew_nf = ew_index({t: bars[t] for t in nonfin})
    ew_all = ew_index(bars)
    sd = list(spy.index); so = spy.Open.to_numpy(); sc_ = spy.Close.to_numpy()
    closes = {t: (list(b.index), b.Close.to_numpy()) for t, b in bars.items()}
    _rs_cache = {}

    def rankset_ret(M, d0, d1):
        """Sekundaer: gleichgewichtete Rangmenge zum Stichtag M (Close vor d0 -> Close d1)."""
        key = (M, d0, d1)
        if key not in _rs_cache:
            rr = []
            for t in snaps[M]:
                dd, cc = closes[t]
                i = bisect.bisect_left(dd, d0) - 1; j = bisect.bisect_right(dd, d1) - 1
                if i >= 0 and j > i:
                    rr.append(cc[j] / cc[i] - 1)
            _rs_cache[key] = float(np.mean(rr)) if rr else None
        return _rs_cache[key]

    def spy_ret(d0, d1):
        i = bisect.bisect_left(sd, d0); j = bisect.bisect_right(sd, d1) - 1
        return None if i >= len(sd) or j < i else sc_[j] / so[i] - 1

    def pct_rank(d, key, invert=False):
        items = sorted(d.items(), key=lambda kv: kv[1][key])
        n = len(items)
        r = {t: (k + 1) / n for k, (t, _) in enumerate(items)}
        return {t: 1 - v + 1 / n for t, v in r.items()} if invert else r

    picks = {"Q1": {}, "Q2": {}}
    for M in months:
        s = snaps[M]
        if len(s) < 50:
            continue
        r1, r2, r3 = pct_rank(s, "gpa"), pct_rank(s, "acc", invert=True), pct_rank(s, "growth")
        score = {t: (r1[t] + r2[t] + r3[t]) / 3 for t in s}
        thr = np.percentile(list(score.values()), 90)
        top = [t for t, v in score.items() if v >= thr]
        mom = {}
        for t in s:
            b = bars[t]; k = b.index.get_loc(M)
            if k >= 252:
                mom[t] = b.Close.iloc[k - 21] / b.Close.iloc[k - 252] - 1
        med = np.median(list(mom.values())) if mom else None
        picks["Q1"][M] = top
        picks["Q2"][M] = [t for t in top if med is not None and t in mom and mom[t] > med]

    res = defaultdict(list)
    for var in ("Q1", "Q2"):
        for h in HORIZONS:
            busy = defaultdict(str)
            for M in months:
                for t in picks[var].get(M, []):
                    b = bars[t]; dates = list(b.index)
                    e = bisect.bisect_right(dates, M)
                    if e >= len(dates) or dates[e] <= busy[t]:
                        continue
                    x = e + h - 1
                    if x >= len(dates) or dates[x] > END:
                        continue
                    o = b.Open.to_numpy(); l = b.Low.to_numpy(); c = b.Close.to_numpy()
                    ep = o[e]; stop = ep * (1 - STOP); xp, xi = c[x], x
                    for j in range(e, x + 1):
                        if l[j] <= stop:
                            xp, xi = min(o[j], stop), j
                            break
                    g = xp / ep - 1
                    r_nf, r_all, r_spy = ew_nf(dates[e], dates[xi]), ew_all(dates[e], dates[xi]), spy_ret(dates[e], dates[xi])
                    if r_nf is None or r_all is None or r_spy is None:
                        continue
                    r_rs = rankset_ret(M, dates[e], dates[xi])
                    res[(var, h)].append(dict(t=t, d=dates[e], M=M, g=g, nf=r_nf, all=r_all, spy=r_spy, rs=r_rs,
                                              seg=uni[t], cost=cost(uni[t]), stop=xi < x))
                    busy[t] = dates[xi]

    verdicts = {}
    for var in ("Q1", "Q2"):
        for h in HORIZONS:
            rows = res[(var, h)]
            ho = [r for r in rows if r["d"] >= split]
            tr = [r for r in rows if r["d"] < split]
            if not ho:
                print(f"{var} H{h}: keine Holdout-Trades"); continue
            exv = [r["g"] - r["nf"] - r["cost"] for r in ho]
            n = len(exv); m = sum(exv) / n
            coh = defaultdict(list)
            for r, v in zip(ho, exv):
                coh[r["M"]].append(v)
            series = [np.mean(coh[k]) for k in sorted(coh)]
            t_ = nw_t(series, math.ceil(h / 21)); tm = trimmed(exv)
            by = defaultdict(list)
            for r, v in zip(ho, exv):
                by[r["t"]].append(v)
            tot = sum(exv)
            loo = m > 0 and all((tot - sum(v)) / (n - len(v)) > 0 for v in by.values() if n - len(v) > 0)
            ab = [r["g"] - r["cost"] for r in ho]; ma = sum(ab) / n
            yr = defaultdict(list)
            for r, v in zip(ho, exv):
                yr[r["d"][:4]].append(v)
            yrs_pos = sum(1 for v in yr.values() if np.mean(v) > 0)
            yrs_ok = yrs_pos >= math.ceil(len(yr) * 2 / 3)
            crit = [n >= N_MIN, len(coh) >= MONTHS_MIN, m > 0, t_ >= T_MIN, tm > 0, loo, yrs_ok]
            v_ = "UNENTSCHEIDBAR" if not (crit[0] and crit[1]) else ("ANGENOMMEN" if all(crit) else "verworfen")
            verdicts[(var, h)] = (v_, t_)
            wins = [x for x in ab if x > 0]; loss = [x for x in ab if x <= 0]
            print(f"\n── {var} | H {h} Handelstage — n={n} (Monate {len(coh)}), Trefferq. {len(wins)/n*100:.0f} %, "
                  f"Ø Gewinn {np.mean(wins)*100 if wins else 0:+.2f} %, Ø Verlust {np.mean(loss)*100 if loss else 0:+.2f} %, "
                  f"Stops {sum(r['stop'] for r in ho)}")
            print(f"   Excess vs. Universum o. Fin {m*100:+.2f} % (NW-t {t_:+.2f}, Trim {tm*100:+.2f} %, "
                  f"LOO {'✓' if loo else '✗'}, Jahre positiv {yrs_pos}/{len(yr)}) -> {v_}")
            print(f"   vs. Universum inkl. Fin {np.mean([r['g']-r['all']-r['cost'] for r in ho])*100:+.2f} % | "
                  f"vs. Rangmenge {np.mean([r['g']-r['rs']-r['cost'] for r in ho if r['rs'] is not None])*100:+.2f} % | "
                  f"vs. SPY {np.mean([r['g']-r['spy']-r['cost'] for r in ho])*100:+.2f} % | absolut {ma*100:+.2f} %")
            for s_ in SPREAD:
                ss = [v for r, v in zip(ho, exv) if r["seg"] == s_]
                if ss:
                    print(f"   {s_}: n={len(ss)}, Excess {np.mean(ss)*100:+.2f} %, "
                          f"absolut {np.mean([r['g']-r['cost'] for r in ho if r['seg'] == s_])*100:+.2f} %")
            print("   je Jahr Excess: " + ", ".join(f"{y} {np.mean(v)*100:+.1f}% (n={len(v)})" for y, v in sorted(yr.items())))
            print(f"   schlechteste 5 %: {np.percentile(ab, 5)*100:+.1f} %, beste 5 %: {np.percentile(ab, 95)*100:+.1f} %")
            if tr:
                print(f"   [Training] n={len(tr)} Excess {np.mean([r['g']-r['nf']-r['cost'] for r in tr])*100:+.2f} %, "
                      f"absolut {np.mean([r['g']-r['cost'] for r in tr])*100:+.2f} %")
    acc = sorted([(t_, k) for k, (v_, t_) in verdicts.items() if v_ == "ANGENOMMEN"], reverse=True)
    print("\nAngenommen:", [k for _, k in acc] if acc else "keine")
    if acc:
        print("Definitives Signal (hoechstes t):", acc[0][1])
    return 0


if __name__ == "__main__":
    sys.exit(main())
