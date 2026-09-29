#!/usr/bin/env python3
"""Shadow EXIT-LAYER REPLAY (Session 5).

Replays all CLOSED trades since 2026-07-26 (26.07-Zaesur) with alternative
exit stacks, purely from daily bars (yfinance 1y CSVs in
data/exit_replay_bars/, auto_adjust=True) + trade metadata from trading.db
(read-only). No look-ahead: entry = OPEN of first bar strictly after
trades.created_at date; indicator values used on bar i are computed from
bars <= i; chandelier stop ACTIVE on bar i is computed from bars < i.

Scenarios (ablation + full stack):
  S0 baseline   : fixed hard stop (trade's own stop_loss_pct, floor 3%) +
                  break-even arm +3% / floor +0.3% (current stack,
                  mechanical daily approximation — validation anchor)
  S1 wide_stop  : hard stop = entry - 3*ATR14(entry), clamped [2.5%,10%],
                  no trail (catastrophe stop only)
  S2 chandelier : S1 + chandelier trail: stop = max(entry, max(HH)) - 3*ATR,
                  ratchet-up only
  S3 reversion  : S2 + primary exit when RSI14 crosses >=55 OR close >=
                  entry + 1*ATR -> exit at NEXT bar's open
  S4 relax_be   : S3 + break-even arm at +1*ATR instead of +3%
  S5 time_stop  : S4 + time stop at 18 trading days (close)
  S6 atr_size   : S5 + size = (1% equity) / stop_width, capped 25% equity
  S7 full       : S6 + skip entry if close < own 200d SMA before entry

Cost: 0.18% round-trip of deployed capital per closed trade (advisor-
validated stock rate). Trades without bar data keep their realized PnL in
every scenario (label NODATA).

Validation: S0 vs realized DB PnL; holdout (train = created_at < 2026-08-30,
holdout = Sep); monthly folds (Jul/Aug/Sep); day-level block-bootstrap CIs.

Output: stdout table + data/exit_replay_results.json
"""
import json, math, os, statistics, sqlite3
from typing import Dict, List, Optional

REPO = "/home/mvolli/.hermes/workspace/etoro_v3"
DB = f"{REPO}/data/trading.db"
BARS = f"{REPO}/data/exit_replay_bars"
OUT = f"{REPO}/data/exit_replay_results.json"

EQUITY = 9112.96
RISK_PCT = 1.0                     # % of equity risked per trade (ATR sizing)
SIZE_CAP_PCT = 25.0                # % of equity max per position (stay small)
COST_RT = 0.0018                   # 0.18% round-trip
BE_FLOOR = 0.003                   # break-even floor +0.3%
TIME_STOP_D = 18
RSI_EXIT = 55
REV_ATR_MULT = 1.0
WIDE_K = 3.0
CHAN_K = 3.0
MIN_STOP_PCT = 0.025
MAX_STOP_PCT = 0.10
TAIL_BARS = 60                     # hard tail per trade

con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
con.row_factory = sqlite3.Row
q = lambda s, p=(): [dict(r) for r in con.execute(s, p).fetchall()]

# ---------------- price data ----------------
print("loading bar CSVs ...", flush=True)
series: Dict[str, List[tuple]] = {}
data_end_dates: Dict[str, str] = {}
for fn in os.listdir(BARS):
    if not fn.endswith(".csv"):
        continue
    sym = fn[:-4].replace("_", "/")
    rows = []
    with open(os.path.join(BARS, fn)) as f:
        next(f)
        for line in f:
            p = line.strip().split(",")
            if len(p) < 5 or not p[0]:
                continue
            try:
                rows.append((p[0], float(p[1]), float(p[2]), float(p[3]), float(p[4])))
            except ValueError:
                continue
    if rows:
        series[sym] = rows
        data_end_dates[sym] = rows[-1][0]
print(f"  {len(series)} series loaded", flush=True)
print("  bar data end dates:", sorted(set(data_end_dates.values())), flush=True)


