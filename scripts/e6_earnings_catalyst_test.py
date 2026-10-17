#!/usr/bin/env python3
"""E6-Test — Earnings-Katalysator-Long (30-126 Handelstage), nur lesend.

Vorregistrierung: docs/vorregistrierung-e6-earnings-katalysator-2026-10-17.md
(VOR diesem Lauf committet). Daten: data/e6_cache/ (scripts/e6_fetch_data.py).

  /usr/bin/python3 scripts/e6_earnings_catalyst_test.py
"""
from __future__ import annotations

import bisect
import json
import math
import os
import sys
from collections import defaultdict
from datetime import date

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
C = os.path.join(ROOT, "data", "e6_cache")

START, END = "2012-01-01", "2026-10-15"
SPLIT = 0.60
HORIZONS = (30, 63, 126)
STOP = 0.15
SIZE, FEE, SPREAD = 880.0, 1.0, 0.002
COST = 2 * FEE / SIZE + SPREAD
MIN_EST = 0.05
SURPRISE_MIN, EAR_MIN = 5.0, 0.02
N_MIN, WEEKS_MIN, T_MIN, TRIM = 300, 40, 2.4, 0.05


def load_bars(t):
    p = os.path.join(C, "bars", f"{t}.csv")
    if not os.path.exists(p) or os.path.getsize(p) == 0:
        return None
    df = pd.read_csv(p, index_col=0, parse_dates=True)
    df = df[(df.Close > 0) & (df.Open > 0) & (df.Low > 0)]
    df.index = df.index.strftime("%Y-%m-%d")
    return df


def load_earn(t):
    p = os.path.join(C, "earn", f"{t}.csv")
    if not os.path.exists(p) or os.path.getsize(p) == 0:
        return []
    df = pd.read_csv(p)
    col = df.columns[0]
    out = []
    for _, r in df.iterrows():
        try:
            ts = pd.Timestamp(r[col])
            est, rep = float(r["EPS Estimate"]), float(r["Reported EPS"])
        except Exception:  # noqa: BLE001
            continue
        if not (math.isfinite(est) and math.isfinite(rep)) or abs(est) < MIN_EST:
            continue
        if ts.tzinfo is not None:
            ts = ts.tz_convert("America/New_York")
        # vor 16:00 ET (und nicht exakt 00:00 = Uhrzeit unbekannt) -> day0 = Datum;
        # sonst (nach Schluss / unbekannt) -> day0 = naechster Handelstag
        bmo = (ts.hour, ts.minute) != (0, 0) and ts.hour < 16
        out.append((ts.strftime("%Y-%m-%d"), bmo, (rep - est) / abs(est) * 100.0))
    return out


def load_ratings(t):
    p = os.path.join(C, "ratings", f"{t}.csv")
    if not os.path.exists(p) or os.path.getsize(p) == 0:
        return {}
    df = pd.read_csv(p)
    col = df.columns[0]
    sc = defaultdict(int)
    for _, r in df.iterrows():
        d = str(r[col])[:10]
        a = str(r.get("Action", "")).lower()
        pt = str(r.get("priceTargetAction", "")).lower()
        s = (1 if a == "up" else -1 if a == "down" else 0) + (1 if pt == "raises" else -1 if pt == "lowers" else 0)
        sc[d] += s
    return sc


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


def trimmed(v):
    v = sorted(v); k = int(len(v) * TRIM)
    v = v[k:len(v) - k] if len(v) - 2 * k > 0 else v
    return sum(v) / len(v)


def week(d):
    y, w, _ = date.fromisoformat(d).isocalendar()
    return f"{y}-W{w:02d}"


