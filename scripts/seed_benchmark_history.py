#!/usr/bin/env python3
"""Benchmark-Historie (SPY) in ohlcv_daily auffuellen — feat/market-regime (2026-10-16).

Das Marktregime braucht >= 252 Handelstage SPY (SMA200 + 52-Wochen-Hoch).
ohlcv_daily hatte SPY erst ab 2026-06-12. Quelle: data/exit_replay_bars/SPY.csv
(yfinance auto_adjust, identisch zur Bereinigung in ohlcv_daily an den
Ueberlappungstagen). INSERT OR IGNORE auf UNIQUE(instrument_id, date):
vorhandene Zeilen werden nie ueberschrieben, mehrfaches Ausfuehren ist harmlos.

  PYTHONPATH=src python3 scripts/seed_benchmark_history.py          # Bericht
  PYTHONPATH=src python3 scripts/seed_benchmark_history.py --write  # schreiben
"""
from __future__ import annotations

import argparse
import csv
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "trading.db"
SOURCES = {3000: ROOT / "data" / "exit_replay_bars" / "SPY.csv"}


def rows_from_csv(path: Path) -> list[tuple]:
    out = []
    with path.open() as fh:
        rd = csv.reader(fh)
        hdr = [h.strip().lower() for h in next(rd)]
        ix = {k: hdr.index(k) for k in ("date", "open", "high", "low", "close", "volume")}
        for r in rd:
            try:
                close = float(r[ix["close"]])
                out.append((r[ix["date"]][:10], float(r[ix["open"]]), float(r[ix["high"]]),
                            float(r[ix["low"]]), close, int(float(r[ix["volume"]] or 0)), close))
            except (ValueError, IndexError):
                continue
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true")
    args = ap.parse_args()
    con = sqlite3.connect(str(DB) if args.write else f"file:{DB}?mode=ro", uri=not args.write)
    for iid, path in SOURCES.items():
        rows = rows_from_csv(path)
        have = {d for (d,) in con.execute("SELECT substr(date,1,10) FROM ohlcv_daily WHERE instrument_id=?", (iid,))}
        new = [r for r in rows if r[0] not in have]
        print(f"instrument {iid}: Quelle {len(rows)} Tage, vorhanden {len(have)}, neu {len(new)}")
        if args.write and new:
            con.executemany(
                "INSERT OR IGNORE INTO ohlcv_daily (instrument_id, date, open, high, low, close, volume, adjusted_close) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)", [(iid,) + r for r in new])
            con.commit()
        n, lo, hi = con.execute("SELECT COUNT(*), MIN(date), MAX(date) FROM ohlcv_daily WHERE instrument_id=?", (iid,)).fetchone()
        print(f"  ohlcv_daily jetzt: {n} Tage, {lo} -> {hi}")
    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