def wilder_rsi(closes: List[float], period: int = 14) -> List[Optional[float]]:
    n = len(closes)
    out: List[Optional[float]] = [None] * n
    if n <= period:
        return out
    gains = losses = 0.0
    for i in range(1, period + 1):
        d = closes[i] - closes[i - 1]
        gains += max(d, 0.0)
        losses += max(-d, 0.0)
    ag, al = gains / period, losses / period
    out[period] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    for i in range(period + 1, n):
        d = closes[i] - closes[i - 1]
        ag = (ag * (period - 1) + max(d, 0.0)) / period
        al = (al * (period - 1) + max(-d, 0.0)) / period
        out[i] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    return out


pre: Dict[str, dict] = {}
for sym, rows in series.items():
    dts = [r[0] for r in rows]
    o = [r[1] for r in rows]
    h = [r[2] for r in rows]
    l = [r[3] for r in rows]
    c = [r[4] for r in rows]
    n = len(c)
    # TRAILING ATR: value at index i uses bars < i ONLY (no look-ahead).
    # (Session 6 advisor fix: the old window max(1, i-13)..i included bar i's
    #  high/low, leaking entry-day data into stops/sizing decisions.)
    atr: List[Optional[float]] = [None] * n
    for i in range(2, n):
        s = 0.0
        cnt = 0
        for j in range(max(1, i - 13), i):
            s += max(h[j] - l[j], abs(h[j] - c[j - 1]), abs(l[j] - c[j - 1]))
            cnt += 1
        if cnt:
            atr[i] = s / cnt
    rsi = wilder_rsi(c, 14)
    ma200: List[Optional[float]] = [None] * n
    run = 0.0
    for i in range(n):
        run += c[i]
        if i >= 200:
            run -= c[i - 200]
            ma200[i] = run / 200.0
    pre[sym] = dict(dts=dts, o=o, h=h, l=l, c=c, atr=atr, rsi=rsi, ma200=ma200)

# ---------------- trades ----------------
trades = q("""SELECT t.id, t.instrument_id, i.yfinance_symbol ysym,
   i.asset_class, t.entry_price, t.amount_usd, t.stop_loss_pct,
   t.pnl_usd realized_usd, t.pnl_pct realized_pct, t.created_at, t.closed_at,
   s.signal_type
   FROM trades t
   JOIN instruments i ON i.instrument_id = t.instrument_id
   LEFT JOIN signals s ON s.id = t.signal_id
   WHERE t.created_at >= '2026-07-26' AND t.status = 'CLOSED'""")
print(f"closed trades: {len(trades)}", flush=True)

SCENARIOS = [
    ("S0_baseline",   dict(stop="fixed", chan=False, rev=False, be="base", tstop=False, size="fixed", ma200=False)),
    ("S1_wide_stop",  dict(stop="wide",  chan=False, rev=False, be="base", tstop=False, size="fixed", ma200=False)),
    ("S2_chandelier", dict(stop="wide",  chan=True,  rev=False, be="base", tstop=False, size="fixed", ma200=False)),
    ("S3_reversion",  dict(stop="wide",  chan=True,  rev=True,  be="base", tstop=False, size="fixed", ma200=False)),
    ("S4_relax_be",   dict(stop="wide",  chan=True,  rev=True,  be="relax", tstop=False, size="fixed", ma200=False)),
    ("S5_time_stop",  dict(stop="wide",  chan=True,  rev=True,  be="relax", tstop=True,  size="fixed", ma200=False)),
    ("S6_atr_size",   dict(stop="wide",  chan=True,  rev=True,  be="relax", tstop=True,  size="atr",   ma200=False)),
    ("S7_filter_fixsz", dict(stop="wide", chan=True,  rev=True,  be="relax", tstop=True,  size="fixed", ma200=True)),
    ("S8_full_cap10", dict(stop="wide", chan=True,  rev=True,  be="relax", tstop=True,  size="atr10", ma200=True)),
    ("S7_full",       dict(stop="wide", chan=True,  rev=True,  be="relax", tstop=True,  size="atr",   ma200=True)),
    # Session-6 ablations: isolate the MA200 entry filter from exit levers
    ("S9_ma200_S3",   dict(stop="wide", chan=True,  rev=True,  be="base",  tstop=False, size="fixed", ma200=True)),
    ("S10_ma200_S1",  dict(stop="wide", chan=False, rev=False, be="base",  tstop=False, size="fixed", ma200=True)),
    ("S11_ma200_S5",  dict(stop="wide", chan=True,  rev=True,  be="relax", tstop=True,  size="fixed", ma200=True)),
    ("S12_ma200_S0",  dict(stop="fixed", chan=False, rev=False, be="base",  tstop=False, size="fixed", ma200=True)),
    # Session-6b (advisor): live-faithful filter decision = close[D-1] vs
    # SMA200 of closes ending D-1 (bot decides intraday on signal day D)
    ("S12b_L2",       dict(stop="fixed", chan=False, rev=False, be="base",  tstop=False, size="fixed", ma200=True, ma200_lag=2)),
    ("S9b_L2",        dict(stop="wide", chan=True,  rev=True,  be="base",  tstop=False, size="fixed", ma200=True, ma200_lag=2)),
]