def main() -> int:
    uni = json.load(open(os.path.join(C, "universe.json")))
    bars = {t: load_bars(t) for t in uni}
    bars = {t: b for t, b in bars.items() if b is not None and len(b) > 260}
    spy = load_bars("SPY")
    axis = sorted(set().union(*[set(b.index) for b in bars.values()]) | set(spy.index))
    axis = [d for d in axis if START <= d <= END]
    split = axis[int(len(axis) * SPLIT)]
    print(f"Universum {len(bars)} Titel mit Kursen, {axis[0]}..{axis[-1]}, Holdout ab {split}")

    # Gleichgewichteter Universums-Index: tagesmittlere Close-to-Close-Rendite
    rets = []
    for b in bars.values():
        r = b.Close.pct_change()
        r = r[(r > -0.5) & (r < 1.0)]                      # Datenfehler raus
        rets.append(r)
    ew = pd.concat(rets, axis=1).mean(axis=1).fillna(0.0)
    ew_idx = (1 + ew).cumprod()
    ewd = list(ew_idx.index); ewv = ew_idx.to_numpy()

    def ew_ret(d0, d1):          # Open d0 ~ Close(d0-1); Close d1
        i = bisect.bisect_left(ewd, d0) - 1; j = bisect.bisect_right(ewd, d1) - 1
        if i < 0 or j <= i:
            return None
        return ewv[j] / ewv[i] - 1

    sd = list(spy.index); so = spy.Open.to_numpy(); sc_ = spy.Close.to_numpy()

    def spy_ret(d0, d1):
        i = bisect.bisect_left(sd, d0); j = bisect.bisect_right(sd, d1) - 1
        return None if i >= len(sd) or j < i else sc_[j] / so[i] - 1

    def spy_cc(d_prev, d0):
        i = bisect.bisect_left(sd, d_prev); j = bisect.bisect_left(sd, d0)
        return None if j >= len(sd) or i >= len(sd) else sc_[j] / sc_[i] - 1

    res = defaultdict(list)    # (variant, H) -> rows
    for t, b in bars.items():
        dates = list(b.index); o = b.Open.to_numpy(); l = b.Low.to_numpy(); c = b.Close.to_numpy()
        sma200 = b.Close.rolling(200).mean().to_numpy()
        ratings = load_ratings(t)
        busy_until = {k: "" for k in [(v, h) for v in ("S1", "S2") for h in HORIZONS]}
        for ed, bmo, surprise in load_earn(t):
            if not (START <= ed <= END):
                continue
            k0 = bisect.bisect_left(dates, ed)
            if k0 >= len(dates):
                continue
            if not bmo or dates[k0] != ed:
                k0 = k0 + 1 if dates[k0] == ed else k0       # nach Schluss/unbekannt -> naechster Tag
            if k0 < 1 or k0 + 2 >= len(dates):
                continue
            s_cc = spy_cc(dates[k0 - 1], dates[k0])
            if s_cc is None:
                continue
            ear = c[k0] / c[k0 - 1] - 1 - s_cc
            d1 = k0 + 1                                        # Entscheidung zum Close von day0+1
            if not (surprise >= SURPRISE_MIN and ear >= EAR_MIN):
                continue
            if not (math.isfinite(sma200[d1]) and c[d1] > sma200[d1]):
                continue
            rs = sum(ratings.get(dates[j], 0) for j in range(k0 - 1, d1 + 1) if j >= 0)
            e = d1 + 1                                         # Einstieg Open day0+2
            for var in ("S1", "S2"):
                if var == "S2" and rs < 1:
                    continue
                for h in HORIZONS:
                    if dates[e] <= busy_until[(var, h)]:
                        continue
                    x = min(e + h - 1, len(dates) - 1)
                    if e + h - 1 >= len(dates):
                        continue                               # Horizont nicht abgeschlossen
                    ep = o[e]; stop = ep * (1 - STOP); xp, xi = c[x], x
                    for j in range(e, x + 1):
                        if l[j] <= stop:
                            xp, xi = min(o[j], stop), j
                            break
                    g = xp / ep - 1
                    ewr = ew_ret(dates[e], dates[xi]); spr = spy_ret(dates[e], dates[xi])
                    if ewr is None or spr is None:
                        continue
                    res[(var, h)].append(dict(t=t, d=dates[e], g=g, ew=ewr, spy=spr,
                                              stop=xi < x, hold=xi - e + 1))
                    busy_until[(var, h)] = dates[xi]

    verdicts = {}
    for var in ("S1", "S2"):
        for h in HORIZONS:
            rows = res[(var, h)]
            ho = [r for r in rows if r["d"] >= split]
            tr = [r for r in rows if r["d"] < split]
            if not ho:
                print(f"{var} H{h}: keine Holdout-Trades"); continue
            ex = [(r["g"] - r["ew"] - COST, week(r["d"])) for r in ho]
            exv = [v for v, _ in ex]
            n = len(exv); m = sum(exv) / n
            t_ = cluster_t(ex); tm = trimmed(exv)
            weeks = len({k for _, k in ex})
            by = defaultdict(list)
            for r, v in zip(ho, exv):
                by[r["t"]].append(v)
            tot = sum(exv)
            loo = m > 0 and all((tot - sum(v)) / (n - len(v)) > 0 for v in by.values() if n - len(v) > 0)
            ab = [r["g"] - COST for r in ho]; ma = sum(ab) / n
            yr = defaultdict(list)
            for r, v in zip(ho, exv):
                yr[r["d"][:4]].append(v)
            yrs_pos = sum(1 for v in yr.values() if sum(v) / len(v) > 0)
            yrs_ok = yrs_pos >= math.ceil(len(yr) * 2 / 3)
            crit = [n >= N_MIN, weeks >= WEEKS_MIN, m > 0, t_ >= T_MIN, tm > 0, loo, yrs_ok]
            v_ = "UNENTSCHEIDBAR" if not (crit[0] and crit[1]) else ("ANGENOMMEN" if all(crit) else "verworfen")
            verdicts[(var, h)] = (v_, t_)
            wins = [x for x in ab if x > 0]; loss = [x for x in ab if x <= 0]
            print(f"\n── {var} | H {h} Handelstage — n={n} (Wochen {weeks}), Trefferq. {len(wins)/n*100:.0f} %, "
                  f"Ø Gewinn {np.mean(wins)*100 if wins else 0:+.2f} %, Ø Verlust {np.mean(loss)*100 if loss else 0:+.2f} %, "
                  f"Stops {sum(r['stop'] for r in ho)}")
            print(f"   Excess vs. Universum {m*100:+.2f} % (t {t_:+.2f}, Trim {tm*100:+.2f} %, LOO {'✓' if loo else '✗'}, "
                  f"Jahre positiv {yrs_pos}/{len(yr)}) | vs. SPY {np.mean([r['g']-r['spy']-COST for r in ho])*100:+.2f} % | "
                  f"absolut {ma*100:+.2f} %  -> {v_}")
            print("   je Jahr Excess: " + ", ".join(f"{y} {np.mean(v)*100:+.1f}% (n={len(v)})" for y, v in sorted(yr.items())))
            print(f"   schlechteste 5 %: {np.percentile(ab, 5)*100:+.1f} %, beste 5 %: {np.percentile(ab, 95)*100:+.1f} %")
            if tr:
                print(f"   [Training] n={len(tr)} Excess {np.mean([r['g']-r['ew']-COST for r in tr])*100:+.2f} %, "
                      f"absolut {np.mean([r['g']-COST for r in tr])*100:+.2f} %")
    acc = sorted([(t_, k) for k, (v_, t_) in verdicts.items() if v_ == "ANGENOMMEN"], reverse=True)
    print("\nAngenommen:", [k for _, k in acc] if acc else "keine")
    if acc:
        print("Definitives Signal (hoechstes t):", acc[0][1])
    return 0


if __name__ == "__main__":
    sys.exit(main())
