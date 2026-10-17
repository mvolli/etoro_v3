#!/usr/bin/env python3
"""E7-Test — Insider-Clusterkaeufe, 30-126 Handelstage, nur lesend.

Vorregistrierung: docs/vorregistrierung-e7-insider-cluster-2026-10-17.md
(VOR diesem Lauf committet, 099649d). Daten: data/e7_cache/purchases.csv
(scripts/e7_fetch_insider.py) und data/e7_cache/bars/ (scripts/e7_fetch_bars.py).

  /usr/bin/python3 scripts/e7_insider_cluster_test.py
"""
from __future__ import annotations

import bisect
import json
import math
import os
import sys
from collections import defaultdict
from datetime import date, timedelta

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
C = os.path.join(ROOT, "data", "e7_cache")

START, END = "2006-04-01", "2026-10-15"
SPLIT = 0.60
HORIZONS = (30, 63, 126)
STOP = 0.15
SIZE, FEE = 880.0, 1.0
SPREAD = {"large_mid": 0.002, "small": 0.005}
MIN_TX, MAX_TX = 10_000.0, 100_000_000.0
WINDOW_DAYS, LOCK_DAYS = 30, 90
VARIANTS = {"I1": (2, 100_000.0), "I2": (3, 250_000.0)}
N_MIN, WEEKS_MIN, T_MIN, TRIM = 300, 40, 2.4, 0.05


def cost(seg):
    return 2 * FEE / SIZE + SPREAD[seg]


def load_bars(t):
    p = os.path.join(C, "bars", f"{t}.csv")
    if not os.path.exists(p) or os.path.getsize(p) == 0:
        return None
    df = pd.read_csv(p, index_col=0, parse_dates=True)
    df = df[(df.Close > 0) & (df.Open > 0) & (df.Low > 0)]
    df.index = df.index.strftime("%Y-%m-%d")
    return df


def load_purchases(uni):
    df = pd.read_csv(os.path.join(C, "purchases.csv"), dtype=str)
    df = df[df.doc_type == "4"]                                    # nur Original-Form-4
    df = df[(df.is_director == "1") | (df.is_officer == "1")]      # nur Director/Officer
    df["value"] = pd.to_numeric(df.value, errors="coerce")
    # Bereinigung (Nachtrag vor dem Lauf): Werte > $100 Mio. sind ueberwiegend
    # Preisfehler in den Meldungen (z. B. $44.394/Aktie) -> raus.
    df = df[(df.value >= MIN_TX) & (df.value <= MAX_TX)]
    df["sym"] = df.symbol.fillna("").str.upper().str.strip().str.replace(".", "-", regex=False)
    df = df[df.sym.isin(uni)]
    df["F"] = pd.to_datetime(df.filing_date, format="%d-%b-%Y", errors="coerce")
    df = df.dropna(subset=["F"])
    # Bereinigung (Nachtrag vor dem Lauf): Ticker-Wiederverwendung. Nur Meldungen
    # des Emittenten (CIK), der den Ticker zuletzt gemeldet hat; aeltere Firmen mit
    # demselben Kuerzel wuerden sonst gegen die heutige Kurshistorie gerechnet.
    latest = df.sort_values("F").groupby("sym").issuer_cik.last()
    df = df[df.issuer_cik == df.sym.map(latest)]
    df = df.drop_duplicates(subset=["accession", "owner_cik", "trans_date", "shares", "price"])
    return df