def replay_trade(t: dict, sc: dict) -> Optional[dict]:
    """Returns per-trade result dict, or None if no bar data for the symbol."""
    P = pre.get(t["ysym"])
    if P is None:
        return None
    dts, o, h, l, c = P["dts"], P["o"], P["h"], P["l"], P["c"]
    atr, rsi = P["atr"], P["rsi"]
    n = len(dts)
    ed = t["created_at"][:10]
    e0 = None
    for i in range(n):
        if dts[i] > ed:
            e0 = i
            break
    if e0 is None or e0 >= n or atr[e0] is None or e0 < 15:
        return None

    ma_win = sc.get("ma_win", 200)
    rsi_exit = sc.get("rsi_exit", RSI_EXIT)
    chan_k = sc.get("chan_k", CHAN_K)
    if sc["ma200"]:
        # own-SMA entry filter. ma200_lag=1: decision at signal-day close
        # (c[e0-1] vs SMA200 of closes ending e0-1). ma200_lag=2: live-
        # faithful proxy — the live bot decides INTRADAY on signal day, so
        # it can only use closes up to the day BEFORE (advisor rule:
        # block if close[D-1] < SMA200(close[D-200..D-1])).
        r = e0 - sc.get("ma200_lag", 1)
        if r - ma_win + 1 < 0:
            return None  # no history -> cannot pass the filter
        sma_prior = sum(c[r - ma_win + 1:r + 1]) / float(ma_win)
        if c[r] < sma_prior:
            return dict(pnl=0.0, reason="BLOCKED_MA200", hold_days=0,
                        entry_date=dts[e0], exit_date=None, size_usd=0.0,
                        blocked=True, stop_pct=None)

    entry = o[e0]
    a0 = atr[e0]
    atr_pct = a0 / entry

    if sc["stop"] == "fixed":
        stop_pct = max(t["stop_loss_pct"] or 3.0, 3.0) / 100.0
    else:
        stop_pct = min(max(WIDE_K * atr_pct, MIN_STOP_PCT), MAX_STOP_PCT)
    stop0 = entry * (1 - stop_pct)

    if sc["size"] == "atr":
        size_usd = min((EQUITY * RISK_PCT / 100.0) / stop_pct,
                       EQUITY * SIZE_CAP_PCT / 100.0)
    elif sc["size"] == "atr10":
        # ATR sizing capped at the live instrument limit (10% of equity)
        size_usd = min((EQUITY * RISK_PCT / 100.0) / stop_pct,
                       EQUITY * 0.10)
    else:
        base = None
        if t["realized_pct"] and abs(t["realized_pct"]) > 0.15 and t["realized_usd"] is not None:
            base = t["realized_usd"] / (t["realized_pct"] / 100.0)
        size_usd = base if base else t["amount_usd"]

    be_arm = (entry * (1 + REV_ATR_MULT * atr_pct)) if sc["be"] == "relax" else (entry * 1.03)
    chan_stop: Optional[float] = None
    pending: Optional[str] = None  # close-based exit queued for next open
    exit_px = exit_i = None
    exit_reason = ""

    for i in range(e0, min(e0 + TAIL_BARS, n)):
        peak = max(entry, max(h[e0:i + 1]))
        # chandelier candidate using bars < i (no look-ahead)
        if sc["chan"] and i > e0:
            hh = max(entry, max(h[e0:i]))
            cand = hh - chan_k * (atr[i - 1] or 0.0)
            if chan_stop is None or cand > chan_stop:
                chan_stop = cand
        active = stop0
        if sc["chan"] and chan_stop is not None:
            active = max(active, chan_stop)
        # intrabar stop hit
        if l[i] <= active:
            exit_px = min(o[i], active) if o[i] < active else active
            exit_i, exit_reason = i, "STOP"
            break
        # close-based triggers (evaluated on bar i's close)
        trigger = None
        if sc["rev"]:
            r_i, r_im1 = rsi[i], rsi[i - 1]
            if r_i is not None and r_i >= rsi_exit:
                if r_im1 is None or r_im1 < rsi_exit:
                    trigger = "REVERSION"
                elif c[i] >= entry * (1 + REV_ATR_MULT * atr_pct):
                    trigger = "REVERSION"
            if c[i] >= entry * (1 + REV_ATR_MULT * atr_pct):
                trigger = "REVERSION"
        if trigger is None:
            be_hit = peak >= be_arm and c[i] <= entry * (1 + BE_FLOOR)
            if be_hit:
                trigger = "BE"
        if trigger is not None:
            pending = trigger
            if i + 1 < n:
                exit_px, exit_i, exit_reason = o[i + 1], i + 1, pending
            else:
                exit_px, exit_i, exit_reason = c[i], i, pending
            break
        if sc["tstop"] and (i - e0) >= TIME_STOP_D:
            exit_px, exit_i, exit_reason = c[i], i, "TIME"
            break

    if exit_i is None:
        i = min(e0 + TAIL_BARS - 1, n - 1)
        exit_px, exit_i, exit_reason = c[i], i, "TIME(tail)"

    ret = exit_px / entry - 1.0
    cost = COST_RT * size_usd
    pnl = ret * size_usd - cost
    return dict(pnl=pnl, reason=exit_reason, hold_days=exit_i - e0,
                entry_date=dts[e0], exit_date=dts[exit_i], size_usd=size_usd,
                blocked=False, stop_pct=stop_pct)


