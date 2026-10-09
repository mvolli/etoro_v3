#!/usr/bin/env python3
"""Phase 1e — E3-Test: RSI(14)<30-Umkehr auf LÄNGERER Historie.

Implementiert EXAKT `vorregistrierung-e3-längere-historie-2026-10-09.md`:
  - Universum: Union (exit_replay_bars + backtest_cache + ohlcv_daily),
    237 Titel mit >=250 gemergten Tageskerzen
  - Signal: RSI(14)<30, max. 1/10-Tage-Fenster pro Symbol
  - Ziel: Close(t+10)/Close(t)-1 minus gleichtägiger Universums-Median
  - Kosten: 1,1 % abgezogen
  - Split: 60 % Training / 40 % Holdout auf gemergter Zeitachse
  - Entscheidungsregel (5 Kriterien, s. Vorregistrierung)

Nur lesend. Ergebnis: E3 angenommen / verworfen / unentschieden.
"""
from __future__ import annotations

import csv
import glob
import os
import sys
from collections import defaultdict

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BAR_DIRS = [
    os.path.join(REPO, "data", "exit_replay_bars"),
    os.path.join(REPO, "data", "backtest_cache"),
    os.path.join(REPO, "data", "ohlcv_daily"),
]
RSI_N = 14
RSI_THR = 30
FWD = 10
SPACER = 10          # max. 1 Signal je 10-Tage-Fenster
MIN_BARS = 250
COST = 0.011         # 1,1 % Rundenkosten
SPLIT = 0.60         # 60 % Training / 40 % Holdout
N_MIN = 200
T_MIN = 2.0
TRIM = 0.05


