"""Nachtraegliche Einordnung (VoLLi 19.10., Option 3): US-S1 und Q2 unter der E10-Huerde.

Gleiche Messung wie E10: X0 (126 Handelstage, Stop -15 %), netto je Trade
(2 x $1 / $575 + 0,2 % Spread), Mittel je Einstiegswoche, Newey-West-t mit 26 Lags,
je Titel hoechstens eine Position. Holdout wie E9 (ab 2020-11-11), S&P 500/400.
Kein Test, keine Entscheidung — nur Vergleich.
"""
import bisect, json, math, sys
from collections import defaultdict
from datetime import date
import numpy as np
sys.path.insert(0, "scripts")
import e9_exit_capital_test as E9
import e8_quality_growth_test as E8

COST = 2 / 575 + 0.002


def nw_t(series, lags=26):
    x = np.asarray(series, float); T = len(x); m = x.mean(); e = x - m
    var = (e @ e) / T
    for l in range(1, min(lags, T - 1) + 1):
        var += 2 * (1 - l / (lags + 1)) * (e[l:] @ e[:-l]) / T
    return m / math.sqrt(var / T)


uni = json.load(open("data/e7_cache/universe.json"))
bars = {t: E8.load_bars(t) for t in uni}
bars = {t: b for t, b in bars.items() if b is not None and len(b) > 60}
bars["SPY"] = E8.load_bars("SPY")
s1 = E9.s1_candidates(uni, bars)
q2 = E9.q2_candidates(uni, bars)
sets = {"S1 (US-Earnings)": [(d, t) for d, ts in s1.items() for t in ts],
        "Q2 (Qualitaets-Screen)": [(d, t) for d, lst in q2.items() for _, t in lst]}
for name, ev in sets.items():
    busy = defaultdict(str); net = []; wk = defaultdict(list)
    for d, t in sorted(ev):
        if d <= busy[t]:
            continue
        b = bars[t]; dates = list(b.index)
        e = bisect.bisect_left(dates, d)
        if e >= len(dates) or dates[e] != d:
            continue
        p = E9.exit_path(b, (dates, b.Open.to_numpy(), b.Low.to_numpy(), b.Close.to_numpy(), None), e, "X0", [])
        v = p.g - COST; net.append(v)
        y, w, _ = date.fromisoformat(d).isocalendar(); wk[f"{y}-W{w:02d}"].append(v)
        busy[t] = p.d1
    series = [np.mean(wk[k]) for k in sorted(wk)]
    print(f"{name}: n={len(net)}, Wochen {len(wk)}, Ø netto {np.mean(net)*100:+.2f} %, "
          f"Median {np.median(net)*100:+.2f} %, Gewinner {np.mean([v > 0 for v in net])*100:.0f} %, "
          f"NW-t {nw_t(series):+.2f}")