results: Dict[str, list] = {name: [] for name, _ in SCENARIOS}
nodata: Dict[str, int] = {}
for t in trades:
    for name, sc in SCENARIOS:
        r = replay_trade(t, sc)
        if r is None:
            nodata[name] = nodata.get(name, 0) + 1
            r = dict(pnl=t["realized_usd"] or 0.0, reason="NODATA(realized)",
                     hold_days=0, entry_date=None, exit_date=None,
                     size_usd=t["amount_usd"], blocked=False, stop_pct=None)
        r["trade_id"] = t["id"]
        r["created"] = t["created_at"]
        r["month"] = t["created_at"][:7]
        r["signal_type"] = t["signal_type"] or "CORE_SWEEP"
        r["realized_usd"] = t["realized_usd"]
        results[name].append(r)
print("no-data trades per scenario (kept at realized PnL):",
      {k: v for k, v in sorted(nodata.items()) if v})

# ---------------- aggregation ----------------

def day_series(rs: list) -> List[float]:
    d: Dict[str, float] = {}
    for r in rs:
        if r["reason"] == "BLOCKED_MA200":
            continue
        key = r["exit_date"] or r["created"][:10]
        d[key] = d.get(key, 0.0) + r["pnl"]
    return [v for k, v in sorted(d.items())]


