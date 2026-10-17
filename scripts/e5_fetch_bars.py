#!/usr/bin/env python3
"""E5 — OHLCV fuer das E4-Universum + SPY laden (nur lesend gegen die DB).

Vorregistrierung: docs/vorregistrierung-e5-signale-exits-gebuehren-2026-10-17.md
Universum = Dateien in data/e4_bars/ (Kopfzeile "# <symbol> <yfinance>").
Schreibt data/e5_bars/<instrument_id>.csv (date,open,high,low,close,volume).

  /usr/bin/python3 scripts/e5_fetch_bars.py
"""
from __future__ import annotations

import glob
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "data", "e4_bars")
OUT = os.path.join(ROOT, "data", "e5_bars")
START, END = "2019-01-01", "2026-10-16"


def universe() -> list[tuple[str, str, str]]:
    out = [("SPY", "SPY", "SPY")]
    for f in sorted(glob.glob(os.path.join(SRC, "*.csv"))):
        with open(f) as fh:
            head = fh.readline().strip().lstrip("#").split()
        if len(head) >= 2:
            out.append((os.path.basename(f)[:-4], head[0], head[1]))
    return out


def main() -> int:
    import yfinance as yf
    os.makedirs(OUT, exist_ok=True)
    todo = [u for u in universe() if not os.path.exists(os.path.join(OUT, f"{u[0]}.csv"))]
    print(f"zu laden: {len(todo)}")
    ok = fail = 0
    for k in range(0, len(todo), 50):
        chunk = todo[k:k + 50]
        tickers = [u[2] for u in chunk]
        try:
            df = yf.download(tickers, start=START, end=END, auto_adjust=True,
                             progress=False, group_by="ticker", threads=True)
        except Exception as exc:  # noqa: BLE001
            print(f"  Chunk {k}: {exc}"); fail += len(chunk); continue
        for key, sym, t in chunk:
            try:
                sub = (df[t] if len(tickers) > 1 else df)[["Open", "High", "Low", "Close", "Volume"]].dropna()
            except Exception:  # noqa: BLE001
                fail += 1; continue
            if len(sub) < 50:
                fail += 1; continue
            with open(os.path.join(OUT, f"{key}.csv"), "w") as fh:
                fh.write(f"# {sym} {t}\ndate,open,high,low,close,volume\n")
                for d, r in sub.iterrows():
                    fh.write(f"{d.strftime('%Y-%m-%d')},{r.Open},{r.High},{r.Low},{r.Close},{r.Volume}\n")
            ok += 1
        print(f"  {min(k + 50, len(todo))}/{len(todo)} — ok {ok}, fehlend {fail}")
        time.sleep(1.0)
    print(f"Fertig: {ok} geladen, {fail} ohne Daten")
    return 0


if __name__ == "__main__":
    sys.exit(main())
