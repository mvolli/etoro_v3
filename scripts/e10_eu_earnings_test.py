#!/usr/bin/env python3
"""E10-Test — Earnings-Katalysator S1 in Europa, Ausstieg X0, Rendite in USD. Nur lesend.

Vorregistrierung: docs/vorregistrierung-e10-earnings-europa-2026-10-19.md (c821f6a).
Daten: data/e10_cache/ (scripts/e10_fetch_eu.py), Universum docs/e10-universum-2026-10-19.json.

  /usr/bin/python3 scripts/e10_eu_earnings_test.py
"""
from __future__ import annotations

import bisect
import json
import math
import os
import random
import sys
from collections import defaultdict
from datetime import date, time as dtime

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
C = os.path.join(ROOT, "data", "e10_cache")
UNI = os.path.join(ROOT, "docs", "e10-universum-2026-10-19.json")

START, END = "2006-01-01", "2026-10-15"
SPLIT = 0.60
HOLD, STOP = 126, 0.15
SURPRISE_MIN, EAR_MIN, MIN_EST = 5.0, 0.02, 0.05
TRANCHE, FEE = 575.0, 1.0
CAPITAL, K, RUNS, SEED = 4600.0, 8, 200, 17
N_MIN, WEEKS_MIN, T_MIN, NW_LAGS = 150, 30, 2.0, 26
MID_INDICES = {"MDAX", "FTSE_250_Index", "AMX_index"}
TAX = {"UK": 0.005, "FR": 0.004, "ES": 0.002, "IT": 0.001, "IE": 0.01}
CCY = {"UK": "GBPUSD=X", "CH": "CHFUSD=X", "SE": "SEKUSD=X", "DK": "DKKUSD=X", "NO": "NOKUSD=X"}
TZ = {".L": "Europe/London", ".DE": "Europe/Berlin", ".PA": "Europe/Paris", ".AS": "Europe/Amsterdam",
      ".MC": "Europe/Madrid", ".MI": "Europe/Rome", ".SW": "Europe/Zurich", ".ST": "Europe/Stockholm",
      ".CO": "Europe/Copenhagen", ".HE": "Europe/Helsinki", ".OL": "Europe/Oslo", ".BR": "Europe/Brussels"}
CLOSE = {".L": dtime(16, 30), ".OL": dtime(16, 20), ".CO": dtime(17, 0), ".HE": dtime(18, 30)}
DEFAULT_CLOSE = dtime(17, 30)


def load_bars(t):
    p = os.path.join(C, "bars", f"{t}.csv")
    if not os.path.exists(p) or os.path.getsize(p) == 0:
        return None
    df = pd.read_csv(p, index_col=0, parse_dates=True)
    df = df[(df.Close > 0) & (df.Open > 0) & (df.Low > 0)]
    df.index = df.index.strftime("%Y-%m-%d")
    return df[(df.index >= START) & (df.index <= END)]


def fx_series(t):
    df = load_bars(t)
    return (list(df.index), df.Close.to_numpy()) if df is not None else None


def fx_at(fx, d):
    if fx is None:
        return 1.0
    i = bisect.bisect_right(fx[0], d) - 1
    return float(fx[1][max(i, 0)])


def events(t, dates):
    """[(k0, surprise)] nach der E10-Regel (day0 in Ortszeit, Bereinigung EPS)."""
    p = os.path.join(C, "earn", f"{t}.csv")
    if not os.path.exists(p) or os.path.getsize(p) == 0:
        return []
    df = pd.read_csv(p)
    suf = "." + t.rsplit(".", 1)[1]
    close = CLOSE.get(suf, DEFAULT_CLOSE)
    out = []
    for v, est, rep in zip(df[df.columns[0]], df["EPS Estimate"], df["Reported EPS"]):
        try:
            est, rep = float(est), float(rep); ts = pd.Timestamp(v)
        except Exception:  # noqa: BLE001
            continue
        if not (math.isfinite(est) and math.isfinite(rep)) or abs(est) < MIN_EST:
            continue
        ratio = rep / est
        if abs(ratio) > 20 or 0 < ratio < 0.05:
            continue
        if ts.tzinfo is None:
            continue
        if (ts.hour, ts.minute) == (0, 0):                      # ohne Uhrzeit -> Datum selbst
            k0 = bisect.bisect_left(dates, ts.strftime("%Y-%m-%d"))
        else:
            loc = ts.tz_convert(TZ.get(suf, "Europe/Berlin"))
            ld = loc.strftime("%Y-%m-%d")
            if loc.time() < close:
                k0 = bisect.bisect_left(dates, ld)              # lokales Datum oder naechster Handelstag
            else:
                k0 = bisect.bisect_right(dates, ld)             # naechster Handelstag
        if 1 <= k0 < len(dates):
            out.append((k0, (rep - est) / abs(est) * 100.0))
    out.sort()
    return out