def block_bootstrap(vals: List[float], n_boot: int = 2000, block: int = 5,
                    seed: int = 42) -> tuple:
    import random
    rng = random.Random(seed)
    m = len(vals)
    if m == 0:
        return 0.0, 0.0, 0.0
    if m < block:
        block = max(1, m)
    tot = sum(vals)
    nblk = max(1, math.ceil(m / block))
    boots = []
    for _ in range(n_boot):
        s = 0.0
        for _ in range(nblk):
            j = rng.randrange(m - block + 1)
            s += sum(vals[j:j + block])
        boots.append(s)
    boots.sort()
    return tot, boots[int(0.025 * n_boot)], boots[min(n_boot - 1, int(0.975 * n_boot))]


def summarize(rs: list) -> dict:
    active = [r for r in rs if r["reason"] != "BLOCKED_MA200"]
    blocked = [r for r in rs if r["reason"] == "BLOCKED_MA200"]
    pnls = [r["pnl"] for r in active]
    wins = sum(1 for p in pnls if p > 0)
    days = day_series(rs)
    tot, lo, hi = block_bootstrap(days)
    mdd, run = 0.0, 0.0
    for v in days:
        run += v
        mdd = min(mdd, run)
    holds = [r["hold_days"] for r in active]
    reason_mix: Dict[str, int] = {}
    for r in active:
        reason_mix[r["reason"]] = reason_mix.get(r["reason"], 0) + 1
    return dict(n=len(active), n_blocked=len(blocked), total=round(sum(pnls), 1),
                winrate=round(100 * wins / max(1, len(active)), 1),
                mean_per_trade=round(statistics.mean(pnls), 2) if pnls else 0.0,
                ci95=[round(lo, 1), round(hi, 1)], mdd=round(mdd, 1),
                median_hold=statistics.median(holds) if holds else 0,
                reason_mix=reason_mix,
                blocked_realized=round(sum(r["realized_usd"] or 0 for r in blocked), 1))


HOLDOUT_FROM = "2026-08-30"
WINS = {"all": (None, None),
        "train": (None, HOLDOUT_FROM),
        "holdout": (HOLDOUT_FROM, None),
        "2026-07": ("2026-07-01", "2026-08-01"),
        "2026-08": ("2026-08-01", "2026-09-01"),
        "2026-09": ("2026-09-01", None)}

table: Dict[str, dict] = {}
for name, _ in SCENARIOS:
    table[name] = {}
    for wname, (a, b) in WINS.items():
        sel = [r for r in results[name]
               if (a is None or r["created"] >= a) and (b is None or r["created"] < b)]
        table[name][wname] = summarize(sel)

table["__realized__"] = {}
for wname, (a, b) in WINS.items():
    sel = [t for t in trades
           if (a is None or t["created_at"] >= a) and (b is None or t["created_at"] < b)]
    p = [t["realized_usd"] or 0 for t in sel]
    wins = sum(1 for x in p if x > 0)
    table["__realized__"][wname] = dict(n=len(p), total=round(sum(p), 1),
                                        winrate=round(100 * wins / max(1, len(p)), 1),
                                        mean_per_trade=round(statistics.mean(p), 2) if p else 0.0)

# ---------------- print ----------------
for wname in WINS:
    print(f"\n===== WINDOW: {wname} =====")
    hdr = (f"{'scenario':16s} {'n':>4s} {'PnL$':>9s} {'CI95':>16s} {'$/trade':>8s} "
           f"{'WR%':>5s} {'MDD$':>8s} {'hold':>4s} {'blk':>4s} {'blkReal$':>9s}")
    print(hdr)
    for name, _ in SCENARIOS:
        s = table[name][wname]
        print(f"{name:16s} {s['n']:>4d} {s['total']:>9.1f} {str(s['ci95']):>16s} "
              f"{s['mean_per_trade']:>8.2f} {s['winrate']:>5.1f} {s['mdd']:>8.1f} "
              f"{s['median_hold']:>4.0f} {s['n_blocked']:>4d} {s['blocked_realized']:>9.1f}")
    s = table["__realized__"][wname]
    print(f"{'REALIZED (DB)':16s} {s['n']:>4d} {s['total']:>9.1f} {'-':>16s} "
          f"{s['mean_per_trade']:>8.2f} {s['winrate']:>5.1f}")

