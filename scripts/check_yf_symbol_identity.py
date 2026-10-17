#!/usr/bin/env python3
"""Pruefung: Rechnet der Bot fuer jedes aktive Instrument mit den Kursen der RICHTIGEN Firma?

Hintergrund (17.10.2026): instruments.yfinance_symbol ist bei 5 704 von 6 266
US-Aktien nicht der eToro-Ticker, teils eine ANDERE Firma (CLPT -> CNI,
BRBI -> BAP). Kursdaten (ohlcv_daily) kommen ueber dieses Symbol.

Vergleich je Instrument (Watchlist, offene Positionen, Signale 30 Tage):
  letzter Close in ohlcv_daily  vs.  eToro-Kurs (Batch-Rates, nur lesend)
  Verhaeltnis ~1          -> ok
  ~100 oder ~0,01         -> Einheit (Pence/Pfund), gleiche Firma
  sonst (> 15 % daneben)  -> VERDACHT falsche Firma / falsche Boerse
Ausgabe: Tabelle der Verdachtsfaelle; --csv schreibt alle Zeilen.

  PYTHONPATH=src /usr/bin/python3 scripts/check_yf_symbol_identity.py [--csv out.csv]
"""
from __future__ import annotations

import argparse
import csv
import math
import os
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
DB = ROOT / "data" / "trading.db"
TOL = 0.15


def classify(ratio: float | None) -> str:
    if ratio is None or ratio <= 0:
        return "keine Daten"
    for unit, label in ((1.0, "ok"), (100.0, "Einheit x100"), (0.01, "Einheit /100")):
        if abs(math.log(ratio / unit)) <= math.log(1 + TOL):
            return label
    return "VERDACHT"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv")
    args = ap.parse_args()
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    ids = {r[0] for r in con.execute("SELECT instrument_id FROM watchlist")}
    ids |= {r[0] for r in con.execute("SELECT instrument_id FROM portfolio_snapshot WHERE instrument_id IS NOT NULL")}
    ids |= {r[0] for r in con.execute(
        "SELECT DISTINCT instrument_id FROM signals WHERE generated_at > datetime('now','-30 days')")}
    ids.discard(None)
    meta = {r["instrument_id"]: dict(r) for r in con.execute(
        f"SELECT instrument_id, symbol, yfinance_symbol, name, asset_class, yahoo_status FROM instruments "
        f"WHERE instrument_id IN ({','.join('?' * len(ids))})", list(ids))}
    last = {}
    for r in con.execute(
            f"SELECT o.instrument_id, o.date, o.close FROM ohlcv_daily o JOIN ("
            f" SELECT instrument_id, MAX(date) d FROM ohlcv_daily WHERE instrument_id IN ({','.join('?' * len(ids))})"
            f" GROUP BY instrument_id) m ON m.instrument_id = o.instrument_id AND m.d = o.date", list(ids)):
        last[r["instrument_id"]] = (r["date"], float(r["close"] or 0))
    held = {r[0] for r in con.execute("SELECT instrument_id FROM portfolio_snapshot")}

    import yaml
    from bot.api.client import ClientConfig, EToroClient
    cfg = yaml.safe_load((ROOT / "config" / "config.yaml").read_text()) or {}
    client = EToroClient(api_key=os.environ["ETORO_BOT_API_KEY"], user_key=os.environ["ETORO_BOT_USER_KEY"],
                         config=ClientConfig.from_dict(cfg.get("api", {})))
    rates = client.get_rates_batch(sorted(ids))

    rows = []
    for iid in sorted(ids):
        m = meta.get(iid, {})
        r = rates.get(int(iid)) or {}
        px = None
        for k in ("lastExecution", "bid", "ask"):
            try:
                if r.get(k):
                    px = float(r[k]); break
            except (TypeError, ValueError):
                pass
        d, c = last.get(iid, (None, None))
        ratio = (c / px) if (c and px) else None
        rows.append(dict(instrument_id=iid, symbol=m.get("symbol"), yf=m.get("yfinance_symbol"),
                         name=m.get("name"), asset_class=m.get("asset_class"), held=iid in held,
                         ohlcv_date=d, ohlcv_close=c, etoro=px,
                         ratio=round(ratio, 4) if ratio else None, verdict=classify(ratio)))
    if args.csv:
        with open(args.csv, "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=list(rows[0]))
            w.writeheader(); w.writerows(rows)
    from collections import Counter
    print("Instrumente:", len(rows), dict(Counter(r["verdict"] for r in rows)))
    for r in sorted(rows, key=lambda r: (not r["held"], r["verdict"] != "VERDACHT", str(r["symbol"]))):
        if r["verdict"] == "VERDACHT":
            print(f"  {'[POSITION] ' if r['held'] else ''}{r['symbol']:12s} yf={str(r['yf']):10s} "
                  f"ohlcv {r['ohlcv_close']} ({r['ohlcv_date']}) vs eToro {r['etoro']}  "
                  f"x{r['ratio']}  {str(r['name'])[:40]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
