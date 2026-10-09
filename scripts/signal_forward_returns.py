#!/usr/bin/env python3
"""
Phase 1a — Forward-Returns für alle Signale (90 Tage).

Liest:  signals (trading.db), exit_replay_bars/*.csv, SPY.csv (Benchmark)
Schreibt: signal_outcomes (CREATE IF NOT EXISTS, idempotent via UNIQUE)

Features je Signal:
  - fwd_1d / fwd_3d / fwd_5d / fwd_10d  (netto, 1.1 % Round-Trip-Kosten)
  - ma200_pos       (Preis vs. SMA200, positiv = darüber)
  - rsi_depth       (RSI14 am Signal-Tag)
  - bench_trend     (SPY 20d-Rendite am Signal-Tag)
  - vol_ratio       (Volumen Signal-Tag / 20d-Durchschnitt)
  - atr_dist        (Abstand in ATR14-Einheiten vom 20d-Mittel)
  - co_signals      (Anzahl Ko-Signale, gleiches Instrument, gleicher Tag)

Zusätzlich: Verlustprofil-TREND_PULLBACK (D3) als separater Report.

Aufruf:  python scripts/signal_forward_returns.py
"""
import csv
import glob
import json
import math
import os
import sqlite3
import statistics as st
import sys
from collections import defaultdict
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(ROOT, "data", "trading.db")
BARS = os.path.join(ROOT, "data", "exit_replay_bars")
IMAP = os.path.join(ROOT, "src", "data", "instrument_map.json")
COST = 0.011  # 1.1 % Round-Trip (identisch zu e_hypothesis_test.py)
HORIZONS = (1, 3, 5, 10)
LOOKBACK_DAYS = 90

# ---------------------------------------------------------------------------
# Indikator-Helfer
# ---------------------------------------------------------------------------

def sma(vals, n, i):
    """Gleitender Durchschnitt; None wenn nicht genug Daten."""
    if i + 1 < n:
        return None
    return sum(vals[i - n + 1 : i + 1]) / n


def rsi14_series(closes):
    """RSI(14) als Liste (None für erste 14 Einträge)."""
    n = len(closes)
    out: list = [None] * n
    if n < 15:
        return out
    gains = [max(closes[i] - closes[i - 1], 0) for i in range(1, n)]
    losses = [max(closes[i - 1] - closes[i], 0) for i in range(1, n)]
    ag = sum(gains[:14]) / 14
    al = sum(losses[:14]) / 14
    out[14] = 100.0 - 100.0 / (1.0 + (ag / al if al > 0 else 1e9))
    for i in range(15, n):
        ag = (ag * 13 + gains[i - 1]) / 14
        al = (al * 13 + losses[i - 1]) / 14
        out[i] = 100.0 - 100.0 / (1.0 + (ag / al if al > 0 else 1e9))
    return out


def atr14_series(highs, lows, closes):
    """ATR(14) als Liste (None für erste 14 Einträge)."""
    n = len(closes)
    out: list = [None] * n
    if n < 15:
        return out
    trs = []
    for i in range(1, n):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        )
        trs.append(tr)
    a = sum(trs[:14]) / 14
    out[14] = a
    for i in range(15, n):
        a = (a * 13 + trs[i - 1]) / 14
        out[i] = a
    return out


# ---------------------------------------------------------------------------
# Bar-Loader
# ---------------------------------------------------------------------------

def load_bar(path):
    """Lädt eine CSV-Bar-Datei → (dates, opens, highs, lows, closes, volumes)."""
    dates, opens, highs, lows, closes, vols = [], [], [], [], [], []
    with open(path) as fh:
        for r in csv.DictReader(fh):
            try:
                dates.append(r["date"])
                opens.append(float(r["open"]))
                highs.append(float(r["high"]))
                lows.append(float(r["low"]))
                closes.append(float(r["close"]))
                vols.append(float(r.get("volume", 0) or 0))
            except (KeyError, ValueError):
                pass
    # dedup + sort
    seen = {}
    for i, d in enumerate(dates):
        seen[d] = i
    idx = sorted(seen.values(), key=lambda i: dates[i])
    return (
        [dates[i] for i in idx],
        [opens[i] for i in idx],
        [highs[i] for i in idx],
        [lows[i] for i in idx],
        [closes[i] for i in idx],
        [vols[i] for i in idx],
    )