for name in ["S0_baseline", "S3_reversion", "S6_atr_size", "S7_full"]:
    print(f"\nreason mix (all) {name}: {table[name]['all']['reason_mix']}")

# ---- edge robustness probe: top-3 removal + CI, per window ----
print("\nEDGE ROBUSTNESS (per window): sum minus top-3 winning trades, boot CI")
for name in ["S5_time_stop", "S7_filter_fixsz", "S8_full_cap10", "S7_full",
             "S9_ma200_S3", "S10_ma200_S1", "S11_ma200_S5", "S12_ma200_S0"]:
    line = []
    for wname in ["all", "train", "holdout"]:
        rs = [r for r in results[name]
              if (wname != "train" or r["created"] < HOLDOUT_FROM)
              and (wname != "holdout" or r["created"] >= HOLDOUT_FROM)]
        pnls = sorted([r["pnl"] for r in rs if r["reason"] != "BLOCKED_MA200"],
                      reverse=True)
        without = pnls[3:]
        tot, lo, hi = block_bootstrap(without)
        line.append(f"{wname}: sum={tot:8.1f} CI=[{lo:8.1f},{hi:8.1f}] (n={len(pnls)})")
    print(f"  {name:18s} | " + " | ".join(line))

# ---- robustness grid: MA window x RSI exit x chandelier k ----
print("\nROBUSTNESS GRID (PnL$, all / holdout, n-pass):")
print("  full = wide stop + chan + reversion + BE-relax + time-stop + fixed size")
for ma_win in [100, 150, 200]:
    for rsi_exit in [50, 55, 60]:
        for chan_k in [2.5, 3.0]:
            sc = dict(stop="wide", chan=True, rev=True, be="relax", tstop=True,
                      size="fixed", ma200=True, ma_win=ma_win,
                      rsi_exit=rsi_exit, chan_k=chan_k)
            pnls_all, pnls_ho, npass, nblock = [], [], 0, 0
            for t in trades:
                r = replay_trade(t, sc)
                if r is None:
                    continue
                if r["blocked"]:
                    nblock += 1
                    continue
                npass += 1
                pnls_all.append(r["pnl"])
                if t["created_at"] >= HOLDOUT_FROM:
                    pnls_ho.append(r["pnl"])
            s_all = sum(pnls_all) if pnls_all else 0.0
            s_ho = sum(pnls_ho) if pnls_ho else 0.0
            print(f"  MA{ma_win} RSI{rsi_exit} K{chan_k}: all={s_all:8.1f} "
                  f"ho={s_ho:8.1f}  (n={npass}, blocked={nblock})")

# per-signal-type for full stack
print("\nS7_full by signal type (all window):")
by_type: Dict[str, List[float]] = {}
for r in results["S7_full"]:
    by_type.setdefault(r["signal_type"], []).append(r["pnl"])
for st, pnls in sorted(by_type.items(), key=lambda kv: sum(kv[1])):
    w = sum(1 for p in pnls if p > 0)
    print(f"  {st:42s} n={len(pnls):3d} sum={sum(pnls):>8.1f} "
          f"WR={100*w/max(1,len(pnls)):5.1f}%")

# ---- filter counterfactual: what do BLOCKED trades earn WITHOUT the filter? ----
print("\nFILTER COUNTERFACTUAL (MA200 scenarios): blocked trades replayed "
      "without the filter, same exit levers (opportunity cost of blocking)")
