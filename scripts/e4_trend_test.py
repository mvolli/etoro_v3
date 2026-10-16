#!/usr/bin/env python3
"""E4-Test — Trendfolge F1/F2/F3 (nur lesend).

Vorregistrierung: docs/vorregistrierung-e4-trendfolge-2026-10-16.md (Commit
42db78d, VOR diesem Skript). Daten: data/e4_bars/ (scripts/e4_fetch_bars.py).

  /usr/bin/python3 scripts/e4_trend_test.py
"""
from __future__ import annotations

import glob
import math
import os
import sys
from collections import defaultdict
from datetime import date

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BARS = os.path.join(ROOT, "data", "e4_bars")

MIN_BARS = 300
GAP_DAYS = 10            # Luecke > 10 Kalendertage = neues Segment (kein Lookback darueber)
COST, COST_SENS = 0.011, 0.005
SPLIT = 0.60
N_MIN, T_MIN, TRIM = 200, 2.4, 0.05

FAMILIES = {"F1": 20, "F2": 20, "F3": 5}          # Horizont je Familie


# ── Daten ─────────────────────────────────────────────────────────────────────
def load() -> dict[str, tuple[list[str], list[float], list[int]]]:
    """{instrument_id: (dates, closes, segment_start_index_per_bar)}"""
    out = {}
    for f in glob.glob(os.path.join(BARS, "*.csv")):
        dates, closes = [], []
        with open(f) as fh:
            for line in fh:
                if line.startswith("#") or line.startswith("date"):
                    continue
                d, c = line.strip().split(",")
                c = float(c)
                if c > 0 and math.isfinite(c):
                    dates.append(d); closes.append(c)
        if len(dates) < MIN_BARS:
            continue
        seg, start = [], 0
        for i, d in enumerate(dates):
            if i and (date.fromisoformat(d) - date.fromisoformat(dates[i - 1])).days > GAP_DAYS:
                start = i
            seg.append(start)
        out[os.path.basename(f)[:-4]] = (dates, closes, seg)
    return out


def sma_ok(closes, seg, i, n):
    return i - n + 1 >= seg[i]


def sma(closes, i, n):
    return sum(closes[i - n + 1:i + 1]) / n


def rsi2(closes, seg):
    """Wilder RSI(2), pro Segment neu gestartet."""
    out = [None] * len(closes)
    ag = al = 0.0
    for i in range(len(closes)):
        k = i - seg[i]                       # Position im Segment
        if k == 0:
            ag = al = 0.0
            continue
        d = closes[i] - closes[i - 1]
        g, l = max(d, 0.0), max(-d, 0.0)
        if k <= 2:                           # Startwert: Mittel der ersten 2 Aenderungen
            ag += g / 2; al += l / 2
            if k < 2:
                continue
        else:
            ag = (ag + g) / 2
            al = (al + l) / 2
        out[i] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    return out


def fwd(closes, seg, i, h):
    """Close(t+h)/Close(t)-1, nur innerhalb desselben Segments."""
    j = i + h
    if j >= len(closes) or seg[j] != seg[i]:
        return None
    return closes[j] / closes[i] - 1


# ── Signale ───────────────────────────────────────────────────────────────────
def signals_f1(data):
    ev = []
    for sid, (dates, closes, seg) in data.items():
        last = -10**9
        for i in range(len(closes)):
            if not sma_ok(closes, seg, i, 252) or i - last < 20:
                continue
            hi = max(closes[i - 251:i + 1])
            s50, s200 = sma(closes, i, 50), sma(closes, i, 200)
            if closes[i] >= 0.98 * hi and s50 > s200 and closes[i] > s50:
                ev.append((sid, i)); last = i
    return ev


def signals_f3(data):
    ev = []
    for sid, (dates, closes, seg) in data.items():
        r2 = rsi2(closes, seg)
        last = -10**9
        for i in range(len(closes)):
            if not sma_ok(closes, seg, i, 200) or i - last < 5 or r2[i] is None:
                continue
            if closes[i] > sma(closes, i, 200) and r2[i] < 10:
                ev.append((sid, i)); last = i
    return ev


