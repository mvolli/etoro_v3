#!/usr/bin/env python3
"""E7 — OHLCV fuer das Universum S&P 500 + 400 + 600 (eToro-handelbar) ab 2006.

Der E6-Cache beginnt 2011; die E7-Achse beginnt laut Vorregistrierung 2006-04.
Deshalb eigener Cache data/e7_cache/bars/ (gleiche Quelle/Einstellungen wie E6:
yfinance, auto_adjust). Nur lesend gegen trading.db.

  /usr/bin/python3 scripts/e7_fetch_bars.py <sp1500.json>
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(ROOT, "data", "trading.db")
OUT = os.path.join(ROOT, "data", "e7_cache")


def universe(sp_path: str) -> dict[str, str]:
    """Ticker -> Index ('large_mid' fuer S&P 500/400, 'small' fuer S&P 600)."""
    sp = json.load(open(sp_path))
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    tradable = {r[0].upper().replace(".", "-") for r in con.execute(
        "SELECT symbol FROM instruments WHERE asset_class='stock' AND is_tradable=1 "
        "AND symbol NOT LIKE '%.%'")}
    con.close()
    out: dict[str, str] = {}
    for key, seg in (("sp600", "small"), ("sp400", "large_mid"), ("sp500", "large_mid")):
        for n in sp[key]:
            t = n.upper().replace(".", "-")
            if t in tradable:
                out[t] = seg
    return dict(sorted(out.items()))


def main() -> int:
    import yfinance as yf
    uni = universe(sys.argv[1])
    os.makedirs(os.path.join(OUT, "bars"), exist_ok=True)
    json.dump(uni, open(os.path.join(OUT, "universe.json"), "w"))
    seg = {s: sum(1 for v in uni.values() if v == s) for s in ("large_mid", "small")}
    print(f"Universum: {len(uni)} Titel {seg}")
    todo = [t for t in list(uni) + ["SPY"] if not os.path.exists(os.path.join(OUT, "bars", f"{t}.csv"))]
    for k in range(0, len(todo), 50):
        chunk = todo[k:k + 50]
        df = yf.download(chunk, start="2006-01-01", end="2026-10-16", auto_adjust=True,
                         progress=False, group_by="ticker", threads=True)
        for t in chunk:
            try:
                sub = (df[t] if len(chunk) > 1 else df)[["Open", "High", "Low", "Close", "Volume"]].dropna()
                if len(sub) > 50:
                    sub.to_csv(os.path.join(OUT, "bars", f"{t}.csv"))
            except Exception:  # noqa: BLE001
                pass
        print(f"  Kurse {min(k + 50, len(todo))}/{len(todo)}", flush=True)
        time.sleep(1)
    print("Fertig.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