for name in ["S9_ma200_S3", "S10_ma200_S1", "S11_ma200_S5", "S12_ma200_S0"]:
    sc = dict(next(sc for nm, sc in SCENARIOS if nm == name), ma200=False)
    line = []
    for wname in ["all", "train", "holdout"]:
        rs = [t for t in trades
              if (wname != "train" or t["created_at"] < HOLDOUT_FROM)
              and (wname != "holdout" or t["created_at"] >= HOLDOUT_FROM)]
        pnls = []
        for t in rs:
            # only the trades that the filter WOULD block
            scb = dict(next(s2 for nm, s2 in SCENARIOS if nm == name))
            r_b = replay_trade(t, scb)
            if r_b is None or not r_b.get("blocked"):
                continue
            r = replay_trade(t, sc)
            if r is not None:
                pnls.append(r["pnl"])
        if pnls:
            line.append(f"{wname}: n={len(pnls)} sum={sum(pnls):8.1f} "
                        f"$/trade={sum(pnls)/len(pnls):6.2f} WR={100*sum(1 for p in pnls if p>0)/len(pnls):5.1f}%")
        else:
            line.append(f"{wname}: n=0")
    print(f"  {name:18s} | " + " | ".join(line))

# ---- per-month for the MA200 entry-filter candidates ----
print("\nPER-MONTH PnL$ for MA200 entry-filter candidates (active trades):")
for name in ["S9_ma200_S3", "S10_ma200_S1", "S11_ma200_S5", "S12_ma200_S0"]:
    row = []
    for m in ["2026-07", "2026-08", "2026-09"]:
        pnls = [r["pnl"] for r in results[name]
                if r["month"] == m and r["reason"] != "BLOCKED_MA200"]
        if pnls:
            row.append(f"{m}: n={len(pnls):3d} sum={sum(pnls):7.1f} "
                       f"WR={100*sum(1 for p in pnls if p>0)/len(pnls):5.1f}%")
        else:
            row.append(f"{m}: n=0")
    print(f"  {name:18s} | " + " | ".join(row))

# ---- MEASUREMENT 1 (advisor nullth step): realized blocked vs passed, DB only ----
# The filter's strongest evidence is regime-independent: which realized trades
# would the MA200 filter have blocked vs passed? (S12 = filter + today's exits.)
print("\nMEASUREMENT 1 — REALIZED blocked vs passed (trading.db only, no replay mechanics)")
S12_SC = dict(next(sc for nm, sc in SCENARIOS if nm == "S12_ma200_S0"))
S12B_SC = dict(next(sc for nm, sc in SCENARIOS if nm == "S12b_L2"))
for tag, sc in [("S12 (decision on signal-day close D)", S12_SC),
                ("S12b (live-faithful decision on close[D-1])", S12B_SC)]:
    print(f"  [{tag}]")
    for wname in ["all", "train", "holdout"]:
        recs = []
        for t in trades:
            if (wname != "train" or t["created_at"] < HOLDOUT_FROM) \
               and (wname != "holdout" or t["created_at"] >= HOLDOUT_FROM):
                r = replay_trade(t, sc)
                if r is None:
                    continue
                recs.append(dict(day=t["created_at"][:10], sym=t["ysym"],
                                 blocked=1 if r.get("blocked") else 0,
                                 pnl=float(t["realized_usd"] or 0.0)))
        if not recs:
            print(f"    {wname:8s}: n=0")
            continue
        pb = [x["pnl"] for x in recs if x["blocked"]]
        pa = [x["pnl"] for x in recs if not x["blocked"]]
        mb, ma_ = sum(pb) / len(pb), sum(pa) / len(pa)
        diff = ma_ - mb  # per-trade edge of PASSING vs being blocked
        for unit, key in [("day", "day"), ("symbol", "sym")]:
            import random
            rng = random.Random(42)
            groups = {}
            for x in recs:
                groups.setdefault(x[key], []).append(x)
            boots = []
            for _ in range(2000):
                gs = [groups[g] for g in rng.choices(list(groups), k=len(groups))]
                tot = 0.0
                for g in gs:
                    b = sum(x["pnl"] for x in g if x["blocked"])
                    a = sum(x["pnl"] for x in g if not x["blocked"])
                    nb, na = sum(x["blocked"] for x in g), sum(1 - x["blocked"] for x in g)
                    if nb and na:
                        tot += (a / na) - (b / nb)
                boots.append(tot / max(1, len(gs)))
            boots.sort()
            lo, hi = boots[50], boots[1949]
            print(f"    {wname:8s}: n={len(recs):3d} passed={len(pa):3d} ({sum(pa):8.1f} $, {ma_:6.2f} $/tr) "
                  f"blocked={len(pb):3d} ({sum(pb):8.1f} $, {mb:6.2f} $/tr) "
                  f"diff(passed−blocked)={diff:+7.2f} $/tr  boot-CI[{unit}] [{lo:+7.2f},{hi:+7.2f}]")

