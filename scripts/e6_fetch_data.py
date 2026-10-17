#!/usr/bin/env python3
"""E6 — Daten laden: Earnings-Termine/Ueberraschungen, Analysten-Ratings, OHLCV.

Universum: S&P 500 + S&P 400 (Liste Wikipedia, Stand Abruf) geschnitten mit den
bei eToro handelbaren US-Aktien (instruments, is_tradable=1, ohne Boersen-Suffix).
Cache: data/e6_cache/{earn,ratings,bars}/<TICKER>.csv. Nur lesend gegen trading.db.

  /usr/bin/python3 scripts/e6_fetch_data.py <sp1500.json>
"""
from __future__ import annotations

import io
import json
import os
import sqlite3
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(ROOT, "data", "trading.db")
OUT = os.path.join(ROOT, "data", "e6_cache")


def universe(sp_path: str) -> list[str]:
    sp = json.load(open(sp_path))
    names = set(sp["sp500"]) | set(sp["sp400"])
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    tradable = {r[0].upper().replace(".", "-") for r in con.execute(
        "SELECT symbol FROM instruments WHERE asset_class='stock' AND is_tradable=1 "
        "AND symbol NOT LIKE '%.%'")}
    con.close()
    return sorted(n for n in names if n.upper() in tradable)


def main() -> int:
    import yfinance as yf
    uni = universe(sys.argv[1])
    for sub in ("earn", "ratings", "bars"):
        os.makedirs(os.path.join(OUT, sub), exist_ok=True)
    json.dump(uni, open(os.path.join(OUT, "universe.json"), "w"))
    print(f"Universum: {len(uni)} Titel")
    # OHLCV in Batches (SPY als Referenz dabei)
    todo = [t for t in uni + ["SPY"] if not os.path.exists(os.path.join(OUT, "bars", f"{t}.csv"))]
    for k in range(0, len(todo), 50):
        chunk = todo[k:k + 50]
        df = yf.download(chunk, start="2011-01-01", end="2026-10-16", auto_adjust=True,
                         progress=False, group_by="ticker", threads=True)
        for t in chunk:
            try:
                sub = (df[t] if len(chunk) > 1 else df)[["Open", "High", "Low", "Close", "Volume"]].dropna()
                if len(sub) > 50:
                    sub.to_csv(os.path.join(OUT, "bars", f"{t}.csv"))
            except Exception:  # noqa: BLE001
                pass
        print(f"  Kurse {min(k + 50, len(todo))}/{len(todo)}")
        time.sleep(1)
    # Earnings + Ratings je Titel (gedrosselt)
    n_e = n_r = 0
    for i, t in enumerate(uni):
        pe = os.path.join(OUT, "earn", f"{t}.csv")
        pr = os.path.join(OUT, "ratings", f"{t}.csv")
        # Leere Datei = frueherer Fehler oder keine Daten -> erneut versuchen
        # (sonst wuerden voruebergehende Rate-Limit-Fehler zu Dauerluecken).
        if (os.path.exists(pe) and os.path.getsize(pe) > 0
                and os.path.exists(pr) and os.path.getsize(pr) > 0):
            continue
        tk = yf.Ticker(t)
        try:
            ed = tk.get_earnings_dates(limit=100)
            if ed is not None and len(ed):
                ed.to_csv(pe); n_e += 1
            else:
                open(pe, "w").close()
        except Exception:  # noqa: BLE001
            open(pe, "w").close()
        try:
            ud = tk.upgrades_downgrades
            if ud is not None and len(ud):
                ud.to_csv(pr); n_r += 1
            else:
                open(pr, "w").close()
        except Exception:  # noqa: BLE001
            open(pr, "w").close()
        if i % 50 == 0:
            print(f"  Earnings/Ratings {i}/{len(uni)} (neu: {n_e}/{n_r})")
        time.sleep(0.4)
    print("Fertig.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
