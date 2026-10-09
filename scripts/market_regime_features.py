#!/usr/bin/env python3
"""Phase 1d — Marktregime-Feature: ADX/ATR auf BENCHMARK-Ebene.

Hintergrund (PLAN 1d): Die Konto-Regime-Spalte (NORMAL/CAUTION/DEFENSIVE)
erklärt nichts — alle drei Regimes sind in den Outcomes negativ. Was
fehlt, ist ein **Marktregeim-Feature** auf BENCHMARK-Ebene (SPY):

  - **bench_adx**    : ADX(14) des SPY — Trendstärke (0-100).
                       >25 = trendend, <20 = range-bound. Sagt, ob der
                       Markt gerade ein Trend- oder Seitwärtsregime ist.
  - **bench_atr_pct**: ATR(14)/Close in % des SPY — Marktvolatilität.

Die Feature-Werte werden pro `entry_quality_events`-Zeile per Datum
gesetzt (LETZTE SPY-BAR streng VOR dem Event-Datum = kein Look-ahead:
das Signal feuert intraday asiatisch/europäisch, die US-BAR des Tages
ist noch offen).

Idempotent: ADD COLUMN via PRAGMA-Check; UPDATE ist stabil. `ta`-Lib
(ADX/ATR) — falls nicht verfügbar, fail-open (Spalten bleiben NULL).
"""
from __future__ import annotations

import csv
import glob
import os
import sqlite3
from bisect import bisect_left

DB = os.path.join(os.path.dirname(__file__), "..", "data", "trading.db")
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Alle SPY-Bar-Quellen (werden gemerged + dedupliziert)
SPY_SOURCES = [
    os.path.join(REPO, "data", "backtest_cache"),
    os.path.join(REPO, "data", "exit_replay_bars"),
    os.path.join(REPO, "data", "ohlcv_daily"),
]
ADX_PERIOD = 14
ATR_PERIOD = 14


# ---------------------------------------------------------------------------
# SPY-Bars laden + mergen
# ---------------------------------------------------------------------------
def load_spy_bars_for(sources: list[str]) -> list[tuple[str, float, float, float]]:
    """Alle SPY-Bar-Quellen → {date: (high, low, close)} → sortierte Liste.

    Rückgabe: [(date, high, low, close), ...] aufsteigend nach Datum,
    dedupliziert (spätere Quelle überschreibt bei Kollision — gleiche
    Datenquelle, gleiche Werte).
    """
    by_date: dict[str, tuple[float, float, float]] = {}
    for d in sources:
        if not os.path.isdir(d):
            continue
        for f in glob.glob(os.path.join(d, "SPY*.csv")):
            try:
                with open(f, newline="") as fh:
                    rd = csv.reader(fh)
                    header = next(rd, None)
                    if not header:
                        continue
                    hdr = [h.strip().lower() for h in header]
                    di = hdr.index("date")
                    hi = hdr.index("high")
                    li = hdr.index("low")
                    ci = hdr.index("close")
                    for row in rd:
                        if len(row) <= max(di, hi, li, ci):
                            continue
                        try:
                            dt = row[di][:10]
                            h = float(row[hi])
                            l = float(row[li])
                            c = float(row[ci])
                        except (ValueError, IndexError):
                            continue
                        if h > 0 and l > 0 and c > 0:
                            by_date[dt] = (h, l, c)
            except Exception:
                continue
    return sorted((d, v[0], v[1], v[2]) for d, v in by_date.items())


def load_spy_bars() -> list[tuple[str, float, float, float]]:
    """Alle konfigurierten SPY-Quellen mergen."""
    return load_spy_bars_for(SPY_SOURCES)


# ---------------------------------------------------------------------------
# ADX / ATR (Wilder) — ta-Lib, fail-open
# ---------------------------------------------------------------------------
def compute_adx_atr(bars: list[tuple[str, float, float, float]]):
    """{date: (adx, atr_pct)} — ta-Lib. Bei Fehler → {} (fail-open)."""
    if len(bars) < 30:
        return {}
    try:
        import pandas as pd
        import ta
    except ImportError:
        return {}
    df = pd.DataFrame(bars, columns=["date", "high", "low", "close"])
    df["date"] = pd.to_datetime(df["date"])
    df = df.set_index("date")
    try:
        adx = ta.trend.ADXIndicator(
            df["high"], df["low"], df["close"], window=ADX_PERIOD
        ).adx()
        atr = ta.volatility.AverageTrueRange(
            df["high"], df["low"], df["close"], window=ATR_PERIOD
        ).average_true_range()
    except Exception:
        return {}
    out: dict[str, tuple[float, float]] = {}
    for i, d in enumerate(df.index):
        a = adx.iloc[i] if adx is not None else None
        t = atr.iloc[i] if atr is not None else None
        c = df["close"].iloc[i]
        # Warmup-Rows: ta gibt 0.0 (nicht NaN) zurück → nur echte Werte behalten
        if a is None or t is None or pd.isna(a) or pd.isna(t):
            continue
        if not (a > 0 and t > 0 and c):
            continue
        out[d.strftime("%Y-%m-%d")] = (float(a), float(t / c * 100.0))
    return out