# ---- MEASUREMENT 2 (advisor nullth step): fixed-date window, >=10 bars follow-through ----
# Trades whose entry is >= 10 bars before their symbol's last bar: fully closed
# within the data, immune to the mark-to-market date.
print("\nMEASUREMENT 2 — FIXED-DATE WINDOW (entry + 10 bars <= symbol's last bar)")
MIN_FOLLOW = 10
eligible = []
for t in trades:
    sym = t["ysym"]
    P = pre.get(sym)
    if P is None:
        continue
    dts = P["dts"]
    ed = t["created_at"][:10]
    e0 = None
    for i in range(len(dts)):
        if dts[i] > ed:
            e0 = i
            break
    if e0 is None or e0 + MIN_FOLLOW >= len(dts):
        continue
    eligible.append(t)
print(f"  eligible trades: {len(eligible)} / {len(trades)} "
      f"(train {sum(1 for t in eligible if t['created_at'] < HOLDOUT_FROM)} / "
      f"holdout {sum(1 for t in eligible if t['created_at'] >= HOLDOUT_FROM)})")
for name in ["S0_baseline", "S3_reversion", "S7_filter_fixsz", "S11_ma200_S5",
             "S7_full", "S8_full_cap10", "S12_ma200_S0", "S12b_L2"]:
    sc = next(s2 for nm, s2 in SCENARIOS if nm == name)
    row = []
    for wname in ["all", "train", "holdout"]:
        pnls, pnls_block = [], []
        for t in eligible:
            if (wname != "train" or t["created_at"] < HOLDOUT_FROM) \
               and (wname != "holdout" or t["created_at"] >= HOLDOUT_FROM):
                r = replay_trade(t, sc)
                if r is None:
                    continue
                (pnls_block if r.get("blocked") else pnls).append(r["pnl"])
        if pnls:
            tot, lo, hi = block_bootstrap(pnls)
            row.append(f"{wname}: n={len(pnls):3d} sum={tot:8.1f} CI=[{lo:8.1f},{hi:8.1f}] "
                       f"WR={100*sum(1 for p in pnls if p>0)/len(pnls):5.1f}% blkReal$={sum(pnls_block):8.1f}(n={len(pnls_block)})")
        else:
            row.append(f"{wname}: n=0")
    print(f"  {name:18s} | " + " | ".join(row))

with open(OUT, "w") as f:
    json.dump({"table": table,
               "nodata": nodata,
               "params": dict(RISK_PCT=RISK_PCT, SIZE_CAP_PCT=SIZE_CAP_PCT,
                              COST_RT=COST_RT, WIDE_K=WIDE_K, CHAN_K=CHAN_K,
                              TIME_STOP_D=TIME_STOP_D, BE_FLOOR=BE_FLOOR,
                              RSI_EXIT=RSI_EXIT, REV_ATR_MULT=REV_ATR_MULT,
                              HOLDOUT_FROM=HOLDOUT_FROM, TAIL_BARS=TAIL_BARS)},
              f, indent=2)
print(f"\nresults -> {OUT}")
