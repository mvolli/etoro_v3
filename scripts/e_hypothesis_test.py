#!/usr/bin/env python3
"""Pre-registered test for E2 (docs/vorregistrierung-e2-rsi-umkehr-2026-10-09.md).
RSI(14) < 30, 10-day excess return vs same-day universe median, 1.1% round-trip cost,
holdout = last 40 %% of trading days. Read-only on data/exit_replay_bars.
"""
import csv, glob, math, os, statistics as st, collections
ROOT = '/home/mvolli/.hermes/workspace/etoro_v3/data/exit_replay_bars'
COST = 0.011; H = 10
series = {}
for f in glob.glob(os.path.join(ROOT, '*.csv')):
    sym = os.path.basename(f)[:-4]
    rows = []
    with open(f) as fh:
        for r in csv.DictReader(fh):
            try: rows.append((r['date'], float(r['close'])))
            except (KeyError, ValueError): pass
    rows = sorted(set(rows))
    if len(rows) >= 250: series[sym] = rows
print('Universum (>=250 Kerzen):', len(series))
def rsi14(c):
    out = [None]*len(c)
    if len(c) < 15: return out
    g = [max(c[i]-c[i-1],0) for i in range(1,len(c))]; l = [max(c[i-1]-c[i],0) for i in range(1,len(c))]
    ag = sum(g[:14])/14; al = sum(l[:14])/14
    out[14] = 100 - 100/(1+ (ag/al if al else 1e9))
    for i in range(15, len(c)):
        ag = (ag*13 + g[i-1])/14; al = (al*13 + l[i-1])/14
        out[i] = 100 - 100/(1+(ag/al if al else 1e9))
    return out
fwd = {}   # (sym, date) -> fwd return
sig = []   # (sym, date, excess_raw)
ind = {}
for sym, rows in series.items():
    dates = [d for d,_ in rows]; c = [x for _,x in rows]
    r = rsi14(c)
    sma = [None]*len(c)
    for i in range(49, len(c)): sma[i] = sum(c[i-49:i+1])/50
    for i in range(len(c)-H):
        fwd[(sym, dates[i])] = c[i+H]/c[i]-1
    last = -999
    for i in range(len(c)-H):
        if r[i] is not None and sma[i] is not None and r[i] < 30 and i - last >= H:
            sig.append((sym, dates[i], i)); last = i
# Tagesmedian ueber alle Symbole mit Fwd-Rendite
bydate = collections.defaultdict(list)
for (sym, d), v in fwd.items(): bydate[d].append(v)
med = {d: st.median(v) for d, v in bydate.items() if len(v) >= 5}
ev = []
for sym, d, i in sig:
    if d in med:
        ev.append((sym, d, fwd[(sym,d)] - med[d] - COST))
alld = sorted(med)
cut = alld[int(len(alld)*0.6)]
train = [e for e in ev if e[1] < cut]; hold = [e for e in ev if e[1] >= cut]
print('Handelstage gesamt:', len(alld), '| Split-Datum (Holdout ab):', cut)
print('Signale gesamt:', len(ev), '| Train:', len(train), '| Holdout:', len(hold))
def stats(xs, label):
    if len(xs) < 2: print(label, 'zu wenig Daten'); return
    v = [e[2] for e in xs]; m = st.mean(v); sd = st.stdev(v); t = m/(sd/math.sqrt(len(v)))
    k = int(len(v)*0.05); tr = sorted(v)[k:len(v)-k] if k>0 else v
    print(f'{label}: n={len(v)} mean_net={100*m:+.2f}% t={t:+.2f} getrimmt5%={100*st.mean(tr):+.2f}% hit={100*sum(x>0 for x in v)/len(v):.0f}%')
    syms = sorted(set(e[0] for e in xs)); loo = []
    for s_ in syms:
        rest = [e[2] for e in xs if e[0] != s_]
        if rest: loo.append((st.mean(rest), s_))
    if loo:
        worst = min(loo); print(f'   Leave-one-out: min mean={100*worst[0]:+.2f}% (ohne {worst[1]}), Symbole={len(syms)}')
    return m, t
stats(train, 'TRAIN (nur Beschreibung)')
res = stats(hold, 'HOLDOUT (Entscheidung)')