# ---------------------------------------------------------------------------
# DB
# ---------------------------------------------------------------------------
def _has_col(db: sqlite3.Connection, col: str) -> bool:
    return any(r[1] == col for r in db.execute("PRAGMA table_info(entry_quality_events)"))


def add_columns(db: sqlite3.Connection) -> None:
    for col in ("bench_adx", "bench_atr_pct"):
        if not _has_col(db, col):
            db.execute(f"ALTER TABLE entry_quality_events ADD COLUMN {col} REAL")
    db.commit()


def populate(db: sqlite3.Connection, feat: dict[str, tuple[float, float]]) -> int:
    """Jede Event-Zeile ← letzte SPY-BAR streng VOR created_at-Datum."""
    if not feat:
        return 0
    dates = sorted(feat.keys())
    rows = db.execute(
        "SELECT id, created_at FROM entry_quality_events WHERE created_at IS NOT NULL"
    ).fetchall()
    updated = 0
    for eid, created in rows:
        d = (created or "")[:10]
        # letzte SPY-BAR streng VOR d (kein Look-ahead)
        i = bisect_left(dates, d) - 1
        if i < 0:
            continue
        adx, atr_pct = feat[dates[i]]
        db.execute(
            "UPDATE entry_quality_events SET bench_adx=?, bench_atr_pct=? WHERE id=?",
            (adx, atr_pct, eid),
        )
        updated += 1
    db.commit()
    return updated


def report(db: sqlite3.Connection) -> None:
    tot = db.execute("SELECT COUNT(*) FROM entry_quality_events").fetchone()[0]
    cov = db.execute(
        "SELECT COUNT(*) FROM entry_quality_events WHERE bench_adx IS NOT NULL"
    ).fetchone()[0]
    print(f"\n  Coverage: {cov}/{tot} Events ({100*cov/tot:.0f} %)")
    r = db.execute(
        "SELECT MIN(bench_adx), MAX(bench_adx), AVG(bench_adx), "
        "AVG(bench_atr_pct) FROM entry_quality_events WHERE bench_adx IS NOT NULL"
    ).fetchone()
    print(f"  bench_adx:    min={r[0]:.1f}  max={r[1]:.1f}  mean={r[2]:.1f}")
    print(f"  bench_atr_pct: mean={r[3]:.2f} %")

    # Regime-Mix: ADX-Level × Konto-Regime (erklärt der Marktregeim mehr?)
    print("\n  ADX-Level × Konto-Regime (n Events):")
    print(f"  {'regime':12s} {'range(<20)':>12s} {'neutral(20-25)':>16s} {'trend(>25)':>12s}")
    for regime in ("NORMAL", "CAUTION", "DEFENSIVE", "CRITICAL"):
        row = db.execute(
            "SELECT "
            "SUM(CASE WHEN bench_adx<20 THEN 1 ELSE 0 END), "
            "SUM(CASE WHEN bench_adx>=20 AND bench_adx<=25 THEN 1 ELSE 0 END), "
            "SUM(CASE WHEN bench_adx>25 THEN 1 ELSE 0 END), COUNT(*) "
            "FROM entry_quality_events WHERE regime=? AND bench_adx IS NOT NULL",
            (regime,),
        ).fetchone()
        if row[3]:
            print(f"  {regime:12s} {row[0]:12d} {row[1]:16d} {row[2]:12d}   (n={row[3]})")


def main() -> None:
    print("=" * 64)
    print("Phase 1d — Marktregime-Feature (Benchmark ADX/ATR)")
    print("=" * 64)
    print("\n[1/4] SPY-Bars laden (alle Quellen, merged) …")
    bars = load_spy_bars()
    if bars:
        print(f"  {len(bars)} SPY-Tage: {bars[0][0]} … {bars[-1][0]}")
    else:
        print("  KEINE SPY-Bars gefunden — Abbruch.")
        return

    print("\n[2/4] ADX(14) + ATR(14) berechnen …")
    feat = compute_adx_atr(bars)
    print(f"  {len(feat)} Datenpunkte mit ADX/ATR")

    db = sqlite3.connect(DB)
    db.row_factory = sqlite3.Row
    print("\n[3/4] Spalten + Populate (letzte SPY-BAR vor Event-Datum) …")
    add_columns(db)
    updated = populate(db, feat)
    print(f"  {updated} Events aktualisiert")

    print("\n[4/4] Report …")
    report(db)
    db.close()
    print("\nFERTIG. bench_adx + bench_atr_pct sind in entry_quality_events.")


if __name__ == "__main__":
    main()