def nw_t(series, lags):
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
    uni = json.load(open(UNI))
    bars = {t: load_bars(t) for t in uni}
    bars = {t: b for t, b in bars.items() if b is not None and len(b) > 260}
    fx = {k: fx_series(v) for k, v in CCY.items()}
    fx_eur = fx_series("EURUSD=X")
    stoxx = load_bars("^STOXX")
    axis = sorted(set().union(*[set(b.index) for b in bars.values()]))
    split = axis[int(len(axis) * SPLIT)]
    print(f"Universum {len(bars)} Titel mit Kursen, {axis[0]}..{axis[-1]}, Holdout ab {split}")

    rets = []
    for b in bars.values():
        r = b.Close.pct_change()
        rets.append(r[(r > -0.5) & (r < 1.0)])
    ew = pd.concat(rets, axis=1).mean(axis=1)
    ew_day = ew.to_dict()
    ew_idx = (1 + ew.fillna(0)).cumprod(); ewd = list(ew_idx.index); ewv = ew_idx.to_numpy()

    def ew_ret(d0, d1):
        i = bisect.bisect_left(ewd, d0) - 1; j = bisect.bisect_right(ewd, d1) - 1
        return None if i < 0 or j <= i else ewv[j] / ewv[i] - 1

    sd = list(stoxx.index) if stoxx is not None else []
    so = stoxx.Open.to_numpy() if stoxx is not None else None
    sc = stoxx.Close.to_numpy() if stoxx is not None else None

    def stoxx_ret(d0, d1):
        if not sd:
            return None
        i = bisect.bisect_left(sd, d0); j = bisect.bisect_right(sd, d1) - 1
        return None if i >= len(sd) or j < i else sc[j] / so[i] - 1

    trades = []
    n_ev = 0
    for t, b in bars.items():
        u = uni[t]
        dates = list(b.index); o = b.Open.to_numpy(); l = b.Low.to_numpy(); c = b.Close.to_numpy()
        sma = b.Close.rolling(200).mean().to_numpy()
        country = u["country"]
        cost = 2 * FEE / TRANCHE + (0.005 if u["index"] in MID_INDICES else 0.002) + TAX.get(country, 0.0)
        fxs = fx.get(country, fx_eur)
        busy = ""
        for k0, surprise in events(t, dates):
            n_ev += 1
            d1 = k0 + 1; e = d1 + 1
            if e >= len(dates) or surprise < SURPRISE_MIN:
                continue
            m = ew_day.get(dates[k0])
            if m is None or not math.isfinite(m):
                continue
            ear = c[k0] / c[k0 - 1] - 1 - m
            if ear < EAR_MIN or not (math.isfinite(sma[d1]) and c[d1] > sma[d1]):
                continue
            if dates[e] <= busy or e + HOLD - 1 >= len(dates):
                continue
            x = e + HOLD - 1
            ep = o[e]; stop = ep * (1 - STOP); xp, xi = c[x], x
            for j in range(e, x + 1):
                if l[j] <= stop:
                    xp, xi = min(o[j], stop), j
                    break
            g = xp / ep - 1
            fe, fx_x = fx_at(fxs, dates[e]), fx_at(fxs, dates[xi])
            gu = (xp * fx_x) / (ep * fe) - 1
            mk_d = [dates[j] for j in range(e, xi + 1)]
            mk_v = [(c[j] * fx_at(fxs, dates[j])) / (ep * fe) for j in range(e, xi)] + [gu + 1]
            marks = (mk_d, mk_v)
            trades.append(dict(t=t, d=dates[e], d1=dates[xi], g=g, gu=gu, cost=cost, country=country,
                               tier="mittel" if u["index"] in MID_INDICES else "gross",
                               ew=ew_ret(dates[e], dates[xi]), sx=stoxx_ret(dates[e], dates[xi]),
                               stop=xi < x, marks=marks))
            busy = dates[xi]
    ho = [r for r in trades if r["d"] >= split]
    tr = [r for r in trades if r["d"] < split]
    print(f"Ereignisse geprueft {n_ev}, Trades {len(trades)} (Training {len(tr)}, Holdout {len(ho)})")
    if not ho:
        print("keine Holdout-Trades"); return 0

    net = [r["gu"] - r["cost"] for r in ho]
    n = len(net); m = float(np.mean(net))
    wk = defaultdict(list)
    for r, v in zip(ho, net):
        wk[week(r["d"])].append(v)
    series = [np.mean(wk[k]) for k in sorted(wk)]
    t_ = nw_t(series, NW_LAGS)
    print(f"\n── Holdout S1-EU, X0, USD: n={n}, Wochen {len(wk)}, Ø netto {m*100:+.2f} % "
          f"(Median {np.median(net)*100:+.2f} %, Gewinner {np.mean([v > 0 for v in net])*100:.0f} %), "
          f"NW-t {t_:+.2f}, Stops {sum(r['stop'] for r in ho)}")
    loc_net = [r["g"] - r["cost"] for r in ho]
    print(f"   Lokalwaehrung: Ø {np.mean(loc_net)*100:+.2f} % | vs. EW-Universum "
          f"{np.mean([r['g'] - r['ew'] - r['cost'] for r in ho if r['ew'] is not None])*100:+.2f} % | vs. STOXX 600 "
          f"{np.mean([r['g'] - r['sx'] - r['cost'] for r in ho if r['sx'] is not None])*100:+.2f} %")
    print(f"   5 %: {np.percentile(net, 5)*100:+.1f} %, 95 %: {np.percentile(net, 95)*100:+.1f} %")
    for key in ("country", "tier"):
        grp = defaultdict(list)
        for r, v in zip(ho, net):
            grp[r[key]].append(v)
        print(f"   je {key}: " + ", ".join(f"{k} {np.mean(v)*100:+.1f}% (n={len(v)})" for k, v in sorted(grp.items())))
    if tr:
        print(f"   [Training] n={len(tr)}, Ø netto USD {np.mean([r['gu'] - r['cost'] for r in tr])*100:+.2f} %")

    # Portfolio-Simulation (wie E9, USD)
    cal = [d for d in axis if d >= split]
    cands = defaultdict(list)
    for r in ho:
        cands[r["d"]].append(r)
    cost_abs = {id(r): 2 * FEE + TRANCHE * (r["cost"] - 2 * FEE / TRANCHE) for r in ho}
    T = CAPITAL / K
    runs = []
    for run in range(RUNS):
        rng = random.Random(SEED + run)
        held, realized, eq, n_tr = {}, 0.0, [], 0
        for d in cal:
            for tk in [tk for tk, r in held.items() if r["d1"] <= d]:
                r = held.pop(tk); realized += T * r["gu"] - cost_abs[id(r)] * T / TRANCHE
            todo = list(cands.get(d, [])); rng.shuffle(todo)
            for r in todo:
                if len(held) >= K or r["t"] in held:
                    continue
                held[r["t"]] = r; n_tr += 1
            un = 0.0
            for r in held.values():
                i = bisect.bisect_right(r["marks"][0], d) - 1
                un += T * (r["marks"][1][i] - 1) if i >= 0 else 0.0
            eq.append(CAPITAL + realized + un - sum(cost_abs[id(r)] * T / TRANCHE for r in held.values()))
        eq = np.array(eq); yrs = len(cal) / 252.0
        ann = (eq[-1] / CAPITAL) ** (1 / yrs) - 1
        peak = np.maximum.accumulate(eq); mdd = float(((eq - peak) / peak).min())
        yr, prev, last_i = {}, CAPITAL, {}
        for i, d in enumerate(cal):
            last_i[d[:4]] = i
        for y in sorted(last_i):
            yr[y] = eq[last_i[y]] / prev - 1; prev = eq[last_i[y]]
        runs.append(dict(ann=ann, mdd=mdd, yr=yr, n=n_tr / yrs))
    runs.sort(key=lambda r: r["ann"])
    med = runs[len(runs) // 2]; p10 = runs[len(runs) // 10]["ann"]
    yrs_pos = sum(1 for v in med["yr"].values() if v > 0)
    print(f"\n── Portfolio (k={K}, ${T:.0f}, USD, {RUNS} Laeufe): Jahresrendite Median {med['ann']*100:+.1f} %, "
          f"10 %-Quantil {p10*100:+.1f} %, 90 %-Quantil {runs[len(runs)*9//10]['ann']*100:+.1f} %, "
          f"max. DD {np.median([r['mdd'] for r in runs])*100:.1f} %, Trades/Jahr {med['n']:.0f}")
    print("   Jahre (Median-Lauf): " + ", ".join(f"{y} {v*100:+.1f}%" for y, v in med["yr"].items()))
    if sd and fx_eur:
        i0 = bisect.bisect_left(sd, cal[0]); i1 = len(sd) - 1
        tot = (sc[i1] * fx_at(fx_eur, sd[i1])) / (so[i0] * fx_at(fx_eur, sd[i0]))
        print(f"   STOXX 600 Buy & Hold (USD): Jahresrendite {(tot ** (252 / len(cal)) - 1)*100:+.1f} %")

    crit = [n >= N_MIN and len(wk) >= WEEKS_MIN, m > 0 and t_ >= T_MIN, med["ann"] > 0 and p10 > 0,
            yrs_pos >= math.ceil(len(med["yr"]) * 2 / 3)]
    verdict = "UNENTSCHEIDBAR" if not crit[0] else ("ANGENOMMEN" if all(crit) else "verworfen")
    print(f"\nKriterien: Umfang {'✓' if crit[0] else '✗'}, Ø>0 & NW-t≥2 {'✓' if crit[1] else '✗'}, "
          f"Portfolio Median & 10 % > 0 {'✓' if crit[2] else '✗'}, Jahre positiv {yrs_pos}/{len(med['yr'])} "
          f"{'✓' if crit[3] else '✗'} -> {verdict}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