def triggers(df_sym, need_n, need_val):
    """Filing-Tage F mit >= need_n Insidern und >= need_val in den 30 Kalendertagen bis F."""
    rows = sorted(zip(df_sym.F, df_sym.owner_cik, df_sym.value))
    out, lock_until = [], None
    for F in sorted({r[0] for r in rows}):
        if lock_until is not None and F <= lock_until:
            continue
        lo = F - timedelta(days=WINDOW_DAYS - 1)
        win = [r for r in rows if lo <= r[0] <= F]
        if len({r[1] for r in win}) >= need_n and sum(r[2] for r in win) >= need_val:
            out.append(F.strftime("%Y-%m-%d"))
            lock_until = F + timedelta(days=LOCK_DAYS)
    return out


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
    bars = {t: b for t, b in bars.items() if b is not None and len(b) > 60}
    spy = load_bars("SPY")
    axis = sorted(set().union(*[set(b.index) for b in bars.values()]) | set(spy.index))
    axis = [d for d in axis if START <= d <= END]
    split = axis[int(len(axis) * SPLIT)]
    seg_n = {s: sum(1 for t in bars if uni[t] == s) for s in SPREAD}
    print(f"Universum {len(bars)} Titel mit Kursen {seg_n}, {axis[0]}..{axis[-1]}, Holdout ab {split}")

    rets = []
    for b in bars.values():
        r = b.Close.pct_change()
        r = r[(r > -0.5) & (r < 1.0)]
        rets.append(r)
    ew = pd.concat(rets, axis=1).mean(axis=1).fillna(0.0)
    ew_idx = (1 + ew).cumprod()
    ewd = list(ew_idx.index); ewv = ew_idx.to_numpy()

    def ew_ret(d0, d1):
        i = bisect.bisect_left(ewd, d0) - 1; j = bisect.bisect_right(ewd, d1) - 1
        if i < 0 or j <= i:
            return None
        return ewv[j] / ewv[i] - 1

    sd = list(spy.index); so = spy.Open.to_numpy(); sc_ = spy.Close.to_numpy()

    def spy_ret(d0, d1):
        i = bisect.bisect_left(sd, d0); j = bisect.bisect_right(sd, d1) - 1
        return None if i >= len(sd) or j < i else sc_[j] / so[i] - 1

    pur = load_purchases(set(bars))
    print(f"Kaeufe (Form 4, Director/Officer, >= $10k, im Universum): {len(pur)} "
          f"in {pur.sym.nunique()} Titeln, Filings {pur.F.min().date()}..{pur.F.max().date()}")

    res = defaultdict(list)
    n_trig = defaultdict(int)
    for t, g in pur.groupby("sym"):
        b = bars[t]
        dates = list(b.index); o = b.Open.to_numpy(); l = b.Low.to_numpy(); c = b.Close.to_numpy()
        seg = uni[t]
        for var, (need_n, need_val) in VARIANTS.items():
            busy_until = {h: "" for h in HORIZONS}
            for F in triggers(g, need_n, need_val):
                e = bisect.bisect_right(dates, F)                  # erster Handelstag nach F
                if e >= len(dates) or not (START <= dates[e] <= END):
                    continue
                n_trig[var] += 1
                for h in HORIZONS:
                    if dates[e] <= busy_until[h]:
                        continue
                    if e + h - 1 >= len(dates) or dates[e + h - 1] > END:
                        continue                                   # Horizont nicht abgeschlossen
                    x = e + h - 1
                    ep = o[e]; stop = ep * (1 - STOP); xp, xi = c[x], x
                    for j in range(e, x + 1):
                        if l[j] <= stop:
                            xp, xi = min(o[j], stop), j
                            break
                    gr = xp / ep - 1
                    ewr = ew_ret(dates[e], dates[xi]); spr = spy_ret(dates[e], dates[xi])
                    if ewr is None or spr is None:
                        continue
                    res[(var, h)].append(dict(t=t, d=dates[e], g=gr, ew=ewr, spy=spr, seg=seg,
                                              cost=cost(seg), stop=xi < x))
                    busy_until[h] = dates[xi]
    print(f"Ausloesungen: {dict(n_trig)}")

    verdicts = {}
    for var in VARIANTS:
        for h in HORIZONS:
            rows = res[(var, h)]
            ho = [r for r in rows if r["d"] >= split]
            tr = [r for r in rows if r["d"] < split]
            if not ho:
                print(f"{var} H{h}: keine Holdout-Trades"); continue
            ex = [(r["g"] - r["ew"] - r["cost"], week(r["d"])) for r in ho]
            exv = [v for v, _ in ex]
            n = len(exv); m = sum(exv) / n
            t_ = cluster_t(ex); tm = trimmed(exv)
            weeks = len({k for _, k in ex})
            by = defaultdict(list)
            for r, v in zip(ho, exv):
                by[r["t"]].append(v)
            tot = sum(exv)
            loo = m > 0 and all((tot - sum(v)) / (n - len(v)) > 0 for v in by.values() if n - len(v) > 0)
            ab = [r["g"] - r["cost"] for r in ho]; ma = sum(ab) / n
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
                  f"Jahre positiv {yrs_pos}/{len(yr)}) | vs. SPY {np.mean([r['g']-r['spy']-r['cost'] for r in ho])*100:+.2f} % | "
                  f"absolut {ma*100:+.2f} %  -> {v_}")
            for s in SPREAD:
                ss = [v for r, v in zip(ho, exv) if r["seg"] == s]
                if ss:
                    sa = [r["g"] - r["cost"] for r in ho if r["seg"] == s]
                    print(f"   {s}: n={len(ss)}, Excess {np.mean(ss)*100:+.2f} %, absolut {np.mean(sa)*100:+.2f} %")
            print("   je Jahr Excess: " + ", ".join(f"{y} {np.mean(v)*100:+.1f}% (n={len(v)})" for y, v in sorted(yr.items())))
            print(f"   schlechteste 5 %: {np.percentile(ab, 5)*100:+.1f} %, beste 5 %: {np.percentile(ab, 95)*100:+.1f} %")
            if tr:
                print(f"   [Training] n={len(tr)} Excess {np.mean([r['g']-r['ew']-r['cost'] for r in tr])*100:+.2f} %, "
                      f"absolut {np.mean([r['g']-r['cost'] for r in tr])*100:+.2f} %")
    acc = sorted([(t_, k) for k, (v_, t_) in verdicts.items() if v_ == "ANGENOMMEN"], reverse=True)
    print("\nAngenommen:", [k for _, k in acc] if acc else "keine")
    if acc:
        print("Definitives Signal (hoechstes t):", acc[0][1])
    return 0


if __name__ == "__main__":
    sys.exit(main())
