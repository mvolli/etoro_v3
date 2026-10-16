#!/usr/bin/env python3
"""E4 — Kurshistorie fuer das Arbeitsuniversum laden (nur lesend gegen die DB).

Vorregistrierung: docs/vorregistrierung-e4-trendfolge-2026-10-16.md
Universum: Aktien/ETFs mit Kaufsignal seit 2026-07-13, auflösbares yfinance_symbol.
Schreibt data/e4_bars/<instrument_id>.csv (date,close), 2019-01-01 .. 2026-10-15.

  /usr/bin/python3 scripts/e4_fetch_bars.py
"""
from __future__ import annotations

import os
import sqlite3
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(ROOT, "data", "trading.db")
OUT = os.path.join(ROOT, "data", "e4_bars")
START, END = "2019-01-01", "2026-10-16"   # END exklusiv -> letzte Kerze 15.10.


def universe() -> list[tuple[int, str, str]]:
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    rows = con.execute(
        """
        SELECT DISTINCT i.instrument_id, i.symbol, i.yfinance_symbol
        FROM signals s JOIN instruments i ON i.instrument_id = s.instrument_id
        WHERE s.generated_at >= '2026-07-13'
          AND i.asset_class IN ('stock', 'etf')
          AND s.signal_type NOT LIKE '%SELL%'
          AND s.signal_type NOT LIKE 'BB_UPPER%'
          AND s.signal_type NOT LIKE 'CORE%'
          AND i.yfinance_symbol IS NOT NULL AND i.yfinance_symbol != ''
        """
    ).fetchall()
    con.close()
    out = []
    for iid, sym, yf in rows:
        # Kaputte Mappings verwerfen (IMA_4002, Krypto-Ticker bei Aktien).
        if "_" in yf or yf.endswith("-USD") or yf.endswith("=X") or yf.startswith("^"):
            continue
        out.append((iid, sym, yf))
    return out


def main() -> int:
    import yfinance as yf
    os.makedirs(OUT, exist_ok=True)
    uni = universe()
    todo = [u for u in uni if not os.path.exists(os.path.join(OUT, f"{u[0]}.csv"))]
    print(f"Universum {len(uni)} Titel, zu laden {len(todo)}")
    ok = fail = 0
    for k in range(0, len(todo), 50):
        chunk = todo[k:k + 50]
        tickers = [u[2] for u in chunk]
        try:
            df = yf.download(tickers, start=START, end=END, auto_adjust=True,
                             progress=False, group_by="ticker", threads=True)
        except Exception as exc:  # noqa: BLE001
            print(f"  Chunk {k}: Fehler {exc}")
            fail += len(chunk)
            continue
        for iid, sym, t in chunk:
            try:
                sub = df[t] if len(tickers) > 1 else df
                closes = sub["Close"].dropna()
            except Exception:  # noqa: BLE001
                fail += 1
                continue
            if len(closes) < 50:
                fail += 1
                continue
            with open(os.path.join(OUT, f"{iid}.csv"), "w") as fh:
                fh.write(f"# {sym} {t}\ndate,close\n")
                for d, c in closes.items():
                    fh.write(f"{d.strftime('%Y-%m-%d')},{float(c)}\n")
            ok += 1
        print(f"  {min(k + 50, len(todo))}/{len(todo)} — ok {ok}, fehlend {fail}")
        time.sleep(1.0)
    print(f"Fertig: {ok} geladen, {fail} ohne Daten")
    return 0


if __name__ == "__main__":
    sys.exit(main())