def signals_f2(data, axis):
    idx = {sid: {d: i for i, d in enumerate(v[0])} for sid, v in data.items()}
    ev = []
    for k in range(0, len(axis), 20):
        d = axis[k]
        scored = []
        for sid, (dates, closes, seg) in data.items():
            i = idx[sid].get(d)
            if i is None or i - 126 < seg[i]:
                continue
            scored.append((closes[i - 21] / closes[i - 126] - 1, sid, i))
        if len(scored) < 50:
            continue
        scored.sort(reverse=True)
        top = scored[:max(1, len(scored) // 10)]
        ev += [(sid, i) for _, sid, i in top]
    return ev


# ── Statistik ─────────────────────────────────────────────────────────────────
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


def trimmed(vals, frac=TRIM):
    v = sorted(vals); k = int(len(v) * frac)
    v = v[k:len(v) - k] if len(v) - 2 * k > 0 else v
    return sum(v) / len(v)


def main() -> int:
    data = load()
    axis = sorted({d for v in data.values() for d in v[0]})
    split_date = axis[int(len(axis) * SPLIT)]
    print(f"Universum: {len(data)} Titel (>= {MIN_BARS} Kerzen), Achse {axis[0]}..{axis[-1]}, "
          f"{len(axis)} Handelstage, Holdout ab {split_date}")

    # Gleichtaegige Mediane je Horizont
    medians = {}
    for h in set(FAMILIES.values()):
        per_day = defaultdict(list)
        for dates, closes, seg in data.values():
            for i in range(len(closes)):
                r = fwd(closes, seg, i, h)
                if r is not None:
                    per_day[dates[i]].append(r)
        med = {}
        for d, rs in per_day.items():
            rs.sort(); n = len(rs)
            med[d] = rs[n // 2] if n % 2 else (rs[n // 2 - 1] + rs[n // 2]) / 2
        medians[h] = med

    gens = {"F1": signals_f1(data), "F2": signals_f2(data, axis), "F3": signals_f3(data)}
    verdicts = {}
    for fam, h in FAMILIES.items():
        rows = []   # (sid, date, raw, excess_raw)
        for sid, i in gens[fam]:
            dates, closes, seg = data[sid]
            r = fwd(closes, seg, i, h)
            m = medians[h].get(dates[i])
            if r is None or m is None:
                continue
            rows.append((sid, dates[i], r, r - m))
        ho = [x for x in rows if x[1] >= split_date]
        tr = [x for x in rows if x[1] < split_date]
        ex = [(x[3] - COST, x[1]) for x in ho]
        exv = [v for v, _ in ex]
        ab = [x[2] - COST for x in ho]
        n = len(ho)
        print(f"\n── {fam} (Horizont {h}d) ─────────────────────────────")
        if n == 0:
            print("  keine Holdout-Signale"); verdicts[fam] = "UNENTSCHEIDBAR"; continue
        mean_ex = sum(exv) / n
        t = cluster_t(ex)
        trim = trimmed(exv)
        by_sym = defaultdict(list)
        for x, (v, _) in zip(ho, ex):
            by_sym[x[0]].append(v)
        tot = sum(exv)
        loo = all((tot - sum(v)) / (n - len(v)) > 0 for v in by_sym.values() if n - len(v) > 0) if mean_ex > 0 else False
        mean_ab = sum(ab) / n
        c = [n >= N_MIN, mean_ex > 0, t >= T_MIN, trim > 0, loo, mean_ab > 0]
        labels = [f"n >= {N_MIN}: n={n}", f"Netto-Excess > 0: {mean_ex*100:+.2f} %",
                  f"t (Tages-Cluster) >= {T_MIN}: t={t:.2f}", f"5 %-Trim > 0: {trim*100:+.2f} %",
                  "Leave-one-out Titel", f"Absolut netto > 0: {mean_ab*100:+.2f} %"]
        for k, (ok, lab) in enumerate(zip(c, labels), 1):
            print(f"  {k}) {lab:42s} {'✓' if ok else '✗'}")
        days = len({d for _, d in ex})
        print(f"  Signaltage im Holdout: {days}, Titel: {len(by_sym)}")
        sens_ex = mean_ex + COST - COST_SENS
        sens_ab = mean_ab + COST - COST_SENS
        print(f"  [Sensitivitaet 0,5 % Kosten] Excess {sens_ex*100:+.2f} %, absolut {sens_ab*100:+.2f} %")
        clean = [v for x, v in zip(ho, exv) if abs(x[2]) < 1.0]
        print(f"  [Deskriptiv] ohne |Return| >= 100 %: n={len(clean)}, Excess {sum(clean)/max(len(clean),1)*100:+.2f} %")
        if tr:
            trv = [x[3] - COST for x in tr]
            print(f"  [Deskriptiv, Training] n={len(tr)} Excess {sum(trv)/len(trv)*100:+.2f} %, "
                  f"absolut {sum(x[2]-COST for x in tr)/len(tr)*100:+.2f} %")
        verdicts[fam] = ("UNENTSCHEIDBAR (n < 200)" if not c[0]
                         else "ANGENOMMEN" if all(c) else "VERWORFEN")
        print(f"  >>> {fam} {verdicts[fam]}")
    print("\nFazit: " + ", ".join(f"{k} {v}" for k, v in verdicts.items()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