def build_bar_cache(bar_dir):
    """Alle Bar-Dateien laden → {symbol: (dates, o, h, l, c, v)}."""
    cache = {}
    for f in glob.glob(os.path.join(bar_dir, "*.csv")):
        sym = os.path.basename(f)[:-4]
        try:
            cache[sym] = load_bar(f)
        except Exception:
            pass
    return cache


# ---------------------------------------------------------------------------
# Signal-Lade + Dedup
# ---------------------------------------------------------------------------

def load_signals(db, lookback_days=LOOKBACK_DAYS):
    """Alle Signale der letzten N Tage, dedupliziert pro (instrument_id, date)."""
    rows = db.execute(
        """
        SELECT id, instrument_id, generated_at, signal_type,
               conviction, score, rsi, macd_hist, bb_pct, price, status
        FROM signals
        WHERE generated_at >= datetime('now', ?)
        ORDER BY generated_at
        """,
        (f"-{lookback_days} days",),
    ).fetchall()

    seen = set()
    deduped = []
    for r in rows:
        d = r[2][:10]  # date part
        key = (r[1], d, r[3])  # (instrument_id, date, signal_type)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(r)
    return deduped


def count_co_signals(db, signals):
    """Anzahl Ko-Signale pro (instrument_id, date) — exkl. das eigene."""
    co = defaultdict(int)
    for r in signals:
        d = r[2][:10]
        co[(r[1], d)] += 1
    return co


# ---------------------------------------------------------------------------
# Benchmark (SPY)
# ---------------------------------------------------------------------------

def build_benchmark(bar_cache):
    """SPY 20d-Rendite pro Datum → {date: ret_20d}."""
    if "SPY" not in bar_cache:
        return {}
    dates, _, _, _, closes, _ = bar_cache["SPY"]
    out = {}
    for i in range(20, len(closes)):
        out[dates[i]] = closes[i] / closes[i - 20] - 1
    return out


# ---------------------------------------------------------------------------
# Forward-Return + Features
# ---------------------------------------------------------------------------

def compute_forward_and_features(bar, signal_date, price_at_signal):
    """
    Gibt dict mit fwd_returns + features zurück, oder None wenn Datum fehlt.
    bar = (dates, opens, highs, lows, closes, vols)
    """
    dates, opens, highs, lows, closes, vols = bar
    if signal_date not in dates:
        return None
    i = dates.index(signal_date)
    n = len(closes)

    # --- Forward Returns ---
    fwd = {}
    for h in HORIZONS:
        if i + h < n:
            fwd[f"fwd_{h}d"] = closes[i + h] / closes[i] - 1 - COST
        else:
            fwd[f"fwd_{h}d"] = None

    # --- Features ---
    # ma200
    ma200 = sma(closes, 200, i)
    ma200_pos = (closes[i] / ma200 - 1) if ma200 else None

    # rsi
    rsi_ser = rsi14_series(closes)
    rsi_val = rsi_ser[i]

    # vol_ratio
    vol_20 = sma(vols, 20, i)
    vol_ratio = (vols[i] / vol_20) if vol_20 and vol_20 > 0 else None

    # atr_dist: Abstand in ATR-Einheiten vom 20d-Mittel
    atr_ser = atr14_series(highs, lows, closes)
    atr_val = atr_ser[i]
    ma20 = sma(closes, 20, i)
    atr_dist = (closes[i] - ma20) / atr_val if (atr_val and atr_val > 0 and ma20) else None

    return {
        **fwd,
        "ma200_pos": ma200_pos,
        "rsi_depth": rsi_val,
        "vol_ratio": vol_ratio,
        "atr_dist": atr_dist,
    }


# ---------------------------------------------------------------------------
# signal_outcomes Tabelle
# ---------------------------------------------------------------------------

