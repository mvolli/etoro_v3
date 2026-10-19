#!/usr/bin/env python3
"""E11-Test — ereignisgetriebene Q2-Ausloeser (A Einreichung, B Qualitaet+Earnings,
C Qualitaet+Insider) gegen den Monats-Screen M. Nur lesend.

Vorregistrierung: docs/vorregistrierung-e11-ereignisgetrieben-2026-10-19.md (645d793).
Baut auf E8 (Kennzahlen, Kurse), E9 (S1-Kandidaten, Ausstieg X0, Portfolio) und
E7 (Insider-Kaeufe, Bereinigung) auf.

  /usr/bin/python3 scripts/e11_event_driven_test.py
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

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import e7_insider_cluster_test as E7  # noqa: E402
import e8_quality_growth_test as E8  # noqa: E402
import e9_exit_capital_test as E9  # noqa: E402

PRE_FROM, HOLDOUT_FROM, END = "2011-09-01", "2020-11-11", "2026-10-15"
COST = 2 / 575 + 0.002
K, RUNS, SEED = 8, 200, 17
N_MIN, WEEKS_MIN, T_MIN, NW_LAGS = 100, 30, 2.4, 26
C_WINDOW, C_MIN_N, C_MIN_VAL, C_LOCK = 7, 2, 50_000.0, 90


def nw_t(series, lags=NW_LAGS):
    x = np.asarray(series, float); T = len(x)
    if T < 3:
        return 0.0
    m = x.mean(); e = x - m
    var = (e @ e) / T
    for l in range(1, min(lags, T - 1) + 1):
        var += 2 * (1 - l / (lags + 1)) * (e[l:] @ e[:-l]) / T
    return m / math.sqrt(var / T) if var > 0 else 0.0


def week(d):
    y, w, _ = date.fromisoformat(d).isocalendar()
    return f"{y}-W{w:02d}"


def main() -> int:
    uni = json.load(open(os.path.join(E8.C7, "universe.json")))
    bars = {t: E8.load_bars(t) for t in uni}
    bars = {t: b for t, b in bars.items() if b is not None and len(b) > 60}
    bars["SPY"] = E8.load_bars("SPY")
    tcik = E8.ticker_cik({t: b for t, b in bars.items() if t != "SPY"})
    filings, tsic = E8.build_filings(tcik)
    nonfin = [t for t in bars if t != "SPY" and not E8.is_fin(tsic.get(t))]
    buyable = {t for t in bars if uni.get(t) == "large_mid"}
    idx = {t: {d: i for i, d in enumerate(bars[t].index)} for t in nonfin}
    closes = {t: bars[t].Close.to_numpy() for t in nonfin}
    print(f"Rangmenge-Basis {len(nonfin)} Nicht-Finanzwerte, kaufbar {len(buyable)}", flush=True)

    cache: dict[str, tuple] = {}

    def rank_at(D):
        """(score{t}, thr, mom{t}, median) zum Tag D — E8-Regeln (accepted < D, <= 200 Tage, Kurs an D)."""
        if D in cache:
            return cache[D]
        lo = (date.fromisoformat(D) - timedelta(days=E8.MAX_AGE_DAYS)).isoformat()
        s = {}
        for t in nonfin:
            fl = filings.get(t)
            if not fl or D not in idx[t]:
                continue
            i = bisect.bisect_left(fl, (D,)) - 1
            if i >= 0 and fl[i][0] >= lo:
                s[t] = fl[i][1]
        if len(s) < 50:
            cache[D] = ({}, 1.0, {}, None); return cache[D]

        def pr(key, inv=False):
            items = sorted(s.items(), key=lambda kv: kv[1][key]); n = len(items)
            r = {t: (k + 1) / n for k, (t, _) in enumerate(items)}
            return {t: 1 - v + 1 / n for t, v in r.items()} if inv else r
        r1, r2, r3 = pr("gpa"), pr("acc", True), pr("growth")
        score = {t: (r1[t] + r2[t] + r3[t]) / 3 for t in s}
        thr = float(np.percentile(list(score.values()), 90))
        mom = {}
        for t in s:
            k = idx[t][D]
            if k >= 252:
                mom[t] = closes[t][k - 21] / closes[t][k - 252] - 1
        med = float(np.median(list(mom.values()))) if mom else None
        cache[D] = (score, thr, mom, med)
        return cache[D]

    spy_dates = list(bars["SPY"].index)

    def next_td(dates, d):            # erster Handelstag strikt nach d
        k = bisect.bisect_right(dates, d)
        return k if k < len(dates) else None

    # ── M: Monats-Screen ─────────────────────────────────────────────────────
    axis = [d for d in spy_dates if PRE_FROM[:7] <= d[:7] and d <= END]
    month_ends = [axis[i] for i in range(len(axis)) if i + 1 == len(axis) or axis[i + 1][:7] != axis[i][:7]]
    cand = defaultdict(list)            # kind -> [(entry_date, score, t)]
    for M in month_ends:
        if M >= END:
            continue
        score, thr, mom, med = rank_at(M)
        if med is None:
            continue
        k = next_td(spy_dates, M)
        if k is None:
            continue
        for t, v in score.items():
            if v >= thr and t in buyable and t in mom and mom[t] > med:
                cand["M"].append((spy_dates[k], v, t))
    print(f"M: {len(cand['M'])} Kandidaten", flush=True)

    # ── A: Eintritt ins Top-Dezil bei Einreichung ────────────────────────────
    for t in sorted(buyable & set(nonfin)):
        dates = list(bars[t].index)
        prev_top = False
        for F, _m in filings.get(t, []):
            if F < "2011-01-01":
                continue
            k1 = next_td(dates, F)
            if k1 is None or k1 + 1 >= len(dates):
                break
            d1 = dates[k1]
            score, thr, mom, med = rank_at(d1)
            top = t in score and score[t] >= thr
            if top and not prev_top and med is not None and t in mom and mom[t] > med \
                    and PRE_FROM <= dates[k1 + 1] <= END:
                cand["A"].append((dates[k1 + 1], score[t], t))
            prev_top = top
    print(f"A: {len(cand['A'])} Kandidaten", flush=True)

    # ── S1 und B ─────────────────────────────────────────────────────────────
    E9.HOLDOUT_FROM = PRE_FROM
    s1 = E9.s1_candidates(uni, bars)
    for d, ts in s1.items():
        for t in ts:
            cand["S1"].append((d, 0.0, t))
            dates = list(bars[t].index)
            k = bisect.bisect_left(dates, d)
            if k < 1:
                continue
            score, thr, _, _ = rank_at(dates[k - 1])            # d1 = Tag vor dem Einstieg
            if t in score and score[t] >= thr:
                cand["B"].append((d, score[t], t))
    print(f"S1: {len(cand['S1'])}, B: {len(cand['B'])} Kandidaten", flush=True)

    # ── C: Insider-Cluster (E7-Daten und -Bereinigung) + Top-Dezil ──────────
    pur = E7.load_purchases(set(bars))
    for t, g in pur.groupby("sym"):
        if t not in buyable or t not in idx:
            continue
        rows = sorted(zip(g.F, g.owner_cik, g.value, g.is_officer))
        dates = list(bars[t].index)
        lock = None
        for F in sorted({r[0] for r in rows}):
            if lock is not None and F <= lock:
                continue
            lo = F - timedelta(days=C_WINDOW - 1)
            win = [r for r in rows if lo <= r[0] <= F]
            if (len({r[1] for r in win}) >= C_MIN_N and any(str(r[3]) == "1" for r in win)
                    and sum(r[2] for r in win) >= C_MIN_VAL):
                lock = F + timedelta(days=C_LOCK)
                k = next_td(dates, F.strftime("%Y-%m-%d"))
                if k is None or not (PRE_FROM <= dates[k] <= END):
                    continue
                score, thr, _, _ = rank_at(dates[k])
                if t in score and score[t] >= thr:
                    cand["C"].append((dates[k], score[t], t))
    print(f"C: {len(cand['C'])} Kandidaten", flush=True)

    # ── Pfade (X0) ───────────────────────────────────────────────────────────
    arrs = {}
    paths = {}
    for kind, lst in cand.items():
        for d, sc, t in lst:
            if t not in arrs:
                b = bars[t]
                arrs[t] = (list(b.index), b.Open.to_numpy(), b.Low.to_numpy(), b.Close.to_numpy(), None)
            dates = arrs[t][0]
            e = bisect.bisect_left(dates, d)
            if e >= len(dates) or dates[e] != d:
                continue
            p = E9.exit_path(bars[t], arrs[t], e, "X0", [])
            p.t = t
            paths[(kind, t, d)] = p

    def completed(p):
        n = len(p.marks)
        return n >= 126 or p.g <= -0.15 + 1e-9

    def trade_stats(kind, lo, hi):
        busy = defaultdict(str); net = []; wk = defaultdict(list)
        for d, sc, t in sorted(cand[kind]):
            if not (lo <= d <= hi) or d <= busy[t]:
                continue
            p = paths.get((kind, t, d))
            if p is None:
                continue
            busy[t] = p.d1
            if not completed(p):
                continue
            v = p.g - COST; net.append(v); wk[week(d)].append(v)
        if not net:
            return dict(n=0, weeks=0, mean=0.0, med=0.0, hit=0.0, t=0.0)
        return dict(n=len(net), weeks=len(wk), mean=float(np.mean(net)), med=float(np.median(net)),
                    hit=float(np.mean([v > 0 for v in net])), t=nw_t([np.mean(wk[k]) for k in sorted(wk)]))

    cal = [d for d in spy_dates if HOLDOUT_FROM <= d <= END]

    def portfolio(kinds):
        cands = defaultdict(list)
        pp = {}
        for kind in kinds:
            label = "S1" if kind == "S1" else "Q2"
            for d, sc, t in cand[kind]:
                if HOLDOUT_FROM <= d <= END and (kind, t, d) in paths:
                    cands[d].append((label, sc, t))
                    pp[(label, t, d)] = paths[(kind, t, d)]
        runs = []
        for r in range(RUNS):
            eq, n = E9.simulate(cal, cands, pp, K, random.Random(SEED + r))
            runs.append(E9.metrics(cal, eq, n, K))
        runs.sort(key=lambda m: m["ann"])
        med = runs[len(runs) // 2]
        return dict(ann=float(np.median([m["ann"] for m in runs])), p10=runs[len(runs) // 10]["ann"],
                    mdd=float(np.median([m["mdd"] for m in runs])), yr=med["yr"], tpy=med["trades_py"])

    res = {}
    print("\n=== Holdout (Einstiege ab 2020-11-11), abgeschlossene Trades, netto ===")
    for kind in ("M", "A", "B", "C", "S1"):
        h = trade_stats(kind, HOLDOUT_FROM, END)
        pre = trade_stats(kind, PRE_FROM, "2020-11-10")
        pf = portfolio([kind])
        res[kind] = (h, pf)
        yrs_pos = sum(1 for v in pf["yr"].values() if v > 0)
        print(f"{kind:2s}: n={h['n']:4d} Wochen {h['weeks']:3d}  Ø {h['mean']*100:+.2f} %  Median {h['med']*100:+.2f} %  "
              f"Gewinner {h['hit']*100:.0f} %  NW-t {h['t']:+.2f} | Portfolio {pf['ann']*100:+.1f} %/J "
              f"(10 %: {pf['p10']*100:+.1f} %, DD {pf['mdd']*100:.1f} %, {pf['tpy']:.0f} Trades/J, Jahre+ {yrs_pos}/{len(pf['yr'])})")
        print(f"    vor Holdout: n={pre['n']}, Ø {pre['mean']*100:+.2f} %, NW-t {pre['t']:+.2f}, Gewinner {pre['hit']*100:.0f} %")
        print("    Jahre: " + ", ".join(f"{y} {v*100:+.1f}%" for y, v in pf["yr"].items()))
    combo = {}
    for name, kinds in (("M+S1", ["M", "S1"]), ("A+S1", ["A", "S1"])):
        combo[name] = portfolio(kinds)
        pf = combo[name]
        print(f"{name}: Portfolio {pf['ann']*100:+.1f} %/J (10 %: {pf['p10']*100:+.1f} %, DD {pf['mdd']*100:.1f} %, "
              f"{pf['tpy']:.0f} Trades/J)")

    print("\n=== Entscheidung ===")
    verdict = {}
    for kind in ("A", "B", "C"):
        h, pf = res[kind]
        yrs_pos = sum(1 for v in pf["yr"].values() if v > 0)
        c = [h["n"] >= N_MIN and h["weeks"] >= WEEKS_MIN, h["mean"] > 0 and h["t"] >= T_MIN,
             pf["ann"] > 0 and pf["p10"] > 0, yrs_pos >= math.ceil(len(pf["yr"]) * 2 / 3)]
        v = "UNENTSCHEIDBAR" if not c[0] else ("ANGENOMMEN" if all(c) else "verworfen")
        verdict[kind] = v
        print(f"{kind}: Umfang {'✓' if c[0] else '✗'}, Ø>0 & NW-t≥2,4 {'✓' if c[1] else '✗'}, "
              f"Portfolio {'✓' if c[2] else '✗'}, Jahre {'✓' if c[3] else '✗'} -> {v}")
    a_repl = (verdict["A"] == "ANGENOMMEN" and res["A"][1]["ann"] >= res["M"][1]["ann"]
              and combo["A+S1"]["ann"] >= combo["M+S1"]["ann"])
    print(f"A ersetzt M: {'JA' if a_repl else 'nein'} (A {res['A'][1]['ann']*100:+.1f} vs M {res['M'][1]['ann']*100:+.1f} %/J; "
          f"A+S1 {combo['A+S1']['ann']*100:+.1f} vs M+S1 {combo['M+S1']['ann']*100:+.1f} %/J)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