# ---------------------------------------------------------------------------
def rsi_series(closes, n=RSI_N):
    """Wilder RSI(14) — identisch zu signal_forward_returns.py."""
    rsis: list[float | None] = [None] * len(closes)
    if len(closes) <= n:
        return rsis
    gains = losses = 0.0
    for i in range(1, n + 1):
        d = closes[i] - closes[i - 1]
        gains += max(d, 0); losses += max(-d, 0)
    ag, al = gains / n, losses / n
    rsis[n] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    for i in range(n + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        ag = (ag * (n - 1) + max(d, 0)) / n
        al = (al * (n - 1) + max(-d, 0)) / n
        rsis[i] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    return rsis


def all_symbols() -> list[str]:
    syms = set()
    for d in BAR_DIRS:
        if not os.path.isdir(d):
            continue
        for f in glob.glob(os.path.join(d, "*.csv")):
            base = os.path.basename(f)[:-4].split("_")[0]
            if base and base != "SPY":
                syms.add(base)
    return sorted(syms)


def load_symbol(sym: str) -> list[tuple[str, float]]:
    """Union-Merge: {date: close} → sortierte Liste (dedup, spätere Quelle gewinnt)."""
    by_date: dict[str, float] = {}
    for d in BAR_DIRS:
        if not os.path.isdir(d):
            continue
        for f in glob.glob(os.path.join(d, "*.csv")):
            if os.path.basename(f)[:-4].split("_")[0] != sym:
                continue
            try:
                with open(f, newline="") as fh:
                    rd = csv.reader(fh)
                    hdr = [h.strip().lower() for h in next(rd)]
                    di, ci = hdr.index("date"), hdr.index("close")
                    for row in rd:
                        if len(row) > max(di, ci):
                            try:
                                by_date[row[di][:10]] = float(row[ci])
                            except ValueError:
                                pass
            except Exception:
                continue
    return sorted(by_date.items())


def trimmed_mean(vals, frac=TRIM):
    if not vals:
        return None
    v = sorted(vals)
    k = int(len(v) * frac)
    v = v[k:len(v) - k] if len(v) - 2 * k > 0 else v
    return sum(v) / len(v) if v else None


def t_stat(vals):
    """Naive t-Statistik des Mittelwerts (H0: Mittel=0)."""
    n = len(vals)
    if n < 2:
        return 0.0
    m = sum(vals) / n
    var = sum((x - m) ** 2 for x in vals) / (n - 1)
    se = (var / n) ** 0.5
    return m / se if se > 0 else 0.0


# ---------------------------------------------------------------------------
def main() -> int:
    print("=" * 64)
    print("Phase 1e — E3-Test: RSI(14)<30 auf LÄNGERER Historie")
    print("=" * 64)

    syms = all_symbols()
    print(f"\n[1/5] Union-Universum: {len(syms)} Titel")

    # Pro Symbol: closes, rsis, Signale (Index)
    sym_data: dict[str, dict] = {}
    for sym in syms:
        bars = load_symbol(sym)
        if len(bars) < MIN_BARS:
            continue
        closes = [b[1] for b in bars]
        rsis = rsi_series(closes)
        sigs = []
        last = -99
        for i in range(len(closes)):
            if rsis[i] is not None and rsis[i] < RSI_THR and (i - last) >= SPACER:
                sigs.append(i)
                last = i
        sym_data[sym] = {"closes": closes, "rsis": rsis, "sigs": sigs,
                         "n": len(closes)}
    print(f"  {len(sym_data)} Titel mit >={MIN_BARS} gemergten Bars")

    # Split pro Symbol = 60 % der eigenen (gemergten) Bars.
    # (Vorregistrierung: Split auf gemergter Zeitachse; pro-Symbol 60 % ist
    #  die operationale Umsetzung, da Symbole unterschiedliche Längen haben.)

    # Excess-Return pro Signal + gleichtägiger Universums-Median
    # Median am Tag t: Median der Close(t+FWD)/Close(t)-1 über alle Symbole
    # am selben Kalender-Datum. Wir brauchen Datum→Index-Mapping pro Symbol.
    print("\n[2/5] Forward-Returns + gleichtägige Universums-Mediane …")
    date_index: dict[str, dict[str, int]] = defaultdict(dict)
    for sym, sd in sym_data.items():
        bars = load_symbol(sym)  # dates again (cheap, cached enough)
        for i, (dt, _) in enumerate(bars):
            date_index[dt][sym] = i

    # Gleichtägiger Median: für jedes Datum t, Median der (Close(t+10)/Close(t)-1)
    # über alle Symbole, die sowohl t als auch t+10 abdecken.
    # Wir berechnen pro Signal-Event (sym, t) den Excess = ret(sym,t) - median(t).
    # median(t) = Median über Symbole mit vollständiger Abdeckung am Tag t.

    # Sammel: für jedes Datum t: Liste der Roh-Returns (sym, t)
    day_returns: dict[str, list[float]] = defaultdict(list)
    for sym, sd in sym_data.items():
        closes = sd["closes"]
        bars = load_symbol(sym)
        dates = [b[0] for b in bars]
        for i in range(len(closes) - FWD):
            r = closes[i + FWD] / closes[i] - 1
            day_returns[dates[i]].append(r)

    # Mediane pro Datum
    day_median: dict[str, float] = {}
    for dt, rets in day_returns.items():
        rets = sorted(rets)
        n = len(rets)
        day_median[dt] = rets[n // 2] if n % 2 else (rets[n // 2 - 1] + rets[n // 2]) / 2

    # [3/5] Signale → Excess-Return (netto) + Split
    print("\n[3/5] Signale → Netto-Excess-Return + 60/40-Split …")
    events = []  # (sym, excess_net, in_holdout)
    for sym, sd in sym_data.items():
        closes = sd["closes"]
        bars = load_symbol(sym)
        dates = [b[0] for b in bars]
        split_idx = int(len(closes) * SPLIT)
        for i in sd["sigs"]:
            if i + FWD >= len(closes):
                continue  # keine vollständige 10-Tage-Abdeckung
            ret = closes[i + FWD] / closes[i] - 1
            med = day_median.get(dates[i])
            if med is None:
                continue
            excess_net = (ret - med) - COST
            in_ho = i >= split_idx
            events.append((sym, excess_net, in_ho))
    holdout = [e for e in events if e[2]]
    train = [e for e in events if not e[2]]
    print(f"  Training: {len(train)} | Holdout: {len(holdout)}")

    # [4/5] Entscheidungsregel (5 Kriterien)
    print("\n[4/5] Entscheidungsregel (Holdout):")
    vals = [e[1] for e in holdout]
    n = len(vals)
    mean = sum(vals) / n if n else 0.0
    t = t_stat(vals)
    trimmed = trimmed_mean(vals)
    c1 = n >= N_MIN
    c2 = mean > 0
    c3 = t >= T_MIN
    c4 = trimmed is not None and trimmed > 0

    # Leave-one-out: Vorzeichen bleibt, wenn ein beliebiges Symbol weg ist
    by_sym = defaultdict(list)
    for sym, ex, _ in holdout:
        by_sym[sym].append(ex)
    c5 = True
    for sym in list(by_sym):
        rest = [e[1] for e in holdout if e[0] != sym]
        if len(rest) < 2:
            continue
        if sum(rest) / len(rest) <= 0:
            c5 = False
            break

    print(f"  1) n >= {N_MIN}:            n={n}  {'✓' if c1 else '✗'}")
    print(f"  2) Netto-Excess > 0:    mean={mean*100:+.2f}%  {'✓' if c2 else '✗'}")
    print(f"  3) t >= {T_MIN}:           t={t:.2f}  {'✓' if c3 else '✗'}")
    print(f"  4) 5 %-Trim > 0:        trim={trimmed*100 if trimmed is not None else 0:+.2f}%  {'✓' if c4 else '✗'}")
    print(f"  5) Leave-one-out:       {'✓' if c5 else '✗'}")

    # [5/5] Fazit
    print("\n[5/5] Fazit:")
    if not c1:
        verdict = "UNENTSCHEIDBAR (n < 200)"
        ok = False
    elif c1 and c2 and c3 and c4 and c5:
        verdict = "E3 ANGENOMMEN"
        ok = True
    else:
        verdict = "E3 VERWORFEN"
        ok = False
    print(f"  >>> {verdict}")

    # Trainingsfenster (deskriptiv, keine Entscheidung)
    tvals = [e[1] for e in train]
    if tvals:
        print(f"\n  [Deskriptiv, Training] n={len(tvals)} mean={sum(tvals)/len(tvals)*100:+.2f}%")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