def ensure_table(db):
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS signal_outcomes (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            signal_date   TEXT NOT NULL,
            instrument_id INTEGER NOT NULL,
            symbol        TEXT,
            signal_type   TEXT NOT NULL,
            conviction    TEXT,
            score         REAL,
            rsi_signal    REAL,
            status        TEXT,
            -- Forward Returns (netto, 1.1 % Kosten)
            fwd_1d        REAL,
            fwd_3d        REAL,
            fwd_5d        REAL,
            fwd_10d       REAL,
            -- Features
            ma200_pos     REAL,
            rsi_depth     REAL,
            bench_trend   REAL,
            vol_ratio     REAL,
            atr_dist      REAL,
            co_signals    INTEGER,
            cost          REAL DEFAULT 0.011,
            created_at    TEXT DEFAULT (datetime('now','utc')),
            UNIQUE(instrument_id, signal_date, signal_type)
        )
        """
    )
    db.execute(
        "CREATE INDEX IF NOT EXISTS idx_so_type ON signal_outcomes(signal_type)"
    )
    db.execute(
        "CREATE INDEX IF NOT EXISTS idx_so_date ON signal_outcomes(signal_date)"
    )
    db.commit()


def insert_outcomes(db, outcomes):
    """Idempotentes Insert (ON CONFLICT DO NOTHING)."""
    cols = (
        "signal_date, instrument_id, symbol, signal_type, conviction, "
        "score, rsi_signal, status, fwd_1d, fwd_3d, fwd_5d, fwd_10d, "
        "ma200_pos, rsi_depth, bench_trend, vol_ratio, atr_dist, co_signals, cost"
    )
    ph = ",".join("?" * 19)
    sql = (
        f"INSERT OR IGNORE INTO signal_outcomes ({cols}) "
        f"VALUES ({ph})"
    )
    n = 0
    for o in outcomes:
        db.execute(
            sql,
            (
                o["signal_date"],
                o["instrument_id"],
                o["symbol"],
                o["signal_type"],
                o["conviction"],
                o["score"],
                o["rsi_signal"],
                o["status"],
                o["fwd_1d"],
                o["fwd_3d"],
                o["fwd_5d"],
                o["fwd_10d"],
                o["ma200_pos"],
                o["rsi_depth"],
                o["bench_trend"],
                o["vol_ratio"],
                o["atr_dist"],
                o["co_signals"],
                COST,
            ),
        )
        n += 1
    db.commit()
    return n


# ---------------------------------------------------------------------------
# Verlustprofil TREND_PULLBACK (D3)
# ---------------------------------------------------------------------------

def trend_pullback_profile(db):
    """Aufschlüsselung des TREND_PULLBACK+GOLDEN_CROSS-Verlustprofils."""
    print("\n" + "=" * 70)
    print("VERLUSTPROFIL TREND_PULLBACK (D3)")
    print("=" * 70)

    base = (
        "SELECT * FROM signal_outcomes "
        "WHERE signal_type LIKE '%TREND_PULLBACK%'"
    )
    rows = db.execute(base).fetchall()
    cols = [d[0] for d in db.execute(base).description]

    def _stat(subset, label):
        fwd10 = [r[cols.index("fwd_10d")] for r in subset if r[cols.index("fwd_10d")] is not None]
        fwd5 = [r[cols.index("fwd_5d")] for r in subset if r[cols.index("fwd_5d")] is not None]
        n = len(subset)
        if n < 3:
            print(f"  {label:45s} n={n:4d}  (zu wenig)")
            return
        m10 = st.mean(fwd10) if fwd10 else 0
        m5 = st.mean(fwd5) if fwd5 else 0
        wr10 = 100 * sum(1 for x in fwd10 if x > 0) / len(fwd10) if fwd10 else 0
        print(
            f"  {label:45s} n={n:4d}  "
            f"fwd5={100*m5:+.2f}%  fwd10={100*m10:+.2f}%  WR10={wr10:.0f}%"
        )

    print(f"\n  Gesamt TREND_PULLBACK (alle Varianten): n={len(rows)}")
    _stat(rows, "GESAMT")

    # By conviction
    print("\n  --- Aufteilung nach Conviction ---")
    for conv in ("VERY_HIGH", "HIGH", "MEDIUM", "LOW"):
        sub = [r for r in rows if r[cols.index("conviction")] == conv]
        _stat(sub, conv)

    # By score bucket
    print("\n  --- Aufteilung nach Score-Band ---")
    for lo, hi, lbl in [(0, 50, "score 0-49"), (50, 70, "score 50-69"),
                        (70, 85, "score 70-84"), (85, 101, "score 85-100")]:
        sub = [r for r in rows if r[cols.index("score")] is not None
               and lo <= r[cols.index("score")] < hi]
        _stat(sub, lbl)

    # By ma200 position
    print("\n  --- Aufteilung nach MA200-Position ---")
    above = [r for r in rows if r[cols.index("ma200_pos")] is not None and r[cols.index("ma200_pos")] > 0]
    below = [r for r in rows if r[cols.index("ma200_pos")] is not None and r[cols.index("ma200_pos")] <= 0]
    _stat(above, "MA200 DARBEBER")
    _stat(below, "MA200 UNTER")

    # By benchmark trend
    print("\n  --- Aufteilung nach Benchmark-Trend (SPY 20d) ---")
    bull = [r for r in rows if r[cols.index("bench_trend")] is not None and r[cols.index("bench_trend")] > 0]
    bear = [r for r in rows if r[cols.index("bench_trend")] is not None and r[cols.index("bench_trend")] <= 0]
    _stat(bull, "SPY 20d POSITIV")
    _stat(bear, "SPY 20d NEGATIV")

    # By RSI depth
    print("\n  --- Aufteilung nach RSI-Tiefe ---")
    for lo, hi, lbl in [(0, 40, "RSI < 40"), (40, 55, "RSI 40-54"),
                        (55, 70, "RSI 55-69"), (70, 101, "RSI >= 70")]:
        sub = [r for r in rows if r[cols.index("rsi_depth")] is not None
               and lo <= r[cols.index("rsi_depth")] < hi]
        _stat(sub, lbl)

    # Co-signals
    print("\n  --- Aufteilung nach Ko-Signalen ---")
    for lo, hi, lbl in [(0, 1, "0 Ko-Signale"), (1, 3, "1-2 Ko-Signale"), (3, 999, "3+ Ko-Signale")]:
        sub = [r for r in rows if r[cols.index("co_signals")] is not None
               and lo <= r[cols.index("co_signals")] < hi]
        _stat(sub, lbl)

    # By exact signal_type
    print("\n  --- Nach exaktem Signal-Typ ---")
    types = sorted(set(r[cols.index("signal_type")] for r in rows))
    for t in types:
        sub = [r for r in rows if r[cols.index("signal_type")] == t]
        _stat(sub, t)


# ---------------------------------------------------------------------------
# Haupt
# ---------------------------------------------------------------------------

def main():
    print(f"Phase 1a — Forward-Returns  ({datetime.now().isoformat()})")
    print(f"  DB:     {DB}")
    print(f"  Bars:   {BARS}")
    print(f"  Kosten: {100*COST:.1f}% Round-Trip")
    print(f"  Lookback: {LOOKBACK_DAYS} Tage")

    db = sqlite3.connect(DB)
    db.row_factory = sqlite3.Row

    # 1. Bars laden
    print("\n[1/6] Bar-Dateien laden …")
    bar_cache = build_bar_cache(BARS)
    print(f"  {len(bar_cache)} Symbole geladen")

    # 2. Benchmark
    print("[2/6] Benchmark (SPY 20d) …")
    bench = build_benchmark(bar_cache)
    print(f"  {len(bench)} Datum-Einträge")

    # 3. Signale laden + dedup
    print("[3/6] Signale laden + dedup …")
    signals = load_signals(db)
    print(f"  {len(signals)} Signale nach Dedup (pro instrument_id, date, type)")

    # 4. Ko-Signal-Zählung
    print("[4/6] Ko-Signale zählen …")
    co_counts = count_co_signals(db, signals)
    print(f"  {len(co_counts)} (instrument, date) Paare")

    # 5. Forward Returns + Features berechnen
    print("[5/6] Forward Returns + Features berechnen …")
    imap_raw = json.load(open(IMAP))
    imap = imap_raw["map"]
    outcomes = []
    no_bar = 0
    no_date = 0
    for r in signals:
        inst_id = r[1]
        sym = imap.get(str(inst_id))
        if sym not in bar_cache:
            no_bar += 1
            continue
        sig_date = r[2][:10]
        feats = compute_forward_and_features(bar_cache[sym], sig_date, r[9])
        if feats is None:
            no_date += 1
            continue
        outcomes.append({
            "signal_date": sig_date,
            "instrument_id": inst_id,
            "symbol": sym,
            "signal_type": r[3],
            "conviction": r[4],
            "score": r[5],
            "rsi_signal": r[6],
            "status": r[10],
            "bench_trend": bench.get(sig_date),
            "co_signals": max(0, co_counts.get((inst_id, sig_date), 0) - 1),
            **feats,
        })
    print(f"  {len(outcomes)} Outcomes berechnet "
          f"(ohne Bar: {no_bar}, ohne Datum: {no_date})")

    # 6. Tabelle + Insert
    print("[6/6] signal_outcomes Tabelle + Insert …")
    ensure_table(db)
    n = insert_outcomes(db, outcomes)
    total = db.execute("SELECT COUNT(*) FROM signal_outcomes").fetchone()[0]
    print(f"  {n} Zeilen (neu), {total} gesamt in signal_outcomes")

    # Status-Verteilung
    print("\n  Status-Verteilung in signal_outcomes:")
    for row in db.execute(
        "SELECT status, COUNT(*) c FROM signal_outcomes GROUP BY status ORDER BY c DESC"
    ):
        print(f"    {row[0]:12s} {row[1]}")

    # Signal-Typ-Verteilung
    print("\n  Signal-Typ-Verteilung (top 10):")
    for row in db.execute(
        "SELECT signal_type, COUNT(*) c FROM signal_outcomes "
        "GROUP BY signal_type ORDER BY c DESC LIMIT 10"
    ):
        print(f"    {row[0]:45s} {row[1]}")

    # Fwd-Return-Statistik (10d)
    print("\n  Forward-Return 10d (netto) — Gesamt:")
    rows10 = [r[0] for r in db.execute(
        "SELECT fwd_10d FROM signal_outcomes WHERE fwd_10d IS NOT NULL"
    )]
    if rows10:
        print(f"    n={len(rows10)}  mean={100*st.mean(rows10):+.3f}%  "
              f"median={100*st.median(rows10):+.3f}%  "
              f"WR={100*sum(1 for x in rows10 if x>0)/len(rows10):.0f}%")

    # Verlustprofil TREND_PULLBACK
    trend_pullback_profile(db)

    # Übersicht: Signal-Typ × fwd_10d
    print("\n" + "=" * 70)
    print("SIGNAL-TYP × FORWARD-RETURN 10d (netto)")
    print("=" * 70)
    for row in db.execute(
        """
        SELECT signal_type,
               COUNT(*) n,
               AVG(fwd_10d) mean10,
               MIN(fwd_10d) min10,
               MAX(fwd_10d) max10
        FROM signal_outcomes
        WHERE fwd_10d IS NOT NULL
        GROUP BY signal_type
        ORDER BY n DESC
        """
    ):
        wr = db.execute(
            "SELECT COUNT(*) FROM signal_outcomes WHERE signal_type=? AND fwd_10d > 0",
            (row[0],),
        ).fetchone()[0]
        n = row[1]
        print(
            f"  {row[0]:45s} n={n:4d}  "
            f"mean={100*row[2]:+.2f}%  min={100*row[3]:+.2f}%  "
            f"max={100*row[4]:+.2f}%  WR={100*wr/n:.0f}%"
        )

    db.close()
    print("\nFertig.")


if __name__ == "__main__":
    main()
